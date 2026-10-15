"""scripts/ 下 python 脚本的行为测试。

这些脚本既被上位机调用，也能脱离界面单独运行，所以单独测：
* robot_monitor.py  —— 状态枚举翻译与原子写盘
* play_bag.sh       —— 手型归一化的端到端行为（用假 workspace + 桩 ros2/rviz2）

不依赖 ROS 环境与真实机器人。
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
#: 机器人侧的官方 FCC 抓取测试程序。本上位机**不复用它**（它固定驱动左右两侧），
#: 但协议取值必须与它逐条一致 —— 由 `test_fcc_script_constants_match_vendor_test` 守着。
#: 不在本机（没有机器人工作空间）时那条用例跳过，而不是假装通过。
VENDOR_FCC = Path(os.environ.get(
    "ROBOT_FCC_REFERENCE",
    "/home/nerv/test_ws/wheeled_robot_pkg/install/lib/tools/dex11_fcc_grasp_test.py"))

HAND_NAMES = ["empty", "hangkai_tg9801", "yinshi_rh56d", "yushu_dex11"]

#: 官方手柄示例里手臂实时流（servoj）的实现。它同时是"125Hz / 8ms"这条约定的**依据来源**：
#: 频率要与它对齐，`*_arm_move` 只在"非 follow position 模式"生效这条也在它附近写着。
#: 不在本机（没有机器人工作空间）时相关断言跳过，而不是假装通过。
ARM_PROXY = Path(os.environ.get(
    "ROBOT_ARM_PROXY_REFERENCE",
    "/home/nerv/test_ws/wheeled_robot_pkg/sample/src/joy_event_receiver/src/arm_proxy.cpp"))


def _load_module(name: str, filename: str):
    """按文件路径加载 scripts/ 下的脚本（它们不是包的一部分）。"""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def monitor():
    return _load_module("robot_monitor", "robot_monitor.py")


def make_workspace(tmp_path: Path, hand_type_code: int = 0) -> Path:
    """造一个最小工作空间：rviz 手型目录 + arm_control_config + teleop_config + VERSION。"""
    ws = tmp_path / "ws"
    for name in HAND_NAMES:
        rviz = ws / "install/share/robot_description/rviz" / name
        rviz.mkdir(parents=True, exist_ok=True)
        (rviz / "wheel_robot.rviz").write_text("# fake\n", encoding="utf-8")
    (ws / "VERSION").write_text("v1.4.0\n202609021101\n", encoding="utf-8")
    # play_bag.sh 会 source 它，缺了会在脚本开头就退出
    (ws / "install").mkdir(parents=True, exist_ok=True)
    (ws / "install/setup.bash").write_text("# fake\n", encoding="utf-8")
    (ws / "install/config").mkdir(parents=True, exist_ok=True)
    (ws / "install/config/arm_control_config.yaml").write_text(
        "ArmControlNode:\n"
        "  ros__parameters:\n"
        f"    left_end_effector_type: {hand_type_code}\n"
        f"    right_end_effector_type: {hand_type_code}\n",
        encoding="utf-8",
    )
    (ws / "install/config/teleop_config.yaml").write_text(
        "teleop:\n  calibration: yyt\n  zoffset: 0.22\n"
        "calibrations:\n  yyt:\n    tracker_index: tracker4\n"
        "    shoulder_width: 0.382\n    arm_length: 0.553\n"
        "trackers:\n  tracker4:\n    left_tracker: 1\n    right_tracker: 2\n",
        encoding="utf-8",
    )
    return ws


# ---------- connect_sequence.py 的输出文案 ----------
# 现场反馈：「遥操作开启文案简单一点，不需要暴露那么多信息」。
# 这个脚本模块级 `import rclpy`（缺了直接 sys.exit(2)），而用例环境没有 ROS，
# 所以先塞一组最小替身模块 —— 下面只验**输出文案**，发布/订阅/门禁全由替身顶着。
class _FakePublisher:
    """只记账的发布者替身：记下写过什么，订阅者数假装存在（不走"节点没起来"分支）。"""

    def __init__(self, msg_type):
        self.msg_type = msg_type
        self.published = []

    def publish(self, msg):
        self.published.append(msg)

    def wait_for_all_acked(self, timeout_sec=None):
        return True

    def get_subscription_count(self):
        return 1


class _FakeNode:
    def __init__(self, *args, **kwargs):
        super().__init__() if False else None      # 不调真实 Node（没有 ROS）
        self.publishers = {}

    def create_publisher(self, msg_type, topic, qos):
        publisher = _FakePublisher(msg_type)
        self.publishers[topic] = publisher
        return publisher

    def create_subscription(self, *args, **kwargs):
        return None

    def get_logger(self):
        import logging
        return logging.getLogger("connect_sequence_stub")


class _FakeMsg:
    def __init__(self, data=None):
        self.data = data


@pytest.fixture
def sequence(monkeypatch):
    """加载 scripts/connect_sequence.py（带 rclpy / std_msgs 替身）。"""
    import types

    for name, attrs in (
        ("rclpy", {"init": lambda *a, **k: None, "spin_once": lambda *a, **k: None,
                   "shutdown": lambda *a, **k: None, "ok": lambda: True}),
        ("rclpy.node", {"Node": _FakeNode}),
        ("rclpy.qos", {"QoSProfile": lambda **kwargs: object(),
                       "ReliabilityPolicy": types.SimpleNamespace(
                           RELIABLE="reliable", BEST_EFFORT="best_effort")}),
        ("std_msgs", {}),
        ("std_msgs.msg", {"Bool": _FakeMsg, "Byte": _FakeMsg}),
    ):
        module = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    return _load_module("connect_sequence", "connect_sequence.py")


def test_connect_sequence_summary_line_is_one_liner(sequence):
    """默认预告只有一行：几步、大概等多久 —— 不出现任何话题名。"""
    line = sequence.summary_line("硬件", sequence.SEQUENCE, sequence.DEFAULT_WAITS)

    assert line == "遥操作上电（硬件）：9 步，纯等待约 23 秒"
    assert "/" not in line


def test_connect_sequence_step_line_hides_topics_by_default(sequence):
    """每步进度默认只说人话；话题名/取值只出现在 --verbose。"""
    step = sequence.SEQUENCE[0]

    assert sequence.step_line(False, 1, 9, 0.3, step) == "  [1/9] 双臂下使能（自动切非实时）"

    detailed = sequence.step_line(True, 1, 9, 0.3, step)
    assert "/arm_control/connect" in detailed and "= False" in detailed


def test_connect_sequence_verbose_switch_defaults_off(sequence):
    """`--verbose` 默认关闭；`--dry-run`（预演/排查）自动带上完整表格。"""
    parser = sequence.build_parser()

    assert parser.parse_args([]).verbose is False
    assert parser.parse_args(["--verbose"]).verbose is True
    assert sequence.wants_verbose(parser.parse_args([])) is False
    assert sequence.wants_verbose(parser.parse_args(["--dry-run"])) is True


def test_connect_sequence_default_run_prints_only_progress(sequence, capsys, monkeypatch):
    """整跑一遍默认模式：只有"预告 + 每步进度 + 结果"，没有话题表 / 门禁 / 就绪检查。

    这条是给现场那句反馈兜底的 —— 以后谁把话题表加回默认输出，用例会先红。
    """
    monkeypatch.setattr(sys, "argv", ["connect_sequence.py", "--speed", "0.001"])

    assert sequence.main() == 0

    out = capsys.readouterr().out
    assert "遥操作上电（硬件）：9 步" in out
    assert "[1/9] 双臂下使能（自动切非实时）" in out
    assert "[9/9] 右臂进入跟随" in out
    assert "上电完成，用时" in out
    for leaked in ("/arm_control", "/trunk_control", "/chassis_control",
                   "门禁", "就绪检查", "[执行]", "[发现]"):
        assert leaked not in out, f"默认输出里不该出现 {leaked}"


def test_connect_sequence_verbose_run_keeps_full_table(sequence, capsys, monkeypatch):
    """`--verbose` 才给完整表格（含话题名与每步等待），排查时用。"""
    monkeypatch.setattr(sys, "argv", ["connect_sequence.py", "--speed", "0.001", "--verbose"])

    assert sequence.main() == 0

    out = capsys.readouterr().out
    assert "=== 上电时序（硬件，共 9 步）===" in out
    assert "/trunk_control/motor_mode" in out
    assert "/arm_control/left_arm_follow_position_set" in out
    assert "[发现] 全部就绪" in out
    assert "=== 时序完成，总用时" in out


def test_module_steps_match_connect_sequence(sequence):
    """页面的单模块下发与**整机上电时序同源**：话题名与每步等待都必须对得上。

    两处一旦漂移，会出现"整机时序能起来、页面上单独点某一步没反应"这种最难查的形态：
    话题名或等待值只改了一边，另一边照样"成功"（命令确实发出去了），而硬件没反应。

    **唯一允许不同、而且必须不同的是模式取值**（v6.2.0 修的那一处）：
    上电时序的 `motor_mode 1`（实时）是**遥操作**要的形态 —— 实时模式下关节按允许的
    最大速度追目标、要上位机自己 100Hz 插值；而本页（非实时）只发**单条**点位目标，
    所以上使能结束时必须是 `0`（非实时，节点内部插值）。现场那句"进入直接就是非实时
    模式，不需要重新连接"指的就是它。这条差异由下面的断言显式钉住，谁改都要看见这行注释。
    """
    from data_collector.core_operations import MODULE_STEPS

    seq = [(sequence.TOPICS[key][0], value, sequence.DEFAULT_WAITS[wait_key])
           for key, value, wait_key, _desc, _sim, _gate in sequence.SEQUENCE]
    seq_topics_and_waits = [(topic, wait) for topic, _value, wait in seq]

    for (_module, _enabled), steps in MODULE_STEPS.items():
        for label, topic, _kind, _value, wait in steps:
            assert (topic, wait) in seq_topics_and_waits, (
                f"{label} 的话题或等待与上电时序对不上：{topic} 等待 {wait}")

    # 切模式那一步：话题必须是官方那条，取值必须是 0（非实时）
    mode_label, mode_topic, mode_kind, mode_value, mode_wait = MODULE_STEPS[("trunk", True)][1]
    assert mode_topic == sequence.TOPICS["trunk_mode"][0]
    assert mode_wait == sequence.DEFAULT_WAITS["trunk_rt"]
    assert mode_kind == "byte"                # std_msgs/Byte 的 data 是 octet，按 int 填会被拒
    assert mode_value == 0, f"「{mode_label}」必须是 0（非实时）"

    # 反向锚：官方时序里那一步确实是 1（实时的依据来自它，不是我们编的）
    official = [value for topic, value, _wait in seq
                if topic == sequence.TOPICS["trunk_mode"][0]]
    assert official == [1]


# ---------- dex11_fcc_grasp.py（FCC 实时抓取）----------
@pytest.fixture
def fcc(monkeypatch):
    """加载 scripts/dex11_fcc_grasp.py（带 rclpy / sensor_msgs / std_msgs 替身）。"""
    import types

    class _FakeNode:
        def __init__(self, *args, **kwargs):
            self.publishers = {}

        def create_publisher(self, msg_type, topic, qos):
            publisher = type("P", (), {"publish": lambda self, m: None,
                                       "data": None})()
            self.publishers[topic] = publisher
            return publisher

        def create_subscription(self, *args, **kwargs):
            return None

        def get_logger(self):
            import logging
            return logging.getLogger("fcc_stub")

        def destroy_node(self):
            return None

    class _FakeMsg:
        def __init__(self, data=None):
            self.data = data

    for name, attrs in (
        ("rclpy", {"init": lambda *a, **k: None, "spin_once": lambda *a, **k: None,
                   "shutdown": lambda *a, **k: None, "ok": lambda: True}),
        ("rclpy.node", {"Node": _FakeNode}),
        ("rclpy.qos", {"QoSProfile": lambda **kwargs: object(),
                       "ReliabilityPolicy": types.SimpleNamespace(
                           RELIABLE="reliable", BEST_EFFORT="best_effort")}),
        ("sensor_msgs", {}),
        ("sensor_msgs.msg", {"JointState": _FakeMsg}),
        ("std_msgs", {}),
        ("std_msgs.msg", {"Bool": _FakeMsg, "UInt16MultiArray": _FakeMsg}),
    ):
        module = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    return _load_module("dex11_fcc_grasp", "dex11_fcc_grasp.py")


def _vendor_constants(source: str) -> dict:
    """从官方测试脚本的源码里取出数值常量。

    刻意按"名字列表 = 取值列表"解析、而不是用正则找单名：官方脚本里写的是
    `RAW_OPEN, RAW_CLOSED = 100, 1000` 这种**成对赋值**，单名正则压根匹配不到 ——
    那样会静默地一条都断言不上，比断言失败更糟。
    """
    values = {}
    for line in source.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line or line.startswith(("if ", "return", "assert")):
            continue
        names_part, _, raw_part = line.partition("=")
        names = [name.strip() for name in names_part.split(",")]
        if not names or not all(name.isidentifier() for name in names):
            continue
        parts = [part.strip() for part in raw_part.split(",")]
        if len(parts) != len(names):
            continue
        for name, value in zip(names, parts):
            try:
                values[name] = float(value)
            except ValueError:
                pass                      # 非数值（字符串 / 表达式）不关心
    return values


def test_fcc_script_constants_match_core_operations(fcc):
    """脚本里的协议取值必须与上位机 `core_operations.GRIPPER_*` 完全一致。

    两处漂移的后果是"整机测试能抓、界面上点不动"这种最难查的形态：命令发出去了、
    退出码 0、硬件没反应。脚本是独立进程（不能 import 上位机包，那样会把 GUI 依赖
    拖进来），所以只能用契约测试把它们钉在一起。
    """
    from data_collector import core_operations as co

    assert fcc.SIDES == tuple(co.GRIPPER_JOINTS)
    assert fcc.JOINTS == co.GRIPPER_JOINTS
    assert fcc.RAW_OPEN == co.GRIPPER_RAW_OPEN
    assert fcc.RAW_CLOSED == co.GRIPPER_RAW_CLOSED
    assert fcc.JNT_OPEN == co.GRIPPER_JNT_OPEN
    assert fcc.JNT_CLOSED == co.GRIPPER_JNT_CLOSED
    assert fcc.TAU_MIN == co.GRIPPER_TAU_MIN_NM
    assert fcc.TAU_MAX == co.GRIPPER_TAU_MAX_NM
    assert fcc.SPEED_MAX_MMS == co.GRIPPER_SPEED_MAX_MMS
    assert fcc.OPEN_GAP_MM == pytest.approx(co.GRIPPER_OPEN_GAP_MM, abs=0.01)
    # 两侧对同一件事的说法也要一致（界面显示"左爪"，脚本也打"左爪"）
    assert fcc.SIDE_NAMES == co.GRIPPER_NAMES


@pytest.mark.skipif(not VENDOR_FCC.exists(), reason="本机没有机器人工作区，跳过与官方脚本的比对")
def test_fcc_script_constants_match_vendor_test(fcc):
    """与**官方** `dex11_fcc_grasp_test.py` 的协议取值一致（它是这套协议的权威实现）。

    为什么要比：官方脚本固定驱动左右两侧、而且会随机器人侧升级 —— 一旦它那几个数字
    变了（比如张开位从 100 改成 0），我们这份等价实现必须跟着改，否则会出现
    "官方工具抓得住、界面抓不住"。
    """
    source = VENDOR_FCC.read_text(encoding="utf-8")
    constants = _vendor_constants(source)

    for name in ("RAW_OPEN", "RAW_CLOSED", "JNT_OPEN", "JNT_CLOSED",
                 "TAU_MIN", "TAU_MAX", "SPEED_MAX_MMS", "RATE_HZ"):
        assert name in constants, "官方脚本里没找到 " + name
        assert constants[name] == pytest.approx(getattr(fcc, name)), name
    # 话题名与关节名：官方脚本用 f-string 拼（`f"/arm_control/{s}_hand_forward_set"`），
    # 源码里查不到拼好的全名 —— 所以这边查"命名规则"的各个片段。
    # 我们那份拼出来的全名由 test_fcc_script_topic_names_are_built_from_side 直接验。
    assert '"/arm_control/"' in source or "/arm_control/" in source
    for suffix in ("_hand_forward_set", "_hand_forward_update"):
        assert suffix in source, suffix
    assert 'SIDES = ("left", "right")' in source
    assert '"LJoint1_1"' in source and '"RJoint1_1"' in source


def test_fcc_script_topic_names_are_built_from_side(fcc):
    """我们那份拼出来的话题全名要和官方一致（实测拼装结果，不是查字符串）。"""
    node = fcc.FccGrasp(("left",))

    assert set(node.publishers) == {
        "/arm_control/left_hand_forward_set",
        "/arm_control/left_hand_forward_update",
        "/arm_control/right_hand_forward_set",
        "/arm_control/right_hand_forward_update",
    }
    assert fcc.JOINTS == {"left": "LJoint1_1", "right": "RJoint1_1"}


def test_fcc_script_conversions_are_the_same_as_core(fcc):
    """脚本的换算与上位机同一套（同样的输入给同样的输出）。"""
    from data_collector import core_operations as co

    for raw in (0, 100, 300, 500, 1000, 1200):
        assert fcc.raw_to_tau(raw) == pytest.approx(co.gripper_tau_nm(raw))
        assert fcc.raw_to_speed_mms(raw) == pytest.approx(co.gripper_speed_mms(raw))
        assert fcc.raw_to_joint(raw) == pytest.approx(co.gripper_joint_rad(raw))


def test_fcc_script_seconds_match_core(fcc):
    """预计耗时的公式两边一致 —— 界面用它预告"会安静几秒"，脚本用它判超时。"""
    from data_collector import core_operations as co

    for speed_raw, hold in ((300, 3.0), (1000, 0.5), (100, 60.0)):
        assert fcc.total_seconds(speed_raw, hold) == pytest.approx(
            co.gripper_fcc_seconds(speed_raw, hold))


def test_fcc_script_sides_defaults_and_parsing(fcc):
    """`--side` 三档：left / right / both；`--side` 必填（没有默认值）。"""
    assert fcc.selected_sides("left") == ("left",)
    assert fcc.selected_sides("both") == ("left", "right")

    parser = fcc.build_parser()
    args = parser.parse_args(["--side", "left"])
    assert (args.tau, args.speed, args.hold, args.verbose) == (300, 300, 3.0, False)
    args = parser.parse_args(["--side", "right", "--tau", "500", "--speed", "200",
                              "--hold", "5", "--verbose"])
    assert (args.tau, args.speed, args.hold, args.verbose) == (500, 200, 5.0, True)
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_fcc_script_quintic_is_smooth(fcc):
    """S 曲线五次多项式：两端一阶、二阶导都为 0 → 起停无冲击。"""
    assert fcc.quintic(0.0) == pytest.approx(0.0)
    assert fcc.quintic(1.0) == pytest.approx(1.0)
    assert fcc.quintic(0.5) == pytest.approx(0.5)
    # 单调递增（中途不会回退）
    values = [fcc.quintic(i / 20.0) for i in range(21)]
    assert values == sorted(values)


def test_fcc_script_stroke_and_total_seconds(fcc):
    """一段行程的时长按 mm 与速度算；总时长 = 4.0 + 两段行程 + 夹持。"""
    assert fcc.stroke_seconds(100, 1000, 30.0) == pytest.approx(40.05 / 30.0, abs=0.01)
    assert fcc.stroke_seconds(1000, 100, 30.0) == fcc.stroke_seconds(100, 1000, 30.0)
    assert fcc.total_seconds(300, 3.0) == pytest.approx(
        4.0 + 2 * fcc.stroke_seconds(100, 1000, 30.0) + 3.0)


def test_fcc_script_opening_text_buckets(fcc):
    """开口分三档（与界面同一口径）：已张开 / 夹着东西 / 合到底。

    阈值基准是**张开位**（raw=100 ≈ 40.1mm），不是满行程 44.5mm —— 张开位本身就留了
    10% 余量，拿满行程当基准会把正常张开算成"没开到位"。
    """
    assert fcc.opening_text(fcc.raw_to_joint(fcc.RAW_OPEN)) == "已张开"
    assert "夹着东西" in fcc.opening_text(fcc.raw_to_joint(500))
    assert "没夹住" in fcc.opening_text(fcc.raw_to_joint(fcc.RAW_CLOSED))
    assert fcc.opening_text(None) is not None      # 不炸，给个兜底文案


def test_fcc_script_summary_line_lists_sides_and_hold(fcc):
    """一行预告要说清：哪只爪、多大力、多快、夹多久 —— 现场据此判断要等多久。"""
    line = fcc.summary_line(("left",), 300, 300, 5.0)
    assert "左爪" in line and "0.60 N·m" in line and "30 mm/s" in line and "5.0s" in line

    both = fcc.summary_line(("left", "right"), 300, 300, 5.0)
    assert "左爪、右爪" in both


def test_fcc_script_missing_joint_line_explains_how_to_fix(fcc):
    """收不到关节不是"没抓到"，是链路没通 —— 必须说清"连接手臂"这一步。"""
    line = fcc.missing_joint_line(("left",))
    assert "左爪" in line and "连接手臂" in line


def test_fcc_script_result_line_marks_interrupt(fcc):
    assert fcc.result_line(False, 9.67) == "FCC 抓取完成，用时 9.7s"
    assert "中断" in fcc.result_line(True, 9.67)


# ---------- robot_monitor.py ----------
def test_monitor_describe_translates_and_keeps_unknown(monitor):
    """枚举翻译必须与 SDK 文档逐字一致；表里没有的值原样保留，不猜。"""
    assert monitor.describe(31, monitor.EMERGENCY_TEXT) == "急停"
    assert monitor.describe(32, monitor.EMERGENCY_TEXT) == "解锁"
    assert monitor.describe(99, monitor.EMERGENCY_TEXT) == "未知(99)"
    assert monitor.describe(45, monitor.CHARGE_TEXT) == "自动充电中"
    assert monitor.describe(77, None) == 77            # 电量这类裸数值不翻译
    assert monitor.describe(None, monitor.MAP_TEXT) is None


def test_monitor_default_output_prefers_env(monitor, tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOT_STATUS_FILE", str(tmp_path / "explicit.json"))
    assert monitor.default_output() == tmp_path / "explicit.json"

    monkeypatch.delenv("ROBOT_STATUS_FILE")
    monkeypatch.setenv("NERV_DIR", str(tmp_path / "nerv"))
    assert monitor.default_output() == tmp_path / "nerv" / "status" / "robot_status.json"


def test_monitor_write_atomic_replaces_and_leaves_no_temp(monitor, tmp_path):
    """界面随时可能在读，状态文件必须原子替换、目录里不能留临时文件。"""
    target = tmp_path / "status" / "robot_status.json"
    monitor.write_atomic(target, {"a": 1})
    monitor.write_atomic(target, {"a": 2, "b": "中文"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "b": "中文"}
    leftovers = [p.name for p in target.parent.iterdir() if p != target]
    assert leftovers == []


def test_monitor_status_topics_cover_documented_fields(monitor):
    """文档里的 7 个状态话题都要被订阅到，少一个界面就会少一项状态。"""
    topics = {topic for topic, _text in monitor.FIELDS.values()}
    assert topics == {
        "/power_report", "/androidmsg_initstatus", "/androidmsg_emergencystatus",
        "/androidmsg_chargestatus", "/androidmsg_locationstatus",
        "/androidmsg_navigationstatus", "/map_status",
    }


def _bare_monitor(monitor, **attrs):
    """绕开 `__init__`（它要 rclpy）装配一个裸对象，专门测里面的纯逻辑。

    `build_payload` / `build_module_state` 只依赖几个 dict 与时间戳，没有 ROS 依赖 ——
    这样在没有 ROS 的用例环境里也能验证"数据源断开"的判据。
    """
    obj = monitor.RobotMonitor.__new__(monitor.RobotMonitor)
    obj.started_at = time.time() - 60
    obj._zero_since = {}
    obj.stale_after = monitor.MODULE_STALE_AFTER
    obj.raw, obj.stamps = {}, {}
    obj.modules, obj.module_stamps = {}, {}
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def test_source_is_lost_needs_grace_and_streak(monitor):
    """「数据源断开」的三条边界。

    为什么不是"查一次为 0 就算断开"：DDS 发现需要几秒，刚启动/刚使能时
    `count_publishers` 可能瞬时为 0，立刻下结论会在现场闪一条假警告。
    为什么不是"字段多久没变"：这些话题事件驱动，健康时 age 也会一直涨（见下一条用例）。
    """
    started = 1000.0

    # 启动宽限期内：即使连续为 0 也不下结论
    assert monitor.source_is_lost(started, started + 5, started) is False
    # 过了宽限期，但 0 只持续了 2 秒（< 3 秒阈值）
    assert monitor.source_is_lost(started + 20, started + 22, started) is False
    # 过了宽限期，连续 3 秒为 0 -> 确认断开
    assert monitor.source_is_lost(started + 20, started + 23, started) is True
    # 有发布者（zero_since=None）→ 立刻回到"正常"
    assert monitor.source_is_lost(None, started + 300, started) is False
    assert monitor.SOURCE_LOST_AFTER < monitor.PUBLISHER_GRACE_SEC


def test_subscribed_topics_cover_everything_we_subscribe(monitor, monkeypatch):
    """数据源监视必须覆盖**全部**订阅话题：7 个状态字段 + 2 个模块 state，且不重复。"""
    monkeypatch.setattr(monitor, "HAVE_MODULE_STATE", True)
    monkeypatch.setattr(monitor, "TrunkState", type("TrunkState", (), {}), raising=False)
    monkeypatch.setattr(monitor, "ArmState", type("ArmState", (), {}), raising=False)

    topics = monitor.subscribed_topics()

    assert len(topics) == len(set(topics))
    assert set(topics) == ({topic for topic, _text in monitor.FIELDS.values()}
                           | {"/trunk_control/trunk_state", "/arm_control/arm_state"})


def test_build_payload_marks_source_lost_per_field(monitor):
    """字段级 `source_lost`：只给确认断开的字段写键，缺键表示"不知道"。

    缺键而不是写 False 是有意的：旧版监控脚本不认识这个字段，界面据此**不做任何标注**，
    不会因为"少查了一次"就在现场报警。原始读数照留（由界面决定怎么显示）。
    """
    obj = _bare_monitor(monitor, raw={"power_percent": 88, "emergency": 32},
                        stamps={"power_percent": time.time(), "emergency": time.time()})
    obj._source_lost = lambda topic, now: topic == "/power_report"   # 只有电量那路断了
    obj.build_module_state = lambda: {}

    status = obj.build_payload()["status"]

    assert status["power_percent"]["source_lost"] is True
    assert status["power_percent"]["raw"] == 88
    assert "source_lost" not in status["emergency"]


def test_build_module_state_skips_lost_flag_without_data(monitor, monkeypatch):
    """模块 state 的判据与状态字段不同：**从未收到数据 = 未使能（正常）**，不是故障。

    要报警的只有一种形态：有读数（说明曾经使能过）、但发布者已经没了 ——
    那时界面会一直显示"已使能"，而没有任何人在更新它。
    """
    monkeypatch.setattr(monitor, "HAVE_MODULE_STATE", True)
    monkeypatch.setattr(monitor, "TrunkState", type("TrunkState", (), {}), raising=False)
    monkeypatch.setattr(monitor, "ArmState", type("ArmState", (), {}), raising=False)

    class _TrunkMsg:
        joint_position = [1.0] * 6

    obj = _bare_monitor(monitor)
    obj.modules = {"trunk_state": _TrunkMsg()}            # 躯干有读数
    obj.module_stamps = {"trunk_state": time.time()}
    obj._source_lost = lambda topic, now: True            # 两个话题都没有发布者了

    payload = obj.build_module_state()

    assert payload["trunk_state"]["has_data"] is True
    assert payload["trunk_state"]["source_lost"] is True
    assert "source_lost" not in payload["arm_state"]      # 手臂从没收到数据 → 不标


def test_module_state_specs_cover_trunk_and_arms(monitor, monkeypatch):
    """二期：躯干与手臂的 state 都要订阅到，且键与话题对得上。

    用例环境里没有 ROS（`wheeled_robot_msgs` 导不进来，所以 `HAVE_MODULE_STATE` 是 False），
    这里显式把它置真并塞两个占位消息类，专门验"表本身"；真实环境下的可用性由
    界面启动监控时 source 工作空间 setup.bash 保证（脚本里 `require_ros_environment()` 会先挡一道）。
    """
    monkeypatch.setattr(monitor, "HAVE_MODULE_STATE", True)
    monkeypatch.setattr(monitor, "TrunkState", type("TrunkState", (), {}), raising=False)
    monkeypatch.setattr(monitor, "ArmState", type("ArmState", (), {}), raising=False)

    specs = monitor.module_state_specs()

    topics = {topic for _key, topic, _msg in specs}
    assert topics == {"/trunk_control/trunk_state", "/arm_control/arm_state"}
    for key, topic, msg_type in specs:
        assert key in topic and msg_type is not None


def test_module_state_degrades_without_message_package(monitor, monkeypatch):
    """`wheeled_robot_msgs` 不在时整段跳过，底盘状态监控不受影响。"""
    monkeypatch.setattr(monitor, "HAVE_MODULE_STATE", False)

    assert monitor.module_state_specs() == ()
    # 未知键不给任何推测字段，只有"有没有数据"这三项事实
    assert monitor.module_entry("不存在的键", None, None) == {
        "has_data": False, "age_sec": None, "enabled": False}


def test_module_entry_trunk_needs_fresh_data(monitor):
    """躯干：有数据 = 已上使能，但**必须够新** —— 收到过不等于现在还在使能。"""
    class FakeTrunk:
        joint_position = (-0.697, -1.394, -0.697, 0.0, 0.0, 0.0)

    fresh = monitor.module_entry("trunk_state", FakeTrunk(), age_sec=0.3, stale_after=2.0)
    assert fresh["has_data"] is True
    assert fresh["enabled"] is True
    assert fresh["joint_deg"][0] == -0.697

    stale = monitor.module_entry("trunk_state", FakeTrunk(), age_sec=9.0, stale_after=2.0)
    assert stale["has_data"] is True
    assert stale["enabled"] is False, "9 秒前的数据不能算'当前已使能'"

    missing = monitor.module_entry("trunk_state", None, age_sec=None)
    assert missing == {"has_data": False, "age_sec": None, "enabled": False}


def test_module_entry_arm_needs_connected_and_power_on(monitor):
    """手臂：`connected` 与 `power_on` 都满足才算"已使能"。

    只有 connected 而没上电时，下发 follow 会被 arm_control_node 静默丢弃 ——
    启动时序里那条"等左右臂 connected"的门禁就是等这个状态。
    """
    class FakeArm:
        left_arm_connected = True
        left_arm_power_on = True
        left_arm_moving = False
        left_arm_emergency_stop = False
        right_arm_connected = True
        right_arm_power_on = False          # 连上了但没上电
        right_arm_moving = False
        right_arm_emergency_stop = True

    entry = monitor.module_entry("arm_state", FakeArm(), age_sec=0.2)

    assert entry["has_data"] is True
    assert entry["enabled_left"] is True
    assert entry["enabled_right"] is False
    assert entry["left"]["power_on"] is True
    assert entry["right"]["emergency_stop"] is True


def test_module_entry_arm_reports_joint_angles_in_degrees(monitor):
    """手臂当前关节角要写进状态文件，且**换算成度**。

    `ArmState.*_arm_pos.position[7]` 是**弧度**，而躯干、预设文件（`arm_motion.json`）、
    教学配置都是**度** —— 界面要同时显示这两者，混用单位会让人按错值。
    这条同时钉住"七个值才算数"和"读不出就不写"（少了键界面会说"未知"，不编数）。
    """
    import math

    class Positions:
        def __init__(self, values):
            self.position = values

    class FakeArm:
        left_arm_connected = True
        left_arm_power_on = True
        left_arm_moving = False
        left_arm_emergency_stop = False
        left_arm_pos = Positions([math.radians(v) for v in
                                  (62.238, 68.3, -22.6, 22.3, 31.6, 17.3, 0.0)])
        right_arm_connected = True
        right_arm_power_on = True
        right_arm_moving = False
        right_arm_emergency_stop = False
        right_arm_pos = Positions([1.0, 2.0])          # 只有两个值：不是合法的一条手臂读数

    entry = monitor.module_entry("arm_state", FakeArm(), age_sec=0.2)

    assert entry["left"]["joint_deg"] == [62.24, 68.3, -22.6, 22.3, 31.6, 17.3, 0.0]
    assert "joint_deg" not in entry["right"], "值个数不对时不许编一个凑数的角度"

    class NoPositions:                       # 老固件/降级：消息里没有 *_arm_pos
        left_arm_connected = True
        left_arm_power_on = True
        left_arm_moving = False
        left_arm_emergency_stop = False
        right_arm_connected = True
        right_arm_power_on = True
        right_arm_moving = False
        right_arm_emergency_stop = False

    bare = monitor.module_entry("arm_state", NoPositions(), age_sec=0.1)
    assert "joint_deg" not in bare["left"] and "joint_deg" not in bare["right"]
    assert bare["enabled_left"] is True      # 没有角度不影响"使能"这个判断


# ---------- play_bag.sh：手型归一化的端到端行为 ----------
def _stub_tools(tmp_path: Path) -> Path:
    """把 ros2 / rviz2 换成无害的桩，让脚本能在没有 ROS 的机器上跑完。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    for name in ("ros2", "rviz2"):
        path = fake_bin / name
        path.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return fake_bin


def _run_play_bag(tmp_path: Path, info_payload, monkeypatch) -> str:
    ws = make_workspace(tmp_path)
    bag = tmp_path / "bag_20260101"
    bag.mkdir()
    if info_payload is not None:
        (bag / "info.json").write_text(
            json.dumps(info_payload, ensure_ascii=False), encoding="utf-8")

    fake_bin = _stub_tools(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "ROBOT_WORKSPACE": str(ws),
    }
    result = subprocess.run(
        ["bash", str(SCRIPTS / "play_bag.sh"), "rviz", f"bag={bag}"],
        input="\n\n", capture_output=True, text=True, env=env, timeout=60,
    )
    return result.stdout + result.stderr


def test_play_bag_normalizes_legacy_short_hand_name(tmp_path):
    """老数据写的是短名 hangkai，直接拿去找 rviz 配置必然失败，必须映射。"""
    output = _run_play_bag(tmp_path, {"hand_type": "hangkai"}, None)
    assert "使用手型配置: hangkai_tg9801" in output


def test_play_bag_keeps_valid_hand_name(tmp_path):
    output = _run_play_bag(tmp_path, {"hand_type": "yushu_dex11"}, None)
    assert "使用手型配置: yushu_dex11" in output


def test_play_bag_falls_back_when_hand_name_has_no_rviz_config(tmp_path):
    """查无此手型时回退 empty，绝不让 rviz2 -d 指向不存在的文件。"""
    output = _run_play_bag(tmp_path, {"hand_type": "zhixing"}, None)
    assert "使用手型配置: empty" in output
    assert "没有对应 rviz 配置" in output


def test_play_bag_without_hand_type_field_uses_empty(tmp_path):
    """历史 bag 根本没有 hand_type —— 也不能退回那个不存在的 hangkai。"""
    output = _run_play_bag(tmp_path, {"name": "old"}, None)
    assert "使用手型配置: empty" in output
    assert "rviz/hangkai/" not in output


# ---------- run_teleop.sh：工作空间定位与参数约定 ----------
# 现场给了一份新版的 run_teleop.sh（上电时序改为 scripts/connect_sequence.py 一次性下发）。
# 它是按"脚本就放在机器人工作空间根目录"写的：`cd $(dirname $0)` 之后直接
# `source install/setup.bash`、`python3 scripts/connect_sequence.py`。
# 但上位机解析到的其实是 **项目 scripts/ 下这份**（resolve_script_path 先找 scripts_dir），
# 于是两个相对路径全落空 —— 实测过：`source` 静默失败，紧接着 python3 去找
# `<项目>/scripts/scripts/connect_sequence.py`，现场只看到一句看不懂的 can't open file。
# 下面四条把它钉在"工作空间由 ROBOT_WORKSPACE 决定"这个约定上（与 play_bag.sh 同款）。
def _make_teleop_workspace(tmp_path: Path, *, with_sequence: bool = True) -> Path:
    """能跑通 run_teleop.sh 的最小工作空间（不依赖 ROS：setup.bash 是假的）。

    `with_sequence=False` 用来复现"机器人侧漏部署了 connect_sequence.py"——
    这是现场真实踩过的形态：界面一点「启动遥操作」就退，报错只有一句
    `can't open file`（依赖没跟着走）。
    """
    ws = tmp_path / "ws"
    (ws / "install").mkdir(parents=True)
    (ws / "install/setup.bash").write_text("# fake setup\n", encoding="utf-8")
    (ws / "scripts").mkdir()
    if with_sequence:
        # 打印里带 ws 标记，用来区分"用的是工作空间那份"还是"上位机侧那份"
        (ws / "scripts/connect_sequence.py").write_text(
            "import sys\nprint('connect_sequence ws', sys.argv[1:])\n", encoding="utf-8")
    record = ws / "scripts/record_bag.sh"
    record.write_text('#!/bin/bash\necho "record_bag $*"\n', encoding="utf-8")
    record.chmod(0o755)
    return ws


def _copy_script_somewhere(tmp_path: Path, *, with_sequence: bool) -> Path:
    """把 run_teleop.sh 拷到一个"上位机 scripts/ 等价目录"，返回那份脚本的路径。

    用临时目录而不是项目 `scripts/`，是为了能自己决定"同目录有没有 connect_sequence.py"。
    """
    where = tmp_path / "upper_scripts"
    where.mkdir(exist_ok=True)
    script = where / "run_teleop.sh"
    script.write_text((SCRIPTS / "run_teleop.sh").read_text(encoding="utf-8"), encoding="utf-8")
    if with_sequence:
        (where / "connect_sequence.py").write_text(
            "import sys\nprint('connect_sequence upper', sys.argv[1:])\n", encoding="utf-8")
    return script


def _stub_teleop_tools(tmp_path: Path, project_sequence: Path = None) -> tuple:
    """ros2 / python3 的桩：ros2 记下参数，python3 让 import rclpy 假装成功。

    `project_sequence`：**随上位机交付的那份** connect_sequence.py 的路径。给了它就
    只回显一行、不真执行 —— 真身要 `import rclpy`（本机没有 ROS），而这条用例关心的是
    「选到了哪一份、参数怎么传」。不传则照常执行（给 _copy_script_somewhere 造的桩用）。
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    calls = tmp_path / "ros2_calls.log"
    ros2 = fake_bin / "ros2"
    ros2.write_text(f'#!/bin/bash\necho "$@" >> {calls}\nexit 0\n', encoding="utf-8")
    ros2.chmod(0o755)
    # 脚本会先探 `python3 -c "import rclpy"`：桩里直接返回 0（这台机器没有 ROS）
    branch = ""
    if project_sequence is not None:
        branch = ('if [[ "${1:-}" == "%s" ]]; then\n'
                  '    echo "connect_sequence project-copy"\n'
                  '    exit 0\n'
                  'fi\n' % project_sequence)
    py = fake_bin / "python3"
    py.write_text("#!/bin/bash\n"
                  'if [[ "${1:-}" == "-c" ]]; then exit 0; fi\n'
                  + branch +
                  'exec /usr/bin/python3 "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return fake_bin, calls


def _run_teleop(tmp_path: Path, script: Path, args=(), *, env_workspace,
                project_sequence: Path = None) -> tuple:
    """跑一次 run_teleop.sh。`env_workspace` 显式给（传 None 表示"环境里就没有"）。"""
    fake_bin, calls = _stub_teleop_tools(tmp_path, project_sequence)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("ROBOT_WORKSPACE", None)          # 别把宿主环境里的值带进用例
    if env_workspace is not None:
        env["ROBOT_WORKSPACE"] = str(env_workspace)
    result = subprocess.run(["bash", str(script), *args], capture_output=True,
                            text=True, env=env, cwd=str(tmp_path), timeout=60)
    output = result.stdout + result.stderr
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return result.returncode, output, logged


def test_run_teleop_launches_with_workspace_from_env(tmp_path):
    """上位机的调用形态：工作空间由 ROBOT_WORKSPACE 传入，脚本在项目 scripts/ 下也要能跑。

    这条是核心 —— 脚本里所有相对路径都必须相对**工作空间**，而不是相对脚本自己的位置。
    上电时序用的是**项目 scripts/ 里那份** connect_sequence.py（随包交付、常规路径）。
    """
    ws = _make_teleop_workspace(tmp_path)
    code, output, calls = _run_teleop(tmp_path, SCRIPTS / "run_teleop.sh", env_workspace=ws,
                                      project_sequence=SCRIPTS / "connect_sequence.py")

    assert code == 0, output
    assert "connect_sequence project-copy" in output   # 用的是上位机侧那份（常规）
    assert "警告" not in output                        # 不是兜底路线
    assert "launch teleop teleop_launch.py" in calls


def test_run_teleop_works_when_placed_at_workspace_root(tmp_path):
    """脚本放在工作空间根目录、且没有 ROBOT_WORKSPACE 时，按脚本位置推断也能跑。"""
    ws = _make_teleop_workspace(tmp_path)
    placed = ws / "run_teleop.sh"
    placed.write_text((SCRIPTS / "run_teleop.sh").read_text(encoding="utf-8"), encoding="utf-8")
    fake_bin, calls = _stub_teleop_tools(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("ROBOT_WORKSPACE", None)

    result = subprocess.run(["bash", str(placed)], capture_output=True, text=True,
                            env=env, cwd=str(ws), timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "launch teleop teleop_launch.py" in calls.read_text(encoding="utf-8")


def test_run_teleop_fails_loudly_without_a_workspace(tmp_path):
    """定位不到工作空间时立刻退出并说清试过哪些路径（原先只会静默失败）。"""
    code, output, calls = _run_teleop(tmp_path, SCRIPTS / "run_teleop.sh", env_workspace=None)

    assert code == 2
    assert "定位不到机器人工作空间" in output
    assert "ROBOT_WORKSPACE" in output               # 告诉现场怎么手工执行
    assert calls == ""                              # 什么都没往下执行


def test_run_teleop_passes_record_name_with_name_prefix(tmp_path):
    """record=xxx 必须转成 `name=xxx`。

    record_bag.sh 只认 `name=` / `desc=`（见 tests/test_contracts.py 的接口契约）；
    原来的 `record_bag.sh "${RECORD_NAME}"` 会被它当成 Unknown argument 丢掉 ——
    也就是 record=xxx 一直没生效，录出来始终是默认名。
    """
    ws = _make_teleop_workspace(tmp_path)
    code, output, _calls = _run_teleop(
        tmp_path, SCRIPTS / "run_teleop.sh", args=["record=mybag"], env_workspace=ws,
        project_sequence=SCRIPTS / "connect_sequence.py")

    assert code == 0, output
    assert "record_bag name=mybag" in output


# ---------- connect_sequence.py：run_teleop.sh 唯一的外部文件依赖 ----------
# 现场报"点了启动遥操作跑不起来"，结论是**少了这个依赖**：它当时只有机器人侧一份，
# 上位机的 scripts/ 与随包交付里都没有。现场明确：**机器人侧本来没有这个文件**，
# 它是这次改时序时新增的，**常规就是跑上位机侧那份**。
# 下面几条把"两处都能放、上位机侧优先"钉住。
def test_run_teleop_prefers_shipped_sequence_script(tmp_path):
    """两处都有时必须用**上位机侧**那份（常规路径），且不打印警告。

    这一条与"项目 scripts/ 优先于工作空间"的旧坑方向一致：时序脚本随上位机交付包
    发布和维护，界面跑的就该是上位机这份。机器人侧那份只是调试期留下的副本。
    """
    ws = _make_teleop_workspace(tmp_path)                          # 工作空间也有
    script = _copy_script_somewhere(tmp_path, with_sequence=True)  # 上位机侧有（常规）

    code, output, _calls = _run_teleop(tmp_path, script, env_workspace=ws)

    assert code == 0, output
    assert "connect_sequence upper" in output
    assert "connect_sequence ws" not in output
    assert "警告" not in output                     # 走的是常规路径，不该有警告


def test_run_teleop_falls_back_to_workspace_sequence_script(tmp_path):
    """上位机侧缺这份依赖时，允许用工作空间里那份兜底，但必须显式警告。"""
    ws = _make_teleop_workspace(tmp_path)                          # 工作空间有
    script = _copy_script_somewhere(tmp_path, with_sequence=False)  # 上位机侧反而没有

    code, output, _calls = _run_teleop(tmp_path, script, env_workspace=ws)

    assert code == 0, output
    assert "connect_sequence ws" in output          # 用的是工作空间那份
    assert "警告" in output
    assert "connect_sequence.py" in output          # 警告里点名是哪个文件
    assert str(script.parent) in output             # 并说清上位机侧本该在哪


def test_run_teleop_lists_both_candidates_when_sequence_missing(tmp_path):
    """两处都没有时退 2，且把找过的两个位置都列出来 —— 不再只丢一句 can't open file。"""
    ws = _make_teleop_workspace(tmp_path, with_sequence=False)
    script = _copy_script_somewhere(tmp_path, with_sequence=False)

    code, output, calls = _run_teleop(tmp_path, script, env_workspace=ws)

    assert code == 2
    assert "找不到上电时序脚本" in output
    assert str(script.parent / "connect_sequence.py") in output
    assert str(ws / "scripts/connect_sequence.py") in output
    assert "本次未向机器人下发任何指令" in output
    assert calls == ""                              # 一条指令都没发出去


def test_shipped_connect_sequence_is_present_and_dry_runnable():
    """随上位机交付的那份确实在包里，并保留自检开关。

    `--dry-run` / `--list` 是现场不动机器人就能自查的手段（README 的上机顺序第一步），
    所以这里不仅查"文件在"，还查这两个开关没被删掉。
    """
    shipped = SCRIPTS / "connect_sequence.py"
    assert shipped.is_file(), "上位机 scripts/ 缺 connect_sequence.py（run_teleop.sh 的依赖）"

    text = shipped.read_text(encoding="utf-8")
    assert "--dry-run" in text and "--list" in text
    compile(text, str(shipped), "exec")             # 至少在语法上可执行


def test_launcher_command_line_runs_the_teleop_script(tmp_path):
    """把上位机**真实拼出来的那条命令**跑一遍。

    前面几条测的是脚本本身；这条补上接缝：上位机是
    `cd <工作空间> && export ROBOT_WORKSPACE=…; … && bash <脚本>`，
    而 `<脚本>` 解析到的是**项目 scripts/ 下这份**（scripts_dir 优先于工作空间）。
    接缝两边任何一边改了约定，这条就会红。
    """
    from data_collector.core import ConfigManager, save_yaml
    from data_collector.core_operations import Launcher

    ws = _make_teleop_workspace(tmp_path)
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(ws),
        "teleop_config_path": "install/config/teleop_config.yaml",
        "scripts_dir": "scripts",
        "run_teleop_script": "run_teleop.sh",
        "run_nodes_script": "run_nodes.sh",
        "local_log_dir": str(tmp_path / "logs"),
    })
    cm = ConfigManager(cfg_file)
    launcher = Launcher(cm)

    script = cm.resolve_script_path("run_teleop.sh")
    assert script == SCRIPTS / "run_teleop.sh"        # 就是项目里这份，不是工作空间那份
    assert cm.get_script_env()["ROBOT_WORKSPACE"] == str(ws)   # 脚本靠它定位工作空间

    fake_bin, calls = _stub_teleop_tools(tmp_path, SCRIPTS / "connect_sequence.py")
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", launcher._build_cmd(script)], capture_output=True,
                            text=True, env=env, cwd=str(tmp_path), timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "connect_sequence project-copy" in result.stdout + result.stderr
    assert "launch teleop teleop_launch.py" in calls.read_text(encoding="utf-8")


# ---------- motion_stream.py（姿势控制实时流）----------
@pytest.fixture
def stream_module(monkeypatch):
    """加载 scripts/motion_stream.py（带 rclpy / std_msgs / wheeled_robot_msgs 替身）。

    这个脚本在 import 阶段就要 `rclpy`（缺了就 `sys.exit(2)`），所以替身必须**先装好**
    再加载 —— 否则整个用例进程会被它 exit 掉。
    """
    import types

    class _FakeMsg:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
            self.data = kwargs.get("data", None)

    class _FakeByteMsg:
        """`std_msgs/msg/Byte` 的替身：**字段初值是 b"\x00"**（不是 None/int）。

        这不是随手写的 —— rclpy 里 `Byte.data` 就是长度 1 的 `bytes`，而
        `publish_byte` 正是按字段类型分支的。替身要是给 int，那个分支永远走不到，
        "Byte 赋值给 int 会抛 AssertionError"这个真机故障就会被放过去（现场就是这么漏的）。
        """

        def __init__(self, **kwargs):
            self.data = kwargs.get("data", b"\x00")

    class _FakeNode:
        def __init__(self, *args, **kwargs):
            self.subs = []
            self.pubs = []

        def create_publisher(self, msg_type, topic, qos):
            pub = type("P", (), {"publish": lambda self, m: None})()
            self.pubs.append((topic, pub))
            return pub

        def create_subscription(self, msg_type, topic, callback, qos):
            self.subs.append((topic, callback))
            return None

        def destroy_node(self):
            return None

    for name, attrs in (
        ("rclpy", {"init": lambda *a, **k: None, "spin_once": lambda *a, **k: None,
                   "shutdown": lambda *a, **k: None}),
        ("rclpy.node", {"Node": _FakeNode}),
        ("rclpy.qos", {"QoSProfile": lambda **kwargs: object(),
                       "ReliabilityPolicy": types.SimpleNamespace(
                           RELIABLE="reliable", BEST_EFFORT="best_effort")}),
        ("std_msgs", {}),
        ("std_msgs.msg", {"Bool": _FakeMsg, "Byte": _FakeByteMsg,
                          "Float32MultiArray": _FakeMsg, "Float64MultiArray": _FakeMsg}),
        ("wheeled_robot_msgs", {}),
        ("wheeled_robot_msgs.msg", {"ArmState": _FakeMsg, "TrunkState": _FakeMsg}),
    ):
        module = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    return _load_module("motion_stream", "motion_stream.py")


class _FakeStreamer:
    """实时流脚本的外部世界替身：记录"发了什么"，并按剧本给出"当前角度"。"""

    def __init__(self, trunk=None, arms=None):
        self.trunk = list(trunk) if trunk else None
        self.arms = dict(arms or {})
        self.published = []
        self.listening = False
        self.shutdown_called = False

    def start_listening(self):
        self.listening = True

    def spin(self, seconds):
        return None

    def wait_trunk(self, timeout=None):
        return self.trunk

    def wait_arm(self, side, timeout=None):
        return self.arms.get(side)

    def publish_trunk(self, values):
        self.published.append(("trunk", None, [round(float(v), 3) for v in values]))

    def publish_bool(self, topic, value):
        self.published.append(("bool", topic, bool(value)))

    def publish_byte(self, topic, value):
        self.published.append(("byte", topic, int(value)))

    def publish_follow_position(self, side, values):
        self.published.append(("follow", side, [round(float(v), 4) for v in values]))

    # ---- 读不到当前角度时页面上会看到的那两行（真身按订阅计数给具体原因）----
    def trunk_missing_reason(self):
        return "      （替身：躯干状态话题没数据）"

    def arm_missing_reason(self, side):
        return f"      （替身：{side} 侧状态话题没数据）"

    def shutdown(self):
        self.shutdown_called = True

    # ---- 断言用的便捷视图 ----
    def of(self, kind):
        return [item for item in self.published if item[0] == kind]


def test_motion_stream_publishes_byte_as_length_one_bytes(stream_module):
    """`std_msgs/msg/Byte.data` 在 rclpy 里是 **bytes（长度必须 1）**，不是 int。

    现场就是死在这里：`message.data = int(1)` 抛
    `AssertionError: The 'data' field must be of type 'bytes' ...`，整条躯干流一条都没发就退了。
    所以替身必须**和真消息同型**（`data` 初值是 b"\x00"）—— 用 int 初值的替身会把这个 bug
    放过去（这正是它当初溜进来的原因）。
    """
    sent = []

    class _ByteMsg:
        def __init__(self):
            self.data = b"\x00"

    class _Pub:
        def __init__(self):
            self.published = []

        def get_subscription_count(self):
            return 1

        def publish(self, message):
            sent.append(message.data)

    class _Node:
        def create_publisher(self, msg_type, topic, qos):
            return _Pub()

    class _Streamer:
        def __init__(self):
            self.node = _Node()

        def spin(self, seconds):
            return None

    streamer = _Streamer()
    streamer._simple_pubs = {}
    # 只借两个方法（不跑 `RosStreamer.__init__`，那会 rclpy.init）
    for name in ("publish_byte", "_ensure_subscriber"):
        setattr(streamer, name, getattr(stream_module.RosStreamer, name).__get__(streamer))
    streamer.publish_byte("/trunk_control/motor_mode", 1)

    assert sent == [b"\x01"], f"Byte.data 必须是长度 1 的 bytes，实际 {sent!r}"


def test_motion_stream_easing_and_interpolation(stream_module):
    """缓动曲线：首尾速度趋近 0、中点对称（与参考脚本 `p2-p3.py` 同一条公式）。"""
    ease = stream_module.ease_in_out_cubic
    assert ease(0.0) == 0.0 and ease(1.0) == 1.0
    assert ease(0.5) == pytest.approx(0.5)
    assert ease(-1) == 0.0 and ease(2) == 1.0            # 越界夹住，不外插
    assert ease(0.25) < 0.25 and ease(0.75) > 0.75       # 两头慢
    assert stream_module.interpolate([0.0, 10.0], [10.0, 0.0], 0.5) == [5.0, 5.0]


def test_motion_stream_value_parsing_and_usage_errors(stream_module):
    """`--values` / `--rate` / `--duration` 的合法性：不合法就拒绝执行（退出码 2）。"""
    assert stream_module.parse_values("1, 2,3", 3) == ([1.0, 2.0, 3.0], "")
    assert stream_module.parse_values("", 3)[1] == "没有收到任何数值"
    assert "不是数字" in stream_module.parse_values("1,x,3", 3)[1]
    assert "需要 3 个值，收到 2 个" in stream_module.parse_values("1,2", 3)[1]

    streamer = _FakeStreamer(trunk=[0.0] * 6)
    factory = lambda: streamer                                          # noqa: E731
    assert stream_module.main(["trunk", "--values", "1,2"], streamer_factory=factory) \
        == stream_module.EXIT_USAGE
    assert stream_module.main(["trunk", "--values", "0,0,0,0,0,0",
                               "--rate", "9999"], streamer_factory=factory) \
        == stream_module.EXIT_USAGE
    assert streamer.published == []                # 参数不合法一条都不发


def test_motion_stream_trunk_prepares_mode_and_streams_from_current(stream_module):
    """躯干：`connect → motor_mode 1 → connect` 之后**从当前角度缓动到目标**，再保持一段。"""
    streamer = _FakeStreamer(trunk=[0.0, -42.1, -18.6, 1.2, 12.0, -3.4])
    code = stream_module.main(
        ["trunk", "--values", "30,54,24,0,0,0", "--duration", "0.1",
         "--rate", "50", "--hold", "0.06", "--prepare"],
        streamer_factory=lambda: streamer)

    assert code == stream_module.EXIT_OK
    assert streamer.listening is True
    bools = streamer.of("bool")
    assert [item[1] for item in bools] == ["/trunk_control/connect"] * 2
    assert all(item[2] is True for item in bools)
    modes = streamer.of("byte")
    assert [(item[1], item[2]) for item in modes] == \
        [("/trunk_control/motor_mode", stream_module.TRUNK_MODE_REALTIME)]

    frames = [item[2] for item in streamer.of("trunk")]
    # 0.1s × 50Hz → 0…5 共 6 帧插值；0.06s × 50Hz → 3 帧保持
    assert len(frames) == 6 + round(0.06 * 50)
    assert frames[0] == [0.0, -42.1, -18.6, 1.2, 12.0, -3.4]   # 从当前出发
    assert frames[-1] == [30.0, 54.0, 24.0, 0.0, 0.0, 0.0]     # 最后一定到目标
    assert all(frames[index][0] <= frames[index + 1][0] for index in range(6))
    assert streamer.shutdown_called is True


def test_motion_stream_arm_turns_follow_position_on_then_off(stream_module):
    """手臂：`follow_position_set=true` → 125Hz 流（弧度原样）→ `set=false`。**用完必须关** ——
    少了它，`*_arm_move`（预设走的那条）会被控制节点忽略（SDK 原文）。"""
    streamer = _FakeStreamer(arms={"left": [0.0] * 7})
    code = stream_module.main(
        ["arm", "--side", "left", "--values", "0.5,0,0,0,0,0,0",
         "--duration", "0.1", "--rate", "50"],
        streamer_factory=lambda: streamer)

    assert code == stream_module.EXIT_OK
    assert streamer.arms["left"] == [0.0] * 7        # 读的是 state 里的当前值
    bools = [item for item in streamer.of("bool")
             if item[1] == "/arm_control/left_arm_follow_position_set"]
    assert [item[2] for item in bools] == [True] * 3 + [False] * 3
    frames = [item[2][0] for item in streamer.of("follow")]
    assert frames[0] == 0.0 and frames[-1] == pytest.approx(0.5, abs=1e-3)


def test_motion_stream_arm_topics_match_core_motion(stream_module):
    """契约：脚本用后缀拼出来的两条手臂话题，必须等于 `core_motion` 里登记的那两条。

    两处名字漂了会变成最难查的形态 —— 界面说"已进入实时流"，实际发到了没人听的话题上，
    表现就是"点了没反应"。所以两处都写死，再由这条用例钉在一起。
    """
    from data_collector import core_motion

    for side in ("left", "right"):
        assert f"/arm_control/{side}{stream_module.ARM_FOLLOW_SET_SUFFIX}" == \
            core_motion.ARM_FOLLOW_SET_TOPICS[side]
        assert f"/arm_control/{side}{stream_module.ARM_FOLLOW_UPDATE_SUFFIX}" == \
            core_motion.ARM_FOLLOW_UPDATE_TOPICS[side]
    # 顺带钉住"是 follow_position，不是 servoj"（现场指定换过一次）
    assert "servoj" not in stream_module.ARM_FOLLOW_SET_SUFFIX
    assert "servoj" not in stream_module.ARM_FOLLOW_UPDATE_SUFFIX


def test_motion_stream_refuses_to_move_without_a_current_angle(stream_module):
    """读不到当前角度就**一条都不发**（没有当前值就没法插值，硬发等于全速冲刺）。"""
    streamer = _FakeStreamer()                       # trunk / arms 都是 None
    code = stream_module.main(["trunk", "--values", "30,54,24,0,0,0",
                               "--duration", "0.05", "--rate", "50"],
                              streamer_factory=lambda: streamer)

    assert code == stream_module.EXIT_NO_DATA
    assert streamer.of("trunk") == []


def test_motion_stream_limits_and_rate_match_the_robot_side_sources(stream_module):
    """契约：脚本里的量程与频率必须等于 `core_motion` 的轴表、且频率与官方示例一致。

    两处漂移会变成最难查的形态：界面上拦得住、脚本里却放过去（或反过来）。
    """
    from data_collector import core_motion

    assert stream_module.TRUNK_LIMITS == \
        tuple((low, high) for _key, low, high, _hint in core_motion.TRUNK_AXES)
    assert stream_module.TRUNK_AXIS_COUNT == len(core_motion.TRUNK_AXES)
    assert stream_module.ARM_AXIS_COUNT == len(core_motion.ARM_AXES["left"])
    assert stream_module.DEFAULT_TRUNK_RATE == core_motion.TRUNK_STREAM_RATE
    assert stream_module.DEFAULT_ARM_RATE == core_motion.ARM_STREAM_RATE
    assert stream_module.TRUNK_MODE_REALTIME == core_motion.TRUNK_MODE_REALTIME
    # 手臂 follow_position 的话题名也必须与 core_motion 里登记的一致
    assert stream_module.ARM_STATE_TOPIC == "/arm_control/arm_state"

    # 官方手柄示例是 8ms 一条 = 125Hz（`arm_proxy.cpp`）；频率改这里之前先改那份依据
    if ARM_PROXY.is_file():
        source = ARM_PROXY.read_text(encoding="utf-8", errors="ignore")
        # 官方示例里 follow_position 与 servoj 两套话题都在（M1/M3 与 M4 键各一套）
        assert "left_arm_follow_position_set" in source
        assert "left_arm_follow_position_update" in source
        assert "left_arm_servoj_set" in source
        assert "milliseconds(8)" in source
        assert round(1000 / 8) == stream_module.DEFAULT_ARM_RATE


# ==================== 【采数】头部至 -32°：只改一个轴 ====================
# 现场 2026-10-15 的要求：点一下把 H6 走到 -32°，**其余轴保持当前角度**。
# 做法是给 `motion_stream.py` 加了 `--set`（目标 = 脚本自己读到的当前角度 + 覆写指定的轴），
# 而不是让上位机先读一遍角度、再拼 6 个值发 `--values` —— 那是两次读数，两次之间的差异
# 会变成其余 5 轴的一次无谓运动。下面这几条盯的就是"其余轴真的不动"与"读不到就别动"。

class _HeadStreamRecorder:
    """只记录、不做任何 IO 的 streamer 替身（`main(..., streamer_factory=...)` 用）。"""

    def __init__(self, current=(55.0, 100.0, 45.0, 0.0, 0.0, 0.0)):
        self.current = None if current is None else list(current)
        self.trunk_targets = []          # 只记 `publish_trunk`（prepare 的 bool/byte 不算目标）
        self.other = []

    def start_listening(self):
        pass

    def wait_trunk(self, timeout=0):
        return None if self.current is None else list(self.current)

    def spin(self, seconds):
        pass

    def publish_trunk(self, values):
        self.trunk_targets.append([float(item) for item in values])

    def publish_bool(self, topic, value):
        self.other.append(("bool", topic, value))

    def publish_byte(self, topic, value):
        self.other.append(("byte", topic, value))

    def trunk_missing_reason(self):
        return "（替身）读不到当前角度"

    def shutdown(self):
        pass


def test_apply_set_axes_keeps_every_other_axis_at_the_current_value(stream_module):
    """目标 = 当前角度，**只有被点名的轴变** —— 这就是"只动 H6"这句话的全部含义。

    这条守卫的价值在反例：若把目标误写成"只有被改的轴有值、其余填 0"，躯干 H1~H5 会一起
    冲向 0°（脚踝 / 膝 / 大腿 / 腰 / 颈全动）—— 采数前的一次定位动作会变成一次全身大动作。
    所以逐轴断言"没被点名的轴与当前值**完全相等**"。
    """
    current = [55.0, 100.0, 45.0, 0.0, 0.0, 7.5]
    target = stream_module.apply_set_axes(current, {5: -32.0})
    assert target == [55.0, 100.0, 45.0, 0.0, 0.0, -32.0]
    assert target[:5] == current[:5], "没被点名的轴必须原样保持当前角度"
    assert current == [55.0, 100.0, 45.0, 0.0, 0.0, 7.5], "不该改动入参（纯函数）"


def test_parse_set_axes_accepts_index_and_axis_name(stream_module):
    """轴写 `6` 或 `H6` 都认（现场手敲这两种写法都会用）；写错要给出原因。"""
    assert stream_module.parse_set_axes(["6=-32"]) == ({5: -32.0}, "")
    assert stream_module.parse_set_axes(["H6=-32"]) == ({5: -32.0}, "")
    assert stream_module.parse_set_axes(["h6=-32"]) == ({5: -32.0}, "")
    assert stream_module.parse_set_axes(["1=10", "6=-32"]) == ({0: 10.0, 5: -32.0}, "")
    for bad in ("6", "0=-32", "7=-32", "X=-32", "6=abc", ""):
        overrides, error = stream_module.parse_set_axes([bad])
        assert overrides is None, f"{bad!r} 应当被判为不合法"
        assert error, f"{bad!r} 应当给出人能看懂的原因"


def test_main_set_mode_refuses_out_of_range_without_force(stream_module, capsys):
    """-32 超出文档量程（H6 是 ±30）→ **不给 `--force` 就拦下，一个值都不发**。

    守的是"越界只拦下、不静默钳位"那条纪律在新加的 `--set` 这条路上同样成立
    （新参数最容易只做对 happy path）。`--force` 的唯一用处见
    `core_motion.HEAD_DATA_TARGET_DEG`。
    """
    streamer = _HeadStreamRecorder()
    code = stream_module.main(
        ["trunk", "--set", "6=-32", "--duration", "1", "--rate", "100"],
        streamer_factory=lambda: streamer)
    assert code == stream_module.EXIT_USAGE, "越界没被拦下"
    assert streamer.trunk_targets == [], "越界时一个目标值都不该发出去"
    assert "超出量程" in capsys.readouterr().out


def test_main_set_mode_walks_from_the_current_angle_and_moves_only_that_axis(stream_module):
    """带 `--force` 时：起点 = **脚本读到的当前角度**，终点 = 目标，**其余轴全程不动**。

    这就是现场那句「读取现在躯干的角度，写入 start_position，然后将 H6 改为 -32」。
    """
    current = [55.0, 100.0, 45.0, 0.0, 0.0, 0.0]
    streamer = _HeadStreamRecorder(current=current)
    code = stream_module.main(
        ["trunk", "--set", "6=-32", "--duration", "0.2", "--rate", "100",
         "--hold", "0", "--prepare", "--force"],
        streamer_factory=lambda: streamer)
    assert code == stream_module.EXIT_OK
    targets = streamer.trunk_targets
    assert targets, "应当发出目标"
    assert targets[0] == current, f"起点必须是当前角度：{targets[0]}"
    assert targets[-1] == [55.0, 100.0, 45.0, 0.0, 0.0, -32.0], \
        f"终点必须是目标：{targets[-1]}"
    for frame in targets:
        assert frame[:5] == current[:5], f"没被点名的轴动了：{frame}"
    # `--prepare` 必须真的做了（connect → motor_mode 1 → connect）
    assert [item[:2] for item in streamer.other].count(("bool", "/trunk_control/connect")) == 2
    assert ("byte", "/trunk_control/motor_mode") in [item[:2] for item in streamer.other]


def test_main_set_mode_does_not_move_anything_when_the_current_angle_is_unreadable(stream_module):
    """读不到当前角度 → **一个值都不发**（退出码 3）。`--set` 这条路同样要守住它。

    这条在 `--set` 下比 `--values` 下更要紧：`--set` 的目标是"当前角度 + 覆写"，
    读不到当前值就连目标都构造不出来，那时候最坏的做法是拿 0 当基准往下发。
    """
    streamer = _HeadStreamRecorder(current=None)
    code = stream_module.main(
        ["trunk", "--set", "6=-32", "--duration", "1", "--force"],
        streamer_factory=lambda: streamer)
    assert code == stream_module.EXIT_NO_DATA
    assert streamer.trunk_targets == [], "读不到当前角度时必须一个值都不发"


def test_main_rejects_values_and_set_together(stream_module, capsys):
    """`--values` 与 `--set` **二选一**：同时给要报错，不能猜用户想要哪个。"""
    streamer = _HeadStreamRecorder()
    code = stream_module.main(
        ["trunk", "--values", "0,0,0,0,0,0", "--set", "6=-32", "--force"],
        streamer_factory=lambda: streamer)
    assert code == stream_module.EXIT_USAGE
    assert streamer.trunk_targets == []
    assert "只能给一个" in capsys.readouterr().out


# ========== head_minus32.py（头部至 -32°：专用脚本，时序照现场实测那份）==========
# 现场 2026-10-15：用户提供的 `to-32.py` 实测能驱动机构，要求按时序实现。
# 它与 `motion_stream.py` 的**关键差别是准备阶段的间隔**（2s vs 0.3s）——
# 下面第一条守卫盯的就是它，因为"把两个脚本的等待时间统一一下"这个念头非常自然，
# 而那会把现场验证过的时序改坏，且坏得很安静（目标被控制节点静默丢弃）。

@pytest.fixture
def head_module(stream_module):
    """加载 `scripts/head_minus32.py`。

    直接依赖 `stream_module`：它已经把 rclpy / std_msgs / wheeled_robot_msgs 的替身装进
    `sys.modules`，两个脚本要的替身项是同一套（都订阅 `TrunkState`、都发
    `Bool` / `Byte` / `Float32MultiArray`）。
    """
    return _load_module("head_minus32", "head_minus32.py")


class _HeadScriptStreamer:
    """`head_minus32.py` 的 streamer 替身：把"等了多久"也记下来。

    ⚠️ **必须记 `spins`**：这个脚本的核心特征是"准备阶段每步等 2s"，
    而等待是通过 `spin(seconds)` 表达的 —— 只记录"发了什么"的话，
    时序这条最关键的约束就完全测不到。
    """

    def __init__(self, current=(55.0, 100.0, 45.0, 0.0, 0.0, 0.0)):
        self.current = None if current is None else list(current)
        self.spins = []          # 每次 spin 等了多少秒
        self.other = []          # (类型, 话题, 值) —— 准备阶段的三步
        self.targets = []        # 发出去的躯干目标序列
        self.listened = False

    def listen(self):
        self.listened = True

    def wait_current(self, timeout=0):
        return None if self.current is None else list(self.current)

    def spin(self, seconds):
        self.spins.append(round(float(seconds), 3))

    def publish_bool(self, topic, value):
        self.other.append(("bool", topic, bool(value)))

    def publish_byte(self, topic, value):
        self.other.append(("byte", topic, int(value)))

    def publish_target(self, values):
        self.targets.append([float(item) for item in values])

    def current_missing_reason(self):
        return "（替身）读不到当前角度"

    def shutdown(self):
        pass


def test_head_script_prepares_with_the_field_proven_two_second_gaps(head_module):
    """⚠️ 准备阶段**每步等 2s** —— 这是"能动"与"不动"的分界，别改小。

    现场那份脚本（用户提供、实测能用）的准备是
    `connect` → 等 2s → `motor_mode 1` → 等 2s → `connect` → 等 2s。
    而 `motor_mode` 改完会让控制节点**重新连电机** —— 重连期间发出去的目标
    **会被静默丢弃**，表现是"命令发了、订阅者也匹配、机构就是不动"。

    这条守卫盯的是"顺手统一"这个动作：`motion_stream.py` 用的是 0.3s，
    很容易有人把这里的 2s 也改成 0.3s。改坏之后本地测试照样绿（时序不影响断言），
    只有现场会表现为"点了没反应"。
    """
    assert head_module.PREPARE_GAP == 2.0, (
        "准备间隔必须保持现场脚本的 2s；改小会让目标被控制节点静默丢弃")

    streamer = _HeadScriptStreamer(current=[10.0, 20.0, 30.0, 0.0, 0.0, 5.0])
    assert head_module.run(streamer) == head_module.EXIT_OK

    assert streamer.listened, "必须先建订阅（准备那几秒里就把当前角度收进来）"
    kinds = [(kind, topic) for kind, topic, _value in streamer.other]
    assert kinds == [
        ("bool", head_module.TRUNK_CONNECT_TOPIC),
        ("byte", head_module.TRUNK_MODE_TOPIC),
        ("bool", head_module.TRUNK_CONNECT_TOPIC),
    ], f"准备三步的顺序不对：{kinds}"
    assert streamer.spins[:3] == [2.0, 2.0, 2.0], (
        f"准备阶段每步必须等 2s：{streamer.spins[:3]}")
    # 第二步必须是"切实时"那个值（0x01）
    assert streamer.other[1][2] == head_module.TRUNK_MODE_REALTIME == 1


def test_head_script_moves_only_the_head_axis(head_module):
    """起点 = **读到的当前角度**，终点只改 H6；**其余 5 轴全程一个都不动**。

    这就是现场那句「读取现在躯干的角度，写入 start_position，然后将 H6 改为 -32」。
    若误把起点写成常量 `[55, 100, 45, 0, 0, 0]`（现场脚本里就是这么写的），
    H1~H5 会跟着走一大段 —— 采数前的一次定位会变成一次全身动作。
    """
    current = [10.0, 20.0, 30.0, 1.0, 2.0, 5.0]
    streamer = _HeadScriptStreamer(current=current)

    assert head_module.run(streamer) == head_module.EXIT_OK

    targets = streamer.targets
    assert targets, "应当发出目标"
    assert targets[0] == current, f"起点必须是当前角度：{targets[0]}"
    assert targets[-1] == [10.0, 20.0, 30.0, 1.0, 2.0, -32.0], \
        f"终点应当是当前角度 + H6 = -32：{targets[-1]}"
    for frame in targets:
        assert frame[:5] == current[:5], f"没被点名的轴动了：{frame}"
    # 目标要覆盖整条插值（不是只发一条）
    assert len(targets) > head_module.RATE, f"目标条数太少，像是没插值：{len(targets)}"


def test_head_script_does_not_move_when_the_current_angle_is_unreadable(head_module):
    """读不到当前角度 → **一个目标都不发**（退出码 3）。

    这条比 `--values` 那条更要紧：目标 = 当前角度 + 覆写 H6，读不到当前值就**构造不出目标**，
    那时最坏的做法是拿 0 当基准 —— 会把 H1~H5 一起带向 0°。
    """
    streamer = _HeadScriptStreamer(current=None)

    assert head_module.run(streamer) == head_module.EXIT_NO_DATA
    assert streamer.targets == [], "读不到当前角度时必须一个目标都不发"


def test_head_script_does_not_set_the_layout(head_module):
    """照现场脚本：**不设 `layout`**，只发 `data`。

    这条容易被"顺手补上"：v6.3.3 有一条实测结论"躯干目标必须设 `data_offset = 1`"，
    但那说的是**非实时 + 单条点位**那条路；实时模式那条路的现场可用样本**都不设**。
    两条路不能互相套用（`core_motion` 的 docstring 里也是这么写的）。
    """
    import inspect

    source = inspect.getsource(head_module.RosStreamer.publish_target)
    # docstring 里正是在解释"为什么不设 layout"，所以只检查 docstring **之后**的代码
    body = source.split('"""', 2)[-1]
    assert ".layout" not in body and "layout=" not in body, (
        "`publish_target` 不该设 layout —— 实时模式下照现场脚本原样发 data 就行")


def test_head_script_constants_match_the_client(head_module):
    """脚本里的常量与上位机侧那份**必须一致**（两处各一份：脚本是独立进程、不 import 包）。

    漂了的表现：上位机按 5s/100Hz/3s 算等待上限，而脚本实际按别的节奏跑 ——
    "其实在跑"会被判成超时（或反过来，超时了还在等）。
    """
    from data_collector import core_motion

    assert head_module.HEAD_TARGET_DEG == core_motion.HEAD_DATA_TARGET_DEG == -32.0
    assert head_module.HEAD_AXIS_INDEX == core_motion.HEAD_DATA_AXIS_INDEX == 5
    assert head_module.DURATION == core_motion.HEAD_DATA_DURATION_SEC
    assert head_module.RATE == core_motion.HEAD_DATA_RATE
    assert head_module.HOLD == core_motion.HEAD_DATA_HOLD_SEC
    # 话题名与模式值也必须一致（脚本自己写死，不 import 包）
    assert head_module.TRUNK_TARGET_TOPIC == core_motion.TRUNK_TARGET_TOPIC
    assert head_module.TRUNK_MODE_TOPIC == core_motion.TRUNK_MODE_TOPIC
    assert head_module.TRUNK_CONNECT_TOPIC == core_motion.TRUNK_CONNECT_TOPIC
    assert head_module.TRUNK_MODE_REALTIME == core_motion.TRUNK_MODE_REALTIME


def test_head_script_waits_for_the_first_frame_instead_of_assuming_zero(head_module):
    """`head_target` 是**纯函数**：目标只由"当前角度 + 常量"决定，没有隐式的 0 基准。"""
    assert head_module.head_target([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]) == \
        [1.0, 2.0, 3.0, 4.0, 5.0, -32.0]
    # 入参不被改动
    source = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    head_module.head_target(source)
    assert source == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


# ---------- ros_command_bridge.py（常驻命令通道）----------
@pytest.fixture
def bridge_module(monkeypatch):
    """加载 scripts/ros_command_bridge.py（带 rclpy / std_msgs / wheeled_robot_msgs 替身）。

    与 `stream_module` 同一个套路：脚本在 import 阶段就要 rclpy，替身必须先装好。
    替身刻意做成**和真消息同型**（`Byte.data` 初值是 `b"\\x00"`、`ArmMove.targets` 是列表、
    状态消息带 `*_arm_pos.position`）—— v6.1.0 的 Byte 故障就是被"太宽松的替身"放过去的。
    """
    import types

    class _Layout:
        """`std_msgs/msg/MultiArrayLayout` 的替身。

        **必须带上** —— 真实消息这个字段永远存在（`dim` + `data_offset`），替身缺了它
        就等于把"layout 没设"这类缺陷掩掉：v6.3.3 的根因恰恰就是
        `Float32MultiArray.layout.data_offset` 没设（缺省 0，机构不动）。
        """

        def __init__(self):
            self.dim = []
            self.data_offset = 0

    class _Msg:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
            if not hasattr(self, "layout"):
                self.layout = _Layout()

    class _Byte:
        def __init__(self, **kwargs):
            self.data = kwargs.get("data", b"\x00")

    class _TrunkState:
        #: **原样存**（不 list()）：用例要能造出"没数据 / 不是可迭代的 / 轴数不对"这几种坏帧
        def __init__(self, joint_position=()):
            self.joint_position = joint_position

    class _ArmPosition:
        def __init__(self, position=()):
            self.position = list(position)

    class _ArmState:
        def __init__(self, left=(), right=(), left_connected=True, right_connected=True):
            self.left_arm_pos = _ArmPosition(left)
            self.right_arm_pos = _ArmPosition(right)
            self.left_arm_connected = left_connected
            self.right_arm_connected = right_connected
            self.left_arm_moving = False
            self.right_arm_moving = False

    for name, attrs in (
        ("rclpy", {"init": lambda *a, **k: None, "spin_once": lambda *a, **k: None,
                   "shutdown": lambda *a, **k: None}),
        ("rclpy.node", {"Node": object}),
        ("rclpy.qos", {"QoSProfile": lambda **kwargs: object(),
                       "ReliabilityPolicy": types.SimpleNamespace(
                           RELIABLE="reliable", BEST_EFFORT="best_effort")}),
        ("std_msgs", {}),
        ("std_msgs.msg", {"Bool": _Msg, "Byte": _Byte, "Float32": _Msg,
                          "Float32MultiArray": _Msg, "Float64MultiArray": _Msg,
                          # v6.4.13 躯干自锁（`switch_power_on`）用的类型。**替身必须
                          # 带上它**：`load_types()` 是按 `MSG_TYPES` 逐个查表的，
                          # 缺一个就在 `types["_raw"]` 里少一个键 —— 替身少这一项时
                          # `load_types()` 直接 ImportError，整个通道的用例全挂。
                          "UInt8MultiArray": _Msg}),
        ("wheeled_robot_msgs", {}),
        ("wheeled_robot_msgs.msg", {"ArmMove": _Msg, "ArmPosition": _ArmPosition,
                                    "HandPosture": _Msg, "ArmState": _ArmState,
                                    "TrunkState": _TrunkState}),
    ):
        module = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    return _load_module("ros_command_bridge", "ros_command_bridge.py")


class _BridgePublisher:
    """替身 publisher：记录发了什么，并**可控制订阅者数**（等订阅者那条路要考它）。

    同时往节点的 `log` 里记一条 `(话题, 消息)` —— "先退跟随、再发目标"这种**顺序**
    只能靠全局日志判（publisher 是启动时预建的，字典顺序与发布顺序无关）。
    """

    def __init__(self, topic, node, subscribers=1):
        self.topic = topic
        self.node = node
        self.subscribers = subscribers
        self.messages = []

    def get_subscription_count(self):
        return self.subscribers

    def publish(self, message):
        self.messages.append(message)
        self.node.log.append((self.topic, message))


class _BridgeNode:
    def __init__(self, subscribers=1):
        self.publishers = {}
        self.subscribers = subscribers
        self.calls = []
        self.subscriptions = []
        self.log = []                       # 全局发布日志：(话题, 消息)，按发布顺序

    def create_publisher(self, msg_type, topic, qos):
        publisher = _BridgePublisher(topic, self, self.subscribers)
        self.publishers[topic] = publisher
        return publisher

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((topic, callback))
        return None

    def destroy_node(self):
        return None


def _make_bridge(bridge_module, node=None):
    """造一个通道：类型表来自真身（替身模块），节点是假节点。"""
    node = node or _BridgeNode()
    return bridge_module.Bridge(node, bridge_module.load_types()), node


def test_bridge_byte_field_is_length_one_bytes(bridge_module):
    """`Byte.data` 必须是**长度 1 的 bytes** —— v6.1.0 的躯干流就是死在这里。

    这条在脚本里再钉一次（`motion_stream.py` 那条已经钉过一遍）：两条通道都会发
    `motor_mode`，谁都不能"按 int 填"。
    """
    types = bridge_module.load_types()["_raw"]
    message = bridge_module.build_message("std_msgs/msg/Byte", {"data": 1}, types)

    assert message.data == b"\x01"


def test_bridge_accepts_the_official_set_style_byte_payload(bridge_module):
    """机器人侧 README 写的是 `{'data': {0x00}}` —— YAML 里那是个**集合**，要折成标量。

    照原样透传的话 `bytes({0})` 会得到空串，而空串在 `Byte` 上直接抛断言 ——
    现场表现就是"这条命令永远发不出去"。
    """
    assert bridge_module.parse_payload("{'data': {0x00}}") == {"data": 0}
    assert bridge_module.parse_payload("data: [45, 81, 36, 0, 0, 0]") == {
        "data": [45, 81, 36, 0, 0, 0]}
    assert bridge_module.parse_payload("data: false") == {"data": False}


def test_bridge_sets_the_trunk_layout_offset(bridge_module):
    """躯干目标的 `layout.data_offset` 必须是 **1** —— 从 YAML 一路守到消息对象（v6.3.3）。

    现场实测的根因：`Float32MultiArray` 只发 `data: [...]`（`layout` 缺省 → `data_offset: 0`）
    时，命令"发得出去、订阅者也匹配，但机构一动不动"；同一台机器换成带
    `data_offset: 1` 的单条，机构就动了。三份可用样本写法完全一致
    （机器人侧 `README.md`、现场测试助手 `Robot Test Assistant/modules/preset.py`、
    现场使能脚本 `shell-switch_pown_on_test/swich_pown_on.sh`）：

        {layout: {dim: [], data_offset: 1}, data: [-10, -18, -8, 0, 0, 0]}

    这条用例守的是**整条链**，任何一环掉了都会红：
    `core_motion.trunk_target_payload`（YAML）→ `parse_payload` → `build_message`。
    """
    from data_collector import core_motion

    fields = bridge_module.parse_payload(
        core_motion.trunk_target_payload([45, 81, 36, 0, 0, 0]))
    types = bridge_module.load_types()["_raw"]
    message = bridge_module.build_message("std_msgs/msg/Float32MultiArray", fields, types)

    assert message.layout.data_offset == 1
    assert list(message.layout.dim) == []              # 三份样本的 dim 都是空表
    assert [round(value, 1) for value in message.data] == [45.0, 81.0, 36.0, 0.0, 0.0, 0.0]
    # 两处各有一份常量（本脚本是独立进程，不 import 包）—— 必须一致
    assert bridge_module.TRUNK_LAYOUT_OFFSET == core_motion.TRUNK_LAYOUT_OFFSET == 1


def test_bridge_trunk_speed_rate_matches_the_client(bridge_module):
    """两处 `TRUNK_SPEED_RATE` 必须一致（本脚本是独立进程，不 import 包）。

    它决定"每次下发躯干目标前先发的那条 speed_rate"是多少 —— 两处漂了的话，
    通道那条路与一次性 CLI 那条路会让机构用**不同的速度**跑同一个预设。
    """
    from data_collector import core_motion

    assert bridge_module.TRUNK_SPEED_RATE == core_motion.TRUNK_SPEED_RATE == 1.0


def test_bridge_trunk_lock_indexes_match_the_client(bridge_module):
    """两处 `TRUNK_LOCK_INDEXES` 必须一致 —— 它决定自锁锁哪几个关节。

    SDK 原文：`switch_power_on` 的参数是"需要切换为仅上电状态的电机索引，
    从下至上 0~5"；现场脚本 `swich_pown_on.sh` 用的是 `[0x00, 0x01, 0x02]`（H1~H3）。
    两处漂了的话，界面按一套锁、兜底那条路按另一套锁，现场看到的是
    "同样点了自锁，锁住的关节不一样"。
    """
    from data_collector import core_motion

    assert bridge_module.TRUNK_LOCK_INDEXES == core_motion.TRUNK_LOCK_INDEXES == (0, 1, 2)
    assert bridge_module.TRUNK_LOCK_LAYOUT_OFFSET == core_motion.TRUNK_LOCK_LAYOUT_OFFSET


def test_bridge_builds_the_trunk_lock_message(bridge_module):
    """自锁那条消息的形状：索引原样、`data_offset` 是 **0**（不是躯干目标的 1）。

    `data_offset` 这一个字段就是 v6.3.3 那个"发得出去、机构一动不动"的根因
    （躯干目标缺省 0 时节点收得到但不执行）。自锁这条话题**恰好相反** ——
    现场脚本 `swich_pown_on.sh` 那段「#自锁」写的就是 `data_offset: 0`。
    这里把 0 钉死，防止有人"顺手统一成 1"。
    """
    types = bridge_module.load_types()["_raw"]
    message = bridge_module.build_message(
        "std_msgs/msg/UInt8MultiArray",
        {"layout": {"dim": [], "data_offset": bridge_module.TRUNK_LOCK_LAYOUT_OFFSET},
         "data": list(bridge_module.TRUNK_LOCK_INDEXES)},
        types)

    assert list(message.data) == [0, 1, 2]
    assert message.layout.data_offset == 0
    assert list(message.layout.dim) == []


def test_bridge_uint8_array_clamps_instead_of_asserting(bridge_module):
    """越界值必须被 `& 0xFF`，不能让 rclpy 抛断言。

    与 `Byte` 分支同一个理由：`uint8` 是定长无符号，塞负数或超界值时直接 assert。
    那种失败发生在**按下去的那一刻**，现场只看到"点了没反应"。
    """
    types = bridge_module.load_types()["_raw"]
    message = bridge_module.build_message("std_msgs/msg/UInt8MultiArray",
                                          {"data": [256, -1, 300]}, types)
    assert list(message.data) == [0, 255, 44]


def test_bridge_ensures_the_trunk_lock_and_unlock_shapes(bridge_module):
    """自锁 = **1 步**；解除 = **2 步**（先下使能、再上使能）。

    现场脚本里解除只发一条 `connect true`，而现场确认那样不生效 —— 正确做法是
    `connect false` → `connect true`（与"改完 motor_mode 必须重发 connect 才生效"同源）。
    这个步数差是这条功能最容易写错的地方，所以逐条钉住话题与数据。
    """
    bridge, _node = _make_bridge(bridge_module)
    sent = []

    def fake_publish(item, progress=None):
        sent.append((item["name"], item["topic"], item["type"], dict(item["payload"])))
        return {"name": item["name"], "ok": True, "sent": 1, "matched": 1, "reason": ""}

    bridge.publish = fake_publish
    bridge.spin = lambda seconds: None          # 别真的等那 3 秒

    bridge.ensure_trunk_lock()
    assert len(sent) == 1, "自锁只有一步（switch_power_on）"
    _name, topic, msg_type, payload = sent[0]
    assert topic == "/trunk_control/switch_power_on"
    assert msg_type == "std_msgs/msg/UInt8MultiArray"
    assert list(payload["data"]) == [0, 1, 2]
    assert payload["layout"]["data_offset"] == 0

    sent.clear()
    bridge.ensure_trunk_unlock()
    assert [item[1] for item in sent] == ["/trunk_control/connect",
                                          "/trunk_control/connect"], "解除是两步"
    assert [item[3]["data"] for item in sent] == [False, True], "先下使能、再上使能"
    assert all(item[2] == "std_msgs/msg/Bool" for item in sent)


def test_bridge_prewarms_the_lock_topic(bridge_module):
    """`switch_power_on` 必须在预热表里 —— 自锁的现场用法就是"点一下、立刻生效"。"""
    topics = {topic for topic, _type in bridge_module.PREWARM}
    assert "/trunk_control/switch_power_on" in topics


def test_bridge_move_path_also_carries_the_layout(bridge_module):
    """通道内侧的 move 路径（dict payload，不经过 YAML）也要带上 `layout`。

    两条路各写死一处：`trunk_target_payload` 走 YAML 给 `publish`/兜底 CLI 用，
    `_send_target` 走 dict 给 `move` 用。只改一处的话，另一条路依旧是"发了不动"。
    """
    bridge, node = _make_bridge(bridge_module)

    bridge._send_target("trunk", None, [1, 2, 3, 4, 5, 6], None)

    message = node.publishers["/trunk_control/joint_target_position"].messages[-1]
    assert message.layout.data_offset == 1
    assert [round(value, 1) for value in message.data] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def test_bridge_waits_for_a_subscriber_before_sending(bridge_module):
    """**等订阅者**是成功率的关键：VOLATILE 下没匹配上就发＝没发。

    上一版靠 `ros2 topic pub -w 1` 等它，但每条命令都是新进程、每次都要重新发现，
    等不到就退出码 124。这里一次不等于**不**失败：写清原因（主程序没在跑），
    让现场知道该去查什么。
    """
    bridge, _node = _make_bridge(bridge_module, _BridgeNode(subscribers=0))

    result = bridge.publish({"name": "躯干目标", "topic": "/trunk_control/joint_target_position",
                             "type": "std_msgs/msg/Float32MultiArray",
                             "payload": {"data": [0] * 6}, "wait_sub": 0.05})

    assert result["ok"] is False and result["sent"] == 0
    assert "没有订阅者" in result["reason"]


def test_bridge_repeats_each_command(bridge_module):
    """有订阅者时**重发** `repeat` 次：单发一次在 VOLATILE 下丢了就永远没了。"""
    bridge, node = _make_bridge(bridge_module)

    result = bridge.publish({"name": "躯干目标", "topic": "/trunk_control/joint_target_position",
                             "type": "std_msgs/msg/Float32MultiArray",
                             "payload": {"data": [1, 2, 3, 4, 5, 6]}, "repeat": 3,
                             "gap_ms": 0})

    assert result["ok"] is True and result["sent"] == 3 and result["matched"] == 1
    assert len(node.publishers["/trunk_control/joint_target_position"].messages) == 3
    assert node.publishers["/trunk_control/joint_target_position"].messages[0].data == \
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def test_bridge_move_trunk_confirms_by_readback(bridge_module):
    """躯干运动：发布 → **回读 `trunk_state` 确认到位** → 报残差。这才是"成功"。

    上一版的"成功"= 退出码 0（命令交出去了），机构到没到全靠固定睡眠猜 ——
    猜短了动作被截断、猜长了节奏拖沓，这就是"效果不满意"里的另一半。
    """
    bridge, node = _make_bridge(bridge_module)
    bridge.trunk = [10.0, 0.0, 0.0, 0.0, 0.0, 0.0]        # 已经"到位"（差 0）
    lines = []

    result = bridge.move("trunk", [10.0, 0, 0, 0, 0, 0], tol=1.0, timeout=2.0, retries=0,
                         progress=lines.append)

    assert result["ok"] is True and result["attempts"] == 1
    assert max(abs(item) for item in result["residual"]) <= 1.0
    assert any("已到位" in line for line in lines)
    assert node.publishers["/trunk_control/joint_target_position"].messages


def test_bridge_move_retries_when_the_command_had_no_effect(bridge_module):
    """发出去但**机构一动不动** → 重发（这才是"命令丢了"的样子），最后如实报原因。"""
    bridge, node = _make_bridge(bridge_module)
    bridge.trunk = [0.0] * 6                                # 一直不动

    # `stall_window` / `timeout` 调小只是让用例跑得快（真值见脚本里的常量）
    result = bridge.move("trunk", [30.0, 0, 0, 0, 0, 0], tol=1.0, timeout=1.0, retries=2,
                         stall_window=0.05)

    assert result["ok"] is False and result["attempts"] == 3        # 首发 + 两次重发
    assert "仍未见动作" in result["reason"]
    assert len(node.publishers["/trunk_control/joint_target_position"].messages) >= 3


def test_bridge_move_arm_releases_follow_before_publishing(bridge_module):
    """手臂：走 `ArmMove` 之前**必须先退出 follow / servoj**。

    SDK 原文：`*_arm_move` 只在"非 follow position 模式"生效 —— 遥操作会把手臂留在
    follow 模式，那时 ArmMove 被**静默忽略**（没有任何报错），现场看到的就是
    "预设点了没反应"。所以这两条 `false` 必须在 ArmMove **之前**发出去。
    """
    bridge, node = _make_bridge(bridge_module)
    bridge.arms["left"] = [0.0] * 7

    result = bridge.move("arm", [0.0] * 7, side="left", tol=1.0, timeout=1.0, retries=0)

    assert result["ok"] is True
    published = [topic for topic, _message in node.log]
    assert "/arm_control/left_arm_follow_position_set" in published
    assert "/arm_control/left_arm_servoj_set" in published
    assert published.index("/arm_control/left_arm_follow_position_set") < \
        published.index("/arm_control/left_arm_move")
    assert published.index("/arm_control/left_arm_servoj_set") < \
        published.index("/arm_control/left_arm_move")
    follow = node.publishers["/arm_control/left_arm_follow_position_set"].messages
    assert [item.data for item in follow] == [False] * len(follow)
    move = node.publishers["/arm_control/left_arm_move"].messages[0]
    # 协议里是度，消息里是弧度：换算只发生在通道内
    assert move.targets[0].position == [0.0] * 7
    assert move.scale == pytest.approx(0.4)


def test_bridge_reports_unconfirmed_when_there_is_no_state(bridge_module):
    """读不到 state → `ok=None`（**未确认**），既不算成功也不算失败。"""
    bridge, _node = _make_bridge(bridge_module)          # 一帧状态都没收到

    result = bridge.move("trunk", [10.0, 0, 0, 0, 0, 0], tol=1.0, timeout=0.4, retries=0)

    assert result["ok"] is None
    assert "未经确认" in result["reason"]


def test_bridge_subscribes_to_arm_state_in_degrees(bridge_module):
    """手臂状态进来是**弧度**，通道里立刻换成度 —— 之后协议、日志、容差全是度。"""
    from wheeled_robot_msgs.msg import ArmState

    bridge, node = _make_bridge(bridge_module)
    bridge.subscribe_states()
    arm_state_cb = next(cb for topic, cb in node.subscriptions
                        if topic == bridge_module.ARM_STATE_TOPIC)

    arm_state_cb(ArmState(left=[3.141592653589793 / 2] + [0.0] * 6))

    assert bridge.arms["left"][0] == pytest.approx(90.0)


def test_bridge_handle_rejects_unknown_ops(bridge_module):
    """不认识的请求要**明确报错**，不能静默当成功（协议层也守同一条纪律）。"""
    bridge, _node = _make_bridge(bridge_module)

    assert bridge_module.handle(bridge, {"op": "没这个"})["ok"] is False
    assert bridge_module.handle(bridge, {"op": "move", "kind": "trunk",
                                        "target": [0, 0]})["ok"] is False
    assert bridge_module.handle(bridge, {"op": "ping"})["ok"] is True


def test_bridge_emit_uses_the_protocol_prefix(bridge_module, capsys):
    """协议行带前缀：stdout 上混进别的输出也不会被当成回复。"""
    bridge_module.emit({"ok": True, "id": 7})

    out = capsys.readouterr().out
    assert out.startswith(bridge_module.PROTOCOL_PREFIX + " ")
    assert json.loads(out[len(bridge_module.PROTOCOL_PREFIX):].strip()) == {"ok": True, "id": 7}


def test_bridge_topics_and_units_match_core_motion(bridge_module):
    """契约：通道里写死的话题 / 类型 / 单位换算，必须与 `core_motion` 一致。

    两处漂了会变成最难查的形态 —— 界面说"已下发并确认"、实际发到了没人听的话题上。
    这条同时钉住"勾兑进协议的度与消息上的弧度"这一处换算。
    """
    from data_collector import core_motion

    prewarm = dict(bridge_module.PREWARM)
    assert core_motion.TRUNK_TARGET_TOPIC in prewarm
    assert core_motion.TRUNK_MODE_TOPIC in prewarm
    assert core_motion.TRUNK_CONNECT_TOPIC in prewarm
    assert core_motion.HAND_POSTURE_TOPIC in prewarm
    for side, topic in core_motion.ARM_MOVE_TOPICS.items():
        assert prewarm[topic] == core_motion.ARM_MOVE_MSG
        assert prewarm[f"/arm_control/{side}_arm_follow_position_set"]
    assert prewarm[core_motion.TRUNK_TARGET_TOPIC] == core_motion.TRUNK_TARGET_MSG
    assert prewarm[core_motion.TRUNK_MODE_TOPIC] == core_motion.TRUNK_MODE_MSG
    assert bridge_module.TRUNK_MODE_NON_REALTIME == core_motion.TRUNK_MODE_NON_REALTIME

    sample = [12.5, -7.25, 0.0, 90.0, 180.0, -180.0]
    assert bridge_module.deg_to_radians(sample) == pytest.approx(
        core_motion.deg_to_radians(sample))


class _AmbiguousArray:
    """模拟现场那台机上的数组字段：**布尔判断会抛异常**。

    现场 2026-09-29 的真实故障就是它：`scripts/ros_command_bridge.py` 里写了
    `list(getattr(msg, "joint_position", []) or [])`，`or` 去问"这个数组真不真" →
    `ValueError: The truth value of an array with more than one element is ambiguous.`
    → 异常从 `spin_once` 冒出来、没人接 → **通道进程退出码 1** → 之后每一次运动都静默
    掉回"单次发布"的兜底路径（右臂那条 ArmMove 就是这样丢的）。

    这个替身只做一件事：让 `bool()` 抛同样的错，其它（迭代、长度）都正常。
    """

    def __init__(self, values):
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __bool__(self):
        raise ValueError("The truth value of an array with more than one element is "
                         "ambiguous. Use a.any() or a.all()")


def test_bridge_reads_state_fields_without_asking_their_truth(bridge_module):
    """state 的数组字段**不能用 `or` 读**（现场崩溃的根因），但内容要照常拿到。"""
    from wheeled_robot_msgs.msg import ArmState, TrunkState

    bridge, node = _make_bridge(bridge_module)
    bridge.subscribe_states()
    trunk_cb = next(cb for topic, cb in node.subscriptions
                    if topic == bridge_module.TRUNK_STATE_TOPIC)
    arm_cb = next(cb for topic, cb in node.subscriptions
                  if topic == bridge_module.ARM_STATE_TOPIC)

    # 构造"布尔含糊"的数组（不能先做 bool()，直接塞进消息）
    values = _AmbiguousArray([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    try:
        bool(values)
        raise AssertionError("替身没模拟出'布尔含糊'，这条用例就失去意义了")
    except ValueError:
        pass

    trunk_cb(TrunkState(joint_position=values))              # 不该抛
    arm_cb(ArmState(left=_AmbiguousArray([0.0] * 7),
                    right=_AmbiguousArray([1.5707963267948966] + [0.0] * 6)))

    assert bridge.trunk == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert bridge.arms["left"] == [0.0] * 7
    assert bridge.arms["right"][0] == pytest.approx(90.0)     # 弧度进来、度出去
    assert bridge.errors == 0


def test_bridge_records_but_survives_a_bad_state_frame(bridge_module):
    """读不出来只**记一笔**：判据会如实报"未确认"，通道不能因此死掉。"""
    from wheeled_robot_msgs.msg import TrunkState

    bridge, node = _make_bridge(bridge_module)
    bridge.subscribe_states()
    trunk_cb = next(cb for topic, cb in node.subscriptions
                    if topic == bridge_module.TRUNK_STATE_TOPIC)

    trunk_cb(TrunkState(joint_position=None))                # 没数据
    trunk_cb(TrunkState(joint_position=object()))            # 压根不是可迭代的
    trunk_cb(TrunkState(joint_position=[1.0, 2.0]))          # 轴数不对
    missing = TrunkState()
    del missing.joint_position                               # 连字段都没有（老版本消息）
    trunk_cb(missing)

    assert bridge.trunk is None                              # 不编造值
    assert bridge.trunk_msgs == 4                            # 但帧数照记
    assert bridge.arms["left"] is None


def test_bridge_spin_swallows_callback_errors(bridge_module):
    """**回调抛异常不能带走通道进程** —— 现场就是死在这里。

    这条比上一条更靠底层：即便以后又有人写出会抛的回调，`spin` 也必须活着回来。
    """
    bridge, _node = _make_bridge(bridge_module)
    calls = {"n": 0}

    def boom(node_obj, timeout_sec=0.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("回调里炸了")

    import rclpy
    original = rclpy.spin_once
    rclpy.spin_once = boom                                  # 替身模块是共享对象，直接换
    try:
        bridge.spin(0.2)
    finally:
        rclpy.spin_once = original

    assert calls["n"] >= 2                                  # 挨了一下之后还在继续泵
    assert bridge.errors == 1                               # 但记了一笔


def test_bridge_move_batch_publishes_both_before_waiting(bridge_module):
    """批量：**两条都先发出去**，再一起等到位（左右臂的镜像动作要同时开始）。

    官方手柄示例（`arm_proxy.cpp` 的 `sendConfigMotion`）就是这个顺序：先发左右两条
    `ArmMove`，再一起等 `arm_move_over_`。逐条发会让镜像动作变成"先左后右"。
    """
    bridge, node = _bridge_module_batch(bridge_module)
    bridge.arms["left"] = [10.0] * 7            # 两侧都已经在目标上（只看"发了没有、顺序对不对"）
    bridge.arms["right"] = [-10.0] * 7

    result = bridge.move_batch([{"kind": "arm", "side": "left", "target": [10.0] * 7},
                                {"kind": "arm", "side": "right", "target": [-10.0] * 7}],
                               timeout=1.0, retries=0)

    assert result["ok"] is True
    assert set(result["items"]) == {"arm:left", "arm:right"}
    published = [topic for topic, _message in node.log]
    moves = [topic for topic in published if topic.endswith("_arm_move")]
    # 两条目标都发出去了（每条按 `DEFAULT_REPEAT` 重发），而且都只发了**一轮** ——
    # 说明"先都发出去、再一起等"（若逐条等到位，第二条会在第一条到位之后才出现）
    assert set(moves) == {"/arm_control/left_arm_move", "/arm_control/right_arm_move"}
    assert len(moves) == 2 * bridge_module.DEFAULT_REPEAT
    assert [item["attempts"] for item in result["items"].values()] == [1, 1]


def test_bridge_move_batch_reports_only_the_side_that_did_not_arrive(bridge_module):
    """批量里某一侧没到位 → **只点那一侧**，另一侧照样报到位（现场"只有一只手动了"）。"""
    bridge, _node = _bridge_module_batch(bridge_module)
    bridge.arms["left"] = [10.0] * 7            # 左臂已经到位
    bridge.arms["right"] = [0.0] * 7            # 右臂一动不动

    result = bridge.move_batch([{"kind": "arm", "side": "left", "target": [10.0] * 7},
                                {"kind": "arm", "side": "right", "target": [-10.0] * 7}],
                               timeout=1.0, retries=0, stall_window=0.05)

    assert result["ok"] is False                 # 有一条没到位 → 整体不算成功
    assert result["items"]["arm:left"]["ok"] is True
    assert result["items"]["arm:right"]["ok"] is False
    assert "右臂" in result["reason"] and "左臂" not in result["reason"]
    assert result["reached_text"].startswith("左臂")


def test_bridge_move_batch_turns_follow_off_once_per_side(bridge_module):
    """批量里的前置条件：**每侧各退一次** follow / servoJ，且都在 ArmMove 之前。"""
    bridge, node = _bridge_module_batch(bridge_module)
    bridge.arms["left"] = [0.0] * 7
    bridge.arms["right"] = [0.0] * 7

    bridge.move_batch([{"kind": "arm", "side": "left", "target": [0.0] * 7},
                       {"kind": "arm", "side": "right", "target": [0.0] * 7}],
                      timeout=1.0, retries=0)

    published = [topic for topic, _message in node.log]
    first_move = min(index for index, topic in enumerate(published)
                     if topic.endswith("_arm_move"))
    for side in ("left", "right"):
        for suffix in ("follow_position_set", "servoj_set"):
            topic = f"/arm_control/{side}_arm_{suffix}"
            assert topic in published, topic
            assert published.index(topic) < first_move, topic


def _bridge_module_batch(bridge_module):
    """批量用例共用的通道（节点带全局发布日志）。"""
    return _make_bridge(bridge_module)


# ---------------------------------------------------------------- 躯干链路体检脚本
# `scripts/diagnose_trunk.py` 的**纯函数**部分在这里钉住（rclpy 那半在沙箱里建不了节点 ——
# `/dev/shm` 只有 10MB，实测 `Node()` 会挂住；但"动没动的判定""控制器算不算可动"
# 与"哪一段断了的结论"必须能单测 —— 这三件事正是现场要的答案）。

def _diag():
    import importlib

    return importlib.import_module("scripts.diagnose_trunk")


def test_diagnose_trunk_imports_without_rclpy():
    """沙箱里没有 rclpy（或建不了节点），本模块仍然要能 import —— 纯函数是给单测用的。"""
    diag = _diag()

    assert diag.HAVE_ROS is False
    assert callable(diag.verdict_table) and callable(diag.classify)
    # CLI 入口在缺 rclpy 时要给出"去机器人机上用 /usr/bin/python3 跑"的明确提示，
    # 而不是抛个 ImportError 的栈 —— 现场看到栈只会以为脚本坏了
    assert diag.main([]) == 2


def test_diagnose_trunk_nudge_stays_in_range():
    """实测幅度是**按当前姿态叠加**的，并且要夹进各关节量程（不许越界）。"""
    diag = _diag()

    # 从 0 起步：按 p2-p3 那一族的比例 1 : 1.8 : 0.8
    assert [round(v, 2) for v in diag.nudge([0.0] * 6, 6.0)] == [6.0, 10.8, 4.8, 0.0, 0.0, 0.0]
    # 从 KneeBend_2 的姿态叠加
    assert [round(v, 2) for v in diag.nudge([60.0, 108.0, 48.0, 0.0, 0.0, 0.0], 6.0)] == \
        [66.0, 118.8, 52.8, 0.0, 0.0, 0.0]
    # 越界的被夹住（H1 ±85 / H2 ±150 / H3 ±95 / H6 ±30）
    assert diag.clamp_trunk([100.0, 200.0, -200.0, 0.0, 0.0, 99.0]) == \
        [85.0, 150.0, -95.0, 0.0, 0.0, 30.0]
    # 叠加之后再夹：贴上限时不会超出去
    assert diag.nudge([84.0, 148.0, 94.0, 0.0, 0.0, 0.0], 6.0)[0] == 85.0


def test_diagnose_trunk_moved_threshold():
    """动没动按**最大变化量**判，阈值 2°：3° 算动、1° 算没动。"""
    diag = _diag()

    assert diag.moved_deg([0.0] * 6, [0.0, 0.0, -3.0, 0.0, 0.0, 0.0]) == 3.0
    assert diag.classify([0.0] * 6, [3.0, 0.0, 0.0, 0.0, 0.0, 0.0]) == (True, 3.0)
    assert diag.classify([0.0] * 6, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]) == (False, 1.0)


def test_diagnose_trunk_controller_report():
    """A 段的判据：**有没有名字带 trunk 的控制器处于 active**。

    这一段是"最上游"的断点：它没通的话，后面换什么模式都不会动 —— 所以判定必须只看
    `state == "active"`，不能被"名字出现过"或"是 inactive"蒙过去。
    """
    diag = _diag()

    ok, lines = diag.controller_report(
        [("joint_state_broadcaster", "active"), ("trunk_controller", "active"),
         ("position_controller", "inactive")])
    assert ok is True
    assert any(line.strip().startswith("OK") and "trunk_controller" in line for line in lines)

    # trunk 的都在 inactive → 不可动（这正是要报出来的那种断点）
    ok, lines = diag.controller_report(
        [("joint_state_broadcaster", "active"), ("trunk_controller", "inactive")])
    assert ok is False
    assert any("没有任何名字带 trunk 的控制器处于 active" in line for line in lines)

    # 一个都没问到 → 也要说清是"没问到"，不是"问了都没有"
    ok, lines = diag.controller_report([])
    assert ok is False
    assert any("一个控制器都没问到" in line for line in lines)


def test_diagnose_trunk_verdict_picks_the_right_fault():
    """结论表要把"哪一段断了 + 修哪里"说清楚 —— 六个分支各钉一条。

    这些分支的修法完全不同，所以判定本身必须确定（不能靠人读日志猜）：
    * A 没动 → 关节/控制器/Gazebo 本身不通，先修它（后面换模式都没用）
    * B 没动、C 动了 → **`layout.data_offset` 就是缺的那一步**（本轮定位到的根因）
    * B 动了、C 没动 → **反了**，说明节点把 offset 当"数据起始下标"用，改法完全不同
    * B 没动、D 动了 → 非实时单条不动、实时+插值能动，改用实时路径
    * 该动的都动了 → 链路没问题，回去查上位机下发的内容
    * 什么都没测 → 提示加 --move
    """
    diag = _diag()

    only_a = diag.verdict_table({"A": (False, 0.05)})
    assert "直连控制器都不动" in only_a
    assert "trunk_controller" in only_a

    need_offset = diag.verdict_table({"A": (True, 5.2), "B": (False, 0.1), "C": (True, 4.8)})
    assert "layout.data_offset" in need_offset
    assert "v6.3.3" in need_offset                     # 指到已经改好的那两处

    reversed_offset = diag.verdict_table({"A": (True, 5.2), "B": (True, 4.9), "C": (False, 0.1)})
    assert "反了" in reversed_offset
    assert "占位元素" in reversed_offset                # 这条的改法与上一条完全不同

    need_realtime = diag.verdict_table({"A": (True, 5.2), "B": (False, 0.1), "D": (True, 4.9)})
    assert "非实时单条不动" in need_realtime
    assert "run_motion_streams" in need_realtime

    all_moved = diag.verdict_table({"A": (True, 5.2), "B": (True, 4.9), "C": (True, 4.7),
                                    "D": (True, 4.8)})
    assert "链路本身没问题" in all_moved
    assert "speed_rate" in all_moved                   # 兜底提一句还没试过的口子

    assert "只体检、未实测" in diag.verdict_table({})


def test_diagnose_trunk_honours_the_layout_offset_switch():
    """B / C 两段之间**只差 `layout.data_offset`** —— 这条对照是整个脚本的核心。

    所以那两个常量必须真的是 0 和 1（有人把 C 段也改成 0，对照就废了，
    而且症状极隐蔽：两段结果一样，看起来像"offset 不是原因"）。
    """
    diag = _diag()

    assert diag.TRUNK_LAYOUT_OFFSET == 1
    assert "data_offset=0" in diag.TEST_TITLES["B"]
    assert "data_offset=1" in diag.TEST_TITLES["C"]
    assert set(diag.TESTS) == {"A", "B", "C", "D", "E"}
