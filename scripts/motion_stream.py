#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""姿势控制 · 实时流：把某个姿势**平滑**地走到位（躯干 100Hz / 手臂 follow_position 125Hz）。

⚠️ **当前未启用（v6.1.1）**：现场 2026-09-28 要求「手臂和躯干的控制改回非实时模式」，
所以「姿势控制」页现在全部走**非实时（点位）**：躯干一条 `Float32MultiArray`、手臂一条
`ArmMove`，都由控制节点内部插值 —— 那条路是 `Launcher._publish_topics`（一次性通道）。
本脚本与 `Launcher.run_motion_streams`（以及 `MotionRunner` 的 `streams` 分支）**完整保留**，
单测也都在：`ros2 topic pub --once` 一条要 0.3~1s，**发不出 100Hz**，将来若再切回实时就靠它。
**删之前先问现场**（v6.0.0 → v6.1.1 之间来回切过一次）。

为什么要有这个脚本（而不是像别处那样发一条 `ros2 topic pub --once`）
----------------------------------------------------------------
`ros2 topic pub --once` 的一条命令要 0.3~1s（起进程 + DDS 发现 + `-w 1` 等匹配），
**物理上发不出 100Hz**；而实时模式下目标位置必须由上位机自己插值逐条发 ——
这是 SDK 文档原文（`/trunk_control/joint_target_position` 那条）：
"非实时模式：设置目标位置后，关节会内部插值…**实时模式：…需上位机自行做插值处理**"。
所以连续运动交给本脚本（一条常驻几秒的 rclpy 进程），一次性指令
（急停 / connect / hand_posture / 预设的 ArmMove）仍然走原来那条 CLI 通道。

用法
----
    # 躯干：connect → motor_mode 1 → connect，再从当前角度缓动走到目标（6 个**度**）
    python3 scripts/motion_stream.py trunk --values 30,54,24,0,0,0 --duration 2.5 --prepare

    # 手臂：follow_position_set=true → 125Hz 流（7 个**弧度**）→ set=false
    python3 scripts/motion_stream.py arm --side left --values 1.086,1.192,-0.394,0.389,0.551,0.302,0

单位
----
**脚本不做任何单位换算**：`--values` 原样写进话题字段。躯干话题是"度"、手臂 follow_position 是"弧度"，
两者都由上位机 `core_motion.stream_job_topic_values()` 保证 —— 换算只在一处发生。

安全
----
* **读不到当前角度就不动**（退出码 3）：没有当前值就没法插值，硬发一条目标等于全速冲刺。
* 手臂的 `follow_position_set=false` 在 `finally` 里发 —— 少了它，`*_arm_move`（预设走的那条）会被忽略，
  表现就是"预设点了没反应"。Ctrl+C / SIGTERM 也走这个 `finally`。
* 收到停止信号时**中止在当前角度**（不再往目标走），并如实打印。

输出约定
--------
只报现场要看的：要做什么 → 阶段（准备 / 从哪到哪 / 百分比）→ 结果（走到哪 / 用时）。
逐周期的位置只在 `--verbose` 下打印。
"""

import argparse
import math
import signal
import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, Byte, Float32MultiArray, Float64MultiArray
    from wheeled_robot_msgs.msg import ArmState, TrunkState
except ImportError as exc:  # pragma: no cover - 只在没 source 环境时走到
    sys.stderr.write(
        "ERROR: 无法导入 rclpy / wheeled_robot_msgs（%s）\n"
        "       请先 source <工作空间>/install/setup.bash；若机器上有多个 python，"
        "请用 /usr/bin/python3 执行。\n" % exc
    )
    sys.exit(2)

# ============ 协议常量（每一处都有原文出处，别凭手感改）============
TRUNK_AXIS_COUNT = 6
ARM_AXIS_COUNT = 7
#: 躯干 6 个值的量程（SDK 文档原文：-85~85 脚踝 / -150~150 膝 / -95~95 大腿 / ±90 腰 / ±90 颈转 / ±30 颈抬）
TRUNK_LIMITS = ((-85.0, 85.0), (-150.0, 150.0), (-95.0, 95.0),
                (-90.0, 90.0), (-90.0, 90.0), (-30.0, 30.0))
#: 实时流的频率：躯干 100Hz（与 `p2-p3.py` 一致）、手臂 125Hz（官方 `arm_proxy.cpp` 是 8ms 一条）
DEFAULT_TRUNK_RATE = 100
DEFAULT_ARM_RATE = 125
#: 一条流的默认时长（秒）
DEFAULT_TRUNK_SEC = 2.5
DEFAULT_ARM_SEC = 1.6
#: 躯干到位后按目标保持多久（秒）
DEFAULT_HOLD = 0.8
#: 等第一帧当前角度的上限（秒）：超过就认"读不到"，**不动**。
#:
#: 为什么是 6s 而不是 2s（现场实测）：本脚本是**短驻进程**，每次都要重新做一遍 DDS 发现；
#: 而机器人侧图里节点很多（gazebo + 各控制器 + 语音/手柄/网页事件节点…），本机实测
#: 一次 `rclpy.spin_once` 要几百毫秒，2 秒里只够泵几次。长驻的 `robot_monitor` 没这个问题
#: —— 所以会出现"界面上有角度、脚本却说读不到"。宁可多等 4 秒，也不能误判成"没数据"。
READ_CURRENT_TIMEOUT = 6.0
#: 准备阶段每步之间留的间隔（秒）。p2-p3.py 是 2s，那是人手敲命令的余量；
#: 这里由程序连续发，只需留出让 DDS 匹配的时间
PREPARE_GAP = 0.3
#: 发布频率的上限（防呆）：超过 300Hz 没意义，还可能把控制器喂爆
MAX_RATE = 300
#: 第一次往某个话题发之前，最多等多久"匹配到订阅者"（秒）。
#:
#: **为什么必须等**：QoS 是 VOLATILE —— 订阅者还没匹配上时发出去的消息会被直接丢掉，
#: 等于没发。本脚本是短驻进程，每次都要重新发现一遍，所以"创建 publisher 就立刻 publish"
#: 很可能把 `connect` / `motor_mode` 丢掉，表现就是"预设点了没反应/成功率很低"。
#: 只等第一次（每个 publisher 一次），后面 100Hz 的流不再等。
SUBSCRIBER_WAIT = 1.5
#: 退出码
EXIT_OK, EXIT_USAGE, EXIT_NO_DATA = 0, 2, 3

TRUNK_TARGET_TOPIC = "/trunk_control/joint_target_position"
TRUNK_MODE_TOPIC = "/trunk_control/motor_mode"
TRUNK_CONNECT_TOPIC = "/trunk_control/connect"
TRUNK_STATE_TOPIC = "/trunk_control/trunk_state"
ARM_STATE_TOPIC = "/arm_control/arm_state"
TRUNK_MODE_REALTIME = 1
#: 手臂实时流用 **follow_position**（现场指定），不是 servoj。
#: 两者形状一样（`Bool` 开关 + `Float64MultiArray` 的 7 个**弧度**绝对目标），
#: 依据是官方手柄示例 `sample/src/joy_event_receiver/src/arm_proxy.cpp`：
#: M1/M3 键走 `*_arm_follow_position_set/_update`，M4 键才走 `*_arm_servoj_*`。
ARM_FOLLOW_SET_SUFFIX = "_arm_follow_position_set"
ARM_FOLLOW_UPDATE_SUFFIX = "_arm_follow_position_update"
#: 进度只在 0/25/50/75/100 各报一行（100Hz 每周期一行会把日志刷没）
PROGRESS_STEPS = (0, 25, 50, 75, 100)
#: 停止标志：主线程在流的过程中被 SIGTERM/SIGINT 打断时置真
STOP = {"requested": False}


def say(line: str) -> None:
    """往 stdout 打一行（行缓冲关掉，界面要实时看到）。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def ease_in_out_cubic(ratio: float) -> float:
    """缓动：两头慢、中间快（与 `p2-p3.py` 的 `easeInOutCubic` 同一条公式）。

    为什么不用匀速：机构启停时有加速度突变会抖、也更容易超调；缓动让首尾速度趋近 0。
    """
    ratio = min(max(float(ratio), 0.0), 1.0)
    if ratio < 0.5:
        return 4.0 * ratio * ratio * ratio
    return 1.0 - pow(-2.0 * ratio + 2.0, 3) / 2.0


def interpolate(start, target, ratio: float):
    """按比例在起点与目标之间取一组值（等长序列）。"""
    return [float(a) + (float(b) - float(a)) * float(ratio) for a, b in zip(start, target)]


def parse_values(text: str, expected: int):
    """解析 `--values`：逗号分隔的浮点数；个数不对就返回一句人能看懂的错误。"""
    items = [item.strip() for item in (text or "").split(",") if item.strip()]
    if not items:
        return None, "没有收到任何数值"
    values = []
    for index, item in enumerate(items, start=1):
        try:
            values.append(float(item))
        except ValueError:
            return None, f"第 {index} 个值不是数字：{item!r}"
    if len(values) != expected:
        return None, f"需要 {expected} 个值，收到 {len(values)} 个"
    return values, ""


def check_trunk_limits(values):
    """躯干逐轴量程检查（越界只拦下，**不静默钳位**）。"""
    problems = []
    for index, ((low, high), value) in enumerate(zip(TRUNK_LIMITS, values), start=1):
        if value < low or value > high:
            problems.append(f"H{index} {value:g} 超出 {low:g}~{high:g}")
    return problems


def stream_line(index: int, total: int, values) -> str:
    """逐周期日志（只在 `--verbose` 下打）。"""
    return (f"    [{index}/{total}] " + "、".join(f"{value:.3f}" for value in values))


class RosStreamer:
    """所有与 rclpy 打交道的动作都收在这里 —— 用例换成记录桩就能不开 ROS 地测完整流程。"""

    def __init__(self):
        rclpy.init()
        self.node = _StreamerNode()
        self._trunk_pub = None
        self._simple_pubs = {}
        self._arm_pubs = {}
        self._trunk_values = None
        self._arm_values = {"left": None, "right": None}
        #: 收到过几条状态消息（**只数到达**，不管字段能不能用）—— 用来把
        #: "一条都没来（话题没在发布）"与"来了但该侧没连上"分开报，两者处理方式完全不同。
        self.state_msgs = {"trunk": 0, "arm": 0}
        #: 该侧上电 / 连接事实（`arm_state` 里带，用于把原因说得更具体）
        self.arm_connected = {"left": None, "right": None}

    # ---------- 订阅 ----------
    def start_listening(self) -> None:
        """订阅状态话题（读当前角度用）。QoS 用 BEST_EFFORT：与任何发布端都兼容。"""
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.node.create_subscription(TrunkState, TRUNK_STATE_TOPIC,
                                      self._on_trunk_state, qos)
        self.node.create_subscription(ArmState, ARM_STATE_TOPIC,
                                      self._on_arm_state, qos)

    def _on_trunk_state(self, msg) -> None:
        self.state_msgs["trunk"] += 1
        position = list(getattr(msg, "joint_position", []) or [])
        if len(position) == TRUNK_AXIS_COUNT:
            self._trunk_values = [float(item) for item in position]

    def _on_arm_state(self, msg) -> None:
        self.state_msgs["arm"] += 1
        for side, attribute in (("left", "left_arm_pos"), ("right", "right_arm_pos")):
            self.arm_connected[side] = bool(getattr(msg, f"{side}_arm_connected", False))
            holder = getattr(msg, attribute, None)
            position = list(getattr(holder, "position", []) or []) if holder else []
            if len(position) == ARM_AXIS_COUNT:
                self._arm_values[side] = [float(item) for item in position]

    def spin(self, seconds: float) -> None:
        """在最多 ``seconds`` 秒内把消息泵进来（`spin_once` 带超时，不会忙等）。"""
        deadline = time.monotonic() + max(seconds, 0.0)
        while time.monotonic() < deadline:
            rclpy.spin_once(self.node, timeout_sec=min(0.05, max(deadline - time.monotonic(), 0.0)))

    def wait_trunk(self, timeout: float = READ_CURRENT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while self._trunk_values is None and time.monotonic() < deadline:
            self.spin(0.05)
        return self._trunk_values

    def wait_arm(self, side: str, timeout: float = READ_CURRENT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while self._arm_values.get(side) is None and time.monotonic() < deadline:
            self.spin(0.05)
        return self._arm_values.get(side)

    # ---------- 读不到时把原因说清 ----------
    def trunk_missing_reason(self) -> str:
        """躯干读不到当前角度的具体原因（分"一条都没来"与"来了但字段不对"）。"""
        if self.state_msgs["trunk"] == 0:
            return (f"      一条 {TRUNK_STATE_TOPIC} 都没收到 —— 话题没在发布。"
                    "主程序没在跑 / 躯干控制节点没起来。")
        return (f"      收到了 {self.state_msgs['trunk']} 条，但 joint_position 不是 6 个值 —— "
                "消息版本可能不匹配。")

    def arm_missing_reason(self, side: str) -> str:
        """该侧手臂读不到当前角度的具体原因（分"一条都没来"与"该侧没连上"）。"""
        if self.state_msgs["arm"] == 0:
            return (f"      一条 {ARM_STATE_TOPIC} 都没收到 —— 话题没在发布。"
                    "主程序没在跑 / ArmControlNode 没起来（仿真下常见）。")
        connected = self.arm_connected.get(side)
        if connected is False:
            return f"      收到的状态里 {side}_arm_connected = False —— 该侧手臂没连上。"
        return (f"      收到了 {self.state_msgs['arm']} 条，但 {side}_arm_pos.position 不是 7 个值 —— "
                "该侧可能刚上电、还没开始回报。")

    # ---------- 发布 ----------
    def _ensure_subscriber(self, publisher) -> None:
        """等这个话题匹配到订阅者（只在该 publisher 的第一次发布前等）。

        没有订阅者时不等满 `SUBSCRIBER_WAIT` 就继续 —— 那种情况本来也发不出去，
        多等没有意义；脚本的"到位判据"会如实报未确认。
        """
        deadline = time.monotonic() + SUBSCRIBER_WAIT
        while time.monotonic() < deadline:
            try:
                if publisher.get_subscription_count() > 0:
                    return
            except AttributeError:              # 替身 / 老版本 rclpy：没有这个方法就不等
                return
            self.spin(0.05)

    def publish_trunk(self, values) -> None:
        if self._trunk_pub is None:
            self._trunk_pub = self.node.create_publisher(
                Float32MultiArray, TRUNK_TARGET_TOPIC, 10)
            self._ensure_subscriber(self._trunk_pub)
        message = Float32MultiArray()
        message.data = [float(item) for item in values]
        self._trunk_pub.publish(message)

    def publish_bool(self, topic: str, value: bool) -> None:
        publisher = self._simple_pubs.get(topic)
        if publisher is None:
            publisher = self.node.create_publisher(Bool, topic, 10)
            self._simple_pubs[topic] = publisher
            self._ensure_subscriber(publisher)
        message = Bool()
        message.data = bool(value)
        publisher.publish(message)

    def publish_byte(self, topic: str, value: int) -> None:
        publisher = self._simple_pubs.get(topic)
        if publisher is None:
            publisher = self.node.create_publisher(Byte, topic, 10)
            self._simple_pubs[topic] = publisher
            self._ensure_subscriber(publisher)
        message = Byte()
        # `std_msgs/msg/Byte` 的 `data` 在 rclpy 里是 **bytes（长度必须正好 1）**，不是 int：
        # 直接赋 int 会抛 `AssertionError: The 'data' field must be of type 'bytes' ...`
        # （现场实测，整条躯干流就是死在这里）。所以先看字段实际是什么类型再决定怎么填。
        if isinstance(getattr(message, "data", None), (bytes, bytearray)):
            message.data = bytes([int(value) & 0xFF])
        else:
            message.data = int(value)
        publisher.publish(message)

    def publish_follow_position(self, side: str, values) -> None:
        """发一条 follow_position 目标（7 个**弧度**绝对角）。

        数组里是"接下来该在哪"，不是增量 —— 官方示例也是整份 `position[7]` 原样发。
        """
        publisher = self._arm_pubs.get(side)
        if publisher is None:
            publisher = self.node.create_publisher(
                Float64MultiArray, f"/arm_control/{side}{ARM_FOLLOW_UPDATE_SUFFIX}", 10)
            self._arm_pubs[side] = publisher
            self._ensure_subscriber(publisher)
        message = Float64MultiArray()
        message.data = [float(item) for item in values]
        publisher.publish(message)

    def shutdown(self) -> None:
        try:
            self.node.destroy_node()
        except Exception:                      # noqa: BLE001 —— 退出路径上不值得再抛
            pass
        try:
            rclpy.shutdown()
        except Exception:                      # noqa: BLE001
            pass


class _StreamerNode(Node):
    """只为了给发布/订阅一个节点名。"""

    def __init__(self):
        super().__init__("posture_motion_stream")


# ============ 两件事：准备 & 流 ============
def prepare_trunk(streamer, verbose: bool = False) -> list:
    """把躯干切到**实时模式**：`connect` → `motor_mode 1` → 再 `connect`。

    顺序不能换：`motor_mode` 改完**必须重发 connect 才生效**（SDK 文档原文）。
    返回日志行（界面会原样显示）。
    """
    lines = []
    streamer.publish_bool(TRUNK_CONNECT_TOPIC, True)
    streamer.spin(PREPARE_GAP)
    streamer.publish_byte(TRUNK_MODE_TOPIC, TRUNK_MODE_REALTIME)
    streamer.spin(PREPARE_GAP)
    streamer.publish_bool(TRUNK_CONNECT_TOPIC, True)
    streamer.spin(PREPARE_GAP)
    lines.append(f"    已把躯干切到实时模式（connect → motor_mode {TRUNK_MODE_REALTIME} → connect）")
    if verbose:
        lines.append("    说明：实时模式的**目标由本机插值**逐条下发，控制节点不再内部插值")
    return lines


def run_stream(streamer, publish, start, target, duration: float, rate: int,
               verbose: bool = False) -> list:
    """从 ``start`` 缓动到 ``target``：按时长与频率逐条下发，返回日志行。

    用绝对时间推进（而不是"每次 sleep 1/rate"）—— 后者会把发布耗时的误差累起来，
    实际频率越跑越低、动作越走越慢。
    """
    lines = []
    total = max(int(round(duration * rate)), 1)
    period = 1.0 / max(rate, 1)
    reported = set()
    started = time.monotonic()
    for index in range(total + 1):
        if STOP["requested"]:
            lines.append("    收到停止信号：**中止在当前角度**（不再往目标走）")
            break
        ratio = index / total
        publish(interpolate(start, target, ease_in_out_cubic(ratio)))
        percent = int(round(ratio * 100))
        if percent in PROGRESS_STEPS and percent not in reported:
            reported.add(percent)
            lines.append(f"    已走 {percent}%")
        if verbose:
            lines.append(stream_line(index + 1, total + 1,
                                     interpolate(start, target, ease_in_out_cubic(ratio))))
        deadline = started + (index + 1) * period
        remaining = deadline - time.monotonic()
        if remaining > 0:
            # 发布间隙也泵一下订阅，保证状态是最新的
            streamer.spin(remaining)
    return lines


def hold_at(streamer, publish, target, seconds: float, rate: int) -> list:
    """到位后按目标保持一段（`p2-p3.py` 同款；控制器需要持续的期望值）。"""
    if seconds <= 0:
        return []
    lines = [f"    按目标保持 {seconds:g}s"]
    total = max(int(round(seconds * rate)), 1)
    period = 1.0 / max(rate, 1)
    started = time.monotonic()
    for index in range(total):
        if STOP["requested"]:
            break
        publish(target)
        remaining = started + (index + 1) * period - time.monotonic()
        if remaining > 0:
            streamer.spin(remaining)
    return lines


# ============ 躯干 / 手臂两条流程 ============
def run_trunk(streamer, args, values) -> int:
    limits = check_trunk_limits(values)
    if limits and not args.force:
        say("ERROR: 目标值超出量程（**没有下发**；确认无误可加 --force）："
            + "、".join(limits))
        return EXIT_USAGE
    if args.prepare:
        for line in prepare_trunk(streamer, args.verbose):
            say(line)
    streamer.start_listening()
    say(f"    正在等第一帧躯干角度（最多 {READ_CURRENT_TIMEOUT:g}s；本脚本是短驻进程，"
        "每次都要重新做一遍 DDS 发现）…")
    current = streamer.wait_trunk(READ_CURRENT_TIMEOUT)
    if current is None:
        say(f"ERROR: 读不到躯干当前角度（{TRUNK_STATE_TOPIC}）—— "
            "没有当前值就无法插值，**这一条没有下发**。")
        say(streamer.trunk_missing_reason())
        return EXIT_NO_DATA
    say("躯干实时流：" + "、".join(f"{item:.1f}" for item in current)
        + " → " + "、".join(f"{item:.1f}" for item in values)
        + f"（{args.duration:g}s / {args.rate}Hz）")
    started = time.monotonic()
    for line in run_stream(streamer, streamer.publish_trunk, current, values,
                           args.duration, args.rate, args.verbose):
        say(line)
    for line in hold_at(streamer, streamer.publish_trunk, values, args.hold, args.rate):
        say(line)
    elapsed = time.monotonic() - started
    if STOP["requested"]:
        say(f"已中止（用时 {elapsed:.1f}s）—— 躯干停在当前位置，后面不会再动")
        return EXIT_OK
    say(f"已到位（用时 {elapsed:.1f}s）")
    return EXIT_OK


def run_arm(streamer, args, values) -> int:
    side = args.side
    set_topic = f"/arm_control/{side}{ARM_FOLLOW_SET_SUFFIX}"
    streamer.start_listening()
    say(f"    正在等第一帧{side}手臂角度（最多 {READ_CURRENT_TIMEOUT:g}s；本脚本是短驻进程，"
        "每次都要重新做一遍 DDS 发现）…")
    current = streamer.wait_arm(side, READ_CURRENT_TIMEOUT)
    if current is None:
        say(f"ERROR: 读不到{side}当前角度（{ARM_STATE_TOPIC}.{side}_arm_pos）—— "
            "没有当前值就无法插值，**这一条没有下发**。")
        say(streamer.arm_missing_reason(side))
        return EXIT_NO_DATA
    say(f"{side} 手臂实时流（follow_position）："
        + "、".join(f"{math.degrees(item):.1f}" for item in current)
        + " → " + "、".join(f"{math.degrees(item):.1f}" for item in values)
        + f"（{args.duration:g}s / {args.rate}Hz）")
    started = time.monotonic()
    try:
        # `_set` 连发 3 次：只发一次可能没落上，而"没落上"的表现与"手臂不动"一模一样
        for _ in range(3):
            streamer.publish_bool(set_topic, True)
            streamer.spin(PREPARE_GAP)
        say("    已进入 follow_position 实时流模式（该侧）")
        for line in run_stream(streamer,
                               lambda values_: streamer.publish_follow_position(side, values_),
                               current, values, args.duration, args.rate, args.verbose):
            say(line)
    finally:
        # **必须**退出：留着 `follow_position_set=true` 会让 `*_arm_move`（预设走的那条）被忽略
        for _ in range(3):
            streamer.publish_bool(set_topic, False)
            streamer.spin(PREPARE_GAP)
        say("    已退出 follow_position 实时流模式（该侧）")
    elapsed = time.monotonic() - started
    if STOP["requested"]:
        say(f"已中止（用时 {elapsed:.1f}s）—— 该侧停在当前位置")
        return EXIT_OK
    say(f"已到位（用时 {elapsed:.1f}s）")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="姿势控制实时流：躯干 100Hz 插值 / 手臂 follow_position 125Hz")
    parser.add_argument("--verbose", action="store_true", help="逐周期打印位置（调参用）")
    sub = parser.add_subparsers(dest="kind", required=True)

    trunk = sub.add_parser("trunk", help="躯干实时流（6 个**度**）")
    trunk.add_argument("--values", required=True, help="逗号分隔的 6 个关节角（度）")
    trunk.add_argument("--duration", type=float, default=DEFAULT_TRUNK_SEC)
    trunk.add_argument("--hold", type=float, default=DEFAULT_HOLD)
    trunk.add_argument("--rate", type=int, default=DEFAULT_TRUNK_RATE)
    trunk.add_argument("--prepare", action="store_true",
                       help="先 connect → motor_mode 1 → connect（实时模式）")
    trunk.add_argument("--force", action="store_true", help="跳过量程检查（**危险**）")

    arm = sub.add_parser("arm", help="手臂 follow_position 实时流（7 个**弧度**）")
    arm.add_argument("--side", choices=("left", "right"), required=True)
    arm.add_argument("--values", required=True, help="逗号分隔的 7 个关节角（弧度）")
    arm.add_argument("--duration", type=float, default=DEFAULT_ARM_SEC)
    arm.add_argument("--rate", type=int, default=DEFAULT_ARM_RATE)
    arm.add_argument("--force", action="store_true", help="跳过量程检查（**危险**）")
    return parser


def _install_signal_handlers() -> None:
    def _stop(_signum, _frame):
        STOP["requested"] = True

    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, _stop)
            except (ValueError, OSError):      # pragma: no cover - 非主线程
                pass


def main(argv=None, streamer_factory=None) -> int:
    args = build_parser().parse_args(argv)
    expected = TRUNK_AXIS_COUNT if args.kind == "trunk" else ARM_AXIS_COUNT
    values, error = parse_values(args.values, expected)
    if values is None:
        say(f"ERROR: {error}")
        return EXIT_USAGE
    if not 1 <= args.rate <= MAX_RATE:
        say(f"ERROR: --rate 应在 1~{MAX_RATE}，收到 {args.rate}")
        return EXIT_USAGE
    if args.duration <= 0:
        say(f"ERROR: --duration 应为正数，收到 {args.duration}")
        return EXIT_USAGE

    _install_signal_handlers()
    factory = streamer_factory or RosStreamer
    streamer = factory()
    try:
        if args.kind == "trunk":
            return run_trunk(streamer, args, values)
        return run_arm(streamer, args, values)
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
