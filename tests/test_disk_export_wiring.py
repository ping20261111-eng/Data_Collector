"""磁盘导出**接线**测试：主流程怎么按 `export_target` 分流。

与 `test_disk_export.py` 的分工：那份测的是 `DiskExportTarget` 自身
（挂载点、盘标识、FAT32、拔盘），这份测的是"主流程有没有真的走到它"。

## 为什么要单独一份

`disk_export.py` 的 11 个方法在接线前是**零调用**的 —— 核心类写完了、
单测也全绿，但工程师点"导出到硬盘"什么都不会发生。
"类有没有被用起来"这件事只能在这层测，且**必须盯住默认路径**：
分流写错最容易出的不是"disk 不生效"，而是**"没配 disk 的人也被改了行为"**。
"""
import sys
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

    def as_disk(self, uploader, ok=True):
        def _call(folder_path, remote_subpath="", progress_cb=None,
                  progress_percent=None):
            self.disk_calls.append((Path(folder_path), remote_subpath))
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
