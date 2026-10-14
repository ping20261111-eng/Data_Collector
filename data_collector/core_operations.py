import json
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple
from .errors import AppError, ErrorCode
from .core import (
    logger, run_in_terminal, run_background, run_background_captured,
    run_background_with_log, run_with_env, terminate,
    ConfigManager, ProcessManager, PROJECT_ROOT,
    load_calibrations, load_yaml, normalize_user,
)
# 姿势控制用到的常量与 payload 构造函数（话题名/消息类型/单位换算只有一处定义 —— 见 core_motion）
from .core_motion import (BOOL_MSG, MOTION_STREAM_SCRIPT, STREAM_TIMEOUT_PAD,
                          stream_job_args, stream_job_key)
# 常驻命令通道（v6.2.0）：姿态控制的下发走它，起不来就降级到一次性 CLI
from .ros_bridge import RosBridge


def _terminate_stream(proc: subprocess.Popen, force: bool = False) -> None:
    """结束一条实时流进程（整组）。

    与 `core.terminate` 的分工：那个是给 `ros2 launch` 那种"整棵树的常驻进程"用的（SIGINT）。
    实时流是一次几秒的短进程，且**中止必须让它有机会走 `finally`**（手臂要退出 follow_position，
    否则 `*_arm_move` 会被忽略）—— 所以先 SIGTERM（脚本装了处理器），3 秒不退再 SIGKILL。
    """
    if proc is None or proc.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            return
    try:
        proc.wait(timeout=1.0 if force else 3.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


class UserLookup(NamedTuple):
    """按名字查到的人。`source` 非空表示标定数据**不在当前工作空间**，来自那个文件。"""

    key: str                      # 配置文件里的原始键名（要写进 teleop.calibration 的那个）
    source: Optional[Path]        # 标定数据的来源文件；None = 本来就在当前工作空间
    entry: dict                   # 这条标定的参数（给日志 / 对话框显示）

    @property
    def imported(self) -> bool:
        return self.source is not None

    def summary(self) -> str:
        """一行参数摘要：现场最关心 tracker 组别与肩宽/臂长是否合理。"""
        parts = []
        tracker = self.entry.get("tracker_index")
        if tracker:
            parts.append(str(tracker))
        for field, label in (("shoulder_width", "肩宽"), ("arm_length", "臂长")):
            value = self.entry.get(field)
            if isinstance(value, (int, float)):
                parts.append(f"{label}={value:.4f}")
        return ", ".join(parts)


def _shorten_command(cmd: str, limit: int = 140) -> str:
    """把命令行裁到可读长度。日志里贴整条 `ros2 launch … | python3 -c "…"` 会刷屏。"""
    cmd = " ".join(str(cmd).split())
    return cmd if len(cmd) <= limit else cmd[:limit - 1] + "…"


def _read_remaining_stdout(proc: subprocess.Popen, stderr: bool = False,
                           timeout: float = 3.0) -> str:
    """读取进程的残余输出，读取前先确保它已结束。

    直接 `proc.stdout.read()` 在进程仍存活时会阻塞到管道 EOF —— 标定节点没退出
    时，这一步会**永久挂起标定线程**（对话框关不掉，只能强杀上位机）。所以先用
    terminate/kill 收掉进程再读。读失败一律返回空串：收尾动作不该再抛异常，
    那会掩盖真正的失败原因。
    """
    if proc is None:
        return ""
    stream = proc.stderr if stderr else proc.stdout
    if stream is None:
        return ""
    if proc.poll() is None:
        for killer in (proc.terminate, proc.kill):
            try:
                killer()
                proc.wait(timeout=timeout)
                break
            except Exception:
                continue
    try:
        return stream.read() or ""
    except Exception:
        return ""

# 遥操作进程的运行状态文件。
# 遥操作是 start_new_session 起的独立进程，界面关掉它还在跑；只靠内存里的
# ProcessManager 重新打开界面后就完全看不见它了，所以必须落盘。
TELEOP_STATE_FILENAME = "teleop.state.json"

# /proc 是否存在（Linux）。开发机（macOS）没有 /proc，退化为只判断进程是否存在。
_PROC_AVAILABLE = os.path.isdir("/proc")


def _process_alive(pid: int) -> bool:
    """进程**在进程表里存在**（PermissionError 说明进程在，只是不属于当前用户）。

    ⚠️ 这个判断**不能**当作"在不在跑"用：僵尸进程（已死、还没被父进程回收）同样
    存在，`os.kill(pid, 0)` 也会成功。要判断"真的在跑"用 `_process_running`。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


#: /proc 里的进程状态字母里，这几个代表"已经不执行任何代码了"：
#: Z = 僵尸（退出后未被父进程回收）、X/x = 已消亡。
_DEAD_STATES = ("Z", "X", "x")


def _proc_state(pid: int) -> Optional[str]:
    """读 /proc/<pid>/stat 的 state 字段（R/S/D/Z/T/…）；进程不存在返回 None。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm（第 2 字段）可能含空格与括号，从最后一个 ')' 之后切分更稳
    tail = stat.rpartition(")")[2].split()
    return tail[0] if tail else None


def _process_running(pid: int) -> bool:
    """进程是否**真的在跑** —— 存在，且不是僵尸/已消亡。

    ## 为什么要单独有这一个判断（实测踩过）

    `os.kill(pid, 0)` 对僵尸进程是**成功**的：进程表里还挂着这个 pid，但它不执行
    任何代码。用"pid 存在"当"在运行"，界面就会一直认为遥操作还活着 ——
    按钮停在「停止遥操作」、状态写着"运行中"，点下去却什么都停不掉，
    再点一次就只剩一句"没有正在运行的遥操作进程"（现场截图就是这个形态）。
    实测复现：杀掉遗留遥操作进程、父进程先不回收 → /proc 里 state = Z，
    `os.kill(pid, 0)` 依然成功，界面于是永远停在"运行中"。
    """
    if pid <= 0:
        return False
    if not _PROC_AVAILABLE:
        return _process_alive(pid)
    state = _proc_state(pid)
    if state is None:
        # /proc 里没有这个 pid：可能真是结束了，也可能是内核线程/权限边角，
        # 回退问一次 pid 是否存在（原先的行为）
        return _process_alive(pid)
    return state not in _DEAD_STATES


def _proc_start_ticks(pid: int) -> Optional[int]:
    """进程启动时刻（/proc/<pid>/stat 第 22 字段，单位：内核时钟节拍）。

    pid 会被系统复用，只有「pid + 启动时刻」才能唯一确定一个进程。
    刻意不用 cmdline 做校验：bash 对最后一条命令会做 exec 优化，
    `bash -c "...; bash run_teleop.sh"` 的 cmdline 可能变成被 exec 的程序名，
    靠关键字匹配会漏判。进程不存在时返回 None。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm（第 2 字段）可能含空格与括号，从最后一个 ')' 之后切分更稳
    tail = stat.rpartition(")")[2].split()
    # tail[0] 是 state（第 3 字段），因此 starttime（第 22 字段）落在下标 19
    if len(tail) <= 19:
        return None
    try:
        return int(tail[19])
    except ValueError:
        return None


# ============ 机器人状态（robot_monitor.py 的输出） ============
def _as_float(value, default: float = 0.0) -> float:
    """安全转 float（状态文件里的时间戳也可能是字符串/空值）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    """安全转 int（同 `_as_float`：外部输入不假定类型）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============ 三期：模块（躯干 / 双臂）的上、下使能 ============
# 话题与前置条件**与 scripts/connect_sequence.py 同源** —— 那边是上电时序的真源，
# 这里只是把它拆成"可以单独点"的版本；两处一旦漂移就会出现"整机时序能起来、
# 单独点某一步没反应"，所以有用例核对两张表。
#
# 躯干上使能**不是一条 true**：必须 connect → motor_mode（切模式）→ 再 connect
# （`motor_mode` 改完必须重发 connect 才生效 —— 文档原文）。少中间那步会得到
# "抬头了但模式没变"，后续控制看起来没反应（现场踩过）。
#
# **值是 0（非实时），不是 1（实时）** —— v6.2.0 改的就是这一处，现场那句
# 「进入直接就是非实时模式，不需要重新连接」要求它成立：
#   · 非实时 = 控制节点**内部插值**，一条点位目标就够（本页发的正是单条目标）；
#   · 实时 = 关节按允许的最大速度追目标、**要上位机自己 100Hz 插值**（文档原文）。
#     上使能留成实时、再发单条点位目标，就是"动作又快又硬/看着没反应"的根源；
#   · 遥操作的上电时序（`connect_sequence.py`）确实会切实时并再 connect —— 那是
#     遥操作要的形态；本页反过来，**上使能结束时必须是非实时**。
# 依据：机器人侧 README 的躯干示例就是 `motor_mode {0x00}` → `connect true` → 点位目标。
# 每步后面的等待取自时序脚本，别在这里改小：那是留给硬件动作的时间。
MODULE_STEPS = {
    ("trunk", True): (
        ("接通躯干", "/trunk_control/connect", "bool", True, 5.0),
        ("躯干切非实时模式", "/trunk_control/motor_mode", "byte", 0, 3.0),
        ("躯干重新使能", "/trunk_control/connect", "bool", True, 3.0),
    ),
    ("trunk", False): (
        ("断开躯干", "/trunk_control/connect", "bool", False, 1.0),
    ),
    ("arms", True): (
        ("接通双臂", "/arm_control/connect", "bool", True, 5.0),
    ),
    ("arms", False): (
        ("断开双臂", "/arm_control/connect", "bool", False, 1.0),
    ),
}

#: 模块键 -> 界面上的名字
MODULE_NAMES = {"trunk": "躯干", "arms": "双臂"}

# 模块使能状态（`module_state_of` 的取值）。刻意用四个态、而不是 True/False：
# "不知道"与"未使能"在按钮上必须是两种待遇 —— 不知道只允许先查、不允许点。
MODULE_STATE_ON = "on"
MODULE_STATE_OFF = "off"
MODULE_STATE_PARTIAL = "partial"
MODULE_STATE_UNKNOWN = "unknown"

#: 状态取值的界面说法（结果区里点名当前状态用）
MODULE_STATE_TEXT = {
    MODULE_STATE_ON: "已使能",
    MODULE_STATE_OFF: "未使能",
    MODULE_STATE_PARTIAL: "一侧已使能",
    MODULE_STATE_UNKNOWN: "状态未知",
}


def module_state_of(module_state, module: str) -> str:
    """从状态视图判断某模块当前的使能状态（`MODULE_STATE_*` 之一）。

    - `unknown`：**整段状态读不出来**（监控没跑、文件过期、消息包缺失）——
      这时只允许先查，不允许点：按下去也无从判断成没成；
    - `off`：没数据也算未使能。这不是猜测：现场澄清过「**上了使能才会有状态数据**」
      （实测没使能时 `/trunk_control/trunk_state` 连 Publisher 都是 0），
      二期界面显示的也是"○ 躯干 未使能"。若把"没数据"当成"未知"，
      最常见的场景（躯干还没上使能、现在要点上使能）反而会被自己的判据挡在门外；
    - `partial`：只对双臂成立（一侧已使能、另一侧没有）—— 这个态要单独存在，
      因为两侧按钮都该可用（补齐 / 全下），而且它正是最该被看见的形态。

    只用 `robot_monitor.py` 已经算好的 `has_data` / `enabled` / `enabled_left|right`，
    不在这里重复"多新算新"（那是监控的判据，见 MODULE_STALE_AFTER）。
    """
    # `available` 必须显式为真：老版本监控脚本压根没有这一段（空 dict），
    # 那属于"读不出来"，不能顺着"没数据=未使能"的规则把按钮点亮。
    if not isinstance(module_state, dict) or not module_state.get("available"):
        return MODULE_STATE_UNKNOWN
    entry = module_state.get("trunk_state" if module == "trunk" else "arm_state")
    if not isinstance(entry, dict) or not entry.get("has_data"):
        return MODULE_STATE_OFF
    if module == "trunk":
        return MODULE_STATE_ON if entry.get("enabled") else MODULE_STATE_OFF
    left, right = bool(entry.get("enabled_left")), bool(entry.get("enabled_right"))
    if left and right:
        return MODULE_STATE_ON
    if not left and not right:
        return MODULE_STATE_OFF
    return MODULE_STATE_PARTIAL


# ============ 宇树夹爪 Dex11（yushu_dex11）============
# 协议事实来源（都不是猜的，三处可复查）：
#   * 机器人侧 `install/lib/tools/dex11_fcc_grasp_test.py` —— 官方抓取测试程序，
#     话题名、raw↔关节角/力矩/速度 的换算、张开/闭合的取值全在里面；
#   * `install/config/controller/hand/yushu_dex11_{left,right}_hand.yaml` —— 控制器参数
#     （motor_id / motor_q_max / grasp_tau_min|max / jnt_limit_lower|upper / 关节名）；
#   * 《轮式人形末端手（夹爪）重构设计》—— `hand_posture` 的字段语义与"手臂连接后
#     会自动连接末端手掌"。
#
# 宇树夹爪是**单自由度**（一个闭合关节），协议里的三元组是
# `[闭合速度, 闭合位置, 目标力矩]`，三个都是千分比 0~1000。
GRIPPER_JOINTS = {"left": "LJoint1_1", "right": "RJoint1_1"}
GRIPPER_NAMES = {"left": "左爪", "right": "右爪"}
#: `hand_posture` 的 `side` 字段：0 = 左手 / 1 = 右手（消息注释）
GRIPPER_SIDE_ID = {"left": 0, "right": 1}
#: `dex11_direct_test` 的 `id=`：右手 0 / 左手 1 —— 与上面那条**正好相反**，别抄错
GRIPPER_DIRECT_ID = {"left": 1, "right": 0}
#: 张开 / 闭合的千分比（取自 fcc_grasp_test.py 的 RAW_OPEN / RAW_CLOSED）
GRIPPER_RAW_OPEN = 100
GRIPPER_RAW_CLOSED = 1000
#: 关节角范围（rad），来自控制器 yaml 的 jnt_limit_lower / upper；raw 是它的线性映射
GRIPPER_JNT_OPEN = -0.02
GRIPPER_JNT_CLOSED = 0.0245
#: 目标力矩范围（N·m），来自控制器 yaml 的 grasp_tau_min / grasp_tau_max
GRIPPER_TAU_MIN_NM = 0.5
GRIPPER_TAU_MAX_NM = 2.0
#: 千分比速度 1000 对应的实际速度（mm/s）。fcc 测试程序注释：整行程约 0.45s，
#: 100mm/s 是"实测跟得住的最快值"，再快跟不动 —— 别在这里往上调。
GRIPPER_SPEED_MAX_MMS = 100.0
#: 默认参数（千分比）：力矩 300 = 0.6 N·m（偏保守）、速度 300 = 30 mm/s
GRIPPER_DEFAULT_TAU_RAW = 300
GRIPPER_DEFAULT_SPEED_RAW = 300
#: 非实时单次指令的落点（走 JTC 生成轨迹）
GRIPPER_POSTURE_TOPIC = "/arm_control/hand_posture"
GRIPPER_POSTURE_MSG = "wheeled_robot_msgs/msg/HandPosture"
#: 实时/非实时切换话题：`false` = 关掉 FCC、切回 JTC
GRIPPER_FORWARD_SET_TOPICS = {
    "left": "/arm_control/left_hand_forward_set",
    "right": "/arm_control/right_hand_forward_set",
}
#: 直测程序（相对工作空间根）。**它不依赖 ROS 库** —— ldd 只有 libunitree_motor_sdk /
#: libserialport（rpath 指向 install/lib），所以"不起主程序"这条路径**不需要 ROS 环境**，
#: 也就不能拿"topic 通不通"来判断它能不能用。
GRIPPER_TOOL_REL = "install/lib/tools/dex11_direct_test"
#: 直测工具支持的动作（现场只要求这两个：**抓用 grasp、开用 open**）。
#: 工具本身还有 move/jump/auto/calib-force，都不收进界面 —— 那些是标定/调参用的，
#: 有专门的工具与流程（见 `dex11_calib`），混进"抓一下"的按钮里只会让人误点。
GRIPPER_DIRECT_ACTIONS = ("open", "grasp")
GRIPPER_DIRECT_ACTION_TEXT = {"open": "张开", "grasp": "闭合抓取"}
#: 零点标定工具（同样在工作空间里、同样不依赖 ROS 库）。
#: `--scan` = 只扫描不做改动；`--yes` = 跳过它自己的 [y/N]，确认由界面负责。
#: 它自己的帮助文本里就写着"确保 ROS 程序已停止(串口需要独占)"，与直测工具同一个前提。
GRIPPER_CALIB_TOOL_REL = "install/lib/tools/dex11_calib"
#: FCC 实时抓取脚本。**必须是上位机自己那份**：官方 `dex11_fcc_grasp_test.py` 固定驱动
#: 左右两侧、没有单侧开关，而界面是按侧操作的（只想让左爪抓时右爪不该跟着合）。
GRIPPER_FCC_SCRIPT_NAME = "dex11_fcc_grasp.py"
#: FCC 夹持保持时长（秒）—— 现场说的"抓取多少时间"
GRIPPER_HOLD_SEC_MIN = 0.5
GRIPPER_HOLD_SEC_MAX = 60.0
GRIPPER_HOLD_SEC_DEFAULT = 3.0
#: 手型由 Product.json 决定；只有这个值才说明装的是宇树夹爪
PRODUCT_JSON_PATH = Path("/var/nerv/product/Product.json")
GRIPPER_HAND_TYPE = "yushu_dex11"

# ---------- 末端参数（arm_control_config.yaml）----------
#: 机器人侧的末端参数文件（相对工作空间）。与 teleop_config.yaml 一样是"节点启动时读一次"
#: 的配置，但**内容体量完全不同**：40+ 行注释（灵巧手的重物 / 三指 / 双指档位）+ 四种夹爪
#: 各一套档位。所以上位机**只能按行精准替换目标行的值**，整份 YAML 重写会把注释与别的档位
#: 全丢掉 —— 那份注释是现场调档位的唯一说明，丢一次很难找回来。
ARM_CONTROL_CONFIG_REL = "install/config/arm_control_config.yaml"
#: 文件里的四个手型段（`ArmControlNode.ros__parameters` 下的二级键，与文件头注释同源）
END_EFFECTOR_SECTIONS = ("yinshi", "zhixing", "hangkai", "yushu")
#: 段名 ←→ 类型编号（文件头注释：0 灵巧手 / 1 zhixing / 2 hangkai / 3 yushu）
END_EFFECTOR_TYPE_IDS = {"yinshi": 0, "zhixing": 1, "hangkai": 2, "yushu": 3}
#: 编号 → 段名（解析文件时的反查）
END_EFFECTOR_SECTIONS_BY_ID = {value: key for key, value in END_EFFECTOR_TYPE_IDS.items()}
#: 界面上的名字
END_EFFECTOR_NAMES = {"yinshi": "灵巧手", "yushu": "宇树夹爪",
                      "zhixing": "夹爪（zhixing）", "hangkai": "夹爪（hangkai）"}


def end_effector_label(section: str) -> str:
    """下拉/徽章上的"名字（段名）"。

    段名**已经写进名字里**的那两种（`夹爪（zhixing）` / `夹爪（hangkai）`）不再重复拼接 ——
    直接套 `f"{名字}（{段名}）"` 会拼出"夹爪（zhixing）（zhixing）"，这个重复正是从
    下拉列表里被看出来的（现场反馈"下拉菜单太丑"时截的图里就是这么显示的）。
    """
    name = END_EFFECTOR_NAMES.get(section, section)
    return name if f"（{section}）" in name else f"{name}（{section}）"
#: **本期只做这两种**（现场："先只做灵巧手和宇树夹爪的"）—— 另外两种的档位仍在文件里，
#: 只是不给编辑，保存时原样保留。
END_EFFECTOR_EDITABLE = ("yinshi", "yushu")
#: 两组指令：**open = 张开时下发 / fist = 握拳时下发**（现场 2026-09-28 口头确认）。
#: 与文件内容互相印证：open 的位置项 = 0（全开）、fist = 1000（合到底）；
#: 灵巧手同样是 1000（张开）对 330（握紧）。**这两个词别弄反**。
END_EFFECTOR_CMDS = ("open", "fist")
END_EFFECTOR_CMD_TEXT = {"open": "张开指令", "fist": "握紧指令"}
#: 每种手型要填几个值、每个位置的范围：
#: yinshi = 前 6 手指角度(0~1000) + 后 6 力控(0~3000)（文件注释原文）；
#: yushu  = [开合速度, 开合位置, 力矩] 三个千分比 0~1000（现场说明"第一个速度、第二个位置、
#:          第三个力矩"，位置越大越闭合）。
END_EFFECTOR_RANGES = {
    "yinshi": tuple([(0, 1000)] * 6 + [(0, 3000)] * 6),
    "yushu": tuple([(0, 1000)] * 3),
}
#: 输入框的分组（界面用）：(组名, 起始下标, 个数) —— 灵巧手分两行摆得下 12 个格子
END_EFFECTOR_GROUPS = {
    "yinshi": (("角度 1-6", 0, 6), ("力控 1-6", 6, 6)),
    "yushu": (("速度 · 位置 · 力矩", 0, 3),),
}
#: "恢复默认值"用的出厂值 —— 就是文件里原本那套（yinshi 取自它自己的注释与取值）
END_EFFECTOR_DEFAULTS = {
    "yinshi": {"open": [1000, 1000, 1000, 1000, 1000, 60, 300, 300, 300, 300, 300, 300],
               "fist": [330, 340, 340, 370, 680, 60, 600, 600, 800, 600, 600, 600]},
    "yushu": {"open": [800, 0, 500], "fist": [800, 1000, 500]},
}
#: Product.json 的 `hand` → 文件里的段名。**后续"类型直接跟着 product 走"就靠这张表**，
#: 本期先只用来在页面上显示一行对照（与所选类型不一致时给 warn）。
PRODUCT_HAND_SECTIONS = {"yinshi_rh56d": "yinshi", "yushu_dex11": "yushu",
                         "hangkai_tg9801": "hangkai"}


def _as_optional_float(value):
    """转 float；转不了返回 None（区别于 0.0 —— "没有"和"是 0"要分开）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gripper_tau_nm(raw) -> float:
    """目标力矩千分比 → N·m，并钳到控制器允许的 `grasp_tau_min` ~ `grasp_tau_max`。

    下限钳到 0.5 N·m 而不是 0：`grasp_tau_min=0.5`（控制器 yaml），低于它的力矩
    等于"没给力"，夹爪会完全松掉 —— `dex11_direct_test` 与 fcc 测试程序都是这么换算的。
    """
    value = _as_optional_float(raw)
    if value is None:
        value = float(GRIPPER_DEFAULT_TAU_RAW)
    span = value / 1000.0 * GRIPPER_TAU_MAX_NM
    return max(GRIPPER_TAU_MIN_NM, min(GRIPPER_TAU_MAX_NM, span))


def gripper_speed_mms(raw) -> float:
    """闭合/张开速度千分比 → mm/s。下限取 1 只为避免"0 速度"变成除零。"""
    value = _as_optional_float(raw)
    if value is None:
        value = float(GRIPPER_DEFAULT_SPEED_RAW)
    return max(1.0, min(1000.0, value)) / 1000.0 * GRIPPER_SPEED_MAX_MMS


def gripper_tau_raw(nm) -> int:
    """N·m → 目标力矩千分比（界面填的是物理量，协议要的是千分比）。

    与 `gripper_tau_nm` 互为反函数；输入先钳到 `GRIPPER_TAU_MIN_NM` ~ `MAX`，
    所以界面无论填什么都不会算出协议外的值。
    """
    value = _as_optional_float(nm)
    if value is None:
        value = GRIPPER_TAU_MIN_NM
    clamped = max(GRIPPER_TAU_MIN_NM, min(GRIPPER_TAU_MAX_NM, value))
    return int(round(clamped / GRIPPER_TAU_MAX_NM * 1000))


def gripper_speed_raw(mms) -> int:
    """mm/s → 速度千分比（与 `gripper_speed_mms` 互为反函数）。"""
    value = _as_optional_float(mms)
    if value is None:
        value = GRIPPER_SPEED_MAX_MMS
    clamped = max(0.1, min(GRIPPER_SPEED_MAX_MMS, value))
    return int(round(clamped / GRIPPER_SPEED_MAX_MMS * 1000))


def gripper_position_raw(close: bool) -> int:
    """动作 → 位置千分比。**越大越闭合**（100 张开 / 1000 闭合）。"""
    return GRIPPER_RAW_CLOSED if close else GRIPPER_RAW_OPEN


def gripper_joint_rad(raw) -> float:
    """位置千分比 → 关节角（rad）。用来把回读到的关节角换算成"张合到哪儿了"。"""
    value = _as_optional_float(raw)
    if value is None:
        return GRIPPER_JNT_OPEN
    ratio = max(0.0, min(1000.0, value)) / 1000.0
    return GRIPPER_JNT_OPEN + ratio * (GRIPPER_JNT_CLOSED - GRIPPER_JNT_OPEN)


def gripper_gap_mm(rad) -> float:
    """关节角 → "离完全闭合还差多少"（mm）。

    行程 = `GRIPPER_JNT_CLOSED - GRIPPER_JNT_OPEN` = 44.5mm（fcc 测试程序里
    `stroke_mm = (JNT_CLOSED - JNT_OPEN) * 1000`，即 1 rad ≡ 1 m）。它同时是
    **开口宽度**：夹住东西时等于物体宽度，全开时等于满行程，接近 0 = 手里没东西。
    展示请走 `gripper_opening_text()`（它会分档，避免把"全开"读成"夹着东西"）。
    """
    value = _as_optional_float(rad)
    if value is None:
        return 0.0
    return (GRIPPER_JNT_CLOSED - value) * 1000.0


#: "张开"位（raw=100）对应的开口宽度 ≈ 40.1mm。注意协议里张开位**不是 raw=0** ——
#: 官方测试程序用 100 留了 10% 余量，所以满行程 44.5mm 只在 raw=0 才出现，
#: 别拿它当"张开"的基准，否则正常张开会被算成"没开到位"。
GRIPPER_OPEN_GAP_MM = (GRIPPER_JNT_CLOSED
                       - gripper_joint_rad(GRIPPER_RAW_OPEN)) * 1000.0


def gripper_opening_text(rad) -> str:
    """关节角 → 一句人话：全开 / 已合到底 / 开口多少毫米。

    为什么要分档、而不是直接报毫米数：**张开位**算出来是"离闭合还差 40mm"，
    直接显示成"开口 40mm"还行，但"合到底"（<0.5mm）和"全开"其实都被同一个数字
    表达得不清楚 —— 前者才是"手里没东西"，后者要跟"夹着个 40mm 的东西"分开。
    三档正好对应现场真正会问的三个问题：张开了吗 / 夹住了吗 / 夹了多宽。

    Args:
        rad: `/joint_states` 里该侧夹爪关节的位置（rad）。None → 报"位置未知"。

    Returns:
        中文短句，界面直接贴在读数后面。
    """
    value = _as_optional_float(rad)
    if value is None:
        return "位置未知"
    gap = (GRIPPER_JNT_CLOSED - value) * 1000.0
    if gap < 0.5:
        return "已合到底（没夹住东西）"
    if gap >= GRIPPER_OPEN_GAP_MM - 1.5:
        return f"已张开（开口 {gap:.0f}mm）"
    return f"开口 {gap:.1f}mm（夹着东西）"


def gripper_fcc_seconds(speed_raw, hold_sec) -> float:
    """FCC 抓取整个过程预计要多久（秒）。

    与 `scripts/dex11_fcc_grasp.py::total_seconds()` **同一个公式**（由
    `test_fcc_seconds_matches_script` 守着）—— 界面拿它在动手前告诉现场
    "这次会安静几秒"，否则长达一分钟的夹持看起来就像卡住了。

    组成：张开到位 1.0s + 合拢（一段行程）+ 夹持 + **取值 1.0s** + 松开 + 张开到位 1.0s
    + 收尾 1.0s。夹持那一段就是可设的那个时长。
    """
    span = GRIPPER_OPEN_GAP_MM                      # 张开位 ↔ 闭合位 正好是这一段
    one_way = span / max(1.0, gripper_speed_mms(speed_raw))
    return 4.0 + 2.0 * one_way + max(0.0, _as_optional_float(hold_sec) or 0.0)


def read_product_hand(path: Optional[Path] = None) -> str:
    """读 Product.json 里配置的手型（`hand` 字段）；读不到返回空串。

    空串在界面上显示成"手型 未知"，**不当成"手型不对"**：路径可能随版本变化，
    文件读不到不该把功能锁死；而读到 `empty` / `hangkai_tg9801` 这类**确定的别的值**
    才说明"这台机器上没装宇树夹爪"。

    Args:
        path: 不传则用 `PRODUCT_JSON_PATH`。**刻意写成运行时取**而不是默认参数 ——
            默认参数在函数定义时就绑定好了，那样改模块里的常量不会生效（用例里
            想换一个 Product.json 就换不掉）。
    """
    path = PRODUCT_JSON_PATH if path is None else path
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("hand") or "")


def end_effector_slot_ranges(hand: str) -> tuple:
    """这种末端类型每个输入框的取值范围；未支持的类型返回空元组（界面据此禁用输入）。"""
    return END_EFFECTOR_RANGES.get(str(hand or ""), ())


def product_section(product_hand: str) -> str:
    """Product.json 的 `hand` → 文件里的段名；认不出来返回空串（不猜）。"""
    return PRODUCT_HAND_SECTIONS.get(str(product_hand or ""), "")


def parse_arm_control(payload) -> dict:
    """从 arm_control_config.yaml 的解析结果里取出界面要的那几个数。

    只认这条路径：``ArmControlNode → ros__parameters → <段名> → <side>_<cmd>_cmd``。
    **别的字段一概不碰**（`*_depth_mask`、其它手型段、注释都不在返回值里）—— 页面只显示
    自己要改的东西，"顺手显示"会把不属于这一期的责任也揽过来。

    读数一律要求是"整数的列表"，缺一个位置就整组算"没有"（`None`）：半截数据比没有更危险
    —— 拿它去填写输入框，保存时会把缺的位置写成 0。

    Args:
        payload: `load_yaml()` 的结果（顶层是映射）。

    Returns:
        dict: ``{"params": 有没有 ros__parameters 段, "types": {side: 段名|None},
        "depth_control": {side: True/False/None}, "values": {side: {"open": [...]|None,
        "fist": [...]|None}}}``
    """
    params = {}
    if isinstance(payload, dict):
        node = payload.get("ArmControlNode")
        if isinstance(node, dict) and isinstance(node.get("ros__parameters"), dict):
            params = node["ros__parameters"]

    types, depth = {}, {}
    sections = {}
    for section in END_EFFECTOR_SECTIONS:
        raw_block = params.get(section)
        if not isinstance(raw_block, dict):
            continue
        entry = {}
        for side in ("left", "right"):
            values = {"open": None, "fist": None}
            for cmd in END_EFFECTOR_CMDS:
                raw_values = raw_block.get(f"{side}_{cmd}_cmd")
                if (isinstance(raw_values, list) and raw_values
                        and all(isinstance(item, int) and not isinstance(item, bool)
                                for item in raw_values)):
                    values[cmd] = [int(item) for item in raw_values]
            entry[side] = values
        sections[section] = entry

    values = {}
    for side in ("left", "right"):
        # `True` 也是 int —— 不排除的话类型编号会被 yaml 里的布尔值顶掉
        raw_id = params.get(f"{side}_end_effector_type")
        types[side] = (END_EFFECTOR_SECTIONS_BY_ID.get(raw_id)
                       if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None)
        raw_depth = params.get(f"{side}_end_effector_depth_control")
        depth[side] = raw_depth if isinstance(raw_depth, bool) else None
        # **所有手型段都读出来**（不只是文件里当前那一种）：现场在下拉里换类型时，
        # 页面上要能立刻显示那一种的现有值，而不是把这一种的值填进另一种的格子
        values[side] = ((sections.get(types[side]) or {}).get(side)
                        or {"open": None, "fist": None})
    return {"params": bool(params), "types": types, "depth_control": depth,
            "values": values, "sections": sections}


def validate_end_effector_values(hand: str, values) -> List[str]:
    """检查一组指令值：个数对不对、每个位置在不在范围内。

    返回错误行（空列表 = 通过）。**刻意不静默钳位**：钳位会让现场以为自己填的值生效了，
    实际上被悄悄改掉 —— 报出来比改掉好（界面上把范围写进输入框，越界根本填不进去）。
    """
    ranges = end_effector_slot_ranges(hand)
    if not ranges:
        return [f"不支持的末端类型：{hand or '（空）'}"]
    name = END_EFFECTOR_NAMES.get(hand, hand)
    if not isinstance(values, (list, tuple)):
        return [f"{name} 的指令值不是列表：{values!r}"]
    if len(values) != len(ranges):
        return [f"{name} 需要 {len(ranges)} 个值，实际给了 {len(values)} 个"]

    errors = []
    for index, (item, (low, high)) in enumerate(zip(values, ranges), start=1):
        number = _as_optional_float(item)
        if number is None or int(number) != number:
            errors.append(f"{name} 第 {index} 个值不是整数：{item!r}")
        elif not low <= number <= high:
            errors.append(f"{name} 第 {index} 个值 {int(number)} 超出范围 {low}~{high}")
    return errors


def format_config_list(values, template: str = "") -> str:
    """渲染一行数组：``[ 800, 0, 500 ]``（`[` 后、`]` 前各留一个空格）。

    `template` 是**原行的写法**，给了就复用它的分隔空格（文件里有的行为了对齐写成
    ``[ …, 60,  300, … ]`` 双空格）。为什么在意这一层：现场靠"眼睛 diff 前后两版"确认
    改了哪几个数，整行都变红时真正改动的那一个数字反而看不出来；复用了分隔之后，
    没改动的行**逐字节相同**、diff 里根本不出现。
    模板与值的个数对不上（例如换了手型、行数不同）就退回单空格写法。
    """
    items = [int(item) for item in values]
    if template:
        start, end = template.find("["), template.rfind("]")
        if 0 <= start < end:
            pieces = re.split(r"(\s*,\s*)", template[start + 1:end])
            seps, slots = pieces[1::2], pieces[0::2]
            if len(slots) == len(items) and len(seps) == len(items) - 1:
                lead = slots[0][:len(slots[0]) - len(slots[0].lstrip())]
                trail = slots[-1][len(slots[-1].rstrip()):]
                body = "".join([str(items[0])] + [f"{sep}{item}"
                                                  for sep, item in zip(seps, items[1:])])
                return "[" + lead + body + trail + "]"
    return "[ " + ", ".join(str(item) for item in items) + " ]"


#: 段名行 / 字段行 / 类型行的识别式。缩进写死（4 空格 = 段名、6 空格 = 字段）是**故意的**：
#: 这个文件的层级是固定的（ArmControlNode → ros__parameters → 手型段 → 字段），按缩进认比
#: 反查 YAML 结构简单得多，也不会把别的段里的同名键误当目标（它们不在同一个 current 段里）。
_AE_SECTION_RE = re.compile(r"^(?P<indent> {4})(?P<name>\w+):\s*$")
_AE_FIELD_RE = re.compile(r"^(?P<indent> {6})(?P<name>\w+):(?P<sep>\s*)"
                          r"(?P<value>\[[^\]]*\])(?P<tail>\s*)$")
_AE_TYPE_RE = re.compile(r"^(?P<indent> {4})(?P<name>(?:left|right)_end_effector_type):"
                         r"(?P<sep>\s*)(?P<value>-?\d+)(?P<tail>.*)$")


def patch_arm_control_text(text: str, sections: dict, types: Optional[dict] = None) -> dict:
    """**按行**改 arm_control_config.yaml：只替换目标行的数组值与类型编号，其余逐字保留。

    为什么不做"解析 → 改字典 → `yaml.dump`"：那份文件里有 40 多行注释（灵巧手的重物抓取 /
    三指取药 / 双指捏取三套档位）和另外两种夹爪的整段档位。整份重写会把这些**全部丢掉**，
    而它们正是现场调档位的唯一说明。

    Args:
        text: 原文件全文。
        sections: ``{段名: {"left_open_cmd": [..], "right_fist_cmd": [..]}}``。
            值为 `None` 的键**跳过**（那一侧本期不支持，保持原样）。
        types: ``{"left": 段名, ...}``；只要给了就会把 `left_end_effector_type` 的值一起改
            （行内注释保留）。

    Returns:
        dict: ``{"text": 新全文, "applied": [已改的键], "missing": [没找到的键]}``。
        调用方**必须**看 `missing`：非空说明文件结构与预期不符（比如版本升过、段被改名），
        这时不能写盘 —— 写一半的配置比不写更糟。
    """
    targets = {}
    for section, block in (sections or {}).items():
        for side in ("left", "right"):
            for cmd in END_EFFECTOR_CMDS:
                values = (block or {}).get(f"{side}_{cmd}_cmd")
                if values is not None:
                    targets[(section, f"{side}_{cmd}_cmd")] = list(values)

    type_targets = {}
    for side, section in (types or {}).items():
        if section:
            type_targets[f"{side}_end_effector_type"] = END_EFFECTOR_TYPE_IDS.get(section)

    applied, missing = [], []
    current = None
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]

        section_line = _AE_SECTION_RE.match(body)
        if section_line:
            current = section_line.group("name")
            out.append(line)
            continue

        field = _AE_FIELD_RE.match(body)
        if field and (current, field.group("name")) in targets:
            values = targets.pop((current, field.group("name")))
            out.append(f"{field.group('indent')}{field.group('name')}:{field.group('sep')}"
                       f"{format_config_list(values, field.group('value'))}"
                       f"{field.group('tail')}{ending}")
            applied.append(f"{current}.{field.group('name')}")
            continue

        type_line = _AE_TYPE_RE.match(body)
        if type_line and type_line.group("name") in type_targets:
            value = type_targets.pop(type_line.group("name"))
            out.append(f"{type_line.group('indent')}{type_line.group('name')}:"
                       f"{type_line.group('sep')}{value}{type_line.group('tail')}{ending}")
            applied.append(type_line.group("name"))
            continue

        out.append(line)

    missing.extend(f"{section}.{key}" for section, key in sorted(targets))
    missing.extend(sorted(type_targets))
    return {"text": "".join(out), "applied": applied, "missing": missing}


def parse_joint_state(text: str) -> dict:
    """解析 `ros2 topic echo --once /joint_states` 的 YAML，取 `name`/`position`/`effort`。

    **刻意手写解析、不引 yaml 库**：`ros2 topic echo` 的 YAML 里
    `stamp: {sec: 0, nanosec: 0}` 这类无单位标量会被 PyYAML 读成数字（丢失原文），
    而我们只要三个**平行列表**。块状（`- 0.1`）与行内（`[0.1, 0.2]`）两种写法都认，
    解析不出来返回空 dict —— 调用方据此显示"未知"，不猜。

    Args:
        text: 命令的原始输出（含 `---` 分隔符、可能的警告行，一律容忍）。

    Returns:
        ``{"name": [...], "position": [...], "effort": [...]}``；长度对不上时返回空 dict
        （两个列表长度不等说明这段输出被截断了，用它会错位取到别人的读数）。
    """
    fields: dict = {"name": [], "position": [], "effort": []}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        matched = re.match(r"^(name|position|effort):\s*(.*)$", stripped)
        if matched:
            current = matched.group(1)
            inline = matched.group(2).strip()
            if inline.startswith("[") and inline.endswith("]"):
                fields[current] = [_yaml_scalar(v) for v in inline[1:-1].split(",")
                                   if v.strip()]
            continue
        if current is None:
            continue
        if stripped.startswith("- "):
            fields[current].append(_yaml_scalar(stripped[2:]))
        elif stripped and ":" in stripped:
            current = None          # 走进了别的段（velocity / header…）就收工
    if not fields["name"]:
        # 一个字都没解析出来 = **没有数据**，返回 {} 而不是三个空列表：
        # 空列表在调用方看来是"读到了、但两侧都没连上"，那是两个完全不同的结论
        return {}
    if len(fields["name"]) != len(fields["position"]):
        return {}
    return fields


def _yaml_scalar(text: str):
    """YAML 标量 → 数字；转不了原样返回字符串（关节名走这条）。"""
    value = text.strip().strip("'\"")
    try:
        return float(value)
    except ValueError:
        return value


def gripper_reading(fields: dict) -> dict:
    """从 `/joint_states` 的解析结果取左右夹爪读数。

    关节名不在 `name` 里 = 该侧末端**没连上**：官方测试程序
    （`dex11_fcc_grasp_test.py::wait_joints`）的注释写明了"收不到夹爪关节，
    请确保真机已启动和手臂已连接"，手掌连上才会出现这一路关节。

    Args:
        fields: `parse_joint_state` 的结果（可能是空 dict）。

    Returns:
        ``{side: {"connected": bool, "rad": float|None, "effort": float|None}}``。
    """
    names = fields.get("name") or []
    positions = fields.get("position") or []
    efforts = fields.get("effort") or []
    reading = {}
    for side, joint in GRIPPER_JOINTS.items():
        if joint not in names:
            reading[side] = {"connected": False, "rad": None, "effort": None}
            continue
        index = names.index(joint)
        reading[side] = {
            "connected": True,
            "rad": positions[index] if index < len(positions) else None,
            "effort": efforts[index] if index < len(efforts) else None,
        }
    return reading


#: 直测工具输出里的"这句话说明了什么"（键都是二进制里实测存在的字符串，别凭印象加）
_GRASP_MARKERS = (
    ("grasp by kStiffHard", "抓取完成（刚度档：硬）"),
    ("grasp by kStiffMedium", "抓取完成（刚度档：中）"),
    ("not grasped", "没夹住物体（夹到底了但没接触到东西）"),
    ("motor has errors", "⚠ 电机报错，需要断电重连"),
    ("failed to calculate", "⚠ 刚度/标定计算失败（力矩范围太窄或样本不足）"),
    ("port with id=", "⚠ 找不到对应串口（夹爪是否插好、当前用户是否在 dialout 组）"),
)
#: 这些行无论哪种动作都值得原样带进结果区（去重按"匹配到的标记"做，见下）
_DIRECT_RAW_TOKENS = ("contacted", "m, tau=", "m, stiffness=", "start to grasp",
                      "move to", "q=")


def _pick_raw_lines(rows, tokens, limit: int = 6) -> List[str]:
    """从工具输出里挑有价值的原文，**按匹配到的标记去重**。

    为什么要按标记去重而不是按整行：逐周期的数据表里每行的时间/位置都不一样，
    按整行去重等于没去重（一屏还是几十行）。同一个标记只留第一条。
    """
    seen = set()
    picked = []
    for row in rows:
        token = next((t for t in tokens if t in row), None)
        if token is None or token in seen:
            continue
        seen.add(token)
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _summarize_direct_action(side: str, action: str, tau_raw, completed) -> List[str]:
    """把 `dex11_direct_test` 的输出收成几行结论 + 少量原文。

    工具每次控制周期都会打一行数据表，几十上百行原样铺进结果区只会淹掉结论，
    所以：**先给结论**（按 `_GRASP_MARKERS` 命中），再附最多 6 行有价值的原文。

    Args:
        side: ``"left"`` / ``"right"``。
        action: ``"open"`` / ``"grasp"``。
        tau_raw: 请求的力矩千分比（`grasp` 才有意义）。
        completed: `subprocess.CompletedProcess`（读 `returncode` / `stdout` / `stderr`）。

    Returns:
        日志行列表。
    """
    name = GRIPPER_NAMES[side]
    verb = GRIPPER_DIRECT_ACTION_TEXT[action]
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    rows = [row.strip() for row in text.splitlines() if row.strip()]

    head = f"{name}直接{verb}：id={GRIPPER_DIRECT_ID[side]}"
    if action == "grasp":
        head += f"，目标力矩 {gripper_tau_nm(tau_raw):.2f} N·m"
    lines = [head]

    conclusions = []
    for marker, sentence in _GRASP_MARKERS:
        if any(marker in row for row in rows):
            conclusions.append(sentence)
    if completed.returncode != 0:
        conclusions.append(f"⚠ 工具退出码 {completed.returncode}")
    elif not conclusions:
        # 退出码 0 且没有明确结果标记：张开就是这样（它没有"夹住没夹住"这回事）
        conclusions.append(f"工具正常退出（{verb}指令已执行）")
    lines.extend(conclusions or ["⚠ 工具没有输出任何内容"])

    extras = _pick_raw_lines(rows, _DIRECT_RAW_TOKENS)
    if extras:
        lines.append("工具输出：" + " ｜ ".join(extras))
    return lines


#: 一次性子进程的结果（`dex11_calib` 的三段式调用要把它在方法之间递来递去）
_CalibRun = namedtuple("_CalibRun", "returncode stdout stderr")
_EMPTY_RUN = _CalibRun(0, "", "")


def _calib_text(run) -> str:
    """`_CalibRun` → 一段文本（stdout 与 stderr 合起来，摘要函数只认文本）。

    `dex11_calib` 的提示有的走 stdout、有的走 stderr，分开看会漏掉一半。
    """
    return ((getattr(run, "stdout", "") or "") + (getattr(run, "stderr", "") or "")).strip()


def _summarize_scan(text: str) -> List[str]:
    """把 `dex11_calib --scan` 的输出收成"找到几只、在哪、当前角度"。

    真实输出形如（无夹爪时）::

        扫描 /dev/ttyUSB* -> [<empty>] ...
        没有找到夹爪。检查供电、USB线，以及ROS程序是否还占着串口

    有夹爪时会多出 `… id=0 (/dev/ttyUSB0)  q=  0.000 rad` 这样的行。
    """
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows:
        return ["⚠ 扫描没有任何输出"]
    if "没有找到夹爪" in text:
        # 工具自己的提示已经说全了（供电 / USB 线 / 串口被占），直接采用它
        return ["⚠ 没有找到夹爪 —— 检查供电、USB 线，以及主程序是否还占着串口"]

    scan_rows = [row for row in rows if "扫描" in row or "/dev/" in row]
    gripper_rows = [row for row in rows if " id=" in row or "rad" in row]
    lines = ["扫描结果：" + "、".join(scan_rows[:2]) if scan_rows else "扫描完成"]
    if gripper_rows:
        lines.extend("  " + row for row in gripper_rows[:6])
        lines.append(f"共找到 {len(gripper_rows)} 条读数")
    return lines


def _summarize_calibration(before: str, done, after: str) -> List[str]:
    """零点标定的三段输出（标定前扫描 → 标定 → 标定后扫描）收成结论。

    为什么要自己再扫两次：`dex11_calib --yes` 会就地写零点偏移，**成没成只有角度能说明** ——
    标定完成后各爪角度应回到 0.000 rad 附近。把前后两次扫描摆在一起，现场一眼能看出
    "这次标定到底动没动"。
    """
    lines = ["零点标定（dex11_calib --yes）"]
    if "没有找到夹爪" in before:
        # 一只都没扫到就别往下标了 —— 工具会自己再扫一遍，结果一样，白等
        return lines + ["⚠ 标定前没找到夹爪，未执行标定（先查供电 / USB 线 / 串口是否被占）"]
    lines.append("标定前：" + "；".join(_summarize_scan(before)[:3]))

    if done.returncode != 0:
        lines.append(f"⚠ 标定程序退出码 {done.returncode}")
    body = [row.strip() for row in ((done.stdout or "") + (done.stderr or "")).splitlines()
            if row.strip()]
    problems = []
    for marker, sentence in (("refusing to calibrate", "⚠ 电机上报的 id 与请求的不一致，该爪未标定"),
                             ("does not reply", "⚠ 有电机没有应答（查线 / 供电）")):
        hits = [row for row in body if marker in row][:2]
        if hits:
            problems.append(f"{sentence}：" + "；".join(hits))
    # `err=<非 0>` 才是错误码 —— 正常输出里也有 `q=0.000 rad (err=0)`，
    # 把 `err=` 当关键词会把正常结果误判成故障（报"有问题"之前先证伪自己）
    bad = [row for row in body if re.search(r"err=([1-9]\d*)", row)][:2]
    if bad:
        problems.append("读到非 0 错误码：" + "；".join(bad))
    lines.extend(problems)
    if not problems and done.returncode == 0:
        lines.append("标定程序正常退出")

    lines.append("标定后：" + "；".join(_summarize_scan(after)[:3]))
    return lines


def _summarize_fcc(side: str, log_text: str, returncode: int) -> List[str]:
    """把 FCC 脚本的输出收成结果行（脚本本身已经把进度打成了人话）。

    默认把脚本的输出原样带进结果区（它就是"阶段 + 结果"几行），只在退出码非 0 时
    补一句结论；因为脚本已经把失败原因说清了（比如"收不到夹爪关节…"），
    这里再编一句只会和它打架。
    """
    rows = [row for row in log_text.splitlines() if row.strip()]
    if not rows:
        return [f"{GRIPPER_NAMES[side]} FCC 抓取没有任何输出（退出码 {returncode}）"]
    lines = list(rows)
    if returncode != 0 and not any(row.startswith(("⚠", "FCC 抓取已中断")) for row in rows):
        lines.append(f"⚠ FCC 抓取异常结束（退出码 {returncode}）")
    return lines


def read_robot_status(config: ConfigManager) -> dict:
    """读取 robot_monitor.py 写的状态文件，返回界面可直接使用的视图。

    available 只在「文件可读」且「监控仍在刷新（updated_at 未超过 TTL）」时为真。
    界面必须区分这两种"没有数据"：监控没启动 vs 监控在跑但一个状态都没收到。
    """
    path = config.get_robot_status_file()
    view = {
        "path": path,
        "available": False,
        "stale": False,
        "reason": "",
        "updated_at": 0.0,
        "age_sec": None,
        "monitor_pid": 0,
        "status": {},
        "module_state": {},
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        view["reason"] = "尚未启动机器人状态监控"
        return view
    except (OSError, ValueError) as exc:
        view["reason"] = f"状态文件不可用：{exc}"
        return view

    if not isinstance(payload, dict):
        view["reason"] = "状态文件格式不正确"
        return view

    # 字段类型一律当"外部输入"处理：状态文件由 robot_monitor.py 产生，中途被读到
    # 半截内容、或脚本版本不一致写出字符串/列表都是可能的。这里**不能**让 float()
    # 抛出去 —— 这个函数跑在 QTimer 槽里，异常会让 PyQt5 直接 abort 整个界面。
    updated_at = _as_float(payload.get("updated_at"))
    age = max(time.time() - updated_at, 0.0) if updated_at else None
    status = payload.get("status")
    if not isinstance(status, dict):
        status = {}
    # 模块使能状态（二期）：同属"外部输入"，类型不对就当没有，别让界面崩在槽里
    module_state = payload.get("module_state")
    view.update({
        "updated_at": updated_at,
        "age_sec": age,
        "monitor_pid": _as_int(payload.get("monitor_pid")),
        "status": status,
        "module_state": module_state if isinstance(module_state, dict) else {},
        "available": True,
    })
    ttl = config.get_robot_monitor_status_ttl()
    if age is None or age > ttl:
        view["stale"] = True
        view["available"] = False
        view["reason"] = f"状态已 {age or 0:.0f}s 未刷新，监控可能已停止"
    elif not payload.get("has_data"):
        view["reason"] = "监控已启动，但还没收到任何状态话题"
    return view


# ============ SDK 系统服务（wheel_controller.service） ============
# ============ 机器人主程序运行探测 ============
# 判定标志与机器人侧 run_nodes.sh（工作空间根目录）里的
# `ps aux | grep wheeled_robot_launch.py` 完全一致。两边必须同源，否则会出现
# "界面说没运行、脚本却直接 exit 0"的矛盾。
LAUNCH_PROCESS_MARKER = "wheeled_robot_launch.py"

# 急停覆盖的部件（《轮式机器人SDK接口》底盘控制 / 躯干控制两张表里的
# emergency_stop 话题，都是 std_msgs/msg/Bool：true 急停，false 退出急停）。
#
# 为什么只有两个：**手臂没有软件急停接口** —— SDK 的 C++ 侧只有
# ChassisControl::emergencyStop(bool) 与 TrunkControl::emergencyStop()，
# 手臂（ArmControl）没有对应接口。所以界面上的"急停"只能覆盖底盘与躯干，
# 这一点必须在 UI 与文档里如实说明，不能让人以为按下去就等于全都停了。
#
# 躯干急停的已知不确定性：文档的躯干使用示例要求先发
# /trunk_control/motor_mode 与 /trunk_control/connect true 才能控制关节，
# 但**没有说明急停是否也依赖连接状态**。这里按"急停是独立安全动作"实现
# （只发急停，不代发 connect/motor_mode）—— 后者会带来副作用（改模式、
# 重新连接设备），不该绑定在急停上。真机验证时若发现躯干急停不生效，
# 再评估是否需要补 connect。
EMERGENCY_STOP_TARGETS = (
    ("底盘", "/chassis_control/emergency_stop"),
    ("躯干", "/trunk_control/emergency_stop"),
)

EMERGENCY_STOP_PART_NAMES = "、".join(name for name, _topic in EMERGENCY_STOP_TARGETS)

# 关闭主程序**之前**要"下使能"的部件（各控制节点的 connect 话题，std_msgs/msg/Bool：
# true 连接/使能，false 断开连接）。
#
# 为什么必须在下杀进程之前发（现场 P0）：控制节点被杀死时，**硬件那边的使能状态不会
# 跟着复位**。躯干最明显 —— 上着使能直接关程序，驱动器留在使能（实时）状态里，
# 随后电机就不受控了。先让节点自己走完"下使能"，再收进程，才是干净的顺序。
#
# 与急停（EMERGENCY_STOP_TARGETS）的区别：急停是独立安全动作、只有底盘与躯干有接口；
# 这里是**正常关机流程**的一部分，三个部件都有 connect 话题可用，所以三个都发。
# 手臂没有急停接口，但有 connect —— 关程序时同样需要下使能。
DISCONNECT_TARGETS = (
    ("躯干", "/trunk_control/connect"),
    ("手臂", "/arm_control/connect"),
    ("底盘", "/chassis_control/connect"),
)

DISCONNECT_PART_NAMES = "、".join(name for name, _topic in DISCONNECT_TARGETS)

# 启动主程序后**自动**要连接的部件。
#
# 为什么要有这一步（现场反馈）：**不连底盘就没有 `/power_report`** —— 电量一直显示
# "未知"，每次都得手工补一条 `ros2 topic pub`。底盘接口文档里「4、获取电量状态」
# 就是 `Topic: /power_report`，它由底盘侧上报，连接之前不会发。
#
# 为什么只列底盘、不顺手把手臂/躯干也连上：给硬件上使能有安全含义（手臂会掉力、
# 躯干会抬头），"启动主程序"这个动作不该顺手把机构动了。手臂/躯干的使能由
# 遥操作启动时序（connect_sequence.py）与三期的使能管理显式控制。
AUTO_CONNECT_TARGETS = (
    ("底盘", "/chassis_control/connect"),
)


def _describe_code(code) -> str:
    """把 `ros2 topic pub` 的退出码翻成一句人话（日志里要能直接读）。

    两个特殊值先单独认：`None` = 没取到结果（进程起不来/被外层超时杀掉）—— 既不是成功
    也不是失败；`124` = `timeout` 命令自己的退出码，含义是"等了这么久还没匹配到订阅者"，
    现场最常见的原因就是主程序没在跑。
    """
    if code is None:
        return "未取到结果"
    if code == 124:
        return "等待超时（没有订阅者，主程序可能没在跑）"
    return f"退出码 {code}"


def _proc_argv(pid: int) -> List[str]:
    """读 /proc/<pid>/cmdline 并切分成 argv（内核以 NUL 分隔参数）。"""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace")
            for part in raw.split(b"\x00") if part]


def _is_launch_argv(argv: List[str]) -> bool:
    """判定一个进程的 argv 是否属于轮式机器人主程序。

    关键：marker 必须是**独立的参数**，不能只是某个长串的子串。
    实测踩过：`/proc/1/cmdline` 是整条 shell 命令原文，其中恰好提到过
    `wheeled_robot_launch.py`（比如正在 grep 它），用子串匹配就会把
    init 认成"主程序正在运行"，于是一开机就显示"运行中"。
    `ros2 launch wheeled_robot wheeled_robot_launch.py` 里它是独立参数，
    所以按参数比对既准确又不受命令行文本污染。
    """
    for arg in argv:
        if arg == LAUNCH_PROCESS_MARKER or arg.endswith("/" + LAUNCH_PROCESS_MARKER):
            return True
    return False


def find_launch_process() -> Optional[int]:
    """找到正在运行的轮式机器人主程序 pid；没有则返回 None。

    /proc 不可用时（非 Linux 开发机）退回 ps，保证探测不因平台而失效。
    """
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            if _is_launch_argv(_proc_argv(int(entry.name))):
                return int(entry.name)
        return None

    try:
        result = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (result.stdout or "").splitlines()[1:]:
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        if _is_launch_argv(fields[1].split()):
            try:
                return int(fields[0])
            except ValueError:
                continue
    return None


# ---------- 关闭主程序：残留节点清理 ----------
# 关闭主程序必须清掉"程序关了、节点还在"：run_nodes.sh 跑在**新终端**里，
# 我们手里的 Popen 是终端进程而不是 `ros2 launch`，单靠 terminate() 收不掉
# `ros2 launch … | python3 过滤日志` 这条管道拉起的节点 —— 现场 `ros2 topic list`
# 一看，/arm_control、/trunk_control、/navi_*、/ascamera 全在（实测就是这个现象）。
# 所以关停后按"工作空间归属"把残留进程整组收掉，再核验一次话题数。
#
# 不一起收的进程：这些是上位机按需单独拉起的辅助工具，生命周期与主程序无关
# （遥操作甚至可以在主程序停掉后单独跑标定）。判定用**路径片段**而不是包名，
# 避免工作空间路径里恰好含某个词时误判。
STOP_KEEP_MARKERS: Tuple[str, ...] = (
    "/install/lib/teleop/",
    "run_teleop",
    "/install/lib/teleop_video_streamer/",
    "vr_bridge",
    "pokepoint",
    "calibration_node",
)

# 关停信号阶梯：先给 ros2 launch 一个优雅收尾的机会（它会转发给子节点），
# 不退再升级。SIGKILL 放最后，只用于前两轮都不退的顽固进程。
STOP_SIGNAL_STEPS: Tuple[Tuple[int, float], ...] = (
    (signal.SIGINT, 6.0),
    (signal.SIGTERM, 4.0),
    (signal.SIGKILL, 1.5),
)


def _workspaces_from_command(cmd: str) -> List[str]:
    """从一条启动命令里解析出工作空间路径（比配置更贴近现场实际）。

    两种形态都要认：
    - `./run_nodes.sh` 的调用方通常先 `cd <ws> &&`；
    - 也可能写成绝对路径 `<ws>/run_nodes.sh`。
    """
    found: List[str] = []
    for match in re.finditer(r"(/[^\s;&|'\"]+)/run_nodes\.sh", cmd):
        found.append(match.group(1).rstrip("/"))
    for match in re.finditer(r"\bcd\s+(/[^\s;&|'\"]+)", cmd):
        found.append(match.group(1).rstrip("/"))
    # 去重且保持大致顺序（短的路径优先，避免把子目录当工作空间）
    unique: List[str] = []
    for item in sorted(set(found), key=len):
        if item and item not in unique:
            unique.append(item)
    return unique


def list_processes() -> List[dict]:
    """枚举进程：[{"pid", "pgid", "cmd"}]。/proc 不可用时退回 ps。"""
    result: List[dict] = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            argv = _proc_argv(pid)
            if not argv:
                continue
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                pgid = 0
            result.append({"pid": pid, "pgid": pgid, "cmd": " ".join(argv)})
        return result

    try:
        completed = subprocess.run(["ps", "-eo", "pid,pgid,args"], capture_output=True,
                                   text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return result
    for line in (completed.stdout or "").splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        try:
            result.append({"pid": int(fields[0]), "pgid": int(fields[1]),
                           "cmd": fields[2]})
        except ValueError:
            continue
    return result


#: 标定节点"就绪"的等待上限与轮询间隔（秒）。节点从起进程到把服务注册出来实测要十几秒，
#: 而 `ros2 service list` 自己每次约 1 秒 —— 2 秒一轮、上限 90 秒足够覆盖冷启动。
CAMERA_CALIB_READY_TIMEOUT = 90.0
CAMERA_CALIB_READY_INTERVAL = 2.0


class MainProgramCleaner:
    """把主程序遗留的 ROS 节点进程收干净（按工作空间归属判定，不做全局 pkill）。

    安全性靠三道闸门：
    1. 只认四类进程 —— `ros2 launch … wheeled_robot_launch.py`、跑 `run_nodes.sh`
       的 shell/终端、`<workspace>/install/` 下的可执行文件、以及从
       `run_nodes.sh` 命令行里**解析出来的工作空间**下的同类进程；
    2. 辅助工具（遥操作 / 视频流 / VR 桥 / 戳点 / 标定节点）一律放过；
    3. 永不动 pid ≤ 1、自己、以及与上位机同进程组的进程。
    这样即使有人在别处手动起了无关程序，也不会被"关闭程序"顺走。

    **为什么要解析工作空间**：配置里的 `workspace_root` 可能和现场实际在用的
    那份工作空间不是同一个目录（换过版本、并存多个副本都发生过）。只按配置路径
    匹配会"扫不到任何目标 → 报告没有残留 → 话题却还在"。这里从实际的启动命令里
    把工作空间路径读出来，比配置更可信。
    """

    def __init__(self, config, lister=None, sleeper=None):
        self.config = config
        self.workspace = str(getattr(config, "workspace", "") or "")
        self._lister = lister or list_processes
        self._sleep = sleeper or time.sleep
        self.skipped: List[str] = []      # 判定为放过或不可动的进程（诊断用）
        self.workspaces: List[str] = []   # 本次识别到的工作空间（配置 + 命令行解析）

    def keep_markers(self) -> Tuple[str, ...]:
        custom = getattr(self.config, "app_config", {}).get("stop_keep_markers")
        if isinstance(custom, list) and custom:
            return tuple(str(item) for item in custom)
        return STOP_KEEP_MARKERS

    def is_launcher(self, cmd: str) -> bool:
        """启动入口：`ros2 launch … wheeled_robot_launch.py` 或跑 run_nodes.sh 的 shell。"""
        if not cmd:
            return False
        return _is_launch_argv(cmd.split()) or "run_nodes.sh" in cmd

    def is_workspace_process(self, cmd: str, workspaces) -> bool:
        """工作空间里的节点进程（colcon 布局：<ws>/install/{lib,share}/…）。"""
        if not cmd:
            return False
        return any(f"{ws}/install/" in cmd for ws in workspaces if ws)

    def is_target(self, cmd: str) -> bool:
        if not cmd:
            return False
        if any(marker in cmd for marker in self.keep_markers()):
            return False
        # scan() 会把"从命令行解析出的真实工作空间"补进来；单独调用时退回配置里的那个
        workspaces = self.workspaces or ([self.workspace] if self.workspace else [])
        return self.is_launcher(cmd) or self.is_workspace_process(cmd, workspaces)

    def scan(self) -> List[dict]:
        mine = os.getpid()
        my_group = os.getpgid(0)
        rows = []
        for item in self._lister():
            try:
                pid = int(item.get("pid") or 0)
                pgid = int(item.get("pgid") or 0)
            except (TypeError, ValueError):
                continue
            rows.append((pid, pgid, item.get("cmd") or ""))

        # 先认启动入口，并从它的命令行里解析出**实际在用**的工作空间
        workspaces = {self.workspace} if self.workspace else set()
        for _pid, _pgid, cmd in rows:
            if self.is_launcher(cmd):
                workspaces.update(_workspaces_from_command(cmd))
        workspaces.discard("")
        self.workspaces = sorted(workspaces)

        found: List[dict] = []
        for pid, pgid, cmd in rows:
            if pid <= 1 or pid == mine or pgid == my_group:
                # 不动 init、不动自己、不动与上位机同组的进程（否则等于自杀）
                if cmd and self.is_target(cmd):
                    self.skipped.append(cmd)
                continue
            if self.is_target(cmd):
                found.append({"pid": pid, "pgid": pgid, "cmd": cmd})
        return found

    def _send(self, sig: int, pgid: int) -> bool:
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False            # 已经退干净了，不算失败
        except PermissionError:
            logger.warning(f"没有权限向进程组 {pgid} 发送信号")
            return False
        except OSError as exc:
            logger.debug(f"向进程组 {pgid} 发送信号失败: {exc}")
            return False

    def stop(self) -> dict:
        """按信号阶梯清理残留；返回 {"found", "steps", "survivors"}。"""
        report = {"found": [], "steps": [], "survivors": []}
        targets = self.scan()
        report["found"] = [item["cmd"] for item in targets]
        if not targets:
            report["steps"].append("没有发现残留节点进程")
            return report

        for sig, wait in STOP_SIGNAL_STEPS:
            groups = sorted({item["pgid"] for item in self.scan() if item["pgid"] > 0})
            if not groups:
                break
            sent = sum(1 for pgid in groups if self._send(sig, pgid))
            report["steps"].append(
                f"{signal.Signals(sig).name} → {sent}/{len(groups)} 个进程组")
            if not sent:
                break
            self._sleep(wait)

        report["survivors"] = [item["cmd"] for item in self.scan()]
        return report


# 上位机自己拉起的辅助节点（按注册名匹配，退出时兜底收干净）。
#
# 为什么要兜底：这些节点是上位机"点一下"拉起来的后台进程，**不随界面退出而结束**。
# 实测场景：做完相机标定没点「停止相机标定」就关界面 → /ascamera/*、/vision/* 等
# 话题留在机器人上，下一次启动看到的是一半旧一半新的节点表。
#
# 与 MainProgramCleaner 的边界：那个管"关闭程序"（清机器人主程序残留），
# 因此**放过**这些辅助工具；这个管"上位机退出"，只收自己拉起过的东西，
# **绝不碰机器人主程序**，也不做全局 pkill（见下面 stop_aux_nodes 的说明）。
AUX_NODE_NAME_PATTERNS: Tuple[str, ...] = (
    "camera_calib",          # 标定节点 + `ros2 service call` 客户端
    "pokepoint",
    "video_streamer",
    "vr_bridge",
    "calibration",
)


def read_self_cgroup() -> str:
    """读本进程所在的 cgroup 路径（Linux；读不到返回空串）。

    cgroup v1 与 v2 的格式不同，这里不做解析——只看里面**有没有出现服务名**就够了。
    """
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def runs_under_service(name: str, cgroup_text: Optional[str] = None) -> bool:
    """本界面是不是被 systemd 服务 `name` 拉起来的（同一 cgroup）。

    为什么必须知道这件事：如果上位机就在该服务的 cgroup 里，那么
    `systemctl --user stop <服务>` 会**连带把界面一起杀掉** ——
    现场看到的就是"输入这条命令，主页就崩了"，而且永远查不到代码里的异常
    （进程是被 SIGTERM 收走的，不是抛异常）。所以这里提前识别，好让界面
    在动手之前把话说清楚，或者干脆引导用户走「关闭程序」。
    """
    if not name:
        return False
    text = read_self_cgroup() if cgroup_text is None else cgroup_text
    if not text:
        return False
    unit = str(name).strip()
    key = unit[:-len(".service")] if unit.endswith(".service") else unit
    return bool(key) and key in text


class SdkServiceManager:
    """机器人主程序（SDK 服务）的运行状态。

    纪律（v4.7.3 收敛）：这个类**不下发任何 systemctl 指令**，既不 start 也不 stop。
    - 不 start：需要权限、unit 名随部署变化，实测在开发机上必然失败并让用户以为"点了没反应"；
    - 不 stop：停止流程归机器人侧/运维。上位机代为停过一次的代价是"界面跟着一起没了、
      还查不出原因"（服务级联杀 / 权限边界）。现在只把命令**贴给用户**，
      由人决定在哪个终端执行 —— 只读探测 + 提示，不替人按按钮。

    探测用进程而不是 systemctl：`systemctl --user is-active` 对未加载的 unit
    和非 systemd 部署一律返回非 0，会出现"服务明明在跑、界面却说没运行"。
    进程判定还有一个好处——它和 run_nodes.sh 的冲突检测是同一个判据。
    """

    def __init__(self, config: ConfigManager):
        self.config = config
        self.name = config.get_sdk_service_name()

    def launch_pid(self) -> Optional[int]:
        """正在运行的主程序 pid（None = 没在跑）。"""
        return find_launch_process()

    def is_running(self) -> bool:
        return self.launch_pid() is not None

    def stop_command(self) -> str:
        """停止命令。探测与停止用了不同手段，失败时把它交给用户手动执行。"""
        return f"systemctl --user stop {self.name}"

    def self_hosted(self) -> bool:
        """本界面是否运行在这个服务的 cgroup 里（= 停服务会连带杀掉界面）。"""
        return runs_under_service(self.name)

class Launcher:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.pm = ProcessManager()
        #: 常驻命令通道（`ros_bridge.RosBridge`）：懒创建 —— 第一次真要用它时才起进程，
        #: 打开界面不该凭空多一个 ROS 进程。见 `bridge()`。
        self._bridge = None
        self._bridge_lock = threading.RLock()
        #: 通道不可用时的原因（界面显示用；空 = 没试过或可用）
        self.bridge_note = ""

    # ---------- 常驻命令通道 ----------
    def bridge(self, on_line=None):
        """取常驻命令通道客户端（懒创建，不复用已死的进程）。

        **通道不是必须的**：起不来（没脚本 / 没 rclpy / 工作空间不在）时返回 None，
        调用方一律降级到 `_publish_topics` 那条一次性 CLI 通道 —— 那条路一直在，
        通道只是把\"每条命令都要重新发现\"这笔账省掉。
        """
        with self._bridge_lock:
            if self._bridge is not None and self._bridge.alive:
                return self._bridge
            bridge = RosBridge(self.config, env_prefix=self._env_prefix(),
                               source_prefix=self._source_setup_prefix(), on_line=on_line)
            if bridge.start():
                self._bridge = bridge
                self.bridge_note = ""
                return bridge
            self.bridge_note = bridge.unavailable_reason()
            self._bridge = None
            return None

    def close_bridge(self):
        """关掉通道（退出程序时调用；失败不影响退出）。"""
        with self._bridge_lock:
            bridge, self._bridge = self._bridge, None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:            # noqa: BLE001 —— 退出路径不该被它拖住
                logger.warning(f"关闭命令通道失败: {exc}")

    # ---------- 统一脚本调用约定 ----------
    def _script_for(self, key: str, label: str, default: str = "") -> Path:
        """取 scripts/ 下的脚本并做存在性校验（所有脚本调用的唯一入口）。"""
        try:
            script = (self.config.get_script_path(key, default) if default
                      else self.config.get_script_path(key))
        except KeyError as exc:
            raise KeyError(f"{label}未配置: {key}") from exc
        if not script.exists():
            raise FileNotFoundError(f"{label}不存在: {script}")
        if not os.access(script, os.X_OK):
            # 统一用 bash/python3 调用，缺可执行位不影响运行，只告警。
            logger.warning(f"{label}没有执行权限（不影响运行）: {script}")
        return script

    def _env_prefix(self) -> str:
        """脚本统一通过环境变量定位路径：ROBOT_WORKSPACE / NERV_DIR / BAG_DIR / LOG_DIR。"""
        exports = self.config.get_script_env()
        return "".join(f"export {name}={shlex.quote(value)}; " for name, value in exports.items())

    def _setup_script(self) -> Path:
        return self.config.workspace / "install/setup.bash"

    def _source_setup_prefix(self) -> str:
        """需要 ROS 环境的命令（如直接 python3 运行脚本）先 source 工作空间的 setup.bash。

        脚本内部自带 source 的（run_nodes/run_teleop/record_*/play_bag）不需要该前缀。
        """
        setup_script = self._setup_script()
        if not setup_script.exists():
            return ""
        return f"source {shlex.quote(str(setup_script))} && "

    def _run_in_new_terminal(self, label: str, script_name: str, cmd: str,
                             process_name: str) -> subprocess.Popen:
        logger.info(f"在新终端启动{label} ({script_name}): {cmd}")
        proc = run_in_terminal(cmd, self.config.get_terminal())
        self.pm.register(proc, process_name)
        return proc

    # ---------- 脚本启动的统一入口 ----------
    def _sudo_preauth_prefix(self) -> str:
        """sudo 预授权前缀。

        未配置密码时不带 `sudo -S`：机器人上通常是免密 sudo 或已缓存凭据；
        配置了就沿用管道喂密码的老行为。与 cwd 无关，故可放在 cd 之前。
        """
        sudo_password = self.config.get_sudo_password()
        if sudo_password:
            return f"echo {shlex.quote(sudo_password)} | sudo -S -v 2>/dev/null && "
        logger.info(
            "未配置 sudo 密码，跳过 sudo 预授权"
            "（需要密码时请设置 DATA_COLLECTOR_SUDO_PASSWORD）"
        )
        return ""

    def _build_cmd(self, script: Path, args=(), *, cwd=None, env: str = "script",
                   runner: str = "bash", prelude: str = "", hold: bool = False) -> str:
        """组装一条脚本调用命令：prelude + cd + 环境 + 解释器 + 参数（+ 保活尾巴）。

        env 三种取值对应脚本对 ROS 环境的三种依赖方式：
          - "script"：脚本内部自带 source（run_nodes/run_teleop/record_*/play_bag）
          - "ros"   ：由 python3 直接执行，需要先 source setup.bash
          - "none"  ：连脚本环境变量都不需要（setup_product.sh）
        """
        work_dir = Path(cwd) if cwd else self.config.workspace
        if env == "script":
            env_prefix = self._env_prefix()
        elif env == "ros":
            env_prefix = self._env_prefix() + self._source_setup_prefix()
        else:
            env_prefix = ""

        args_str = (" " + " ".join(args)) if args else ""
        cmd = (
            f"{prelude}"
            f"cd {shlex.quote(str(work_dir))} && "
            f"{env_prefix}"
            f"{runner} {shlex.quote(str(script))}{args_str}"
        )
        if hold:
            cmd += (
                f" ; echo \"=== {script.name} 已退出，退出码: $? ===\" ; "
                f"read -p '按 Enter 键关闭窗口...'"
            )
        return cmd

    def _start_background(self, cmd: str, label: str, *, cwd=None,
                          log_file: Optional[Path] = None,
                          proc_name: str = "") -> subprocess.Popen:
        """后台启动一条命令并注册进程；日志文件不可写时退化为丢弃输出。"""
        work_dir = Path(cwd) if cwd else self.config.workspace
        if log_file is not None:
            try:
                proc = run_background_with_log(cmd, work_dir, log_file)
                logger.info(f"后台启动{label}（日志: {log_file}）: {cmd}")
            except OSError as exc:
                logger.warning(f"{label}日志文件不可写({exc})，改为丢弃输出: {log_file}")
                proc = run_background(cmd, work_dir)
        else:
            proc = run_background(cmd, work_dir)
            logger.info(f"后台启动{label}: {cmd}")
        self.pm.register(proc, proc_name or label)
        return proc

    def _run_script(self, script: Path, label: str, args=(), *, mode: str = "terminal",
                    cwd=None, env: str = "script", runner: str = "bash",
                    need_setup: bool = False, hold: bool = False, prelude: str = "",
                    log_file: Optional[Path] = None,
                    proc_name: str = "") -> subprocess.Popen:
        """统一的脚本启动入口：校验 → 组装命令 → 按模式分发 → 注册进程。

        各 launch_* 只决定"用哪个脚本、带什么参数"，进程怎么起（新终端 / 后台）、
        带哪种环境前缀、退出后窗口留不留，一律在这里处理。否则每加一个脚本就要把
        这几段再抄一遍 —— 而"复制粘贴的守卫"必然会被漏掉：`check_workspace()`
        原先在 9 个方法里有、在 play_bag / monitor_performance 里漏了，就是这么来的。
        """
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        if need_setup and not self._setup_script().exists():
            raise FileNotFoundError(f"setup.bash 不存在: {self._setup_script()}")

        cmd = self._build_cmd(script, args, cwd=cwd, env=env, runner=runner,
                              prelude=prelude, hold=hold)
        if mode == "terminal":
            return self._run_in_new_terminal(label, script.name, cmd,
                                             proc_name or label)
        return self._start_background(cmd, label, cwd=cwd, log_file=log_file,
                                      proc_name=proc_name)

    def launch_run_nodes(self, options: List[str]) -> subprocess.Popen:
        """启动主程序（新终端，退出后保活以便查看报错）。"""
        script = self._script_for("run_nodes_script", "主程序脚本")
        return self._run_script(
            script, "主程序", options, mode="terminal",
            need_setup=True, hold=True,
            prelude=self._sudo_preauth_prefix(), proc_name="run_nodes")

    # ---------- 关程序前的下使能 ----------
    def _publish_topics(self, entries, timeout: float, label: str,
                        keep_alive: float = 2.0) -> dict:
        """向若干话题**并发**下发一次性消息，返回 ``{名字: 退出码}``。

        `entries` 是 ``(名字, 话题, 消息类型, payload)`` 的序列，`payload` 是
        `ros2 topic pub` 里那段 YAML —— 例如 ``"data: false"``（Bool）或
        ``"{side: 0, id: 0, start_addr: 0, position: [300, 100, 300]}"``（HandPosture）。

        这是**上位机侧对外下发的唯一机制**（急停 / 关闭前下使能 / 启动后连底盘 / 夹爪
        四处共用），四条性质都一样：

        1. 一次性 `ros2 topic pub`，不依赖任何常驻进程存活；
        2. 并发发 —— 每条都要等 DDS 发现，串行就是 N 倍等待；
        3. **`-w 1`（等匹配到至少一个订阅者再发）**：发现没完成就发出去的消息会被丢掉，
           等于没发。代价是"压根没有订阅者"时会一直等，所以每条都套 `timeout` 兜住；
        4. `--keep-alive` 覆盖晚加入的订阅者，也给硬件留出真正执行的时间
           （"下使能"需要它，单次指令只要 1 秒即可）。

        Args:
            entries: ``(名字, 话题, 消息类型, payload)`` 序列。
            timeout: 每条命令的等待上限（秒）。
            label: 日志里那句"在做什么"。
            keep_alive: `--keep-alive` 的秒数。

        Returns:
            ``{名字: 退出码}``；``None`` 表示"没取到结果"（子进程起不来 / 被外层超时
            杀掉）—— 调用方据此如实汇报，既不能当成功也不能当失败。
        """
        if not self.config.check_workspace():
            return {}

        launches, waits = [], []
        for index, (_name, topic, msg_type, payload) in enumerate(entries, start=1):
            launches.append(
                f"timeout {int(timeout)} ros2 topic pub --once -w 1 "
                f"--keep-alive {keep_alive:g} "
                f"{topic} {msg_type} \"{payload}\" >/dev/null 2>&1 & pid{index}=$!"
            )
            waits.append(f'wait $pid{index}; rc{index}=$?; echo "rc{index}=$rc{index}"')
        # 子 shell 包住整段的原因同急停：`&` 与 `;` 混用会按优先级被拆错，必须显式分组
        body = "; ".join(launches) + "; " + "; ".join(waits)
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"{self._source_setup_prefix()}( {body} )")
        logger.warning(f"{label}（并发 {len(entries)} 条）: {cmd}")

        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=timeout + 8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"下发指令失败：{exc}")
            return {name: None for name, *_rest in entries}

        codes = {}
        for line in (completed.stdout or "").splitlines():
            matched = re.match(r"rc(\d+)=(-?\d+)\s*$", line.strip())
            if matched:
                codes[int(matched.group(1))] = int(matched.group(2))
        return {name: codes.get(index)
                for index, (name, *_rest) in enumerate(entries, start=1)}

    def _publish_bool_topics(self, targets, value: bool, timeout: float) -> dict:
        """向若干 connect 话题**并发**下发一条 `std_msgs/Bool`，返回 {部件名: 退出码}。

        取值 None 表示"没取到结果"（子进程起不来/被外层超时杀掉），调用方据此如实汇报。

        三条与急停（`launch_emergency_stop`）一致的选择：
        1. 一次性 `ros2 topic pub`，不依赖任何常驻进程存活；
        2. 并发发 —— 每条都要等 DDS 发现，串行就是 N 倍等待；
        3. **`-w 1`（等匹配到订阅者再发）**：发现没完成就发出去的消息会被丢掉，等于没发。
           代价是"没有订阅者"时会一直等 → 每条套 `timeout` 兜住。

        `--keep-alive 2` 让发布进程多活 2 秒：覆盖晚加入的订阅者，也给硬件留出
        真正执行（下使能 / 建链）的时间 —— 调用方靠"这些进程退出"来判断时机到了。

        具体命令拼装见 `_publish_topics`（夹爪那条路也走它，两边是同一套机制）。
        """
        flag = "true" if value else "false"
        return self._publish_topics(
            [(name, topic, "std_msgs/msg/Bool", f"data: {flag}") for name, topic in targets],
            timeout, f"并发下发 Bool({flag})")

    # ---------- 姿态控制的下发：常驻通道优先，一次性 CLI 兜底 ----------
    def publish_motion(self, entries, timeout: float = 6.0, label: str = "姿势控制",
                       on_line=None) -> dict:
        """「姿势控制」页面的下发入口：**常驻通道优先，一次性 CLI 兜底**。

        为什么加通道（v6.2.0，现场：「成功率与效果十分不满意，重构」）：
        `ros2 topic pub --once -w 1` 每条命令都要**冷启动一个进程 + 重做一遍 DDS 发现**
        （0.3~3s），等不到订阅者就退出码 124，于是"发现慢"被如实报成"未确认送达" ——
        这是假失败的主要来源；而且单次发布在 VOLATILE 下丢了就永远没了。
        常驻通道里 publisher 建一次、等订阅者出现再发、默认重发 3 次、并回读关节角确认到位。

        **两条通道一套语义**：都返回 ``{条目名: {ok, reason, matched, sent, channel}}``，
        `ok` 取 ``None`` = 未确认（既不能当成功也不能当失败）。
        通道起不来时自动降级，并在结果里标 `channel="一次性 CLI"`，
        **不改变原有可用性**（本机没 ROS 时界面照旧全靠兜底路径）。

        `timeout` 只对兜底路径与"等订阅者"有意义；通道那条由脚本自己的 `wait_sub` 决定。
        """
        entries = list(entries)
        bridge = self.bridge(on_line=on_line)
        if bridge is not None:
            results = bridge.publish(entries, timeout=max(timeout, 8.0), on_line=on_line)
            if results is not None:
                for item in results.values():
                    item["channel"] = "常驻通道"
                if all(item.get("ok") is not None for item in results.values()):
                    return results
                logger.warning(f"{label}：通道未回话，降级到一次性 CLI")
                self.close_bridge()
            else:
                logger.warning(f"{label}：通道没回结果，降级到一次性 CLI")
        # `keep_alive=2.0`（原来是 1.0）：现场 2026-09-29 的证据是"一次发两条只到一条"——
        # `--once` 的退出码 0 只证明**匹配到订阅者**，而 `--keep-alive` 到点进程就退，
        # RELIABLE 的交付握手可能没走完。降级路径本来就**没有回读确认**，
        # 所以这里用时间换概率（并发下发，墙钟成本 ≈ 2s）。
        codes = self._publish_topics(entries, timeout, label, keep_alive=2.0)
        return {entry[0]: self._cli_result(entry[0], codes) for entry in entries}

    @staticmethod
    def _cli_result(name: str, codes) -> dict:
        """把一次性 CLI 的退出码翻成统一结果（**措辞与通道那条保持一致**）。"""
        code = (codes or {}).get(name)
        if code is None:
            return {"ok": None, "reason": "没有回取到结果（下发进程没回话）",
                    "matched": -1, "sent": 0, "channel": "一次性 CLI"}
        if code == 0:
            return {"ok": True, "reason": "", "matched": -1, "sent": 1,
                    "channel": "一次性 CLI"}
        if code == 124:
            return {"ok": False, "matched": 0, "sent": 0, "channel": "一次性 CLI",
                    "reason": "等待超时：没等到订阅者（主程序没在跑？）"}
        return {"ok": False, "matched": -1, "sent": 0, "channel": "一次性 CLI",
                "reason": f"退出码 {code}"}

    def move_trunk(self, values_deg, *, tol: float = 1.0, timeout: float = 15.0,
                   retries: int = 2, on_line=None) -> dict:
        """走一条躯干点位目标并**确认到位**（回读 `trunk_state`，带残差与重发）。

        返回 ``{ok, residual, attempts, matched, reason}``；`ok=None` = 未确认。
        通道不可用时返回 ``{"ok": None, "reason": <原因>}`` —— 由调用方决定是否降级。
        """
        bridge = self.bridge(on_line=on_line)
        if bridge is None:
            return {"ok": None, "residual": None, "attempts": 0, "matched": -1,
                    "reason": self.bridge_note or "常驻通道不可用"}
        return bridge.move("trunk", values_deg, tol=tol, timeout=timeout,
                           retries=retries, on_line=on_line)

    def move_arm(self, side: str, values_deg, *, tol: float = 2.0, timeout: float = 15.0,
                 retries: int = 2, on_line=None) -> dict:
        """走**一条**手臂点位目标（`ArmMove`）并确认到位。

        **发之前通道会自动退出 follow / servoj** —— `*_arm_move` 只在"非 follow position
        模式"生效（SDK 原文），而遥操作会把手臂留在 follow 模式：此时 ArmMove 被**静默忽略**，
        外部表现就是"预设点了没反应"。
        """
        result = self.move_arms({side: values_deg}, tol=tol, timeout=timeout,
                                retries=retries, on_line=on_line)
        item = next(iter((result.get("items") or {}).values()), {})
        return {"ok": result.get("ok"), "residual": item.get("residual"),
                "attempts": result.get("attempts", 0), "matched": item.get("matched", -1),
                "reason": result.get("reason", "")}

    def move_arms(self, targets_deg, *, tol: float = 2.0, timeout: float = 15.0,
                  retries: int = 2, on_line=None) -> dict:
        """**一次下发两侧（或多侧）手臂目标**，然后一起等到位。

        为什么要批量（语义依据）：预设里左右臂本来就是**同一时刻**的目标，官方手柄示例
        （`arm_proxy.cpp` 的 `sendConfigMotion`）也是"先把两条 `ArmMove` 都发出去，
        再一起等 `arm_move_over_`"。逐条走会让镜像动作变成"先左后右"。

        Args:
            targets_deg: ``{"left": [度×7], "right": [度×7]}``，只放要动的那几侧。

        Returns:
            ``{ok, items: {键: {...}}, attempts, reason}``；`attempts == 0` = 通道没跑起来。
        """
        bridge = self.bridge(on_line=on_line)
        if bridge is None:
            return {"ok": None, "items": {}, "attempts": 0,
                    "reason": self.bridge_note or "常驻通道不可用"}
        items = [{"kind": "arm", "side": side, "target": list(values), "tol": tol}
                 for side, values in targets_deg.items()]
        if not items:
            return {"ok": False, "items": {}, "attempts": 0, "reason": "没有要动的手臂"}
        return bridge.move_batch(items, timeout=timeout, retries=retries, on_line=on_line)

    def ensure_trunk_non_realtime(self, on_line=None) -> dict:
        """把躯干切到**非实时**（`motor_mode 0` + 重新 `connect`）—— 会动机构（抬头）。

        用途只有一个：**遥操作之后**。遥操作的上电时序会把躯干切成实时模式
        （`connect → motor_mode 1 → connect`），而实时模式下躯干按最大速度追目标、
        需要上位机自己插值；本页只发单条点位目标，所以必须切回非实时。
        """
        bridge = self.bridge(on_line=on_line)
        if bridge is None:
            return {"ok": None, "reason": self.bridge_note or "常驻通道不可用"}
        return bridge.ensure("trunk_non_realtime", on_line=on_line)

    def set_trunk_locked(self, locked: bool, on_line=None) -> dict:
        """躯干**自锁** / **解除自锁**（v6.4.13）。

        - **自锁**（`locked=True`）：往 `/trunk_control/switch_power_on` 发一条
          `UInt8MultiArray`，把 `core_motion.TRUNK_LOCK_INDEXES`（H1~H3）切成
          **「仅上电」** —— 电机**退出使能**。效果：**还能读角度**（`trunk_state` 照常），
          但**不接受运动指令**。做法逐字取自现场脚本
          `shell-switch_pown_on_test/swich_pown_on.sh` 里那段「#自锁」。
        - **解除**（`locked=False`）：`connect false` → 等 → `connect true`
          （现场确认的两步；单发一条 `connect true` 不生效）。
          ⚠️ **会动机构**：`connect true` 会让躯干抬头，调用方必须先确认。

        **状态不可回读**：ROS 侧没有任何"当前是否自锁"的话题
        （`getMotorBrakeState()` 只存在于 C++ SDK 接口里），所以这里只说
        "发出去了 / 没发出去"，**不说"现在是自锁状态"**。界面同理。

        走**常驻通道**（与其它"一次性指令"同一条路），不新开机制。
        """
        bridge = self.bridge(on_line=on_line)
        if bridge is None:
            return {"ok": None, "reason": self.bridge_note or "常驻通道不可用"}
        # 解除是两步、每步都要等够（见通道里的 TRUNK_LOCK_SETTLE），
        # 所以给它比默认 20s 更宽的窗口，别让"其实在做"被读成超时。
        timeout = 20.0 if locked else 40.0
        return bridge.ensure("trunk_lock" if locked else "trunk_unlock",
                             timeout=timeout, on_line=on_line)

    def ensure_arm_point_mode(self, side: str, on_line=None) -> dict:
        """让某侧手臂退出 follow / servoj（`ArmMove` 的前置条件）。"""
        bridge = self.bridge(on_line=on_line)
        if bridge is None:
            return {"ok": None, "reason": self.bridge_note or "常驻通道不可用"}
        return bridge.ensure("arm_point_mode", side=side, on_line=on_line)

    def read_motion_state(self) -> Optional[dict]:
        """从通道读一次实时的躯干 / 手臂角度（度）。

        与状态文件（`robot_monitor` 写的 JSON，0.4s 一份）的分工：这里是**通道顺带订阅到的
        最新一帧**，用来判"到没到位"；界面显示仍以状态文件为准（它是既有链路，不动）。
        """
        bridge = self.bridge()
        if bridge is None:
            return None
        return bridge.state()

    def run_motion_streams(self, jobs, on_line=None, is_aborted=None,
                           timeout: Optional[float] = None) -> dict:
        """并发执行若干**实时流**任务，返回 ``{任务键: True/False/None}``。

        为什么要单开这条通道（与"只有一条下发机制"的关系）：`ros2 topic pub --once`
        的一条命令要 0.3~1s，**发不出 100Hz**，而实时模式下目标位置必须由上位机自己插值
        （SDK 文档原文）。所以连续运动交给 `scripts/motion_stream.py`（rclpy，一条进程活几秒），
        其余一次性指令仍然全部走 `_publish_topics` —— 两条通道各自只有一套语义：

        * `True`  = 脚本正常退出（它自己走到位才退，"到位"是它打印并返回 0 的）
        * `False` = 脚本非 0 退出（读不到当前角度、参数不合法、缺环境…），原因已随进度行打出
        * `None`  = **没取到结果**（超时/被强杀），调用方必须按"未经确认"如实汇报

        Args:
            jobs: `core_motion` 的流任务字典序列，``(kind, side, values, duration, hold, rate)``。
            on_line: 每收到一行输出就回调一次（界面拿它写结果区）。
            is_aborted: 返回 True 时终止所有子进程（脚本自己会在 `finally` 里退出 follow_position）。
            timeout: 整批任务的上限（秒）；默认按任务时长 + 启动开销算。
        """
        jobs = list(jobs)
        keys = [stream_job_key(job) for job in jobs]
        if not jobs:
            return {}

        def emit(line: str) -> None:
            if on_line is not None:
                try:
                    on_line(line)
                except Exception as exc:            # noqa: BLE001 —— 界面问题不该打断动作
                    logger.warning(f"实时流进度回调失败: {exc}")

        if not self.config.check_workspace():
            emit(f"工作空间不存在（{self.config.workspace}），没有下发任何实时流")
            return {key: False for key in keys}
        script = self.config.resolve_script_path(MOTION_STREAM_SCRIPT)
        if not script.is_file():
            emit(f"找不到实时流脚本 {script} —— 它随上位机交付包发布在 scripts/ 下")
            return {key: False for key in keys}

        if timeout is None:
            timeout = max((job["duration"] + job["hold"] for job in jobs), default=0.0) \
                + STREAM_TIMEOUT_PAD

        procs: Dict[str, subprocess.Popen] = {}
        readers: Dict[str, threading.Thread] = {}
        queues: Dict[str, "queue.Queue"] = {}
        for job, key in zip(jobs, keys):
            args = " ".join(shlex.quote(item) for item in stream_job_args(job))
            cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
                   f"{self._source_setup_prefix()}python3 {shlex.quote(str(script))} {args}")
            logger.warning(f"实时流（{key}）: {cmd}")
            try:
                # `start_new_session`：让脚本自己占一个进程组，中止时能整组收到信号
                # （否则 `terminate()` 只会打到外层 bash，python3 收不到、follow_position 退不出来）
                proc = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                                        start_new_session=True)
            except OSError as exc:
                emit(f"[{key}] 起不来实时流进程：{exc}")
                procs[key] = None
                continue
            procs[key] = proc
            box: "queue.Queue" = queue.Queue()
            queues[key] = box

            def pump(handle=proc, target=box, name=key):
                try:
                    for raw in handle.stdout or []:
                        target.put(raw.rstrip("\n"))
                finally:
                    target.put(None)

            reader = threading.Thread(target=pump, daemon=True)
            readers[key] = reader
            reader.start()

        results: Dict[str, Optional[bool]] = {key: None for key in keys}
        deadline = time.monotonic() + float(timeout)
        pending = {key for key, proc in procs.items() if proc is not None}
        aborted = False
        while pending and time.monotonic() < deadline:
            if is_aborted is not None and is_aborted():
                aborted = True
                break
            for key in list(pending):
                box = queues.get(key)
                if box is None:
                    pending.discard(key)
                    continue
                closed = False
                while True:
                    try:
                        item = box.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        closed = True                     # 读线程收尾（stdout 已到 EOF）
                        break
                    emit(f"[{key}] {item}")
                proc = procs[key]
                if closed or proc.poll() is not None:
                    pending.discard(key)
            if pending:
                time.sleep(0.05)

        for key in list(pending):
            proc = procs.get(key)
            if proc is None:
                continue
            if aborted:
                emit(f"[{key}] 中止：正在结束这个实时流（脚本会先退出 follow_position 再退）")
            else:
                emit(f"[{key}] 超过 {float(timeout):.0f}s 还没结束 —— 结束它，结果**未经确认**")
            _terminate_stream(proc)

        for key in keys:
            box = queues.get(key)
            if box is not None:
                while True:                          # 收尾：把已经打出来的行都放出去
                    try:
                        item = box.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        emit(f"[{key}] {item}")
            proc = procs.get(key)
            if proc is None:
                results[key] = False
                continue
            try:
                code = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                _terminate_stream(proc, force=True)
                code = None
            if aborted:
                # 中止时"脚本退没退干净"不能当成"走没走到位"，两件事分开说
                emit(f"[{key}] 已在执行中中止 —— 这一条**没有走到位**")
                results[key] = None
                continue
            if code is None:
                emit(f"[{key}] 收不到退出码 → **该条未经确认**")
                results[key] = None
            elif code == 0:
                results[key] = True
            else:
                emit(f"[{key}] 脚本以退出码 {code} 结束（原因见上面那几行）")
                results[key] = False
        return results

    def release_hardware_connections(self, timeout: float = 4.0) -> List[str]:
        """向 躯干 / 手臂 / 底盘 下发 `connect=false`（下使能），返回可直出的日志行。

        **必须在下杀进程之前调用**（原因见 DISCONNECT_TARGETS）：控制节点被杀死时，
        硬件那边的使能状态不会跟着复位 —— 躯干会留在使能/实时状态里，随后电机就不受控。

        下发机制见 `_publish_bool_topics`（一次性 CLI、并发、`-w 1` 等发现、每条套 timeout）。
        这里只负责**把结果讲清楚**：分开"确认送达"与"未确认" —— 这三条是下使能，
        没送到就意味着那个部件可能还上着使能，需要人工确认，不能混进一句"已完成"。
        """
        if not self.config.check_workspace():
            return [f"工作空间不存在（{self.config.workspace}），跳过断开连接指令"]

        codes = self._publish_bool_topics(DISCONNECT_TARGETS, False, timeout)

        delivered, unconfirmed = [], []
        for name, code in codes.items():
            if code == 0:
                delivered.append(name)
            elif code is None:
                unconfirmed.append(f"{name}（未取到结果）")
            elif code == 124:
                unconfirmed.append(f"{name}（等待超时：没有订阅者，节点可能没在跑）")
            else:
                unconfirmed.append(f"{name}（ros2 topic pub 退出码 {code}）")

        lines = []
        if delivered:
            lines.append(f"关闭前已下发断开连接（下使能）：{'、'.join(delivered)}")
        if unconfirmed:
            lines.append("以下部件未确认送达：" + "、".join(unconfirmed)
                         + " —— 若它上着使能，请人工确认已下使能")
        return lines

    def connect_chassis(self, timeout: float = 30.0) -> List[str]:
        """启动主程序后自动连接底盘（`/chassis_control/connect = true`），返回日志行。

        为什么需要它：**不连底盘就没有 `/power_report`** —— 电量一直显示"未知"，
        现场每次都得手工补一条（见 AUTO_CONNECT_TARGETS）。

        为什么超时给到 30s（其它下发只有几秒）：主程序在新终端里 `ros2 launch`，
        底盘节点要过几秒才起来。`-w 1` 会**一直等它出现、一出现就发** —— 也就是
        "等底盘节点就绪再连"，不必猜 sleep 多少。跑在后台线程里，不占界面。
        """
        codes = self._publish_bool_topics(AUTO_CONNECT_TARGETS, True, timeout)
        if not codes:
            return [f"工作空间不存在（{self.config.workspace}），跳过底盘连接"]

        lines = []
        delivered = [name for name, code in codes.items() if code == 0]
        if delivered:
            lines.append(f"已自动连接底盘（{'、'.join(delivered)}）"
                         "—— /power_report 电量数据随后上报")
        for name, code in codes.items():
            if code == 0:
                continue
            if code == 124:
                lines.append(f"底盘连接未确认：{name} 等待超时（底盘节点没起来？"
                             "电量仍会显示未知，可稍后手工补一条 connect）")
            elif code is None:
                lines.append(f"底盘连接未确认：{name}（没取到结果）")
            else:
                lines.append(f"底盘连接失败：{name}（ros2 topic pub 退出码 {code}）")
        return lines

    # ---------- 三期：单模块上 / 下使能 ----------
    def set_module_enabled(self, module: str, enabled: bool,
                           per_step_timeout: float = 8.0) -> List[str]:
        """上/下使能单个模块（`trunk` / `arms`），返回可直出的日志行。

        三条与既有并发下发（`_publish_bool_topics`）**刻意不同**的地方：

        1. **按序下发**，不并发：躯干那条序列本身有前后依赖（connect → motor_mode →
           connect），并发出去顺序就没有保证了；
        2. **每步之间真的等**：等待值取自上电时序（实测 connect→抬头完成约 2.8s），
           删掉会出现"上一条还没生效就发下一条"；
        3. **成没成不在这里下结论**：这里只保证"发出去了"，生效与否由调用方轮询状态
           是否翻转（见界面 `PostureDialog` 的模块使能条）—— 退出码 0 只说明命令发完了。

        v6.2.0 起**优先走常驻命令通道**（等订阅者 + 重发，且不必每步冷启动一个进程，
        3 步序列能省下 3 次 DDS 发现）；通道不可用时自动降级到原来的并发...不，是
        **按序 CLI** 那条路，行为与之前完全一致。

        Args:
            module: `trunk`（躯干）/ `arms`（双臂）。
            enabled: True = 上使能，False = 下使能。
            per_step_timeout: 降级路径里单条 `ros2 topic pub` 的等待上限（秒）。
                `-w 1` 会一直等订阅者出现，节点没在跑时靠它兜住，否则整条命令会挂住。

        Returns:
            日志行列表：分开"已下发"与"未确认"，不混成一句"已完成"。

        Raises:
            ValueError: 模块名或动作不在 `MODULE_STEPS` 里（编程错误，不是运行时情况）。
        """
        steps = MODULE_STEPS.get((module, enabled))
        if not steps:
            raise ValueError(f"未知的模块动作: {module}/{enabled}")
        name = MODULE_NAMES[module]
        verb = "上使能" if enabled else "下使能"
        if not self.config.check_workspace():
            return [f"工作空间不存在（{self.config.workspace}），{name}{verb}未下发任何指令"]

        def msg_of(kind):
            return "std_msgs/msg/Byte" if kind == "byte" else "std_msgs/msg/Bool"

        def payload_of(value):
            flag = str(value).lower() if isinstance(value, bool) else str(value)
            return f"data: {flag}"

        bridge = self.bridge()
        if bridge is not None:
            delivered, unconfirmed = [], []
            for label, topic, kind, value, wait in steps:
                result = (bridge.publish([(label, topic, msg_of(kind), payload_of(value))])
                          or {}).get(label) or {"ok": None, "reason": "通道没回话"}
                if result.get("ok"):
                    delivered.append(f"{label}（发 {result.get('sent', 1)} 次，"
                                     f"订阅者 {result.get('matched', -1)}）")
                else:
                    unconfirmed.append(f"{label}（{result.get('reason') or '未确认'}）")
                time.sleep(min(float(wait), 5.0))       # 每步之间真的等（硬件动作时间）
            lines = []
            if delivered:
                lines.append(f"{name}{verb}指令已下发（常驻通道）：{'、'.join(delivered)}")
            if unconfirmed:
                lines.append(f"{name}{verb}未确认：{'、'.join(unconfirmed)}")
            return lines

        parts = []
        for index, (_label, topic, kind, value, wait) in enumerate(steps, start=1):
            parts.append(
                f"timeout {int(per_step_timeout)} ros2 topic pub --once -w 1 --keep-alive 2 "
                f"{topic} {msg_of(kind)} \"{payload_of(value)}\" >/dev/null 2>&1; "
                f"rc{index}=$?; echo \"rc{index}=$rc{index}\"; sleep {wait}"
            )
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"{self._source_setup_prefix()}( {'; '.join(parts)} )")
        logger.warning(f"{name}{verb}（{len(steps)} 步）: {cmd}")

        # 预算 = 每步等待 + 每步的等待订阅者上限 + 收尾余量；超了就当"没取到结果"
        budget = sum(step[4] for step in steps) + (per_step_timeout + 4) * len(steps)
        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=budget)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"{name}{verb}指令失败：{exc}")
            return [f"{name}{verb}指令未能确认（{exc}）"
                    f" —— 请人工确认{name}的使能状态"]

        codes = {}
        for line in (completed.stdout or "").splitlines():
            matched = re.match(r"rc(\d+)=(-?\d+)\s*$", line.strip())
            if matched:
                codes[int(matched.group(1))] = int(matched.group(2))

        delivered, unconfirmed = [], []
        for index, (label, _topic, _kind, _value, _wait) in enumerate(steps, start=1):
            code = codes.get(index)
            if code == 0:
                delivered.append(label)
            elif code is None:
                unconfirmed.append(f"{label}（未取到结果）")
            elif code == 124:
                unconfirmed.append(f"{label}（等待超时：没有节点在订阅，主程序没在跑？）")
            else:
                unconfirmed.append(f"{label}（ros2 topic pub 退出码 {code}）")

        lines = []
        if delivered:
            lines.append(f"{name}{verb}指令已下发：{'、'.join(delivered)}")
        if unconfirmed:
            lines.append(f"{name}{verb}未确认：{'、'.join(unconfirmed)}")
        return lines

    # ---------- 夹爪（宇树 Dex11）：两条路径 ----------
    def probe_gripper(self, timeout: float = 6.0) -> dict:
        """探测夹爪可用性与末端连接情况，返回一个纯数据 dict（界面据此决定按钮）。

        两段探测，缺一不可：

        1. **`/arm_control/hand_posture` 的订阅者数** —— 有订阅者说明 `arm_control_node`
           在跑。这是"能不能走话题"的判据，比扫 `/proc` 的命令行特征串可靠
           （后者不校验进程归属）；
        2. 主程序在跑时**才**去读一次 `/joint_states` —— 宇树夹爪的两个关节只有
           手掌连上才会出现在这里（官方测试程序的 `wait_joints()` 注释写明了）。
           没跑主程序时跳过这段，省掉一次必然超时的等待。

        返回值里 ``running`` 与 ``blocked`` 是**两件事**，别合成一个：
        ``running`` 决定"能不能走话题"，``blocked`` 决定"能不能走直测工具"。
        主程序刚启动、话题还没起来的那几秒，两者会同时为假 —— 这时两条路都不该走
        （直测会与主程序抢串口，官方文档明确警告过），界面提示"等一会儿再检测"。

        Returns:
            dict:
                ``available``  探测本身是否跑成（False = ROS 环境没进来，不是"没在跑"）
                ``running``    话题有订阅者 → 可走 `hand_posture`
                ``blocked``    检测到主程序进程 → **不许**走直测工具
                ``launch_pid`` 主程序 pid（诊断用）
                ``subscribers``话题订阅者数（None = 没取到）
                ``hand``       Product.json 里配置的手型（空串 = 读不到）
                ``hand_type_ok``手型是宇树夹爪（读不到时不判错）
                ``sides``      ``{side: {"connected": bool|None, "rad":…, "effort":…}}``
                ``reason``     一句话结论（界面直接显示）
        """
        result = {
            "available": False,
            "running": False,
            "blocked": False,
            "launch_pid": find_launch_process(),
            "subscribers": None,
            "hand": read_product_hand(),
            "hand_type_ok": True,
            "sides": {side: {"connected": None, "rad": None, "effort": None}
                      for side in GRIPPER_JOINTS},
            "reason": "",
        }
        hand = result["hand"]
        # 只在**读到了别的确定值**时才判"手型不对"；读不到（开发机没配过机型、
        # 路径随版本变了）不当成错误，否则会把功能锁死在一个我们并不确定的前提上
        result["hand_type_ok"] = (not hand) or hand == GRIPPER_HAND_TYPE

        if not self.config.check_workspace():
            result["reason"] = f"工作空间不存在（{self.config.workspace}），无法探测"
            return result

        probe = " ; ".join([
            'if ! command -v ros2 >/dev/null 2>&1; then echo "ROS2=missing"; exit 0; fi',
            f"subs=$(timeout {int(timeout)} ros2 topic info {GRIPPER_POSTURE_TOPIC} 2>&1 "
            "| grep -oE 'Subscription count: [0-9]+' | grep -oE '[0-9]+' | head -1)",
            'echo "SUBS=${subs:-none}"',
            'if [ "${subs:-0}" -gt 0 ]; then echo "JOINTS_BEGIN"; '
            f"timeout {int(timeout)} ros2 topic echo --once /joint_states 2>&1 | head -200; "
            'echo "JOINTS_END"; fi',
        ])
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"{self._source_setup_prefix()}( {probe} )")
        logger.info(f"探测夹爪可用性: {cmd}")

        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=timeout * 2 + 6)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["reason"] = f"探测未能完成（{exc}）"
            return result

        out = (completed.stdout or "") + (completed.stderr or "")
        if "ROS2=missing" in out:
            result["reason"] = ("ROS 环境不可用（找不到 ros2）—— 请确认工作空间的 "
                               "install/setup.bash 在位")
            return result

        matched = re.search(r"^SUBS=(\S+)\s*$", out, re.MULTILINE)
        if not matched:
            result["reason"] = "探测未取到结果（ROS 命令没有输出）"
            return result
        token = matched.group(1)
        result["available"] = True
        result["blocked"] = result["launch_pid"] is not None
        if token.isdigit():
            result["subscribers"] = int(token)
            result["running"] = int(token) > 0

        if result["running"]:
            fields = parse_joint_state(out)
            if fields:
                result["sides"] = gripper_reading(fields)
            # 读到订阅者但读不出关节（echo 没出东西）：两个都不动，保持 None
            # —— "不知道"与"确认没连上"在界面上是两种待遇，不能合并

        result["reason"] = self._gripper_reason(result)
        return result

    @staticmethod
    def _gripper_reason(result: dict) -> str:
        """把探测结果说成一句话（界面直接显示，不用再拼）。"""
        if not result["hand_type_ok"]:
            return (f"当前手型是 {result['hand']}，不是宇树夹爪（{GRIPPER_HAND_TYPE}）"
                    f"—— 换手型请到机器人侧跑 ./setup_product.sh")
        if result["running"]:
            unknown = [GRIPPER_NAMES[side] for side, item in result["sides"].items()
                       if item["connected"] is None]
            if unknown:
                # 有订阅者却读不出关节：/joint_states 暂时没数据。这与"关节名不在里面"
                # 是两件事，给的操作指引也不一样，所以分开说
                return ("主程序在跑，但没读到夹爪关节 —— 可能是末端手掌还没连上，"
                        "或 /joint_states 暂时没有数据；连接手臂后点「重新检测」")
            missing = [GRIPPER_NAMES[side] for side, item in result["sides"].items()
                       if item["connected"] is False]
            if missing:
                return ("主程序在跑，但 %s 没检测到 —— 末端手掌要在「手臂连接」之后"
                        "才会连上（到遥操作里连接手臂即可）" % "、".join(missing))
            return "主程序在跑：走 ROS 话题下发（单次非实时指令）"
        if result["blocked"]:
            return ("检测到主程序进程，但 ROS 话题还没就绪（可能正在启动中）"
                    "—— 两条路都不适合走，请等几秒后重新检测")
        return "主程序没在跑：走直测工具（不起程序时只做抓取）"

    def set_gripper_posture(self, sides, close: bool, tau_raw: int,
                            speed_raw: int = GRIPPER_DEFAULT_SPEED_RAW,
                            timeout: float = 6.0) -> List[str]:
        """走话题下发**单次非实时**张/合指令（主程序在跑时的那条路）。返回可直出的日志行。

        两步，顺序不能换：

        1. 先向该侧的 `*_hand_forward_set` 发 `false` —— 关掉 FCC 实时控制、切回 JTC。
           `hand_posture` 是交给 JTC 生成轨迹执行的；若现场刚用过实时抓取测试而没切回来，
           直接发 `hand_posture` 会**安静地不生效**（那类问题最难查）；
        2. 再发 `/arm_control/hand_posture`，`position = [闭合速度, 闭合位置, 目标力矩]`
           （宇树夹爪协议，千分比 0~1000，越大越闭合）。

        机制与 `_publish_topics` 完全相同（一次性 CLI、并发、`-w 1`、每条套 `timeout`），
        只是消息类型不同 —— 上位机侧对外下发仍只有这一条通道。

        Args:
            sides: 要操作的侧别（``"left"`` / ``"right"``），可同时给两个。
            close: True = 闭合抓取，False = 张开。
            tau_raw: 目标力矩千分比（经 `gripper_tau_nm` 钳到 0.5~2.0 N·m）。
            speed_raw: 闭合速度千分比（经 `gripper_speed_mms` 换算）。
            timeout: 每条 `ros2 topic pub` 的等待上限（秒）。

        Returns:
            日志行列表；分开"已下发"与"未确认"，不混成一句"已完成"。
        """
        if not self.config.check_workspace():
            return [f"工作空间不存在（{self.config.workspace}），未下发任何指令"]
        sides = tuple(side for side in sides if side in GRIPPER_JOINTS)
        if not sides:
            return ["未指定要操作的夹爪（左/右），未下发任何指令"]

        position = gripper_position_raw(close)
        tau = gripper_tau_nm(tau_raw)
        speed = gripper_speed_mms(speed_raw)
        names = "、".join(GRIPPER_NAMES[side] for side in sides)
        verb = "闭合抓取" if close else "张开"

        switch = self._publish_topics(
            [(GRIPPER_NAMES[side], GRIPPER_FORWARD_SET_TOPICS[side], "std_msgs/msg/Bool",
              "data: false") for side in sides],
            timeout, f"切回非实时控制（{names}）")

        codes = self._publish_topics(
            [(GRIPPER_NAMES[side], GRIPPER_POSTURE_TOPIC, GRIPPER_POSTURE_MSG,
              f"{{side: {GRIPPER_SIDE_ID[side]}, id: 0, start_addr: 0, "
              f"position: [{int(speed_raw)}, {position}, {int(tau_raw)}]}}")
             for side in sides],
            # 单次指令只需送达一次，不必像下使能那样留 2 秒给硬件动作
            timeout, f"夹爪{verb}（{names}）", keep_alive=1.0)

        lines = []
        delivered = [GRIPPER_NAMES[s] for s in sides
                     if codes.get(GRIPPER_NAMES[s]) == 0]
        unconfirmed = []
        for side in sides:
            name = GRIPPER_NAMES[side]
            code = codes.get(name)
            if code == 0:
                continue
            if code is None:
                unconfirmed.append(f"{name}（未取到结果）")
            elif code == 124:
                unconfirmed.append(f"{name}（等待超时：没有订阅者，主程序可能没在跑）")
            else:
                unconfirmed.append(f"{name}（ros2 topic pub 退出码 {code}）")

        if delivered:
            lines.append(f"夹爪{verb}指令已下发：{'、'.join(delivered)}"
                         f"（位置 {position}/1000，力矩 {tau:.2f} N·m，速度 {speed:.0f} mm/s）")
        if unconfirmed:
            lines.append(f"夹爪{verb}未确认：{'、'.join(unconfirmed)}")
        # 切模式那两条只是兜底，没送到不代表本次失败 —— 但要留痕，否则现场排查时
        # 会疑惑"为什么发了三条而结果区只提两条"
        missed_switch = [GRIPPER_NAMES[s] for s in sides
                         if switch.get(GRIPPER_NAMES[s]) != 0]
        if missed_switch:
            lines.append("提示：未能确认已切回非实时模式（%s）—— 若夹爪没有动作，"
                         "多半是它还停在实时控制模式，可先跑一次机器人侧的 "
                         "dex11_fcc_grasp_test.py（它会自己切回来）"
                         % "、".join(missed_switch))
        return lines

    def run_gripper_direct(self, side: str, action: str, tau_raw: Optional[int] = None,
                           timeout: float = 45.0) -> List[str]:
        """不起主程序那条路：直接用 `dex11_direct_test` 做一次动作（抓 / 开）。

        现场要求原话：「不起程序用 grasp 抓，用 open 开」—— 所以这里只有这两个动作，
        工具另外那几个（`move` / `jump` / `auto` / `calib-force`）不往界面收。

        **前提是主程序没在跑**：官方文档明确"启动前先确保没有启动 ./run_nodes.sh
        主程序，否则会有冲突"（两边抢同一个串口）。拦这件事是**调用方**（界面，靠
        `probe_gripper()["blocked"]`）的职责 —— 这里不做进程探测，免得探测本身又引入
        一次可能误判的判断；只挡参数非法与工具缺失。

        为什么不 source 工作空间的 `setup.bash`：这个工具**不依赖 ROS 库**
        （ldd 只有 `libunitree_motor_sdk` / `libserialport`，rpath 指向 `install/lib`），
        硬 source 只是多一层依赖 —— "不起主程序"这条路径本来就该在 ROS 环境
        没进来的情况下也能用。

        Args:
            side: ``"left"`` / ``"right"``。
            action: ``"open"``（张开）/ ``"grasp"``（恒速闭合并限力）。
            tau_raw: 目标力矩千分比，仅 `grasp` 需要（换算成 N·m 后传给 `grasp=`）。
            timeout: 整个工具进程的上限（秒）。它要开串口、读标定、走完行程，
                给得宽松些；超时会杀掉子进程并如实报。

        Returns:
            日志行列表（已把工具的长输出收成结论 + 少量原文，见 `_summarize_direct_action`）。

        Raises:
            ValueError: 侧别或动作不在常量表里（编程错误，不是运行时情况）。
        """
        if side not in GRIPPER_JOINTS:
            raise ValueError(f"未知的夹爪侧别: {side}")
        if action not in GRIPPER_DIRECT_ACTIONS:
            raise ValueError(f"直测工具不支持的动作: {action}")

        tool = self.config.workspace / GRIPPER_TOOL_REL
        if not tool.is_file():
            return [f"找不到直测程序 {tool} —— 它随机器人工作空间的 install/lib/tools/ "
                    f"一起发布，请确认当前工作空间是完整部署的那一份"]

        name = GRIPPER_NAMES[side]
        verb = GRIPPER_DIRECT_ACTION_TEXT[action]
        # id：**右手 0 / 左手 1**，与 hand_posture 的 side 正好相反，别抄错
        arg = "open" if action == "open" else f"grasp={gripper_tau_nm(tau_raw):.3f}"
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"./{GRIPPER_TOOL_REL} id={GRIPPER_DIRECT_ID[side]} {arg}")
        logger.warning(f"{name}直接{verb}（不走主程序）: {cmd}")

        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # run() 超时会杀掉子进程；夹爪可能停在半途，但力矩是限住的，不会一直加力
            return [f"{name}直接{verb}超时（{timeout:.0f}s 未返回），已终止该进程 —— "
                    f"请目视确认夹爪状态，必要时断电复位后重试"]
        except OSError as exc:
            return [f"{name}直接{verb}未能执行：{exc}"]
        return _summarize_direct_action(side, action, tau_raw, completed)

    # ---------- 夹爪：零点标定（只扫描 / 扫描-标定-复扫）----------
    def scan_gripper_ports(self, timeout: float = 20.0) -> List[str]:
        """只扫描夹爪（`dex11_calib --scan`，**不做任何改动**）。

        它回答的是"夹爪到底插好了没、在哪个口"：找到几只、每只当前的关节角与错误码。
        标定前先看这一眼，比标定完发现"一只都没扫到"要省事得多。

        与直测工具同一个前提（串口独占 → 主程序不能跑），同样**不需要 ROS 环境**。
        """
        return self._run_calib_tool("--scan", None, timeout, label="扫描夹爪")

    def calibrate_gripper(self, timeout: float = 60.0) -> List[str]:
        """零点标定：**扫描 → 标定 → 再扫描**，返回可直出的日志行。

        为什么要夹着两次扫描：`dex11_calib --yes` 是就地写零点偏移，**成没成只有角度
        能说明**（标定后各爪角度应回到 0.000 rad 附近）。前后两次摆在一起，现场一眼
        能看出这次标定到底动没动，而不是只看到一句"程序退出码 0"。

        `--yes` 跳过的只是工具自己那个 `[y/N]`；**界面上仍然要二次确认** ——
        这一步会把"当前物理位置"永久写成零位，前提是两指已经手动推到完全夹紧。

        标定前那一扫如果"没有找到夹爪"，就直接返回不往下标（工具会自己再扫一遍，
        结果一样，白等）。
        """
        if not self.config.check_workspace():
            return [f"工作空间不存在（{self.config.workspace}），未执行标定"]
        tool = self.config.workspace / GRIPPER_CALIB_TOOL_REL
        if not tool.is_file():
            return [f"找不到标定程序 {tool} —— 它随机器人工作空间的 install/lib/tools/ "
                    f"一起发布，请确认当前工作空间是完整部署的那一份"]

        before = self._run_calib_tool("--scan", tool, timeout, label="标定前扫描", quiet=True)
        before_text = _calib_text(before)
        if "没有找到夹爪" in before_text:
            return _summarize_calibration(before_text, _EMPTY_RUN, "")

        done = self._run_calib_tool("--yes", tool, timeout, label="零点标定", quiet=True)
        after = self._run_calib_tool("--scan", tool, timeout, label="标定后扫描", quiet=True)
        return _summarize_calibration(before_text, done, _calib_text(after))

    def _run_calib_tool(self, flag: str, tool: Optional[Path], timeout: float, *,
                        label: str, quiet: bool = False):
        """跑一次 `dex11_calib`。

        `quiet=True` 时返回 `_CalibRun`（原始输出交给摘要函数），否则返回可直接写进界面
        的日志行 —— 扫描那条路只关心"找到没有"，标定那条路要把前后三段的输出一起比对。
        """
        empty = _EMPTY_RUN

        if tool is None:
            tool = self.config.workspace / GRIPPER_CALIB_TOOL_REL
        if not self.config.check_workspace():
            return empty if quiet else [f"工作空间不存在（{self.config.workspace}），跳过{label}"]
        if not tool.is_file():
            missing = [f"找不到标定程序 {tool} —— 它随机器人工作空间的 install/lib/tools/ "
                       f"一起发布，请确认当前工作空间是完整部署的那一份"]
            return empty if quiet else missing

        # 同样不 source setup.bash：标定程序 ldd 里没有任何 ROS 库
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"./{GRIPPER_CALIB_TOOL_REL} {flag}")
        logger.warning(f"{label}: {cmd}")
        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            text = f"⚠ {label}超时（{timeout:.0f}s 未返回），已终止该进程 —— 请目视确认夹爪状态"
            return _CalibRun(-1, text, "") if quiet else [text]
        except OSError as exc:
            text = f"⚠ {label}未能执行：{exc}"
            return _CalibRun(-1, text, "") if quiet else [text]

        if quiet:
            return completed
        lines = [f"{label}："] + _summarize_scan(
            (completed.stdout or "") + (completed.stderr or ""))
        return lines

    # ---------- 夹爪：FCC 实时抓取（可设夹持时长）----------
    def run_gripper_fcc(self, side: str, tau_raw: int, speed_raw: int, hold_sec: float,
                        log_file: Optional[Path] = None,
                        timeout: Optional[float] = None) -> List[str]:
        """走**实时（FCC）模式**抓一次：S 曲线合拢 → 夹持 `hold_sec` 秒 → 松开 → 退出实时模式。

        与 `set_gripper_posture`（单次非实时、JTC 生成轨迹）的分工：那条是"抓一下就走"，
        不需要主程序额外配合；这条是**实时力控抓取**，由上位机按 50Hz 持续喂指令，
        好处是可以指定夹持时长、并按实际力矩反馈判断有没有夹住 —— 现场说的
        "开启主控的 FCC 抓取模式，可以设置抓取多少时间"就是它。

        实现放在 `scripts/dex11_fcc_grasp.py`（随上位机交付包发布），而不是用机器人侧的
        `dex11_fcc_grasp_test.py`：官方那个**固定驱动左右两侧**，而这里要按侧操作。

        三条安全约定：
        1. **输出同时落盘**到 `log_file`：界面在动作进行中 tail 它显示进度 ——
           一次夹持可能几十秒，没有进度看起来就像卡住了；
        2. **超时不硬杀**：先给整个进程组发 SIGINT，脚本的 `finally` 会松开并退出实时模式；
           只有它连收尾都不动时才 SIGKILL，并在结果里明说"夹爪可能停在半合位置"；
        3. 脚本自己也会在异常路径里松开 + 退出实时模式（实时模式下没人继续喂指令，
           控制器会保持最后一条指令 = 夹爪冻在半合位置还在使劲）。

        Args:
            side: ``"left"`` / ``"right"``。
            tau_raw: 目标力矩千分比。
            speed_raw: 合拢速度千分比。
            hold_sec: 夹持保持时长（秒），会钳到 `GRIPPER_HOLD_SEC_MIN/MAX`。
            log_file: 输出落盘位置（None 表示不落盘）。
            timeout: 整个过程的上限；None 时按 `gripper_fcc_seconds()` 自动放宽。

        Returns:
            日志行列表（脚本输出原文，见 `_summarize_fcc`）。

        Raises:
            ValueError: 侧别不在常量表里。
            FileNotFoundError: FCC 脚本不在位（它随上位机交付包发布）。
        """
        if side not in GRIPPER_JOINTS:
            raise ValueError(f"未知的夹爪侧别: {side}")
        if not self.config.check_workspace():
            return [f"工作空间不存在（{self.config.workspace}），未下发任何指令"]

        script = self._script_for("gripper_fcc_script", "FCC 抓取脚本",
                                  GRIPPER_FCC_SCRIPT_NAME)
        hold_sec = max(GRIPPER_HOLD_SEC_MIN, min(GRIPPER_HOLD_SEC_MAX, float(hold_sec)))
        budget = timeout or (gripper_fcc_seconds(speed_raw, hold_sec) + 45.0)
        args = (f"--side {side} --tau {int(tau_raw)} --speed {int(speed_raw)} "
                f"--hold {hold_sec:.1f}")
        # 这个脚本要 rclpy（不像直测/标定那两个二进制），所以必须 source 工作空间
        cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self._env_prefix()}"
               f"{self._source_setup_prefix()}python3 {shlex.quote(str(script))} {args}")
        logger.warning(f"{GRIPPER_NAMES[side]} FCC 抓取（夹持 {hold_sec:.1f}s）: {cmd}")

        handle = open(log_file, "w", encoding="utf-8") if log_file else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(["bash", "-c", cmd], stdout=handle,
                                    stderr=subprocess.STDOUT, text=True,
                                    start_new_session=True)
            try:
                returncode = proc.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                killed = self._stop_fcc_process(proc)
                note = ("⚠ FCC 抓取超时，已中断 —— "
                        + ("进程组收到 SIGINT，脚本应已松开并退出实时模式"
                           if not killed else
                           "脚本没能收尾，夹爪可能停在半合位置，请断电复位"))
                text = log_file.read_text(encoding="utf-8") if log_file else ""
                return _summarize_fcc(side, text, -1) + [note]
        except OSError as exc:
            return [f"{GRIPPER_NAMES[side]} FCC 抓取未能执行：{exc}"]
        finally:
            if handle is not subprocess.DEVNULL:
                handle.close()

        text = log_file.read_text(encoding="utf-8") if log_file else ""
        return _summarize_fcc(side, text, returncode)

    @staticmethod
    def _stop_fcc_process(proc: subprocess.Popen, grace: float = 8.0) -> bool:
        """中断 FCC 脚本：先给**整个进程组** SIGINT（让脚本的 finally 收尾），再兜底 SIGKILL。

        为什么是进程组：命令是 `bash -c "… && python3 …"`，只发给 bash 不保证传到 python
        （脚本收不到 SIGINT 就不会走 finally，夹爪会留在实时模式+半合位置）。

        Returns:
            True 表示最后用了 SIGKILL（脚本没能收尾）。
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=grace)
            return False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("FCC 抓取进程没能杀掉")
            return True

    # ---------- 末端参数（arm_control_config.yaml）----------
    def read_end_effector_config(self, product_hand: Optional[str] = None) -> dict:
        """读末端参数文件并解析成界面要的那几个数（纯读，不写任何东西）。

        为什么不用后台线程：这是**本地文件读**（毫秒级），不像 `probe_gripper()` 那样要跑
        几秒的 ROS 命令。放进线程只会让"打开页面"多一层时序。

        Args:
            product_hand: Product.json 的手型；不传则现读一次（用例里可注入）。

        Returns:
            dict:
                ``available``      读到且解析成功（False = 不给编辑，原因在 `reason`）
                ``path``           文件路径（界面显示出来，现场好自己去看）
                ``reason``         一句话结论
                ``types``          ``{side: 段名|None}``
                ``depth_control``  ``{side: True/False/None}``（只读展示，本期不改）
                ``values``         ``{side: {"open": [...]|None, "fist": [...]|None}}``
                                   —— 按**文件里当前那一种**手型取
                ``sections``       ``{段名: {side: {"open":…, "fist":…}}}``
                                   —— 四种手型段全都读出来了，界面在下拉里换类型时用
                ``product_hand``   Product.json 的 hand（空串 = 读不到）
                ``product_section``它对应的段名（空串 = 认不出来）
        """
        path = self.config.arm_control_config_path
        # 手型走 `config.get_product_hand()` —— 与顶栏读的是**同一个文件**。
        # 不用那个写死的 /var/nerv 路径：会出现"顶栏写着宇树夹爪、这一页却说没读到"。
        hand = self.config.get_product_hand() if product_hand is None else product_hand
        result = {
            "available": False,
            "path": str(path),
            "reason": "",
            "types": {"left": None, "right": None},
            "depth_control": {"left": None, "right": None},
            "values": {side: {"open": None, "fist": None} for side in ("left", "right")},
            "sections": {},
            "product_hand": hand,
            "product_section": product_section(hand),
        }
        if not path.is_file():
            result["reason"] = f"配置文件不存在：{path}"
            return result
        try:
            payload = load_yaml(path)
        except Exception as exc:      # noqa: BLE001 (YAML 坏 / 权限 / 编码… 一律当"读不出来")
            result["reason"] = f"配置文件解析失败：{exc}"
            return result

        parsed = parse_arm_control(payload)
        if not parsed["params"]:
            result["reason"] = "文件里没有 ArmControlNode.ros__parameters 段（结构不对？）"
            return result
        result.update({key: parsed[key] for key in ("types", "depth_control",
                                                    "values", "sections")})
        result["available"] = True
        result["reason"] = "已读取"
        return result

    def save_end_effector_config(self, selections: dict, values: dict,
                                 backup_dir: Optional[Path] = None) -> dict:
        """写回末端参数：**备份 → 按行精准改 → 回读校验**。

        三步都必要，缺哪一步都有具体后果：

        1. **备份**（默认 `<上位机 logs>/config_backup/arm_control_config_<时间戳>.yaml`）：
           写的是机器人侧配置文件，改错了要能一键找回。备份放上位机日志目录、**不放到
           `install/config/` 里** —— 那个目录是交付物，塞 `.bak` 只会让现场分不清哪份在用。
           备份失败**不拦写入**，但如实报出来（否则现场会以为有备份）。
        2. **按行改**（`patch_arm_control_text`）：只动目标行，注释与其它手型段逐字保留。
        3. **回读校验**：写文件没有退出码可看，**唯一可信的判据是重新解析后逐值相等**。

        Args:
            selections: ``{"left": 段名, "right": 段名}``。段名为 `None` 的一侧不改类型。
            values: ``{"left": {"open": [...], "fist": [...]}, ...}``。某一侧整体给 `None`
                表示"只改类型、不碰它的指令值"（本期不支持的手型就是这么处理的）。
                某一组给 `None` 则跳过那一组。
            backup_dir: 备份目录；不传用 `config.get_local_log_dir() / "config_backup"`。

        Returns:
            dict: ``{"ok": bool, "lines": [人话日志行], "errors": [错误行],
            "backup": 备份路径|None}``。`ok=False` 时**没有写盘**（或写了但回读不符，
            此时 lines 里会点明是哪个值）。
        """
        path = self.config.arm_control_config_path
        lines: List[str] = []
        errors: List[str] = []

        if not path.is_file():
            return {"ok": False, "lines": lines,
                    "errors": [f"配置文件不存在，未写入任何内容：{path}"], "backup": None}

        selections = selections or {}
        values = values or {}

        # ---- 1) 先校验，任何一项不合格就整笔不写（避免"改了一半"）----
        # 内部结构统一成 {段名: {(侧, 指令): 值}}：后面核对回读结果时按它逐项比，
        # 不必再从 "left_open_cmd" 这样的键名里反推是哪一侧、哪一组。
        wanted: dict = {}
        types: dict = {}
        for side in ("left", "right"):
            section = selections.get(side)
            if not section:
                continue
            if section not in END_EFFECTOR_EDITABLE:
                errors.append(f"{side} 的末端类型 {section} 本期不支持编辑，未写入任何内容")
                continue
            types[side] = section
            side_values = values.get(side)
            if side_values is None:
                continue                       # 只改类型，不动指令值
            for cmd in END_EFFECTOR_CMDS:
                cmd_values = side_values.get(cmd)
                if cmd_values is None:
                    continue
                problems = validate_end_effector_values(section, cmd_values)
                if problems:
                    errors.extend(f"{side} {END_EFFECTOR_CMD_TEXT[cmd]}：{item}"
                                  for item in problems)
                else:
                    wanted.setdefault(section, {})[(side, cmd)] = [int(item)
                                                                   for item in cmd_values]

        if errors:
            errors.insert(0, "参数校验未通过，未写入任何内容：")
            return {"ok": False, "lines": lines, "errors": errors, "backup": None}
        if not wanted and not types:
            return {"ok": False, "lines": lines,
                    "errors": ["没有要写入的内容（类型与指令值都没填）"], "backup": None}

        # ---- 2) 备份 ----
        original = path.read_text(encoding="utf-8")
        backup: Optional[Path] = None
        directory = Path(backup_dir) if backup_dir else self.config.get_local_log_dir() / "config_backup"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = directory / f"arm_control_config_{stamp}.yaml"
            backup.write_text(original, encoding="utf-8")
            lines.append(f"已备份原文件 → {backup}")
        except OSError as exc:
            backup = None
            lines.append(f"注意：备份没写成（{exc}）—— 继续写入，但没有可回滚的副本")

        # ---- 3) 按行改 ----
        patch_sections = {section: {f"{side}_{cmd}_cmd": item
                                    for (side, cmd), item in block.items()}
                          for section, block in wanted.items()}
        patched = patch_arm_control_text(original, patch_sections, types)
        if patched["missing"]:
            return {"ok": False, "lines": lines, "backup": backup,
                    "errors": ["文件里没找到这些键，未写入任何内容（结构变了？）："
                               + "、".join(patched["missing"])]}

        # 先写临时文件再 `os.replace`：中途断电/被 SIGKILL 也不会留下半截配置
        temp = path.with_name(path.name + f".tmp{os.getpid()}")
        try:
            temp.write_text(patched["text"], encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            return {"ok": False, "lines": lines, "backup": backup,
                    "errors": [f"写入失败，配置未改动：{exc}"]}
        lines.append(f"已写入：{'、'.join(patched['applied'])}")

        # ---- 4) 回读校验 ----
        verified = self.read_end_effector_config()
        if not verified["available"]:
            return {"ok": False, "lines": lines, "backup": backup,
                    "errors": [f"写入后回读失败：{verified['reason']}"
                               f"（原文件已备份，可退回）"]}

        mismatched = []
        for section, block in wanted.items():
            for (side, cmd), item in sorted(block.items()):
                if verified["types"].get(side) != section:
                    mismatched.append(f"{side} 末端类型（回读为 "
                                      f"{verified['types'].get(side) or '未知'}）")
                elif verified["values"][side][cmd] != item:
                    mismatched.append(f"{section}.{side}_{cmd}_cmd"
                                      f"（回读为 {verified['values'][side][cmd]}）")
        for side, section in sorted(types.items()):
            if verified["types"].get(side) != section:
                mismatched.append(f"{side}_end_effector_type（回读为 "
                                  f"{verified['types'].get(side) or '未知'}）")
        mismatched = list(dict.fromkeys(mismatched))     # 同一侧的类型会报两次，去个重
        if mismatched:
            return {"ok": False, "lines": lines, "backup": backup,
                    "errors": ["写入后回读不一致：" + "、".join(mismatched)
                               + "（原文件已备份，可退回）"]}

        groups = sum(len(block) for block in wanted.values())
        if groups:
            lines.append(f"回读校验通过（{groups} 组指令值逐个一致）")
        if types:
            lines.append("末端类型已改为：" + "、".join(
                f"{side} {END_EFFECTOR_NAMES.get(section, section)}"
                for side, section in sorted(types.items())))
        lines.append("提示：末端参数在 arm_control_node 启动时读取，需重启主程序后生效")
        return {"ok": True, "lines": lines, "errors": [], "backup": str(backup) if backup else None}

    def stop_main_program(self) -> List[str]:
        """关闭主程序：停注册进程 → 清残留节点 → 核验话题数。返回可直出的日志行。

        为什么不是一句 `terminate_by_name("run_nodes")` 了事：主程序跑在**新终端**里，
        我们持有的 Popen 是终端进程，`ros2 launch | python3` 那条管道拉起的节点
        收不到信号。实测关完之后 `/arm_control`、`/trunk_control`、`/navi_*`、
        `/ascamera` 一整套话题还在。

        全程十几秒（信号阶梯的等待 + 话题核验），必须由调用方放到后台线程里跑。

        **第一步是下使能，不是停进程**（v4.21.0，现场 P0）：控制节点被杀掉时，
        硬件那边的使能状态不会自动复位 —— 躯干上着使能就直接关程序，驱动器会留在
        使能/实时状态里，随后电机不受控。所以先向躯干/手臂/底盘发 `connect=false`，
        等这三条命令真的发出去（每条都等发现、回来才继续），再动进程。
        """
        lines: List[str] = []
        # 顺序不能反：先下使能，再收进程
        try:
            lines.extend(self.release_hardware_connections())
        except Exception as exc:                      # noqa: BLE001 —— 收尾动作不该中断清理
            lines.append(f"下发断开连接指令失败（继续清理）：{exc}")

        try:
            self.pm.terminate_by_name("run_nodes")
            lines.append("已向主程序发送关闭信号")
        except Exception as exc:                      # noqa: BLE001 —— 收尾动作不该中断清理
            lines.append(f"停止主程序进程失败：{exc}")

        cleaner = MainProgramCleaner(self.config)
        report = cleaner.stop()
        if cleaner.workspaces:
            lines.append("识别到的工作空间：" + "、".join(cleaner.workspaces))
        if report["found"]:
            lines.append(f"发现残留节点进程 {len(report['found'])} 个，按信号阶梯清理：")
            for cmd in report["found"][:8]:
                lines.append(f"    {_shorten_command(cmd)}")
            lines.extend(f"  {step}" for step in report["steps"])
            if report["survivors"]:
                lines.append(f"仍有 {len(report['survivors'])} 个进程未退出（可能需 sudo）：")
                for cmd in report["survivors"][:8]:
                    lines.append(f"    {_shorten_command(cmd)}")
            else:
                lines.append("残留节点已全部退出")
        else:
            lines.append("没有发现残留节点进程"
                         + ("（若话题仍在，说明它们不由本机这份工作空间启动）"
                            if cleaner.workspaces else ""))

        count, sample, mode = self.count_ros_topics()
        if count < 0:
            lines.append("未能核验话题列表（ROS 环境不可用或超时），请手动 ros2 topic list 复查")
        elif count == 0:
            lines.append("核验：ros2 topic list 已无话题")
        else:
            lines.append(f"核验（{mode}）：仍需关注的话题 {count} 个：")
            lines.extend(f"    {topic}" for topic in sample)
            if report["survivors"] or count > len(sample):
                lines.append("若仍有多余话题，可手动补一刀：")
                lines.append(f"    pkill -INT -f '{cleaner.workspaces[0]}/install/'"
                             if cleaner.workspaces else "    pkill -INT -f ros2")
                lines.append("    ros2 daemon stop   # 让话题表立即刷新，避免看到缓存")
        return lines

    def count_ros_topics(self, timeout: float = 12.0) -> Tuple[int, List[str], str]:
        """跑一次 `ros2 topic list`：返回（话题数, 前若干条, 用的哪种方式）。

        只用来回答"关干净了没有"，不解析话题语义。失败返回 (-1, [], "未核验")，
        表示"没核验成"——不是"没有残留"，两者不能混。

        **优先 `--no-daemon`**：ros2 CLI 默认走常驻 daemon，而 daemon 的话题表是
        discovery 缓存 —— 节点刚被杀掉时它可能还留着旧条目，看起来像"清理没生效"。
        不走 daemon 拿到的是当下真实发现结果。老版本不认识这个参数，所以留了回退。
        """
        if not self.config.check_workspace() or not self._setup_script().exists():
            return -1, [], "未核验"

        prefix = (f"cd {shlex.quote(str(self.config.workspace))} && "
                  f"{self._env_prefix()}{self._source_setup_prefix()}")
        attempts = (("--no-daemon", "绕过 daemon 直查"), ("", "经 daemon 查询"))
        for flag, mode in attempts:
            cmd = f"{prefix}timeout {int(timeout)} ros2 topic list {flag} 2>/dev/null".strip()
            try:
                completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                           text=True, timeout=timeout + 6)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning(f"核验残留话题失败（{mode}）：{exc}")
                continue
            topics = [line.strip() for line in (completed.stdout or "").splitlines()
                      if line.strip().startswith("/")]
            if topics or completed.returncode == 0:
                return len(topics), topics[:20], mode
        return -1, [], "未核验"

    def stop_aux_nodes(self) -> List[str]:
        """退出兜底：把本会话由上位机拉起的辅助节点收干净。返回可直出的日志行。

        判定只用 ProcessManager 里**本会话注册过**的进程，不做 /proc 全局匹配：
        机器人主程序里也可能有同名包（例如某个 calibration 节点），全局匹配会误杀。
        代价是"上一次界面崩溃前留下的节点"清不到 —— 那种情况用「关闭程序」的
        残留清理覆盖。宁可少清，不可错杀。
        """
        lines: List[str] = []
        for pattern in AUX_NODE_NAME_PATTERNS:
            proc = self.pm.find_by_name(pattern)
            if proc is None:
                continue
            try:
                self.pm.terminate_by_name(pattern)
            except Exception as exc:                  # noqa: BLE001 —— 退出路径不许抛
                lines.append(f"停止 {pattern} 失败：{exc}")
            else:
                lines.append(f"已停止 {pattern}（退出前清理，避免机器人上留残留节点）")
        return lines

    def launch_setup_product(self) -> subprocess.Popen:
        """产品配置向导：工作空间根目录的 setup_product.sh（交互式，新终端运行）。

        该脚本必须从工作空间运行——它内部要读 install/share/product/{products,hands,
        wrists} 目录，且需要 sudo 建 /var/nerv。所以真源在工作空间根目录，
        上位机不放镜像。
        """
        script = self.config.workspace / "setup_product.sh"
        if not script.exists():
            raise FileNotFoundError(f"产品配置脚本不存在: {script}")
        # env="none"：该脚本不依赖上位机注入的路径变量
        return self._run_script(
            script, "产品配置", mode="terminal", env="none",
            hold=True, proc_name="setup_product")

    def launch_teleop(self, log_file: Optional[Path] = None) -> subprocess.Popen:
        """后台启动遥操作。

        log_file 不为空时，脚本原本输出到终端的内容会写入该文件（界面读取后回显到日志区）；
        文件不可写时自动退化为丢弃输出，不影响遥操作启动。
        """
        script = self._script_for("run_teleop_script", "遥操作脚本", "run_teleop.sh")
        return self._run_script(
            script, "遥操作", mode="background", need_setup=True,
            log_file=log_file, proc_name="run_teleop")

    #: 录制脚本里那行落盘目录的**合法写法**：必须允许被环境变量覆盖。
    #: 只认这一种形状 —— 其余（尤其 `BAG_DIR="$NERV_DIR/bag"` 这种硬编码赋值）
    #: 会让上位机导出的 `BAG_DIR` 被无声吃掉，见 `_require_bag_dir_override`。
    _BAG_DIR_OVERRIDABLE = (
        re.compile(r'^\s*BAG_DIR="\$\{BAG_DIR:-[^"]*\}"\s*$', re.M),
    )

    def _require_bag_dir_override(self, script: Path) -> None:
        """确认录制脚本认 `BAG_DIR` 环境变量；不认就**拒绝起录**。

        为什么宁可拒绝也不继续：不认的后果是数据写到**另一个**目录，而界面、录制
        清单、打点、可用空间预检全都按"写在盘上"理解 —— 一次采集几百 GB 落到
        `/var/nerv/bag`，现场按界面提示拔盘交付时才发现盘是空的。
        **静默写错位置比当场报错严重得多**，所以这里选报错。

        （机器侧那 3 个录制脚本已经改成可覆盖式；但那些文件**不在版本控制里**，
        一次重刷就可能退回去，所以每次真正改落盘位置时都现查一遍。）
        """
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(f"读取录制脚本失败，跳过 BAG_DIR 覆盖检查：{exc}")
            return                  # 读不到不在这里拦，交给产出巡检兜底
        if any(pat.search(text) for pat in self._BAG_DIR_OVERRIDABLE):
            return
        raise AppError(
            ErrorCode.SCRIPT_FAILED,
            f"录制脚本不认 BAG_DIR 覆盖：{script}\n"
            f"这一份把落盘目录写死了，改成别的位置会被忽略（数据仍落在脚本内置的目录）。\n"
            f"改法：把脚本里那一行换成 BAG_DIR=\"${{BAG_DIR:-$NERV_DIR/bag}}\" 后再试。"
            f"本次未起录。",
        )

    def launch_record(self, script_type: str, name: str = "", desc: str = "",
                      label: str = "", action_id: Optional[int] = None) -> subprocess.Popen:
        """启动录制。

        真机脚本（record_*.sh）不解析 label=/action_id= 参数，所以动作标签直接并进
        desc —— 真机脚本会把 desc 写进 info.json，这样标签信息不丢、也不改脚本。
        """
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        key = f"record_{script_type}_script"
        script = self._script_for(key, f"录制脚本({script_type})")

        # 动作标签并进 desc：label 为主任务名，action_id 是可选 SDK 编号
        tag = str(label or "").strip()
        if action_id is not None and str(action_id) != "":
            tag = f"{tag} #{action_id}".strip() if tag else f"#{action_id}"
        desc_parts = [p for p in (str(desc or "").strip(), tag) if p]

        args = []
        if name:
            args.append(shlex.quote(f"name={name}"))
        if desc_parts:
            args.append(shlex.quote(f"desc={' '.join(desc_parts)}"))

        # 录制在 bag_dir 下执行（脚本按相对路径落 bag），故 cwd 与 workspace 不同。
        # `bag_dir` 取的是**有效**落盘目录：本次会话选了移动硬盘时它就是盘上那个目录
        # （`ConfigManager.get_bag_dir` 的会话级覆盖），录制脚本的 `BAG_DIR` 走同一个值。
        bag_dir = self.config.get_bag_dir()
        # 本次真的把落盘位置**改**了才查这一条（正常路径上不产生任何额外开销）：
        # 机器侧的录制脚本必须认 `BAG_DIR` 环境变量（`BAG_DIR="${BAG_DIR:-…}"`）。
        # 若它写回硬编码赋值，环境变量会被**无声吃掉** —— 数据落回 /var/nerv/bag，
        # 而界面以为写到了盘上；等到现场拔盘去读，盘上什么也没有。
        # 机器侧那些脚本**不在版本控制里**（工作空间不是 git 仓库），一次重刷就可能
        # 把它退回去，所以在起录这一点上现查一次，比在文档里写一句"请勿回退"可靠。
        if bag_dir != self.config.get_configured_bag_dir():
            self._require_bag_dir_override(script)
        # 建目录这一步**不吞失败**（原先写成 `|| true`）：目录建不出来时 `cd` 也进不去，
        # 吞掉只会把"盘没插好/不可写"退化成后面一句看不懂的"脚本启动后立即退出"。
        # 真正的用户可读原因由界面层的起录前预检给出（见 `_apply_record_target`）。
        return self._run_script(
            script, f"录制({script_type})", args, mode="background", cwd=bag_dir,
            prelude=f"mkdir -p {shlex.quote(str(bag_dir))} && ",
            proc_name=f"record_{script_type}")

    def launch_play_bag(self, bag_path: str = "", with_rviz: bool = False) -> subprocess.Popen:
        """回放录制的 bag（新终端，交互式选择/播放）。"""
        script = self._script_for("play_bag_script", "回放脚本", "play_bag.sh")
        args = []
        if with_rviz:
            args.append("rviz")
        if bag_path:
            args.append(shlex.quote(f"bag={bag_path}"))
        return self._run_script(
            script, "回放", args, mode="terminal", proc_name="play_bag")

    def launch_bag_rate_stat(self, bag_path: str) -> subprocess.Popen:
        """对指定 bag 做完整话题频率统计（新终端，避免阻塞 UI）。

        bag_rate_stat.py 由 python3 直接执行，需要先 source 工作空间的 setup.bash
        才能 import rclpy / rosbag2_py / sensor_msgs。
        """
        if not bag_path:
            raise ValueError("bag 路径不能为空")
        script = self._script_for("bag_rate_stat_script", "频率统计脚本", "bag_rate_stat.py")
        return self._run_script(
            script, "采集统计", [shlex.quote(str(bag_path))], mode="terminal",
            env="ros", runner="python3", hold=True, proc_name="bag_rate_stat")

    def launch_monitor_performance(self, output_dir: str = "") -> subprocess.Popen:
        """录制性能监控（新终端，等待 ros2 bag record 启动后采样）。"""
        script = self._script_for("monitor_performance_script", "性能监控脚本",
                                  "monitor_record_performance.sh")
        args = [shlex.quote(str(output_dir))] if output_dir else []
        return self._run_script(
            script, "性能监控", args, mode="terminal", proc_name="monitor_performance")

    # ---------- 机器人状态监控（robot_monitor.py） ----------
    def launch_robot_monitor(self, log_file: Optional[Path] = None) -> subprocess.Popen:
        """后台启动状态监控：订阅电量/急停/定位/地图等话题并落盘 JSON。

        与 bag_rate_stat 一样需要 ROS 环境，因此先 source 工作空间的 setup.bash。
        """
        script = self._script_for("robot_monitor_script", "状态监控脚本",
                                  "robot_monitor.py")
        return self._run_script(
            script, "机器人状态监控", mode="background", env="ros", runner="python3",
            need_setup=True, log_file=log_file, proc_name="robot_monitor")

    def stop_robot_monitor(self) -> bool:
        """停止状态监控。返回是否确有待停进程。"""
        if self.pm.find_by_name("robot_monitor") is None:
            return False
        self.pm.terminate_by_name("robot_monitor")
        return True

    # ---------- 急停 ----------
    def launch_emergency_stop(self, engage: bool = True) -> subprocess.Popen:
        """下发急停/解除急停：底盘 + 躯干（见 EMERGENCY_STOP_TARGETS）。

        三个刻意的选择：

        1. 用独立的一次性 `ros2 topic pub`，不做常驻进程转发 ——
           急停这条链路不能依赖任何后台进程是否存活。
        2. **并发**下发而不是串行：`--keep-alive 2` 每条要占住约 2 秒，
           串行就是 4 秒，对急停来说太慢。
        3. 把各部件的结果**汇总成整条命令的退出码**（任一失败即非 0）：
           躯干没有状态回读（trunk_state 只报关节角），界面只能靠它判断
           "到底发出去了没有"。返回的 Popen 被 UI 等待并如实汇报。

        `--once --keep-alive 2` 与 run_teleop.sh 里既有的用法保持一致。
        """
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")

        flag = "true" if engage else "false"
        launches, waits = [], []
        for index, (_name, topic) in enumerate(EMERGENCY_STOP_TARGETS, start=1):
            launches.append(
                f'ros2 topic pub --once --keep-alive 2 {topic} '
                f'std_msgs/msg/Bool "data: {flag}" & pid{index}=$!'
            )
            waits.append(f"wait $pid{index} || rc=1")

        # 子 shell 包住整段：`&&` 与 `&` 的优先级会打架（A && B & C 会被解析成
        # (A && B) & C），必须显式分组；exit 的码也正好成为整条命令的退出码。
        body = "; ".join(launches) + "; rc=0; " + "; ".join(waits) + "; exit $rc"
        cmd = (
            f"cd {shlex.quote(str(self.config.workspace))} && "
            f"{self._env_prefix()}"
            f"{self._source_setup_prefix()}"
            f"( {body} )"
        )
        logger.warning(f"{'下发急停' if engage else '解除急停'}（{EMERGENCY_STOP_PART_NAMES}）: {cmd}")
        return self._start_background(cmd, "急停" if engage else "解除急停",
                                      proc_name="emergency_stop")

    # ---------- 相机标定 / 戳点（机器人侧 ROS 节点） ----------
    # 这些节点由机器人侧提供，上位机只负责"拉起"和"下发一条服务指令"：
    # 采集流程、按键交互、标定算法全在节点里。上位机不做流程编排，
    # 免得两边各维护一套"标定进行到哪一步"的状态。
    def vision_log_path(self, node: str) -> Path:
        """节点的终端输出落盘位置（供界面 tail 回显、也便于离线翻看）。"""
        return self.config.get_log_dir() / f"{node}.log"

    def _ros_run_cmd(self, package: str, node: str, args=()) -> str:
        """组装 `ros2 run` 命令：cd 工作空间 + 环境变量 + source setup.bash。"""
        args_str = (" " + " ".join(args)) if args else ""
        return (
            f"cd {shlex.quote(str(self.config.workspace))} && "
            f"{self._env_prefix()}"
            f"{self._source_setup_prefix()}"
            f"ros2 run {shlex.quote(package)} {shlex.quote(node)}{args_str}"
        )

    def _launch_ros_node(self, package: str, node: str, label: str, *,
                         mode: str = "terminal", args=(), hold: bool = True,
                         log_file: Optional[Path] = None,
                         proc_name: str = "") -> subprocess.Popen:
        """启动一个 ROS 节点（统一校验 → 组装 → 分发）。

        `mode="background"` 且给了 `log_file` 时输出到日志文件：这样界面能 tail
        回显到日志区，而常驻节点也不会因为管道没人读而卡死（见
        `run_background_with_log` 的说明）。
        """
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        setup_script = self._setup_script()
        if not setup_script.exists():
            raise FileNotFoundError(f"setup.bash 不存在: {setup_script}")
        if not package or not node:
            raise ValueError(f"{label}未配置包名/节点名（检查 app_config.yaml）")

        cmd = self._ros_run_cmd(package, node, args)
        if mode == "terminal":
            # 保活尾巴只在终端里才有意义（后台进程没人按 Enter）
            if hold:
                cmd += (f" ; echo \"=== {node} 已退出，退出码: $? ===\" ; "
                        f"read -p '按 Enter 键关闭窗口...'")
            return self._run_in_new_terminal(label, node, cmd, proc_name or node)
        return self._start_background(cmd, label, log_file=log_file,
                                      proc_name=proc_name or node)

    def _start_video_stream_if_enabled(self, enabled: Optional[bool]) -> Optional[subprocess.Popen]:
        """按配置拉起视频流节点；返回 None 表示未启用（不是失败）。"""
        if enabled is None:
            enabled = bool(self.config.app_config.get("start_video_stream", True))
        if not enabled:
            logger.info("按配置跳过视频流节点（start_video_stream: false）")
            return None
        package = self.config.ros_node("video_streamer_package")
        node = self.config.ros_node("video_streamer_node")
        return self._launch_ros_node(package, node, "视频流", mode="background",
                                     log_file=self.vision_log_path(node))

    def launch_camera_calibration(self, with_video: Optional[bool] = None) -> subprocess.Popen:
        """启动三相机标定节点（后台，输出落盘后由界面 tail 回显）。

        为什么不是新终端：节点的输出（采集进度、每张相机的重投影误差、报错）
        是判断标定有没有成功的唯一依据，开在独立终端里界面看不到，
        现场只能凭记忆转述。落盘 + tail 才能进日志区。
        交互不受影响 —— 节点的键盘/鼠标输入来自它自己的 OpenCV 窗口
        （实测 `pokepoint_node` 用 `waitKey`），不依赖终端 stdin。

        注意与遥操作的 VR tracker 标定区分：那个走 `run_calibration` + vr_bridge，
        这个走机器人侧 `calibration` 包的节点，两者毫无关系。
        """
        package = self.config.ros_node("camera_calib_package")
        node = self.config.ros_node("camera_calib_node")
        proc = self._launch_ros_node(package, node, "相机标定", mode="background",
                                     log_file=self.vision_log_path(node))
        try:
            self._start_video_stream_if_enabled(with_video)
        except Exception as exc:
            # 视频流只是看画面用，拉不起来不该挡住标定本身
            logger.warning(f"视频流节点启动失败（标定节点已启动，可继续）: {exc}")
        return proc

    def launch_pokepoint(self, with_video: Optional[bool] = None) -> subprocess.Popen:
        """启动戳点节点（后台，输出落盘后由界面 tail 回显）。"""
        package = self.config.ros_node("pokepoint_package")
        node = self.config.ros_node("pokepoint_node")
        proc = self._launch_ros_node(package, node, "戳点", mode="background",
                                     log_file=self.vision_log_path(node))
        try:
            self._start_video_stream_if_enabled(with_video)
        except Exception as exc:
            logger.warning(f"视频流节点启动失败（戳点节点已启动，可继续）: {exc}")
        return proc

    def wait_camera_calibration_ready(self, timeout: Optional[float] = None,
                                      interval: Optional[float] = None) -> dict:
        """等标定节点把自己的服务注册出来 —— 这是"它真的能接指令"的判据。

        **为什么不用"进程还在不在"**：`ros2 service call` 要的是 ROS 图里那个服务。
        节点进程起来了、但 DDS 发现还没完 / 还没走到 `create_service` 那一步时，调用只会
        拿到"服务不存在"。这与夹爪那边"看话题订阅者数、不看 `/proc` 命令行"是同一条判据
        （见 `MEMORY.md` 的三条不能弄错的判据）。

        一次 bash 内部轮询（不是每轮起一个进程）：**用 `$SECONDS` 对着 deadline 循环**，
        而不是"轮数 × 间隔" —— 每轮 `ros2 service list` 自己就要 1~2 秒，按轮数算会把
        实际等待放大两倍。

        Args:
            timeout: 等待上限（秒），不传用 `CAMERA_CALIB_READY_TIMEOUT`。
            interval: 轮询间隔（秒），不传用 `CAMERA_CALIB_READY_INTERVAL`。

        Returns:
            dict: ``{"ready": bool, "elapsed": float, "service": str, "text": str}``。
            `text` 是给人看的一句话（就绪说用了多久，没就绪说等了多少秒、还差什么）。
        """
        service = self.config.ros_node("camera_calib_service")
        budget = CAMERA_CALIB_READY_TIMEOUT if timeout is None else float(timeout)
        step = CAMERA_CALIB_READY_INTERVAL if interval is None else max(0.5, float(interval))
        if not self.config.check_workspace():
            return {"ready": False, "elapsed": 0.0, "service": service,
                    "text": f"工作空间不存在（{self.config.workspace}），未等待节点就绪"}
        cmd = (
            f"cd {shlex.quote(str(self.config.workspace))} && "
            f"{self._env_prefix()}{self._source_setup_prefix()}"
            f"deadline=$((SECONDS + {int(budget)})); "
            f"while [ $SECONDS -lt $deadline ]; do "
            f"if ros2 service list 2>/dev/null | grep -qx {shlex.quote(service)}; then "
            f"echo CALIB_READY; exit 0; fi; sleep {step:g}; done; echo CALIB_TIMEOUT"
        )
        logger.info(f"等标定节点就绪（{service}，上限 {budget:.0f}s）")
        started = time.time()
        try:
            completed = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                       text=True, timeout=budget + 60)
            output = completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            elapsed = time.time() - started
            logger.warning(f"等待标定节点就绪失败：{exc}")
            return {"ready": False, "elapsed": elapsed, "service": service,
                    "text": f"等待 {service} 时出错（{exc}）—— 没有下发采集指令"}
        elapsed = time.time() - started
        if "CALIB_READY" in output:
            return {"ready": True, "elapsed": elapsed, "service": service,
                    "text": f"标定节点已就绪（{service} 已注册，用时 {elapsed:.0f}s）"}
        return {"ready": False, "elapsed": elapsed, "service": service,
                "text": f"等了 {elapsed:.0f}s 仍没看到 {service} —— 节点可能没起来或正在报错"
                        f"（节点日志已接入下方日志区）"}

    def start_camera_calibration_call(self) -> subprocess.Popen:
        """发起标定采集指令（`ros2 service call`），**不等待结果**。

        返回进程句柄，由调用方（后台线程）负责等待与取消 —— 采集要走完左右臂
        和头部相机几个位姿，可能持续几分钟，绝不能在 UI 线程里等。
        """
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        setup_script = self._setup_script()
        if not setup_script.exists():
            raise FileNotFoundError(f"setup.bash 不存在: {setup_script}")

        service = self.config.ros_node("camera_calib_service")
        cmd = (
            f"cd {shlex.quote(str(self.config.workspace))} && "
            f"{self._env_prefix()}"
            f"{self._source_setup_prefix()}"
            f"ros2 service call {shlex.quote(service)} "
            f"example_interfaces/srv/Trigger \"{{}}\""
        )
        logger.info(f"下发相机标定指令: {cmd}")
        # 必须捕获输出：`ros2 service call` 的回执（success/message）是判断有没有
        # 标定成功的唯一依据，用 DEVNULL 起进程的话界面上什么也看不到。
        proc = run_background_captured(cmd, self.config.workspace)
        self.pm.register(proc, "camera_calibration_call")
        return proc

    def launch_vr_bridge(self) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        cmd = f"source {self._setup_script()} && ros2 run teleop vr_bridge_node"
        # 输出写日志文件而不是 PIPE：vr_bridge 是常驻节点，用 PIPE 又没人读取时，
        # 管道缓冲写满（64KB）会让它阻塞在 write 上 —— 表现就是标定莫名卡死。
        proc = run_background_with_log(
            cmd, self.config.workspace, self.config.vr_bridge_log_path())
        self.pm.register(proc, "vr_bridge")
        return proc

    def launch_calibration(self, username: str, tracker_number_str: str) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        cmd = self._ros_run_cmd("teleop", "calibration_node")
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(self.config.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr 并进 stdout：ROS 2 节点的日志（进度、警告、报错）默认写 stderr，
            # 而上层只读 stdout —— 分开接的话日志区几乎看不到东西。
            # 并流后上层那一个 select 就能拿到全部输出，也不会有 stderr 管道
            # 无人读、写满 64KB 后把节点卡死的问题。
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        proc.stdin.write(f"{username}\n{tracker_number_str}\n")
        proc.stdin.flush()
        self.pm.register(proc, "calibration")
        return proc


class TeleopManager:
    def __init__(self, config: ConfigManager, launcher: Launcher,
                 state_path: Optional[Path] = None):
        self.config = config
        self.launcher = launcher
        self.pm = ProcessManager()
        # 遥操作运行状态文件：界面重启后靠它重新接管后台进程
        self.state_path = (
            Path(state_path) if state_path
            else PROJECT_ROOT / "logs" / TELEOP_STATE_FILENAME
        )

    # ---------- 运行状态持久化（界面重启后仍可接管/停止） ----------
    def _save_state(self, proc: subprocess.Popen, log_file: Optional[Path]) -> None:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = proc.pid
        data = {
            "pid": proc.pid,
            "pgid": pgid,
            "start_ticks": _proc_start_ticks(proc.pid),   # 与 pid 一起唯一标识进程
            "log_file": str(log_file) if log_file else "",
            "started_at": time.time(),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"遥操作状态写入失败（重启界面后将无法接管该进程）：{exc}")

    def clear_state(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def _state_process_alive(self, state: dict) -> bool:
        """状态文件记录的进程是否仍然是原来那个、且**真的在跑**。

        - 僵尸进程算"没在跑"（见 `_process_running`）：进程表里还在，但它已经死了，
          继续当成"遥操作在运行"会让界面停在一个按不动的「停止遥操作」上；
        - Linux：比对 pid + 启动时刻，pid 被复用给别的进程时判为已消亡；
        - 其它平台（开发机没有 /proc）：退化为只判断进程是否存在。

        `pid` / `start_ticks` 来自**外部文件**，类型不保证：这里一律走 `_as_int`，
        一个 `int("abc")` 抛出去就是"启动界面即崩"（这条路径跑在 `MainWindow.__init__` 里）。
        """
        pid = _as_int(state.get("pid"))
        if not _process_running(pid):
            logger.info(f"清理遥操作残留状态：PID={pid} 已不在运行")
            return False
        if not _PROC_AVAILABLE:
            return True

        ticks = _proc_start_ticks(pid)
        if ticks is None:
            logger.info(f"清理遥操作残留状态：PID={pid} 已不在运行")
            return False
        expected = state.get("start_ticks")
        if expected is not None and _as_int(expected, -1) != ticks:
            logger.info(f"清理遥操作残留状态：PID={pid} 已被系统复用给其它进程")
            return False
        return True

    def running_state(self) -> Optional[dict]:
        """返回仍在运行的遥操作状态；进程已消失时清理残留状态文件。

        同时覆盖两种情况：本次会话内启动（内存里有 Popen）与界面重启前启动
        （只有状态文件里的 pid/pgid）。
        """
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning(f"遥操作状态文件不可用（{exc}），按未运行处理")
            self.clear_state()
            return None

        if isinstance(data, dict) and self._state_process_alive(data):
            return data
        self.clear_state()
        return None

    def known_users(self) -> List[str]:
        """能直接切过去的用户名 = 当前工作空间 ∪ 其它工作空间里的那些（去重、保持顺序）。

        给对话框列出来用。**必须包含其它工作空间的用户**：版本升级后新工作空间里
        只有出厂配置，只列当前这份的话现场就得盲打名字 —— 而盲打正是这次要治的病。
        """
        users: List[str] = []
        seen = set()
        for path in [self.config.teleop_config_path,
                     *self.config.teleop_config_candidates()]:
            for name in load_calibrations(path):
                key = normalize_user(name)
                if key and key not in seen:
                    seen.add(key)
                    users.append(name)
        return users

    def find_user(self, username: str) -> Optional[UserLookup]:
        """按输入的名字找人：先当前工作空间，再其它工作空间（后者的数据随激活一起导入）。

        这一条就是"读取输入的人员，查一遍我们的文件内是否有这个人"。名字按
        `normalize_user` 比对（大小写、首尾空白、零宽字符都不影响），命中后返回
        **文件里的原始键名** —— 写回 `teleop.calibration` 必须用它。

        纯查找，不写任何文件；真正落盘在 `activate_user`。
        """
        wanted = normalize_user(username)
        if not wanted:
            return None
        own = self.config.teleop_config_path
        entry = self._entry_in(own, wanted)
        if entry is not None:
            return UserLookup(self._key_in(own, wanted), None, entry)
        for path in self.config.teleop_config_candidates():
            entry = self._entry_in(path, wanted)
            if entry is not None:
                return UserLookup(self._key_in(path, wanted), path, entry)
        return None

    @staticmethod
    def _key_in(path: Path, wanted: str) -> Optional[str]:
        for name in load_calibrations(path):
            if normalize_user(name) == wanted:
                return name
        return None

    def _entry_in(self, path: Path, wanted: str) -> Optional[dict]:
        key = self._key_in(path, wanted)
        return load_calibrations(path).get(key) if key else None

    def check_user(self, username: str) -> bool:
        """我们的文件里有没有这个人。"""
        return self.find_user(username) is not None

    def activate_user(self, username: str, source: Optional[Path] = None) -> bool:
        """把「当前标定用户」切成 username，返回配置文件是否写成功。

        `source` 非空（这个人的标定在别的工作空间）时先把它导进当前工作空间：
        机器人侧的 teleop_node 只读当前工作空间那份配置，不导入的话遥操作会退回
        默认标定 —— 界面显示切成功了、机器人用的却是别人的参数，这种不一致最难查。
        """
        key = self.config.find_calibration_key(username)
        if key is None:
            # 不在当前工作空间：把那个人的标定导进来。键名沿用来源文件里的写法，
            # 导入后 `set_teleop_calibration_user` 才能按这个名字把它切过去。
            key = username
            if source is not None and not self.config.import_calibration(key, source):
                logger.warning(f"{key} 的标定未能从 {source} 导入，按当前位置的配置继续")
        return self.config.set_teleop_calibration_user(key)

    def cancel_calibration(self):
        """取消标定：终止 vr_bridge 和 calibration 进程"""
        self.pm.terminate_by_name("vr_bridge")
        self.pm.terminate_by_name("calibration")

    def stop_teleop(self) -> bool:
        """停止后台遥操作（含 ros2 launch 拉起的所有节点）。返回是否确有待停进程。

        进程来自本次会话时走 ProcessManager（有 Popen 句柄，整组 SIGINT）；
        来自上一次界面会话时只有状态文件，按记录的 pgid 发信号。
        """
        if self.pm.find_by_name("run_teleop") is not None:
            self.pm.terminate_by_name("run_teleop")
            self.clear_state()
            return True

        state = self.running_state()
        if state is None:
            logger.warning("没有正在运行的遥操作进程")
            return False

        # 状态文件是外部输入：字段可能是字符串、可能是半截文件，一律安全转换。
        # `int(state["pid"])` 在缺字段时是 KeyError、类型不对时是 ValueError ——
        # 两者都会从界面槽里抛出去（虽然被 guard_slot 拦住，但停止动作就此失效了）。
        pid = _as_int(state.get("pid"))
        pgid = _as_int(state.get("pgid"), pid) or pid
        logger.info(f"停止界面重启前启动的遥操作（PID={pid}, PGID={pgid}）")
        try:
            if pgid == os.getpgid(0):
                os.kill(pid, signal.SIGINT)
            else:
                os.killpg(pgid, signal.SIGINT)
        except (ProcessLookupError, OSError) as exc:
            logger.warning(f"发送停止信号失败（进程可能已退出）：{exc}")
        self.clear_state()
        return True

    def run_calibration(self, username: str, tracker_number: int, output_callback=None) -> Tuple[bool, Optional[str], Optional[float], Optional[float]]:
        tracker = f"tracker{tracker_number}"
        try:
            self.launcher.launch_vr_bridge()   # 注册已由 launch_vr_bridge 完成
            time.sleep(1)
            cal_proc = self.launcher.launch_calibration(username, str(tracker_number))
            self.pm.register(cal_proc, "calibration")
            sw, al = None, None
            calibration_done = False
            timeout = self.config.teleop_calibration_timeout()
            start = time.time()
            while time.time() - start < timeout:
                import select
                if select.select([cal_proc.stdout], [], [], 0.1)[0]:
                    line = cal_proc.stdout.readline()
                    if not line:
                        if cal_proc.poll() is not None:
                            if calibration_done:
                                break
                            return False, f"标定节点意外退出: {_read_remaining_stdout(cal_proc)}", None, None
                        continue
                    if output_callback:
                        output_callback(line.strip())
                    logger.debug(f"calibration: {line.strip()}")
                    if "标定完成" in line:
                        calibration_done = True
                        for _ in range(5):
                            nxt = cal_proc.stdout.readline()
                            if not nxt:
                                break
                            if output_callback:
                                output_callback(nxt.strip())
                            m = re.search(r"shoulder_width\s*=\s*([\d.]+)", nxt)
                            if m:
                                sw = float(m.group(1))
                            m = re.search(r"arm_length\s*=\s*([\d.]+)", nxt)
                            if m:
                                al = float(m.group(1))
                        break
                else:
                    if cal_proc.poll() is not None:
                        if calibration_done:
                            break
                        return False, f"标定节点意外退出: {_read_remaining_stdout(cal_proc)}", None, None

            if sw is None or al is None:
                rest = _read_remaining_stdout(cal_proc)
                if output_callback and rest:
                    output_callback(rest.strip())
                m = re.search(r"shoulder_width\s*=\s*([\d.]+)", rest)
                if m:
                    sw = float(m.group(1))
                m = re.search(r"arm_length\s*=\s*([\d.]+)", rest)
                if m:
                    al = float(m.group(1))
                if sw is None or al is None:
                    return False, f"解析标定结果失败: {_read_remaining_stdout(cal_proc)}", None, None

            self.config.add_calibration_user(username, tracker, sw, al, 0.35)
            return True, None, sw, al
        except Exception as e:
            logger.error(f"标定异常: {e}", exc_info=True)
            return False, str(e), None, None
        finally:
            # 收尾必须放在 finally：原先这段在正常路径和 except 里各写一遍，而中间
            # 那几条 `return False`（节点意外退出、解析失败）既不进 except 也不走
            # 正常收尾，于是 vr_bridge 这个常驻节点被留在后台 —— 累积起来会让
            # 下一次标定撞上话题冲突。
            self.pm.terminate_by_name("vr_bridge")
            self.pm.terminate_by_name("calibration")

    def launch_teleop(self, log_file: Optional[Path] = None) -> subprocess.Popen:
        """启动遥操作并记录运行状态（供界面重启后接管）。

        状态文件里已经有活着的遥操作时直接拒绝启动，避免同名进程互相覆盖状态。
        """
        existing = self.running_state()
        if existing is not None:
            raise RuntimeError(
                f"遥操作已在运行（PID={existing.get('pid')}），请先停止再启动"
            )
        proc = self.launcher.launch_teleop(log_file)
        self._save_state(proc, log_file)
        return proc