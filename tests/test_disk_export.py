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


# ---------- 进度百分比（v6.4.2 修的现场报障） ----------

def test_progress_callback_receives_percent_not_megabytes(tmp_path, monkeypatch):
    """`progress_percent` 收的必须是**百分比**，不是已写入的 MB 数。

    现场报障「前端显示 100% 后仍过了 5 秒才完成」。根因就在这里：
    这里曾经发 `int(written / 1024 / 1024)`（已写入 MB 数），而界面
    `update_upload_progress` 会 `min(100, value)` —— 于是 3MB 的包瞬间满格，
    8000MB 的包传到 100MB 就满格。**"传输完成"与真实进度彻底脱钩**，
    现场看到满格就去拔盘，数据还没写完。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    seen = []
    total = 8 * 1024 * 1024                     # 声称总量 8MB
    payload = b"x" * (1024 * 1024)              # 实写 1MB
    t.write_stream(target, "b.zip", lambda sink: sink(payload),
                   progress_percent=seen.append, total_bytes=total)

    # 每一次回调都必须落在 0~100，且整体单调不降
    assert all(0 <= v <= 100 for v in seen), f"收到超出 0~100 的值：{seen}"
    assert seen == sorted(seen), f"进度回退了：{seen}"
    # 写入途中的值必须是 1MB/8MB ≈ 12% —— 旧实现这里会发 "1"（MB 数），
    # 8MB 的包差别不大，但换个量级就完全错了（见下一条）。
    assert seen[:-1] == [12], f"1MB/8MB 的过程进度应是 12，实际 {seen}"
    assert seen[-1] == 100, "收尾发 100"
    assert max(seen[:-1]) < 99, "写入途中不该接近满格"


def test_large_bag_does_not_look_finished_at_100_megabytes(tmp_path, monkeypatch):
    """8GB 的包传到 100MB 时，进度必须还是 1% 左右，**不能满格**。

    这条钉的是现场报障的那个量级：旧实现发的是 MB 数，界面又 `min(100, value)`
    —— 于是 100MB 就显示 100%，而实际只传了八十分之一。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    seen = []
    total = 8 * 1024 * 1024 * 1024               # 声称 8GB
    t.write_stream(target, "b.zip", lambda sink: sink(b"w" * (100 * 1024 * 1024)),
                   progress_percent=seen.append, total_bytes=total)

    mid = seen[:-1][-1]
    assert mid <= 2, f"传了 100MB/8GB，进度应约 1%，实收 {mid}%"
    assert seen[-1] == 100


def test_progress_reaches_100_only_after_the_file_is_final(tmp_path, monkeypatch):
    """100% 只在**真正完成**时出现，且一定是最后一个值。

    提前报 100 会让"满格"这个信号失去意义 —— 现场会以为可以拔盘了。
    写入过程中最多卡在 99。
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    order = []
    t.write_stream(target, "b.zip", lambda sink: sink(b"y" * 4096),
                   progress_percent=lambda v: order.append((v, (target / "b.zip").exists())),
                   total_bytes=4096)

    assert order[-1][0] == 100, f"最后应是 100，实际 {order[-1][0]}"
    assert order[-1][1] is True, "报 100 时文件必须已经落成（rename 已完成）"
    # 100 之前的所有值都不到 100
    assert all(v < 100 for v, _ in order[:-1]), f"写入途中就报了 100：{order}"


def test_no_percentage_when_total_is_unknown(tmp_path, monkeypatch):
    """总量未知时**不发百分比** —— 发一个看似合理实则错误的数字比不发更糟。

    （发 MB 数就是这种情况的旧实现：看起来在动，实际上与百分比无关。）
    """
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    seen = []
    t.write_stream(target, "b.zip", lambda sink: sink(b"z" * 8192),
                   progress_percent=seen.append)          # 不传 total_bytes

    # 只允许收尾那一个 100（表示"确实写完了"）；过程里一个都不该有 ——
    # 那种值只能从字节数瞎猜，而猜测出来的百分比比没有更误导。
    assert seen == [100], f"总量未知时只应有收尾的 100，实收 {seen}"


def test_progress_percent_is_monotonic_across_many_chunks(tmp_path, monkeypatch):
    """分块写入时进度必须单调不降（界面 bar 不该来回跳）。"""
    fake = tmp_path / "usb"
    fake.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    t = DiskExportTarget(str(fake), require_volume_id=False)
    target = t.prepare("")

    seen = []

    def producer(sink):
        for _ in range(20):
            sink(b"q" * 65536)

    t.write_stream(target, "b.zip", producer,
                   progress_percent=seen.append, total_bytes=20 * 65536)

    assert seen == sorted(seen), f"进度出现回退：{seen}"
    assert seen[-1] == 100
