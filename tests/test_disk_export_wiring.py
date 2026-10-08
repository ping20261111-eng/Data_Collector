"""磁盘导出**接线**测试：主流程怎么按 `export_target` 分流。

与 `test_disk_export.py` 的分工：那份测的是 `DiskExportTarget` 自身
（挂载点、盘标识、FAT32、拔盘），这份测的是"主流程有没有真的走到它"。

## 为什么要单独一份

`disk_export.py` 的 11 个方法在接线前是**零调用**的 —— 核心类写完了、
单测也全绿，但工程师点"导出到硬盘"什么都不会发生。
"类有没有被用起来"这件事只能在这层测，且**必须盯住默认路径**：
分流写错最容易出的不是"disk 不生效"，而是**"没配 disk 的人也被改了行为"**。
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core import ConfigManager
from data_collector.core_data import DataUploader
from data_collector.errors import ErrorCode

BASE = "workspace_root: /tmp\nteleop_config_path: a.yaml\narm_control_config_path: b.yaml\n"


def _mgr(tmp_path, extra=""):
    p = tmp_path / "app_config.yaml"
    p.write_text(BASE + extra, encoding="utf-8")
    return ConfigManager(p)


def _uploader(tmp_path, extra="", **kw):
    return DataUploader(_mgr(tmp_path, extra), **kw)


def _bag(tmp_path, name="挥手"):
    """造一个最小的 bag 目录（含一个非空文件）。"""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "a.mcap").write_bytes(b"x" * 2048)
    return folder


class _Recorder:
    """记调用，用来断言"某条路有没有被走"。"""

    def __init__(self):
        self.disk_calls = []
        self.server_calls = []
        self.disk_result = True

    def as_disk(self, uploader, ok=True):
        # 签名与生产代码保持一致（`_export_to_disk` **不收** remote_subpath：
        # 磁盘落点由自己的规则决定）。若这里保留那个形参，
        # 守卫就会接受一个已经不存在的参数 —— 有人把 subpath 加回去也照样绿。
        #
        # 落点 subpath（`SN/日期/bag名`）虽然不作为**参数**进来，但它写在
        # 目标目录里，而 `prepare` 会拿到它 —— 所以从 `prepare` 侧观测。
        def _call(folder_path, progress_cb=None, progress_percent=None):
            self.disk_calls.append((Path(folder_path), None))
            if not ok:
                return False
            return True
            if not ok:
                uploader.last_error = "拔盘了"
                uploader.last_code = ErrorCode.LOCAL_IO
            return ok
        return _call

    def as_server(self, ok=True):
        def _call(folder_path, remote_subpath="", progress_cb=None,
                  progress_percent=None):
            self.server_calls.append((Path(folder_path), remote_subpath))
            return ok
        return _call


# ---------- 零行为变更（最重要的一条） ----------

def test_default_config_never_touches_the_disk(tmp_path, monkeypatch):
    """老配置（没有 export_target）→ **磁盘侧一行都不碰**。

    这是整个功能的前提：现场已经在跑的部署不能因为加了一个键就改行为。
    断言方式不是"配置读出来是 server"（那只能证明解析对），
    而是**把磁盘方法换成一个会记录并炸掉的替身**，证明它压根没被调用。
    """
    folder = _bag(tmp_path)
    up = _uploader(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up))
    monkeypatch.setattr(up, "_upload_bag_folder_to_server", rec.as_server(ok=True))

    assert up.upload_bag_folder(folder) is True
    assert rec.disk_calls == [], "没配 export_target 却走了磁盘分支"
    assert len(rec.server_calls) == 1


def test_uploader_without_config_defaults_to_server(tmp_path, monkeypatch):
    """不传 config 构造的 `DataUploader` 也不许碰硬盘。

    若干历史调用方是 `DataUploader()` 这样构造的。那条路径上更不能
    凭空去写盘 —— 它连挂载点从哪读都不知道。
    """
    folder = _bag(tmp_path)
    up = DataUploader()
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up))

    assert up._export_target() == "server"
    up.upload_bag_folder(folder)
    assert rec.disk_calls == []


def test_config_layer_error_falls_back_to_server(tmp_path, monkeypatch):
    """配置层抛异常时按 server 处理，而不是让"导出"整体失败。"""

    class _Boom:
        """配置层的其他方法都正常，只有 `get_export_settings` 抛异常
        —— 要验的是"导出目标读不到时不拖垮整个上传"，不是"配置完全不能用"。"""

        def get_remote_settings(self):
            return {}

        def get_upload_settings(self):
            return {}

        def get_bag_dir(self):
            return Path("/tmp")

        def get_export_settings(self):
            raise RuntimeError("配置读不了")

    up = DataUploader(_Boom())
    assert up._export_target() == "server"


# ---------- 三态分流 ----------

def test_disk_target_skips_the_server_entirely(tmp_path, monkeypatch):
    """`disk`：只写硬盘，**完全不建立 SSH 连接**。

    离线现场连不上服务器是常态，磁盘模式如果还去建 SSH 就会失败 ——
    所以断言的是"服务器方法没被调"，而不是"它失败了没关系"。
    """
    folder = _bag(tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n")
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up, ok=True))
    monkeypatch.setattr(up, "_upload_bag_folder_to_server", rec.as_server())

    assert up.upload_bag_folder(folder) is True
    assert len(rec.disk_calls) == 1
    assert rec.server_calls == [], "disk 模式不该碰服务器"


def test_both_writes_disk_then_server(tmp_path, monkeypatch):
    """`both`：两边都写，顺序是磁盘在前。"""
    folder = _bag(tmp_path)
    up = _uploader(tmp_path, "export_target: both\n")
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up, ok=True))
    monkeypatch.setattr(up, "_upload_bag_folder_to_server", rec.as_server(ok=True))

    assert up.upload_bag_folder(folder) is True
    assert len(rec.disk_calls) == 1 and len(rec.server_calls) == 1


def test_both_stops_when_disk_fails(tmp_path, monkeypatch):
    """磁盘失败就不该继续往服务器传 —— 现场"带走数据"是更急的那件事。"""
    folder = _bag(tmp_path)
    up = _uploader(tmp_path, "export_target: both\n")
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up, ok=False))
    monkeypatch.setattr(up, "_upload_bag_folder_to_server", rec.as_server(ok=True))

    assert up.upload_bag_folder(folder) is False
    assert rec.server_calls == []


def test_invalid_target_behaves_like_server(tmp_path, monkeypatch):
    """非法取值回退 server（配置层已判过一次，这里确认端到端也一致）。"""
    folder = _bag(tmp_path)
    up = _uploader(tmp_path, "export_target: usb\n")
    rec = _Recorder()
    monkeypatch.setattr(up, "_export_to_disk", rec.as_disk(up))
    monkeypatch.setattr(up, "_upload_bag_folder_to_server", rec.as_server(ok=True))

    assert up.upload_bag_folder(folder) is True
    assert rec.disk_calls == []


# ---------- 磁盘侧真实执行（不打桩 DiskExportTarget） ----------

def test_disk_export_writes_an_archive(tmp_path, monkeypatch):
    """真跑一次：挂载点检查被放行后，归档真的落到盘上。"""
    mount = tmp_path / "mnt"
    mount.mkdir()
    (mount / ".nerv_volume").write_text("ok", encoding="utf-8")
    # 沙箱里造不出真挂载点，把 is_mounted 放行（它本身在 test_disk_export 里已单测）
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    folder = _bag(tmp_path)
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n"
                   "  require_volume_id: true\n")

    assert up.upload_bag_folder(folder) is True
    archives = list(mount.rglob("*.zip"))
    assert len(archives) == 1, f"盘上应有归档，实际 {[p.name for p in mount.rglob('*')]}"
    assert archives[0].stat().st_size > 0
    # 半成品必须不留
    assert not list(mount.rglob("*.partial"))


def test_disk_export_rejects_when_not_configured(tmp_path):
    """选了 disk 却没配挂载点 → 明确说清怎么改，而不是抛原始异常。"""
    folder = _bag(tmp_path)
    up = _uploader(tmp_path, "export_target: disk\n")

    assert up.upload_bag_folder(folder) is False
    assert up.last_code == ErrorCode.CONFIG_MISSING
    assert "mount_point" in up.last_error


def test_disk_export_keeps_the_error_code_of_a_failed_precheck(tmp_path, monkeypatch):
    """盘没挂载时，错误码必须是带码的（界面据此给建议），不能归成"传输中断"。"""
    folder = _bag(tmp_path)
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   "  mount_point: /definitely/not/here\n")
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    assert up.upload_bag_folder(folder) is False
    assert up.last_code == ErrorCode.NOT_FOUND
    assert "移动硬盘" in up.last_error or "导出路径不存在" in up.last_error


def test_empty_bag_is_rejected_before_touching_the_disk(tmp_path, monkeypatch):
    """空目录不该先建目录再发现没内容。"""
    empty = tmp_path / "空"
    empty.mkdir()
    mount = tmp_path / "never_used"
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n")

    assert up.upload_bag_folder(empty) is False
    assert up.last_code == ErrorCode.NOT_FOUND
    # 关键：连盘都没碰过 —— 校验在接触硬件之前
    assert not mount.exists(), "空目录不该先去建挂载点目录"


def test_bag_dir_cancel_is_honored_by_the_disk_path(tmp_path, monkeypatch):
    """取消信号要传到打包器 —— 磁盘侧同样是长时间 IO。"""
    mount = tmp_path / "mnt2"
    mount.mkdir()
    (mount / ".nerv_volume").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    folder = _bag(tmp_path)
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n")
    up.upload_cancelled = True          # 起步就取消

    up.upload_bag_folder(folder)
    # 取消时不留半成品（可能写出了完整小包，那也不该有 .partial）
    assert not list(mount.rglob("*.partial"))


def test_producer_handles_both_file_protocols(tmp_path, monkeypatch):
    """`producer` 收到的 `sink` 是**回调**，而打包器要的是**文件对象**（zipfile 会
    调 `flush`）。中间那层适配必须同时满足 `write` 与 `flush` —— v6.3.6 实测：
    只补 `write` 会报 `'object has no attribute 'flush'`，而 zip 根本没生成。

    这条钉的是**契约**而不是实现细节：`sink` 是回调（`bytes -> None`）这件事
    写在 `DiskExportTarget.write_stream` 的契约里，改动那边就要同步改这里。
    """
    mount = tmp_path / "mnt3"
    mount.mkdir()
    (mount / ".nerv_volume").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    seen = {}
    original = DataUploader._pack_folder

    def spy(self, folder, fp, fmt, compress, entries, **kw):
        seen["has_write"] = callable(getattr(fp, "write", None))
        seen["has_flush"] = callable(getattr(fp, "flush", None))
        return original(self, folder, fp, fmt, compress, entries, **kw)

    monkeypatch.setattr(DataUploader, "_pack_folder", spy)

    folder = _bag(tmp_path)
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n")
    assert up.upload_bag_folder(folder) is True
    assert seen == {"has_write": True, "has_flush": True}, \
        f"传给打包器的对象必须像文件对象，实际探测到 {seen}"


def test_disk_archive_is_readable_zip(tmp_path, monkeypatch):
    """落盘的归档必须是**真能打开的 zip**，不是"看起来像包"的字节流。

    `disk_export` 的原子写入是"临时文件 + rename"，中间任何一步出错都可能
    留下一个完整文件名的截断包 —— 下游按文件名取用会把残缺数据当正常数据。
    """
    import zipfile

    mount = tmp_path / "mnt4"
    mount.mkdir()
    (mount / ".nerv_volume").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    folder = _bag(tmp_path, name="鞠躬")
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n")
    assert up.upload_bag_folder(folder) is True

    archives = list(mount.rglob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as zf:
        assert zf.namelist() == ["鞠躬/a.mcap"]
        assert zf.read("鞠躬/a.mcap") == b"x" * 2048
    # 原子写入的临时文件不许留下
    assert not list(mount.rglob("*.partial"))


def test_content_verification_writes_sidecar_on_disk(tmp_path, monkeypatch):
    """开了 `verify_content` → 盘上也应有 `.sha256` 边车文件（与远端同构）。

    离线场景没有服务器来复核，边车文件是"这份数据没坏"的**唯一**外部凭据。
    """
    mount = tmp_path / "mnt5"
    mount.mkdir()
    (mount / ".nerv_volume").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda p: True)

    folder = _bag(tmp_path)
    up = _uploader(tmp_path,
                   "export_target: disk\n"
                   "disk_export:\n"
                   f"  mount_point: {mount}\n")
    up.verify_content = True

    assert up.upload_bag_folder(folder) is True
    sidecars = list(mount.rglob("*.sha256"))
    assert len(sidecars) == 1, "开了内容校验却没有边车文件"
    assert sidecars[0].read_text(encoding="utf-8").strip().split()[0]


# ---------- 落点分层：挂载点 / 序列号 / 采集日期 / bag 名（v6.4.3） ----------

def _product_config(tmp_path, mount, serial=None, raw_serial=None, target="disk"):
    """造一份带 Product.json 的配置，返回 uploader。

    `target` 可调：`both` 用例要用同一份 Product.json 但换导出目标，
    之前这里写死 `disk`，于是那条用例走了 disk 分支、server 侧从不被调用。
    """
    prod = tmp_path / "product"
    prod.mkdir(exist_ok=True)
    payload = {}
    if raw_serial is not None:
        payload["serial_number"] = raw_serial
    elif serial is not None:
        payload["serial_number"] = serial
    (prod / "Product.json").write_text(json.dumps(payload), encoding="utf-8")
    return _uploader(tmp_path,
                     f'product_file: "{prod / "Product.json"}"\n'
                     f'export_target: {target}\n'
                     f'disk_export:\n  mount_point: "{mount}"\n'
                     f"  require_volume_id: false\n")


def test_landing_spot_is_serial_then_date_then_bag(tmp_path, monkeypatch):
    """落点层级：`挂载点/<序列号>/<YYYY-MM-DD>/<bag 名>/`。

    序列号这一层是**因为盘会被多台机器轮着插** —— 不分层的话两台机器的
    数据混在一根目录里，盘上看不出来，现场也说不清哪批是谁的。
    """
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, serial="NRV-2026-0007")
    bag = _bag(tmp_path, "挥手")

    span = up._disk_subpath(bag)
    assert span.startswith("NRV-2026-0007/"), f"首层应是序列号，实际 {span}"
    date_part = span.split("/")[1]
    assert len(date_part) == 10 and date_part[4] == date_part[7] == "-", \
        f"第二层应是 YYYY-MM-DD，实际 {date_part}"


def test_date_layer_follows_capture_time_not_export_time(tmp_path, monkeypatch):
    """日期层取**采集时间**（bag 目录 mtime），不是导出当天。

    现场很平常的一件事是今天导出上周录的数据。按导出日期分层，
    那批数据会被归进今天 —— 而今天盘上可能还有当天的正常采集，
    两批不同的数据混进同一目录，现场谁也分不出来。
    """
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, serial="SN1")
    bag = _bag(tmp_path, "补录的旧数据")

    three_days_ago = time.time() - 3 * 86400
    os.utime(bag, (three_days_ago, three_days_ago))

    got = up._disk_subpath(bag).split("/")[1]
    expected = datetime.fromtimestamp(three_days_ago).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    assert got == expected, f"应按采集日期 {expected}，实际 {got}"
    assert got != today, "本用例就是要证明它不等于导出当天"


def test_each_bag_gets_its_own_directory_under_one_day(tmp_path, monkeypatch):
    """同一天多次采集：序列号/日期相同，但各自一个 bag 名目录，不互相覆盖。"""
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, serial="SN1")

    # `_disk_subpath` 只到"序列号/日期"这一层（同一天本来就该相同）；
    # **不覆盖**靠的是 `_export_to_disk` 再拼的 bag 名那一段。
    spans = [up._disk_subpath(_bag(tmp_path, n)) for n in ("第一次", "第二次")]
    assert all(s.startswith("SN1/") for s in spans), f"首层应是序列号：{spans}"

    # 完整落点（含 bag 名）必须各不相同 —— 这才是"不互相覆盖"的实际含义
    full = [f"{s}/{n}" for s, n in zip(spans, ("第一次", "第二次"))]
    assert len(set(full)) == 2, f"同一天的两次采集落点不该相同：{full}"


def test_missing_serial_falls_back_to_a_visible_placeholder(tmp_path, monkeypatch):
    """读不到序列号 → `unknown-serial`，**不是**静默扁平到挂载点根。

    扁平会让"两台机器的数据混在一起"在盘上完全看不出来，而这个盘本来
    就是多台机器轮着插的。一个显眼的占位名让这件事一眼可见，
    现场看到就知道该去查 Product.json。
    """
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, raw_serial="")     # 字段存在但为空

    assert up._disk_subpath(_bag(tmp_path, "挥手")).startswith("unknown-serial/")


def test_missing_product_file_still_exports(tmp_path, monkeypatch):
    """Product.json 整个读不到时**照样导出** —— 落到占位目录。

    读不到产品信息不该把整批数据卡住不给出去：宁可目录名难看一点，
    也不能让现场录的东西交不出来（那才是真正的数据损失）。
    """
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _uploader(tmp_path,
                   f'product_file: "{tmp_path / "不存在.json"}"\n'
                   f'export_target: disk\n'
                   f'disk_export:\n  mount_point: "{mount}"\n'
                   f"  require_volume_id: false\n")

    # 只验路径计算：真写盘需要**真挂载点**，沙箱里没有，
    # 硬要在这里验就得挂一整套替身 —— 那是上面both 那条用例的职责。
    # 关键性质是「读不到产品信息不该抛异常、不该把导出卡死」。
    assert up._disk_subpath(_bag(tmp_path, "挥手")).startswith("unknown-serial/")


@pytest.mark.parametrize("raw", ["../../etc", "a/b", "..", "/abs/path", "  "])
def test_serial_with_path_separators_cannot_escape_the_mount(raw, tmp_path, monkeypatch):
    """**路径分隔符必须被过滤掉**。

    序列号会被拼进文件系统路径，所以读到 `../../etc` 这种值时，
    拼路径这一步就该失败 —— 而不是把数据写到挂载点外面去。
    落点必须始终在挂载点**下面一层**之内。
    """
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, raw_serial=raw)

    first = up._disk_subpath(_bag(tmp_path, "挥手")).split("/")[0]
    assert first not in ("..", "") and "/" not in first, \
        f"序列号层不该含路径分隔符或相对路径，实际 {first!r}"
    assert not first.startswith("."), f"不该是隐藏目录：{first!r}"


def test_both_mode_keeps_the_remote_subpath_for_the_server_half(tmp_path, monkeypatch):
    """`both`：磁盘侧按 SN/日期分层，远端侧仍用用户选的子路径 —— 两边各按各的。

    这不是"要保持一致"，而是"两边语义本就不同"：远端那一侧是服务器目录树，
    磁盘这一侧要按机器与日期分层。
    """
    disk_spans, server_spans = [], []

    class _Fake:
        def __init__(self, mount_point="", **kw):
            self.mount_point = Path(mount_point)
            self.require_volume_id = False
            self.warn_file_bytes = 0

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
    mount = tmp_path / "usb"
    mount.mkdir()
    up = _product_config(tmp_path, mount, serial="SN-both", target="both")
    monkeypatch.setattr(up, "_upload_bag_folder_to_server",
                        lambda folder, subpath, cb=None, pct=None:
                        server_spans.append(subpath) or True)

    result = up.upload_bag_folder(_bag(tmp_path, "挥手"), "tasks/10-08")
    assert result is True, f"导出失败：{up.describe_last_error()}"
    assert disk_spans and disk_spans[0].startswith("SN-both/"), \
        f"磁盘侧应按序列号分层，实际 {disk_spans}"
    assert server_spans == ["tasks/10-08"], f"远端子路径被改了：{server_spans}"
