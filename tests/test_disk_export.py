"""本地磁盘导出目标（移动硬盘）测试。

重点覆盖现场真实会遇到的场景：盘被拔、盘插错、FAT32 超限、
写到一半断开时是否会留下"看起来完整"的半成品。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.disk_export import (
    DeviceDisconnectedError,
    DiskExportTarget,
    is_device_lost,
)
from data_collector.errors import AppError, ErrorCode, is_retryable


# ---------- 挂载点与盘标识 ----------

def test_not_mounted_path_rejected(tmp_path):
    """普通目录不是挂载点：数据写进去不会上盘，必须拦住。

    这是最容易踩的坑——路径存在但盘没挂上，工程师以为导出成功了。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    t = DiskExportTarget(str(fake))
    assert t.is_mounted() is False
    with pytest.raises(AppError) as ei:
        t.check_volume()
    assert ei.value.code == ErrorCode.CONFIG_MISSING
    assert "不是已挂载" in ei.value.message


def test_missing_path_gives_actionable_hint(tmp_path):
    """路径不存在时要说清怎么修，而不是只报"失败"。"""
    t = DiskExportTarget(str(tmp_path / "nope"))
    with pytest.raises(AppError) as ei:
        t.check_volume()
    assert ei.value.code == ErrorCode.NOT_FOUND
    assert "mount_point" in ei.value.message


def test_volume_marker_blocks_wrong_disk(tmp_path, monkeypatch):
    """插入非数据盘必须被拒绝——防"把数据写进错盘"。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), volume_id_file=".nerv_volume",
                         require_volume_id=True)
    assert t.has_volume_marker() is False
    with pytest.raises(AppError) as ei:
        t.check_volume()
    assert ei.value.code == ErrorCode.CONFIG_MISSING
    assert "不是数据盘" in ei.value.message


def test_volume_marker_present_passes(tmp_path, monkeypatch):
    """有标识文件且是挂载点 → 校验通过。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    (fake / ".nerv_volume").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), volume_id_file=".nerv_volume",
                         require_volume_id=True)
    t.check_volume()      # 不抛异常即通过


def test_marker_check_skipped_when_not_required(tmp_path, monkeypatch):
    """require_volume_id=false 时不校验标识（自用机场景）。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), volume_id_file=".nerv_volume",
                         require_volume_id=False)
    t.check_volume()


# ---------- 体积预判（只提示不拦截） ----------

def test_oversize_not_flagged_on_exfat(tmp_path, monkeypatch):
    """exFAT 无 4GB 限制：同样的文件不该被提醒。

    误报一次，现场就不会再相信这个功能 —— 这是提醒类功能最容易死掉的方式。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    monkeypatch.setattr(t, "filesystem_type", lambda: "exfat")
    assert t.warn_oversize([("huge.mcap", 5_000_000_000)]) == []


def test_oversize_flagged_on_fat32(tmp_path, monkeypatch):
    """FAT32 上超 4GB 的文件必须被点出来。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    monkeypatch.setattr(t, "filesystem_type", lambda: "vfat")
    over = t.warn_oversize([("ok.mcap", 1024), ("huge.mcap", 5_000_000_000)])
    assert [n for n, _ in over] == ["huge.mcap"]


def test_limit_hint_mentions_exfat_remedy(tmp_path, monkeypatch):
    """FAT32 的提示要给出可执行建议（换 exFAT），不能只说"太大了"。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    t = DiskExportTarget(str(fake), require_volume_id=False)
    monkeypatch.setattr(t, "filesystem_type", lambda: "vfat")
    hint = t.describe_limit_hint()
    assert "exFAT" in hint
    monkeypatch.setattr(t, "filesystem_type", lambda: "exfat")
    assert "无 4GB" in t.describe_limit_hint()


# ---------- 空间预检 ----------

def test_space_precheck_blocks_when_insufficient(tmp_path, monkeypatch):
    fake = tmp_path / "usb"
    fake.mkdir()
    t = DiskExportTarget(str(fake), require_volume_id=False, min_free_margin_mb=1)
    monkeypatch.setattr(t, "free_space", lambda: 1024)   # 只剩 1KB
    with pytest.raises(AppError) as ei:
        t.check_free_space(10 * 1024 * 1024)
    assert ei.value.code == ErrorCode.LOCAL_SPACE
    assert "换一块盘" in ei.value.message


def test_space_precheck_allows_when_unknown(tmp_path, monkeypatch):
    """探测不到可用空间时不拦 —— 真正写入失败会自己暴露。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    t = DiskExportTarget(str(fake), require_volume_id=False)
    monkeypatch.setattr(t, "free_space", lambda: 0)
    t.check_free_space(10 * 1024 * 1024)   # 不抛异常


# ---------- 写入原子性 ----------

def test_write_stream_creates_file(tmp_path, monkeypatch):
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")
    out = t.write_stream(target, "bag.zip", lambda sink: sink(b"payload" * 100))
    assert out.exists()
    assert out.read_bytes() == b"payload" * 100
    assert not (target / "bag.zip.partial").exists(), "临时文件应被 rename 掉"


def test_write_stream_leaves_no_partial_on_success(tmp_path, monkeypatch):
    """成功路径不留 .partial，否则下次导出会看到一堆半成品。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")
    t.write_stream(target, "b.zip", lambda sink: sink(b"x" * 10))
    assert list(target.glob("*.partial")) == []


def test_device_lost_midway_raises_and_cleans(tmp_path, monkeypatch):
    """拔盘中断：抛可识别的异常，且**不留半成品**。

    留着半截文件比直接失败危险 —— 它会被下游按文件名当成正常数据取走。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("sub")

    def producer(sink):
        sink(b"first chunk")
        raise OSError(5, "Input/output error")

    with pytest.raises(DeviceDisconnectedError) as ei:
        t.write_stream(target, "bag.zip", producer)
    assert not (target / "bag.zip").exists(), "最终文件不应存在"
    assert not (target / "bag.zip.partial").exists(), "半成品必须清理"
    assert "断开" in ei.value.message


def test_device_lost_is_retryable(tmp_path, monkeypatch):
    """拔盘是外部条件，重插后应能重来 —— 不该被当成不可重试错误。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    def producer(sink):
        raise OSError(19, "No such device")

    with pytest.raises(DeviceDisconnectedError):
        t.write_stream(target, "b.zip", producer)
    assert is_retryable(ErrorCode.LOCAL_IO) is True


def test_cancel_during_write_cleans_partial(tmp_path, monkeypatch):
    """用户取消时也要清理半成品（BaseException 路径）。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    def producer(sink):
        sink(b"partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        t.write_stream(target, "b.zip", producer)
    assert list(target.glob("*.partial")) == []


# ---------- 设备丢失判定 ----------

@pytest.mark.parametrize("exc,expected", [
    (OSError(5, "Input/output error"), True),
    (OSError(19, "No such device"), True),
    (OSError(6, "No such device or address"), True),
    (OSError(13, "Permission denied"), False),
    (OSError(28, "No space left on device"), False),
])
def test_is_device_lost(exc, expected):
    """权限不足/空间不足是**别的盘**的问题，不能误报成"设备断开"。

    误报会让工程师白忙一场去换盘，真正的问题却没人管。
    """
    assert is_device_lost(exc) is expected


def test_is_device_lost_by_message():
    assert is_device_lost(OSError("device not ready")) is True
    assert is_device_lost(OSError("some other problem")) is False
