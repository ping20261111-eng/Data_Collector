"""录制落盘位置（本机数据盘 / 移动硬盘）—— v6.4.17。

## 为什么要单独一份

这一版把"这次录到哪"从一个**只能改配置文件**的选项，变成录制界面里的一次选择，
并且默认跟着"有没有插数据盘"走。它的风险面很集中：

1. **静默写错位置**：录制脚本若把 `BAG_DIR` 写死，上位机导出的 `BAG_DIR` 会被无声
   吃掉 —— 数据落回 `/var/nerv/bag`，而界面、清单、打点、可用空间预检全都按"写在
   盘上"理解。现场按界面提示拔盘交付时，盘上什么都没有。这类失败**没有任何报错**，
   所以必须在这一层钉住。
2. **选择会黏住**：选了硬盘之后再改回本机，若覆盖没清干净，取值会停在盘上 ——
   拔盘起录就失败。
3. **默认值不能被配置文件拖住**：选择刻意**不写回 app_config**（盘拔了配置还指着它
   就会起录失败），所以"默认值按检测走"这条要独立验。

## 沙箱限制

没有真移动硬盘，也没有真挂载点 —— 所以 `DiskExportTarget.is_mounted` 用打桩
（它自身在 `test_disk_export.py` 里已单测）。判定用的是与导出共用的同一个类，
不是另写一套。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 这一份里有一半是用例要开真对话框（「保存记录位置」那一栏的行为只能这样验），
# 所以和 test_ui_wiring / test_points 一样走离屏平台 + 模块级 QApplication。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from data_collector import disk_export as de
from data_collector.core import ConfigManager, is_within
from data_collector.core_data import DataUploader
from data_collector.core_operations import Launcher
from data_collector.errors import AppError

BASE = "workspace_root: /tmp\nteleop_config_path: a.yaml\narm_control_config_path: b.yaml\n"
MARKER = ".nerv_volume"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def mounted_disk(monkeypatch):
    """把 `is_mounted` 放行 —— 沙箱里造不出真挂载点。

    `has_volume_marker()` 不打桩（它是普通的"文件在不在"），这样"插了但不是数据盘"
    这条分支仍然走真实实现。
    """
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: True)


@pytest.fixture
def unmounted_disk(monkeypatch):
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: False)


def _mgr(tmp_path, mount=None, marker=MARKER, require_marker=True,
         bag_dir="/var/nerv/bag"):
    """造一份最小配置；`mount=None` 表示不配移动硬盘段。"""
    extra = f"bag_dir: {bag_dir}\n"
    if mount is not None:
        extra += (f"disk_export:\n  mount_point: \"{mount}\"\n"
                  f"  volume_id_file: {marker}\n"
                  f"  require_volume_id: {require_marker}\n"
                  f"  min_free_margin_mb: 1\n")
    path = tmp_path / "app_config.yaml"
    path.write_text(BASE + extra, encoding="utf-8")
    return ConfigManager(path)


def _usb(tmp_path, with_marker=True):
    usb = tmp_path / "usb"
    usb.mkdir(exist_ok=True)
    if with_marker:
        (usb / MARKER).write_text("ok", encoding="utf-8")
    return usb


# ---------- 候选落点的解析 ----------

def test_disk_target_points_into_the_disk_with_serial_and_date(tmp_path, mounted_disk):
    """盘可用时，落点是 `<挂载点>/<序列号>/<采集日期>` —— 与导出到硬盘同一套分层。"""
    usb = _usb(tmp_path)
    target = de.disk_record_target(_mgr(tmp_path, mount=usb))
    assert target.available, target.reason
    assert target.path.is_relative_to(usb)
    span = str(target.path.relative_to(usb)).split("/")
    assert len(span) == 2, f"应当是「序列号/日期」两层，实际 {span}"
    assert span[1] == de.capture_date(None), "日期应当是今天（本次采集还没发生）"


def test_missing_mount_point_is_reported_verbatim(tmp_path):
    """没配挂载点 → 说明指向配置项本身，不是含糊的"不可用"。"""
    target = de.disk_record_target(_mgr(tmp_path, mount=None))
    assert not target.available
    assert "disk_export.mount_point" in target.reason


def test_absent_disk_reports_not_detected(tmp_path):
    """挂载点目录不存在（没插盘）→ 说"未检测到"，而不是"路径错误"。"""
    target = de.disk_record_target(_mgr(tmp_path, mount=tmp_path / "nope"))
    assert not target.available
    assert "未检测到移动硬盘" in target.reason


def test_plain_directory_is_not_treated_as_a_disk(tmp_path, unmounted_disk):
    """路径存在但不是挂载点 → 必须拦住。

    这是最容易骗过人的一种：目录在、能写，但盘根本没挂载，数据会落在本机磁盘上。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / MARKER).write_text("ok", encoding="utf-8")
    target = de.disk_record_target(_mgr(tmp_path, mount=plain))
    assert not target.available
    assert "不是已挂载的设备目录" in target.reason


def test_wrong_disk_is_rejected_by_the_marker(tmp_path, mounted_disk):
    """插了但不是数据盘（缺标识）→ 拦住并点出标识文件 —— 与导出同一条规则。"""
    usb = _usb(tmp_path, with_marker=False)
    target = de.disk_record_target(_mgr(tmp_path, mount=usb))
    assert not target.available
    assert "不是数据盘" in target.reason and MARKER in target.reason


@pytest.mark.parametrize("case", ["ok", "no_mount", "no_disk", "no_marker"])
def test_default_is_the_disk_only_when_it_is_really_usable(tmp_path, monkeypatch, case):
    """默认值：**插了可用数据盘就用它，否则本机** —— 四种情形一次钉住。"""
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: True)
    if case == "ok":
        mgr = _mgr(tmp_path, mount=_usb(tmp_path))
    elif case == "no_mount":
        mgr = _mgr(tmp_path, mount=None)
    elif case == "no_disk":
        mgr = _mgr(tmp_path, mount=tmp_path / "nope")
    else:
        mgr = _mgr(tmp_path, mount=_usb(tmp_path, with_marker=False))
    expect = de.DISK_TARGET if case == "ok" else de.LOCAL_TARGET
    assert de.default_record_target_key(mgr) == expect


# ---------- 选择怎么影响 BAG_DIR ----------

def test_choosing_the_disk_moves_bag_dir_and_the_script_env(tmp_path, mounted_disk):
    """选硬盘 → `get_bag_dir()` 与脚本环境变量 `BAG_DIR` **同时**跟着走。

    这两者必须一致：前者决定界面/清单/打点看哪个目录，后者才是脚本真正写的地方；
    只动一个就会出现"界面显示在盘上、数据在本机"。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    target = de.disk_record_target(mgr)
    mgr.set_record_target(de.DISK_TARGET, target.path)

    assert mgr.get_bag_dir() == target.path
    assert mgr.get_script_env()["BAG_DIR"] == str(target.path)
    assert mgr.record_target_key() == de.DISK_TARGET


def test_choosing_local_again_does_not_stick_to_the_disk(tmp_path, mounted_disk):
    """改回本机 → 取值回到配置里的 `bag_dir`，**不能被上一次的选择黏住**。

    黏住的后果很具体：盘已经拔了，界面还按盘上的目录给脚本传 `BAG_DIR`，
    起录直接失败。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb, bag_dir=str(tmp_path / "localbags"))
    mgr.set_record_target(de.DISK_TARGET, de.disk_record_target(mgr).path)
    assert mgr.get_bag_dir() != mgr.get_configured_bag_dir()

    mgr.set_record_target(de.LOCAL_TARGET)
    assert mgr.get_bag_dir() == mgr.get_configured_bag_dir()
    assert mgr.get_script_env()["BAG_DIR"] == str(tmp_path / "localbags")
    assert mgr.record_target_key() == de.LOCAL_TARGET


def test_the_choice_is_never_written_into_the_config_file(tmp_path, mounted_disk):
    """选择只放内存 —— 配置文件必须一字不改。

    写回去的话，"盘拔了配置还指着它"就会让下一次起录失败，而失败点在最不该出问题的
    地方（连界面都还没起来就该能录）。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    before = mgr.config_path.read_text(encoding="utf-8")
    mgr.set_record_target(de.DISK_TARGET, de.disk_record_target(mgr).path)
    assert mgr.config_path.read_text(encoding="utf-8") == before


# ---------- 录制界面 ----------

@pytest.fixture
def dialog_factory(qapp):
    from data_collector.ui import RecordDialog

    def make(config):
        return RecordDialog(None, config=config)

    return make


def _items(dialog):
    combo = dialog.target_combo
    return [(combo.itemText(i), combo.model().item(i).isEnabled())
            for i in range(combo.count())]


def test_dialog_lists_both_targets_and_defaults_to_the_disk(tmp_path, mounted_disk,
                                                            dialog_factory):
    """两个候选**始终都在**；插了盘默认选硬盘，并把落点摆出来。"""
    usb = _usb(tmp_path)
    dialog = dialog_factory(_mgr(tmp_path, mount=usb))
    texts = [t for t, _ in _items(dialog)]
    assert len(texts) == 2, texts
    assert all(enabled for _, enabled in _items(dialog)), "盘可用时两项都该能选"
    settings = dialog.get_settings()
    assert settings["record_target"] == de.DISK_TARGET
    assert Path(settings["record_dir"]).is_relative_to(usb)
    assert "本次将写入" in dialog.target_hint.text()


def test_dialog_disables_the_disk_with_the_reason(tmp_path, mounted_disk,
                                                  dialog_factory):
    """盘不可用时：那一项**列出来但不可选**，且文字里写明原因。

    不隐藏的理由：隐藏会让人以为程序不支持；不给原因的置灰会让人反复点它。
    """
    usb = _usb(tmp_path, with_marker=False)
    dialog = dialog_factory(_mgr(tmp_path, mount=usb))
    items = dict((t, e) for t, e in _items(dialog))
    disk_text = [t for t in items if t.startswith("移动硬盘")][0]
    assert items[disk_text] is False, "不可用的盘不该可点"
    assert "不可用" in disk_text
    # 原因放在下面那行提示里（下拉里只放短标签 —— 否则 QComboBox 会按最长项把对话框撑开）
    hint = dialog.target_hint.text()
    assert "不可用" in hint and MARKER in hint, f"提示行没写明原因：{hint!r}"
    assert dialog.get_settings()["record_target"] == de.LOCAL_TARGET


def test_target_options_stay_short_so_the_dialog_width_is_stable(tmp_path, mounted_disk,
                                                                dialog_factory):
    """下拉项文字必须**短**：路径与原因都放提示行。

    实测教训：把落点路径拼进选项文字时，`QComboBox` 会按最长项撑开，
    本对话框在"插盘 / 拔盘"两态之间宽度从 560 跳到 816；同一现场换一块挂载点路径
    长短不同的盘，界面宽度还会再变。所以这里把"项文字里不许出现路径分隔符"钉住。
    """
    usb = _usb(tmp_path)
    dialog = dialog_factory(_mgr(tmp_path, mount=usb))
    for text, _enabled in _items(dialog):
        assert "/" not in text, f"下拉项里出现了路径：{text!r}"
        assert len(text) <= 12, f"下拉项文字过长：{text!r}"
    # 路径与说明都在提示行
    assert str(usb) in dialog.target_hint.text()


def test_dialog_falls_back_when_the_remembered_disk_gone(tmp_path, monkeypatch,
                                                        dialog_factory):
    """上次选了硬盘、这次盘拔了 → **回落到本机**，不能把不可用的项留在选中态。

    留在选中态的后果是：填完名字点「开始录制」才发现写不进去。
    """
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: True)
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    mgr.set_record_target(de.DISK_TARGET, de.disk_record_target(mgr).path)
    (usb / MARKER).unlink()                      # 盘拔了

    dialog = dialog_factory(mgr)
    assert dialog.get_settings()["record_target"] == de.LOCAL_TARGET


def test_dialog_remembers_the_users_own_choice(tmp_path, mounted_disk, dialog_factory):
    """用户明确选过「本机」→ 下次打开仍是本机（盘插着也不抢回默认）。"""
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    mgr.set_record_target(de.LOCAL_TARGET)
    dialog = dialog_factory(mgr)
    settings = dialog.get_settings()
    assert settings["record_target"] == de.LOCAL_TARGET
    assert settings["record_dir"] is None, "本机不需要传具体目录，None 才不会被黏住"


def test_dialog_is_wide_enough_for_the_landing_hint(tmp_path, mounted_disk, dialog_factory):
    """对话框要够宽，让「本次将写入：<路径>/」那行能换行显示完整。

    实测踩过：按内容自适应时只有 269px 宽，提示被裁掉、还和下一行的
    「名称（name）」标签叠在一起 —— 这两件事都是从截图里看出来的，
    代码里没有任何东西会报错。
    """
    dialog = dialog_factory(_mgr(tmp_path, mount=_usb(tmp_path)))
    assert dialog.minimumWidth() >= 400, (
        f"最小宽度只有 {dialog.minimumWidth()}px：落点提示会被裁掉并压到下一行")


def test_dialog_without_config_still_opens(tmp_path, dialog_factory):
    """`config=None`（旧用法/替身）也要能开 —— 不能因为多了这一栏就崩。"""
    dialog = dialog_factory(None)
    assert dialog.target_combo.count() == 2
    assert dialog.get_settings()["record_target"] == de.LOCAL_TARGET


# ---------- 起录时怎么落地 ----------

class _Dummy:
    """只借 `_apply_record_target` 需要的那几样（config / append / show_error）。

    不构造整个 `MainWindow`：这个方法本来就不该依赖窗口的其它部分 ——
    它只决定"落盘位置定成哪个、目录建不建得出来"。
    """

    def __init__(self, config):
        self.config = config
        self.logs = []
        self.errors = []

    def append(self, message):
        self.logs.append(message)

    def show_error(self, title, message):
        self.errors.append((title, message))


def _apply(config, settings):
    from data_collector.ui.panels.data_ops import DataOpsMixin

    dummy = _Dummy(config)
    ok = DataOpsMixin._apply_record_target(dummy, settings)
    return ok, dummy


def test_starting_on_the_disk_applies_it_and_creates_the_folder(tmp_path, mounted_disk):
    """选硬盘起录：落点目录被建出来、选择被记住、日志里说清写到哪。"""
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    target = de.disk_record_target(mgr)
    ok, dummy = _apply(mgr, {"record_target": de.DISK_TARGET, "record_dir": target.path})

    assert ok, dummy.errors
    assert target.path.is_dir(), "起录前必须把落点目录建出来"
    assert mgr.get_bag_dir() == target.path
    assert any("移动硬盘" in line for line in dummy.logs)


def test_starting_on_an_unavailable_disk_is_refused(tmp_path, mounted_disk):
    """盘不可用时**拒绝起录**并说清原因，而不是起录后才发现写不进去。

    起录后才发现的表现是"录制进程启动后立即退出" —— 真实原因（盘没挂载/不是数据盘）
    在机器人自己的日志里，现场不会去查。
    """
    usb = _usb(tmp_path, with_marker=False)
    mgr = _mgr(tmp_path, mount=usb)
    ok, dummy = _apply(mgr, {"record_target": de.DISK_TARGET,
                             "record_dir": usb / "whatever"})
    assert not ok
    title, message = dummy.errors[-1]
    assert "移动硬盘不可用" in title
    assert "本机数据盘" in message, "要给出可照做的下一步"
    assert mgr.record_target_key() is None, "被拒绝时不该留下选择"


def test_starting_on_local_clears_a_previous_disk_choice(tmp_path, mounted_disk):
    """先选过硬盘、这次选本机 → 覆盖被清掉（脚本环境回到 /var/nerv/bag 那一档）。"""
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb, bag_dir=str(tmp_path / "localbags"))
    mgr.set_record_target(de.DISK_TARGET, de.disk_record_target(mgr).path)

    ok, dummy = _apply(mgr, {"record_target": de.LOCAL_TARGET})
    assert ok, dummy.errors
    assert mgr.get_script_env()["BAG_DIR"] == str(tmp_path / "localbags")


# ---------- 脚本契约闸门（防"静默写错位置"） ----------

def _run_contract_check(tmp_path, text):
    class _Borrow:
        _BAG_DIR_OVERRIDABLE = Launcher._BAG_DIR_OVERRIDABLE
        _require_bag_dir_override = Launcher._require_bag_dir_override

    script = tmp_path / "record_x.sh"
    script.write_text(text, encoding="utf-8")
    _Borrow()._require_bag_dir_override(script)          # 不抛 = 放行


@pytest.mark.parametrize("text", [
    'NERV_DIR="/var/nerv"\nBAG_DIR="${BAG_DIR:-$NERV_DIR/bag}"\n',   # 机器侧改后
    '    BAG_DIR="${BAG_DIR:-$NERV_DIR/bag}"\n',                     # 带前导空格
])
def test_overridable_bag_dir_is_accepted(tmp_path, text):
    _run_contract_check(tmp_path, text)


@pytest.mark.parametrize("text", [
    'NERV_DIR="/var/nerv"\nBAG_DIR="$NERV_DIR/bag"\n',               # 硬编码（改前）
    'BAG_DIR="/media/nerv/fixed"\n',                                 # 写死成另一个绝对路径
    '# BAG_DIR="${BAG_DIR:-$NERV_DIR/bag}"\nBAG_DIR="$NERV_DIR/bag"\n',  # 只在注释里
])
def test_script_that_ignores_the_env_is_refused(tmp_path, text):
    """脚本不认 `BAG_DIR` 覆盖 → **拒绝起录**。

    这是本功能里唯一"错了也没有报错"的失败形态：环境变量被无声吃掉，数据落到
    另一个目录，而界面/清单/打点全按"写在盘上"理解。
    """
    with pytest.raises(AppError) as excinfo:
        _run_contract_check(tmp_path, text)
    assert "不认 BAG_DIR 覆盖" in str(excinfo.value.message)


# ---------- 与「导出目标」的关系 ----------

def test_recording_straight_onto_the_disk_is_not_copied_again(tmp_path, mounted_disk):
    """录制已直接写在盘上 → 不再"导出到移动硬盘"（那会把采集拷进它自己）。

    白拷一份的代价是实打实的：一倍空间 + 一次全量拷贝时间，而且盘上会多出
    `…/<bag>/<bag>.zip` 这种嵌套。判据是**目标目录落在源目录里面**：录制直接写盘时
    两者就是同一个目录。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    uploader = DataUploader(mgr)
    # 落盘位置选盘之后，录制的 bag 就长在导出落点上（同一个目录）
    span = de.disk_subpath(uploader._serial_number(), None)
    bag = usb / span / "vla_20261014103000"
    bag.mkdir(parents=True)
    (bag / "part.mcap").write_bytes(b"x" * 2048)
    assert de.capture_date(bag) == de.capture_date(None), "前提：刚落盘的目录 mtime 是今天"

    logs = []
    assert uploader._export_to_disk(bag, progress_cb=logs.append) is True
    assert not list(bag.rglob("*.zip")), "不该再拷一份归档进它自己"
    assert any("跳过重复拷贝" in line for line in logs)


def test_a_local_bag_is_still_exported_to_the_disk(tmp_path, mounted_disk):
    """回归：源在本机时**照常**导出 —— 别把常规路径一起跳过了。"""
    usb = _usb(tmp_path)
    local = tmp_path / "localbags" / "vla_20261014110000"
    local.mkdir(parents=True)
    (local / "part.mcap").write_bytes(b"x" * 4096)

    mgr = _mgr(tmp_path, mount=usb, bag_dir=str(tmp_path / "localbags"))
    uploader = DataUploader(mgr)
    assert uploader._export_to_disk(local) is True
    assert list(usb.rglob("vla_20261014110000.zip")), "常规路径必须仍然导出"


def test_the_skip_needs_a_real_mount_not_just_a_matching_path(tmp_path, unmounted_disk):
    """路径撞名（盘没插）**不能**算"已在盘上"：跳过导出要以挂载点确实是挂载点为前提。

    否则"挂载点配成了本机某个普通目录"会让导出被静默跳过，而用户以为自己已经
    导出到盘上了 —— 这类"什么都没做却说做完了"比报错严重。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    uploader = DataUploader(mgr)
    # 让"目标落在源里面"这个路径关系**真的成立**（即 bag 就长在导出落点上）
    span = de.disk_subpath(uploader._serial_number(), None)
    bag = usb / span / "vla_x"
    bag.mkdir(parents=True)
    (bag / "a.mcap").write_bytes(b"x" * 2048)

    assert is_within(usb / span / bag.name, bag), "前提：路径层面目标确实落在源里面"

    # 但盘没挂载 → 不许跳过，要走正常的挂载点校验（那里会报"不是已挂载的设备目录"）
    assert uploader._export_to_disk(bag) is False
    assert "挂载" in uploader.last_error


# ---------- 口径单一来源 ----------

def test_serial_and_date_helpers_have_a_single_source(tmp_path, mounted_disk):
    """`DataUploader` 的落点助手必须**委托**给 `disk_export` 那一份。

    两处各写一份的话，"录制直接写盘"与"录完导出"迟早给出不同落点 ——
    而界面按其中一份说话，人按界面去盘上找，找不到。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    uploader = DataUploader(mgr)
    assert DataUploader.UNKNOWN_SERIAL_DIR == de.UNKNOWN_SERIAL_DIR
    assert uploader._safe_dir_name("../../etc") == de.safe_dir_name("../../etc")
    assert uploader._disk_subpath() == de.disk_subpath(uploader._serial_number(), None)
    # 路径归属这个原语只有 core 一份（data_view 早就在用），别在别处再写一个
    import data_collector.core as core_mod
    assert is_within is core_mod.is_within
