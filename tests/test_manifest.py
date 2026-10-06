"""录制清单（manifest）测试。

核心场景：断电后重启，要能识别出"上次没录完"并说清数据区间。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.manifest import (
    MANIFEST_NAME,
    STATUS_COMPLETE,
    STATUS_INTERRUPTED,
    STATUS_LABELS,
    STATUS_RECORDING,
    STATUS_UNKNOWN,
    BagManifest,
    list_manifests,
    scan_interrupted,
    write_info_field,
)


# ---------- 基本生命周期 ----------

def test_start_writes_recording_manifest(tmp_path):
    m = BagManifest(tmp_path)
    m.start(task_name="挥手", operator="吴工", script_type="bag")
    data = m.read()
    assert data["status"] == STATUS_RECORDING
    assert data["task_name"] == "挥手"
    assert data["operator"] == "吴工"
    assert data["started_at"]
    assert m.path.exists()


def test_finish_marks_complete(tmp_path):
    m = BagManifest(tmp_path)
    m.start(task_name="t")
    m.finish()
    assert m.status() == STATUS_COMPLETE
    assert m.read()["finished_at"]


def test_add_file_records_progress(tmp_path):
    m = BagManifest(tmp_path)
    m.start(task_name="t")
    m.add_file("a.mcap", 1024)
    m.add_file("b.mcap", 2048)
    files = m.read()["files"]
    assert len(files) == 2
    assert files[0]["name"] == "a.mcap"
    assert files[1]["size"] == 2048


# ---------- 断电场景（最关键） ----------

def test_recording_left_while_not_active_is_interrupted(tmp_path):
    """断电现场：清单写着"录制中"，但此刻没有录制进程 → 判为中断。

    这正是要解决的核心问题：工程师第二天开机，必须能知道昨天那次没录完。
    """
    m = BagManifest(tmp_path)
    m.start(task_name="挥手")
    m.add_file("a.mcap", 100)
    # 进程已死，recording_active=False
    assert m.infer_status(recording_active=False) == STATUS_INTERRUPTED
    assert m.unresolved() is True


def test_recording_with_active_process_stays_recording(tmp_path):
    """此刻确实在录 → 不该误报成中断。"""
    m = BagManifest(tmp_path)
    m.start(task_name="挥手")
    assert m.infer_status(recording_active=True) == STATUS_RECORDING
    assert m.unresolved(recording_active=True) is False
    # 传 True 才是"此刻在录"的前提；不传则按"已停止"判定为中断
    assert m.unresolved() is True


def test_complete_manifest_never_flagged_interrupted(tmp_path):
    """正常完成的录制，事后扫描不该把它报成中断（误报会让工程师白紧张）。"""
    m = BagManifest(tmp_path)
    m.start(task_name="t")
    m.finish()
    assert m.infer_status(recording_active=False) == STATUS_COMPLETE
    assert m.unresolved() is False


def test_scan_interrupted_reports_data_range(tmp_path):
    """扫描结果要带起止时间，工程师据此判断哪段数据能用。"""
    bag = tmp_path / "挥手_20260101_170000"
    bag.mkdir()
    m = BagManifest(bag)
    m.start(task_name="挥手", operator="吴工")
    m.add_file("part0.mcap", 500)

    found = scan_interrupted(tmp_path)
    assert len(found) == 1
    item = found[0]
    assert item["name"] == bag.name
    assert item["operator"] == "吴工"
    assert item["status_label"] == STATUS_LABELS[STATUS_INTERRUPTED]
    assert item["started_at"]
    assert item["last_file_at"], "应给出最后一个完成文件的时间，供判断数据区间"


def test_scan_ignores_completed(tmp_path):
    bag = tmp_path / "ok_bag"
    bag.mkdir()
    m = BagManifest(bag)
    m.start(task_name="t")
    m.finish()
    assert scan_interrupted(tmp_path) == []


def test_scan_ignores_bag_without_manifest(tmp_path):
    """没有清单的旧 bag（v6.3.5 之前录的）不能被当成中断。"""
    old = tmp_path / "legacy_bag"
    old.mkdir()
    (old / "a.mcap").write_bytes(b"x")
    assert scan_interrupted(tmp_path) == []


def test_scan_does_not_rewrite_files(tmp_path):
    """扫描只做判定、不改写 —— 判定逻辑有bug 也不能污染好数据。"""
    bag = tmp_path / "b"
    bag.mkdir()
    m = BagManifest(bag)
    m.start(task_name="t")
    before = m.path.read_text(encoding="utf-8")
    scan_interrupted(tmp_path)
    assert m.path.read_text(encoding="utf-8") == before


def test_list_manifests_only_lists_manifested(tmp_path):
    (tmp_path / "with_manifest").mkdir()
    (BagManifest(tmp_path / "with_manifest")).start(task_name="t")
    (tmp_path / "plain_dir").mkdir()
    assert [p.name for p in list_manifests(tmp_path)] == ["with_manifest"]


# ---------- 鲁棒性 ----------

def test_missing_manifest_is_unknown_not_complete(tmp_path):
    """读不出清单 → 状态未知，**不是完整**。

    把"不知道"当成"好"是这类工具最危险的默认。
    """
    m = BagManifest(tmp_path)
    assert m.read() is None
    assert m.infer_status() == STATUS_UNKNOWN
    assert m.status() == STATUS_UNKNOWN


def test_corrupt_manifest_does_not_raise(tmp_path):
    """清单损坏时返回 None，不能让"查看数据"界面打不开。"""
    (tmp_path / MANIFEST_NAME).write_text("{不是合法 JSON", encoding="utf-8")
    m = BagManifest(tmp_path)
    assert m.read() is None
    assert m.infer_status() == STATUS_UNKNOWN


def test_status_labels_cover_all_statuses():
    for s in (STATUS_RECORDING, STATUS_COMPLETE, STATUS_INTERRUPTED, STATUS_UNKNOWN):
        assert s in STATUS_LABELS, f"{s} 缺中文标签"


def test_unknown_status_value_falls_back(tmp_path):
    """清单里写了没定义的状态值 → 按未知处理，不崩。"""
    m = BagManifest(tmp_path)
    m._write({"status": "来自未来版本的状态"})
    assert m.infer_status() == STATUS_UNKNOWN


# ---------- info.json 追加（不能覆盖机器人侧数据） ----------

def test_write_info_field_creates_when_absent(tmp_path):
    assert write_info_field(tmp_path, "quality_gate", {"verdict": "ok"}) is True
    data = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert data["quality_gate"] == {"verdict": "ok"}


def test_write_info_field_preserves_robot_side_fields(tmp_path):
    """机器人侧生成的采集条件字段必须原样保留。

    机型、序列号、固件版本是事后筛选数据的关键依据，
    上位机覆盖掉它们等于毁掉这批数据的溯源能力。
    """
    (tmp_path / "info.json").write_text(json.dumps({
        "name": "挥手", "desc": "演示 #4", "version": "1.4.0",
        "hardware_model": "轮式人形", "serial_number": "R-001",
        "hand": "dex11",
    }, ensure_ascii=False), encoding="utf-8")

    write_info_field(tmp_path, "quality_gate", {"verdict": "warn"})

    data = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert data["quality_gate"]["verdict"] == "warn"
    for key in ("name", "desc", "version", "hardware_model", "serial_number", "hand"):
        assert key in data, f"原有字段 {key} 被覆盖了"
    assert data["serial_number"] == "R-001"


def test_write_info_field_updates_existing_key(tmp_path):
    (tmp_path / "info.json").write_text('{"quality_gate": {"verdict": "ok"}}',
                                        encoding="utf-8")
    write_info_field(tmp_path, "quality_gate", {"verdict": "error"})
    data = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert data["quality_gate"]["verdict"] == "error"


def test_write_info_field_survives_corrupt_source(tmp_path):
    """源文件损坏时重建而不是崩 —— 现场的数据不能因为元数据坏了就丢弃。"""
    (tmp_path / "info.json").write_text("{坏的", encoding="utf-8")
    assert write_info_field(tmp_path, "operator", "吴工") is True
    data = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert data["operator"] == "吴工"


def test_write_info_field_leaves_no_tmp_file(tmp_path):
    write_info_field(tmp_path, "k", 1)
    assert not (tmp_path / "info.json.tmp").exists()


# ---------- RecorderManager 集成 ----------

class _FakeCfg:
    def __init__(self, bag_root):
        self._bag = bag_root

    def get_bag_dir(self):
        return str(self._bag)


def _recorder(bag_root, started=True):
    """造一个只用于测试的 RecorderManager（绕过 launcher 与真实进程）。"""
    from data_collector.core_data import RecorderManager
    rm = RecorderManager(_FakeCfg(bag_root), None)
    if started:
        rm._started_at = time.time()
    return rm


def test_finalize_marks_complete_when_data_written(tmp_path):
    """有落盘 → 完整。"""
    bag = tmp_path / "挥手"
    bag.mkdir()
    (bag / "a.mcap").write_bytes(b"x" * 1000)
    rm = _recorder(tmp_path)
    rm._manifest_dir = bag
    BagManifest(bag).start(task_name="挥手")
    (bag / "b.mcap").write_bytes(b"y" * 1000)
    assert rm.finalize_manifest() == STATUS_COMPLETE
    assert BagManifest(bag).read()["status"] == STATUS_COMPLETE


def test_finalize_marks_interrupted_when_nothing_written(tmp_path):
    """脚本起来了但零落盘 → 必须标中断，不能说完整。

    这是"界面显示录制中、实际一个字节没写"的事故形态，
    收尾时若报complete，工程师会以为这段数据能用。
    """
    bag = tmp_path / "空录制"
    bag.mkdir()
    BagManifest(bag).start(task_name="空录制")
    rm = _recorder(tmp_path)
    rm._manifest_dir = bag
    assert rm.finalize_manifest() == STATUS_INTERRUPTED
    assert BagManifest(bag).read()["status"] == STATUS_INTERRUPTED


def test_finalize_ignores_metadata_only_bag(tmp_path):
    """只有 manifest/info.json 不算数据产出。

    这两个文件是上位机自己写的元数据，把它们当产出会让"空录制"蒙混过关。
    """
    bag = tmp_path / "只有元数据"
    bag.mkdir()
    (bag / "info.json").write_text('{"name":"x"}', encoding="utf-8")
    BagManifest(bag).start(task_name="只有元数据")
    rm = _recorder(tmp_path)
    rm._manifest_dir = bag
    assert rm.finalize_manifest() == STATUS_INTERRUPTED


def test_finalize_ignores_bag_directory_itself(tmp_path):
    """新建的 bag 目录本身不算产出（脚本先建目录再写数据）。"""
    bag = tmp_path / "新目录"
    bag.mkdir()
    rm = _recorder(tmp_path)
    rm._manifest_dir = bag
    assert rm.finalize_manifest() == STATUS_INTERRUPTED


def test_latest_bag_dir_picks_newest(tmp_path):
    import os
    import time as _t
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    now = _t.time()
    os.utime(old, (now - 600, now - 600))
    os.utime(new, (now, now))
    rm = _recorder(tmp_path, started=False)
    assert rm._latest_bag_dir().name == "new"
