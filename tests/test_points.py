"""遥操作打点的测试：记录内容、目录定位、兜底路径，以及**两套标签库互不干扰**。

最需要钉住的三件事：
1. **相对时间**要以录制目录创建时间为基准，且基准一旦算出就不再变 ——
   否则同一份 CSV 里前面的点与后面的点基准不同，分析时对不上。
2. **定位不到录制目录时不能静默丢**：先攒着、下次补写，整场录完还没写进去就落一个
   明确命名的兜底文件。
3. **打点标签与采集任务标签是两套**：文件不同、种子不同、互不读写。
   手输的新标签只进打点库，绝不碰采集任务库。
"""
import os
import time
from pathlib import Path

from PyQt5.QtCore import Qt  # noqa: E402

import pytest

from data_collector.core_points import (CSV_HEADER, CSV_NAME, PointRecorder,
                                        PointRecord)


# ---------- 记录内容 ----------
def test_add_writes_csv_with_header_and_relative_time(tmp_path):
    bag = tmp_path / "bag"
    record_dir = bag / "布草挂架_20260920190000"
    record_dir.mkdir(parents=True)
    base = record_dir.stat().st_ctime

    recorder = PointRecorder(bag, "布草挂架", started_at=base - 5,
                            locator=lambda: record_dir)
    recorder.add("开始任务", 1, epoch=base + 12.5)
    recorder.add("双爪夹住第一个角", 2, note="左边那只", epoch=base + 18.0)

    text = (record_dir / CSV_NAME).read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert len(lines) == 3
    assert lines[1].split(",")[0] == "1"
    assert lines[1].split(",")[3] == "12.500"          # 相对录制秒
    assert lines[1].split(",")[5] == "开始任务"
    assert lines[2].split(",")[7] == "左边那只"          # 备注


def test_index_increases_and_wall_time_has_millis(tmp_path):
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)

    recorder = PointRecorder(bag, "任务", started_at=time.time() - 5,
                            locator=lambda: record_dir)
    first = recorder.add("A", None, epoch=time.time())
    second = recorder.add("B", None, epoch=time.time())

    assert (first.index, second.index) == (1, 2)
    assert len(first.wall_time.split(".")[-1]) == 3      # 毫秒三位
    assert recorder.count == 2


def test_update_changes_label_but_keeps_the_moment(tmp_path):
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    moment = record_dir.stat().st_ctime + 3

    recorder = PointRecorder(bag, "任务", started_at=moment - 10,
                            locator=lambda: record_dir)
    recorder.add("打错的标签", None, epoch=moment)
    recorder.update(1, "第一个角上夹具", 3, note="改过")

    record = recorder.records[0]
    assert record.label == "第一个角上夹具"
    assert record.label_id == 3
    assert record.note == "改过"
    assert record.epoch == moment                        # 时间不动：时间才是打点的本体
    assert "第一个角上夹具" in (record_dir / CSV_NAME).read_text(encoding="utf-8")


def test_remove_renumbers_and_rewrites(tmp_path):
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    base = record_dir.stat().st_ctime

    recorder = PointRecorder(bag, "任务", started_at=base - 10,
                            locator=lambda: record_dir)
    for offset, name in enumerate(("一", "二", "三")):
        recorder.add(name, None, epoch=base + offset)

    assert recorder.remove(2) is True
    assert [record.index for record in recorder.records] == [1, 2]
    assert [record.label for record in recorder.records] == ["一", "三"]

    rows = (record_dir / CSV_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3                                # 表头 + 两条
    assert rows[2].split(",")[5] == "三"
    assert recorder.remove(99) is False                  # 不存在的序号：明确返回 False


# ---------- 目录定位（用真实文件系统） ----------
def test_locates_nothing_when_start_time_after_everything(tmp_path):
    """起始时刻晚于所有目录 → 一个都不认（别把上一场的目录当成本场）。"""
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "布草挂架_上一场").mkdir()

    recorder = PointRecorder(bag, "布草挂架", started_at=time.time() + 100)
    assert recorder.bag_path() is None


def test_tolerance_covers_script_startup_delay(tmp_path):
    """容差要盖住"发指令 → 脚本 source 环境 → mkdir"这段延迟。

    否则现场会出现：录制明明正常开始了，打点却一直说"还没找到目录"。
    目录的 ctime 没法伪造，所以这里反过来把起始时刻放到目录创建之后 1.5 秒，
    验证它仍在 2 秒容差内被认出来。
    """
    bag = tmp_path / "bag"
    bag.mkdir()
    record_dir = bag / "布草挂架_20260920190000"
    record_dir.mkdir(parents=True)

    recorder = PointRecorder(bag, "布草挂架",
                             started_at=record_dir.stat().st_ctime + 1.5)
    assert recorder.bag_path() == record_dir


def test_locates_earliest_when_several_match(tmp_path):
    """同前缀且都晚于起始时刻时取**最早**的那个：那才是本次录制的目录。"""
    bag = tmp_path / "bag"
    bag.mkdir()
    first = bag / "布草挂架_a"
    first.mkdir()
    time.sleep(0.05)
    second = bag / "布草挂架_b"
    second.mkdir()

    recorder = PointRecorder(bag, "布草挂架", started_at=first.stat().st_ctime - 1)
    assert recorder.bag_path() == first


def test_locator_ignores_other_prefixes(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    other = bag / "别的任务_20260920190000"
    other.mkdir()

    recorder = PointRecorder(bag, "布草挂架", started_at=other.stat().st_ctime - 1)
    assert recorder.bag_path() is None
    assert "还没找到" in recorder.last_error


def test_default_name_is_script_default(tmp_path):
    """name 为空时要按脚本的默认名 `rosbag2` 去找（脚本里 NAME="rosbag2"）。"""
    bag = tmp_path / "bag"
    record_dir = bag / "rosbag2_20260920190000"
    record_dir.mkdir(parents=True)

    recorder = PointRecorder(bag, "", started_at=record_dir.stat().st_ctime - 1)
    assert recorder.bag_path() == record_dir


# ---------- 兜底路径：不静默丢 ----------
def test_unlocated_points_are_pending_then_flushed(tmp_path):
    """目录还没出现时先攒着；目录一出现，下次打点一并补写。"""
    bag = tmp_path / "bag"
    bag.mkdir()
    recorder = PointRecorder(bag, "布草挂架", started_at=time.time() - 1)
    recorder.add("开始任务", 1, epoch=time.time())

    assert recorder.pending_count == 1                   # 攒着，没丢
    assert recorder.count == 1

    # 脚本晚了几秒才把目录建出来
    record_dir = bag / "布草挂架_20260920190000"
    record_dir.mkdir()
    time.sleep(0.01)
    recorder.add("双爪夹住第一个角", 2, epoch=time.time())

    rows = (record_dir / CSV_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3                                # 表头 + 补写的两条
    assert recorder.pending_count == 0
    assert "开始任务" in rows[1]


def test_finalize_writes_orphans_to_named_file(tmp_path):
    """整场录完仍没关联上的点 → 落到一个明确命名的文件，而不是消失。"""
    bag = tmp_path / "bag"
    bag.mkdir()
    recorder = PointRecorder(bag, "对不上的名字", started_at=time.time() - 1)
    recorder.add("结束任务", 6, epoch=time.time())

    fallback = recorder.finalize()

    assert fallback is not None
    assert fallback.name.startswith("points_unlinked_")
    assert fallback.name.endswith(".csv")
    assert "结束任务" in fallback.read_text(encoding="utf-8")
    assert recorder.pending_count == 0


def test_finalize_returns_none_when_nothing_pending(tmp_path):
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    recorder = PointRecorder(bag, "任务", started_at=record_dir.stat().st_ctime - 1,
                            locator=lambda: record_dir)
    recorder.add("开始任务", 1, epoch=time.time())

    assert recorder.finalize() is None                   # 都写进去了，不必兜底


def test_write_failure_keeps_records_and_reports(tmp_path, monkeypatch):
    """写盘失败不能把记录弄丢，也不能装作成功。"""
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    recorder = PointRecorder(bag, "任务", started_at=record_dir.stat().st_ctime - 1,
                            locator=lambda: record_dir)

    def boom(*args, **kwargs):
        raise OSError("磁盘只读")

    monkeypatch.setattr(Path, "open", boom)
    recorder.add("开始任务", 1, epoch=time.time())

    assert recorder.count == 1                           # 内存里还在
    assert recorder.pending_count == 1                   # 待补写
    assert recorder.last_error                            # 有明确原因
    assert recorder.csv_path() is not None


# ---------- 相对时间基准 ----------
def test_relative_base_is_the_record_dir_creation_time(tmp_path):
    """基准取目录创建时间，而不是发指令时间 —— 脚本 source 环境的几秒不该算进去。"""
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    created = record_dir.stat().st_ctime

    recorder = PointRecorder(bag, "任务", started_at=created - 6,   # 发指令早 6 秒
                            locator=lambda: record_dir)
    record = recorder.add("开始任务", 1, epoch=created + 10)

    assert record.rel_seconds == pytest.approx(10.0, abs=0.05)


def test_relative_base_is_fixed_after_first_use(tmp_path):
    """基准首次算出后固定：晚到的目录不能把已有点的比较口径换掉。"""
    bag = tmp_path / "bag"
    bag.mkdir()
    started = time.time() - 3
    recorder = PointRecorder(bag, "任务", started_at=started)

    first = recorder.add("开始任务", 1, epoch=started + 1)     # 目录还没出现
    base_before = recorder.base_epoch()

    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir()                                          # 目录现在出现了

    assert recorder.base_epoch() == base_before                 # 基准没变
    second = recorder.add("第二个", 2, epoch=started + 2)
    assert second.rel_seconds > first.rel_seconds               # 同一口径下仍然递增


def test_clock_never_negative_on_clock_skew(tmp_path):
    """时钟回拨（点早于基准）不给负数 —— 负的相对秒在下游会被当成脏数据。"""
    bag = tmp_path / "bag"
    record_dir = bag / "任务_20260920190000"
    record_dir.mkdir(parents=True)
    created = record_dir.stat().st_ctime

    recorder = PointRecorder(bag, "任务", started_at=created,
                            locator=lambda: record_dir)
    record = recorder.add("开始任务", 1, epoch=created - 30)

    assert record.rel_seconds == 0.0


# ============ 界面与标签库隔离 ============
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def point_env(tmp_path, qapp):
    """带打点标签库与 bag 目录的环境：两套标签库各自落到独立的临时文件。"""
    from data_collector.core import ConfigManager, save_yaml

    bag = tmp_path / "bag"
    bag.mkdir()
    cfg = tmp_path / "app_config.yaml"
    save_yaml(cfg, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "bag_dir": str(bag),
        "local_log_dir": str(tmp_path / "logs"),
        "action_labels_file": str(tmp_path / "action_labels.yaml"),
        "point_labels_file": str(tmp_path / "point_labels.yaml"),
        "record_action_labels": ["布草挂架", "矿泉水瓶分拣"],
        # 打点标签按采集任务分组：通用组 + 各任务组（"*" 是通用组的固定键）
        "point_labels": {
            "*": [{"name": "开始任务", "action_id": 1},
                  {"name": "结束任务", "action_id": 2}],
            "布草挂架": [{"name": "双爪夹住第一个角", "action_id": 1},
                         {"name": "第一个角上夹具", "action_id": 2}],
        },
    })
    return ConfigManager(cfg), bag


def _make_recorder(bag, name="布草挂架", task="布草挂架"):
    record_dir = bag / f"{name}_20260920190000"
    record_dir.mkdir(parents=True, exist_ok=True)
    recorder = PointRecorder(bag, name, started_at=record_dir.stat().st_ctime - 3,
                             locator=lambda: record_dir, task=task)
    return recorder, record_dir


def _label_item(dialog, name):
    """按名字取标签行（列表里有分节标题行，用行号定位会指错）。"""
    for row in range(dialog.label_list.count()):
        item = dialog.label_list.item(row)
        if item.data(Qt.UserRole) == name:
            return item
    raise AssertionError(f"标签列表里没有「{name}」")


def _pick_label(dialog, name):
    """点一下某个标签 —— 现在这一下就等于记一个点。"""
    item = _label_item(dialog, name)
    dialog.label_list.setCurrentRow(dialog.label_list.row(item))
    dialog.on_label_clicked(item)
    return item


def test_two_label_stores_do_not_touch_each_other(point_env):
    """打点标签与采集任务标签是两套：文件不同、内容互不读写。

    现场明确要求过"不要混在一起"—— 任务名（布草挂架）与时刻标注（第一个角上夹具）
    不是一个层级，混一份会让录制时那个下拉框里塞满用不上的条目。
    """
    config, _bag = point_env

    assert config.get_point_labels_file() != config.get_action_labels_file()
    assert [item["name"] for item in config.get_point_labels()] == [
        "开始任务", "结束任务", "双爪夹住第一个角", "第一个角上夹具"]
    assert [item["name"] for item in config.get_action_labels()] == [
        "布草挂架", "矿泉水瓶分拣"]

    # 往打点库加一条：采集任务库必须纹丝不动
    config.point_labels.upsert("第二个角上夹具", 3, task="布草挂架")
    assert "第二个角上夹具" in [i["name"] for i in config.get_point_labels()]
    assert [i["name"] for i in config.get_action_labels()] == [
        "布草挂架", "矿泉水瓶分拣"]

    # 反过来也一样
    config.action_labels.upsert("另一个活儿")
    assert "另一个活儿" not in [i["name"] for i in config.get_point_labels()]


def test_input_path_records_and_autostores_new_label(point_env):
    """手输的新标签自动沉淀进**打点库**（当前任务组），并当场写进 CSV。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    dialog.edit_label.setText("这台机器的专用标签")
    dialog.edit_note.setText("备注一下")
    dialog.on_record_input()

    assert recorder.count == 1
    record = recorder.records[0]
    assert record.label == "这台机器的专用标签"
    assert record.note == "备注一下"
    # 手输的新标签先落进当前任务组、再算序号，所以它当场就有一个号：
    # 没有组内编号 → 排在任务组末尾 → 也就是"结束任务"前面那一位（本例是 4）。
    # 这样面板刷新后显示的序号与记录里的编号仍然一致。
    assert record.label_id == 4
    # 输入框与备注框都清空：备注只作用于"下一个点"
    assert dialog.edit_label.text() == ""
    assert dialog.edit_note.text() == ""

    assert "这台机器的专用标签" in [i["name"] for i in config.get_point_labels()]
    assert "这台机器的专用标签" not in [i["name"] for i in config.get_action_labels()]
    assert "这台机器的专用标签" in (record_dir / CSV_NAME).read_text(encoding="utf-8")


def test_single_label_click_records_a_point(point_env):
    """点一下标签就以那一刻记一个点，带组内编号；面板**不关**，接着打下一个。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    before = time.time()
    _pick_label(dialog, "双爪夹住第一个角")
    after = time.time()

    assert recorder.count == 1
    record = recorder.records[0]
    assert record.label == "双爪夹住第一个角"
    # 写进记录的是**面板里显示的序号**（流程里的位置），不是组内编号 ——
    # 第一版写组内编号，面板显示 2、记录里写 1，按编号对齐动作时就会错位
    assert record.label_id == 2
    # 时刻就是"点下去那一刻"（不是选完标签之后）：一个点一次点击
    assert before <= record.epoch <= after

    # 不关窗：第二个点照样点一下就行（v4.9 是"保存后关闭、下一个点再开一次窗"）
    _pick_label(dialog, "第一个角上夹具")
    assert recorder.count == 2
    assert [r.label for r in recorder.records] == ["双爪夹住第一个角", "第一个角上夹具"]


def test_double_click_records_only_one_point(point_env):
    """一次双击会被 Qt 拆成 itemClicked×2 + itemActivated：不去重就是一条点击记三个点。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)
    item = _label_item(dialog, "开始任务")

    dialog.on_label_clicked(item)       # 双击的第一下
    dialog.on_label_clicked(item)       # 第二下
    dialog.on_label_activated(item)     # 双击 / 键盘 Enter 也会发这个
    assert recorder.count == 1

    # 换一个标签不受去重影响（去重的键是"同一个标签 + 时间窗"）
    _pick_label(dialog, "结束任务")
    assert recorder.count == 2


def test_panel_is_not_modal(point_env):
    """面板必须非模态：录制中操作员随时要按主窗口的「停止录制」和急停柱。"""
    from PyQt5.QtCore import Qt

    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    assert dialog.isModal() is False
    assert dialog.windowModality() == Qt.NonModal


def test_note_applies_to_the_next_point_only(point_env):
    """备注框的内容记在下一个点上，记完清空（不会再挂到后面的点）。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    dialog.edit_note.setText("左边那只")
    _pick_label(dialog, "开始任务")
    assert recorder.records[0].note == "左边那只"
    assert dialog.edit_note.text() == ""

    _pick_label(dialog, "结束任务")
    assert recorder.records[1].note == ""


def test_selecting_a_row_does_not_change_what_a_click_does(point_env):
    """选中一行只是"准备改/删"，点标签仍然照常打点。

    v4.9 里选中一行会进入编辑模式、把"点标签"变成"填入输入框"，于是操作员点完一行
    再点标签就什么都不发生 —— 现场只会以为坏了。改/删现在必须显式点按钮。
    """
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    recorder.add("打错的", None, epoch=record_dir.stat().st_ctime + 4)

    dialog = PointDialog(config, recorder)
    dialog.records_list.setCurrentRow(0)                 # 选中已打的那条
    assert dialog.btn_edit.isEnabled() and dialog.btn_delete.isEnabled()

    _pick_label(dialog, "结束任务")
    assert recorder.count == 2                           # 照常记录，不是"填入输入框"


def test_empty_input_records_a_placeholder_point(point_env):
    """空输入直接打点 → 记成「点1（待命名）」，而不是拦下来报错。

    现场常有"先记下来、回头再命名"的那一刻，那一下等不起你想名字。
    """
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    dialog.on_record_input()

    assert recorder.count == 1
    record = recorder.records[0]
    assert record.label == "点1（待命名）"
    assert record.label_id is None                   # 占位名不在流程里，没有序号
    assert "占位名" in dialog.status_label.text()
    # 占位名不写进标签库（否则标签列表会被"点N（待命名）"堆满）
    assert "点1（待命名）" not in [i["name"] for i in config.get_point_labels()]
    texts = [dialog.label_list.item(i).text() for i in range(dialog.label_list.count())]
    assert not any("待命名" in text for text in texts)


def _click_record(dialog):
    """按一次「打点」（模拟"隔一会儿再点一次"）。

    清掉双击去重窗口的状态：那个 0.6 秒窗口是给"一次双击被 Qt 拆成三条事件"用的，
    用例里两次点击之间没有真实时间间隔，不清就全被当成重复。
    """
    dialog._last_label, dialog._last_record_at = "", 0.0
    dialog.on_record_input()


def test_placeholder_names_count_up_without_reuse(point_env):
    """连打两个占位点：点1、点2；删掉点1 再打 → 点3（不复用已经用过的名字）。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    _click_record(dialog)
    assert [r.label for r in recorder.records] == ["点1（待命名）"]

    _click_record(dialog)
    assert [r.label for r in recorder.records] == ["点1（待命名）", "点2（待命名）"]

    recorder.remove(1)                            # 删掉点1
    _click_record(dialog)
    assert [r.label for r in recorder.records] == ["点2（待命名）", "点3（待命名）"]


def test_record_button_is_the_prominent_action(qapp, point_env):
    """打点按钮：文案就是「打点」，专属醒目层级，且明显比旁边的次级按钮大。

    量的是**布局之后的实际尺寸**而不是 `sizeHint()`：打点按钮的高度是代码
    写死的（`ROW_H`，为了和同一行的输入框严格等高），纵向内距已经是 0，
    于是 `sizeHint()` 只反映字体高度，跟它真正占的地方不是一回事。
    要钉住的是"现场看到的那颗按钮有多大"。
    """
    from data_collector import theme as _theme
    from data_collector.ui.point_dialog import PointDialog

    _theme.apply_theme(qapp, "light")
    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)
    dialog.show()
    dialog.ensurePolished()
    qapp.processEvents()

    assert dialog.btn_record_input.text() == "打点"
    assert dialog.btn_record_input.property("variant") == "point"
    big = dialog.btn_record_input.size()
    small = dialog.btn_manage_labels.size()              # 旁边那个次级药丸按钮
    assert big.width() > small.width()
    assert big.height() > small.height()
    assert big.height() >= 44                            # 触摸目标下限（现场戴薄手套）
    dialog.close()


def test_hint_lines_are_drawn_lighter_than_typed_text(qapp, point_env):
    """两个输入框的提示文字必须比正文淡（自绘提示，QSS 压不住它）。

    判据用"与背景的对比度"而不是具体色值：这样亮/暗两套主题都成立，
    也正好对应现场的说法"把提示字改淡一点"。
    """
    from PyQt5.QtGui import QColor, QPixmap

    from data_collector import theme as _theme
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)

    def ink_contrast(widget, typed=""):
        """返回（提示文字的对比度, 正文的对比度）。"""
        widget.setText(typed)
        widget.resize(300, 36)
        widget.show()
        qapp.processEvents()
        shot = QPixmap(widget.size())
        shot.fill(QColor("#ff00ff"))
        widget.render(shot)
        img = shot.toImage()
        colors = {}
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixel(x, y)
                colors[px] = colors.get(px, 0) + 1
        bg = QColor(max(colors, key=colors.get))
        ink = [QColor(px) for px, n in colors.items() if px != max(colors, key=colors.get) and n > 3]
        if not ink:
            return 0.0, bg
        darkest = min(ink, key=lambda c: c.lightness())
        # 对比度用"墨色与背景的亮度差"近似即可（同一控件内比较，够判"谁更淡"）
        return abs(bg.lightness() - darkest.lightness()), bg

    for mode in ("light", "dark"):
        _theme.apply_theme(qapp, mode)
        dialog = PointDialog(config, recorder)
        dialog.show()
        qapp.processEvents()
        hint_gap, _bg = ink_contrast(dialog.edit_note, typed="")          # 只有提示
        text_gap, _bg2 = ink_contrast(dialog.edit_note, typed="WWWWWW")   # 只有正文
        assert hint_gap < text_gap, f"{mode}：提示 {hint_gap} 应比正文 {text_gap} 淡"
        dialog.close()
    _theme.apply_theme(qapp, "light")


def test_edit_dialog_keeps_the_moment(point_env):
    """改标签不动时间：时间才是打点的本体（改的是标注，不是时刻）。"""
    from data_collector.ui.point_dialog import PointEditDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    moment = record_dir.stat().st_ctime + 7
    recorder.add("打错的", None, epoch=moment)

    editor = PointEditDialog(config, recorder, 1)
    assert editor.edit_label.text() == "打错的"           # 预填原标签
    assert editor.edit_note.text() == ""

    editor.edit_label.setText("开始任务")
    editor.on_save()

    assert editor.saved is True
    assert recorder.records[0].label == "开始任务"
    assert recorder.records[0].epoch == moment          # 时间不动
    assert recorder.records[0].label_id == 1            # 通用组里的编号


def test_dialog_delete_removes_the_point(point_env):
    """打错能删（确认框在测试里被统一点掉）。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    times = [record_dir.stat().st_ctime + offset for offset in (1, 2, 3)]
    for offset, moment in enumerate(times):
        recorder.add(f"点{offset + 1}", None, epoch=moment)

    dialog = PointDialog(config, recorder)
    dialog.records_list.setCurrentRow(1)                 # 中间那条
    dialog.on_delete_record()

    assert [record.label for record in recorder.records] == ["点1", "点3"]
    assert [record.index for record in recorder.records] == [1, 2]


def test_manage_labels_opens_the_grouped_manager(point_env, monkeypatch):
    """「管理标签…」开的是**按任务分组的**打点标签管理，且只作用于打点库。"""
    from data_collector.ui import point_dialog as module
    from data_collector.ui.point_dialog import PointDialog

    seen = {}

    class FakeDialog:
        def __init__(self, config, parent=None):
            seen["config"] = config

        def exec_(self):
            return 0

    monkeypatch.setattr(module, "PointLabelDialog", FakeDialog)

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)
    dialog.on_manage_labels()

    assert seen["config"] is config
    # 进的是"按任务分组"的那套库，采集任务库仍是扁平的
    assert config.point_labels.grouped is True
    assert config.action_labels.grouped is False


def test_action_label_dialog_copy_follows_kind(point_env):
    """同一个管理界面，两套库的文案必须分开（不能给打点标签印上"写进 info.json"）。"""
    from data_collector.ui.dialogs import ActionLabelDialog

    config, _bag = point_env
    point_dialog = ActionLabelDialog(config, None, store=config.point_labels, kind="打点标签")
    action_dialog = ActionLabelDialog(config, None)

    assert "打点标签" in point_dialog.windowTitle()
    assert "points.csv" in point_dialog.layout().itemAt(0).widget().text()
    assert "管理采集任务" == action_dialog.windowTitle()
    assert "info.json" in action_dialog.layout().itemAt(0).widget().text()
    point_dialog.close()
    action_dialog.close()


class _RecorderStub:
    def __init__(self, count=0):
        self.count = count


def test_point_button_visibility_follows_recording_state(qapp):
    """按钮可见 = 真的能打点：录制中且本会话有打点会话才显示。"""
    from PyQt5.QtWidgets import QPushButton

    from data_collector.ui.panels.data_ops import DataOpsMixin

    class _Fake:
        def __init__(self):
            self.btn_point = QPushButton("打点")
            self.btn_point.setVisible(True)
            self.point_recorder = None
            self.recorder = type("R", (), {"is_active": staticmethod(lambda: False)})()

    window = _Fake()

    # 没录制 → 隐藏（打点没有录制起点就没有意义，灰按钮会让人反复点）
    DataOpsMixin._sync_point_button(window)
    assert window.btn_point.isHidden()

    # 录制中但没有打点会话（例如重开界面接管一个正在跑的录制）→ 仍然隐藏
    window.recorder.is_active = lambda: True
    DataOpsMixin._sync_point_button(window)
    assert window.btn_point.isHidden()

    # 录制中且有会话 → 显示，并带本次已打点数
    window.point_recorder = _RecorderStub(count=2)
    DataOpsMixin._sync_point_button(window)
    assert not window.btn_point.isHidden()
    assert window.btn_point.text() == "打点 (2)"


def test_on_point_without_session_explains_instead_of_crashing(qapp, point_env):
    """没有打点会话时点打点：说清原因，不抛异常（槽里抛异常会 abort 整个界面）。"""
    from data_collector.ui.panels.data_ops import DataOpsMixin

    config, _bag = point_env

    class _Fake:
        def __init__(self):
            self.config = config
            self.btn_point = None
            self.point_recorder = None
            self.recorder = type("R", (), {"is_active": staticmethod(lambda: False)})()
            self.messages = []

        def append(self, text):
            self.messages.append(text)

    window = _Fake()
    DataOpsMixin.on_point(window)          # 不该抛


# ============ 按采集任务分组的打点标签 ============
def test_labels_are_served_by_the_recording_task(point_env):
    """选了布草挂架 → 面板给的是**该任务的流程**（通用首项 → 任务标签 → 通用末项）。"""
    config, _bag = point_env

    names = [i["name"] for i in config.get_point_label_flow("布草挂架")]
    assert names == ["开始任务", "双爪夹住第一个角", "第一个角上夹具", "结束任务"]

    # 还没配标签的任务只有通用那两项（不会把别的任务的标签塞过来）
    assert [i["name"] for i in config.get_point_label_flow("矿泉水瓶分拣")] == [
        "开始任务", "结束任务"]


def test_empty_task_shows_only_common_labels(point_env):
    """本次录制没选任务 → 只显示通用标签。

    把全部任务的标签都摆出来看着方便，但很容易选到不属于本次任务的标签，
    而这种标错在录制中途看不出来。
    """
    config, _bag = point_env
    served = config.get_point_label_flow("")
    assert [i["name"] for i in served] == ["开始任务", "结束任务"]
    assert all(i["task"] == config.point_labels.COMMON_TASK for i in served)


def test_ids_are_unique_per_task_not_globally(point_env):
    """编号只要求**组内**唯一：不同任务各有各的 1、2、3…"""
    config, _bag = point_env
    store = config.point_labels

    # 布草挂架组已经有编号 1 → 组内再来一个 1 要拦下
    ok, message = store.upsert("另一个", 1, task="布草挂架")
    assert ok is False and "占用" in message

    # 换个任务用同一个编号 → 允许（否则第二个任务一个编号都填不进去）
    ok, _message = store.upsert("开始抓取", 1, task="矿泉水瓶分拣")
    assert ok is True

    # 同名不同组也允许
    ok, _message = store.upsert("开始任务", 9, task="矿泉水瓶分拣")
    assert ok is True
    assert [i["name"] for i in config.get_point_label_flow("矿泉水瓶分拣")] == [
        "开始任务", "开始抓取", "开始任务", "结束任务"]


def test_delete_and_edit_only_touch_the_selected_group(point_env):
    """删/改只作用在指定分组里：同名标签在别的组里不受影响。"""
    config, _bag = point_env
    store = config.point_labels
    store.upsert("开始任务", 9, task="矿泉水瓶分拣")

    ok, _message = store.remove("开始任务", task="")   # 通用组
    # 通用组里的「开始任务」被删掉，矿泉水瓶分拣组里的同名标签还在
    assert ok is True
    assert [i["name"] for i in config.get_point_label_flow("")] == ["结束任务"]
    assert "开始任务" in [i["name"] for i in config.get_point_label_flow("矿泉水瓶分拣")]


def _section_head(dialog):
    """标签区标题行的全部文字（标题 + 计数提示 + 任务徽章）。"""
    return " | ".join((dialog.label_title.text(),
                       dialog.label_hint.text(),
                       dialog.label_task.text()))


def test_dialog_shows_one_ordered_flow(point_env):
    """面板里是**一条按顺序下来的流程**：通用首项 → 本任务标签 → 通用末项，序号 1..N。

    分两段列（通用 1、2 + 任务 1、2、3、4）现场看着像编号重复了，所以合并成一条。
    """
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag, task="布草挂架")
    dialog = PointDialog(config, recorder)

    texts = [dialog.label_list.item(i).text() for i in range(dialog.label_list.count())]
    assert [t.split("    ")[-1] for t in texts] == [
        "开始任务", "双爪夹住第一个角", "第一个角上夹具", "结束任务"]
    assert [int(t.split("    ")[0]) for t in texts] == [1, 2, 3, 4]
    # 卡片自绘用的是 data 里的序号与名字（文本只是纯文本表示），两者必须一致
    assert [dialog.label_list.item(i).data(Qt.UserRole + 1) for i in range(4)] == [1, 2, 3, 4]
    assert [dialog.label_list.item(i).data(Qt.UserRole) for i in range(4)] == [
        "开始任务", "双爪夹住第一个角", "第一个角上夹具", "结束任务"]
    # 不再有"通用（所有任务）"这类分段标题，也就没有"通用"两个字占版面
    assert not any(t.startswith("──") for t in texts)
    assert "通用" not in _section_head(dialog)
    # 标题要说清"点一下就是记一个点"，否则没人敢点
    assert "点一下即记录此刻" in dialog.label_title.text()
    # 任务名从标题里搬到了标题行右侧的徽章上：标签就是按任务给的，要挨着它说
    assert dialog.label_task.text() == "布草挂架"
    assert dialog.label_hint.text() == "共 4 个"


def test_dialog_without_task_shows_common_and_says_why(point_env):
    """没选任务时：只显示通用标签，并在徽章上明说原因。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag, name="rosbag2", task="")
    dialog = PointDialog(config, recorder)

    texts = [dialog.label_list.item(i).text() for i in range(dialog.label_list.count())]
    assert [t.split("    ")[-1] for t in texts] == ["开始任务", "结束任务"]
    assert [int(t.split("    ")[0]) for t in texts] == [1, 2]
    assert not any("双爪夹住第一个角" in t for t in texts)
    assert dialog.label_task.text() == "未选采集任务"
    # 用 warn 色说清"为什么只有这两项"，而不是让现场以为标签丢了
    assert dialog.label_task.property("state") == "warn"
    assert "只列通用标签" in dialog.label_task.toolTip()


def test_new_label_lands_in_the_recording_task_group(point_env):
    """手输的新标签沉淀进**当前任务那一组**，不往通用组里塞。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag, task="布草挂架")
    dialog = PointDialog(config, recorder)

    dialog.edit_label.setText("临时发现的标签")
    dialog.on_record_input()

    stored = config.get_point_labels()
    landed = [i for i in stored if i["name"] == "临时发现的标签"]
    assert len(landed) == 1
    assert landed[0]["task"] == "布草挂架"
    assert landed[0]["task"] != config.point_labels.COMMON_TASK


def test_manager_lists_tasks_plus_common_plus_orphan_groups(point_env):
    """管理界面左侧：通用 → 采集任务列表 → 只剩标签的孤儿组（标注出来）。"""
    from data_collector.ui.point_labels_dialog import PointLabelDialog

    config, _bag = point_env
    config.point_labels.upsert("遗留标签", None, task="已经删掉的任务")

    manager = PointLabelDialog(config, None)
    groups = manager._groups()
    keys = [key for key, _text in groups]
    labels = {key: text for key, text in groups}

    assert keys[0] == config.point_labels.COMMON_TASK          # 通用恒在第一位
    assert "布草挂架" in keys
    assert "矿泉水瓶分拣" in keys                              # 采集任务里有的都列出
    assert "已经删掉的任务" in keys                             # 孤儿组不丢
    assert "已不在采集任务里" in labels["已经删掉的任务"]        # 且标注出来
    assert "通用" in labels[config.point_labels.COMMON_TASK]
    manager.close()


def test_manager_edits_the_selected_group(point_env):
    """管理界面里新增/删除都作用在左侧选中的那一组。"""
    from data_collector.ui.point_labels_dialog import PointLabelDialog

    config, _bag = point_env
    manager = PointLabelDialog(config, None)

    # 切到「布草挂架」
    for row in range(manager.groups_list.count()):
        if manager.groups_list.item(row).data(Qt.UserRole) == "布草挂架":
            manager.groups_list.setCurrentRow(row)
            break
    assert manager._group == "布草挂架"

    manager.name_edit.setText("补一个标签")
    manager.id_edit.setText("9")
    manager.add_label()

    added = [i for i in config.get_point_labels() if i["name"] == "补一个标签"]
    assert len(added) == 1 and added[0]["task"] == "布草挂架"
    assert "补一个标签" in [manager.labels_list.item(i).text().split("    ")[-1]
                        for i in range(manager.labels_list.count())]
    manager.close()


# ============ 打点入口：录制一开始就出现 ============
# 现场反馈：录制已经跑起来了，操作区却没有「打点」，打点面板也没出现。
# 根因是起录路径里 `_set_record_button("recording")` 排在 `point_recorder` 赋值**之前** ——
# `_sync_point_button` 读到会话还是 None，就按"不可用"把按钮藏了，而之后没有任何地方
# 再同步它，按钮就此失踪到录制结束。下面这几条钉住"顺序 + 自动弹出 + 收尾回收"。
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QLabel, QPushButton, QWidget  # noqa: E402

from data_collector.ui.panels.data_ops import DataOpsMixin  # noqa: E402


class _RecordingStub:
    """RecorderManager 的替身：只提供 on_record / on_stop_record / 看门狗用到的能力。

    `health_result` 与 `disk_full_result` 让用例能摆出"没落盘""进程已死""磁盘满"
    这几种现场状态 —— 它们在真机上都是 `RecorderManager` 内部算出来的。
    `stop()` 故意**不清** `started_at`：那正是"进程没了但会话还在、界面没复位"
    那个故障窗口（v6.4.13 修的就是它）。
    """

    def __init__(self):
        self._active = False
        self.started_at = time.time()
        self.start_calls = 0
        self.health_result = {"alive": True, "producing": True, "message": ""}
        self.disk_full_result = ""

    def is_active(self):
        return self._active

    def start(self, *_args):
        self._active = True
        self.started_at = time.time()
        self.start_calls += 1
        return True

    def stop(self):
        self._active = False
        return True

    def health(self, *_args, **_kwargs):
        return self.health_result

    def disk_full(self):
        return self.disk_full_result


class _UploaderStub:
    def get_bag_folders(self):
        return []


class _RecordHarness(QWidget, DataOpsMixin):
    """把 on_record / on_stop_record 需要的那点东西搭起来。

    必须是真 QWidget：打点面板拿它当父窗口，非 QWidget 的父窗口 PyQt 会直接报错。
    """

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.messages = []
        self.btn_record = QPushButton("数据录制")
        self.btn_point = QPushButton("打点")
        self.btn_point.setVisible(False)
        self.record_status_label = QLabel("录制 未开始")
        self.record_timer = QTimer(self)
        self.record_seconds = 0
        self.record_start_time = None
        self._record_health_warned = False
        self.point_recorder = None
        self.point_panel = None
        self.recorder = _RecordingStub()
        self.uploader = _UploaderStub()

    def append(self, text):
        self.messages.append(text)

    def show_error(self, title, detail):
        self.messages.append(f"{title}: {detail}")


def _patch_record_dialog(monkeypatch, name="布草挂架", label="布草挂架"):
    """把录制弹窗换成"填好了、直接确定"的替身（用例不该真的开一个模态框）。"""
    from PyQt5.QtWidgets import QDialog

    from data_collector.ui.panels import data_ops as module

    class FakeDialog:
        def __init__(self, parent=None, config=None):
            pass

        def exec_(self):
            return QDialog.Accepted

        def get_settings(self):
            return {"type": "rosbag2", "name": name, "desc": "用例", "label": label,
                    "action_id": None}

    monkeypatch.setattr(module, "RecordDialog", FakeDialog)


def test_point_entry_and_panel_appear_when_recording_starts(qapp, point_env, monkeypatch):
    """起录后：入口按钮出现 + 面板自动弹出（现场 bug 的回归）。"""
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.on_record()

    assert harness.point_recorder is not None
    assert not harness.btn_point.isHidden()                  # 入口出现
    assert harness.point_panel is not None                   # 面板自动弹出
    assert harness.point_panel.isVisible()
    assert harness.point_panel.recorder is harness.point_recorder
    assert any("打点面板已弹出" in message for message in harness.messages)
    harness.point_panel.close()


def test_panel_closes_and_entry_hides_when_recording_stops(qapp, point_env, monkeypatch):
    """收尾后：面板关掉、入口隐藏、会话解散 —— 面板不能挂在已结束的会话上。"""
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)
    harness.on_record()
    assert harness.point_panel is not None

    harness.on_stop_record()

    assert harness.point_panel is None
    assert harness.point_recorder is None
    assert harness.btn_point.isHidden()


# ---------- 录制看门狗与按钮分派（v6.4.13：现场"无法停止录制"的根因）----------


def test_watchdog_resets_ui_even_after_warning_latched(qapp, point_env, monkeypatch):
    """**核心回归**：告警闩锁已经置位之后，进程死亡也必须把界面复位。

    现场故障链：机器人磁盘满 → bag 写不进去 → 第一秒报一次"没有新文件落盘"、
    闩锁置 True → 脚本随后退出 → 此后每秒都因"已告警过"提前 return →
    **界面永远停在「录制中」**，按钮永远显示「停止录制」。

    修复前这一段嵌在"首次告警"分支里，本用例必然红。
    """
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.on_record()                               # 正常起录
    assert harness.btn_record.text() == "停止录制"
    harness.point_panel.close()

    # 第 1 秒：进程还在，但没有新文件落盘（磁盘满的典型表现）→ 报一次
    harness.recorder.health_result = {"alive": True, "producing": False,
                                      "message": "bag 目录下还没有新文件落盘"}
    harness.update_record_time()
    assert harness._record_health_warned is True
    assert harness.btn_record.text() == "停止录制"     # 此刻确实还在录

    # 脚本随后自行退出 —— 闩锁已经是 True 了
    harness.recorder.health_result = {"alive": False, "producing": False,
                                      "message": "录制进程已退出"}
    harness.update_record_time()

    assert harness.record_status_label.text() == "录制 已中断"
    assert harness.btn_record.text() == "数据录制"
    assert harness.record_timer.isActive() is False
    assert any("录制进程已退出" in m for m in harness.messages)


def test_watchdog_still_warns_only_once_while_process_alive(qapp, point_env):
    """进程还活着时，同一件事只报一次 —— 节流不能因为上面的修复而丢掉。"""
    config, _bag = point_env
    harness = _RecordHarness(config)
    harness.recorder.health_result = {"alive": True, "producing": False,
                                      "message": "bag 目录下还没有新文件落盘"}

    harness.update_record_time()
    harness.update_record_time()
    harness.update_record_time()

    warns = [m for m in harness.messages if m.startswith("录制异常")]
    assert len(warns) == 1


def test_watchdog_resets_latch_when_recording_restarts(qapp, point_env, monkeypatch):
    """起录必须复位闩锁：它原先只在"又开始落盘"时复位，于是报过一次之后就再也不提示。"""
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.recorder.health_result = {"alive": True, "producing": False, "message": "没落盘"}
    harness.update_record_time()
    assert harness._record_health_warned is True

    harness.on_record()
    assert harness._record_health_warned is False
    harness.point_panel.close()


def test_record_toggle_dispatches_to_stop_when_session_remains(qapp, point_env, monkeypatch):
    """进程已死但会话还在时，点按钮的意图是"停下来"，不能变成"新建录制"。

    按钮显示的是 `_set_record_button` 记下的状态，而进程可能在看门狗跑下一次之前
    就死了。原来按 `is_active()` 分派 → 走进 `on_record()` → 弹出新建录制对话框，
    现场看到的正是"点了停止却弹出一个新建录制的窗口，怎么都停不下来"。
    """
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.recorder._active = False                   # 进程已经没了
    assert harness.recorder.started_at is not None     # 但这次会话还在
    harness._set_record_button("recording")           # 按钮仍显示「停止录制」
    assert harness.btn_record.text() == "停止录制"

    called = []
    monkeypatch.setattr(harness, "on_stop_record", lambda: called.append("stop"))
    monkeypatch.setattr(harness, "on_record", lambda: called.append("new"))

    harness.on_record_toggle()
    assert called == ["stop"]


def test_record_toggle_starts_when_no_session(qapp, point_env, monkeypatch):
    """反向钉住：真的没有会话时，点按钮仍然是"新建录制"。"""
    config, _bag = point_env
    harness = _RecordHarness(config)
    harness.recorder._active = False
    harness.recorder.started_at = None

    called = []
    monkeypatch.setattr(harness, "on_stop_record", lambda: called.append("stop"))
    monkeypatch.setattr(harness, "on_record", lambda: called.append("new"))

    harness.on_record_toggle()
    assert called == ["new"]


def test_exit_hint_names_full_disk(qapp, point_env):
    """进程退出时若磁盘已满，要把原因说出来 —— 脚本输出不落盘，界面是唯一的出口。"""
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.recorder.disk_full_result = "录制盘已满（bag 目录所在分区可用空间为 0）"
    assert "录制盘已满" in harness._record_exit_hint({})

    harness.recorder.disk_full_result = ""
    hint = harness._record_exit_hint({})
    assert "检查 bag 目录" in hint and "录制盘已满" not in hint


def test_watchdog_exit_message_carries_disk_reason(qapp, point_env):
    """整条链：磁盘满 → 进程退出 → 界面复位 + 日志里写明是磁盘满。"""
    config, _bag = point_env
    harness = _RecordHarness(config)

    harness.recorder.health_result = {"alive": True, "producing": False, "message": "没落盘"}
    harness.update_record_time()
    harness.recorder.disk_full_result = "录制盘已满（bag 目录所在分区可用空间为 0）"
    harness.recorder.health_result = {"alive": False, "producing": False,
                                      "message": "录制进程已退出"}
    harness.update_record_time()

    assert harness.record_status_label.text() == "录制 已中断"
    assert any("录制盘已满" in m for m in harness.messages)


def test_entry_reopens_the_same_panel_with_count_intact(qapp, point_env, monkeypatch):
    """收起面板后，操作区的「打点」能再打开它；已打的点与按钮计数都在。"""
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)
    harness.on_record()
    panel = harness.point_panel

    _pick_label(panel, "开始任务")
    assert harness.btn_point.text() == "打点 (1)"            # 计数通过信号同步到按钮

    panel.close()
    assert not panel.isVisible()
    harness.on_point()

    assert harness.point_panel is panel                     # 同一个面板，记录还在
    assert panel.isVisible()
    assert panel.recorder.count == 1
    panel.close()


def test_stale_panel_is_replaced_when_the_session_changes(qapp, point_env, monkeypatch):
    """换了一场录制却还挂着旧面板：重开一个，绝不能往已经收尾的会话里写。"""
    _patch_record_dialog(monkeypatch)
    config, _bag = point_env
    harness = _RecordHarness(config)
    harness.on_record()
    stale = harness.point_panel

    harness.point_recorder = PointRecorder(config.get_bag_dir(), "另一场",
                                           started_at=time.time(), task="布草挂架")
    harness.on_point()

    assert harness.point_panel is not stale
    assert harness.point_panel.recorder is harness.point_recorder
    harness.point_panel.close()


# ============ 流程合并规则（纯函数，把"通用夹两端"钉死） ============
def _item(name, action_id=None, task=""):
    return {"name": name, "action_id": action_id, "task": task}


def test_merge_puts_common_first_and_last_around_task_labels():
    """通用首项排最前、其余排最后 —— 现场看到的是一条 1..N 的完整顺序。"""
    from data_collector.core import merge_point_flow

    items = [
        _item("开始任务", 1, "*"),
        _item("双爪夹住第一个角", 1, "布草挂架"),
        _item("第一个角上夹具", 2, "布草挂架"),
        _item("结束任务", 2, "*"),
    ]

    flow = merge_point_flow(items, "*")

    assert [i["name"] for i in flow] == [
        "开始任务", "双爪夹住第一个角", "第一个角上夹具", "结束任务"]


def test_merge_sorts_each_block_by_group_number():
    """组内按编号排 —— 管理界面里怎么编号，面板里就什么顺序。"""
    from data_collector.core import merge_point_flow

    items = [
        _item("结束任务", 2, "*"),
        _item("开始任务", 1, "*"),
        _item("第二个角上夹具", 3, "布草挂架"),
        _item("双爪夹住第一个角", 1, "布草挂架"),
    ]

    flow = merge_point_flow(items, "*")

    assert [i["name"] for i in flow] == [
        "开始任务", "双爪夹住第一个角", "第二个角上夹具", "结束任务"]


def test_merge_puts_unnumbered_labels_last_within_their_block():
    """没编号的排在它那一块的末尾（新加的标签就是这样），不会插到中间去。"""
    from data_collector.core import merge_point_flow

    items = [
        _item("第一个角上夹具", 2, "布草挂架"),
        _item("临时标签", None, "布草挂架"),
        _item("双爪夹住第一个角", 1, "布草挂架"),
        _item("开始任务", 1, "*"),
        _item("结束任务", 2, "*"),
    ]

    flow = merge_point_flow(items, "*")

    assert [i["name"] for i in flow] == [
        "开始任务", "双爪夹住第一个角", "第一个角上夹具", "临时标签", "结束任务"]


def test_merge_without_common_labels_is_just_the_task_block():
    from data_collector.core import merge_point_flow

    flow = merge_point_flow([_item("只有一个", 1, "布草挂架")], "*")
    assert [i["name"] for i in flow] == ["只有一个"]


def test_merge_with_single_common_label_puts_it_first():
    """通用组只剩一项时放最前（它多半是"开始任务"），不会掉到队尾。"""
    from data_collector.core import merge_point_flow

    items = [_item("开始任务", 1, "*"), _item("中间动作", 1, "布草挂架")]
    assert [i["name"] for i in merge_point_flow(items, "*")] == ["开始任务", "中间动作"]


def test_point_label_number_is_the_position_in_the_flow(point_env):
    """序号 = 流程里的位置；不在流程里的名字返回 None。"""
    config, _bag = point_env

    assert config.get_point_label_number("布草挂架", "开始任务") == 1
    assert config.get_point_label_number("布草挂架", "双爪夹住第一个角") == 2
    assert config.get_point_label_number("布草挂架", "第一个角上夹具") == 3
    assert config.get_point_label_number("布草挂架", "结束任务") == 4
    assert config.get_point_label_number("布草挂架", "并不存在的标签") is None
    # 换个任务（没有自己的标签）就只有通用两项
    assert config.get_point_label_number("矿泉水瓶分拣", "结束任务") == 2


# ============ 打点面板的版面（v4.14 重做） ============
# 这一版把版面按使用频率重排：标签是"点一下就打点"的高频动作，所以要做得像可按的东西
# （大卡片网格），而不是一块只读的列表。下面几条钉住其中会静默退化的部分。
def test_label_cards_form_a_two_column_grid(qapp, point_env):
    """标签按两列卡片铺开 —— 不是一条要滚动的长列表，也不在格子中间缩着。

    钉的是实测踩过的两个坑，两者都会把"明明够两列"变成一列：
    ① `gridSize` 里带了卡片缝；② 算列数时没预留竖滚动条的宽度。
    现象都是"标签竖着排一列、右边空一大块、还要滚动"，从现象几乎推不回原因。
    """
    from data_collector import theme as _theme
    from data_collector.ui.point_dialog import PointDialog

    _theme.apply_theme(qapp, "light")
    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag, task="布草挂架")
    dialog = PointDialog(config, recorder)
    dialog.show()
    qapp.processEvents()

    view = dialog.label_list
    rects = [view.visualRect(view.model().index(i, 0)) for i in range(view.count())]
    assert len(rects) == 4
    assert rects[1].top() == rects[0].top()             # 前两张并排
    assert rects[1].left() > rects[0].left()
    assert rects[2].top() > rects[0].top()              # 第三张换行
    assert rects[2].left() == rects[0].left()
    # 卡片要填满格子：小于格子时 Qt 会把它居中，每边留出等宽空隙，整片网格看着很散
    assert rects[0].width() == view.gridSize().width()
    # 两行就放得下 → 没有可滚动的内容（滚动条本身显隐是 Qt 的窗口状态，
    # 会受前一个用例留下的窗口影响，这里钉"内容放得下"这个事实即可）
    assert view.verticalScrollBar().maximum() == 0
    dialog.close()


def test_label_grid_height_follows_row_count(qapp, point_env):
    """网格高度按行数长：切半张卡会被当成"界面坏了"，而这里本来就要一眼看全。"""
    from data_collector import theme as _theme
    from data_collector.ui.point_dialog import (CARD_H, PointDialog,
                                               _LabelCardDelegate)

    _theme.apply_theme(qapp, "light")
    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag, task="布草挂架")
    dialog = PointDialog(config, recorder)
    dialog.show()
    qapp.processEvents()

    step = CARD_H + _LabelCardDelegate.GAP                # 一行的步长 = 卡片高 + 缝
    # 4 个标签 / 两列 = 两行；末尾那 4px 是刻意多给的余量（见 _fit_label_list_height）
    assert dialog.label_list.height() == 2 * step + 4

    # 标签多到超过 3 行就封顶（面板高度不能被标签数量牵着走），多出来的改为滚动
    for index in range(6):
        config.point_labels.upsert(f"多出来的{index}", task="布草挂架")
    dialog.reload_labels()
    qapp.processEvents()
    assert dialog.label_list.height() == 3 * step + 4
    assert dialog.label_list.verticalScrollBar().maximum() > 0
    dialog.close()


def test_records_area_shows_a_hint_instead_of_an_empty_box(point_env):
    """一个点都没打时用一句提示替掉空列表 —— 空白列表占着最显眼的位置却不说明任何事。"""
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    assert dialog.records_list.isHidden() is True
    assert dialog.records_empty.isHidden() is False

    recorder.add("开始任务", 1, epoch=record_dir.stat().st_ctime + 2)
    dialog._refresh_records()

    assert dialog.records_list.isHidden() is False
    assert dialog.records_empty.isHidden() is True
    assert "1" in dialog.records_title.text()


def test_write_badge_reports_whether_points_landed(point_env):
    """顶部徽章直接回答"写进文件没有"：已写入（绿）/ 待写入（黄）。

    这一条原来只在底部那一行长句子里说，和顶部的"已打点 N 条"重复；现在合成一处，
    而且用状态色，扫一眼就知道 —— 现场不会去读完整句话。
    """
    import time

    from data_collector.core_points import PointRecorder
    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env

    linked, _dir = _make_recorder(bag)
    ok_dialog = PointDialog(config, linked)
    assert ok_dialog.where_badge.text() == "已写入 0 条"
    assert ok_dialog.where_badge.property("state") == "ok"

    # 名字对不上 → 定位不到本次录制的目录（记录进内存、等目录出现再补写）
    stray = PointRecorder(bag, "对不上的名字", started_at=time.time() - 1)
    stray.add("结束任务", None, epoch=time.time())
    bad_dialog = PointDialog(config, stray)
    assert bad_dialog.where_badge.text() == "待写入 1 条"
    assert bad_dialog.where_badge.property("state") == "warn"
    assert "还没找到录制目录" in bad_dialog.where_label.text()


def test_label_card_delegate_holds_no_reference_back(point_env):
    """卡片 delegate 不许反向持有面板 / 视图的 Python 引用。

    实测踩过的段错误：delegate 拿着宿主窗口去问主题（`parent.theme_mode`），于是形成
    Python 引用环（窗口 → 视图 → delegate → 窗口）。环被回收时 pyqt 里 C++ 对象的销毁
    顺序不定，**之后任意一次 `setStyleSheet` 都会段错误** —— 崩在主题层，离病因很远。

    现在主题走 `theme.active_mode()`、尺寸走 item 自己的 sizeHint，delegate 谁也不认识。
    """
    from PyQt5.QtCore import QObject

    from data_collector.ui.point_dialog import PointDialog

    config, bag = point_env
    recorder, _record_dir = _make_recorder(bag)
    dialog = PointDialog(config, recorder)

    delegate = dialog.label_list.itemDelegate()
    held = {name: value for name, value in vars(delegate).items()
            if isinstance(value, QObject)}
    assert held == {}, f"delegate 上还挂着 QObject 引用: {sorted(held)}"
