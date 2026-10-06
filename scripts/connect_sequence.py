#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机器人上电时序（遥操作启动）。

背景
----
原 run_teleop.sh 用 10 次 `ros2 topic pub --once --keep-alive 2` 逐条下发，
每次都要冷启动一个 ros2 CLI 进程（Python 解释器 + DDS 初始化 + 2 秒 keep-alive 空等）。
这些开销本身没有意义，却"顺带"充当了硬件等待时间——所以脚本既慢，又让每条指令的
实际到达时刻 = sleep + 发布开销，难以推算。

本脚本做两件事：
1) 把整条时序收进**一个进程**：publisher 一次性创建、统一等一次发现，
   每条指令发完即走（等其被 ACK），把每条 2~3 秒的固定开销降到接近 0。
2) 把"双臂使能后等 5 秒"这种猜测，换成**真实就绪判据**：
   订阅 `/arm_control/arm_state`，等到 left_arm_connected 且 right_arm_connected。
   这不是锦上添花——arm_control_node 里有
   `left arm is not connected, ignore setting follow` 这种静默丢弃，
   使能没完成就下发 follow，指令会消失且没有任何报错。

时序取值与原脚本逐条对应，只删除了逻辑变更后不再需要的一步：
   `/trunk_control/motor_mode 0x00`（显式切非实时）——
   现在躯干/手臂下使能即自动切到非实时模式。
其余步骤（含两次 trunk connect True）保持不变，原因见交付说明。

用法
----
    python3 connect_sequence.py                 # 硬件时序
    python3 connect_sequence.py --sim           # 仿真：只下发手臂相关步骤
    python3 connect_sequence.py --dry-run       # 只打印时序，不发布
    python3 connect_sequence.py --list          # 查看各等待项当前取值
    python3 connect_sequence.py --speed 0.8     # 所有等待 ×0.8（台架调参）
    python3 connect_sequence.py --wait trunk_enable1=4.0
    python3 connect_sequence.py --no-gate       # 关闭就绪门禁，退回纯固定等待
    python3 connect_sequence.py --verbose       # 打印完整时序表（话题名/取值/等待/门禁），排查用

输出约定
--------
默认只打印**现场需要**的三件事：要等多久、走到哪一步、成没成（一行预告 + 每步一行进度 + 一行结果）。
话题名、取值、每步等待、门禁策略、就绪检查这些**实现细节默认不打印**（现场反馈："不需要暴露
那么多信息"）；排查时加 `--verbose`（`--dry-run` 自动带上）拿到完整表格。
"""

import argparse
import signal
import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, Byte
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 rclpy（%s）\n"
        "       请先 source install/setup.bash；若机器上有多个 python，请用 /usr/bin/python3 执行。\n" % exc
    )
    sys.exit(2)

try:
    from wheeled_robot_msgs.msg import ArmState
    HAVE_ARM_STATE = True
except ImportError:  # 消息包不可用时降级为纯固定等待，不影响可用性
    ArmState = None
    HAVE_ARM_STATE = False


TOPICS = {
    "arm_connect":     ("/arm_control/connect",                       Bool),
    "trunk_connect":   ("/trunk_control/connect",                     Bool),
    "trunk_mode":      ("/trunk_control/motor_mode",                  Byte),
    "chassis_connect": ("/chassis_control/connect",                   Bool),
    "left_follow":     ("/arm_control/left_arm_follow_position_set",  Bool),
    "right_follow":    ("/arm_control/right_arm_follow_position_set", Bool),
}

ARM_STATE_TOPIC = "/arm_control/arm_state"

# 各步骤之后的等待时间（秒）。来源：原 run_teleop.sh 的 sleep 值。
DEFAULT_WAITS = {
    "arm_disable":    1.0,
    "trunk_disable":  1.0,
    "chassis_enable": 3.0,
    "trunk_enable1":  5.0,   # 日志实测：connect→Header move done ≈ 2.8s，可台架下调
    "trunk_rt":       3.0,
    "trunk_enable2":  3.0,
    "arm_enable":     5.0,   # 门禁失效时的兜底值；门禁生效时取 max(此值, 实际就绪耗时)
    "follow_left":    1.0,
    "follow_right":   1.0,
}

# 时序：(主题键, 取值, 之后等待的键, 说明, 仿真是否执行, 就绪门禁)
SEQUENCE = [
    ("arm_connect",     False, "arm_disable",    "双臂下使能（自动切非实时）",   True,  None),
    ("trunk_connect",   False, "trunk_disable",  "躯干下使能（自动切非实时）",   False, None),
    ("chassis_connect", True,  "chassis_enable", "底盘使能",                     False, None),
    ("trunk_connect",   True,  "trunk_enable1",  "躯干使能（自动抬头，非实时）", False, None),
    ("trunk_mode",      1,     "trunk_rt",       "躯干切实时模式",               False, None),
    ("trunk_connect",   True,  "trunk_enable2",  "躯干重新使能（实时）",         False, None),
    ("arm_connect",     True,  "arm_enable",     "双臂使能（等左右臂 connected）", True,  "arms_connected"),
    ("left_follow",     True,  "follow_left",    "左臂进入跟随（遥操作通道）",   True,  None),
    ("right_follow",    True,  "follow_right",   "右臂进入跟随",                 True,  None),
]


# ============ 输出文案：默认简洁 / --verbose 详细 ============
# 现场反馈「遥操作开启文案简单一点，不需要暴露那么多信息」，于是把两件事分开：
#   * 现场需要的：**要等多久**、**走到哪一步**、**成没成**；
#   * 排查需要的：话题名 / 取值 / 每步等待 / 门禁策略 / 就绪检查 —— 实现细节，默认不打印。
# 顺带消掉一处重复：原来时序会被打印**两遍**（开跑前的表格 + 执行时逐条），
# 现在只有"每步一行进度"，表格留给 --verbose。
def summary_line(mode: str, steps, waits) -> str:
    """一行预告：几步、大概要等多久（默认输出只有这一行预告）。"""
    return "遥操作上电（%s）：%d 步，纯等待约 %.0f 秒" % (
        mode, len(steps), sum(waits[s[2]] for s in steps))


def plan_lines(mode: str, steps, waits, *, gate_enabled: bool,
               sim: bool, have_arm_state: bool) -> list:
    """`--verbose` 的完整时序表：话题名 + 取值 + 每步等待 + 门禁标记。"""
    lines = ["=== 上电时序（%s，共 %d 步）===" % (mode, len(steps))]
    for i, (key, value, wait_key, desc, _sim, gate) in enumerate(steps, 1):
        extra = "  [门禁:%s]" % gate if gate else ""
        lines.append("  %d. %-46s = %-5s 等待 %4.1fs   %s%s"
                     % (i, TOPICS[key][0], str(value), waits[wait_key], desc, extra))
    if gate_enabled:
        lines.append("  门禁策略: 等左右臂 connected，且不短于该步固定等待")
    elif not sim and not have_arm_state:
        lines.append("  提示: 未找到 wheeled_robot_msgs，就绪门禁不可用，退回固定等待")
    return lines


def step_line(verbose: bool, index: int, total: int, elapsed: float, step) -> str:
    """每一步的进度行。

    默认只说人话（第几步 / 做什么），详细模式才带话题名与累计耗时 ——
    "卡在哪一步"是现场最常用的信息，但它不需要知道话题叫什么。
    """
    key, value, _wait_key, desc, _sim, _gate = step
    if verbose:
        return "  %d/%d  +%5.1fs  %-46s = %s" % (index, total, elapsed, TOPICS[key][0], value)
    return "  [%d/%d] %s" % (index, total, desc)


def missing_line(verbose: bool, elapsed: float, count: int) -> str:
    """有主题等了半天没有订阅者：这是**故障**，两种模式都要说，只是措辞不同。"""
    if verbose:
        return "[发现] 用时 %.1fs，未发现订阅者的主题：" % elapsed
    return "⚠ %d 条指令的目标节点没起来（这些指令会丢失）：" % count


def result_line(verbose: bool, interrupted: bool, total: float) -> str:
    """最后一行结论：成没成、用多久。"""
    if verbose:
        return "=== %s，总用时 %.1fs ===" % ("已中断" if interrupted else "时序完成", total)
    return "%s，用时 %.1fs" % ("上电已中断" if interrupted else "上电完成", total)


class SequenceRunner(Node):
    def __init__(self, node_name="connect_sequence"):
        super().__init__(node_name)
        self.pubs = {}
        for key, (topic, msg_type) in TOPICS.items():
            # 与 ros2 topic pub 默认 QoS 对齐：RELIABLE / VOLATILE / KEEP_LAST 10
            qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
            self.pubs[key] = self.create_publisher(msg_type, topic, qos)

        self.arm_state = None
        self.arm_state_msgs = 0
        if HAVE_ARM_STATE:
            # 订阅端用 BEST_EFFORT：与 reliable / best_effort 发布端都兼容
            monitor_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
            self.create_subscription(ArmState, ARM_STATE_TOPIC, self._on_arm_state, monitor_qos)

    # -- 内部回调 ----------------------------------------------------------
    def _on_arm_state(self, msg):
        self.arm_state = msg
        self.arm_state_msgs += 1

    # -- 工具函数 ----------------------------------------------------------
    def spin_for(self, duration):
        """等待期间保持节点活跃，使订阅回调与 Ctrl+C 都及时。"""
        end = time.monotonic() + duration
        while True:
            remain = end - time.monotonic()
            if remain <= 0:
                return
            rclpy.spin_once(self, timeout_sec=min(0.05, remain))

    def wait_for_subscribers(self, keys, timeout):
        """等每个待用主题至少被一个订阅者发现，返回未匹配的主题键列表。"""
        pending = set(keys)
        end = time.monotonic() + timeout
        while pending and time.monotonic() < end:
            for key in list(pending):
                if self.pubs[key].get_subscription_count() > 0:
                    pending.discard(key)
            if pending:
                rclpy.spin_once(self, timeout_sec=0.05)
        return sorted(pending)

    def wait_arms_connected(self, timeout):
        """等左右臂（含手爪串口）均 connected。

        返回 (结果, 耗时秒)：True=就绪 / False=超时 / None=拿不到状态（消息包缺失或无发布者）
        """
        if not HAVE_ARM_STATE:
            return None, 0.0
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            st = self.arm_state
            if st is not None and st.left_arm_connected and st.right_arm_connected:
                return True, time.monotonic() - start
            rclpy.spin_once(self, timeout_sec=0.1)
        if self.arm_state is None:
            return None, time.monotonic() - start
        return False, time.monotonic() - start

    def send(self, key, value):
        pub = self.pubs[key]
        pub.publish(self._make_msg(pub.msg_type, value))
        try:
            pub.wait_for_all_acked(timeout_sec=0.5)
        except Exception:
            self.spin_for(0.05)

    @staticmethod
    def _make_msg(msg_type, value):
        """构造消息。

        注意：ROS 2 中 std_msgs/Byte 的 data 字段类型为 `octet`，
        Python 绑定要求长度为 1 的 bytes（传 int 会抛 AssertionError）。
        """
        field_types = getattr(msg_type, "_fields_and_field_types", {}) or {}
        if field_types.get("data") in ("octet", "char"):
            return msg_type(data=bytes([value]))
        return msg_type(data=value)

    def describe_arm_state(self):
        st = self.arm_state
        if st is None:
            if not HAVE_ARM_STATE:
                return "不可用（未找到 wheeled_robot_msgs）"
            return "未收到 %s 任何消息" % ARM_STATE_TOPIC
        return ("左臂 connected=%s power_on=%s estop=%s moving=%s | "
                "右臂 connected=%s power_on=%s estop=%s moving=%s"
                % (st.left_arm_connected, st.left_arm_power_on, st.left_arm_emergency_stop,
                   st.left_arm_moving, st.right_arm_connected, st.right_arm_power_on,
                   st.right_arm_emergency_stop, st.right_arm_moving))


def build_waits(args):
    # 先应用全局缩放，再应用显式覆盖 —— 显式 --wait 优先于 --speed
    waits = dict(DEFAULT_WAITS)
    if args.speed != 1.0:
        waits = {k: round(v * args.speed, 3) for k, v in waits.items()}
    for item in args.wait or []:
        if "=" not in item:
            raise SystemExit("--wait 需要 name=value 形式，例如 --wait trunk_enable1=4.0")
        name, _, raw = item.partition("=")
        if name not in waits:
            raise SystemExit("未知等待项 '%s'，可用：%s" % (name, ", ".join(sorted(waits))))
        waits[name] = float(raw)
    return waits


def wants_verbose(args) -> bool:
    """是否打印完整时序表。

    `--dry-run` 是预演/排查模式，自动带上完整表格（它本来就是为了看时序内容而存在的）。
    """
    return bool(args.verbose or args.dry_run)


def build_parser():
    """命令行参数。单独成一个函数：`--verbose` 这类开关要能被用例断言存在与默认值。"""
    parser = argparse.ArgumentParser(
        description="机器人上电时序（替代 run_teleop.sh 中的多次 ros2 topic pub）")
    parser.add_argument("--sim", action="store_true", help="仿真模式：只下发手臂相关步骤")
    parser.add_argument("--dry-run", action="store_true", help="只打印时序，不发布")
    parser.add_argument("--list", action="store_true", help="列出等待项取值后退出")
    parser.add_argument("--speed", type=float, default=1.0, help="所有等待时间缩放系数（默认 1.0）")
    parser.add_argument("--wait", action="append", metavar="NAME=SEC", help="覆盖单项等待，可重复")
    parser.add_argument("--discovery-timeout", type=float, default=10.0, help="等待订阅者发现的总超时")
    parser.add_argument("--arm-ready-timeout", type=float, default=20.0,
                        help="等待左右臂 connected 的超时（默认 20s）")
    parser.add_argument("--no-gate", action="store_true", help="关闭就绪门禁，退回纯固定等待")
    parser.add_argument("--strict", action="store_true", help="有主题无订阅者时以非 0 退出")
    parser.add_argument("--verbose", action="store_true",
                        help="打印完整时序表（话题名/取值/每步等待/门禁），排查时用；"
                             "默认只给一行预告 + 每步进度 + 结果")
    return parser


def main():
    args = build_parser().parse_args()

    waits = build_waits(args)
    steps = [s for s in SEQUENCE if (not args.sim) or s[4]]
    gate_enabled = (not args.no_gate) and (not args.sim) and HAVE_ARM_STATE

    if args.list:
        print("等待项取值（秒）：")
        for name in DEFAULT_WAITS:
            mark = "   <- 被 --wait/--speed 修改" if waits[name] != DEFAULT_WAITS[name] else ""
            print("  %-16s %5.1f%s" % (name, waits[name], mark))
        print("  时序合计（纯等待）: %.1f 秒" % sum(waits[s[2]] for s in steps))
        print("  就绪门禁: %s" % ("开启（等 arm_state 左右臂 connected）" if gate_enabled else "关闭"))
        return 0

    mode = "仿真" if args.sim else "硬件"
    verbose = wants_verbose(args)
    if verbose:
        for line in plan_lines(mode, steps, waits, gate_enabled=gate_enabled,
                               sim=args.sim, have_arm_state=HAVE_ARM_STATE):
            print(line)
    else:
        print(summary_line(mode, steps, waits))
    if args.dry_run:
        print("=== dry-run：未发布任何指令，纯等待合计约 %.1fs ===" % sum(waits[s[2]] for s in steps))
        return 0

    rclpy.init()
    node = SequenceRunner()

    def _on_sigint(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    started = time.monotonic()
    missing = []
    warnings = []
    total = 0.0
    interrupted = False
    try:
        need = sorted({s[0] for s in steps})
        if verbose:
            print("\n[发现] 等待 %d 个主题的订阅者（最多 %.1fs）..."
                  % (len(need), args.discovery_timeout))
        missing = node.wait_for_subscribers(need, args.discovery_timeout)
        discovered = time.monotonic() - started
        if missing:
            # 这是故障路径：默认也要说，而且要点名是哪几条 —— 否则现场无从修
            print(missing_line(verbose, discovered, len(missing)))
            for key in missing:
                print("       - %s（该指令会丢失，请确认对应节点已启动）" % TOPICS[key][0])
        elif verbose:
            print("[发现] 全部就绪，用时 %.1fs" % discovered)

        if verbose:
            print("\n[执行]")
        for i, step in enumerate(steps, 1):
            key, value, wait_key, _desc, _sim, gate = step
            node.send(key, value)
            # flush：进度写进日志文件后要立刻可见（界面是 tail 这个文件的）
            print(step_line(verbose, i, len(steps), time.monotonic() - started, step), flush=True)
            wait = waits[wait_key]
            if gate == "arms_connected" and gate_enabled:
                ok, waited = node.wait_arms_connected(args.arm_ready_timeout)
                if ok is True:
                    if verbose:
                        print("        就绪检查: 左右臂已 connected（等待 %.1fs）" % waited)
                elif ok is False:
                    warnings.append("左右臂在 %.0fs 内未全部 connected" % args.arm_ready_timeout)
                    if verbose:
                        print("        就绪检查: 超时！左右臂未全部 connected（已等 %.1fs）" % waited)
                    else:
                        # 这不是内部细节：臂没就绪会让后面的 follow 指令被静默丢弃
                        print("    ↳ 左右臂未就绪（已等 %.0fs），先按固定等待继续"
                              % args.arm_ready_timeout, flush=True)
                elif verbose:
                    print("        就绪检查: 未收到 %s，退回固定等待" % ARM_STATE_TOPIC)
                # 门禁已消耗 waited 秒，只需补足到固定等待的差额（避免重复计时）
                wait = max(0.0, wait - waited)
            node.spin_for(wait)
    except KeyboardInterrupt:
        interrupted = True
        if verbose:
            print("\n[中断] 收到 Ctrl+C，时序未执行完")
    finally:
        total = time.monotonic() - started
        # 手臂状态这一行只在排查模式打印：默认模式下"没就绪"已经单独提示过了
        if verbose:
            print("\n[状态] 手臂: %s" % node.describe_arm_state())
        try:
            node.destroy_node()
        except Exception:
            pass
        try:
            rclpy.shutdown()
        except Exception:
            pass

    for w in warnings:
        print("[警告] %s" % w)
    print("\n" + result_line(verbose, interrupted, total))
    if missing and args.strict:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
