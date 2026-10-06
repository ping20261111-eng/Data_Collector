#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""躯干链路分段体检：把「上位机 → TrunkControlNode → trunk_controller → Gazebo/关节」
拆成五段，每段单独给结论 —— 而不是整条链一起猜。

## 为什么需要它

现场现象（2026-09-29 10:40 / 12:15 两次）：
    躯干预设 KneeBend_1 下发 3 次 → 「没走到位：躯干下发 3 次后仍未见动作（最大还差 54.0°）」
而同一时刻手臂能动、`trunk_state` 有读数（~0°）。也就是说：话题通、状态在、**机构不动**。

**本轮已按现场实测改了上位机（v6.3.3）**：现场给的两组对照指向同一个字段 ——
`Float32MultiArray.layout.data_offset`。脚本留着是为"改完到底动没动"给一条可复核的实测；
B / C 两段就是这一处的干净对照。

## 权威口径（`sdk_docs/sdk_iface.txt` 的躯干那一节）

    /trunk_control/joint_target_position（单位：度，6 个关节）
        非实时模式：设置目标位置后，**关节会内部插值运行到目标位置**，
                    建议目标位置下发时间间隔不小于 1.5 秒。
        实时模式：关节按允许的最大速度运行到目标位置，**需上位机自行做插值处理**。
    /trunk_control/motor_mode  Byte  0=非实时 / 1=实时
        **修改运行模式后，需重新发送连接消息（/trunk_control/connect）后才起效。**
    /trunk_control/speed_rate  Float32  0~1.0  **非实时模式设置关节运行速率**
        C++ 侧同名参数默认 **1.0**，且"仅非实时模式时有效"。
    官方示例的次序：`motor_mode{0x00}` → `connect{true}` → `joint_target_position[10,18,8,0,0,0]`

**所以"非实时 + 单条点位"本身就是官方推荐的用法** —— 这是本脚本要验证的前提，
不是要推翻它。段划分（`--move` 时逐段实测）：

    A 直连 trunk_controller 发 JointTrajectory（topic 形式，绕过 TrunkControlNode，弧度）
    B 非实时 + 单条，**layout 缺省**（`data_offset = 0`）—— 上位机**旧**写法
    C 非实时 + 单条 + **`data_offset = 1`** —— 官方 / 现场 CLI 的写法
    D 实时 + 100Hz 上位机插值（现场 rt_trunk_publisher.py / p2-p3.py 一族的做法）
    E 与插件同路：给 JTC 发 action goal（弧度）

**B 与 C 之间只差 `layout.data_offset` 一个字段** —— 这是本轮定位到的根因所在：
现场实测「带 `data_offset: 1` 的 CLI 单条能让躯干动」，而机器人侧 README、现场测试助手、
现场使能脚本三处写的都是 1（见 v6.3.3 的改动说明）。

## 用法（在**机器人那台机**的终端里，先 source 工作空间）

    source install/setup.bash
    /usr/bin/python3 diagnose_trunk.py                # 只体检，一条指令都不发、不动机构
    /usr/bin/python3 diagnose_trunk.py --move         # 追加五段实测（**会动躯干**，先确认无人）
    /usr/bin/python3 diagnose_trunk.py --move --only B,C   # 只看 data_offset 这一处对照
    /usr/bin/python3 diagnose_trunk.py --move --only A,C
    /usr/bin/python3 diagnose_trunk.py --move --delta 10   # 实测幅度（度），默认 6

**必须用 `/usr/bin/python3`**：ROS Humble 的 `rclpy` 是给 cpython-3.10 编的；机器上若
`python3` 指向别的版本，会报 `No module named 'rclpy._rclpy_pybind11'`（实测踩到）。

## 安全

- 不加 `--move` 时**只读**：只订阅、只问一次 controller_manager，不发任何指令。
- 加 `--move` 时：开场 3 秒倒计时；实测只做**小幅**动作（默认 6°，按当前姿态叠加、
  并按关节量程夹住）；每段测完**立刻回到起始姿态**；全程打印每一步的下发与回读。
- 判定阈值 2°（小于它算"没动"），角度全部是**度**。
- 全程 240 秒看门狗（`faulthandler`）：卡住时把**卡在哪一行**打出来再退出 ——
  没有它的话，输出会因为块缓冲而一个字都不出来（第一次实测就是这样）。
"""

import argparse
import faulthandler
import os
import sys
import time

# ---------------------------------------------------------------- 常量

#: 6 个躯干关节名（顺序：脚踝 → 颈），与 `trunk_controller.yaml` 的 `trunk_joints` 一致
TRUNK_JOINTS = ("trunk1_joint", "trunk2_joint", "trunk3_joint",
                "trunk4_joint", "head1_joint", "head2_joint")
#: 关节量程（度）：与上位机 `core_motion.TRUNK_AXES` 一致，实测时用来夹住目标
TRUNK_LIMITS = ((-85.0, 85.0), (-150.0, 150.0), (-95.0, 95.0),
                (-90.0, 90.0), (-90.0, 90.0), (-30.0, 30.0))

TARGET_TOPIC = "/trunk_control/joint_target_position"
CONNECT_TOPIC = "/trunk_control/connect"
MODE_TOPIC = "/trunk_control/motor_mode"
SPEED_TOPIC = "/trunk_control/speed_rate"
STATE_TOPIC = "/trunk_control/trunk_state"
LEGACY_POSITION_TOPIC = "/trunk_control/joint_position"
JTC_TOPIC = "/trunk/trunk_controller/joint_trajectory"
#: 插件真正用的那条路（action，不是 topic）—— 见 `test_e_action_goal` 的说明
ACTION_TOPIC = "/trunk/trunk_controller/follow_joint_trajectory"
JOINT_STATES_TOPIC = "/joint_states"
CONTROLLER_MANAGER = "/trunk/controller_manager"
LIST_CONTROLLERS_SRV = CONTROLLER_MANAGER + "/list_controllers"

#: 动没动的判定阈值（度）
MOVED_THRESHOLD_DEG = 2.0
#: 实测的缓动时长与频率（与现场脚本一致：100Hz）
MOVE_SECONDS = 2.5
STREAM_RATE = 100.0
#: 非实时模式下两条目标之间的最小间隔（SDK 文档："不小于 1.5 秒"）
NON_REALTIME_GAP_SEC = 1.6
#: 文档给的默认速度倍率（`setJointTargetPosition(..., speed_rate = 1.0f)`）
DOC_SPEED_RATE = 1.0
#: 躯干目标 `Float32MultiArray.layout.data_offset` —— **1**（v6.3.3 现场实测的根因）。
#: 三份可用样本（机器人侧 README / 现场测试助手 / 现场使能脚本）写的都是 1。
TRUNK_LAYOUT_OFFSET = 1
#: 全程看门狗（秒）：卡住就打出卡在哪一行再退出
WATCHDOG_SEC = 240.0

DEG2RAD = 3.141592653589793 / 180.0


def _say(text: str = "") -> None:
    """**带 flush** 的输出。

    为什么必须 flush：本脚本最常见的用法是 `python3 diagnose_trunk.py > log 2>&1`，
    这时 stdout 是**块缓冲**的 —— 一旦某一步卡住（见看门狗），缓冲区永远不会吐出来，
    现场拿到的就是一个空文件，"什么都没有"比报错更难查。这是实测踩到的：第一次跑挂死时
    日志里一个字都没有。
    """
    print(text, flush=True)


# ---------------------------------------------------------------- 纯函数（可单测，不依赖 rclpy）


def clamp_trunk(values):
    """把 6 个角度夹进各自量程（实测用，防止小幅动作越界）。"""
    out = []
    for index, value in enumerate(values):
        low, high = TRUNK_LIMITS[index]
        out.append(min(max(float(value), low), high))
    return out


def nudge(current, deg, pattern=(1.0, 1.8, 0.8, 0.0, 0.0, 0.0)):
    """由**当前姿态**叠加一个小幅动作，并按量程夹住。

    方向用 `pattern`（默认按 `p2-p3.py` 那一族的比例 1 : 1.8 : 0.8 对应脚踝 / 膝 / 大腿）——
    只动一个关节会让仿真里的机器人失衡，成比例的屈膝才稳。
    """
    delta = [float(deg) * ratio for ratio in pattern]
    return clamp_trunk([now + d for now, d in zip(current, delta)])


def moved_deg(before, after):
    """6 个关节里**变化最大**的那个的绝对变化量（度）。"""
    return max(abs(float(a) - float(b)) for a, b in zip(before, after))


def classify(before, after, threshold=MOVED_THRESHOLD_DEG):
    """一次实测动没动：返回 ``(是否动了, 最大变化量)``。"""
    delta = moved_deg(before, after)
    return delta >= threshold, delta


def controller_report(controllers):
    """把 controller_manager 回的控制器列表整理成"这一段能不能动"的判据。

    `controllers`：`[(名字, 状态)]`，状态取自 `ListControllers` 服务的 `state` 字段
    （`active` / `inactive` / `unconfigured` / …）。

    返回 ``(是否可动, 人类可读的行列表)``。判据只有一条：**有一个名字带 trunk 的处于 active**。
    没有 active 的躯干控制器时，后面每一段实测都注定不动 —— 这时先修这一段，别怪上位机。
    """
    lines = []
    active_trunk = False
    for name, state in controllers:
        mark = "OK " if state == "active" else "   "
        lines.append(f"  {mark}{name:<34} {state}")
        if "trunk" in name and state == "active":
            active_trunk = True
    if not controllers:
        lines.append("  ⚠  一个控制器都没问到（controller_manager 没在跑？命名空间不对？）")
    elif not active_trunk:
        lines.append("  ⚠  没有任何名字带 trunk 的控制器处于 active —— 这一段就是断点；"
                     "先把它拉起来（load_controller + set_controller_state … active）")
    return active_trunk, lines


def verdict_table(results: dict) -> str:
    """把五段实测的结果翻成结论。

    `results`：``{"A": (moved, delta), "B": …, "C": …, "D": …, "E": …}``，缺的段用 `None`。
    判据只锚定**可观测事实**（哪一段动了），不做概率推测。
    """
    labels = {
        "A": "A 直连 JTC（topic 形式，绕过 TrunkControlNode）",
        "B": "B 非实时 + 单条，layout 缺省（data_offset=0）",
        "C": "C 非实时 + 单条 + data_offset=1（官方 / 现场 CLI 写法）",
        "D": "D 实时 + 100Hz 上位机插值（现场脚本的做法）",
        "E": "E 与插件同路：给 JTC 发 action goal",
    }
    lines = ["", "=" * 72, "结论", "=" * 72]
    for key in ("A", "B", "C", "D", "E"):
        item = results.get(key)
        if item is None:
            lines.append(f"  {labels[key]}：未测 / 不可达")
        else:
            moved, delta = item
            lines.append(f"  {labels[key]}：{'动了' if moved else '没动'}"
                         f"（最大变化 {delta:.2f}°）")
    lines.append("-" * 72)

    a, b, c, d = (results.get(k) for k in ("A", "B", "C", "D"))
    if a is not None and not a[0]:
        lines.append("→ 关节 / 控制器 / Gazebo 这一段**本身**就不通（直连控制器都不动）。")
        lines.append(f"  先解决它：`ros2 control list_controllers -c {CONTROLLER_MANAGER}`，")
        lines.append("  看 trunk_controller 是否 active。**它在仿真里是由 gzserver 里那个插件")
        lines.append("  自己激活的**（插件日志原话：`Waiting for switch_controller service...` →")
        lines.append("  `Successfully activated trunk controller` / `Failed to activate controller`），")
        lines.append("  所以这一段坏掉时，去看 gzserver 的终端输出最直接。")
        lines.append("  **在这一段修好之前，后面几段换什么模式都不会有用。**")
    elif b is not None and not b[0] and d is not None and d[0]:
        lines.append("→ **实时 + 上位机插值能动、非实时单条不动**。注意这一条与 SDK 文档写的")
        lines.append("  「非实时模式：设置目标位置后，关节会内部插值运行到目标位置」**不符** ——")
        lines.append("  说明这台机器上非实时那条路没走通（控制器没切过去 / 目标没送到）。")
        lines.append("  先看 gzserver 终端：`Failed to activate controller` 说明模式切控制器失败；")
        lines.append("  `Action server not available after waiting` / `Server has rejected the goal`")
        lines.append("  说明目标没送到 JTC。")
        lines.append("  可用的改法（与现场脚本一致）：上使能发 `motor_mode 1`，躯干运动走")
        lines.append("  `run_motion_streams`（100Hz 由上位机插值）—— 这套机制在 Data_Collector")
        lines.append("  里完整保留着，v6.1.1 起页面没用它而已。")
    elif b is not None and not b[0] and c is not None and c[0]:
        lines.append("→ **`layout.data_offset` 就是缺的那一步**（本轮定位到的根因）。")
        lines.append("  非实时 + 单条这条路本身是通的（与 SDK 文档一致）：动的和不动的只差")
        lines.append("  `Float32MultiArray.layout.data_offset`（我们发 0、官方发 1）。")
        lines.append("  v6.3.3 已经把两处都补上了：`core_motion.trunk_target_payload` 的 YAML，")
        lines.append("  与 `ros_command_bridge.build_message` 的消息对象。**这条若是这个结论，")
        lines.append("  说明修的方向对了。**")
    elif b is not None and b[0] and c is not None and not c[0]:
        lines.append("→ 反了：`data_offset` 缺省能动、设成 1 反而不动 —— 说明这台机器的节点")
        lines.append("  把 `data_offset` 当成「数据起始下标」在用（而不是标志位）。")
        lines.append("  那就该发 `data_offset: 0` 并在数组**前面**补一个占位元素，")
        lines.append("  别照搬官方 README 的字面写法。")
    elif d is not None and not d[0]:
        lines.append("→ 现场脚本那条路（实时 + 插值）在这台机器上也不动 —— 那就不是模式/速率的问题。")
        lines.append("  回到 A / E 段的结论去查（控制器有没有 active、JTC 的 action 通不通）。")
    elif a is not None and a[0] and b is not None and b[0]:
        lines.append("→ 该动的都动了：链路本身没问题，回去查上位机那侧下发的**内容**")
        lines.append("  （话题名 / 消息类型 / 单位 / 关节顺序 / 是否被前置条件拦下）。")
        lines.append(f"  还没试过的最后一个口子：`{SPEED_TOPIC}`（文档默认 {DOC_SPEED_RATE:g}，")
        lines.append("  仅非实时有效）—— 真机节点才可能涉及，仿真插件不订阅它。")
    elif a is None and b is None and c is None and d is None:
        lines.append("→ 只体检、未实测。要判定哪一段断，加 `--move` 重跑。")
    else:
        lines.append("→ 结果不完整，按上表逐段看；缺的那段单独重跑（`--only X`）。")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------- rclpy 部分

_ROS_IMPORT_ERROR = ""
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, Byte, Float32, Float32MultiArray
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
    HAVE_ROS = True
except ImportError as _exc:             # 没装 ROS 的机器上也要能 import 纯函数（供单测）
    HAVE_ROS = False
    _ROS_IMPORT_ERROR = str(_exc)

#: 本模块要在**没有 rclpy** 的机器上也能 import（纯函数是给单测用的），所以基类退化成 `object`。
if HAVE_ROS:
    _NodeBase = Node
else:
    _NodeBase = object


class TrunkProbe(_NodeBase):
    """订阅者 + 发布者都建好，实测时只用这一套句柄。"""

    def __init__(self, timeout=6.0):
        super().__init__("diagnose_trunk")
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
        self.pub_target = self.create_publisher(Float32MultiArray, TARGET_TOPIC, qos)
        self.pub_connect = self.create_publisher(Bool, CONNECT_TOPIC, qos)
        self.pub_mode = self.create_publisher(Byte, MODE_TOPIC, qos)
        self.pub_speed = self.create_publisher(Float32, SPEED_TOPIC, qos)
        self.pub_jtc = self.create_publisher(JointTrajectory, JTC_TOPIC, qos)
        self.trunk_state = None
        self.legacy_position = None
        self.joint_states = None
        self.state_msgs = 0
        self.timeout = timeout
        self._traj_client = None
        monitor = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(JointState, JOINT_STATES_TOPIC,
                                 self._on_joint_states, monitor)
        # `/trunk_control/joint_position` 在文档里标了"后续会删除"，但现在还在 ——
        # 拿它做第三个交叉来源正好（三个来源两两对比，谁也赖不掉）
        self.create_subscription(Float32MultiArray, LEGACY_POSITION_TOPIC,
                                 self._on_legacy_position, monitor)
        self.has_trunk_state = False
        try:                              # trunk_state 是自定义消息，读不到就退回另外两个来源
            from wheeled_robot_msgs.msg import TrunkState
            self.create_subscription(TrunkState, STATE_TOPIC, self._on_trunk_state, monitor)
            self.has_trunk_state = True
        except ImportError:
            pass

    # -- 回调 --------------------------------------------------------
    def _on_trunk_state(self, msg):
        self.trunk_state = [float(v) for v in msg.joint_position]
        self.state_msgs += 1

    def _on_legacy_position(self, msg):
        values = [float(v) for v in msg.data]
        self.legacy_position = values if len(values) == 6 else None

    def _on_joint_states(self, msg):
        names, positions = list(msg.name), list(msg.position)
        self.joint_states = [float(positions[names.index(joint)])
                             if joint in names else None for joint in TRUNK_JOINTS]

    # -- 小工具 ------------------------------------------------------
    def spin_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)

    def wait_first_state(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            if self.read_deg() is not None:
                return True
        return False

    def read_deg(self):
        """当前 6 个关节角度（度）；三个来源都读不到返回 None。

        `trunk_state.joint_position` 的单位就是**度**（见 `TrunkState.msg` 注释与 SDK 文档），
        所以三个来源可以直接对比 —— 对不上就说明有一个是错的，这也是体检项之一。
        """
        for source in (self.trunk_state, self.legacy_position, self.joint_states):
            if source is not None:
                return list(source)
        return None

    def subscriber_count(self, topic: str) -> int:
        """话题当前订阅者数（等价于 `ros2 topic info -v` 的 "Subscription count"）。"""
        return self.count_subscribers(topic)

    def publisher_count(self, topic: str) -> int:
        return self.count_publishers(topic)

    def send_trajectory_goal(self, positions_deg, seconds=MOVE_SECONDS):
        """给 JTC 的 `follow_joint_trajectory` 发一个 goal（**与插件同一条路**）。

        返回：``True`` 被接受 / ``False`` 被拒绝 / ``None`` server 不可达。
        单位是**弧度**（这条 action 的官方示例用弧度）。
        """
        try:
            from control_msgs.action import FollowJointTrajectory
            from rclpy.action import ActionClient
        except ImportError as exc:
            _say(f"  ⚠  缺 control_msgs / rclpy.action（{exc}），跳过")
            return None
        if self._traj_client is None:
            self._traj_client = ActionClient(self, FollowJointTrajectory, ACTION_TOPIC)
        client = self._traj_client
        if not client.wait_for_server(timeout_sec=5.0):
            return None
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(TRUNK_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [v * DEG2RAD for v in positions_deg]
        point.velocities = [0.0] * len(TRUNK_JOINTS)
        point.time_from_start.sec = int(seconds)
        goal.trajectory.points = [point]
        future = client.send_goal_async(goal)
        deadline = time.monotonic() + 5.0
        while not future.done() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
        if not future.done():
            return None
        handle = future.result()
        return bool(handle is not None and handle.accepted)

    def list_controllers(self, timeout=5.0):
        """问一次 controller_manager 的 `list_controllers` 服务。

        **为什么不用 `ros2 control list_controllers` 那个命令**：它每次冷启动一个 CLI 进程、
        还可能拉起 `ros2` 守护进程；而 `subprocess.run(capture_output=True, timeout=…)` 在超时后
        只杀**直接子进程**，孙进程还攥着管道 → `communicate()` 会**无限等下去**（实测脚本第一次
        跑就是这么挂死的）。直接调服务没有子进程、拿到的还是结构化字段，一举两得。
        """
        try:
            from controller_manager_msgs.srv import ListControllers
        except ImportError:
            return None, ["  （本机没装 controller_manager_msgs，跳过这项）"]
        try:
            client = self.create_client(ListControllers, LIST_CONTROLLERS_SRV)
        except Exception as exc:                       # 服务名非法等
            return None, [f"  ⚠  建不了服务客户端：{exc}"]
        if not client.wait_for_service(timeout_sec=timeout):
            return None, [f"  ⚠  {LIST_CONTROLLERS_SRV} 不在（controller_manager 没跑？）"]
        future = client.call_async(ListControllers.Request())
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
        if not future.done():
            return None, ["  ⚠  服务回了没结果（超时）"]
        try:
            response = future.result()
            return [(c.name, c.state) for c in response.controller], None
        except Exception as exc:                       # 服务异常
            return None, [f"  ⚠  服务调用失败：{exc}"]


# ---------------------------------------------------------------- 体检（只读）


def physical_exam(node: TrunkProbe) -> None:
    _say("=" * 72)
    _say("① 链路体检（只读，不下发任何指令）")
    _say("=" * 72)
    node.spin_for(3.0)                     # 等 DDS 发现收敛，订阅者计数才有意义
    rows = [
        (TARGET_TOPIC, "订阅者", node.subscriber_count(TARGET_TOPIC),
         "应该是 1：TrunkControlNode（仿真里是 gzserver 插件）"),
        (CONNECT_TOPIC, "订阅者", node.subscriber_count(CONNECT_TOPIC), "应该是 1"),
        (MODE_TOPIC, "订阅者", node.subscriber_count(MODE_TOPIC), "应该是 1"),
        (SPEED_TOPIC, "订阅者", node.subscriber_count(SPEED_TOPIC),
         "应该是 1（非实时模式的限速口）"),
        (STATE_TOPIC, "发布者", node.publisher_count(STATE_TOPIC),
         "应该是 1；0 就说明躯干状态没人报，界面读数会是「—」"),
        (JTC_TOPIC, "订阅者", node.subscriber_count(JTC_TOPIC),
         "应该是 1：trunk_controller（JointTrajectoryController）"),
    ]
    for topic, kind, count, why in rows:
        _say(f"  {'OK ' if count else '⚠  '}{topic:<44} {kind} {count}   （{why}）")

    node.wait_first_state(6.0)
    deg = node.read_deg()
    if deg is None:
        _say("  ⚠  读不到躯干角度：`trunk_state` / `joint_position` / `/joint_states` 都没有躯干关节")
    else:
        _say("  当前躯干角度（度）：" + "、".join(f"{v:+.2f}" for v in deg))
    sources = (("trunk_state", node.trunk_state), ("joint_position", node.legacy_position),
               ("/joint_states", node.joint_states))
    present = [(name, values) for name, values in sources if values is not None]
    for index in range(len(present)):
        for other in range(index + 1, len(present)):
            name_a, values_a = present[index]
            name_b, values_b = present[other]
            if None in values_a or None in values_b:
                continue
            diff = moved_deg(values_a, values_b)
            _say(f"  {name_a} vs {name_b} 的最大差：{diff:.3f}°"
                 f"（{'一致' if diff < 1.0 else '⚠ 不一致，两个来源有一个不对'}）")
    if not node.has_trunk_state:
        _say(f"  ⚠  收不到 {STATE_TOPIC} —— 可能没装 wheeled_robot_msgs，或躯干状态确实没在发")

    _say("\n② 控制器状态（直接问 controller_manager 的 list_controllers 服务）")
    controllers, problem = node.list_controllers()
    if controllers is None:
        for line in (problem or []):
            _say(line)
    else:
        _, lines = controller_report(controllers)
        for line in lines:
            _say(line)


# ---------------------------------------------------------------- 四段实测


def _stream_to(node: TrunkProbe, start, target, seconds=MOVE_SECONDS,
               rate=STREAM_RATE, hold=0.8) -> None:
    """以 `rate` Hz 把 start→target 缓动发出去，再保持 `hold` 秒（现场脚本的做法）。"""
    steps = max(2, int(seconds * rate))
    for step in range(steps):
        t = step / steps
        ease = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
        values = [s + (e - s) * ease for s, e in zip(start, target)]
        node.pub_target.publish(Float32MultiArray(data=[float(v) for v in values]))
        node.spin_for(1.0 / rate)
    for _ in range(max(1, int(hold * rate))):
        node.pub_target.publish(Float32MultiArray(data=[float(v) for v in target]))
        node.spin_for(1.0 / rate)


def _prepare(node: TrunkProbe, mode: int, speed=None) -> None:
    """走 /trunk_control/* 的握手（照 SDK 文档给的次序）：

        motor_mode{0|1} → connect{true} → [speed_rate] → 目标

    文档原文："修改运行模式后，需重新发送连接消息（/trunk_control/connect）后才起效"。
    """
    node.pub_mode.publish(Byte(data=bytes([mode & 0xFF])))
    node.spin_for(1.0)
    node.pub_connect.publish(Bool(data=True))
    node.spin_for(2.0)
    if speed is not None:
        node.pub_speed.publish(Float32(data=float(speed)))
        node.spin_for(0.5)


def _publish_target(node: TrunkProbe, target, times=3, gap=NON_REALTIME_GAP_SEC,
                    offset=0) -> None:
    """非实时模式下重复下发同一条目标；间隔不小于文档要求的 1.5 秒。

    `offset` 写进 `layout.data_offset` —— 这一个字段就是 B 段与 C 段的**唯一差别**，
    也是本轮定位到的根因（缺省 0 时命令发得出去、订阅者也匹配，但机构不动）。
    """
    for _ in range(times):
        message = Float32MultiArray(data=[float(v) for v in target])
        message.layout.data_offset = int(offset)
        node.pub_target.publish(message)
        node.spin_for(gap)


def _measure(node: TrunkProbe, label: str):
    node.wait_first_state(3.0)
    before = node.read_deg()
    if before is None:
        _say(f"  {label}：读不到起始角度，跳过")
    return before


def _readback(node: TrunkProbe):
    after = node.read_deg()
    _say("  读回：" + ("、".join(f"{v:+.2f}" for v in after) if after else "读不到"))
    return after


def _jtc_trajectory(node: TrunkProbe, positions_deg) -> None:
    traj = JointTrajectory()
    traj.joint_names = list(TRUNK_JOINTS)
    point = JointTrajectoryPoint()
    # 注意单位：**这条走 JTC，是弧度**；/trunk_control/* 那条是度。两组官方示例各自如此。
    point.positions = [v * DEG2RAD for v in positions_deg]
    point.velocities = [0.0] * len(TRUNK_JOINTS)
    point.time_from_start.sec = int(MOVE_SECONDS)
    traj.points = [point]
    for _ in range(3):                     # 与现场示例一致：连发几次（等价 --times 2）
        node.pub_jtc.publish(traj)
        node.spin_for(0.5)
    node.spin_for(MOVE_SECONDS + 1.5)


def test_a_jtc(node: TrunkProbe, deg: float):
    """A：直连 trunk_controller 发一条 JointTrajectory（**弧度**），绕过 TrunkControlNode。"""
    before = _measure(node, "A")
    if before is None:
        return None
    target = nudge(before, deg)
    _say(f"  下发 {JTC_TOPIC} → " + "、".join(f"{v:+.1f}" for v in target) + "（度，转弧度）")
    _jtc_trajectory(node, target)
    after = _readback(node)
    result = classify(before, after) if after else None
    _jtc_trajectory(node, before)                       # 回到起始姿态
    return result


def test_b_non_realtime(node: TrunkProbe, deg: float):
    """B：非实时 + 单条点位，**`layout` 缺省**（`data_offset = 0`）—— 上位机**旧**写法。

    留这一段是为了给 C 段当对照：两段之间只差 `layout.data_offset` 一个字段。
    """
    before = _measure(node, "B")
    if before is None:
        return None
    _prepare(node, 0)
    target = nudge(before, deg)
    _say("  非实时 + 单条（layout 缺省 → data_offset=0）→ "
         + "、".join(f"{v:+.1f}" for v in target))
    _publish_target(node, target, offset=0)
    after = _readback(node)
    result = classify(before, after) if after else None
    if result and result[0]:
        _publish_target(node, before)                   # 能动才需要挪回去
    return result


def test_c_with_layout_offset(node: TrunkProbe, deg: float):
    """C：非实时 + 单条点位 + **`layout.data_offset = 1`** —— 官方 / 现场 CLI 的写法。

    为什么这一段是**本轮最该跑的一条**：现场给了两组干净的对照。

    1. 现场 CLI（实测**能让躯干动**）：

           ros2 topic pub --once /trunk_control/joint_target_position \
               std_msgs/msg/Float32MultiArray \
               "{ 'layout': { 'dim': [], 'data_offset': 1 }, 'data': [0, 0, 0, 0, 0, 0] }"

    2. 三处可用样本写法完全一致（机器人侧 README、现场测试助手
       `Robot Test Assistant/modules/preset.py`、现场使能脚本
       `shell-switch_pown_on_test/swich_pown_on.sh`）：都是 `data_offset: 1`。

    而 B 段与 C 段之间**只差这一个字段**，所以结论是确定的：

        B 不动、C 动     → 根因就是它（v6.3.3 已按这个结论改了两处下发代码）
        B 动、C 也不动   → offset 不是原因，继续往 A / E 段查
    """
    before = _measure(node, "C")
    if before is None:
        return None
    _prepare(node, 0)
    target = nudge(before, deg)
    _say(f"  非实时 + 单条 + layout.data_offset={TRUNK_LAYOUT_OFFSET} → "
         + "、".join(f"{v:+.1f}" for v in target))
    _publish_target(node, target, offset=TRUNK_LAYOUT_OFFSET)
    after = _readback(node)
    result = classify(before, after) if after else None
    if result and result[0]:
        _publish_target(node, before)
    return result


def test_d_realtime_stream(node: TrunkProbe, deg: float):
    """D：实时模式（motor_mode 1）+ 100Hz 由上位机插值 —— 现场脚本的做法。"""
    before = _measure(node, "D")
    if before is None:
        return None
    _prepare(node, 1)
    target = nudge(before, deg)
    _say("  实时 + 100Hz 插值 → " + "、".join(f"{v:+.1f}" for v in target))
    _stream_to(node, before, target)
    node.spin_for(0.5)
    after = _readback(node)
    result = classify(before, after) if after else None
    _stream_to(node, after or target, before)           # 回到起始姿态
    return result


def test_e_action_goal(node: TrunkProbe, deg: float):
    """E：**与插件同一条路** —— 给 `/trunk/trunk_controller/follow_joint_trajectory`
    发一个 `FollowJointTrajectory` **action goal**（弧度）。

    为什么这条最该测：从 `libtrunk_control_node_gazebo_plugin.so` 里挖出来的字符串显示，
    TrunkControlNode（仿真里就是它）收到目标后**正是往这个 action 发 goal**，
    并且它会打 `Action server not available after waiting` / `Server has rejected the goal` /
    `Goal result : %d` 这几句日志。所以"这个 action 通不通"= "插件能不能把目标送出去"。

    与 D 的差别：D 走的是 topic（`joint_trajectory`），E 走 action。JTC 两者都提供，
    但**插件的代码路径是 action**，所以 E 更贴近真实故障点。
    """
    before = _measure(node, "E")
    if before is None:
        return None
    target = nudge(before, deg)
    action_ok = node.send_trajectory_goal(target, MOVE_SECONDS)
    if action_ok is None:
        _say(f"  ⚠  到不了 action server（{ACTION_TOPIC}）—— 这一段就是断点")
        return None
    _say(f"  action goal 已发出（{'被接受' if action_ok else '被拒绝'}）→ "
         + "、".join(f"{v:+.1f}" for v in target))
    node.spin_for(MOVE_SECONDS + 2.0)
    after = _readback(node)
    result = classify(before, after) if after else None
    node.send_trajectory_goal(before, MOVE_SECONDS)     # 回到起始姿态
    node.spin_for(MOVE_SECONDS + 1.0)
    return result


TESTS = {"A": test_a_jtc, "B": test_b_non_realtime,
         "C": test_c_with_layout_offset, "D": test_d_realtime_stream,
         "E": test_e_action_goal}
TEST_TITLES = {
    "A": f"A 直连 {JTC_TOPIC}（topic 形式，绕过 TrunkControlNode，弧度）",
    "B": "B 非实时 + 单条，layout 缺省（data_offset=0）—— 上位机旧写法",
    "C": f"C 非实时 + 单条 + data_offset={TRUNK_LAYOUT_OFFSET} —— 官方 / 现场 CLI 写法",
    "D": "D 实时 + 100Hz 上位机插值（现场脚本的做法）",
    "E": f"E 与插件同路：给 {ACTION_TOPIC} 发 action goal（弧度）",
}


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="躯干链路分段体检")
    parser.add_argument("--move", action="store_true",
                        help="追加五段实测（**会动躯干**）；不给就只做只读体检")
    parser.add_argument("--only", default="A,B,C,D,E",
                        help="只跑指定的实测段，例如 --only B,C（B/C 对照只看 offset）")
    parser.add_argument("--delta", type=float, default=6.0,
                        help="实测幅度（度，按当前姿态叠加），默认 6")
    args = parser.parse_args(argv)

    if not HAVE_ROS:
        sys.stderr.write(
            "ERROR: 无法导入 rclpy —— 这个脚本要在**机器人那台机**上、且用 /usr/bin/python3 跑：\n"
            "       source install/setup.bash && /usr/bin/python3 diagnose_trunk.py [--move]\n"
            f"       本机导入失败原因：{_ROS_IMPORT_ERROR}\n")
        return 2

    # 看门狗：卡住时把"卡在哪一行"打出来再退出。没有它，一旦某步阻塞，
    # 输出会因为块缓冲而**一个字都不出来**（第一次实测就是这样）。
    faulthandler.dump_traceback_later(WATCHDOG_SEC, exit=True)

    _say("=" * 72)
    _say("先做这一步（零成本，最省事）：看 gzserver 那个终端的输出")
    _say("=" * 72)
    _say("  仿真里 TrunkControlNode 是 gzserver 的一个插件，它自己在打日志。")
    _say("  按下「上使能」和「运行预设」的同时，去 gzserver 的终端看这几句：")
    _say("    Waiting for switch_controller service...   ← 卡在这 = 切控制器那步没走通")
    _say("    Failed to activate controller             ← **控制器没激活，这就是断点**")
    _say("    Successfully activated trunk controller   ← 这一段是好的")
    _say("    Connect trunk motor / Disconnect trunk motor")
    _say("    Action server not available after waiting ← 目标送不到 JTC")
    _say("    Server has rejected the goal              ← JTC 拒了这条目标")
    _say("    Feedback desired position: ...            ← 打印了目标 = 节点确实收到并转发了")
    _say("  这几句能直接指到断点，比跑脚本还快。脚本用来在「日志里什么都没留下」时补位。\n")
    _say("躯干链路体检：只读体检 →（可选）五段实测")
    _say(f"  ROS_DOMAIN_ID={os.environ.get('ROS_DOMAIN_ID', '0')}，"
         f"看门狗 {WATCHDOG_SEC:.0f}s（卡住会打出卡在哪一行）")
    _say("  ① 初始化 rclpy…（若卡在这一行，说明 DDS 起不来）")
    rclpy.init()
    node = TrunkProbe()
    try:
        _say("  ② 节点已建立，开始体检\n")
        physical_exam(node)

        results = {}
        if args.move:
            keys = [k.strip().upper() for k in args.only.split(",") if k.strip()]
            unknown = [k for k in keys if k not in TESTS]
            if unknown:
                sys.stderr.write(f"ERROR: --only 里有不认识的分段：{unknown}（可选 A/B/C/D/E）\n")
                return 2
            _say("\n" + "=" * 72)
            _say(f"③ 实测（幅度 {args.delta:g}°，判定阈值 {MOVED_THRESHOLD_DEG:g}°）")
            _say("=" * 72)
            _say("⚠  接下来会动躯干。3 秒后开始 —— 确认机构运动范围内没有人。")
            for countdown in (3, 2, 1):
                _say(f"    {countdown} …")
                node.spin_for(1.0)
            for key in keys:
                _say(f"\n[{key}] {TEST_TITLES[key]}")
                results[key] = TESTS[key](node, args.delta)
                if results[key] is None:
                    _say("  → 未测出（读不到角度）")
                else:
                    moved, delta = results[key]
                    _say(f"  → {'动了' if moved else '没动'}（最大变化 {delta:.2f}°）")
        _say(verdict_table(results))
        _say("\n提示：把上面 ①②③ 三段输出**整段**回贴，就能定位到具体是哪一段。")
        return 0
    finally:
        faulthandler.cancel_dump_traceback_later()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
