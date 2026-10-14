"""上传链路的**界面闸门**测试：`export_target` 分流在界面层真的生效吗。

## 这份测试为什么存在（它记的是一个真实故障）

v6.4.0 把 `export_target` 的分流点放在 `core_data.upload_bag_folder`，
逻辑层写得没问题、测试也全绿。但现场选「移动硬盘」之后**一个文件都没写出去**。

根因是分流点放得太靠里：界面在进入 `upload_bag_folder` **之前**还有两道
远端专属闸门 —— 问 SFTP 密码、弹远端目录选择器。而那个选择器构造完就去
连 SFTP 列目录，离线现场连不上。于是无论逻辑层分流多正确，
`disk` 目标都走不到它。

**这类故障没有任何断言能提前发现**：逻辑层测试全绿（因为它确实分流对了），
界面测试也全绿（因为界面单独看没问题）。只有把两层放在一起看才暴露 ——
所以这份测试的全部意义就在"一起看"。

## 钉住的三条不变量

| 不变量 | 违反时的现场症状 |
| --- | --- |
| `disk` 目标不问密码、不弹远端浏览器 | 离线现场被密码框挡住，无从下手 |
| 磁盘落点不含远端子路径 | 盘上出现 `robot/linen/2026-10-08/` 这种远端层级，现场看不懂 |
| `server` / `both` 行为逐字不变 | 存量部署被打断（最严重的一类回归） |
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from data_collector.core import ConfigManager
from data_collector.core_data import DataUploader
from data_collector.ui import common as ui_common
from data_collector.ui.common import (REMOTE_TARGETS, remote_subpath_arg,
                                      resolve_upload_subpath)

BASE = "workspace_root: /tmp\nteleop_config_path: a.yaml\narm_control_config_path: b.yaml\n"


def _uploader(tmp_path, extra=""):
    p = tmp_path / "app_config.yaml"
    p.write_text(BASE + extra, encoding="utf-8")
    return DataUploader(ConfigManager(p))


def _bag(tmp_path, name="挥手"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "a.mcap").write_bytes(b"x" * 2048)
    return folder


def _today() -> str:
    """今天的日期层名。

    单独抽出来是因为这些守卫在**收集与运行之间**可能跨过零点 ——
    凌晨 23:59 收集、00:01 运行时，bag 目录的 mtime 是昨天，
    而用例里现算的"今天"已经是新的一天。这类失败极难复现。
    """
    return datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")


class _Log:
    """冒充有 append/append_log 的界面对象。"""

    def __init__(self):
        self.lines = []

    def append(self, text):
        self.lines.append(text)

    append_log = append


@pytest.fixture
def no_remote(monkeypatch):
    """把两处远端接触点（问密码 / 弹目录选择器）都换成"一碰就炸"的探针。

    用探针而不是 mock 返回值：返回值可以被"调用了但结果没用"糊弄过去，
    而**调用本身**就是这条测试要禁止的行为。
    """
    hits = {"password": 0, "dialog": 0}

    def _boom_password(uploader, parent=None):
        hits["password"] += 1
        return True

    def _boom_dialog(uploader, parent=None, select_mode=False):
        hits["dialog"] += 1
        raise AssertionError("disk 目标下不应该弹远端目录选择器")

    monkeypatch.setattr(ui_common, "ensure_remote_password", _boom_password)
    # 目录选择器是惰性导入的，所以要打到 `remote_browser` 模块属性上——
    # 直接改 `ui_common` 里那个名字没用（函数体在调用时才去 import）。
    import data_collector.ui.remote_browser as rb
    monkeypatch.setattr(rb, "RemoteDirectoryDialog", _boom_dialog)
    return hits


# ---------- disk 目标：绝不碰远端 ----------

def test_disk_target_asks_for_no_password(tmp_path, no_remote):
    up = _uploader(tmp_path, "export_target: disk\n")
    log = _Log()

    assert resolve_upload_subpath(up, log) is not None, "disk 目标不该要求任何输入"
    assert no_remote["password"] == 0, "disk 目标下弹了密码框"
    assert no_remote["dialog"] == 0, "disk 目标下弹了远端目录选择器"


def test_disk_target_says_out_that_it_stays_offline(tmp_path, no_remote):
    """日志里必须说清"不连服务器"。

    现场看到"上传"两个字会以为要走网络；先说清不联网，他才不会去
    找网线、找服务器密码。
    """
    up = _uploader(tmp_path, "export_target: disk\n")
    log = _Log()

    resolve_upload_subpath(up, log)

    assert any("不连接远端" in line for line in log.lines), \
        f"日志没有说明本次不联网：{log.lines}"


def test_disk_target_survives_with_no_password_at_all(tmp_path, no_remote):
    """回归的那一刀：uploader 完全没有密码，disk 导出照样能起步。

    这是现场报障的原样复现 —— 离线机器上没人知道服务器密码。
    """
    up = _uploader(tmp_path, "export_target: disk\n")
    assert up.password == "", "前置条件不成立：本用例要的就是无密码状态"

    assert resolve_upload_subpath(up, _Log()) is not None


def test_sentinel_is_not_confused_with_remote_root(tmp_path, no_remote):
    """sentinel 与 `""`（远端根目录）必须可区分。

    用一个字符串当哨兵、而 `""` 在 server 侧是合法值 —— 一旦混淆，
    `both` 模式会把远端子路径也写成空，等于静默丢用户选的位置。
    """
    up = _uploader(tmp_path, "export_target: disk\n")
    disk_side = resolve_upload_subpath(up, _Log())
    assert disk_side != "", "disk 侧的 sentinel 不能是空串（那是远端根目录）"
    assert remote_subpath_arg(disk_side) == "", "sentinel 应转成空子路径给 uploader"

    up_both = _uploader(tmp_path, "export_target: both\n")
    assert remote_subpath_arg("") == "", "空串本来就是合法的远端子路径"
    assert remote_subpath_arg("robot/linen") == "robot/linen", "真实子路径必须原样透传"


def test_cancel_stays_distinguishable_from_disk(tmp_path, monkeypatch):
    """用户取消 → None；disk → 非 None。两者混同会让"取消"变成"开始导出"。"""
    monkeypatch.setattr(ui_common, "ensure_remote_password",
                        lambda u, p=None: False)
    up = _uploader(tmp_path, "export_target: server\n")
    assert resolve_upload_subpath(up, _Log()) is None


# ---------- server / both：行为逐字不变（最要紧的回归防线）----------

def test_server_target_still_asks_password_then_picks_directory(tmp_path, monkeypatch):
    order = []

    def _pw(uploader, parent=None):
        order.append("password")
        return True

    class _Dlg:
        def __init__(self, uploader, parent=None, select_mode=False):
            order.append("dialog")

        def exec_(self):
            return 1                       # QDialog.Accepted

        def get_selected_subpath(self):
            return "robot/linen"

    monkeypatch.setattr(ui_common, "ensure_remote_password", _pw)
    import data_collector.ui.remote_browser as rb
    monkeypatch.setattr(rb, "RemoteDirectoryDialog", _Dlg)

    up = _uploader(tmp_path, "export_target: server\n")
    assert resolve_upload_subpath(up, _Log()) == "robot/linen"
    assert order == ["password", "dialog"], f"顺序变了：{order}"


def test_both_target_still_needs_the_remote_half(tmp_path, monkeypatch):
    """`both` 要两边都写，所以远端那半边（密码 + 目录）必须照旧。"""
    calls = {"pw": 0, "dlg": 0}

    def _pw(uploader, parent=None):
        calls["pw"] += 1
        return True

    class _Dlg:
        def __init__(self, *a, **k):
            calls["dlg"] += 1

        def exec_(self):
            return 1

        def get_selected_subpath(self):
            return "tasks/2026-10-08"

    monkeypatch.setattr(ui_common, "ensure_remote_password", _pw)
    import data_collector.ui.remote_browser as rb
    monkeypatch.setattr(rb, "RemoteDirectoryDialog", _Dlg)

    up = _uploader(tmp_path, "export_target: both\n")
    assert resolve_upload_subpath(up, _Log()) == "tasks/2026-10-08"
    assert calls == {"pw": 1, "dlg": 1}


def test_remote_targets_list_matches_the_three_config_values():
    """分流名单与 `get_export_settings` 认的三态必须对得上。

    名单写漏一个 → 那个目标静默不问密码（server 会直接失败）；
    名单多一个 → 无意义。两条都要挡住。
    """
    from data_collector.core import ConfigManager as CM
    assert set(REMOTE_TARGETS) == {"server", "both"}
    assert set(CM.EXPORT_TARGETS) == {"server", "disk", "both"}
    assert not set(REMOTE_TARGETS) & {"disk"}, "disk 绝不能出现在需要远端密码的名单里"


# ---------- 磁盘落点：挂载点下的 bag 名目录 ----------

def _fake_disk(monkeypatch, recorded, redirect_to):
    """把 DiskExportTarget 换掉，只记 `prepare` 拿到的 subpath。

    `redirect_to`：把挂载点重定向到可写的临时目录。**必须重定向** ——
    配置里写的 `/media/nerv/DISK` 是真实系统路径，沙箱不可写（Errno 13），
    而落点断言只看 subpath，落哪个目录不影响结论。
    """

    class _Fake:
        def __init__(self, mount_point="", **kw):
            self.mount_point = Path(redirect_to)
            self.require_volume_id = False
            self.warn_file_bytes = 0

        def is_mounted(self):
            """落盘位置功能（v6.4.17）先问挂载点、再判"源是不是已经在盘上"。

            这些用例的 bag 都在挂载点**之外**，所以给 True 也不会触发"同盘跳过"
            （挂载点判定本身在 `test_disk_export.py` 里单测）。
            **替身要跟生产代码收敛**：`_export_to_disk` 现在会问它，缺了就是
            AttributeError，不是"少测一点"。
            """
            return True

        def prepare(self, subpath, total_bytes=0):
            recorded.append(subpath)
            return self.mount_point / subpath

        def warn_oversize(self, entries):
            return []

        def filesystem_type(self):
            return "exfat"

        def write_stream(self, target_dir, filename, producer, **kw):
            # 替身要自己建目录：生产代码的目录由 `prepare` 建（真实实现里
            # prepare 会 mkdir），替身不建就是"测试自己造的失败"。
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / filename
            path.write_bytes(b"payload")
            return path

    import data_collector.disk_export as de
    monkeypatch.setattr(de, "DiskExportTarget", _Fake)


def test_disk_landing_spot_is_the_bag_folder_under_the_mount(tmp_path, monkeypatch):
    recorded = []
    _fake_disk(monkeypatch, recorded, tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n"
                             'disk_export:\n  mount_point: "/media/nerv/DISK"\n')
    bag = _bag(tmp_path, "挥手_20261008")

    assert up.upload_bag_folder(bag) is True
    # v6.4.3 起落点是 `<序列号>/<采集日期>/<bag 名>`。
    # 第一层**不写死具体值**：项目里有一份真的 Product.json（开发机的），
    # 所以无配置时会读到真序列号（实测 12345678）而不是 unknown-serial。
    # 要断言的是层级结构与第二三层的具体值。
    parts = recorded[0].split("/")
    assert len(parts) == 3, f"落点应是三层（序列号/日期/bag名），实际 {recorded}"
    assert parts[0], "序列号层不能为空"
    assert parts[1] == _today(), f"第二层应是今天，实际 {parts[1]}"
    assert parts[2] == "挥手_20261008", f"第三层应是 bag 名，实际 {parts[2]}"


def test_disk_landing_spot_ignores_a_remote_subpath(tmp_path, monkeypatch):
    """即使调用方硬塞一个远端子路径，磁盘侧也不采信。

    这条钉的是"远端目录树的语义不许渗进本地盘"：那串路径在现场看是
    `移动硬盘/robot/linen/2026-10-08/`，没人认得，而且它原本只能从
    远端目录选择器拿到 —— 等于让"只写硬盘"重新依赖远端可用。
    """
    recorded = []
    _fake_disk(monkeypatch, recorded, tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n"
                             'disk_export:\n  mount_point: "/media/nerv/DISK"\n')
    bag = _bag(tmp_path, "挥手_20261008")

    up.upload_bag_folder(bag, "robot/linen/2026-10-08")

    got = recorded[0]
    parts = got.split("/")
    assert len(parts) == 3, f"落点应是三层，实际 {got}"
    assert parts[1] == _today() and parts[2] == "挥手_20261008", \
        f"磁盘侧不该采信远端子路径，实际落点 {got}"
    assert "robot" not in got and "linen" not in got, \
        f"远端层级渗进了磁盘落点：{got}"


def test_each_bag_gets_its_own_directory(tmp_path, monkeypatch):
    """两次采集落在两个目录，不会互相覆盖。"""
    recorded = []
    _fake_disk(monkeypatch, recorded, tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n"
                             'disk_export:\n  mount_point: "/media/nerv/DISK"\n')

    up.upload_bag_folder(_bag(tmp_path, "第一次"))
    up.upload_bag_folder(_bag(tmp_path, "第二次"))

    # 不写死序列号（那是这台开发机真值，换一台就红）：比结构就够了 ——
    # 两次落点必须不同，且第二层同为今天。
    today = _today()
    assert len(recorded) == 2 and recorded[0] != recorded[1], \
        f"同一天的两次采集应各有落点，实际 {recorded}"
    assert all(f"/{today}/" in s for s in recorded), \
        f"第二层都应是今天（{today}），实际 {recorded}"
    assert recorded[0].split("/")[0] == recorded[1].split("/")[0], \
        "两批数据的序列号层应相同（同一台机器）"


def test_both_keeps_the_remote_subpath_for_the_server_half(tmp_path, monkeypatch):
    """`both`：磁盘用 bag 名，远端用用户选的子路径 —— 两边语义本就不同。

    这不是"要保持一致"，而是"两边各按各的规则"：远端那一侧的子路径
    仍然要原样传下去，否则用户选的位置会被静默忽略。
    """
    disk_spans, server_spans = [], []

    class _Fake:
        def __init__(self, mount_point="", **kw):
            self.mount_point = Path(tmp_path / "fake-mount")
            self.require_volume_id = False
            self.warn_file_bytes = 0

        def is_mounted(self):
            """落盘位置功能（v6.4.17）先问挂载点、再判"源是不是已经在盘上"。

            这些用例的 bag 都在挂载点**之外**，所以给 True 也不会触发"同盘跳过"
            （挂载点判定本身在 `test_disk_export.py` 里单测）。
            **替身要跟生产代码收敛**：`_export_to_disk` 现在会问它，缺了就是
            AttributeError，不是"少测一点"。
            """
            return True

        def prepare(self, subpath, total_bytes=0):
            disk_spans.append(subpath)
            return self.mount_point / subpath

        def warn_oversize(self, entries):
            return []

        def filesystem_type(self):
            return "exfat"

        def write_stream(self, target_dir, filename, producer, **kw):
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / filename
            path.write_bytes(b"payload")
            return path

    import data_collector.disk_export as de
    monkeypatch.setattr(de, "DiskExportTarget", _Fake)
    up = _uploader(tmp_path, "export_target: both\n"
                             'disk_export:\n  mount_point: "/media/nerv/DISK"\n')
    monkeypatch.setattr(up, "_upload_bag_folder_to_server",
                        lambda folder, subpath, cb=None, pct=None:
                        server_spans.append(subpath) or True)

    assert up.upload_bag_folder(_bag(tmp_path, "挥手"), "tasks/10-08") is True
    # 磁盘侧：序列号/日期/bag名（语义是"哪台机器、哪天、哪一批"）
    parts = disk_spans[0].split("/")
    assert len(parts) == 3 and parts[2] == "挥手", \
        f"磁盘侧落点应是序列号/日期/bag名，实际 {disk_spans}"
    # 远端侧：用户选的子路径原样传下去（语义是"服务器目录树上的位置"）
    assert server_spans == ["tasks/10-08"], f"远端子路径被丢了：{server_spans}"


# ---------- 两个入口必须走同一个分流点 ----------

def test_both_upload_entries_call_the_same_resolver():
    """两个上传入口不许各写一套判断。

    界面有两条上传路径（数据页的「上传选中」与上传页的「上传数据」）。
    分流写在一处是它们能长期一致的唯一办法 —— 各写一份的话，
    改漏一处就是"某个入口还是要问密码"，而这类不一致极难在测试里发现。
    """
    from data_collector.ui.panels import upload as upload_panel
    from data_collector.ui import data_view

    for label, src in (("upload.py", Path(upload_panel.__file__).read_text(encoding="utf-8")),
                       ("data_view.py", Path(data_view.__file__).read_text(encoding="utf-8"))):
        assert "resolve_upload_subpath" in src, f"{label} 没走统一分流点"

    # 断言只能看**上传方法体**，不能扫整个文件：
    # `data_view.py` 里的 `open_remote_browser`（浏览远端目录）本来就要问密码 ——
    # 那是远端功能，不归"导出目标"管。全文件扫会把这个正确的行为一起判错。
    def _method_body(source, marker, stop):
        start = source.index(marker)
        return source[start:source.index(stop, start)]

    up_body = _method_body(Path(upload_panel.__file__).read_text(encoding="utf-8"),
                           "def on_upload(self)", "def on_cancel_upload")
    dv_body = _method_body(Path(data_view.__file__).read_text(encoding="utf-8"),
                           "def upload_selected_bag(self)", "def _busy_controls")
    for label, body in (("upload.py::on_upload", up_body),
                        ("data_view.py::upload_selected_bag", dv_body)):
        assert "ensure_remote_password" not in body, \
            f"{label} 仍在上传前直接问密码（应交给 resolve_upload_subpath）"
        assert "RemoteDirectoryDialog" not in body, \
            f"{label} 仍直接弹远端目录选择器"


def test_actual_export_never_builds_an_ssh_connection(tmp_path, monkeypatch):
    """端到端：disk 导出全程不建 SSH。

    这条比任何 mock 都硬 —— 它把 `DataUploader._connect_sftp` 换成一颗雷，
    只要导出路径上有任何一处碰远端就会炸。
    """
    recorded = []
    _fake_disk(monkeypatch, recorded, tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n"
                             'disk_export:\n  mount_point: "/media/nerv/DISK"\n')

    def _boom():
        raise AssertionError("disk 导出不该建立 SSH 连接")

    monkeypatch.setattr(up, "_connect_sftp", _boom)

    assert up.upload_bag_folder(_bag(tmp_path, "挥手")) is True
