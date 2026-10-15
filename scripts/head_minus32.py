#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【采数】头部走到 -32°：读当前躯干角度 → 只把 H6 改成 -32° → 平滑到位。

⚠️ **本文件的时序逐字来自现场实测能用的脚本**（2026-10-15 现场提供并验证）：
`connect true` → 等 **2s** → `motor_mode 1` → 等 **2s** → `connect true` → 等 **2s**
→ 100Hz `easeInOutCubic` 插值 **5s** → 按目标保持 **3s**。

**别把准备阶段的 2s 改小。** `motor_mode` 改完会让控制节点重新连电机，
**重连期间发出去的目标会被静默丢弃** —— 表现是"命令发出去了、订阅者也匹配、
机构就是不动"，属于最难查的一类。`motion_stream.py` 用的是 0.3s（那是它自己的形态），
这两份脚本的时序**不能互相套用**。

与 `motion_stream.py` 的分工
--------------------------
| | `head_minus32.py`（本份） | `motion_stream.py` |
| --- | --- | --- |
| 用途 | **这一个**现场动作（采数前的定位） | 通用实时流 |
| 目标 | 写死在 `HEAD_TARGET_DEG` | `--values`（完整）/ `--set`（只改指定轴） |
| 准备间隔 | **2s × 3**（现场实测） | 0.3s × 3 |
| 起点 | 读到的当前角度 | 读到的当前角度 |

用法
----
    python3 scripts/head_minus32.py            # 无参数：目标与节奏都写在本文件里

输出约定
--------
只报现场要看的：准备 → 从哪到哪 → 进度（0/25/50/75/100%）→ 结果。
逐周期的位置只在 `--verbose` 下打印。

安全
----
* **读不到当前角度就不动**（退出码 3）：目标 = 当前角度 + 覆写 H6，没有当前值就
  构造不出目标 —— 拿 0 当基准会把 H1~H5 一起带向 0°（一次全身大动作）。
* ⚠️ **-32° 超出 SDK 文档量程**（文档写 H6 为 ±30°）—— 这是现场采数要的位姿，
  所以本脚本**不做量程检查**。通用路径（轴表 / 预设 / 手动输入）仍然拦。
* 实时模式下发后**不退出实时模式**（与现场脚本一致）。
* 收发消息都不会因为某个字段读不出来而抛异常带走进程（与通道脚本同一条纪律）。
"""

import argparse
import signal
import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, Byte, Float32MultiArray
    from wheeled_robot_msgs.msg import TrunkState
except ImportError as exc:                     # pragma: no cover - 只在没 source 环境时走到
    sys.stderr.write(
        "ERROR: 无法导入 rclpy / wheeled_robot_msgs（%s）\n"
        "       请先 source <工作空间>/install/setup.bash；若机器上有多个 python，"
        "请用 /usr/bin/python3 执行。\n" % exc
    )
    sys.exit(2)

# ============ 参数（与现场脚本逐字对齐；改之前先看文件头那两段）============
TRUNK_AXIS_COUNT = 6
#: 要动的轴：**H6（颈 · 抬头）**，0 基下标 5。
#: 顺序与 `trunk_state.joint_position` 一致：脚踝 / 膝 / 大腿 / 腰 / 颈转 / 颈抬。
HEAD_AXIS_INDEX = 5
#: 目标角度（度）。**超出文档量程**（H6 写的是 ±30°）—— 见文件头"安全"那节。
#: 与 `data_collector.core_motion.HEAD_DATA_TARGET_DEG` 必须一致（两处各一份：
#: 这个脚本是独立进程、不 import 包），由 `tests/test_scripts.py` 的契约用例钉住。
HEAD_TARGET_DEG = -32.0
#: 准备阶段每步之间等多久（秒）。**现场脚本是 2s，别改小**（见文件头第 2 段）。
PREPARE_GAP = 2.0
#: 运动时长（秒）/ 频率（Hz）/ 到位后保持（秒）——
#: 现场脚本的 `duration = 5.0` / `control_rate = 100` / `hold_time = 3.0`。
DURATION = 5.0
RATE = 100
HOLD = 3.0
#: 等第一帧躯干角度的上限（秒）。本脚本是短驻进程、每次都要重做一遍 DDS 发现；
#: 而准备阶段已经等了 6s（那段时间也在泵消息），所以这里只是兜底。
READ_CURRENT_TIMEOUT = 6.0
#: 进度只在 0/25/50/75/100 各报一行（100Hz 每周期一行会把日志刷没）
PROGRESS_STEPS = (0, 25, 50, 75, 100)
#: 退出码
EXIT_OK, EXIT_USAGE, EXIT_NO_DATA = 0, 2, 3

TRUNK_TARGET_TOPIC = "/trunk_control/joint_target_position"
TRUNK_MODE_TOPIC = "/trunk_control/motor_mode"
TRUNK_CONNECT_TOPIC = "/trunk_control/connect"
TRUNK_STATE_TOPIC = "/trunk_control/trunk_state"
TRUNK_MODE_REALTIME = 1

#: 停止标志：主线程被 SIGTERM/SIGINT 打断时置真
STOP = {"requested": False}


def say(line: str) -> None:
    """往 stdout 打一行（要实时看到，所以每行都 flush）。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def ease_in_out_cubic(ratio: float) -> float:
    """缓动：两头慢、中间快 —— 与现场脚本里的同名公式**逐字一致**。

    为什么不用匀速：机构启停时加速度突变会抖、也更容易超调；缓动让首尾速度趋近 0。
    """
    ratio = min(max(float(ratio), 0.0), 1.0)
    if ratio < 0.5:
        return 4.0 * ratio * ratio * ratio
    return 1.0 - pow(-2.0 * ratio + 2.0, 3) / 2.0


def interpolate(start, target, ratio: float):
    """按比例在起点与目标之间取一组值（等长序列）。"""
    return [float(a) + (float(b) - float(a)) * float(ratio)
            for a, b in zip(start, target)]


def head_target(current) -> list:
    """目标 = **当前角度**，只把 `HEAD_AXIS_INDEX` 那一轴换成目标值（纯函数，便于单测）。

    其余 5 轴原样保持当前角度 —— 所以这一趟里它们不会动。
    这就是现场那句「读取现在躯干的角度，写入 start_position，然后将 H6 改为 -32」。
    """
    target = [float(item) for item in current]
    target[HEAD_AXIS_INDEX] = float(HEAD_TARGET_DEG)
    return target


class _HeadNode(Node):
    """只为了给发布/订阅一个节点名。"""

    def __init__(self):
        super().__init__("head_minus32")


class RosStreamer:
    """所有与 rclpy 打交道的动作都收在这里 —— 用例换成记录桩就能不开 ROS 地测完整流程。"""

    def __init__(self):
        rclpy.init()
        self.node = _HeadNode()
        self._pubs = {}
        self._target_pub = None
        self._current = None
        #: 收到过几条状态消息（**只数到达**，不管字段能不能用）——
        #: 用来把"一条都没来（话题没在发布）"与"来了但字段不对"分开报。
        self.state_msgs = 0

    # ---------- 订阅 ----------
    def listen(self) -> None:
        """订阅躯干状态（读当前角度用）。QoS 用 BEST_EFFORT：与任何发布端都兼容。"""
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.node.create_subscription(TrunkState, TRUNK_STATE_TOPIC,
                                      self._on_trunk_state, qos)

    def _on_trunk_state(self, msg) -> None:
        self.state_msgs += 1
        position = list(getattr(msg, "joint_position", []) or [])
        if len(position) == TRUNK_AXIS_COUNT:
            self._current = [float(item) for item in position]

    def spin(self, seconds: float) -> None:
        """在最多 ``seconds`` 秒内把消息泵进来（`spin_once` 带超时，不会忙等）。"""
        deadline = time.monotonic() + max(seconds, 0.0)
        while time.monotonic() < deadline:
            rclpy.spin_once(self.node,
                            timeout_sec=min(0.05, max(deadline - time.monotonic(), 0.0)))

    def wait_current(self, timeout: float = READ_CURRENT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while self._current is None and time.monotonic() < deadline:
            self.spin(0.05)
        return self._current

    def current_missing_reason(self) -> str:
        """读不到当前角度的具体原因（分"一条都没来"与"来了但字段不对"）。"""
        if self.state_msgs == 0:
            return (f"      一条 {TRUNK_STATE_TOPIC} 都没收到 —— 话题没在发布。"
                    "主程序没在跑 / 躯干控制节点没起来。")
        return (f"      收到了 {self.state_msgs} 条，但 joint_position 不是 "
                f"{TRUNK_AXIS_COUNT} 个值 —— 消息版本可能不匹配。")

    # ---------- 发布 ----------
    def publish_bool(self, topic: str, value: bool) -> None:
        publisher = self._pubs.get(topic)
        if publisher is None:
            publisher = self.node.create_publisher(Bool, topic, 10)
            self._pubs[topic] = publisher
        message = Bool()
        message.data = bool(value)
        publisher.publish(message)

    def publish_byte(self, topic: str, value: int) -> None:
        publisher = self._pubs.get(topic)
        if publisher is None:
            publisher = self.node.create_publisher(Byte, topic, 10)
            self._pubs[topic] = publisher
        message = Byte()
        # `std_msgs/msg/Byte` 的 `data` 在 rclpy 里是**长度 1 的 bytes**（不是 int）：
        # 直接赋 int 会抛 `AssertionError: The 'data' field must be of type 'bytes' ...`
        # （v6.1.0 现场整条躯干流就死在这里）。所以先看字段实际是什么类型再决定怎么填。
        if isinstance(getattr(message, "data", None), (bytes, bytearray)):
            message.data = bytes([int(value) & 0xFF])
        else:
            message.data = int(value)
        publisher.publish(message)

    def publish_target(self, values) -> None:
        """发一条躯干目标（6 个**度**）。

        **不设 `layout`** —— 现场脚本就是这么发的，而且它实测能动。
        （v6.3.3 那条"必须设 `data_offset = 1`"是**非实时 + 单条点位**那条路的实测
        结论，实时模式下的可用样本都不设 —— 两条路不能互相套用。）
        """
        if self._target_pub is None:
            self._target_pub = self.node.create_publisher(
                Float32MultiArray, TRUNK_TARGET_TOPIC, 10)
        message = Float32MultiArray()
        message.data = [float(item) for item in values]
        self._target_pub.publish(message)

    def shutdown(self) -> None:
        try:
            self.node.destroy_node()
        except Exception:                      # noqa: BLE001 —— 退出路径上不值得再抛
            pass
        try:
            rclpy.shutdown()
        except Exception:                      # noqa: BLE001
            pass


# ============ 流程 ============
def prepare(streamer, verbose: bool = False) -> list:
    """把躯干切到**实时模式**：`connect` → 等 → `motor_mode 1` → 等 → `connect` → 等。

    **每步等 `PREPARE_GAP`（2s，照现场脚本）**：`motor_mode` 改完必须重发 `connect`
    才生效（SDK 原文），而重连期间发出去的目标会被**静默丢弃** —— 等够才发得动。
    这三步的间隔是本脚本与 `motion_stream.py` 最关键的差别（那份是 0.3s）。
    """
    lines = []
    streamer.publish_bool(TRUNK_CONNECT_TOPIC, True)
    streamer.spin(PREPARE_GAP)
    streamer.publish_byte(TRUNK_MODE_TOPIC, TRUNK_MODE_REALTIME)
    streamer.spin(PREPARE_GAP)
    streamer.publish_bool(TRUNK_CONNECT_TOPIC, True)
    streamer.spin(PREPARE_GAP)
    lines.append(f"    已把躯干切到实时模式（connect → motor_mode {TRUNK_MODE_REALTIME} → "
                 f"connect，每步等 {PREPARE_GAP:g}s）")
    if verbose:
        lines.append(f"    说明：目标由本机插值逐帧下发（{RATE}Hz），控制节点不再内部插值")
    return lines


def move(streamer, start, target, duration: float, rate: int, verbose: bool = False) -> list:
    """从 `start` 缓动到 `target`：按时长与频率逐条下发，返回日志行。

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
            lines.append("    收到停止信号：中止在当前角度（不再往目标走）")
            break
        ratio = index / total
        frame = interpolate(start, target, ease_in_out_cubic(ratio))
        streamer.publish_target(frame)
        percent = int(round(ratio * 100))
        if percent in PROGRESS_STEPS and percent not in reported:
            reported.add(percent)
            lines.append(f"    已走 {percent}%")
        if verbose:
            lines.append("    [%d/%d] %s" % (index + 1, total + 1,
                                             "、".join(f"{item:.3f}" for item in frame)))
        remaining = started + (index + 1) * period - time.monotonic()
        if remaining > 0:
            # 发布间隙也泵一下订阅，保证状态是最新的
            streamer.spin(remaining)
    return lines


def hold_at(streamer, target, seconds: float, rate: int) -> list:
    """到位后按目标保持一段（控制器需要持续的期望值）。"""
    if seconds <= 0:
        return []
    lines = [f"    按目标保持 {seconds:g}s"]
    total = max(int(round(seconds * rate)), 1)
    period = 1.0 / max(rate, 1)
    started = time.monotonic()
    for index in range(total):
        if STOP["requested"]:
            break
        streamer.publish_target(target)
        remaining = started + (index + 1) * period - time.monotonic()
        if remaining > 0:
            streamer.spin(remaining)
    return lines


def run(streamer, verbose: bool = False) -> int:
    """完整流程：准备 → 读当前角度 → 只改 H6 → 缓动 → 保持。"""
    say(f"头部到 {HEAD_TARGET_DEG:g}°（H{HEAD_AXIS_INDEX + 1}），其余轴保持当前角度")
    # 订阅先建好：准备那 6s 里就把当前角度收进来，后面就不用再额外等
    streamer.listen()
    for line in prepare(streamer, verbose):
        say(line)
    current = streamer.wait_current(READ_CURRENT_TIMEOUT)
    if current is None:
        say(f"ERROR: 读不到躯干当前角度（{TRUNK_STATE_TOPIC}）—— 目标要按当前角度构造，"
            "**这一条没有下发**。")
        say(streamer.current_missing_reason())
        return EXIT_NO_DATA
    target = head_target(current)
    say("    开始位置：" + "、".join(f"{item:.1f}" for item in current))
    say("    目标位置：" + "、".join(f"{item:.1f}" for item in target)
        + f"（只改 H{HEAD_AXIS_INDEX + 1}）")
    started = time.monotonic()
    for line in move(streamer, current, target, DURATION, RATE, verbose):
        say(line)
    for line in hold_at(streamer, target, HOLD, RATE):
        say(line)
    elapsed = time.monotonic() - started
    if STOP["requested"]:
        say(f"已中止（用时 {elapsed:.1f}s）—— 躯干停在当前位置，后面不会再动")
        return EXIT_OK
    say(f"已到位（用时 {elapsed:.1f}s）")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="头部走到目标角度：读当前角度 → 只改 H6 → 平滑到位")
    parser.add_argument("--verbose", action="store_true", help="逐周期打印位置（调参用）")
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
    _install_signal_handlers()
    factory = streamer_factory or RosStreamer
    streamer = factory()
    try:
        return run(streamer, args.verbose)
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    sys.exit(main())
