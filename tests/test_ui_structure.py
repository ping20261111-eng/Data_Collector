"""界面包结构契约：拆包之后对外接口不能变。

2026-09-18 把单文件 `ui.py`（3455 行，其中 `MainWindow` 一类占 43%）按职责拆成
`data_collector/ui/` 包。拆分的价值是"改某块界面只需打开一个文件"，但它带来一个
明确风险：**对外接口悄悄变了** —— 原来的 `from data_collector.ui import MainWindow`
失效、或者某个方法在搬运过程中掉了。

这个文件把接口钉住，对应拆分方案里的验收清单：
① 包对外名字仍可用；② MainWindow 仍由各面板组成且方法齐全；③ 单个文件不再长回去。
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# MainWindow 由这 8 个面板 Mixin 组合而成
PANEL_MIXINS = (
    "TopbarMixin", "LayoutMixin", "ActionsMixin", "StatusMixin",
    "LaunchMixin", "TeleopMixin", "DataOpsMixin", "UploadMixin",
)

# 包的对外名字：历史代码与测试直接用这些
PUBLIC_NAMES = (
    # 主窗口与对话框
    "MainWindow", "TeleopDialog", "CalibrationDialog", "RecordDialog",
    "ActionLabelDialog", "DataViewDialog", "RemoteDirectoryDialog",
    # 控件
    "ClickableLabel", "LogView", "BatteryIndicator",
    # 工作线程
    "RemoteTaskThread", "ProcessExitThread", "TeleopLogThread",
    "CalibrationThread", "UploadThread", "FetchLogThread",
    # 共享常量与函数
    "SP", "ARM_MOTION_LABELS", "ensure_remote_password", "file_link",
    "make_panel_head", "_as_int", "_main_program_running", "_play_bag_folder",
    # PyQt 名字：历史写法是 `ui_mod.QDialog`，拆包后必须仍能取到
    "QMainWindow", "QDialog", "QMessageBox", "QFileDialog", "QDesktopServices",
    "QThread", "pyqtSignal", "QTimer",
)

# MainWindow 拆分前的全部方法（67 个）。搬运过程丢一个就在这里红。
MAIN_WINDOW_METHODS = (
    "__init__", "append", "append_link", "append_safe", "closeEvent",
    "on_bag_rate_stat", "on_calib_finished", "on_cancel_calibration", "on_cancel_upload",
    "on_clear_log", "on_emergency_stop", "on_fetch_log", "on_fetch_log_finished",
    "on_launch", "on_monitor_performance", "on_play_data", "on_record",
    "on_release_emergency_stop", "on_select_version", "on_stop_main", "on_stop_record",
    "on_stop_teleop", "on_teleop", "on_teleop_exit", "on_teleop_started",
    "on_teleop_startup_timeout", "on_toggle_robot_monitor", "on_toggle_theme",
    "on_upload", "on_upload_finished", "on_view_data", "refresh_hand_display",
    "refresh_robot_status", "refresh_topbar_facts", "show_error", "show_upload_progress",
    "update_progress_label", "update_record_time", "update_teleop_time",
    "update_upload_progress", "_adopt_running_teleop", "_build_actions",
    "_build_console", "_build_main_column", "_build_pillar", "_build_status_line",
    "_build_topbar", "_block_launch_when_running", "_enter_calibration_mode",
    "_enter_teleop_running_mode", "_enter_teleop_starting_mode", "_exit_calibration_mode",
    "_exit_teleop_mode", "_launch_teleop", "_make_fact", "_maybe_setup_product",
    "_on_estop_finished", "_robot_monitor_running", "_run_startup_doctor",
    "_set_teleop_button", "_shutdown_threads", "_sync_theme_button", "_teleop_elapsed_text",
    "_teleop_log_path", "_update_robot_status", "_watch_estop_process",
    "_watch_product_setup",
)

# 单个界面文件的行数上限。拆分前是 3455 行；给足余量，只在"又长回巨型文件"时触发。
MAX_LINES = 1000


def test_package_exports_public_names():
    """拆包后包命名空间上仍能取到全部对外名字。

    特别容易漏的是 PyQt 名字 —— 原来它们在 ui.py 的模块作用域里，拆包后落到子模块，
    如果不在 __init__ 重导出，取用时会变成静默的 AttributeError。
    """
    import data_collector.ui as ui

    missing = [n for n in PUBLIC_NAMES if not hasattr(ui, n)]

    assert not missing, f"包命名空间缺少对外名字: {missing}"


def test_main_window_composes_panel_mixins():
    """MainWindow 由 8 个面板 Mixin 组合而成（方法归位后的结构）。"""
    from data_collector.ui import MainWindow

    bases = [c.__name__ for c in MainWindow.__mro__]

    assert bases[0] == "MainWindow"
    for mixin in PANEL_MIXINS:
        assert mixin in bases, f"{mixin} 不在 MainWindow 的 MRO 里"
    assert "QMainWindow" in bases


def test_main_window_keeps_all_methods():
    """拆分是"方法归位"，一个都不该丢 —— 67 个方法全部仍可调用。"""
    from data_collector.ui import MainWindow

    missing = [m for m in MAIN_WINDOW_METHODS if not hasattr(MainWindow, m)]

    assert not missing, f"拆分后丢失方法: {missing}"


def test_each_method_defined_exactly_once():
    """同一方法不能同时留在 MainWindow 又搬到面板里 —— 否则会出现"改了没生效"。"""
    from data_collector.ui import MainWindow

    own_owners = {}
    for cls in MainWindow.__mro__:
        if not cls.__module__.startswith("data_collector.ui"):
            continue
        for name in cls.__dict__:
            if name.startswith("__") or not callable(getattr(MainWindow, name, None)):
                continue
            own_owners.setdefault(name, []).append(cls.__name__)

    dup = {n: v for n, v in own_owners.items() if len(v) > 1}

    assert not dup, f"以下方法在多个面板重复定义: {dup}"


def test_no_single_ui_file_balloons():
    """没有哪个界面文件长回巨型文件（拆分前 ui.py 是 3455 行）。"""
    import data_collector.ui as ui

    ui_dir = Path(ui.__file__).parent
    oversized = {f.name: len(f.read_text(encoding="utf-8").splitlines())
                 for f in ui_dir.rglob("*.py")
                 if len(f.read_text(encoding="utf-8").splitlines()) > MAX_LINES}

    assert not oversized, f"界面文件超过 {MAX_LINES} 行: {oversized}"


#: 「姿势控制」这一块的文件行数上限。为什么只圈这一块：`core_operations.py` / `core.py`
#: 是历史文件（2000+ 行），本次不动它们；而这一块是新拆出来的，拆的理由就是"过了 1000 行"，
#: 不给它一条守卫，下次又会悄悄长回去（v6.0.0 的 `core_motion.py` 就是这么到 1099 行的 ——
#: ui 守卫覆盖不到它，所以一直没有报错）。
POSTURE_FILES = ("core_motion.py", "motion_kinematics.py", "motion_runner.py",
                 "ros_bridge.py", "ui/posture.py", "ui/posture_widgets.py",
                 "ui/posture_draw.py", "ui/posture_modules.py",
                 "ui/posture_dialogs.py", "ui/posture_cards.py")


def test_posture_modules_stay_under_the_line_limit():
    """姿势控制的每个文件都在 1000 行内（拆出来的册子不该再长回去）。"""
    import data_collector

    root = Path(data_collector.__file__).parent
    sizes = {name: len((root / name).read_text(encoding="utf-8").splitlines())
             for name in POSTURE_FILES}
    oversized = {name: lines for name, lines in sizes.items() if lines > MAX_LINES}

    assert not oversized, f"姿势控制文件超过 {MAX_LINES} 行: {oversized}（拆册见 NOTES-gui.md）"


#: `scripts/` 下与姿势控制有关的两本册子（脚本不受 ui 那条守卫覆盖，单独圈住）。
#: 通道脚本里那一册到 v6.2.0 已是 669 行，离红线还有余量 —— 但"有余量"正是上次它们
#: 悄悄涨到 1099 行的原因，所以这里显式圈住。
SCRIPT_FILES = ("motion_stream.py", "ros_command_bridge.py", "diagnose_trunk.py")


def test_posture_scripts_stay_under_the_line_limit():
    """姿势控制用到的几个脚本也都在 1000 行内。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sizes = {name: len((scripts / name).read_text(encoding="utf-8").splitlines())
             for name in SCRIPT_FILES}
    oversized = {name: lines for name, lines in sizes.items() if lines > MAX_LINES}

    assert not oversized, f"脚本超过 {MAX_LINES} 行: {oversized}"


def test_no_duplicate_top_level_names_in_a_test_file():
    """**同一个测试文件里不许有重名的顶层定义**（v6.2.0 踩到的坑）。

    为什么值得一条守卫：测试文件的所有顶层名字共享一个模块命名空间，**后定义的会静默
    覆盖先定义的** —— 新写一个辅助类时若与旧的重名，被"吃掉"的是**另一批用例**，
    报错信息指向的文件与原因完全对不上（实测：新加的 `_FakePublisher` 覆盖了给
    `connect_sequence` 用的那个，于是"上电时序"那两条用例报 `__init__() missing 1
    required positional argument`，排查方向全错）。
    """
    import ast

    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(tests_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen = {}
        for node in tree.body:                       # 只看顶层（函数内的遮蔽是正常的）
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Assign):
                name = ", ".join(target.id for target in node.targets
                                 if isinstance(target, ast.Name)) or None
            else:
                continue
            if not name:
                continue
            for item in name.split(", "):
                if item in seen:
                    offenders.append(f"{path.name}: {item}（第 {seen[item]} 行与第 "
                                     f"{node.lineno} 行）")
                else:
                    seen[item] = node.lineno

    assert offenders == [], ("同一个测试文件里出现了重名的顶层定义（后者会静默覆盖前者）：\n"
                             + "\n".join(offenders))
