"""DataUploader 远程目录操作的单元测试。

使用假的 SFTP/Transport 替换 _connect_sftp，不依赖真实网络与远程主机。
"""
from pathlib import Path

import paramiko
import pytest

from data_collector.core import ConfigManager, save_yaml
from data_collector.core_data import DataUploader, LogFetcher, UploadCancelledError

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


class FakeSFTP:
    """按 {目录: [FakeAttr]} 描述的假文件系统。"""

    def __init__(self, tree=None, stats=None):
        self.tree = tree or {}
        self.stats = stats or {}
        self.puts = []
        self.removed = []
        self.mkdirs = []
        self.closed = False

    def listdir_attr(self, path):
        if path not in self.tree:
            raise IOError(f"no such directory: {path}")
        return self.tree[path]

    def stat(self, path):
        if path in self.stats:
            return self.stats[path]
        raise IOError(f"no such file: {path}")

    def mkdir(self, path):
        self.mkdirs.append(path)

    def put(self, local, remote, callback=None):
        size = Path(local).stat().st_size
        if callback:
            callback(0, size)
            callback(size, size)
        self.puts.append((local, remote))

    def remove(self, path):
        self.removed.append(path)

    def close(self):
        self.closed = True


@pytest.fixture
def uploader(monkeypatch):
    """DataUploader + 全假 SFTP：_connect_sftp 与 paramiko.Transport/from_transport 都被替换。"""
    up = DataUploader()
    up.remote_base = BASE
    fake = FakeSFTP()
    transport = FakeTransport()
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
    return up


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


def test_uploader_defaults_without_config():
    up = DataUploader()
    assert up.remote_host == "172.16.10.2"
    assert up.remote_port == 22
    assert up.bag_dir == Path("/var/nerv/bag")
