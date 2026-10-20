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
from typing import List

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


def _fake_volume(path, **overrides) -> de.MountedVolume:
    """把一个目录伪装成"枚举到的外接盘"。"""
    fields = dict(path=Path(path), label=Path(path).name, fs="ext4",
                  total_bytes=512 * 1024 ** 3, free_bytes=500 * 1024 ** 3)
    fields.update(overrides)
    return de.MountedVolume(**fields)


#: 本次用例里"插着"的盘。`_usb()` 建目录时登记、`mounted_disk` 夹具桩枚举时读取。
#: 用清单而不是参数传递，是为了**不改动 17 个用例的签名** —— 它们本来就只写
#: `_usb(tmp_path)` 就期待"这块盘插着"，把"插盘"这件事变成必须显式表达的参数
#: 只会让每条用例多一行与被测行为无关的样板。
PLUGGED: List[Path] = []


@pytest.fixture
def mounted_disk(monkeypatch):
    """把 `_usb()` 建出来的目录当成"插着的移动硬盘"（沙箱里造不出真挂载点）。

    ## v6.5.1 为什么桩的是 `list_removable_mounts` 而不是 `is_mounted`

    候选落点改成从**枚举结果**里挑盘之后，`DiskExportTarget.is_mounted` 那一层
    压根不会被问到 —— 只桩它的话"插着盘"这个前提根本不成立，一大批用例会一起红，
    而且红得看不出真实原因。桩枚举入口之后：
    ① "插盘 / 拔盘"能被精确控制；
    ② 不再依赖**跑测试的那台机器碰巧插没插盘**（这条很重要：不桩枚举的话，
       开发机上插着移动硬盘会让"没插盘"的用例反向失效）。

    `is_mounted` 那一层仍然要桩：导出侧的 `check_volume` 查的是 `DiskExportTarget`
    （不查枚举），沙箱里造不出真挂载点。
    """
    PLUGGED.clear()
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: True)
    monkeypatch.setattr(de, "list_removable_mounts",
                        lambda configured="": [_fake_volume(p) for p in PLUGGED])
    yield
    PLUGGED.clear()


@pytest.fixture
def unmounted_disk(monkeypatch):
    """枚举得到盘，但 `is_mounted` 判假 —— 目录在、盘没挂（路径撞名的陷阱）。"""
    PLUGGED.clear()
    monkeypatch.setattr(de.DiskExportTarget, "is_mounted", lambda self: False)
    monkeypatch.setattr(de, "list_removable_mounts",
                        lambda configured="": [_fake_volume(p) for p in PLUGGED])
    yield
    PLUGGED.clear()


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
    """造一个"插着的移动硬盘"目录（并登记到 `PLUGGED`，供枚举桩读取）。"""
    usb = tmp_path / "usb"
    usb.mkdir(exist_ok=True)
    if with_marker:
        (usb / MARKER).write_text("ok", encoding="utf-8")
    if usb not in PLUGGED:
        PLUGGED.append(usb)
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


def test_missing_mount_point_is_reported_verbatim(tmp_path, mounted_disk):
    """没配挂载点、也没插盘 → 说清"插上即自动识别"，**不再要求人去填路径**。

    v6.5.1 起「没配 `mount_point`」是**正常配置**（推荐做法），所以这条断言的方向
    变了：旧文案是"请在 app_config.yaml 的 mount_point 填入实际挂载点"—— 那等于
    把问题绕回原点（挂载点由系统按卷标生成，换盘就变，抄不准是必然的）。
    """
    # 用 `disk_record_targets`（列全部）而不是 `disk_record_target`（只给"第一个可用"）：
    # 后者没有可用盘时返回 None，**拿不到那句"没插盘"的话术**，而话术才是这条要验的。
    targets = de.disk_record_targets(_mgr(tmp_path, mount=None))
    assert len(targets) == 1
    assert not targets[0].available
    assert "未检测到移动硬盘" in targets[0].reason
    assert "自动识别" in targets[0].reason


def test_absent_disk_reports_not_detected(tmp_path, mounted_disk):
    """配置里那块盘现在没插着（枚举不到）→ 说"未检测到"，而不是"路径错误"。

    **注意前提**：v6.5.1 之后判定看的是**枚举结果**，不是"配置里那个目录在不在"。
    所以"配置写着某路径"不再意味着"这块盘会被用上" —— 枚举不到就是没插盘。
    """
    targets = de.disk_record_targets(_mgr(tmp_path, mount=tmp_path / "nope"))
    assert len(targets) == 1 and not targets[0].available
    assert "未检测到移动硬盘" in targets[0].reason


def test_plain_directory_is_not_treated_as_a_disk(tmp_path, unmounted_disk):
    """枚举得到它（目录在），但 `is_mounted` 判假 → 仍然必须拦住。

    这是最容易骗过人的一种：目录在、能写、能通过枚举，但盘根本没挂载 ——
    数据会落在本机磁盘上，而界面显示"移动硬盘"。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / MARKER).write_text("ok", encoding="utf-8")
    PLUGGED.append(plain)
    # 枚举已经放行了；拦住它的是"必须是真挂载点"这一层。
    # 断言"无可用盘"而不是去匹配具体措辞：措辞会随文案调整变，但"必须拦住"不会。
    assert de.disk_record_target(_mgr(tmp_path, mount=plain)) is None, (
        "目录在但盘没挂载时绝不能当成可用移动硬盘（数据会落在本机盘上）")
    targets = de.disk_record_targets(_mgr(tmp_path, mount=plain))
    assert len(targets) == 1 and not targets[0].available


def test_wrong_disk_is_rejected_by_the_marker(tmp_path, mounted_disk):
    """插了但不是数据盘（缺标识）→ 拦住并点出标识文件 —— 与导出同一条规则。"""
    usb = _usb(tmp_path, with_marker=False)
    assert de.disk_record_target(_mgr(tmp_path, mount=usb)) is None, (
        "缺标识文件的盘不该被当成可用数据盘")
    targets = de.disk_record_targets(_mgr(tmp_path, mount=usb))
    assert len(targets) == 1 and not targets[0].available
    assert "不是数据盘" in targets[0].reason and MARKER in targets[0].reason


@pytest.mark.parametrize("case", ["ok", "no_mount", "no_disk", "no_marker"])
def test_default_is_the_disk_only_when_it_is_really_usable(tmp_path, mounted_disk, case):
    """默认值：**插了可用数据盘就用它，否则本机** —— 四种情形一次钉住。

    键在 v6.5.1 变成 `disk:<挂载点>`（要区分多块盘），所以"期望值"要跟着算出来，
    不能硬写 `de.DISK_TARGET` —— 那样写会把"键格式变了"这件事藏起来。
    """
    if case == "ok":
        usb = _usb(tmp_path)
        mgr = _mgr(tmp_path, mount=usb)
        expect = de.disk_target_key(usb)
    elif case == "no_mount":
        mgr = _mgr(tmp_path, mount=None)
        expect = de.LOCAL_TARGET
    elif case == "no_disk":
        mgr = _mgr(tmp_path, mount=tmp_path / "nope")
        expect = de.LOCAL_TARGET
    else:
        mgr = _mgr(tmp_path, mount=_usb(tmp_path, with_marker=False))
        expect = de.LOCAL_TARGET
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
    mgr.set_record_target(target.key, target.path)

    assert mgr.get_bag_dir() == target.path
    assert mgr.get_script_env()["BAG_DIR"] == str(target.path)
    assert mgr.record_target_key() == target.key
    assert target.key.startswith(f"{de.DISK_TARGET}:"), "外接盘键必须带挂载点（多盘时要能区分）"


def test_choosing_local_again_does_not_stick_to_the_disk(tmp_path, mounted_disk):
    """改回本机 → 取值回到配置里的 `bag_dir`，**不能被上一次的选择黏住**。

    黏住的后果很具体：盘已经拔了，界面还按盘上的目录给脚本传 `BAG_DIR`，
    起录直接失败。
    """
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb, bag_dir=str(tmp_path / "localbags"))
    disk_target = de.disk_record_target(mgr)
    mgr.set_record_target(disk_target.key, disk_target.path)
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
    assert settings["record_target"] == de.disk_target_key(usb), \
        "界面交出去的键必须带挂载点（多盘时才能区分选的是哪一块）"
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
                                                        mounted_disk, dialog_factory):
    """上次选了硬盘、这次盘拔了 → **回落到本机**，不能把不可用的项留在选中态。

    留在选中态的后果是：填完名字点「开始录制」才发现写不进去。
    """
    # ① 先在"盘插着"的时候选一次（正常流程）。枚举由 `mounted_disk` 夹具提供。
    usb = _usb(tmp_path)
    mgr = _mgr(tmp_path, mount=usb)
    disk_target = de.disk_record_target(mgr)
    assert disk_target is not None, "前提：盘插着时应当有一个可用候选"
    mgr.set_record_target(disk_target.key, disk_target.path)

    # ② 再拔盘：枚举不到 = 枚举层认为没有外接盘（这才是"拔了"的准确表达，
    #    目录还在那儿但盘没了，只删标识文件那是不完整的情形）
    monkeypatch.setattr(de, "list_removable_mounts", lambda configured="": [])

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


def test_dialog_without_config_still_opens(tmp_path, mounted_disk, dialog_factory):
    """`config=None`（旧用法/替身）也要能开 —— 不能因为多了这一栏就崩。

    枚举层返回空（夹具默认）→ 外接盘那一条不可用并写着原因；**不允许整条消失**：
    消失了操作员分不清是"没插盘"还是"没认出来"。
    """
    dialog = dialog_factory(None)
    assert dialog.target_combo.count() == 2, "本机项 + 外接盘项（不可用也在）"
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
    ok, dummy = _apply(mgr, {"record_target": target.key, "record_dir": target.path})

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
    ok, dummy = _apply(mgr, {"record_target": de.disk_target_key(usb),
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
    # v6.7.0 起磁盘侧不打包：**目录**落在序列号/日期/名字下，不再有 zip
    landed = list(usb.rglob("part.mcap"))
    assert len(landed) == 1, "常规路径必须仍然导出"
    assert landed[0].parent.name == "vla_20261014110000"
    assert not list(usb.rglob("*.zip")), "磁盘侧不该再有归档"


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
