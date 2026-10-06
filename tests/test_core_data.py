"""DataUploader 远程目录操作的单元测试。

使用假的 SFTP/Transport 替换 _connect_sftp，不依赖真实网络与远程主机。
"""
import io
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import paramiko
import pytest

from data_collector.core import ConfigManager, save_yaml
from data_collector.core_data import (DataUploader, LogFetcher, RemoteAuthError,
                                     UploadCancelledError)
from data_collector.errors import ErrorCode

BASE = "/remote/base"
DIR_MODE = 0o040755
FILE_MODE = 0o100644


class FakeAttr:
    def __init__(self, filename, is_dir=False, size=0, mtime=0):
        self.filename = filename
        self.st_mode = DIR_MODE if is_dir else FILE_MODE
        self.st_size = size
        self.st_mtime = mtime


class FakeTransport:
    def __init__(self):
        self.closed = False
        self.connect_kwargs = None

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def close(self):
        self.closed = True


class FakeRemoteFile:
    """模拟 paramiko 的 SFTPFile：支持 seek/write/set_pipelined。

    `r+b` 的语义是「可读可写、**不截断、不追加**」，续传正是靠它把剩余字节
    写到已有内容的后面。这里如实照做，避免测试通过而真机错乱。
    """

    def __init__(self, sftp, path, mode):
        self.sftp = sftp
        self.path = path
        self.mode = mode
        self.buf = bytearray(sftp.remote_files.get(path, b""))
        self.pos = 0
        self.pipelined = None

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset
        return self.pos

    def write(self, data):
        if self.pos > len(self.buf):
            raise IOError("不支持在文件末尾之外留空洞")
        self.buf[self.pos:self.pos + len(data)] = data
        self.pos += len(data)
        return len(data)

    def set_pipelined(self, flag=True):
        self.pipelined = flag

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.sftp.remote_files[self.path] = bytes(self.buf)
        return False


class FakeSFTP:
    """按 {目录: [FakeAttr]} 描述的假文件系统。"""

    def __init__(self, tree=None, stats=None):
        self.tree = tree or {}
        self.stats = stats or {}
        self.puts = []
        self.removed = []
        self.mkdirs = []
        self.closed = False
        self.remote_files = {}  # 由 exec 通道（cat > file）写出的“远端文件”
        self.opened = []        # open() 的调用记录：(路径, 模式)
        self.put_fail_after = None   # 只写前 N 字节后报错（模拟传输中断，用于续传测试）
        self.put_drop_tail = 0       # 落盘时少写尾部 N 字节（模拟截断，用于校验测试）

    def listdir_attr(self, path):
        if path not in self.tree:
            raise IOError(f"no such directory: {path}")
        return self.tree[path]

    def stat(self, path):
        if path in self.stats:
            return self.stats[path]
        if path in self.remote_files:
            return FakeAttr(Path(path).name, size=len(self.remote_files[path]))
        raise IOError(f"no such file: {path}")

    def mkdir(self, path):
        self.mkdirs.append(path)

    def put(self, local, remote, callback=None):
        """上传整个文件。

        三个可注入的故障钩子（默认全关，不影响既有测试）：
        - `put_fail_after`：只落下前 N 字节就报错，模拟"传一半断掉"（用于验证续传）；
        - `put_drop_tail`：落盘时尾部少 N 字节但不报错，模拟"传完却被截断"
          （用于验证完整性校验真的能发现）；
        - 两者都只生效一次，便于断言"重试后成功"。
        """
        data = Path(local).read_bytes()
        size = len(data)
        self.puts.append((local, remote))

        if self.put_fail_after is not None:
            limit = self.put_fail_after
            self.put_fail_after = None
            if callback:
                callback(limit, size)
            self.remote_files[remote] = data[:limit]
            raise IOError(f"连接中断（已传 {limit} 字节）")

        if self.put_drop_tail:
            drop = self.put_drop_tail
            self.put_drop_tail = 0
            if callback:
                callback(0, size)
                callback(size, size)
            self.remote_files[remote] = data[: size - drop]
            return

        if callback:
            callback(0, size)
            callback(size, size)
        self.remote_files[remote] = data

    def open(self, path, mode="r", bufsize=-1):
        """模拟 paramiko 的 SFTPClient.open：`r+b` = 可读可写且**不截断**。

        续传依赖这个语义（见 DataUploader._put_from_offset），所以假的也必须
        照做 —— 如果这里退化成"截断重写"，测试会假装通过而真机是坏的。
        """
        if "w" in mode:
            self.remote_files[path] = b""
        elif path not in self.remote_files and "r" in mode:
            raise IOError(f"no such file: {path}")
        self.opened.append((path, mode))
        return FakeRemoteFile(self, path, mode)

    def remove(self, path):
        self.removed.append(path)
        self.remote_files.pop(path, None)

    def close(self):
        self.closed = True


class FakeStdin:
    """模拟 paramiko 的 ChannelFile（wb）：写入落到“远端文件”。

    注意：真实 ChannelFile.close() 只 flush、不给远端发 EOF，
    所以这里也不做任何 EOF 处理 —— 用来回归“必须 shutdown_write()”这个坑。
    """

    def __init__(self, channel):
        self.channel = channel
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        if not self.channel.supported:
            raise EOFError("channel closed: remote command not found")
        fail_after = self.channel.fail_after_bytes
        if fail_after is not None and len(self.buf) + len(data) > fail_after:
            raise OSError("broken pipe")
        self.buf += data
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if not self.channel.supported:
            return
        path = self.channel.target_path
        if not path:
            return
        data = bytes(self.buf)
        if self.channel.drop_tail_bytes:
            data = data[: len(data) - self.channel.drop_tail_bytes]
        self.channel.sftp.remote_files[path] = data


class FakeChannel:
    """模拟 exec 通道：支持 `cat > 文件`（流式直传）与 `df`（空间检查）。"""

    DF_OUTPUT = (
        b"Filesystem     1024-blocks      Used  Available Capacity Mounted on\n"
        b"/dev/sda1       7751271852 924215704 6436338504      13% /remote\n"
    )

    def __init__(self, sftp, supported=True, fail_after_bytes=None, drop_tail_bytes=0,
                 limits=None):
        self.sftp = sftp
        self.supported = supported
        self.fail_after_bytes = fail_after_bytes
        self.drop_tail_bytes = drop_tail_bytes
        self.limits = limits or {}
        self.command = ""
        self.target_path = ""
        self.stdout = b""
        self.stderr = b""
        self.exit_status = 0
        self.closed = False
        self.write_shutdown = False

    def exec_command(self, command):
        self.command = command
        if command.startswith("cat > "):
            if self.supported:
                self.target_path = command.split("> ", 1)[1].strip().strip("'")
                # 只让“第一次 cat”失败，用于验证失败重试
                if self.limits.get("fail_first_after_bytes") is not None \
                        and not self.limits.get("used"):
                    self.fail_after_bytes = self.limits["fail_first_after_bytes"]
                    self.limits["used"] = True
            else:
                self.stderr = b"/bin/sh: 1: cat: not found\n"
                self.exit_status = 127
        elif command.startswith("df "):
            self.stdout = self.DF_OUTPUT

    def makefile(self, mode, bufsize=-1):
        if mode == "wb":
            return FakeStdin(self)
        return io.BytesIO(self.stdout)

    def makefile_stderr(self, mode, bufsize=-1):
        return io.BytesIO(self.stderr)

    def shutdown_write(self):
        self.write_shutdown = True

    def recv_exit_status(self):
        return self.exit_status

    def close(self):
        self.closed = True


class FakeStreamTransport(FakeTransport):
    """带 exec 通道的假 transport：默认支持流式直传（cat）。"""

    def __init__(self, sftp, supported=True, fail_first_after_bytes=None,
                 drop_tail_bytes=0):
        super().__init__()
        self.sftp = sftp
        self.supported = supported
        self.drop_tail_bytes = drop_tail_bytes
        self.channels = []
        self.limits = {"fail_first_after_bytes": fail_first_after_bytes, "used": False}

    def open_session(self):
        channel = FakeChannel(
            self.sftp,
            supported=self.supported,
            drop_tail_bytes=self.drop_tail_bytes,
            limits=self.limits,
        )
        self.channels.append(channel)
        return channel

    def cat_channels(self):
        return [c for c in self.channels if c.command.startswith("cat > ")]


@pytest.fixture
def uploader(monkeypatch):
    """DataUploader + 全假 SFTP/exec 通道：_connect_sftp 与 paramiko.Transport 都被替换。"""
    up = DataUploader()
    up.remote_base = BASE
    up.password = "test-secret"      # 凭据不再内置默认值，测试里显式给出
    up.retry_delay = 0  # 测试里不等待重试间隔
    up.temp_dir = Path(tempfile.mkdtemp(prefix="dc_uploader_test_"))
    fake = FakeSFTP()
    transport = FakeStreamTransport(fake)
    created = {}

    def fake_transport_factory(addr, *args, **kwargs):
        created["addr"] = addr
        return transport

    monkeypatch.setattr(up, "_connect_sftp", lambda: (transport, fake))
    monkeypatch.setattr(paramiko, "Transport", fake_transport_factory)
    monkeypatch.setattr(paramiko.SFTPClient, "from_transport", staticmethod(lambda t: fake))
    up.fake_sftp = fake
    up.fake_transport = transport
    up.created = created
    yield up
    shutil.rmtree(up.temp_dir, ignore_errors=True)


# ---------- get_remote_file_info ----------
def test_get_remote_file_info_returns_metadata(uploader):
    uploader.fake_sftp.stats[f"{BASE}/bag_a"] = FakeAttr("bag_a", is_dir=True, size=4096, mtime=1700000000)
    info = uploader.get_remote_file_info(f"{BASE}/bag_a")
    assert info == {
        "name": "bag_a",
        "path": f"{BASE}/bag_a",
        "is_dir": True,
        "size": 4096,
        "mtime": 1700000000,
    }
    assert uploader.fake_transport.closed is True


def test_get_remote_file_info_outside_base_rejected(uploader):
    with pytest.raises(ValueError):
        uploader.get_remote_file_info("/etc/passwd")


# ---------- search_remote ----------
def test_search_remote_recursive_case_insensitive(uploader):
    uploader.fake_sftp.tree = {
        BASE: [FakeAttr("bag_a", is_dir=True), FakeAttr("readme.txt", size=10)],
        f"{BASE}/bag_a": [FakeAttr("Bag_A_2", is_dir=True), FakeAttr("note.TXT", size=3)],
        f"{BASE}/bag_a/Bag_A_2": [FakeAttr("bag_a_3", size=1)],
    }
    progress = []
    results = uploader.search_remote(
        BASE, "bag_a", progress_cb=lambda s, m: progress.append((s, m))
    )

    assert [r["name"] for r in results] == ["bag_a", "Bag_A_2", "bag_a_3"]
    # 目录排在文件前面
    assert [r["is_dir"] for r in results] == [True, True, False]
    assert results[2]["path"] == f"{BASE}/bag_a/Bag_A_2/bag_a_3"
    assert progress, "应至少回调一次进度"


def test_search_remote_skips_unreadable_dir(uploader):
    # locked 目录不在 tree 中 -> listdir_attr 抛 IOError，应跳过而不是整体失败
    uploader.fake_sftp.tree = {BASE: [FakeAttr("locked", is_dir=True)]}
    assert uploader.search_remote(BASE, "anything") == []


def test_search_remote_outside_base_rejected(uploader):
    with pytest.raises(ValueError):
        uploader.search_remote("/", "x")


def test_search_remote_cancelled(uploader):
    uploader.fake_sftp.tree = {BASE: [FakeAttr("a.txt"), FakeAttr("b.txt")]}
    calls = {"n": 0}

    def running_cb():
        calls["n"] += 1
        return calls["n"] < 2

    with pytest.raises(UploadCancelledError):
        uploader.search_remote(BASE, ".txt", running_cb=running_cb)


# ---------- upload_local_file ----------
def test_upload_local_file_success(uploader, tmp_path):
    local = tmp_path / "note.txt"
    local.write_bytes(b"hello")
    progress = []

    ok = uploader.upload_local_file(
        local, f"{BASE}/sub/note.txt", progress_cb=lambda d, t: progress.append((d, t))
    )

    assert ok is True
    assert uploader.fake_sftp.puts == [(str(local), f"{BASE}/sub/note.txt")]
    assert f"{BASE}/sub" in uploader.fake_sftp.mkdirs
    assert progress[0] == (0, 5)
    assert progress[-1] == (5, 5)
    assert uploader.fake_transport.closed is True
    # 连接使用的是配置里的主机与端口（回归：端口曾经被硬编码为 22）
    assert uploader.created["addr"] == (uploader.remote_host, uploader.remote_port)


def test_upload_local_file_missing_local_returns_false(uploader, tmp_path):
    assert uploader.upload_local_file(tmp_path / "nope.txt", f"{BASE}/nope.txt") is False
    assert uploader.fake_sftp.puts == []


def test_upload_local_file_outside_base_rejected(uploader, tmp_path):
    local = tmp_path / "note.txt"
    local.write_bytes(b"hello")
    assert uploader.upload_local_file(local, "/tmp/note.txt") is False
    assert uploader.fake_sftp.puts == []


def test_upload_local_file_cancel_removes_partial(uploader, tmp_path):
    local = tmp_path / "big.bin"
    local.write_bytes(b"x" * 128)
    remote = f"{BASE}/big.bin"

    ok = uploader.upload_local_file(local, remote, running_cb=lambda: False)

    assert ok is False
    assert uploader.fake_sftp.removed == [remote]
    assert uploader.fake_transport.closed is True


# ---------- 配置驱动 ----------
def test_uploader_and_logfetcher_use_config(tmp_path):
    app_cfg = {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "bag_dir": str(tmp_path / "bags"),
        "log_dir": str(tmp_path / "logs"),
        "remote_host": "10.0.0.9",
        "remote_port": 2222,
        "remote_user": "alice",
        "remote_password": "secret",
        "remote_base": "/data/bags",
    }
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, app_cfg)
    cm = ConfigManager(cfg_file)

    up = DataUploader(cm)
    assert up.remote_host == "10.0.0.9"
    assert up.remote_port == 2222
    assert up.remote_user == "alice"
    assert up.password == "secret"
    assert up.remote_base == "/data/bags"
    assert up.bag_dir == tmp_path / "bags"

    fetcher = LogFetcher(tmp_path / "local_logs", cm)
    assert fetcher.log_dir == tmp_path / "logs"
    assert fetcher.local_log_dir == tmp_path / "local_logs"
    # 抓回来的机器人日志另落一个子目录，与上位机自己的日志分开
    assert fetcher.fetch_dir == tmp_path / "local_logs" / "fetched"


def test_uploader_defaults_without_config(tmp_path):
    up = DataUploader()
    assert up.remote_host == "172.16.10.2"
    assert up.remote_port == 22
    # 默认目录与 scripts/ 各脚本头部的 BAG_DIR / LOG_DIR 默认值保持一致
    assert up.bag_dir == Path("/var/nerv/bag")

    fetcher = LogFetcher(tmp_path / "local_logs")
    assert fetcher.log_dir == Path("/var/nerv/log")
    assert fetcher.local_log_dir == tmp_path / "local_logs"
    assert fetcher.fetch_dir == tmp_path / "local_logs" / "fetched"


# ---------- 抓取落盘：与上位机自身日志分开 ----------
def test_fetched_logs_land_in_own_subdir(tmp_path):
    """抓回来的机器人日志不能和上位机的 ui_error / teleop_* 混在一个目录里。

    现场要求"上位机的 log 与拉取的日志保存位置分开"：两种日志名字都很像
    （`wheeled_robot_*.log` 与 `teleop_*.log`），混放之后抓两次就分不清
    哪份是本机现场、哪份是从机器人拉回来的，清理时也容易连带删错。
    """
    robot_log_dir = tmp_path / "robot_log"
    robot_log_dir.mkdir()
    (robot_log_dir / "wheeled_robot_20260922.log").write_text("robot side\n", encoding="utf-8")

    host_log_dir = tmp_path / "host_logs"
    host_log_dir.mkdir()
    (host_log_dir / "ui_error.log").write_text("host side\n", encoding="utf-8")

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "log_dir": str(robot_log_dir),
    })
    fetcher = LogFetcher(host_log_dir, ConfigManager(cfg_file))

    assert fetcher.fetch_latest_log() is True

    # 落进子目录
    assert (fetcher.fetch_dir / "wheeled_robot_20260922.log").is_file()
    # 上位机日志目录既没多出机器人那份，原有内容也没被动过
    assert not (host_log_dir / "wheeled_robot_20260922.log").exists()
    assert (host_log_dir / "ui_error.log").read_text(encoding="utf-8") == "host side\n"
    # 抓取目录是 LogFetcher 自己建出来的（不需要界面先创建）
    assert fetcher.fetch_dir.is_dir()


def test_fetch_specific_log_also_goes_to_fetch_dir(tmp_path):
    """逐条抓取与"抓最新一条"落同一处，不各走一套路径。"""
    robot_log_dir = tmp_path / "robot_log"
    robot_log_dir.mkdir()
    old = robot_log_dir / "startup.log"
    old.write_text("startup\n", encoding="utf-8")

    host_log_dir = tmp_path / "host_logs"
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "log_dir": str(robot_log_dir),
    })
    fetcher = LogFetcher(host_log_dir, ConfigManager(cfg_file))

    assert fetcher.fetch_specific_log(old) is True
    assert (fetcher.fetch_dir / "startup.log").is_file()
    assert not (host_log_dir / "startup.log").exists()


# ---------- 凭据：不再内置默认密码 ----------
def test_connect_sftp_requires_password():
    """未配置远端密码时给出可操作的提示，而不是让 paramiko 抛底层认证错误。"""
    from data_collector.core_data import RemoteAuthError

    up = DataUploader()
    assert up.password == ""

    with pytest.raises(RemoteAuthError, match="DATA_COLLECTOR_REMOTE_PASSWORD"):
        up._connect_sftp()


# ---------- 打包 + 上传（默认：不压缩 + 流式直传） ----------
def make_bag(root: Path, name: str = "bag_a", payload: bytes = b"x" * 4096) -> Path:
    bag = root / name
    bag.mkdir(parents=True)
    (bag / "info.json").write_bytes(b'{"ok": true}')
    (bag / "data_0.mcap").write_bytes(payload)
    return bag


def test_upload_bag_folder_streams_zip_without_temp_file(uploader, tmp_path):
    """默认策略：zip(不压缩) + 边打包边写远端，不落地任何临时归档。"""
    bag = make_bag(tmp_path)
    progresses, percents = [], []

    ok = uploader.upload_bag_folder(
        bag, "sub", progress_cb=progresses.append, progress_percent=percents.append
    )

    assert ok is True
    assert uploader.last_error == ""
    remote = f"{BASE}/sub/{bag.name}.zip"
    assert f"{BASE}/sub" in uploader.fake_sftp.mkdirs
    with zipfile.ZipFile(io.BytesIO(uploader.fake_sftp.remote_files[remote])) as z:
        assert z.namelist() == [f"{bag.name}/data_0.mcap", f"{bag.name}/info.json"]
        assert z.read(f"{bag.name}/data_0.mcap") == b"x" * 4096
        assert z.testzip() is None
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
    # 流式直传：没有本地临时文件，也没走 sftp.put
    assert list(uploader.temp_dir.iterdir()) == []
    assert uploader.fake_sftp.puts == []
    # 关键回归：必须先 shutdown_write() 给远端发 EOF，否则 recv_exit_status() 会永久阻塞
    assert uploader.fake_transport.cat_channels()[0].write_shutdown is True
    assert percents[-1] == 100
    assert any("上传方式" in p for p in progresses)


def test_upload_bag_folder_streams_tar_when_configured(uploader, tmp_path):
    bag = make_bag(tmp_path, "bag_tar")
    uploader.transfer_format = "tar"

    assert uploader.upload_bag_folder(bag) is True

    data = uploader.fake_sftp.remote_files[f"{BASE}/{bag.name}.tar"]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        names = tar.getnames()
        assert names[0] == bag.name  # 顶层目录条目
        assert f"{bag.name}/data_0.mcap" in names
        assert tar.extractfile(f"{bag.name}/data_0.mcap").read() == b"x" * 4096


def test_upload_bag_folder_deflate_option(uploader, tmp_path):
    """压缩开关仍可选：文本类数据想压就压。"""
    bag = make_bag(tmp_path, "bag_deflate", payload=b"A" * 100000)
    uploader.transfer_compression = "deflate"

    assert uploader.upload_bag_folder(bag) is True

    payload = uploader.fake_sftp.remote_files[f"{BASE}/{bag.name}.zip"]
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
    assert len(payload) < 100000  # 确实压小了


def test_upload_bag_folder_falls_back_to_tempfile_when_exec_unsupported(uploader, tmp_path):
    """远端不支持 exec（SFTP-only）时，auto 模式回退为本地打包后上传。

    本地打包需要临时目录容纳「数据 + 余量」，余量默认 200MB；若测试机的临时
    目录本身小于该要求（例如 /tmp 是几十 MB 的 tmpfs），这条路径物理上跑不通，
    此时跳过并说明原因，而不是把环境问题伪装成失败。
    """
    bag = make_bag(tmp_path, "bag_fallback")
    needed = sum(p.stat().st_size for p in bag.rglob("*") if p.is_file())
    required = int(needed * 1.05) + uploader.temp_margin_mb * 1024 * 1024
    free = shutil.disk_usage(str(uploader.temp_dir)).free
    if free < required:
        pytest.skip(
            f"临时目录 {uploader.temp_dir} 可用 {free / 1024 / 1024:.1f}MB "
            f"< 本地打包所需 {required / 1024 / 1024:.1f}MB（含 "
            f"upload_temp_margin_mb={uploader.temp_margin_mb}）"
        )

    uploader.fake_transport.supported = False
    msgs = []

    ok = uploader.upload_bag_folder(bag, progress_cb=msgs.append)

    assert ok is True
    remote = f"{BASE}/{bag.name}.zip"
    archive = uploader.temp_dir / f"{bag.name}.zip"
    assert uploader.fake_sftp.puts == [(str(archive), remote)]
    with zipfile.ZipFile(io.BytesIO(uploader.fake_sftp.remote_files[remote])) as z:
        assert f"{bag.name}/info.json" in z.namelist()
    # 回退路径用完即删，不留临时归档
    assert list(uploader.temp_dir.iterdir()) == []
    assert any("回退" in m or "本地打包" in m for m in msgs)


def test_upload_bag_folder_stream_only_mode_reports_unsupported(uploader, tmp_path):
    uploader.fake_transport.supported = False
    uploader.transfer_mode = "stream"

    ok = uploader.upload_bag_folder(make_bag(tmp_path, "bag_stream_only"))

    assert ok is False
    assert "流式直传不可用" in uploader.last_error
    assert uploader.fake_sftp.puts == []


def test_upload_bag_folder_cancel_removes_remote_partial(uploader, tmp_path, monkeypatch):
    """取消必须清理远端半成品，且不触发自动重试。"""
    bag = make_bag(tmp_path, "bag_cancel")
    real_pack = uploader._pack_folder

    def pack_then_cancel(*args, **kwargs):
        uploader.upload_cancelled = True  # 模拟用户点“取消上传”
        return real_pack(*args, **kwargs)

    monkeypatch.setattr(uploader, "_pack_folder", pack_then_cancel)

    assert uploader.upload_bag_folder(bag) is False
    assert uploader.last_error == "上传已取消"
    assert f"{BASE}/{bag.name}.zip" in uploader.fake_sftp.removed
    assert len(uploader.fake_transport.cat_channels()) == 1  # 取消不重试


def test_upload_bag_folder_retries_transient_failure(uploader, tmp_path, monkeypatch):
    """流式中断后重试成功。

    刻意把本地空间压到不足，让"继续用流式重试"这条恢复路径成为**确定**行为：
    auto 模式在本地放得下时会改走本地打包（可续传），两条路径的 cat 次数不同。
    不固定这一点的话，该测试会随测试机临时目录的大小而漂移 —— 换 TMPDIR 就红过。
    """
    import collections

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr("data_collector.core_data.shutil.disk_usage",
                        lambda path: usage(0, 0, 1024))
    bag = make_bag(tmp_path, "bag_retry")
    uploader.fake_transport.limits["fail_first_after_bytes"] = 64
    msgs = []

    ok = uploader.upload_bag_folder(bag, progress_cb=msgs.append)

    assert ok is True
    assert len(uploader.fake_transport.cat_channels()) == 2
    assert any("重试" in m for m in msgs)
    # 第一次的半成品被清掉，最终远端文件是完整的
    remote = f"{BASE}/{bag.name}.zip"
    with zipfile.ZipFile(io.BytesIO(uploader.fake_sftp.remote_files[remote])) as z:
        assert z.read(f"{bag.name}/data_0.mcap") == b"x" * 4096


def test_upload_bag_folder_detects_truncated_remote_file(uploader, tmp_path):
    """远端落盘大小与发送字节数不一致时视为失败，不能把半截文件当成功。

    关掉重试（只跑一次），让"截断被检出"成为最终结果 —— 否则第二次尝试会
    改走本地打包并成功，这条断言的语义就变成了"重试能救回来"。
    """
    uploader.upload_retries = 0
    uploader.fake_transport.drop_tail_bytes = 16
    bag = make_bag(tmp_path, "bag_trunc")

    assert uploader.upload_bag_folder(bag) is False
    assert uploader.last_code == ErrorCode.VERIFY_FAILED
    assert "校验" in uploader.last_error


def test_upload_bag_folder_aborts_when_remote_space_insufficient(uploader, tmp_path, monkeypatch):
    monkeypatch.setattr(uploader, "get_remote_disk_usage", lambda *a, **k: {"free": 1024})

    assert uploader.upload_bag_folder(make_bag(tmp_path, "bag_nospace")) is False

    assert "远端剩余空间不足" in uploader.last_error
    assert uploader.fake_transport.cat_channels() == []
    assert uploader.fake_sftp.puts == []


def test_tempfile_mode_aborts_when_local_space_insufficient(uploader, tmp_path, monkeypatch):
    """本地临时盘写满是过去“压缩失败”的主因，这里要提前给出可操作的报错。"""
    import collections

    uploader.transfer_mode = "tempfile"
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr("data_collector.core_data.shutil.disk_usage",
                        lambda path: usage(0, 0, 1024))

    assert uploader.upload_bag_folder(make_bag(tmp_path, "bag_low_space")) is False

    assert "本地临时目录空间不足" in uploader.last_error
    assert "upload_temp_dir" in uploader.last_error


def test_upload_bag_folder_rejects_empty_and_missing(uploader, tmp_path):
    empty = tmp_path / "empty_bag"
    empty.mkdir()
    assert uploader.upload_bag_folder(empty) is False
    assert "为空" in uploader.last_error

    assert uploader.upload_bag_folder(tmp_path / "not_exists") is False
    assert "不存在" in uploader.last_error


def test_upload_latest_bag_uses_newest_folder(uploader, tmp_path):
    bag_dir = tmp_path / "bags"
    bag_dir.mkdir()
    older = make_bag(bag_dir, "bag_old")
    make_bag(bag_dir, "bag_new")
    os.utime(older, (1, 1))
    uploader.bag_dir = bag_dir

    assert uploader.upload_latest_bag("") is True

    assert f"{BASE}/bag_new.zip" in uploader.fake_sftp.remote_files


def test_compress_folder_compat_still_writes_zip(uploader, tmp_path):
    """旧接口保留：zip + DEFLATE 落盘，进度仍占 0~50%。"""
    bag = make_bag(tmp_path, "bag_zip_compat")
    out = uploader.temp_dir / "out.zip"
    percents = []

    assert uploader.compress_folder(bag, out, progress_percent=percents.append) is True

    with zipfile.ZipFile(out) as z:
        assert f"{bag.name}/info.json" in z.namelist()
    assert percents[-1] == 50


def test_get_remote_disk_usage_parses_bytes_output(uploader):
    """回归：exec 通道返回 bytes（含中文路径时真实存在）时也要能解析 df 输出。"""
    usage = uploader.get_remote_disk_usage(f"{BASE}/sub")

    assert usage["total"] == 7751271852 * 1024
    assert usage["free"] == 6436338504 * 1024


def test_channel_text_decodes_bytes_and_passes_str():
    from data_collector.core_data import DataUploader as _DataUploader

    assert _DataUploader._channel_text(b"abc") == "abc"
    assert _DataUploader._channel_text("abc") == "abc"
    assert _DataUploader._channel_text(None) == ""


# ---------- 录制启动：必须确认进程真的活着 ----------
def test_recorder_start_detects_immediate_exit():
    """脚本秒退不能算成功：否则界面显示「录制中」而实际零数据落盘。"""
    from data_collector.core_data import RecorderManager

    class DeadProc:
        def poll(self):
            return 1

    class FakeLauncher:
        def launch_record(self, *a, **k):
            return DeadProc()

    rm = RecorderManager(None, FakeLauncher())

    assert rm.start("bag", settle_seconds=0) is False
    assert "立即退出" in rm.last_error


def test_recorder_start_ok_when_process_alive():
    from data_collector.core_data import RecorderManager

    class LiveProc:
        def poll(self):
            return None

    class FakeLauncher:
        def launch_record(self, *a, **k):
            return LiveProc()

    rm = RecorderManager(None, FakeLauncher())

    assert rm.start("bag", settle_seconds=0) is True
    assert rm.last_error == ""


def test_recorder_start_reports_launch_failure():
    from data_collector.core_data import RecorderManager

    class FakeLauncher:
        def launch_record(self, *a, **k):
            raise FileNotFoundError("脚本不存在")

    rm = RecorderManager(None, FakeLauncher())

    assert rm.start("bag", settle_seconds=0) is False
    assert "脚本不存在" in rm.last_error


# ---------- 远端路径归一化：必须消解 .. ----------
def test_normalize_remote_path_resolves_dotdot():
    assert DataUploader._normalize_remote_path("/base/../../etc") == "/etc"
    assert DataUploader._normalize_remote_path("/base/sub/./x/") == "/base/sub/x"
    assert DataUploader._normalize_remote_path("//base//x") == "/base/x"


def test_check_remote_path_rejects_dotdot_escape(uploader):
    """归一化后 .. 越界会被拒绝（修复前 startswith 判定可被绕过）。"""
    with pytest.raises(ValueError):
        uploader._check_remote_path(f"{BASE}/../../etc")
    # 正常子路径仍然放行
    assert uploader._check_remote_path(f"{BASE}/sub") == f"{BASE}/sub"


# ---------- 录制看门狗：进程活着 ≠ 数据在落盘 ----------
import time  # noqa: E402


def _recorder_env(tmp_path):
    """构造 RecorderManager + 假进程 / 假配置。"""
    from data_collector.core_data import RecorderManager

    bag = tmp_path / "bag"
    bag.mkdir(exist_ok=True)

    class FakeProc:
        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

    class FakeLauncher:
        def __init__(self):
            self.proc = FakeProc()

        def launch_record(self, *a, **k):
            return self.proc

    class FakeCfg:
        def get_bag_dir(self):
            return bag

    launcher = FakeLauncher()
    return RecorderManager(FakeCfg(), launcher), bag, launcher


def test_recorder_health_reports_not_alive_when_idle(tmp_path):
    rm, _bag, _launcher = _recorder_env(tmp_path)

    health = rm.health()

    assert health["alive"] is False
    assert health["producing"] is False


def test_recorder_health_detects_no_output(tmp_path):
    """进程活着但目录里没有新文件 —— 正是"以为在录、其实没录"。"""
    rm, _bag, _launcher = _recorder_env(tmp_path)

    assert rm.start("bag", settle_seconds=0) is True
    health = rm.health()

    assert health["alive"] is True
    assert health["producing"] is False
    assert "还没有新文件" in health["message"]


def test_recorder_health_ok_when_data_landing(tmp_path):
    rm, bag, _launcher = _recorder_env(tmp_path)
    rm.start("bag", settle_seconds=0)

    (bag / "bag_0.mcap").write_bytes(b"x" * 64)

    health = rm.health()

    assert health["alive"] is True
    assert health["producing"] is True
    assert health["message"] == ""


def test_recorder_health_detects_stall(tmp_path):
    """有新文件但长时间没有写入 —— 采集实际上已经停了。"""
    rm, bag, _launcher = _recorder_env(tmp_path)
    rm.start("bag", settle_seconds=0)
    artifact = bag / "bag_0.mcap"
    artifact.write_bytes(b"x" * 64)

    stale = time.time() - 60
    os.utime(artifact, (stale, stale))
    rm._started_at = stale - 1

    health = rm.health(stale_seconds=15)

    assert health["alive"] is True
    assert health["producing"] is False
    assert "没有新数据写入" in health["message"]


def test_recorder_health_ignores_historical_files(tmp_path):
    """bag 目录里的历史数据不能让看门狗误判为"正在产出"。"""
    rm, bag, _launcher = _recorder_env(tmp_path)
    old = time.time() - 3600
    historical = bag / "old_bag.mcap"
    historical.write_bytes(b"x" * 64)
    os.utime(historical, (old, old))

    rm.start("bag", settle_seconds=0)

    assert rm.health()["producing"] is False


def test_recorder_health_detects_process_exit(tmp_path):
    rm, _bag, launcher = _recorder_env(tmp_path)
    rm.start("bag", settle_seconds=0)

    launcher.proc.alive = False

    health = rm.health()
    assert health["alive"] is False
    assert health["message"] == "录制进程已退出"


# ==================== F-6 上传完整性校验与断点续传 ====================
def _raise(exc):
    """构造一个"调用即抛出"的替身，用于注入凭据类失败。"""
    def _boom(*args, **kwargs):
        raise exc
    return _boom


def _small_uploader(uploader):
    """切到 tempfile 路径并把空间余量归零。

    测试机的 /tmp 是容量很小的 tmpfs，默认 200MB 余量会让"本地空间不足"先于
    被测逻辑触发 —— 那是环境噪音，不是被测行为。
    """
    uploader.transfer_mode = "tempfile"
    uploader.temp_margin_mb = 0
    return uploader


def test_tempfile_upload_detects_truncated_remote_file(uploader, tmp_path):
    """传完但被截断必须判失败 —— 这条路径此前**完全没有校验**，直接 return True。"""
    _small_uploader(uploader)
    uploader.upload_retries = 0            # 只跑一次，避免第二次把坏文件覆盖成好的
    uploader.fake_sftp.put_drop_tail = 16
    bag = make_bag(tmp_path, "bag_tf_trunc")

    assert uploader.upload_bag_folder(bag) is False

    assert uploader.last_code == ErrorCode.VERIFY_FAILED
    assert "校验" in uploader.last_error
    # 坏文件不能留在远端被下游取走
    assert f"{BASE}/{bag.name}.zip" not in uploader.fake_sftp.remote_files


def test_tempfile_upload_resumes_from_remote_partial(uploader, tmp_path):
    """传一半断掉 → 重试从远端断点续传，最终包完整且可解压。"""
    _small_uploader(uploader)
    uploader.upload_retries = 1
    bag = make_bag(tmp_path, "bag_resume")
    remote = f"{BASE}/{bag.name}.zip"

    uploader.fake_sftp.put_fail_after = 64     # 只落下 64 字节就断
    msgs = []

    assert uploader.upload_bag_folder(bag, progress_cb=msgs.append) is True

    # 真的走了续传（r+b 打开远端已有文件），而不是从头重传
    assert any(mode == "r+b" for _path, mode in uploader.fake_sftp.opened)
    # 只比大小说明不了内容对不对：续传后的包必须能正常解压且内容正确
    with zipfile.ZipFile(io.BytesIO(uploader.fake_sftp.remote_files[remote])) as z:
        assert z.read(f"{bag.name}/data_0.mcap") == b"x" * 4096
    # 第二次尝试复用了会话内已打好的归档，没有重新打包
    assert any("复用" in m for m in msgs)


def test_resume_offset_rejects_larger_or_equal_remote(uploader):
    """远端比本地大（不是同一份内容）或相等（未经校验）都不能续传。"""
    uploader.fake_sftp.remote_files["/r/eq"] = b"x" * 100
    uploader.fake_sftp.remote_files["/r/big"] = b"x" * 200
    uploader.fake_sftp.remote_files["/r/small"] = b"x" * 40

    offset = DataUploader._resume_offset

    assert offset(uploader.fake_sftp, "/r/small", 100) == 40   # 唯一可续的情形
    assert offset(uploader.fake_sftp, "/r/eq", 100) == 0
    assert offset(uploader.fake_sftp, "/r/big", 100) == 0
    assert offset(uploader.fake_sftp, "/r/missing", 100) == 0


def test_upload_session_cleans_partial_and_archive_after_final_failure(uploader, tmp_path):
    """重试用尽仍失败 → 远端半成品与本地归档都要清干净（P0-3 的原始意图）。"""
    _small_uploader(uploader)
    uploader.upload_retries = 1
    bag = make_bag(tmp_path, "bag_giveup")

    remote = f"{BASE}/{bag.name}.zip"
    calls = {"n": 0}

    def always_fail(*args, **kwargs):
        """每次尝试都失败，并在远端留下半成品 —— 用来验证"用尽重试后的收尾"。"""
        calls["n"] += 1
        uploader.fake_sftp.remote_files[remote] = b"half" * 8
        uploader._fail(ErrorCode.INTERRUPTED, "模拟持续中断")
        return False

    uploader._upload_file = always_fail

    assert uploader.upload_bag_folder(bag) is False

    assert calls["n"] == 2
    # 远端不留同名截断包，本地不留归档
    assert remote not in uploader.fake_sftp.remote_files
    assert list(uploader.temp_dir.glob("*.zip")) == []


def test_archive_is_kept_between_attempts_but_removed_after_success(uploader, tmp_path):
    """归档必须活到会话结束才删 —— 否则第二次尝试没得续传，只能重打包重传。"""
    _small_uploader(uploader)
    uploader.upload_retries = 1
    bag = make_bag(tmp_path, "bag_keep")
    seen_archive = {}
    original = uploader._upload_file

    def spy(local_path, *args, **kwargs):
        seen_archive["path"] = Path(local_path)
        assert Path(local_path).exists(), "上传时归档必须还在"
        return original(local_path, *args, **kwargs)

    uploader.fake_sftp.put_fail_after = 64
    uploader._upload_file = spy

    assert uploader.upload_bag_folder(bag) is True
    # 会话结束后才删
    assert not seen_archive["path"].exists()


# ==================== F-4 错误码：重试策略与归属 ====================
def test_auth_failure_is_not_retried(uploader, tmp_path, monkeypatch):
    """凭据错误重试一万次也是同一个结果 —— 必须当场停，不空转也不给误导提示。"""
    _small_uploader(uploader)
    uploader.upload_retries = 3
    # 夹具把 _connect_sftp 换成了假的，_transport_auth 不会被走到，
    # 所以直接在凭据入口注入失败（这正是真机上缺密码时的行为）
    monkeypatch.setattr(uploader, "_transport_auth",
                        _raise(RemoteAuthError("未配置远端 SFTP 密码")))
    msgs = []

    assert uploader.upload_bag_folder(make_bag(tmp_path, "bag_auth"),
                                      progress_cb=msgs.append) is False

    assert uploader.last_code == ErrorCode.AUTH
    assert uploader.fake_sftp.puts == []             # 一次都没传
    assert not any("正在重试" in m for m in msgs)    # 没有白等重试


def test_remote_space_failure_is_not_retried(uploader, tmp_path, monkeypatch):
    """远端空间不足同样不可重试（这条以前会被自动重试）。"""
    monkeypatch.setattr(uploader, "get_remote_disk_usage", lambda *a, **k: {"free": 1024})
    uploader.upload_retries = 3
    msgs = []

    assert uploader.upload_bag_folder(make_bag(tmp_path, "bag_rs"),
                                      progress_cb=msgs.append) is False

    assert uploader.last_code == ErrorCode.REMOTE_SPACE
    # 注意不能用 `"重试" in m` 判断：建议文案里本来就有"…后重试"，
    # 要断言的是"没有真的发起重试"这个动作
    assert not any("正在重试" in m for m in msgs)


def test_describe_last_error_carries_owner_and_hint(uploader, tmp_path, monkeypatch):
    """界面拿到的不应只是一句"上传失败"，而要能说清该找谁、怎么办。"""
    _small_uploader(uploader)
    monkeypatch.setattr(uploader, "_transport_auth",
                        _raise(RemoteAuthError("未配置远端 SFTP 密码")))

    uploader.upload_bag_folder(make_bag(tmp_path, "bag_desc"))
    text = uploader.describe_last_error()

    assert "归属：" in text
    assert "建议：" in text
    assert "remote_password" in text           # 建议必须可执行


def test_stream_failure_does_not_switch_to_tempfile_without_local_space(uploader, tmp_path,
                                                                       monkeypatch):
    """auto 模式切本地打包前必须确认放得下 —— 否则会把可重试的失败变成硬失败。"""
    import collections

    bag = make_bag(tmp_path, "bag_nofallback")
    uploader.fake_transport.limits["fail_first_after_bytes"] = 64
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr("data_collector.core_data.shutil.disk_usage",
                        lambda path: usage(0, 0, 1024))   # 本地压根放不下

    # 两次都走流式（而不是第二次改走本地打包然后死在空间上），最终重试成功
    assert uploader.upload_bag_folder(bag) is True
    assert len(uploader.fake_transport.cat_channels()) == 2


def test_retry_switches_to_tempfile_when_local_space_allows(uploader, tmp_path):
    """本地放得下时，流式中断后的重试改走本地打包 —— 那条路径才谈得上续传。

    与 test_upload_bag_folder_retries_transient_failure 是一对：那条固定
    "本地放不下 → 继续流式"，这条固定"本地放得下 → 换成本地打包"。
    """
    import collections

    bag = make_bag(tmp_path, "bag_switch")
    uploader.fake_transport.limits["fail_first_after_bytes"] = 64
    usage = collections.namedtuple("usage", "total used free")
    uploader.temp_margin_mb = 0
    # 本地空间充足（真实磁盘），_tempfile_switch_ok 会放行
    assert uploader.upload_bag_folder(bag) is True

    # 只启了 1 个流式通道，说明第二次尝试没有再走流式
    assert len(uploader.fake_transport.cat_channels()) == 1
    assert f"{BASE}/{bag.name}.zip" in uploader.fake_sftp.remote_files
