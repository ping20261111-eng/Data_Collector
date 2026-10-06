"""姿势控制的数据层与下发编排：轴表 / 量程 / 预设读取 / 下发条目生成 / 逐步执行器。

**这里每个常量都对应一处可查的原文**（不是从印象里写的）：

- `wheeled_robot_pkg/doc/轮式机器人SDK接口_202603.pdf`：话题表、每轴量程、模式语义；
- `install/product/config/arm_motion.json` / `trunk_motion.json`：现场在用的预设；
- `teach_script/rt_trunk_publisher.py` 与 `shell-switch_pown_on_test/p2-p3.py`：**实时模式**的完整用法
  （`connect` → `motor_mode 0x01` → 再 `connect` → 上位机自己插值、逐条发目标）。
  **v6.1.1 起本页不再走实时**（现场 2026-09-28 要求改回非实时），这条留作备用通道的原文依据；
- `sample/src/joy_event_receiver/src/arm_proxy.cpp`：手臂实时流的两套话题
  （`*_arm_follow_position_set/_update` 与 `*_arm_servoj_set/_update`，8ms 一条 = 125Hz）。
  同样**当前未启用**，留作备用；
- `install/share/robot_description/urdf/wheel_robot.urdf` 与
  `install/share/product/products/*/robot_description/urdf/wheel_robot.urdf`（AR5 链）：
  **示意图用的杆长与关节轴**（v6.0.0 起示意图走正运动学，不再手画）；
- `sample/src/wheeled_robot_msgs/msg/`：`ArmMove` / `ArmPosition` / `HandPosture` / `ArmState` 定义；
- `scripts/connect_sequence.py`（现场验证过的上电时序）。

**七条不能弄错的约定**：

1. **单位**：界面与预设文件一律用**度**；`ArmMove.position` 与 `*_arm_follow_position_update` 是**弧度**
   （消息定义原文），换算只在 `arm_move_payload` / `deg_to_radians` 两处做。混用的表现是
   "手臂飞到没见过的姿势"。
2. **轴名不带中文**（现场要求）：躯干 `H1`~`H6`（下 → 上）、双臂 `L1`~`L7` / `R1`~`R7`
   （从肩起）、手 `F1`~`F6`。实体含义（肩/肘/腕、小指…）只放在悬停提示里。
3. **非实时模式（v6.1.1 起全部走这条）**：躯干一条 `Float32MultiArray`（**度**）进去，
   由**控制节点内部插值**；手臂一条 `ArmMove`（**弧度** + `scale`），由臂内部插值。
   于是两条约束回来了：
   * **两条躯干目标之间 ≥1.5s**（文档要求，`TRUNK_TARGET_GAP_SEC`，由 `MotionRunner` 等够）；
   * 完成判据是"等插值时长 / 等 `*_arm_moving` 归 false"，**不是**"脚本退出"。
   **页面进入即非实时、不需要用户做任何连接操作**：本页不主动切模式（现场口径），
   所以也不做那个会动机构的"重连"动作。模式**无法回读**，页面只说"本页按非实时模式下发"。
4. **实时流是备用通道**（v6.1.1 起本页不用）：`scripts/motion_stream.py` + `run_motion_streams`
   仍然完整保留、单测齐全 —— `ros2 topic pub --once` 一条要 0.3~1s，**发不出 100Hz**，
   将来若再切回实时就靠它。`MotionRunner` 的 `streams` 分支同样保留。
   一次性指令（急停 / connect / `hand_posture` / `ArmMove` / 躯干点位目标）走 `_publish_topics`。
5. **手臂若走实时流（`follow_position` / `servoj`）用完必须关**：`*_arm_*_set=false` 少了它，
   `*_arm_move` 会被忽略（SDK 原文：`left_arm_move` 只在"非 follow position 模式"生效）——
   表现是"预设点了没反映"。脚本在 `finally` 里关，异常与 Ctrl+C 也走那里。
6. **越界只拦下、不静默钳位**：钳位会让现场以为自己填的值生效了，实际被悄悄改掉。
7. **"完成"必须有判据**：躯干等插值时长、手臂等 `*_arm_moving` true→false（同教学脚本）、
   手等 `wait_ms`；走实时流时判据是"脚本正常退出"。**判据取不到就如实说"未经确认"**，
   不假装成功（与"写文件没有退出码 → 回读才是判据"是同一条纪律）。
"""
import math
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .core import load_yaml, logger, save_yaml

# ============ 轴表与量程（SDK 文档原文） ============
#: 躯干 6 轴，顺序与 `trunk_state.joint_position` / SDK 文档一致：脚踝 → 颈部。
#: 元组是 ``(界面名, 下限, 上限, 实体含义)`` —— 界面名不带中文，实体含义只进悬停提示。
TRUNK_AXES = (
    ("H1", -85.0, 85.0, "脚踝"),
    ("H2", -150.0, 150.0, "膝关节"),
    ("H3", -95.0, 95.0, "大腿"),
    ("H4", -90.0, 90.0, "腰"),
    ("H5", -90.0, 90.0, "颈 · 转头"),
    ("H6", -30.0, 30.0, "颈 · 抬头"),
)

#: 单臂 7 轴的量程（顺序从肩到腕；第 4 轴是肘，第 7 轴是腕 —— 与下发数组顺序一致）
_ARM_RANGES = ((-176.0, 176.0), (-118.0, 118.0), (-176.0, 176.0), (-58.0, 143.0),
               (-176.0, 176.0), (-58.0, 58.0), (-58.0, 58.0))
_ARM_HINTS = ("肩 1", "肩 2", "肩 3", "肘", "腕 1", "腕 2", "腕 3")

#: 侧别 → 轴名前缀（`L1` / `R1`）
ARM_PREFIXES = {"left": "L", "right": "R"}
ARM_NAMES = {"left": "左臂", "right": "右臂"}

#: 手 6 指：`hand_posture.position` 的语义（灵巧手，0~1000）
HAND_FINGER_HINTS = ("小指", "无名指", "中指", "食指", "大拇指", "大拇指翻转")
HAND_FINGERS = tuple(f"F{index}" for index in range(1, 7))
HAND_RANGE = (0.0, 1000.0)
#: 灵巧手的 Modbus ID（`teach_arm_publisher.py` 的常量：左 0x28 / 右 0x27）
HAND_IDS = {"left": 0x28, "right": 0x27}
#: `HandPosture.side` 取值
HAND_SIDE_IDS = {"left": 0, "right": 1}

# ============ 话题与消息类型 ============
TRUNK_TARGET_TOPIC = "/trunk_control/joint_target_position"
TRUNK_TARGET_MSG = "std_msgs/msg/Float32MultiArray"
TRUNK_SPEED_TOPIC = "/trunk_control/speed_rate"
TRUNK_SPEED_MSG = "std_msgs/msg/Float32"
TRUNK_MODE_TOPIC = "/trunk_control/motor_mode"
TRUNK_MODE_MSG = "std_msgs/msg/Byte"
TRUNK_CONNECT_TOPIC = "/trunk_control/connect"
BOOL_MSG = "std_msgs/msg/Bool"
ARM_MOVE_TOPICS = {"left": "/arm_control/left_arm_move",
                   "right": "/arm_control/right_arm_move"}
ARM_MOVE_MSG = "wheeled_robot_msgs/msg/ArmMove"
HAND_POSTURE_TOPIC = "/arm_control/hand_posture"
HAND_POSTURE_MSG = "wheeled_robot_msgs/msg/HandPosture"

#: 非实时模式（0 = 控制节点内部插值；v6.0.0 起本页不再用它，只留作对照与脚本参数）
#: 躯干**速度倍率**（`speed_rate`，Float32，0~1.0，**仅非实时模式有效**）。
#:
#: 为什么每次下发躯干目标都要显式带一条（v6.3.5）：现场示教脚本
#: `teach_script/teach_arm_publisher.py` 的 `publish_trunk_position()` 就是
#: **目标 + 紧挨着一条 speed_rate** —— 那是现场手里唯一一个"每次发目标都带上它"的样本。
#: 文档只写了 C++ SDK 侧同名参数默认 1.0，**ROS 节点侧的默认值没有任何文档**；
#: 显式发 1.0 等于把这个未知量钉成已知（1.0 就是文档给的默认值，所以不会比现状更快）。
TRUNK_SPEED_RATE = 1.0

TRUNK_MODE_NON_REALTIME = 0
#: 实时模式（1 = 关节按允许的最大速度走，**目标由上位机自己插值** —— 文档原文）
TRUNK_MODE_REALTIME = 1

# ============ 实时流（v6.0.0） ============
#: 实时流脚本（项目 `scripts/` 下；由 `ConfigManager.resolve_script_path` 定位）
MOTION_STREAM_SCRIPT = "motion_stream.py"
#: 躯干实时流的发布频率（Hz）：与 `p2-p3.py` 的 100Hz 一致
TRUNK_STREAM_RATE = 100
#: 手臂实时流（follow_position）的频率（Hz）：官方 `arm_proxy.cpp` 是 8ms 一条 = 125Hz
ARM_STREAM_RATE = 125
#: 一条躯干实时流的默认时长（秒）—— 越短越"急"，越长越"柔"
TRUNK_STREAM_SEC = 2.5
#: 一条手臂实时流的默认时长（秒）
ARM_STREAM_SEC = 1.6
#: 躯干到位后按目标保持多久（秒）：p2-p3.py 是 3s，这里取"够控制器收敛"即可
TRUNK_HOLD_SEC = 0.8
#: 等一条实时流结束的上限（秒）：时长 + 保持 + 脚本启动与 DDS 发现的开销
STREAM_TIMEOUT_PAD = 25.0
#: 手臂实时流（follow_position）话题：`_set` 是 Bool 开关，`_update` 是 7 弧度的
#: `Float64MultiArray`（**绝对角**，不是增量）。与脚本里的 `ARM_FOLLOW_*_SUFFIX` 由
#: `test_motion.py` 的契约用例钉成一致 —— 两处名字漂了会变成最难查的形态。
ARM_FOLLOW_SET_TOPICS = {"left": "/arm_control/left_arm_follow_position_set",
                         "right": "/arm_control/right_arm_follow_position_set"}
ARM_FOLLOW_UPDATE_TOPICS = {"left": "/arm_control/left_arm_follow_position_update",
                            "right": "/arm_control/right_arm_follow_position_update"}
#: 手臂 servoJ 开关（`Bool`）。与 follow 一样，**它开着时 `*_arm_move` 也会被忽略** ——
#: 遥操作/手柄的 M4 键与 `arm_proxy.cpp` 的 `runServoJ()` 用的就是这条。走点位目标之前
#: 两条都要关（通道里由 `ensure_arm_point_mode` 做，一次性 CLI 那条路由执行器做）。
ARM_SERVOJ_SET_TOPICS = {"left": "/arm_control/left_arm_servoj_set",
                         "right": "/arm_control/right_arm_servoj_set"}
FLOAT64_ARRAY_MSG = "std_msgs/msg/Float64MultiArray"
#: 去掉实时流之后，剩下的那次固定等待（脚本退出后给控制器一点收敛时间）
STREAM_SETTLE_SEC = 0.3

# ============ 时序取值 ============
#: 两条躯干目标之间的最小间隔（文档原文：非实时模式建议 ≥1.5s）
TRUNK_TARGET_GAP_SEC = 1.5
#: 一条躯干目标之后的基础等待（节点内部插值，期间不接受下一条）
TRUNK_SETTLE_SEC = 1.5
#: 等手臂 `*_arm_moving` 归 false 的上限（与教学脚本的 10s 一致）
ARM_MOVE_TIMEOUT_SEC = 10.0
#: 下发后多久还没看到 `moving=True` 就不再等它（改为固定等待并**如实标注未确认**）
ARM_MOVE_SETTLE_SEC = 1.5
#: 未观察到手臂运动标志时的固定等待（兜底，也用于监控没跑的场景）
ARM_FIXED_WAIT_SEC = 1.5
#: 只动手（灵巧手）时等多久：`wait_ms` 缺省值
HAND_WAIT_MS = 500
#: `ArmMove.scale` 的合法区间（消息定义原文 0.01~1）
SCALE_RANGE = (0.01, 1.0)
DEFAULT_SCALE = 0.4
#: 预设走一步时，单条 `ros2 topic pub` 的等待上限（秒）
#: 一条一次性下发的等待上限（秒）。**含 `-w 1` 的等待**（等匹配到订阅者再发）——
#: 图刚变动、或机器人在仿真/重载时，发现要好几秒；6s 太紧会把"其实发得出去"的
#: 那几条判成超时（现场表现为"预设成功率低"）。给到 10s。
PUBLISH_TIMEOUT = 10.0


def arm_axes(side: str) -> Tuple[Tuple[str, float, float, str], ...]:
    """一侧手臂的 7 条轴记录（``(轴名, 下限, 上限, 实体含义)``）。"""
    prefix = ARM_PREFIXES[side]
    return tuple((f"{prefix}{index + 1}", low, high, hint)
                 for index, ((low, high), hint) in enumerate(zip(_ARM_RANGES, _ARM_HINTS)))


#: 双臂轴表（键是 ``"left"`` / ``"right"``）
ARM_AXES = {side: arm_axes(side) for side in ARM_PREFIXES}


# ============ 数值格式化与 payload ============
def fmt_number(value: float) -> str:
    """把角度写成**短而准**的字面量：``22.3`` / ``-0.05`` / ``0``。

    为什么不直接 `str(float)`：`22.3` 会变成 `22.300000000000001`，拼进
    `ros2 topic pub` 的 YAML 里既难看又让人怀疑精度；四位小数足够表达 0.1° 级别的输入。
    """
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_list(values: Sequence[float]) -> str:
    return "[" + ", ".join(fmt_number(item) for item in values) + "]"


#: 躯干目标 `Float32MultiArray` **必须带 `layout.data_offset = 1`**（v6.3.3）。
#:
#: 这是现场实测出来的根因：同一台机器、同一条「非实时 + 单条点位」的路，
#: **带 `data_offset: 1` 能动、不带（缺省 0）一动不动**。三份可用样本写法完全一致 ——
#: 机器人侧 `README.md`、现场测试助手 `Robot Test Assistant/modules/preset.py`、
#: 现场使能脚本 `shell-switch_pown_on_test/swich_pown_on.sh`：
#:
#:     {layout: {dim: [], data_offset: 1}, data: [-10, -18, -8, 0, 0, 0]}
#:
#: 我们原先只发 `data: [...]`（layout 缺省），所以命令"发出去了、订阅者也匹配了"，
#: 但机构不动 —— 这正是现场日志里「躯干下发 3 次后仍未见动作」的机制。
#: `dim` 三种样本里都是空表，所以这里也只设 `data_offset`。
TRUNK_LAYOUT_OFFSET = 1


def trunk_target_payload(values_deg: Sequence[float]) -> str:
    """`Float32MultiArray` 的 YAML payload（6 个**度**值 + 必需的 `layout`）。

    形状与机器人侧 README 的示例逐字对齐（外层 flow mapping、`dim` 空表）——
    这条字符串同时喂给两条路：常驻通道解析成消息对象、兜底路径直接拼进
    `ros2 topic pub`，两边看到的必须是同一个形状。
    """
    return (f"{{layout: {{dim: [], data_offset: {TRUNK_LAYOUT_OFFSET}}}, "
            f"data: {_fmt_list(values_deg)}}}")


def trunk_speed_payload(rate: float) -> str:
    """`Float32` 的 YAML payload（0~1.0 的限速比例）。"""
    return f"data: {fmt_number(rate)}"


def arm_move_payload(values_deg: Sequence[float], scale: float = DEFAULT_SCALE) -> str:
    """`ArmMove` 的 YAML payload —— **度进、弧度出**，换算只在这里做。

    一条消息就够（臂内部插值），所以 `targets` 只放一个目标；这与教学脚本
    `publish_left_arm_command` 完全一致。
    """
    radians = [float(deg) * 3.141592653589793 / 180.0 for deg in values_deg]
    return (f"{{targets: [{{position: {_fmt_list(radians)}}}], "
            f"scale: {fmt_number(scale)}}}")


def deg_to_radians(values_deg: Sequence[float]) -> List[float]:
    """度 → 弧度（`*_arm_follow_position_update` 与 `ArmMove.position` 都是弧度）。

    单独抽出来是因为实时流不是拼 YAML，而是要把一组**浮点数**交给脚本 ——
    换算规则只能有一处，否则两条路会各自漂。
    """
    return [float(deg) * 3.141592653589793 / 180.0 for deg in values_deg]


def trunk_stream_job(values_deg: Sequence[float], *,
                     duration: float = TRUNK_STREAM_SEC,
                     hold: float = TRUNK_HOLD_SEC,
                     rate: int = TRUNK_STREAM_RATE) -> dict:
    """一条躯干实时流任务（交给 `scripts/motion_stream.py` 执行）。

    躯干 6 个值一律是**度**；脚本内部负责 `connect → motor_mode 1 → connect`
    以及 100Hz 的缓动插值（为什么要上位机插值见模块 docstring 第 3 条）。
    """
    return {"kind": "trunk", "side": None,
            "values": [float(item) for item in values_deg],
            "duration": float(duration), "hold": float(hold), "rate": int(rate)}


def arm_stream_job(side: str, values_deg: Sequence[float], *,
                   duration: float = ARM_STREAM_SEC,
                   rate: int = ARM_STREAM_RATE) -> dict:
    """一条手臂实时流任务（follow_position：`*_arm_follow_position_set` + 125Hz update）。"""
    if side not in ARM_PREFIXES:
        raise ValueError(f"未知的一侧：{side!r}")
    return {"kind": "arm", "side": side,
            "values": [float(item) for item in values_deg],
            "duration": float(duration), "hold": 0.0, "rate": int(rate)}


def stream_job_summary(job: dict) -> str:
    """一条实时流任务的"要做什么"（日志与确认框用一句话）。"""
    if job["kind"] == "trunk":
        return (f"躯干实时流 → {'、'.join(fmt_number(v) for v in job['values'])}（度）"
                f"，{job['duration']:g}s，{job['rate']}Hz")
    return (f"{ARM_NAMES[job['side']]}实时流（follow_position）→ "
            f"{'、'.join(fmt_number(v) for v in job['values'])}（度）"
            f"，{job['duration']:g}s，{job['rate']}Hz")


def stream_job_key(job: dict) -> str:
    """一条实时流任务的唯一标识（结果字典的键、日志里的名字）。"""
    if job["kind"] == "trunk":
        return "躯干实时流"
    return f"{ARM_NAMES[job['side']]}实时流"


def stream_job_topic_values(job: dict) -> List[float]:
    """一条实时流**实际发到话题上**的数值。

    实时流脚本**不做任何单位换算**（它只管把 `--values` 原样发到话题字段上）——
    "度 → 弧度"这件事只在 `core_motion` 这一层发生一次（模块 docstring 第 1 条）。
    """
    if job["kind"] == "trunk":
        return list(job["values"])              # 躯干话题就是"度"
    return deg_to_radians(job["values"])        # 手臂 follow_position 是"弧度"


def stream_job_args(job: dict) -> List[str]:
    """拼成 `motion_stream.py` 的位置参数（不含脚本路径本身）。"""
    args = [job["kind"], "--values",
            ",".join(fmt_number(v) for v in stream_job_topic_values(job)),
            "--duration", fmt_number(job["duration"]), "--rate", str(int(job["rate"]))]
    if job["kind"] == "arm":
        args += ["--side", job["side"]]
    else:
        args += ["--hold", fmt_number(job["hold"]), "--prepare"]
    return args


def hand_posture_payload(side: str, position: Sequence[int]) -> str:
    """`HandPosture` 的 YAML payload（灵巧手 6 个 0~1000 的位置值）。"""
    return (f"{{side: {HAND_SIDE_IDS[side]}, id: {HAND_IDS[side]}, start_addr: 0, "
            f"position: {_fmt_list([int(item) for item in position])}}}")


def trunk_mode_payload(mode: int) -> str:
    """`Byte` 的 YAML payload（`motor_mode`）。"""
    return f"data: {int(mode)}"


# ============ 校验 ============
def check_axis_values(axes, values, label: str = "") -> List[str]:
    """逐个检查"这条轴的值合不合法"，返回**可直接给人看**的问题列表（空 = 通过）。

    只报问题、不改值：越界就拦下并说明范围（`TRUNK_AXES` 的取值范围来自 SDK 文档）。
    """
    problems: List[str] = []
    if len(values) != len(axes):
        return [f"{label}需要 {len(axes)} 个值，收到 {len(values)} 个"]
    for (key, low, high, _hint), value in zip(axes, values):
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{key} 不是数字：{value!r}")
            continue
        if number < low or number > high:
            problems.append(f"{key}（{_hint}）{fmt_number(number)} 超出范围 {low:g}~{high:g}°")
    return problems


def check_hand_values(values, label: str = "") -> List[str]:
    """灵巧手 6 个位置值：0~1000（协议范围）。"""
    if not isinstance(values, (list, tuple)) or not values:
        return [f"{label}位置值不是列表"]
    problems = []
    for index, value in enumerate(values, start=1):
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{label}第 {index} 个值不是数字：{value!r}")
            continue
        if number < HAND_RANGE[0] or number > HAND_RANGE[1]:
            problems.append(f"{label}第 {index} 个值 {fmt_number(number)} 超出 0~1000")
    return problems


def check_scale(scale) -> List[str]:
    """`ArmMove.scale` 必须在 0.01~1（消息定义原文；越界只拦不钳）。"""
    try:
        number = float(scale)
    except (TypeError, ValueError):
        return [f"速度比例不是数字：{scale!r}"]
    if number < SCALE_RANGE[0] or number > SCALE_RANGE[1]:
        return [f"速度比例 {fmt_number(number)} 超出 {SCALE_RANGE[0]:g}~{SCALE_RANGE[1]:g}"]
    return []


# ============ 预设读取：机器人侧两份 JSON（只读） ============
def _read_json(path: Path) -> dict:
    """读一份 JSON；返回 ``{"available": bool, "reason": str, "data": dict}``。

    外部文件一律当"外部输入"处理：不存在 / 半截内容 / 结构不对都要变成一句人话，
    绝不让异常冒到界面（页面在 QTimer 槽里读它，异常会让 PyQt 直接 abort）。
    """
    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"available": False, "reason": f"文件不存在：{path}", "data": {}}
    except OSError as exc:
        return {"available": False, "reason": f"读不出来（{exc}）：{path}", "data": {}}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {"available": False, "reason": f"不是合法 JSON（{exc}）：{path}", "data": {}}
    if not isinstance(data, dict):
        return {"available": False, "reason": f"顶层不是对象：{path}", "data": {}}
    return {"available": True, "reason": "", "data": data}


def read_trunk_presets(config) -> dict:
    """躯干预设（`trunk_motion.json`）：``{名字: [3 个度值]}`` → 补齐成 6 轴。

    文件里每个预设只有 3 个值（脚踝 / 膝 / 大腿），而躯干有 6 轴 —— 缺的 3 轴（腰 / 颈）
    按 **0** 补（"运动到这个姿势"意味着其余轴回中）。这一步在 `trunk_preset_steps` 里做，
    这里只做诚实读取：**不是列表、值不是数字的预设照样列出来，但标记为不可用**。
    """
    result = _read_json(config.trunk_motion_file)
    presets: Dict[str, dict] = {}
    for name, value in (result["data"] or {}).items():
        problems: List[str] = []
        if not isinstance(value, (list, tuple)) or not value:
            problems.append("值不是非空的数组（文件里应写成 [角度, 角度, 角度]）")
            values: List[float] = []
        else:
            values = []
            for index, item in enumerate(value, start=1):
                try:
                    values.append(float(item))
                except (TypeError, ValueError):
                    problems.append(f"第 {index} 个值不是数字：{item!r}")
            if not problems and len(values) != 3:
                problems.append(f"关节角个数是 {len(values)}，文件里应写 3 个"
                                f"（脚踝 / 膝 / 大腿），其余轴按 0 补")
        presets[str(name)] = {
            "name": str(name),
            "values": values,                    # 3 个度值（原样）
            "problems": problems,
            "source": "robot",
        }
    return {"available": result["available"], "reason": result["reason"],
            "path": str(config.trunk_motion_file), "presets": presets}


def read_arm_presets(config) -> dict:
    """手臂预设（`arm_motion.json`）：``{名字: [步骤…]}``，每步可含双臂 / 双手 / scale / wait_ms。

    多步序列（Y 5 步、LB 8 步）按**原样**保留步序 —— 现场就是靠这个顺序做动作的，
    合并或去重都会改变轨迹。
    """
    result = _read_json(config.arm_motion_file)
    presets: Dict[str, dict] = {}
    for name, value in (result["data"] or {}).items():
        problems: List[str] = []
        if not isinstance(value, (list, tuple)) or not value:
            problems.append("值不是非空的步骤数组")
            steps: List[dict] = []
        else:
            steps = [step for step in value if isinstance(step, dict)]
            if len(steps) != len(value):
                problems.append("有步骤不是对象（每步应是含 left_arm / right_arm… 的对象）")
        presets[str(name)] = {
            "name": str(name),
            "steps": steps,
            "problems": problems,
            "source": "robot",
        }
    return {"available": result["available"], "reason": result["reason"],
            "path": str(config.arm_motion_file), "presets": presets}


# ============ 自定义预设：上位机自己维护的那一份 ============
CUSTOM_PRESETS_HEADER = """\
# 姿势控制 · 自定义预设（上位机自己维护，**不写回机器人侧**）
#
# 为什么另存一份：install/product/config/*_motion.json 是产品交付物 —— 示教脚本、手柄、
# 别的版本界面都读它，从上位机里改一个预设不该把它们的语义一起改掉，也不该被下次交付覆盖。
#
# 写法（值都是**度**）：
#   trunk:                      # 躯干：6 个值（H1 脚踝 → H6 颈抬头）
#     我的站直: [0, 0, 0, 0, 0, 0]
#   arm:                        # 手臂：步骤数组，每步可只写要动的部位
#     我的挥手:
#       - left_arm: [62.2, 68.3, -22.6, 22.3, 31.6, 17.3, 0]
#         left_end_effector: [100, 100, 100, 100, 500, 600]
#         scale: 0.4
#         wait_ms: 800
"""


def load_custom_presets(config) -> dict:
    """读上位机的自定义预设；文件不存在时返回空表（**不**创建，等用户真的存一次再建）。"""
    path = config.get_motion_presets_file()
    empty = {"trunk": {}, "arm": {}, "path": str(path), "reason": "", "available": False}
    try:
        path.read_text(encoding="utf-8")          # 只探"能不能读"，内容交给 load_yaml
    except FileNotFoundError:
        return empty
    except OSError as exc:
        return dict(empty, reason=f"读不出来（{exc}）")
    try:
        data = load_yaml(path)
    except Exception as exc:                       # noqa: BLE001 —— 外部文件，什么都可能
        return dict(empty, reason=f"不是合法 YAML（{exc}）")
    if not isinstance(data, dict):
        return dict(empty, reason="顶层不是映射（应是 trunk: / arm: 两个键）")

    out = dict(empty, available=True)
    for kind in ("trunk", "arm"):
        entries = data.get(kind)
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            out[kind][str(name)] = value
    return out


def save_custom_preset(config, kind: str, name: str, value) -> str:
    """写入（或覆盖）一条自定义预设，返回写到的文件路径。

    整份重写（这份文件**只有上位机自己写**，不存在"保留别人的注释"的问题），
    但走"临时文件 + `os.replace`"：中途断电不会留下半截文件。
    首次创建时把写法说明（注释）一起写进去 —— 现场要照着它手改文件。
    """
    import os

    if kind not in ("trunk", "arm"):
        raise ValueError(f"未知的预设类别: {kind}")
    path = config.get_motion_presets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_custom_presets(config)
    data = {"trunk": dict(current["trunk"]), "arm": dict(current["arm"])}
    data[kind][name] = value
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        # 说明注释与内容**写进同一个临时文件**：先写 path 再 replace 会把注释冲掉
        # （踩过：文件创建出来了，但打开一看只有一行 `trunk:`，写法说明没了）
        save_yaml(temp, data)
        if not path.exists():
            temp.write_text(CUSTOM_PRESETS_HEADER + temp.read_text(encoding="utf-8"),
                            encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return str(path)


def delete_custom_preset(config, kind: str, name: str) -> bool:
    """删掉一条自定义预设；返回是否真的删掉了（原本没有 = False）。"""
    import os

    path = config.get_motion_presets_file()
    current = load_custom_presets(config)
    if name not in current.get(kind, {}):
        return False
    data = {"trunk": dict(current["trunk"]), "arm": dict(current["arm"])}
    data[kind].pop(name, None)
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    save_yaml(temp, data)
    os.replace(temp, path)
    return True


def preset_overview(config) -> dict:
    """页面需要的预设视图：机器人侧（只读）+ 上位机自定义（可改）。

    名字撞车时**两份都留着**，自定义那条的显示名加「（上位机）」后缀 —— 覆盖掉一条
    交付里的预设是"静默改语义"，比多出一行更糟。返回的每个条目都带：

    * ``key``：字典键，唯一（撞车时自定义那条是 ``custom:名字``）；
    * ``name``：**显示用的名字**（可能是加了后缀的那个）；
    * ``source``：``"robot"`` / ``"custom"``。
    """
    trunk = read_trunk_presets(config)
    arm = read_arm_presets(config)
    custom = load_custom_presets(config)

    def merge(entries: Dict[str, dict], custom_values: dict) -> Dict[str, dict]:
        merged = {key: dict(item, key=key, name=item.get("name", key),
                            source=item.get("source", "robot"))
                  for key, item in entries.items()}
        for name, value in custom_values.items():
            key = str(name)
            display = key
            if key in merged:
                key = f"custom:{name}"
                display = f"{name}（上位机）"
            merged[key] = {"key": key, "name": display, "value": value,
                           "problems": [], "source": "custom"}
        return merged

    return {
        "trunk": {"presets": merge(trunk["presets"], custom["trunk"]),
                  "available": trunk["available"], "reason": trunk["reason"],
                  "path": trunk["path"]},
        "arm": {"presets": merge(arm["presets"], custom["arm"]),
                "available": arm["available"], "reason": arm["reason"],
                "path": arm["path"]},
        "custom": {"path": custom["path"], "reason": custom["reason"],
                   "available": custom["available"]},
    }


# ============ 正运动学（示意图用，实现住在 motion_kinematics.py） ============
# 这一段是纯数学（只依赖 math），单独成册见 `motion_kinematics.py` —— core_motion 到
# v6.0.0 已经 1099 行，过了本项目"单文件 1000 行"的红线。这里**再导出**一次，
# 让 `core_motion.fk_trunk` 这类旧写法（含测试里的 patch 口径）继续有效。
from .motion_kinematics import (ARM_CHAIN, ARM_MOUNTS, HAND_LENGTH,  # noqa: F401
                                TRUNK_CHAIN, fit_projection, fk_arm, fk_trunk,
                                map_point, project_front, project_side)


# ============ 预设 → 下发步骤 ============
def trunk_preset_steps(name: str, values_deg: Sequence[float], *,
                       speed_rate: Optional[float] = TRUNK_SPEED_RATE) -> List[dict]:
    """把一个躯干预设（3 个度值）摊成一条**非实时（点位）**步骤（补足 6 轴）。

    补齐规则与设计稿一致：``KneeBend_2 [45, 81, 36]`` → 6 轴 ``[45, 81, 36, 0, 0, 0]``
    ——"运动到这个姿势"包含把腰 / 颈转回中位。**照文件原样下发，不取反**（现场决定）。

    **v6.1.1 起躯干改回非实时模式**（现场 2026-09-28：「先将手臂和躯干的控制改回非实时模式
    控制吧，进入直接就是非实时模式，不需要重新连接」）：一条 `Float32MultiArray`（**度**）
    进去，控制节点内部插值。于是判据是"等插值时长"，并且**两条目标之间要隔 ≥1.5s**
    （文档要求，由 `MotionRunner` 统一负责）。
    实时流那条通道（`scripts/motion_stream.py`）**保留但本页不再使用** —— 见它的文件头。
    """
    padded = [float(item) for item in values_deg] + [0.0] * (len(TRUNK_AXES) - len(values_deg))
    # **限速在前、目标在后**（顺序不是随手定的）：
    #   节点若"读到目标时用当时的速率" → 必须先设，否则目标用旧速率；
    #   节点若"收到速率就应用到正在跑的运动" → 先设也没问题（速率先到、目标后到）。
    # 两种实现下"先设"都对，而"后设"在第一种下会失效 —— 所以取先设。
    entries: List[tuple] = []
    if speed_rate is not None:
        entries.append(("躯干限速", TRUNK_SPEED_TOPIC, TRUNK_SPEED_MSG,
                        trunk_speed_payload(speed_rate)))
    entries.append(("躯干目标", TRUNK_TARGET_TOPIC, TRUNK_TARGET_MSG,
                    trunk_target_payload(padded)))
    return [{"label": f"躯干预设 {name}", "entries": entries, "streams": [], "trunk": True,
             "arms": (), "hand": False, "wait_ms": None,
             "target": {"trunk": padded}}]


def arm_preset_steps(name: str, steps: Sequence[dict]) -> List[dict]:
    """把一个手臂预设（步骤数组）摊成下发步骤；每步只发 JSON 里写了的部位。

    一条 ArmMove 能带多个目标，但**这里是逐步发**：多步序列的语义就是"到一步、停一下、
    再到下一步"（LB 就是靠这个做出连贯动作），合成一条会变成一条直线插值到终点。
    """
    out: List[dict] = []
    for index, raw in enumerate(steps, start=1):
        entries: List[dict] = []
        arms: List[str] = []
        hand = False
        targets: Dict[str, list] = {}
        wait_ms = raw.get("wait_ms")
        try:
            wait_ms = int(wait_ms) if wait_ms is not None else None
        except (TypeError, ValueError):
            wait_ms = None
        scale = raw.get("scale", DEFAULT_SCALE)
        for side in ("left", "right"):
            values = raw.get(f"{side}_arm")
            if isinstance(values, (list, tuple)) and values:
                entries.append((f"{ARM_NAMES[side]}运动", ARM_MOVE_TOPICS[side], ARM_MOVE_MSG,
                                arm_move_payload(values, scale)))
                arms.append(side)
                targets[f"{side}_arm"] = [float(item) for item in values]
            positions = raw.get(f"{side}_end_effector")
            if isinstance(positions, (list, tuple)) and positions:
                entries.append((f"{ARM_NAMES[side]}手型", HAND_POSTURE_TOPIC, HAND_POSTURE_MSG,
                                hand_posture_payload(side, positions)))
                hand = True
                targets[f"{side}_end_effector"] = [int(item) for item in positions]
        label = f"{name} 第 {index}/{len(steps)} 步"
        out.append({"label": label, "entries": entries, "streams": [], "trunk": False,
                    "arms": tuple(arms), "hand": hand, "wait_ms": wait_ms,
                    "target": targets})
    return out


def trunk_target_step(label: str, values_deg: Sequence[float], *,
                      speed_rate: Optional[float] = None) -> dict:
    """单轴 / 整组的躯干目标 → 一条**非实时（点位）**步骤（补足 6 轴）。"""
    return trunk_preset_steps(label, values_deg, speed_rate=speed_rate)[0]


def arm_move_step(label: str, side: str, values_deg: Sequence[float], *,
                  scale: float = DEFAULT_SCALE) -> dict:
    """整臂 7 轴一次下发 → 一条**非实时**步骤（`ArmMove`，臂内部插值）。

    手动调节与预设**走同一条**（`ArmMove`）—— 那是手柄 / 示教共用的动作定义，
    同一个预设不该在界面与手柄上做出不同的运动。
    **v6.1.1 起手动调节也改回它**（现场 2026-09-28 要求改回非实时模式）；
    `ArmMove.position` 是**弧度**，换算走 `arm_move_payload`（只有那一处）。
    """
    return {"label": label,
            "entries": [(f"{ARM_NAMES[side]}运动", ARM_MOVE_TOPICS[side], ARM_MOVE_MSG,
                         arm_move_payload(values_deg, scale))],
            "streams": [], "trunk": False, "arms": (side,), "hand": False, "wait_ms": None,
            "target": {f"{side}_arm": [float(item) for item in values_deg]}}


def describe_step(step: dict) -> str:
    """一步的"要做什么"（日志/确认框用一句话）：动词 + 部位 + 值。"""
    parts = [stream_job_summary(job) for job in step.get("streams") or []]
    for name, _topic, _msg, payload in step.get("entries") or []:
        if name == "躯干限速":
            continue          # 速率是"怎么动"，不是"动到哪" —— 不进摘要
        if step["trunk"]:
            parts.append(f"躯干 → {payload.split('data: ')[-1]}")
        elif "手型" in name:
            parts.append(f"{name} {payload.split('position: ')[-1].rstrip('}')}")
        else:
            parts.append(f"{name}（弧度）")
    return "；".join(parts) if parts else "（没有可下发的条目）"


def check_trunk_values(values, name: str = "") -> List[str]:
    """一个躯干预设的目标值是否合法（6 轴，逐轴量程）。"""
    prefix = f"{name}：" if name else ""
    if not isinstance(values, (list, tuple)) or len(values) != len(TRUNK_AXES):
        return [f"{prefix}躯干目标应是 {len(TRUNK_AXES)} 个角度值"]
    return [f"{prefix}{item}" for item in
            check_axis_values(TRUNK_AXES, values)]


def check_arm_steps(steps: Sequence[dict], name: str = "") -> List[str]:
    """一组手臂预设步骤是否合法：逐轴的量程、速度比例、手型位置值。

    **只报问题、不改值**：预设文件（`arm_motion.json`）是现场可能手改的，
    越界就拦下并说明哪一步哪一轴越界 —— 静默钳位会让现场以为文件里的值生效了。
    """
    problems: List[str] = []
    prefix = f"{name} " if name else ""
    for index, step in enumerate(steps, start=1):
        head = f"{prefix}第 {index} 步："
        if not isinstance(step, dict):
            problems.append(f"{head}不是对象（每步应是含 left_arm / right_arm… 的对象）")
            continue
        for side in ("left", "right"):
            values = step.get(f"{side}_arm")
            if values is None:
                continue
            if not isinstance(values, (list, tuple)) or len(values) != 7:
                problems.append(f"{head}{ARM_NAMES[side]}应有 7 个角度值")
                continue
            problems.extend(f"{head}{item}" for item in
                            check_axis_values(ARM_AXES[side], values))
            positions = step.get(f"{side}_end_effector")
            if positions is not None:
                problems.extend(f"{head}{item}" for item in
                                check_hand_values(positions, f"{ARM_NAMES[side]}手型"))
        if "scale" in step and step["scale"] is not None:
            problems.extend(f"{head}{item}" for item in check_scale(step["scale"]))
    return problems


# ============ 逐步执行器（实现住在 motion_runner.py） ============
# 这一段是"怎么发、怎么确认"（要碰 launcher、要等状态），与上面的"要发什么"分开成册：
# core_motion 到 v6.2.0 又到了 1082 行，红线（1000 行）是 `test_ui_structure.py` 守着的。
# 下面**惰性再导出**，让 `core_motion.MotionRunner` 与 `from .core_motion import MotionRunner`
# 继续有效（界面、用例都不用改）。为什么惰性而不是直接 import：`motion_runner` 反向 import
# 本模块的常量，写死方向的 import 会在"先 import motion_runner"时循环导入报错。
def __getattr__(name):
    if name == "MotionRunner":
        from .motion_runner import MotionRunner
        return MotionRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
