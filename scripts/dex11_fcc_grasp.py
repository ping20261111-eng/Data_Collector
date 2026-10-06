#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宇树 Dex11 夹爪的 FCC（实时）抓取：S 曲线合拢 → 按指定时长夹持 → 松开 → 退出实时模式。

为什么不是直接跑机器人侧的 `install/lib/tools/dex11_fcc_grasp_test.py`
------------------------------------------------------------------
那个脚本**固定驱动左右两侧**（源码里 `SIDES = ("left", "right")`，没有单侧开关），而现场操作
界面上左右各有一组按钮 —— 只想让左爪抓的时候，右爪不该跟着合（右爪里可能正夹着东西）。
所以这里重写一份**支持 `--side`** 的等价实现，协议取值与官方脚本逐条对齐，并由
`tests/test_scripts.py::test_fcc_script_constants_match_vendor_test` 守着不许漂。

为什么不放在机器人工作空间
--------------------------
机器人侧的文件一律不动（现场要求）。本脚本放在上位机项目的 `scripts/` 下，随交付包一起发。

用法
----
    python3 scripts/dex11_fcc_grasp.py --side left --hold 3.0
    python3 scripts/dex11_fcc_grasp.py --side both --tau 500 --speed 200 --hold 5.0
    python3 scripts/dex11_fcc_grasp.py --side right --hold 2 --verbose

前提
----
* 主程序在跑，且**手臂已连接**（末端手掌是连手臂时自动连上的）；
* 需要 ROS 环境（先 source 工作空间的 `install/setup.bash`）。

输出约定
--------
默认只报现场要看的：要做什么、走到哪个阶段、结果（开口多少 / 实际力矩多少 / 用时）。
逐周期的位置·力矩跟踪只在 `--verbose` 下打印 —— 那是调参用的，默认刷屏没有意义。
"""

import argparse
import statistics
import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import JointState
    from std_msgs.msg import Bool, UInt16MultiArray
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 rclpy（%s）\n"
        "       请先 source <工作空间>/install/setup.bash；若机器上有多个 python，"
        "请用 /usr/bin/python3 执行。\n" % exc
    )
    sys.exit(2)

# ============ 协议取值（与官方 dex11_fcc_grasp_test.py 逐条对齐）============
# 这些数字**不要凭手感改**：它们同时被上位机（core_operations.GRIPPER_*）与官方测试程序使用，
# 两处漂移会出现"整机测试能抓、这里抓不动"这种最难查的形态。
SIDES = ("left", "right")
SIDE_NAMES = {"left": "左爪", "right": "右爪"}
JOINTS = {"left": "LJoint1_1", "right": "RJoint1_1"}
RAW_OPEN = 100          # 张开位。**不是 0** —— 官方留了 10% 余量
RAW_CLOSED = 1000       # 完全闭合
JNT_OPEN = -0.02        # 张开位对应的关节角（rad），来自控制器 yaml 的 jnt_limit_lower
JNT_CLOSED = 0.0245     # 闭合位对应的关节角，来自 jnt_limit_upper
TAU_MIN = 0.5           # 力矩下限（N·m）：低于它控制器当作"没给力"，夹爪会完全松掉
TAU_MAX = 2.0           # 力矩上限
SPEED_MAX_MMS = 100.0   # 速度千分比 1000 对应的实际速度（实测跟得住的最快值）
RATE_HZ = 50.0          # 指令下发频率。底层 200Hz，业务侧不超过它即可
#: 实时模式下"闭合速度"这一位没有意义（速度由上层轨迹决定），但仍要占位 —— 官方脚本填 1000
SPEED_PLACEHOLDER = 1000
#: 开启/关闭实时模式时每条重复发的次数与间隔（官方脚本同款：一条容易没落上）
FORWARD_SET_REPEAT = 3
FORWARD_SET_GAP_SEC = 0.05
FORWARD_SET_SETTLE_SEC = 1.0
#: 张开到位 / 结束后取平均值的时长（取平均是为了避开抖动，官方脚本同款）
PRE_OPEN_HOLD_SEC = 1.0
SETTLE_SEC = 1.0
#: 收不到夹爪关节时的等待上限
JOINT_TIMEOUT_SEC = 15.0

#: 退出码：界面按它 + 输出里的标记给结论
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_JOINTS = 3
EXIT_RUNTIME = 4


# ============ 纯函数（无 ROS 依赖，可直接单测）============
def quintic(x: float) -> float:
    """S 曲线（五次多项式）。两端一阶、二阶导都为 0 → 起停都没有冲击。"""
    return x * x * x * (10 - 15 * x + 6 * x * x)


def raw_to_joint(raw: float) -> float:
    """位置千分比 → 关节角（rad）。超出 0~1000 的一律钳住。

    钳位是为了与上位机的 `gripper_joint_rad()` 完全一致：回读到的角度偶尔会略微
    出界（装配零点 / 噪声），不钳的话两边算出来的"开口多少"会对不上。
    """
    ratio = max(0.0, min(1000.0, raw)) / 1000.0
    return JNT_OPEN + ratio * (JNT_CLOSED - JNT_OPEN)


def raw_to_tau(raw: float) -> float:
    """力矩千分比 → N·m，并钳到 [TAU_MIN, TAU_MAX]。

    下限钳到 0.5 而不是 0：控制器把低于 `grasp_tau_min` 的力矩当作"没给力"，
    给 0 会让夹爪**完全松掉**（官方两个测试程序都是这么换算的）。
    """
    return max(TAU_MIN, min(TAU_MAX, (raw / 1000.0) * TAU_MAX))


def raw_to_speed_mms(raw: float) -> float:
    """速度千分比 → mm/s。下限取 1 只为避免"0 速度"在算轨迹时长时除零。"""
    return max(1, min(1000, raw)) / 1000.0 * SPEED_MAX_MMS


def stroke_seconds(from_raw: float, to_raw: float, speed_mms: float) -> float:
    """走完这段行程需要多久（秒）。行程按 mm 算：1 rad ≡ 1 m。"""
    stroke_mm = (JNT_CLOSED - JNT_OPEN) * 1000.0
    return abs(to_raw - from_raw) / 1000.0 * stroke_mm / max(1.0, speed_mms)


def total_seconds(speed_raw: float, hold_sec: float) -> float:
    """整个过程大概要多久 —— 界面拿它告诉现场"这次会安静几秒"。

    组成：张开到位 + 合拢 + 夹持 + 取值 + 松开 + 收尾。
    """
    one_way = stroke_seconds(RAW_OPEN, RAW_CLOSED, raw_to_speed_mms(speed_raw))
    return (PRE_OPEN_HOLD_SEC + one_way + hold_sec + SETTLE_SEC + one_way
            + PRE_OPEN_HOLD_SEC + SETTLE_SEC)


def opening_mm(rad) -> float:
    """关节角 → 开口宽度（mm）。夹住东西时等于物体宽度，合到底为 0。"""
    try:
        return (JNT_CLOSED - float(rad)) * 1000.0
    except (TypeError, ValueError):
        return 0.0


#: 张开位（raw=100）对应的开口宽度 ≈ 40.1mm；满行程 44.5mm 只在 raw=0 出现
OPEN_GAP_MM = (JNT_CLOSED - (JNT_OPEN + RAW_OPEN / 1000.0 * (JNT_CLOSED - JNT_OPEN))) * 1000.0


def opening_text(rad) -> str:
    """开口的一句话说法（与上位机界面同一套分档口径）。

    阈值按**张开位**算（raw=100 ≈ 40.1mm），不是满行程 44.5mm —— 张开位本身就留了 10% 余量，
    拿满行程当基准会把正常的张开算成"没开到位"。
    """
    gap = opening_mm(rad)
    if gap < 0.5:
        return "已合到底（没夹住东西）"
    if gap >= OPEN_GAP_MM - 1.5:
        return "已张开"
    return "开口 %.1fmm（夹着东西）" % gap


def summary_line(sides, tau_raw: int, speed_raw: int, hold_sec: float) -> str:
    """一行预告：给哪些爪子、多大力、多快、夹多久。"""
    names = "、".join(SIDE_NAMES[s] for s in sides)
    return ("FCC 实时抓取（%s）：力矩 %.2f N·m，合拢速度 %.0f mm/s，夹持 %.1fs"
            % (names, raw_to_tau(tau_raw), raw_to_speed_mms(speed_raw), hold_sec))


def step_line(index: int, total: int, text: str) -> str:
    """每一步一行进度。"""
    return "  [%d/%d] %s" % (index, total, text)


def plan_lines(verbose: bool, sides, tau_raw: int, speed_raw: int, hold_sec: float) -> list:
    """完整计划（`--verbose` 才打印）。"""
    if not verbose:
        return []
    lines = ["=== FCC 抓取计划 ==="]
    for side in sides:
        lines.append("  %-3s 关节 %-10s 张开 raw=%d → 合拢 raw=%d"
                     % (side, JOINTS[side], RAW_OPEN, RAW_CLOSED))
    lines.append("  力矩 raw=%d → %.2f N·m（钳到 %.1f~%.1f）"
                 % (tau_raw, raw_to_tau(tau_raw), TAU_MIN, TAU_MAX))
    lines.append("  速度 raw=%d → %.1f mm/s；合拢一段 %.2fs；下发频率 %.0f Hz"
                 % (speed_raw, raw_to_speed_mms(speed_raw),
                    stroke_seconds(RAW_OPEN, RAW_CLOSED, raw_to_speed_mms(speed_raw)),
                    RATE_HZ))
    lines.append("  夹持 %.1fs；预计总耗时 %.1fs" % (hold_sec, total_seconds(speed_raw, hold_sec)))
    return lines


def joint_result_line(side: str, rad, tau) -> str:
    """夹持结束后的读数行：开口 + 实际力矩 vs 目标。"""
    text = "%s %s" % (SIDE_NAMES[side], opening_text(rad))
    if isinstance(tau, (int, float)):
        text += "，实际力矩 %.2f N·m" % tau
    return text


def result_line(interrupted: bool, elapsed: float) -> str:
    """最后一行结论。"""
    if interrupted:
        return "FCC 抓取已中断，用时 %.1fs" % elapsed
    return "FCC 抓取完成，用时 %.1fs" % elapsed


def missing_joint_line(sides) -> str:
    """收不到夹爪关节：这不是"没抓到"，而是链路根本没通，必须说清怎么修。"""
    names = "、".join(SIDE_NAMES[s] for s in sides)
    return ("⚠ 收不到 %s 的夹爪关节 —— 请确认主程序在跑，且已在遥操作里**连接手臂**"
            "（末端手掌是连接手臂时自动连上的）" % names)


def build_parser():
    """命令行参数。单独成函数：这些开关要能被用例断言存在与默认值。"""
    parser = argparse.ArgumentParser(
        description="宇树 Dex11 夹爪 FCC（实时）抓取：合拢 → 夹持指定时长 → 松开")
    parser.add_argument("--side", required=True,
                        choices=("left", "right", "both"),
                        help="抓哪一侧：left / right / both")
    parser.add_argument("--tau", type=int, default=300,
                        help="目标力矩千分比 0~1000（默认 300 → 0.6 N·m）")
    parser.add_argument("--speed", type=int, default=300,
                        help="合拢速度千分比 1~1000（默认 300 → 30 mm/s）")
    parser.add_argument("--hold", type=float, default=3.0,
                        help="夹持保持时长（秒，默认 3.0）—— 现场说的「抓取多少时间」")
    parser.add_argument("--joint-timeout", type=float, default=JOINT_TIMEOUT_SEC,
                        help="等夹爪关节出现的上限（秒）")
    parser.add_argument("--verbose", action="store_true",
                        help="打印计划与逐周期跟踪（调参用；默认只报阶段与结果）")
    return parser


def selected_sides(name: str):
    """`--side` 的取值 → 侧别元组。"""
    return SIDES if name == "both" else (name,)


# ============ ROS 侧 ============
class FccGrasp(Node):
    """FCC 实时抓取的一次性执行器。"""

    def __init__(self, sides, verbose: bool = False):
        super().__init__("dex11_fcc_grasp")
        self.sides = tuple(sides)
        self.verbose = verbose
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
        self.set_pub = {s: self.create_publisher(Bool, "/arm_control/%s_hand_forward_set" % s, qos)
                        for s in SIDES}
        self.cmd_pub = {s: self.create_publisher(
            UInt16MultiArray, "/arm_control/%s_hand_forward_update" % s, qos)
            for s in SIDES}
        self.pos = {s: None for s in SIDES}
        self.tau = {s: None for s in SIDES}
        self.create_subscription(JointState, "/joint_states", self._on_joints, 20)

    def _on_joints(self, msg):
        for side in SIDES:
            joint = JOINTS[side]
            if joint in msg.name:
                index = msg.name.index(joint)
                self.pos[side] = msg.position[index]
                if index < len(msg.effort):
                    self.tau[side] = msg.effort[index]

    def spin(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.005)

    def wait_joints(self, timeout: float) -> bool:
        """等目标侧的夹爪关节出现。**只等要操作的那几侧** —— 只动左爪时右爪没连不该挡路。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.05)
            if all(self.pos[s] is not None for s in self.sides):
                return True
        return False

    def set_forward_mode(self, on: bool):
        """开关实时模式（FCC）。

        每条**发 3 次**再等 1 秒（官方脚本同款）：只发一次有可能没落上，而落不上的表现
        是"后面 50Hz 的指令全都无效"—— 那会和"夹爪坏了"长得一模一样。
        """
        for side in self.sides:
            for _ in range(FORWARD_SET_REPEAT):
                self.set_pub[side].publish(Bool(data=on))
                self.spin(FORWARD_SET_GAP_SEC)
        self.spin(FORWARD_SET_SETTLE_SEC)

    def send(self, raw, tau_raw):
        """向目标侧下发一条实时指令 `[闭合速度, 闭合位置, 目标力矩]`（千分比）。"""
        for side in self.sides:
            msg = UInt16MultiArray()
            msg.data = [SPEED_PLACEHOLDER, int(raw), int(tau_raw)]
            self.cmd_pub[side].publish(msg)

    def pace(self, nxt: float):
        while time.monotonic() < nxt and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.001)

    def hold(self, raw, tau_raw, seconds: float):
        """按 RATE_HZ 持续下发同一条指令（实时模式下要一直喂，否则控制器会超时）。"""
        dt = 1.0 / RATE_HZ
        end = time.monotonic() + seconds
        nxt = time.monotonic()
        while time.monotonic() < end and rclpy.ok():
            self.send(raw, tau_raw)
            nxt += dt
            self.pace(nxt)

    def move(self, from_raw, to_raw, tau_raw, mm_per_s: float):
        """S 曲线把指令从 from_raw 推到 to_raw（起停无冲击）。"""
        duration = stroke_seconds(from_raw, to_raw, mm_per_s)
        dt = 1.0 / RATE_HZ
        t0 = nxt = time.monotonic()
        while rclpy.ok():
            t = time.monotonic() - t0
            if t >= duration:
                break
            self.send(from_raw + (to_raw - from_raw) * quintic(t / duration), tau_raw)
            if self.verbose:
                self.get_logger().info(
                    "  q=%.4f rad  tau=%.3f N·m"
                    % (self.pos.get(self.sides[0]) or 0.0, self.tau.get(self.sides[0]) or 0.0))
            nxt += dt
            self.pace(nxt)

    def settle(self, seconds: float = SETTLE_SEC):
        """取最后一段的平均位置与实际力矩（避开抖动，官方脚本同款做法）。"""
        pos = {s: [] for s in self.sides}
        tau = {s: [] for s in self.sides}
        end = time.monotonic() + seconds
        while time.monotonic() < end and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.01)
            for side in self.sides:
                if self.pos[side] is not None:
                    pos[side].append(self.pos[side])
                if self.tau[side] is not None:
                    tau[side].append(self.tau[side])
        mean = statistics.mean
        return {s: (mean(pos[s]) if pos[s] else None,
                    mean(tau[s]) if tau[s] else None) for s in self.sides}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    sides = selected_sides(args.side)
    hold_sec = max(0.1, float(args.hold))
    tau_raw = int(args.tau)
    speed_raw = int(args.speed)

    for line in plan_lines(args.verbose, sides, tau_raw, speed_raw, hold_sec):
        print(line)
    print(summary_line(sides, tau_raw, speed_raw, hold_sec), flush=True)

    rclpy.init()
    node = FccGrasp(sides, verbose=args.verbose)
    started = time.monotonic()
    interrupted = False
    forward_on = False
    code = EXIT_OK
    try:
        print(step_line(1, 6, "等夹爪关节（最多 %.0fs）" % args.joint_timeout), flush=True)
        if not node.wait_joints(args.joint_timeout):
            print(missing_joint_line([s for s in sides if node.pos[s] is None]), flush=True)
            return EXIT_NO_JOINTS            # finally 里不会去关 FCC（本来就没开）

        print(step_line(2, 6, "开启实时模式（FCC）"), flush=True)
        node.set_forward_mode(True)
        forward_on = True

        print(step_line(3, 6, "张开到位"), flush=True)
        node.hold(RAW_OPEN, tau_raw, PRE_OPEN_HOLD_SEC)

        stroke = stroke_seconds(RAW_OPEN, RAW_CLOSED, raw_to_speed_mms(speed_raw))
        print(step_line(4, 6, "S 曲线合拢（约 %.1fs）" % stroke), flush=True)
        node.move(RAW_OPEN, RAW_CLOSED, tau_raw, raw_to_speed_mms(speed_raw))

        print(step_line(5, 6, "夹持 %.1fs" % hold_sec), flush=True)
        node.hold(RAW_CLOSED, tau_raw, hold_sec)

        grip = node.settle()
        for side in sides:
            rad, tau = grip[side]
            print("  夹持结果：%s" % joint_result_line(side, rad, tau), flush=True)

        print(step_line(6, 6, "松开并退出实时模式"), flush=True)
        node.move(RAW_CLOSED, RAW_OPEN, tau_raw, raw_to_speed_mms(speed_raw))
        node.hold(RAW_OPEN, tau_raw, PRE_OPEN_HOLD_SEC)
        node.set_forward_mode(False)
        forward_on = False
    except KeyboardInterrupt:
        interrupted = True
        print("\n[中断] 收到 Ctrl+C", flush=True)
    except Exception as exc:                                    # noqa: BLE001
        code = EXIT_RUNTIME
        print("⚠ 执行出错：%s" % exc, flush=True)
    finally:
        # 没走完就退出时，**必须**把爪子松开并退出实时模式：实时模式下没人继续喂指令，
        # 控制器会保持最后一条指令（夹爪冻在半合位置、还在使劲）。
        if forward_on:
            try:
                node.hold(RAW_OPEN, tau_raw, PRE_OPEN_HOLD_SEC)
                node.set_forward_mode(False)
                print("  已松开并退出实时模式（收尾）", flush=True)
            except Exception as exc:                            # noqa: BLE001
                print("⚠ 收尾未完成（%s）—— 夹爪可能停在半合位置，请断电复位" % exc, flush=True)
        node.destroy_node()
        rclpy.shutdown()

    print(result_line(interrupted, time.monotonic() - started), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
