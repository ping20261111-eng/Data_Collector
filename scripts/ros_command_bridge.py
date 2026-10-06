#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻命令通道：**一次发现、多次下发、回读确认**。

为什么要有它（现场 v6.2.0："姿态控制成功率与效果不满意，重构"）
----------------------------------------------------------------
上一版的每一条指令都是一个新进程：`bash -c "source setup.bash; ros2 topic pub
--once -w 1 --keep-alive N"`。三个性质直接决定了成功率上限：

1. **每次都要重做一遍 DDS 发现**（0.3~3s，图里节点多时更久）。`-w 1` 是"等匹配到
   订阅者再发"，等不到就超时退出 → 退出码 124 → 界面如实报"未确认送达"。
   于是"发现慢"被报成了"下发失败" —— 这是假失败的主要来源；
2. **只发一次**。QoS 是 VOLATILE：订阅者还没匹配上、或那一条丢了，指令就永远没了，
   而且没有任何反馈；
3. **发出去 ≠ 生效**。退出码 0 只说明"命令交出去了"，机构到没到无从判断，
   只能靠固定睡眠猜（1.5s / 3s）—— 猜短了动作被截断，猜长了节奏拖沓，
   这就是"效果不满意"里的另一半。

本进程把这三条一次解决：publisher 只创建一次、发现只做一次；每条命令**等到
订阅者出现再发**、并且**默认重发 3 次**；最后**回读关节角**判断到没到位
（带残差与重试）。于是判据从"退出码"变成"机构真的到了"。

协议
----
行协议，一行一个 JSON。上位机写 stdin、本进程写 stdout。
**本进程写到 stdout 的每一行都以 `@DC@` 开头**，其余输出（rclpy 的日志等）一律走
stderr —— 即便将来混进来也不会污染协议（客户端丢掉不认识的行并记为"通道输出"）。

请求（字段均可省，括注为默认值）：

    {"id": 1, "op": "ping"}
    {"id": 2, "op": "publish", "items": [{"name", "topic", "type",
                                              "payload", "repeat"(3),
                                              "wait_sub"(3.0)}]}
    {"id": 3, "op": "move", "kind": "trunk", "target": [度×6],
                                 "tol"(1.0), "timeout"(12.0), "retries"(2)}
    {"id": 4, "op": "move", "kind": "arm", "side": "left",
                                 "target": [度×7], "tol"(2.0), ...}
    {"id": 5, "op": "move_batch", "items": [{"kind": "arm", "side": "left", ...},
                                            {"kind": "arm", "side": "right", ...}]}
    {"id": 7, "op": "ensure", "what": "trunk_non_realtime"}
    {"id": 8, "op": "ensure", "what": "arm_point_mode", "side": "left"}
    {"id": 9, "op": "state"}
    {"id": 10, "op": "shutdown"}

回复：`{"id": N, "ok": bool, ...}`；`ok` 取 `null` 表示**未确认**
（读不到状态这类"不知道"，不能当成成功也不能当成失败）。耗时动作会先发若干
`{"id": N, "event": "progress", "line": "..."}`。

单位：**协议里一律是度**（与界面、与预设文件一致）；弧度换算只在本进程里做
（`ArmMove.position` 是弧度）。躯干话题本身就是度，原样透传。
"""

import json
import math
import os
import select
import sys
import time

# 协议走 stdout，ROS 的日志必须让它走 stderr —— 否则 rclpy 的 INFO 会把协议行冲散
os.environ.setdefault("RCUTILS_LOGGING_USE_STDOUT", "0")

#: 协议行前缀。客户端只认它，其余行原样记为"通道输出"（排障用）
PROTOCOL_PREFIX = "@DC@"
#: 协议版本。改动字段含义时 +1，客户端据此拒绝/兼容
PROTOCOL_VERSION = 2

#: 空闲多久自动退出（秒）。界面关掉后进程不该留一辈子；也用于兜住"父进程没了"这种情况
IDLE_TIMEOUT = 1800.0
#: 一条命令默认重发次数。VOLATILE 下"发一次"不可靠，重发是最便宜的冗余
DEFAULT_REPEAT = 3
#: 重发之间的间隔（毫秒）。太密会被 DDS 合并，太疏则拖慢一步动作
DEFAULT_GAP_MS = 40
#: 第一次往某个话题发之前，最多等多久出现订阅者（秒）
DEFAULT_WAIT_SUB = 3.0

#: 躯干到位的角度容差（度）
TRUNK_TOL_DEG = 1.0
#: 手臂到位的角度容差（度）
ARM_TOL_DEG = 2.0
#: 一条运动命令的总超时（秒）与重发次数
MOVE_TIMEOUT = 12.0
MOVE_RETRIES = 2
#: "动了没"的判据窗口（秒）：发布后这么久内残差没有减小，就当这条目标没送达 → 重发
MOVE_STALL_WINDOW = 1.5
#: 等 state 首帧的上限（秒）。本进程长驻，第一帧只在启动时等一次
STATE_WAIT = 6.0

#: 躯干目标 `Float32MultiArray.layout.data_offset` —— **必须是 1**（v6.3.3）。
#: 三份可用样本（机器人侧 README / 现场测试助手 / 现场使能脚本）写的都是 1；
#: 缺省 0 时命令"发得出去、订阅者也匹配，但机构一动不动"。
#: 与 `data_collector/core_motion.py::TRUNK_LAYOUT_OFFSET` 必须一致（两处各有一份：
#: 这个脚本是独立进程，不 import 包；一致性由 `tests/test_contracts.py` 钉住）。
TRUNK_LAYOUT_OFFSET = 1

#: 切模式 / 重新使能之后要停多久（秒）才继续。对齐现场使能脚本
#: （`shell-switch_pown_on_test/swich_pown_on.sh`：`motor_mode → sleep 3 → connect →
#: sleep 3`）。见 `ensure_trunk_non_realtime` 的说明。
MODE_SWITCH_SETTLE = 3.0

#: 躯干速度倍率（`speed_rate`，0~1.0，**仅非实时模式有效**）。与
#: `data_collector/core_motion.py::TRUNK_SPEED_RATE` 必须一致（两处各有一份：
#: 本脚本是独立进程，不 import 包；一致性由 `tests/test_scripts.py` 钉住）。
#: 每次下发躯干目标前显式发一条 —— 理由见 `core_motion` 里那个常量的说明。
TRUNK_SPEED_RATE = 1.0

#: 消息类型 → 建消息的函数名（表在下面，**逐字段显式构造**，不做通用 YAML→消息转换）
MSG_TYPES = (
    "std_msgs/msg/Bool",
    "std_msgs/msg/Byte",
    "std_msgs/msg/Float32",
    "std_msgs/msg/Float32MultiArray",
    "std_msgs/msg/Float64MultiArray",
    "wheeled_robot_msgs/msg/ArmMove",
    "wheeled_robot_msgs/msg/HandPosture",
)

#: 启动时就把 publisher 建好的话题：让 DDS 发现在**空闲时**完成。
#: 这是本进程最核心的一笔收益 —— 现场第一条命令不再等 3 秒。
PREWARM = (
    ("/trunk_control/connect", "std_msgs/msg/Bool"),
    ("/trunk_control/motor_mode", "std_msgs/msg/Byte"),
    ("/trunk_control/joint_target_position", "std_msgs/msg/Float32MultiArray"),
    ("/trunk_control/speed_rate", "std_msgs/msg/Float32"),
    ("/arm_control/connect", "std_msgs/msg/Bool"),
    ("/arm_control/left_arm_move", "wheeled_robot_msgs/msg/ArmMove"),
    ("/arm_control/right_arm_move", "wheeled_robot_msgs/msg/ArmMove"),
    ("/arm_control/left_arm_follow_position_set", "std_msgs/msg/Bool"),
    ("/arm_control/right_arm_follow_position_set", "std_msgs/msg/Bool"),
    ("/arm_control/left_arm_servoj_set", "std_msgs/msg/Bool"),
    ("/arm_control/right_arm_servoj_set", "std_msgs/msg/Bool"),
    ("/arm_control/hand_posture", "wheeled_robot_msgs/msg/HandPosture"),
)

TRUNK_STATE_TOPIC = "/trunk_control/trunk_state"
ARM_STATE_TOPIC = "/arm_control/arm_state"
#: 躯干轴数 / 单臂轴数（与消息定义一致）
TRUNK_AXIS_COUNT = 6
ARM_AXIS_COUNT = 7
#: 非实时模式（0）：控制节点内部插值 —— 单条点位目标就是靠它才平滑
TRUNK_MODE_NON_REALTIME = 0


def log(message: str) -> None:
    """排障输出：**一律走 stderr**（stdout 是协议）。"""
    sys.stderr.write(f"[bridge] {message}\n")
    sys.stderr.flush()


def emit(payload: dict) -> None:
    """写一行协议（含收尾换行并立刻 flush —— 上位机在等它）。"""
    sys.stdout.write(f"{PROTOCOL_PREFIX} {json.dumps(payload, ensure_ascii=False)}\n")
    sys.stdout.flush()


# ============ payload 解析（我们自己的 YAML 子集） ============
def parse_payload(text):
    """把条目里的 YAML payload 解析成 dict。

    这些 payload 是**我们自己生成的**（`core_motion.py` 的 `*_payload`），形状只有五种：
    ``data: [...]`` / ``data: 0.4`` / ``data: false`` / ``{targets: [...], scale: 0.4}`` /
    ``{side: 0, id: 40, start_addr: 0, position: [...]}``。用 PyYAML 解析（ROS 环境必有，
    ros2cli 自己就依赖它），解析结果再按**消息类型**逐字段取。

    `Byte` 有个坑：机器人侧 README 写的是 ``{'data': {0x00}}``。那一串在 YAML 里**不是**
    整数，也不是 Python 的 set，而是"只有键、没有值"的映射（PyYAML 给 `{0x00: None}`）。
    照原样透传会在 `bytes(...)` 上炸掉，于是"这条命令永远发不出去" —— 所以这里把这种
    "单键、值为 None"的写法折成那个键。
    """
    import yaml

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"payload 不是一个消息对象：{text!r}")
    return {key: _normalize(value) for key, value in data.items()}


def _normalize(value):
    """把 YAML 的几种"花样写法"归一：单元素集合 / 单键无值映射 → 标量，元组 → 列表。

    `{0x00}` 这一类的写法（机器人侧 README 的 `Byte` 就是这么写的）在 YAML 里**不是**
    整数，也不是 Python 的 set，而是"只有键、没有值"的映射 —— PyYAML 给 `{0: None}`。
    照原样透传会在 `bytes(...)` 上炸掉，表现是"这条命令永远发不出去"。
    """
    if isinstance(value, dict) and len(value) == 1 and next(iter(value.values())) is None:
        return _normalize(next(iter(value)))
    if isinstance(value, (set, frozenset)):
        items = list(value)
        if len(items) != 1:
            raise ValueError(f"集合写法只能有一个元素：{value!r}")
        return items[0]
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


#: 已经打过"字段类型"日志的字段名（同一字段只打一次，别把 stderr 刷满）
_TYPE_LOGGED = set()


def as_floats(value) -> list:
    """把 state 里的数组字段读成 ``float`` 列表。**绝不写 `x or []`**。

    现场就死在那一行：`joint_position` / `*_arm_pos.position` 在这台机上**布尔判断是
    "含糊"的** ——

        ValueError: The truth value of an array with more than one element is ambiguous.

    `or` 会去问"这个数组真不真"，而数组没有"真或假"这个答案。异常又从 `spin_once`
    冒出来 → 通道进程退出码 1 → 所有运动悄悄掉回单次发布的兜底路径（右臂那条 ArmMove
    就是这样丢的）。所以这里只认"是不是 None"和"能不能逐个取出来"，与真假无关。
    """
    if value is None:
        return []
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        log(f"这个字段读不成数字（已跳过）：{type(value).__name__}: {exc}")
        return []


def note_array_type(name: str, value) -> None:
    """把某个字段**真实的类型**打一次到 stderr（排障用）。

    为什么留着它：这一波花了半天才确认"数组的布尔判断含糊"来自哪种类型 ——
    下次再有人遇到同形问题，日志里第一行就能看到类型名，不用再猜。
    """
    if name in _TYPE_LOGGED or value is None:
        return
    _TYPE_LOGGED.add(name)
    log(f"字段类型取到：{name} = {type(value).__name__}"
        f"（长度 {len(value) if hasattr(value, '__len__') else '?'}）")


def deg_to_radians(values) -> list:
    """度 → 弧度。**全项目只有两处做这件事**：这里与 `core_motion.deg_to_radians`
    （后者给一次性 CLI 兜底路径拼 YAML 用），由 `test_scripts.py` 的契约用例钉住两者一致。"""
    return [float(item) * math.pi / 180.0 for item in values]


# ============ 消息构造（逐字段显式，不做通用转换） ============
def build_message(msg_type: str, fields: dict, types: dict):
    """按消息类型逐字段构造消息对象。`types` 是**完整类型名 → 消息类**的表
    （`load_types()["_raw"]`，键形如 `std_msgs/msg/Byte`）。

    为什么不写通用的 "dict → 消息"：那条路要靠反射，`Byte.data`（长度 1 的 `bytes`）
    这类字段会踩坑 —— v6.1.0 的躯干流就是死在"按 int 填 Byte"上。显式构造每条字段，
    读代码的人一眼能看出发的是什么。
    """
    if msg_type == "std_msgs/msg/Bool":
        return types[msg_type](data=bool(fields.get("data")))
    if msg_type == "std_msgs/msg/Byte":
        value = int(fields.get("data"))
        return types[msg_type](data=bytes([value & 0xFF]))
    if msg_type in ("std_msgs/msg/Float32", "std_msgs/msg/Float64"):
        return types[msg_type](data=float(fields.get("data")))
    if msg_type in ("std_msgs/msg/Float32MultiArray", "std_msgs/msg/Float64MultiArray"):
        # `layout.data_offset` 是**必需字段**，不是装饰（v6.3.3 现场实测的根因）：
        # 同一台机器、同一条「非实时 + 单条点位」的路，带 `data_offset: 1` 能动、
        # 缺省 0 一动不动。三份可用样本（机器人侧 README / 现场测试助手 / 现场使能
        # 脚本）写的都是 1。
        # **只设 data_offset，不填 dim** —— 三份样本的 `dim` 都是空表。
        message = types[msg_type](data=[float(item) for item in fields["data"]])
        layout = fields.get("layout") or {}
        message.layout.data_offset = int(layout.get("data_offset", 0))
        return message
    if msg_type == "wheeled_robot_msgs/msg/ArmMove":
        scale = float(fields.get("scale", 0.4))
        positions = [float(item) for item in (fields.get("position") or fields["data"])]
        move = types[msg_type](scale=scale)
        target = types["wheeled_robot_msgs/msg/ArmPosition"]()
        target.position = positions
        move.targets = [target]
        return move
    if msg_type == "wheeled_robot_msgs/msg/HandPosture":
        posture = types[msg_type]()
        posture.side = int(fields.get("side", 0))
        posture.id = int(fields.get("id", 0))
        posture.start_addr = int(fields.get("start_addr", 0))
        posture.position = [int(item) for item in fields["position"]]
        return posture
    raise ValueError(f"不认识的消息类型：{msg_type}")


class Bridge:
    """命令通道本体。**不碰 Qt、不读配置** —— 它只认协议，所以能单独测。"""

    def __init__(self, node, types: dict):
        self.node = node
        self.types = types
        self.pubs = {}
        self.trunk = None            # 最新一帧躯干关节角（度）
        self.trunk_at = 0.0
        self.arms = {"left": None, "right": None}
        self.arms_at = {"left": 0.0, "right": 0.0}
        self.arm_connected = {"left": None, "right": None}
        self.arm_msgs = 0
        self.trunk_msgs = 0
        #: 回调 / spin 里出过的错（**只记账，不致命**）。现场那条崩溃就是没这个计数：
        #: 进程悄悄死了，界面只看到"通道不可用"，不知道死因在订阅回调里。
        self.errors = 0
        self._prewarm()

    # ---------- 订阅：到位判据的来源 ----------
    def subscribe_states(self) -> None:
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        if "TrunkState" in self.types:
            self.node.create_subscription(
                self.types["TrunkState"], TRUNK_STATE_TOPIC, self._on_trunk, qos)
        if "ArmState" in self.types:
            self.node.create_subscription(
                self.types["ArmState"], ARM_STATE_TOPIC, self._on_arm, qos)

    def _on_trunk(self, msg) -> None:
        """躯干状态 → 当前关节角（度）。**这一帧出问题只丢这一帧，绝不带走进程**。"""
        self.trunk_msgs += 1
        raw = getattr(msg, "joint_position", None)
        note_array_type("trunk.joint_position", raw)
        values = as_floats(raw)
        if len(values) == TRUNK_AXIS_COUNT:
            self.trunk = values
            self.trunk_at = time.monotonic()

    def _on_arm(self, msg) -> None:
        """手臂状态 → 两侧当前关节角（弧度进来、**这里换成度**）。"""
        self.arm_msgs += 1
        for side in ("left", "right"):
            try:
                self.arm_connected[side] = bool(getattr(msg, f"{side}_arm_connected", False))
            except Exception:                   # noqa: BLE001 —— 连连接标志都读不到就别猜
                self.arm_connected[side] = None
            holder = getattr(msg, f"{side}_arm_pos", None)
            # **不要写 `if holder else []`**：现场 `*_arm_pos` 的值本身也可能"布尔含糊"，
            # 而且 `holder` 是个消息对象，用 `is None` 表达"没有"才是本意。
            raw = getattr(holder, "position", None) if holder is not None else None
            note_array_type(f"{side}_arm_pos.position", raw)
            values = as_floats(raw)
            if len(values) == ARM_AXIS_COUNT:
                self.arms[side] = [math.degrees(item) for item in values]
                self.arms_at[side] = time.monotonic()

    def spin(self, seconds: float) -> None:
        """泵一会儿。**再出问题也不能让通道死掉**。

        现场就是死在这一点上：`joint_position` 的布尔判断抛异常 → 异常从 `spin_once`
        冒出来 → 进程退出码 1 → 之后每一次运动都静默掉回"单次发布"的兜底路径
        （而那条路正是右臂那条 ArmMove 丢掉的地方）。回调里出的问题只该**记一笔**。
        """
        import rclpy

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                rclpy.spin_once(self.node, timeout_sec=0.02)
            except Exception as exc:            # noqa: BLE001 —— 回调的问题不带走进程
                self.errors += 1
                log(f"spin 里出错（已忽略，通道继续活着）：{type(exc).__name__}: {exc}")

    # ---------- 发布 ----------
    def _prewarm(self) -> None:
        """启动即建 publisher（发现只在空闲时做一次）。"""
        for topic, msg_type in PREWARM:
            try:
                self.publisher(topic, msg_type)
            except Exception as exc:            # noqa: BLE001 —— 缺消息包不该让进程起不来
                log(f"预建 {topic} 失败（{exc}）")

    def publisher(self, topic: str, msg_type: str):
        key = (topic, msg_type)
        pub = self.pubs.get(key)
        if pub is None:
            pub = self.node.create_publisher(self.types["_raw"][msg_type], topic, 10)
            self.pubs[key] = pub
        return pub

    def subscribers(self, publisher) -> int:
        try:
            return int(publisher.get_subscription_count())
        except AttributeError:                  # 替身 / 老版本 rclpy
            return -1                           # -1 = 问不到（不能当成"没有订阅者"）

    def wait_for_subscriber(self, publisher, timeout: float) -> int:
        """等到这个话题出现订阅者；返回最终数到的个数（`-1` = 问不到）。

        **为什么非等不可**：VOLATILE 语义下，订阅者还没匹配上时 publish 会被直接丢掉，
        等于没发。上一版靠 `ros2 topic pub -w 1` 等它，但每一步都是一个新进程、每次都要
        重新发现，等不到就退出码 124。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            count = self.subscribers(publisher)
            if count != 0:
                return count
            if time.monotonic() >= deadline:
                return 0
            self.spin(0.05)

    def publish(self, item: dict, progress=None) -> dict:
        """发一条命令：等订阅者 → 重发 `repeat` 次。返回 ``{ok, reason, matched, sent}``。"""
        name = item.get("name") or item["topic"]
        topic, msg_type = item["topic"], item["type"]
        repeat = int(item.get("repeat", DEFAULT_REPEAT))
        wait_sub = float(item.get("wait_sub", DEFAULT_WAIT_SUB))
        gap = float(item.get("gap_ms", DEFAULT_GAP_MS)) / 1000.0
        try:
            fields = parse_payload(item["payload"]) if isinstance(item["payload"], str) \
                else dict(item["payload"])
            message = build_message(msg_type, fields, self.types["_raw"])
        except Exception as exc:                # noqa: BLE001 —— 参数问题是现场可见的
            return {"name": name, "ok": False, "sent": 0, "matched": -1,
                    "reason": f"这条命令构造不出来：{exc}"}

        try:
            publisher = self.publisher(topic, msg_type)
        except Exception as exc:                # noqa: BLE001
            return {"name": name, "ok": False, "sent": 0, "matched": -1,
                    "reason": f"建不了发布者：{exc}"}

        matched = self.wait_for_subscriber(publisher, wait_sub)
        if matched == 0:
            return {"name": name, "ok": False, "sent": 0, "matched": 0,
                    "reason": f"等 {wait_sub:g}s 没有订阅者（主程序没在跑？话题 {topic}）"}
        sent = 0
        for index in range(max(1, repeat)):
            try:
                publisher.publish(message)
            except Exception as exc:            # noqa: BLE001
                return {"name": name, "ok": False, "sent": sent, "matched": matched,
                        "reason": f"发布失败：{exc}"}
            sent += 1
            if index + 1 < repeat:
                self.spin(gap)
        if progress is not None:
            progress(f"        {name}：已发 {sent} 次（订阅者 {matched} 个）")
        return {"name": name, "ok": True, "sent": sent, "matched": matched, "reason": ""}

    # ---------- 回读到位 ----------
    def _residual(self, kind: str, side, target) -> tuple:
        """当前值与目标的逐轴差；返回 ``(残差列表, 可用?)``。"""
        if kind == "trunk":
            current = self.trunk
        else:
            current = self.arms.get(side)
        if not current or len(current) != len(target):
            return None, False
        return [float(now) - float(want) for now, want in zip(current, target)], True

    def _wait_reach(self, kind, side, target, tol, window, progress=None, label="",
                    stall_window=MOVE_STALL_WINDOW, poll=0.05):
        """在 `window` 秒内等"到位"；返回 ``(状态, 最大残差, 已用秒数)``。

        状态取 ``reached`` / ``moving`` / ``stalled`` / ``nodata``：
        * `reached`：逐轴都在容差内（**这是唯一的"成功"**）；
        * `moving`：残差明显在减小（机构正在走，继续等就有希望）；
        * `stalled`：下发后残差一动不动 → 大概率这条目标没送达（重发的判据）；
        * `nodata`：读不到 state（**"不知道"**，既不能算成功也不算失败）。
        """
        start = time.monotonic()
        best = None
        moved_at = start            # 上一次"残差明显变小"的时刻（没动过就一直等于起点）
        while True:
            residual, ok = self._residual(kind, side, target)
            if ok:
                peak = max(abs(item) for item in residual)
                if peak <= tol:
                    return "reached", residual, time.monotonic() - start
                if best is None or peak < best - 0.05:
                    best = peak
                    moved_at = time.monotonic()
            elapsed = time.monotonic() - start
            if elapsed >= window:
                if not ok:
                    return "nodata", None, elapsed
                stalled_for = time.monotonic() - moved_at
                return ("moving" if stalled_for < stall_window else "stalled"), \
                    residual, elapsed
            self.spin(poll)

    def move(self, kind, target, *, side=None, tol=None, timeout=MOVE_TIMEOUT,
             retries=MOVE_RETRIES, stall_window=MOVE_STALL_WINDOW, poll=0.05,
             progress=None) -> dict:
        """下发**一条**运动目标并确认到位。返回 ``{ok, residual, attempts, elapsed, reason}``。

        单条是批量的特例（`move_batch`），两条路共用同一套判据与重发逻辑 ——
        免得"单发"与"齐发"各自漂成一套语义。
        """
        result = self.move_batch(
            [{"kind": kind, "side": side, "target": target, "tol": tol}],
            timeout=timeout, retries=retries, stall_window=stall_window,
            poll=poll, progress=progress)
        item = next(iter(result.get("items", {}).values()), {})
        return {"ok": result["ok"], "residual": item.get("residual"),
                "attempts": item.get("attempts", 0), "matched": item.get("matched", -1),
                "elapsed": result.get("elapsed", 0.0),
                "reason": result.get("reason", "")}

    def move_batch(self, items, *, timeout=MOVE_TIMEOUT, retries=MOVE_RETRIES,
                   stall_window=MOVE_STALL_WINDOW, poll=0.05, progress=None) -> dict:
        """一次下发**多条**目标，然后**一起**等到位（保持"两条手臂同时动"）。

        为什么要批量：预设里左右臂本来就是**同一时刻**的目标 —— 官方手柄示例
        （`arm_proxy.cpp` 的 `sendConfigMotion`）也是"先把两条 `ArmMove` 都发出去，
        再一起等 `arm_move_over_`"。若逐条走（发左 → 等左到位 → 发右），镜像动作就变成
        "先左后右"，现场一眼能看出来。

        判据与单条完全一致：**逐条**回读残差、**逐条**在卡滞时重发、逐条报结果。
        返回 ``{ok, items: {键: {ok, residual, attempts, matched, reason}}, elapsed, reason}``；
        总判据：全部到位 → `True`；有发不出去/没走到位的 → `False`；读不到状态 → `None`。
        """
        prepared = []
        for item in items:
            kind = item["kind"]
            side = item.get("side")
            prepared.append({
                "key": f"{kind}:{side or '-'}", "kind": kind, "side": side,
                "target": [float(v) for v in item["target"]],
                "tol": float(item.get("tol") if item.get("tol") is not None else
                             (TRUNK_TOL_DEG if kind == "trunk" else ARM_TOL_DEG)),
                "attempts": 0, "matched": -1, "residual": None, "best": None,
                "state": "pending", "reason": "", "moved_at": time.monotonic(),
            })
        if not prepared:
            return {"ok": False, "items": {}, "elapsed": 0.0, "reason": "没有目标"}

        # 前置条件（每条命中的手臂只发一次）：`*_arm_move` 只在"非 follow position
        # 模式"生效 —— 遥操作会把手臂留在 follow 模式，此时 ArmMove 被**静默忽略**。
        for side in dict.fromkeys(p["side"] for p in prepared if p["kind"] == "arm"):
            self.ensure_arm_point_mode(side, progress=progress)

        deadline = time.monotonic() + float(timeout)
        started = time.monotonic()
        for entry in prepared:                          # 先**全部**发出去（同时动的前提）
            self._send_one(entry, progress)

        while True:
            pending = [entry for entry in prepared
                       if entry["state"] not in ("reached", "failed")]
            if not pending:
                break
            for entry in pending:
                was = entry["state"]
                self._sample(entry, stall_window)
                if entry["state"] == "reached" and was != "reached" and progress is not None:
                    peak = max(abs(value) for value in (entry["residual"] or [0.0]))
                    progress(f"        {self._label(entry['kind'], entry['side'])}已到位"
                             f"（第 {entry['attempts']} 次下发，残差 {peak:.2f}°）")
            if time.monotonic() >= deadline:
                break
            resend = [entry for entry in pending
                      if entry["state"] == "stalled" and entry["attempts"] <= int(retries)]
            for entry in resend:                        # 卡滞的**单独**重发（不牵连其他条）
                self._send_one(entry, progress)
            self.spin(poll)

        return self._batch_result(prepared, deadline - started)

    def _send_one(self, entry: dict, progress) -> None:
        """发一条目标，并把这一条的结果记进 `entry`。"""
        entry["attempts"] += 1
        sent = self._send_target(entry["kind"], entry["side"], entry["target"], progress)
        entry["matched"] = sent.get("matched", -1)
        if not sent.get("ok"):
            entry["state"] = "failed"
            entry["reason"] = sent.get("reason") or "没有发出去"
        elif entry["state"] in ("pending", "failed"):
            entry["state"] = "moving"

    def _sample(self, entry: dict, stall_window: float) -> None:
        """采一次这一条的残差，更新它的状态（reached / moving / stalled / nodata）。"""
        residual, ok = self._residual(entry["kind"], entry["side"], entry["target"])
        if not ok:
            entry["state"] = "nodata"
            return
        entry["residual"] = residual
        peak = max(abs(value) for value in residual)
        if peak <= entry["tol"]:
            entry["state"] = "reached"
            return
        if entry["best"] is None or peak < entry["best"] - 0.05:
            entry["best"] = peak
            entry["moved_at"] = time.monotonic()
        entry["state"] = ("moving" if time.monotonic() - entry["moved_at"] < stall_window
                          else "stalled")

    def _batch_result(self, prepared, elapsed: float) -> dict:
        """把逐条状态汇成总结果 + 人话原因（**每条分别说**，不混成一句）。"""
        items, reached, unconfirmed, failed = {}, [], [], []
        for entry in prepared:
            label = self._label(entry["kind"], entry["side"])
            peak = max((abs(value) for value in (entry["residual"] or [])), default=None)
            if entry["state"] == "reached":
                ok, reason = True, ""
                reached.append(f"{label}（残差 {peak:.2f}°，发 {entry['attempts']} 次）")
            elif entry["state"] == "failed":
                ok, reason = False, entry["reason"]
                failed.append(f"{label}（{entry['reason']}）")
            elif entry["state"] == "nodata":
                ok, reason = None, f"读不到{label}的当前角度 —— 是否到位**未经确认**"
                unconfirmed.append(f"{label}（读不到当前角度）—— 是否到位未经确认")
            elif entry["state"] == "stalled":
                ok, reason = False, (f"{label}下发 {entry['attempts']} 次后仍未见动作"
                                     + (f"（最大还差 {peak:.1f}°）" if peak is not None else ""))
                failed.append(reason)
            else:
                ok, reason = False, (f"{label}在 {elapsed:.1f}s 内没走到位"
                                     + (f"（最大还差 {peak:.1f}°）" if peak is not None else ""))
                failed.append(reason)
            items[entry["key"]] = {"ok": ok, "reason": reason, "residual": entry["residual"],
                                   "attempts": entry["attempts"],
                                   "matched": entry["matched"], "state": entry["state"]}
        if failed:
            overall, reason = False, "；".join(failed)
        elif unconfirmed:
            overall, reason = None, "；".join(unconfirmed)
        else:
            overall, reason = True, ""
        return {"ok": overall, "items": items, "elapsed": elapsed, "reason": reason,
                "reached_text": "、".join(reached)}

    @staticmethod
    def _label(kind: str, side) -> str:
        if kind == "trunk":
            return "躯干"
        return "左臂" if side == "left" else "右臂"

    def _send_target(self, kind, side, target, progress) -> dict:
        """发一条运动目标（一次发布，含等订阅者）。"""
        if kind == "trunk":
            # 先设速率、再发目标（v6.3.5）。返回值**刻意丢掉**：速率发不出去不该
            # 挡住目标 —— 它是"怎么动"，不是"动到哪"；失败会经 `progress` 记进日志。
            self.publish({
                "name": "躯干限速", "topic": "/trunk_control/speed_rate",
                "type": "std_msgs/msg/Float32",
                "payload": {"data": TRUNK_SPEED_RATE}}, progress)
            return self.publish({
                "name": "躯干目标", "topic": "/trunk_control/joint_target_position",
                "type": "std_msgs/msg/Float32MultiArray",
                # `layout.data_offset = 1` 是必需的（v6.3.3）：缺省 0 时这条命令
                # 会"发出去但机构不动"。见 `build_message` 里那一段的理由。
                "payload": {"layout": {"data_offset": TRUNK_LAYOUT_OFFSET},
                            "data": target}}, progress)
        positions = deg_to_radians(target)
        return self.publish({
            "name": f"{'左' if side == 'left' else '右'}臂目标",
            "topic": f"/arm_control/{side}_arm_move",
            "type": "wheeled_robot_msgs/msg/ArmMove",
            "payload": {"scale": 0.4, "position": positions}}, progress)

    # ---------- 模式前置条件 ----------
    def ensure_trunk_non_realtime(self, progress=None) -> dict:
        """把躯干切到**非实时**（`motor_mode 0` + 重新 `connect`）。

        依据：机器人侧 README 的躯干示例就是 `motor_mode {0x00}` → `connect true` →
        再发点位目标；`connect_sequence.py` 也写着"下使能即自动切非实时"。
        **改完 motor_mode 必须重发 connect 才生效**（文档原文）。

        **每一步之后都要停下来等够**（v6.3.3，对齐现场使能脚本的时序）：
        `swich_pown_on_test/swich_pown_on.sh` 是 `motor_mode → sleep 3 → connect →
        sleep 3` 之后才发第一条目标。改模式会让节点**重新连一遍电机**，太早发目标
        会被丢掉 —— 而丢掉之后**没有任何报错**（`trunk_state` 照常有读数），
        表现就是现场日志里那句「躯干下发 3 次后仍未见动作」。
        原先只等 0.5s，是照"发一条命令"的节奏估的，不是照"节点重连电机"的节奏。
        """
        steps = [
            {"name": "躯干切非实时", "topic": "/trunk_control/motor_mode",
             "type": "std_msgs/msg/Byte", "payload": {"data": TRUNK_MODE_NON_REALTIME}},
            {"name": "躯干重新使能", "topic": "/trunk_control/connect",
             "type": "std_msgs/msg/Bool", "payload": {"data": True}},
        ]
        results = []
        for step in steps:
            results.append(self.publish(step, progress))
            if not results[-1]["ok"]:
                return {"ok": False, "steps": results,
                        "reason": results[-1]["reason"]}
            self.spin(MODE_SWITCH_SETTLE)
        return {"ok": True, "steps": results, "reason": ""}

    def ensure_arm_point_mode(self, side, progress=None) -> dict:
        """让某侧手臂回到"能接受 `ArmMove`"的状态：退出 follow / servoj。

        两件事都是幂等的（本来就是 false 时再发一次 false 没有副作用），
        所以每次运动前发一遍最省心 —— `*_arm_move` 在 follow 模式下会被**静默忽略**，
        这条失败没有任何报错，只能靠"先退出"规避。
        """
        results = []
        for suffix in ("follow_position_set", "servoj_set"):
            topic = f"/arm_control/{side}_arm_{suffix}"
            results.append(self.publish({
                "name": f"{'左' if side == 'left' else '右'}臂退出{suffix.split('_')[0]}",
                "topic": topic, "type": "std_msgs/msg/Bool",
                "payload": {"data": False}, "wait_sub": 0.6}, progress))
        self.spin(0.15)
        return {"ok": all(item["ok"] for item in results), "steps": results,
                "reason": "" if all(item["ok"] for item in results)
                else "退出跟随模式时没找到订阅者"}

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "trunk": self.trunk,
            "trunk_age": (time.monotonic() - self.trunk_at) if self.trunk else None,
            "arms": {side: self.arms.get(side) for side in ("left", "right")},
            "arm_connected": dict(self.arm_connected),
            "trunk_msgs": self.trunk_msgs,
            "arm_msgs": self.arm_msgs,
            "errors": self.errors,
        }


# ============ 消息类型装载 ============
def load_types() -> dict:
    """装上消息类。缺哪个（比如没装 `wheeled_robot_msgs`）就在表里少哪个。"""
    from std_msgs.msg import (Bool, Byte, Float32, Float32MultiArray,
                              Float64MultiArray)

    types = {"Bool": Bool, "Byte": Byte, "Float32": Float32,
             "Float32MultiArray": Float32MultiArray,
             "Float64MultiArray": Float64MultiArray, "_raw": {}}
    try:
        from wheeled_robot_msgs.msg import ArmMove, ArmPosition, HandPosture
        types.update({"ArmMove": ArmMove, "ArmPosition": ArmPosition,
                      "HandPosture": HandPosture})
    except ImportError as exc:                  # 仿真/缺包时降级，手臂相关命令会明确报错
        log(f"没有 wheeled_robot_msgs（{exc}）—— 手臂与手型命令不可用")

    try:
        from wheeled_robot_msgs.msg import ArmState, TrunkState
        types.update({"ArmState": ArmState, "TrunkState": TrunkState})
    except ImportError:
        pass

    for name in MSG_TYPES + ("wheeled_robot_msgs/msg/ArmPosition",):
        simple = name.split("/")[-1]
        if simple in types:
            types["_raw"][name] = types[simple]
    return types


def handle(bridge: Bridge, request: dict, progress=None) -> dict:
    """执行一条请求，返回回复体（不含 `id`）。"""
    op = request.get("op")
    if op == "ping":
        return {"ok": True, "version": PROTOCOL_VERSION}
    if op == "publish":
        items = [bridge.publish(item, progress) for item in request.get("items", [])]
        return {"ok": all(item["ok"] for item in items), "items": items}
    if op == "move":
        target = request.get("target") or []
        expect = TRUNK_AXIS_COUNT if request.get("kind") == "trunk" else ARM_AXIS_COUNT
        if len(target) != expect:
            return {"ok": False, "reason": f"{request.get('kind')} 目标应是 {expect} 个角度值"}
        return bridge.move(request.get("kind"), target, side=request.get("side"),
                           tol=request.get("tol"), timeout=request.get("timeout", MOVE_TIMEOUT),
                           retries=request.get("retries", MOVE_RETRIES),
                           progress=progress)
    if op == "move_batch":
        # 一次发多条、**一起等**（左右臂的镜像动作要同时开始）—— 见 `move_batch` 的说明
        items = request.get("items") or []
        if not items:
            return {"ok": False, "reason": "move_batch 至少要有一条目标"}
        for item in items:
            kind, side = item.get("kind"), item.get("side")
            if kind not in ("trunk", "arm"):
                return {"ok": False, "reason": f"kind 只能是 trunk / arm，收到 {kind!r}"}
            if kind == "arm" and side not in ("left", "right"):
                return {"ok": False, "reason": f"side 只能是 left / right，收到 {side!r}"}
            expect = TRUNK_AXIS_COUNT if kind == "trunk" else ARM_AXIS_COUNT
            if len(item.get("target") or []) != expect:
                return {"ok": False, "reason": f"{kind} 目标应是 {expect} 个角度值"}
        return bridge.move_batch(items, timeout=request.get("timeout", MOVE_TIMEOUT),
                                 retries=request.get("retries", MOVE_RETRIES),
                                 progress=progress)
    if op == "ensure":
        what = request.get("what")
        if what == "trunk_non_realtime":
            return bridge.ensure_trunk_non_realtime(progress)
        if what == "arm_point_mode":
            side = request.get("side")
            if side not in ("left", "right"):
                return {"ok": False, "reason": f"side 只能是 left / right，收到 {side!r}"}
            return bridge.ensure_arm_point_mode(side, progress)
        return {"ok": False, "reason": f"不认识的 what：{what!r}"}
    if op == "state":
        return {"ok": True, "state": bridge.state()}
    return {"ok": False, "reason": f"不认识的 op：{op!r}"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(__doc__ or "")
        return 0

    try:
        import rclpy
        from rclpy.node import Node
    except ImportError as exc:
        emit({"ok": False, "fatal": True, "version": PROTOCOL_VERSION,
              "reason": f"无法导入 rclpy（{exc}）—— 请先 source install/setup.bash"})
        return 2
    try:
        types = load_types()
    except ImportError as exc:
        emit({"ok": False, "fatal": True, "version": PROTOCOL_VERSION,
              "reason": f"无法导入 std_msgs（{exc}）"})
        return 2

    rclpy.init(args=[item for item in argv if not item.startswith("--")])
    node = Node("data_collector_command_bridge")
    bridge = Bridge(node, types)
    bridge.subscribe_states()
    #: 等状态首帧：本进程是长驻的，这一次等待换来的是一整天都不用再等。
    #: **这一步必须被兜住** —— 现场那条崩溃正是在这里冒出来的（回调处理关节角时抛异常，
    #: 没人接 → 进程退出码 1），而它在主循环的 try 之外。
    try:
        bridge.spin(STATE_WAIT)
    except Exception as exc:                    # noqa: BLE001
        import traceback
        log(traceback.format_exc())
        bridge.errors += 1
        log(f"等首帧时出错（继续起，判据会如实报「未确认」）：{exc}")
    emit({"ok": True, "event": "ready", "version": PROTOCOL_VERSION,
          "trunk_text": "有" if bridge.trunk else "无",
          "arm_msgs": bridge.arm_msgs, "errors": bridge.errors})
    log(f"就绪：publisher {len(bridge.pubs)} 个，躯干状态 {'有' if bridge.trunk else '无'}，"
        f"手臂状态 {bridge.arm_msgs} 条，回调错误 {bridge.errors} 次")

    try:
        while True:
            ready, _w, _x = select.select([sys.stdin], [], [], 0.5)
            if not ready:
                # 父进程没了（EOF 之后 select 会一直可读，由 readline 处理）或空闲太久
                if time.monotonic() - _LAST[0] > IDLE_TIMEOUT:
                    log("空闲超时，退出")
                    return 0
                continue
            line = sys.stdin.readline()
            if not line:                        # EOF：上位机退了
                log("stdin 结束，退出")
                return 0
            line = line.strip()
            if not line:
                continue
            _LAST[0] = time.monotonic()
            try:
                request = json.loads(line)
            except ValueError as exc:
                emit({"ok": False, "reason": f"请求不是 JSON：{exc}"})
                continue
            request_id = request.get("id")
            if request.get("op") == "shutdown":
                emit({"id": request_id, "ok": True})
                return 0

            def progress(text, _id=request_id):
                emit({"id": _id, "event": "progress", "line": text})

            try:
                reply = handle(bridge, request, progress)
            except Exception as exc:            # noqa: BLE001 —— 一条命令炸了不能带走进程
                import traceback
                log(traceback.format_exc())
                reply = {"ok": False, "reason": f"通道内部错误：{exc}"}
            reply["id"] = request_id
            reply.setdefault("channel", "常驻通道")
            emit(reply)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            node.destroy_node()
            rclpy.shutdown()
        except Exception:                       # noqa: BLE001
            pass


#: 最后一次收到请求的时刻（空闲超时用；模块级是为了 `main` 里的闭包能改它）
_LAST = [time.monotonic()]


if __name__ == "__main__":
    sys.exit(main())
