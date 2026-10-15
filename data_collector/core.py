import os
import sys
import re
import json
import time
import signal
import subprocess
import logging
import unicodedata
import yaml
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Any, Dict

from . import convert_tool

# ============ 项目路径约定 ============
# 项目根目录（data_collector 包的上一级）：configs/、scripts/、logs/ 等统一相对它定位。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------- 默认路径与远端参数（唯一来源） ----------
# 这些默认值原先在 core.py 与 core_data.py 各写一份 —— 改一处不生效，另一处
# 静默沿用旧值（远端地址就这么重复定义过）。统一收敛到这里。
DEFAULT_NERV_DIR = Path("/var/nerv")
DEFAULT_BAG_DIR = DEFAULT_NERV_DIR / "bag"
DEFAULT_LOG_DIR = DEFAULT_NERV_DIR / "log"
DEFAULT_VISION_CALIB_DIR = DEFAULT_NERV_DIR / "vision/calib"
DEFAULT_REMOTE_HOST = "172.16.10.2"
DEFAULT_REMOTE_PORT = 22
DEFAULT_REMOTE_USER = "nerv"
DEFAULT_REMOTE_BASE = "/NervData/share/logs/遥操作数据"
DEFAULT_SCRIPTS_DIR = "scripts"
#: 末端参数文件（相对工作空间）。机器人侧 arm_control_node 启动时读它 —— 所以上位机改完
#: 必须提醒"重启主程序生效"；本项可在 app_config.yaml 里覆盖（老配置没有这一项时用默认值）。
DEFAULT_ARM_CONTROL_CONFIG = "install/config/arm_control_config.yaml"
#: 姿势预设（相对工作空间）。机器人侧的 `install/product/config/*_motion.json` —— 示教脚本与
#: 手柄读的就是这两份，所以「姿势控制」页面**只读**它们（沿用现场既有的槽位名）；
#: 现场自定义的预设写到上位机自己的 configs/motion_presets.yaml，不动机器人侧交付物。
DEFAULT_ARM_MOTION = "install/product/config/arm_motion.json"
DEFAULT_TRUNK_MOTION = "install/product/config/trunk_motion.json"

# ============ 日志 ============
def setup_logger(name: str = "data_collector", log_file: Path = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger

logger = setup_logger()

# ============ YAML 工具 ============
def load_yaml(path: Path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def save_yaml(path: Path, data: dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ============ app_config.yaml 的按行改写 ============
# 界面改「导出目标」时要写回 `app_config.yaml`，而那份文件**满是说明性注释**
# （每一行都要告诉现场"这个键是干什么的"）。整份 `yaml.dump` 会把注释全丢掉 ——
# 那是现场唯一的键说明，丢了就再也找不回来。
#
# 所以按行改：只替换目标行 `:` 之后、`#` 之前的那一段，其余逐字保留。
# 与 `core_operations.patch_arm_control_text`（改机器人侧 arm_control_config.yaml）
# 是同一套做法，那边踩过的坑这里同样成立：
# **找不到键就必须如实报出来，不能写盘** —— 写了一半的配置比不写更糟。

#: `export_target` 不存在时补的那一行（连同它上面那句说明）。
EXPORT_TARGET_HINT = """\
# 导出目标：数据往哪儿送。server=只送远端服务器（缺省）| disk=只写移动硬盘 | both=两边都写
export_target: {target}
"""

#: `disk_export` 段不存在时补的整段（含注释），与 `configs/app_config.example.yaml`
#: 保持一致 —— 两处文案不同的话，现场拿到手会以为是两个不同的键。
#:
#: **这一段刻意不含 `export_target`**：那个键一律由上面那一行负责。
#: 让一个"追加整段"的块里也带一个顶层键，就得额外判"文件里是不是已经有一行了"——
#: 判错的后果是**两个同名顶层键**，YAML 取后一个、静默覆盖（第一版就是这么错的）。
#: 拆开之后这个类错误从结构上不可能发生。
DISK_EXPORT_BLOCK = """\

# 这一段只有真正要写硬盘时才需要配。不配它就不会校验盘标识 ——
# 避免在纯服务器模式下自检报一个与本次操作无关的"盘不对"。
disk_export:
  mount_point: {mount_point}         # 移动硬盘挂载点，如 /media/robot/NERV 或 /mnt/nerv_data
  volume_id_file: .nerv_volume       # 盘内标识文件，用于防"插错盘"
                                     # 配了本段就默认要求它存在（可显式写 false 关闭）
  min_free_margin_mb: 2048           # 导出前的可用空间余量，不足时提前报错
  warn_file_bytes: 3800000000        # 超过这个大小的文件会提醒（FAT32 单文件上限约 4GB）
                                     # 只提醒不拦截；目标盘为 exFAT/NTFS 时自动跳过
"""


#: 写回时**不加引号**的标量白名单。
#: 用白名单而不是"看字符像不像路径"：后者会漏 —— `str.isalnum()` 对中文返回 True，
#: 于是 `/media/机器人盘` 被当成"安全值"不加引号（实测踩到）；
#: 而路径里若含 `#`，不加引号会被 YAML 当成注释起点，值被截断。
_UNQUOTED_SCALARS = frozenset({
    "server", "disk", "both", "true", "false", "null", "none",
    "auto", "stream", "tempfile", "zip", "tar", "store", "deflate",
})


def _quote_scalar(value: str) -> str:
    """YAML 标量：枚举值照原样，**其余一律加双引号**。

    与配置文件里的既有写法保持一致（`export_target: server` 不带引号、
    `mount_point: ""` 带引号），否则现场会看到"同一个键两种写法"。
    """
    value = str(value)
    if value in _UNQUOTED_SCALARS:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _replace_scalar_keep_comment(line: str, new_value: str) -> str:
    """替换一行里 `:` 之后、`#` 之前的值，**行内注释与缩进逐字保留**。

    `#` 的判断要避开值内部的井号：YAML 里 `#` 只有在**前面有空白**时才是注释
    （`a: "x#y"` 的井号不是注释）。这里按"前面是空白且不在引号内"来找。

    **注释与值之间那个空格必须补回来**：`cut` 指向 `#`，而 `#` 前的空格属于
    "值那一侧"、会被丢掉，直接拼就成了 `export_target: both# 说明` ——
    那个 `#` 前没有空白，YAML **不认它是注释**，于是整段 `# 说明` 成了值的一部分
    （实测：解析出来是 `'both# server=只送远端 | disk=只写移动硬盘'`）。

    **行尾的换行符也要单独摘出来保住**：没有 `#` 时 `cut` 落在行尾，
    若把换行也算进"要丢掉的部分"，这一行就会与下一行**粘成一行** ——
    于是下一行那句注释成了本行的值（实测：`export_target: server# 这一段只有…`）。
    """
    newline = ""
    if line.endswith("\n"):
        line, newline = line[:-1], "\n"
    colon = line.find(":")
    if colon < 0:
        return line + newline
    head = line[:colon + 1]                       # 含缩进与 `键:`
    rest = line[colon + 1:]
    in_quote = None
    cut = len(rest)
    for i, ch in enumerate(rest):
        if in_quote:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in "\"'":
            in_quote = ch
            continue
        if ch == "#" and i > 0 and rest[i - 1] in " \t":
            cut = i
            break
    tail = rest[cut:]                             # 含注释
    if tail and not tail.startswith((" ", "\t")):
        tail = " " + tail                         # 补回被丢弃的那个空格
    # 值前后各留一个空格，与配置文件里 `key: value` 的写法一致
    return f"{head} {new_value}{tail}{newline}"


def _find_key_line(lines, key: str, indent: str = "") -> int:
    """找 `键:` 所在行号；找不到返回 -1。

    只在**顶层**（indent=""）或指定缩进层里找 —— 否则 `mount_point` 会匹配到
    别的段里同名的键（这份配置里有 60 多个顶层键，重名不是假想）。
    """
    prefix = f"{indent}{key}:"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return -1


def patch_export_settings_text(text: str, target: str,
                               mount_point: Optional[str] = None) -> dict:
    """按行改 `app_config.yaml` 里的导出目标；键不存在时**追加整段**。

    为什么必须支持"追加"：现场在用的 `app_config.yaml` 大多**早于**这两个键
    （实测本机这份就完全没有 `export_target` 与 `disk_export` 段）。
    只做替换的话，界面写回会静默什么都没改 —— 用户以为存进去了。

    Args:
        text: 原文件全文。
        target: `server` / `disk` / `both`。
        mount_point: 挂载点；`None` 表示**不动它**（只改导出目标时用）。

    Returns:
        dict: ``{"text": 新全文, "applied": [...], "appended": [...]}``。
        调用方必须检查两个列表是否覆盖了预期的键，不覆盖就不能写盘。
    """
    lines = text.splitlines(keepends=True)
    applied: List[str] = []
    appended: List[str] = []

    idx = _find_key_line(lines, "export_target")
    if idx >= 0:
        lines[idx] = _replace_scalar_keep_comment(lines[idx], _quote_scalar(target))
        applied.append("export_target")
    else:
        appended.append("export_target")

    mount_idx = -1
    if mount_point is not None:
        disk_idx = _find_key_line(lines, "disk_export")
        if disk_idx >= 0:
            # 只在这个段**之内**找 mount_point（下一个顶层键之前）
            mount_idx = -1
            for i in range(disk_idx + 1, len(lines)):
                line = lines[i]
                if line.strip() and not line[0].isspace():
                    break              # 到下一个顶层键了
                if line.startswith("  mount_point:"):
                    mount_idx = i
                    break
            if mount_idx >= 0:
                lines[mount_idx] = _replace_scalar_keep_comment(
                    lines[mount_idx], _quote_scalar(mount_point))
                applied.append("disk_export.mount_point")
            else:
                appended.append("disk_export.mount_point")
        else:
            appended.append("disk_export.mount_point")

    if appended:
        # 追加缺失的键。两种情形**分开处理**，不混在一个分支里：
        #   ① `export_target` 缺 → 末尾单独补一行
        #   ② `disk_export` 段（或段内 `mount_point`）缺 → 末尾补整段
        # 两个都缺是现场最常见的（老配置）；只缺后者也发生过（段被手工删过）。
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")

        if "export_target" in appended:
            lines.append("\n")
            lines.extend(EXPORT_TARGET_HINT.format(target=_quote_scalar(target)).splitlines(keepends=True))
            applied.append("export_target")

        if "disk_export.mount_point" in appended:
            lines.extend(DISK_EXPORT_BLOCK.format(
                mount_point=_quote_scalar(mount_point or "")).splitlines(keepends=True))
            applied.append("disk_export.mount_point")

        appended = []

    return {"text": "".join(lines), "applied": applied, "appended": appended}


# ============ 遥操作用户名 ============
# 用户名的"同一个人"判定：现场是**手打**进去的（TeleopDialog 只有一个输入框），
# 打出来的是不是文件里那个键，全看有没有多打一个空格、大小写对不对。
# 实测（探针 probe_user_switch.py）：文件里是 `hyx` 时，输入 `HYX` 或带零宽空格的
# `hyx` 都会被判成"这个没标定过"→ 弹一整套 VR 标定流程。所以查找一律先归一化。
_USER_INVISIBLE_CHARS = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")


def normalize_user(name: Any) -> str:
    """用户名的比较键：NFKC 归一化 + 去掉不可见字符与首尾空白 + casefold。

    NFKC 是为了处理输入法：中文输入法下很容易打出全角 `ＨＹＸ`，而文件里存的是
    半角 `hyx` —— 这两者肉眼几乎一样，判成两个人就是一次白跑的标定。

    不做"拼音/模糊匹配"这类聪明事：那是另一个量级的误判风险（把 A 的标定
    套给 B 比让人重新标定一次糟得多）。这里只吸收**肉眼看不出来**的差异。
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    for ch in _USER_INVISIBLE_CHARS:
        text = text.replace(ch, "")
    return text.strip().casefold()


def load_calibrations(path: Path) -> Dict[str, Any]:
    """安全读出某个 teleop 配置里的 calibrations 段。

    读不到（文件不存在 / 不是 YAML 映射 / 语法坏）一律返回空表并记一条日志：
    调用方包括"启动遥操作"的对话框与跨工作空间搜索，那里不该因为一份坏配置
    就打不开对话框或者整个切用户流程失败。文件损坏的事实仍然要留痕。
    """
    try:
        data = load_yaml(Path(path))
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"遥操作配置不可读（{path}）：{exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"遥操作配置不是映射结构（{path}），按空处理")
        return {}
    calibs = data.get("calibrations")
    if not isinstance(calibs, dict):
        return {}
    return {str(k): v for k, v in calibs.items() if isinstance(v, dict)}


def _safe_subdirs(base: Path) -> List[Path]:
    """列目录，出错当空（跨工作空间搜索跑在点按钮的路径上，不能被权限问题打断）。"""
    try:
        return sorted(child for child in base.iterdir() if child.is_dir())
    except OSError:
        return []


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _yaml_scalar(value: str) -> str:
    """把字符串转成安全的 YAML 标量（含空格/冒号/# 时加单引号）。"""
    if re.search(r"[\s:#'\"]", value):
        return "'" + value.replace("'", "''") + "'"
    return value

# ============ 子进程工具 ============
def human_size(size: float) -> str:
    """人类可读体积（B/KB/MB/GB/TB/PB）。

    这是全项目**唯一**的体积格式化实现：远端目录浏览器、本地磁盘空间显示、
    批量删除的体积统计都走它。此前 remote_browser 里另有一份同样逻辑的
    staticmethod —— 两份实现意味着"同一个 12.3 GB"在界面不同位置可能长得不一样。
    """
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} EB"


def dir_size(path: Path) -> int:
    """递归统计目录占用字节数。

    这是**阻塞**操作：几十 GB 的 bag 目录在机械盘上要走几秒。调用方必须在
    后台线程里调它，否则界面会卡住（批量删除就是这么处理的）。
    读不到的项按 0 计 —— 统计不准比弹异常好，用户要的是"大概多大"。
    """
    total = 0
    # followlinks 默认 False：不跟进符号链接，避免环状目录把统计挂死
    for root, _dirs, files in os.walk(path, onerror=lambda exc: None):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内（两边都先解析，防 `..` 与符号链接绕过）。

    用于删除前的越界校验：只允许删配置目录里的东西，别让一个手滑的路径
    把无关数据带走。解析失败（路径不存在）时退回字面比较。
    """
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
    except OSError:
        child_r, parent_r = child, parent
    return child_r == parent_r or parent_r in child_r.parents


def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def run_in_terminal(command: str, terminal: str = "gnome-terminal") -> subprocess.Popen:
    """在新终端中执行命令，自动选择可用终端（优先 xterm）。

    必须 `start_new_session=True`：终端及其拉起的进程要落在独立会话/进程组里。
    否则它们与上位机同组，`terminate()` 里的 `os.killpg` 会把 SIGINT 一起发给
    上位机自己（点「关闭程序」会把界面一起带走）。
    """
    if "DISPLAY" not in os.environ:
        logger.warning("DISPLAY 未设置，可能无法启动图形终端")

    if check_command_exists("xterm"):
        # 以 argv 数组传参，避免命令行里的单引号与外层引用冲突；
        # -hold 让脚本退出后窗口保留，方便查看报错。
        cmd = ["xterm", "-hold", "-e", "bash", "-c", f"{command}; exec bash"]
        try:
            return subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            logger.warning(f"xterm 启动失败: {e}")

    if check_command_exists("gnome-terminal"):
        cmd = ["gnome-terminal", "--disable-factory", "--", "bash", "-c", f"{command}; exec bash"]
        try:
            return subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            logger.warning(f"gnome-terminal (disable-factory) 启动失败: {e}")

    if check_command_exists(terminal):
        cmd = [terminal, "--", "bash", "-c", f"{command}; exec bash"]
        try:
            return subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            raise RuntimeError(f"终端启动失败: {e}")

    raise RuntimeError("没有可用的终端模拟器 (xterm, gnome-terminal)")

def run_background(command: str, cwd: Path) -> subprocess.Popen:
    """后台运行命令（无终端），创建独立进程组"""
    return subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

def run_background_captured(command: str, cwd: Path) -> subprocess.Popen:
    """后台运行并**捕获**输出，供调用方用 `communicate()` 读取。

    只适用于**一次性**命令（如 `ros2 service call`）：常驻节点请用
    `run_background_with_log`，否则没人读的管道写满 64KB 会把进程卡死在 write 上。

    stderr 合并进 stdout：调用方关心的是"命令到底输出了什么"，分开拿反而容易漏。
    """
    return subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def run_background_with_log(command: str, cwd: Path, log_file: Path) -> subprocess.Popen:
    """后台运行命令并把输出写入日志文件（供界面实时回显、也可离线查看）。

    刻意不使用管道：界面关闭后进程只是继续写文件，不会因管道写满而阻塞。
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_file, "ab", buffering=0)
    try:
        return subprocess.Popen(
            ["bash", "-c", command],
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()   # 子进程已持有 fd，父进程句柄可以立即关闭

def run_with_env(cmd_list: List[str], env_add: dict = None, cwd: Path = None) -> subprocess.Popen:
    env = os.environ.copy()
    if env_add:
        env.update(env_add)
    return subprocess.Popen(
        cmd_list,
        env=env,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

def _same_process_group(pid: int) -> bool:
    """判断 pid 是否与上位机同进程组。"""
    try:
        return os.getpgid(pid) == os.getpgid(0)
    except (ProcessLookupError, OSError):
        return False

def terminate(proc: subprocess.Popen) -> None:
    """终止进程及其子进程，发送 SIGINT (Ctrl+C) 模拟优雅退出。

    只有子进程在独立会话/进程组时才用 `killpg` 整组终止（ros2 launch 会拉起
    一堆节点，必须整组发信号）。若子进程与上位机同组——历史版本或第三方以
    普通方式启动的终端——`killpg` 会把 SIGINT 一并发给上位机自己，此时退化为
    只对子进程发信号。
    """
    if not proc or proc.poll() is not None:
        return

    if _same_process_group(proc.pid):
        logger.warning(
            f"子进程 PID={proc.pid} 与上位机同进程组，"
            f"改为只终止该进程（避免整组信号误伤界面）"
        )
        _signal_one(proc)
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=3)
    except (ProcessLookupError, OSError) as e:
        logger.debug(f"无法发送进程组信号，回退到单个进程: {e}")
        _signal_one(proc)
    except subprocess.TimeoutExpired:
        proc.kill()

def _signal_one(proc: subprocess.Popen) -> None:
    """退路：只对子进程本体发 SIGINT，超时再强杀。"""
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=3)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        proc.kill()

# ============ 配置管理器 ============
# SDK 预设位动作表（对应 /arm_control/motion 的 0-6）。
# **仅作为旧配置文件的回退**：现在的标签是"数据采集任务单"，由现场自己定义，
# 与这套预设位没有关系。保留它是为了没有 record_action_labels 键的老配置
# 还能读出一个合理的初始清单。
# （《轮式机器人SDK接口》：0 双臂垂下 / 1 预置位 / 2 邀请 / 3 递话筒 / 4 挥手 / 5 演讲 / 6 端盘）
DEFAULT_ACTION_LABELS = {
    0: "双臂垂下", 1: "预置位", 2: "邀请", 3: "递话筒", 4: "挥手", 5: "演讲", 6: "端盘",
}

# 版本号形态：v1.4.0 / 1.4.0 / v1.4 都能认
VERSION_RE = re.compile(r"v?\d+\.\d+(?:\.\d+)?")
# doc/version.txt 里这一行才是"版本"，其余是构建元信息
VERSION_BRANCH_RE = re.compile(r"^\s*分支\s*[:：]\s*(.+?)\s*$", re.MULTILINE)


def parse_version_text(text: str) -> str:
    """从 doc/version.txt 的正文里取出短版本号（形如 v1.4.0）。

    该文件是为排障写的构建说明，格式可能变，所以按"先找 分支: 行、再全文兜底"
    的顺序取，而不是写死整行匹配。取不到就返回空串，交给上层回退 VERSION 文件。
    """
    match = VERSION_BRANCH_RE.search(text or "")
    if match:
        found = VERSION_RE.search(match.group(1))
        if found:
            return found.group(0)
    found = VERSION_RE.search(text or "")
    return found.group(0) if found else ""


# 构建信息行：中英文冒号都可能出现，且值里本身带冒号（时间 10:10:58），
# 所以必须按"字段名 + 分隔符"整条匹配，不能简单 split(":")。
VERSION_DETAIL_FIELDS = (
    ("onBuildByRe", "packer", r"打包人"),
    ("onBuildAtRe", "built_at", r"打包时间"),
    ("onBranchRe", "branch", r"分支"),
    ("onCommitRe", "commit", r"commit\s*id"),
)


def parse_version_details(text: str) -> dict:
    """把 doc/version.txt 的构建信息解析成 dict（给界面做 tooltip 用）。"""
    details = {}
    for _attr, field, keyword in VERSION_DETAIL_FIELDS:
        match = re.search(rf"^\s*{keyword}\s*[:：]\s*(.+?)\s*$", text or "", re.MULTILINE)
        if match:
            details[field] = match.group(1).strip()
    return details


ACTION_LABELS_HEADER = (
    "# 数据采集任务单（由界面「数据录制 → 管理任务」维护，也可以手改）\n"
    "# name      : 任务名，会写进 info.json 的 label 字段\n"
    "# action_id : 可选，对应 SDK /arm_control/motion 的编号；\n"
    "#             采集任务一般留 null（不需要预设位动作编号）\n"
)

POINT_LABELS_HEADER = (
    "# 打点标签（由界面「打点 → 管理标签」维护，也可以手改）\n"
    "# 分组：'*' 是通用组（装开始 / 结束这类夹在流程两端的标签），其余键是采集任务名\n"
    "# 面板里的顺序 = 通用组首项 → 该任务组的标签 → 通用组其余（末项落在最后）\n"
    "# name      : 标签名，会写进 points.csv 的「标签」列\n"
    "# action_id : 「组内编号」，只决定本组内的先后（没编号的排在组内最后）；\n"
    "#             points.csv 的「标签编号」写的是面板里的流程序号（1..N），不是这个值\n"
)


def _sort_by_number(items: List[dict]) -> List[dict]:
    """按**组内编号**排序；没编号的排在它那一组的最后（组内保持原顺序）。

    用稳定排序：编号相同的条目保持文件里的先后，不会因为"排序"把人工排好的顺序打乱。
    """
    def key(item):
        action_id = item.get("action_id")
        return (1, 0) if action_id is None else (0, action_id)
    return sorted(items, key=key)


def merge_point_flow(items: List[dict], common_task: str) -> List[dict]:
    """把"通用组 + 任务组"两段合并成打点面板里的**一条流程**。

    通用组装的是"开始 / 结束"这类夹在流程两端的标签，所以合并规则是：
    **通用组第一项放最前、其余放最后**（末项因此落在整条流程末尾），中间是本次任务的标签。
    现场看到的是一条 `1 开始任务 … 6 结束任务` 的完整顺序，而不是"通用两个 + 任务四个"
    两段各编各的号 —— 后者在面板里会让人以为编号重复了。

    组内顺序按各自的编号（`_sort_by_number`）；通用组只有一项时放在最前面。
    """
    common = _sort_by_number([i for i in items if i.get("task") == common_task])
    task_items = _sort_by_number([i for i in items if i.get("task") != common_task])
    if len(common) <= 1:
        return common + task_items
    return common[:1] + task_items + common[1:]


class ActionLabelStore:
    """采集任务单（动作标签）的存储与增删改查。

    为什么从 ConfigManager 里独立出来：它是配置类里**唯一带完整业务逻辑**的一组 ——
    三种历史格式的归一化、去重、唯一性校验、独立文件落盘；其余方法本质都是"带默认值
    的配置读取"，拆开只能得到纯转发样板，收益抵不上成本。放在独立类里既能单独测试，
    也不会再被卷进配置类的改动。

    存储刻意与 app_config.yaml 分开：标签是用户数据（界面里随时增删），而配置是带
    注释的文件 —— 把用户数据写回配置会让 save_yaml 重排整个文件、注释全丢。
    """

    #: 通用组的组名（不归属任何任务的打点标签）。用 "*" 而不是"通用"两个字，
    #: 免得现场真有个采集任务就叫"通用"，两组撞在一起分不开。
    COMMON_TASK = "*"

    def __init__(self, app_config: dict, default_file: Path, *,
                 seed_key: str = "record_action_labels",
                 file_key: str = "action_labels_file",
                 label_kind: str = "采集任务",
                 save_header: str = "",
                 grouped: bool = False):
        """一套标签的存储。

        打点标签与采集任务标签**共用这套实现**（结构都是 `{name, action_id}`），
        靠 seed_key / file_key / label_kind 区分。复制一份出来只会让归一化、
        去重、唯一性校验这些逻辑变成两处要同步维护的东西 —— 而它们已经因为
        "三种历史格式"吃过一次苦头了。

        Args:
            grouped: 是否按"任务"分组。打点标签开启 —— 现场需求是"选了布草挂架
                这个任务，打点就该是找角挂架那一套"。采集任务标签保持扁平
                （它本身就是任务清单，没有更高一层）。
        """
        self._config = app_config
        self._default_file = default_file
        self._seed_key = seed_key
        self._file_key = file_key
        self.label_kind = label_kind
        # grouped 必须在 _default_header() 之前赋值 —— 那个方法要按是否分组
        # 生成不同的注释（编号是否只要求组内唯一）
        self.grouped = bool(grouped)
        self._save_header = save_header or self._default_header()

    def file_path(self) -> Path:
        """标签存储文件：配置项（默认 action_labels_file）> 内置默认文件。"""
        raw = self._config.get(self._file_key)
        if raw:
            return Path(str(raw)).expanduser()
        return self._default_file

    def get(self) -> List[dict]:
        """采集任务列表，每项 {"name": str, "action_id": Optional[int]}。

        action_id 是**可选**的兼容字段（对应 SDK /arm_control/motion 的编号）。
        采集任务多为自研动作，默认留 None 即可 —— 填了会一并写进 info.json。

        文件不存在时用 record_action_labels（没有则 SDK 默认表）作为初始内容，
        但**不在读取时落盘** —— 只有用户真的增删了才创建文件。
        """
        items = self._read()
        # None 表示"没有可用的标签文件"，才用播种清单；
        # 空列表表示用户把标签全删了，那是他的意思，不能给他塞回默认标签。
        return items if items is not None else self.seed()

    def seed(self) -> List[dict]:
        """初始标签：配置里的种子项（`seed_key`）。

        配置里**没有这一项**时：采集任务标签回退 SDK 默认 7 个动作（兼容旧配置文件）；
        打点标签没有 SDK 默认表，回退空清单（现场自己维护）。
        显式写了（哪怕是空清单 []）就照写，不再回退 —— 否则"我把标签清空"
        会变成"默认标签又冒出来"。
        """
        if self._seed_key not in self._config:
            if self._seed_key != "record_action_labels":
                return []
            return [{"name": name, "action_id": action_id}
                    for action_id, name in sorted(DEFAULT_ACTION_LABELS.items())]
        return self.normalize(self._config.get(self._seed_key))

    def _read(self) -> Optional[List[dict]]:
        """读标签文件。

        Returns:
            标签列表；**文件不存在或读不动时返回 None**（调用方退回播种清单）。
            注意"空列表"是合法结果，不能和 None 混为一谈。
        """
        path = self.file_path()
        if not path.is_file():
            return None
        try:
            data = load_yaml(path) or {}
        except Exception as exc:
            logger.warning(f"读取{self.label_kind}失败（{path}）：{exc}")
            return None
        raw = data.get("labels") if isinstance(data, dict) else data
        return self.normalize(raw)

    def normalize(self, raw) -> List[dict]:
        """归一化所有历史/手写写法，非法条目跳过并告警。

        无分组（采集任务标签）支持三种：
        - 映射（旧格式）：{4: 挥手, 0: 双臂垂下}
        - 字符串列表：[挥手, 倒水]
        - 标准列表：[{name: 挥手, action_id: 4}, {name: 倒水}]

        有分组（打点标签）再多两种：
        - 分组映射：{布草挂架: [{name: 开始任务, action_id: 1}], "*": [...]}
        - 扁平列表 → 一律归入通用组（兼容"只有一套打点标签"的旧写法）

        注意分组映射与旧的"编号 → 名字"映射**都是 dict**，只能看 value 的类型区分：
        value 是 list 才是分组；不看这一层会把 `4: 挥手` 当成任务名 `4`。
        """
        if not raw:
            return []

        # 只有分组模式才补 task 字段：采集任务标签本来就没有"组"这一层，
        # 给每条塞一个 `task: None` 只会让所有调用方与测试多处理一个没意义的键
        def _tag(entry: dict) -> dict:
            if self.grouped:
                entry["task"] = self.COMMON_TASK
            return entry

        items: List[dict] = []
        if isinstance(raw, dict):
            if self.grouped and any(isinstance(v, (list, tuple)) for v in raw.values()):
                for task, entries in raw.items():
                    group = str(task).strip() or self.COMMON_TASK
                    for entry in self._normalize_entries(entries):
                        entry["task"] = group
                        items.append(entry)
            else:
                for key, value in raw.items():
                    try:
                        items.append(_tag({"name": str(value).strip(),
                                           "action_id": int(key)}))
                    except (TypeError, ValueError):
                        logger.warning(f"标签配置里的键不是数字，已跳过: {key!r}")
        elif isinstance(raw, (list, tuple)):
            for entry in self._normalize_entries(raw):
                items.append(_tag(entry))
        else:
            logger.warning("标签配置不是映射也不是列表，按空清单处理")

        # 去重：分组模式下同一个名字可以出现在不同任务里，所以按 (任务, 名字) 去重
        seen = set()
        unique = []
        for item in items:
            key = (item.get("task"), item["name"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _normalize_entries(self, entries) -> List[dict]:
        """归一化一组标签（字符串列表 / 标准列表两种写法）。"""
        items: List[dict] = []
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            logger.warning(f"标签组不是列表，已跳过: {entries!r}")
            return items

        for entry in entries:
            if isinstance(entry, str):
                items.append({"name": entry.strip(), "action_id": None})
                continue
            if not isinstance(entry, dict):
                logger.warning(f"标签条目格式不对，已跳过: {entry!r}")
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                logger.warning(f"标签缺少 name，已跳过: {entry!r}")
                continue
            action_id = entry.get("action_id")
            if action_id in ("", "null", None):
                action_id = None
            else:
                try:
                    action_id = int(action_id)
                except (TypeError, ValueError):
                    logger.warning(f"标签 {name!r} 的 action_id 不是数字，按自定义处理")
                    action_id = None
            items.append({"name": name, "action_id": action_id})
        return items

    def _default_header(self) -> str:
        """没指定注释头时的兜底：按自己的类型生成通用说明。

        刻意**不**默认套用采集任务那段（它写着"会写进 info.json"）—— 谁忘了传，
        就会在一个打点标签文件顶部看到"会写进 info.json"这种彻底误导的说明。
        """
        return (f"# {self.label_kind}（由界面维护，也可以手改）\n"
                f"# name      : 名称，会写进对应记录\n"
                f"# action_id : 可选编号"
                + ("；分组模式下只要求**组内**唯一\n" if self.grouped else "\n"))

    def _serialize(self, items: List[dict]):
        """落盘结构。分组模式按任务分组，而不是"每条带一个 task 字段"的扁平表 ——
        这个文件现场会手改，按任务缩进一眼就知道哪几条属于哪个任务。"""
        if not self.grouped:
            return [{"name": item["name"], "action_id": item.get("action_id")}
                    for item in items]
        grouped: Dict[str, List[dict]] = {}
        for item in items:
            task = item.get("task") or self.COMMON_TASK
            grouped.setdefault(task, []).append(
                {"name": item["name"], "action_id": item.get("action_id")})
        return grouped

    def save(self, items: List[dict]) -> bool:
        """把标签列表写回文件。写文件是原子性最弱的一环，失败只记日志不抛。"""
        path = self.file_path()
        header = self._save_header
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            save_yaml(path, {"labels": self._serialize(items)})
            path.write_text(header + path.read_text(encoding="utf-8"), encoding="utf-8")
            return True
        except OSError as exc:
            logger.error(f"保存{self.label_kind}失败（{path}）：{exc}")
            return False

    def upsert(self, name: str, action_id=None,
               original_name: str = "", task: Optional[str] = None) -> Tuple[bool, str]:
        """新增或改名一个标签。返回 (是否成功, 给用户看的原因)。

        Args:
            task: 分组模式下的归属任务；None / 空串表示通用组。非分组模式忽略此参数。
        """
        name = str(name or "").strip()
        if not name:
            return False, "标签名不能为空"

        if action_id in ("", "null", None):
            action_id = None
        else:
            try:
                action_id = int(action_id)
            except (TypeError, ValueError):
                return False, f"动作编号必须是整数，收到 {action_id!r}"

        task_key = self._task_key(task)
        items = self.get()
        # 唯一性只在**同一组内**判断：不同任务各有一套自己的编号（都是 1、2、3…），
        # 拿全局唯一去卡，第二个任务就一个编号都填不进去
        scoped = [i for i in items if i.get("task") == task_key]
        original_name = str(original_name or "").strip()
        # 新增时要比对同组全部条目；改名/改编号时把自己排除在外，
        # 否则"把挥手改成挥手"会被自己挡住。
        others = ([i for i in scoped if i["name"] != original_name]
                  if original_name else list(scoped))

        if any(i["name"] == name for i in others):
            return False, f"标签「{name}」已存在"
        if action_id is not None and any(i.get("action_id") == action_id for i in others):
            owner = next(i["name"] for i in others if i.get("action_id") == action_id)
            return False, f"编号 {action_id} 已被「{owner}」占用"

        entry = {"name": name, "action_id": action_id}
        if self.grouped:
            entry["task"] = task_key
        if original_name and not any(i["name"] == original_name for i in scoped):
            return False, f"标签「{original_name}」已不存在，请刷新后重试"

        if original_name:
            items = [entry if (i["name"] == original_name and i.get("task") == task_key)
                     else i for i in items]
            message = f"已更新「{name}」"
        else:
            items.append(entry)
            message = f"已新增「{name}」"

        if not self.save(items):
            return False, "写入标签文件失败，请看日志"
        return True, message

    def remove(self, name: str, task: Optional[str] = None) -> Tuple[bool, str]:
        """删除一个标签（分组模式下只删指定任务里的那一条）。"""
        name = str(name or "").strip()
        task_key = self._task_key(task)
        items = self.get()
        remaining = [i for i in items
                     if not (i["name"] == name and i.get("task") == task_key)]
        if len(remaining) == len(items):
            return False, f"找不到标签「{name}」"

        if not self.save(remaining):
            return False, "写入标签文件失败，请看日志"
        return True, f"已删除「{name}」"

    def get_for_task(self, task: str = "") -> List[dict]:
        """打点窗口要显示的标签：通用组 + 该任务那一组。

        task 为空（本次录制没选采集任务）时**只给通用组** —— 把全部任务的标签都
        摆出来看着方便，但很容易选到不属于本次任务的标签，而这种标错在录制中途
        看不出来（要等分析数据时才发现）。

        分组模式下同一个名字可以出现在不同任务里，所以判断"有没有"一律用
        `(任务, 名字)`，不能只看名字。
        """
        if not self.grouped:
            return self.get()
        wanted = {self.COMMON_TASK}
        name = str(task or "").strip()
        if name:
            wanted.add(name)
        return [item for item in self.get() if item.get("task") in wanted]

    def tasks(self) -> List[str]:
        """已有的任务分组（不含通用组），按首次出现顺序。"""
        if not self.grouped:
            return []
        seen = []
        for item in self.get():
            task = item.get("task")
            if task and task != self.COMMON_TASK and task not in seen:
                seen.append(task)
        return seen

    def _task_key(self, task: Optional[str]):
        """把调用方给的 task 归一到实际的组键。非分组模式恒为 None。"""
        if not self.grouped:
            return None
        name = str(task or "").strip()
        return name or self.COMMON_TASK


class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.app_config = load_yaml(config_path)
        #: 本次运行选的**录制落盘位置**（键：`disk_export` 里的 `local` / `disk`）。
        #: `None` = 还没选过 → 界面按"有没有可用数据盘"自动定默认值。
        #: **只放内存、不写回 app_config** —— 理由见 `get_bag_dir` 的说明。
        self._record_target_key: Optional[str] = None
        #: 会话级的落盘目录覆盖：`None` = 用配置里的 `bag_dir`。
        #: 选「移动硬盘」时是盘上那个目录；选「本机数据盘」时清回 `None`。
        self._record_dir_override: Optional[Path] = None
        self.workspace = Path(self.app_config["workspace_root"]).expanduser()
        self.teleop_config_path = self.workspace / self.app_config["teleop_config_path"]
        # 末端参数（arm_control_config.yaml）：与标定配置一样**按工作空间存放**。
        # 用 `.get` + 默认值：老版本的 app_config.yaml 里没有这一项，不该因此打不开界面。
        self.arm_control_config_path = (
            self.workspace / str(self.app_config.get("arm_control_config_path")
                                 or DEFAULT_ARM_CONTROL_CONFIG))
        # 姿势预设：机器人侧那两份 JSON 跟着工作空间走；上位机自定义的那份在**项目自己的
        # configs/** 下（不随工作空间变 —— 它是"这台机器人的操作习惯"，不是产品交付物）。
        self.arm_motion_file = self.workspace / str(
            self.app_config.get("arm_motion_file") or DEFAULT_ARM_MOTION)
        self.trunk_motion_file = self.workspace / str(
            self.app_config.get("trunk_motion_file") or DEFAULT_TRUNK_MOTION)
        # 采集任务单是配置类里唯一带完整业务逻辑的一组，独立成协作者
        self.action_labels = ActionLabelStore(
            self.app_config, PROJECT_ROOT / "configs" / "action_labels.yaml",
            save_header=ACTION_LABELS_HEADER)
        # 打点标签：与采集任务标签**分两份文件**。两者不是一个层级 ——
        # 采集任务是"这一轮录的是什么活儿"（布草挂架），打点是"这一刻发生了什么"
        # （开始任务 / 双爪夹住第一个角 / …）。混成一份会让录制时那个下拉框里
        # 塞进一堆用不上的条目，而两份清单实际并不重叠。
        self.point_labels = ActionLabelStore(
            self.app_config, PROJECT_ROOT / "configs" / "point_labels.yaml",
            seed_key="point_labels", file_key="point_labels_file",
            label_kind="打点标签", save_header=POINT_LABELS_HEADER,
            grouped=True)      # 按采集任务分组：选了布草挂架就打"找角挂架"那一套

    def set_workspace_root(self, new_root: str) -> None:
        """切换机器人工作空间根目录：更新内存配置并写回 app_config.yaml。

        只重写 workspace_root 这一行，保留文件里其余注释与结构（配置项有大量
        说明注释，整份 dump 会全丢掉）。
        """
        new_root = str(Path(new_root).expanduser())
        text = self.config_path.read_text(encoding="utf-8")
        new_text = re.sub(
            r"^workspace_root:\s*.*$",
            f"workspace_root: {_yaml_scalar(new_root)}",
            text, count=1, flags=re.MULTILINE)
        self.config_path.write_text(new_text, encoding="utf-8")
        self.app_config["workspace_root"] = new_root
        self.workspace = Path(new_root)
        self.teleop_config_path = self.workspace / self.app_config["teleop_config_path"]
        # 末端参数同样跟着换工作空间（忘了这一句的表现是"切了版本后改的还是旧目录那份配置"）
        self.arm_control_config_path = (
            self.workspace / str(self.app_config.get("arm_control_config_path")
                                 or DEFAULT_ARM_CONTROL_CONFIG))
        # 预设同样跟着换（忘了这一句 = 切了版本后点预设走的还是旧版本那套姿势）
        self.arm_motion_file = self.workspace / str(
            self.app_config.get("arm_motion_file") or DEFAULT_ARM_MOTION)
        self.trunk_motion_file = self.workspace / str(
            self.app_config.get("trunk_motion_file") or DEFAULT_TRUNK_MOTION)

    def get_teleop_config(self) -> dict:
        return load_yaml(self.teleop_config_path)

    def save_teleop_config(self, data: dict) -> None:
        """按约定顺序写回 teleop 配置。

        注意：只对已知字段做重排序，其它顶层字段（如 limits）必须原样保留，
        否则标定/切换用户会把这些机器人控制参数整段抹掉。
        """
        top_order = ['teleop', 'calibrations', 'trackers']
        ordered_data = {}
        for key in top_order:
            if key in data:
                ordered_data[key] = data[key]

        if 'calibrations' in ordered_data:
            calib_ordered = {}
            inner_order = ['tracker_index', 'shoulder_width', 'arm_length', 'elbow_length']
            for user, params in ordered_data['calibrations'].items():
                user_ordered = {}
                for k in inner_order:
                    if k in params:
                        user_ordered[k] = params[k]
                for k, v in params.items():
                    if k not in user_ordered:
                        user_ordered[k] = v
                calib_ordered[user] = user_ordered
            ordered_data['calibrations'] = calib_ordered

        # 保留 top_order 之外的其它顶层字段（如 limits），避免丢配置。
        for key, value in data.items():
            if key not in ordered_data:
                ordered_data[key] = value

        save_yaml(self.teleop_config_path, ordered_data)

    def calibration_users(self) -> List[str]:
        """当前工作空间的配置里已标定过的用户名（保持文件里的顺序）。

        读不到就返回空表（见 `load_calibrations`）：这个方法会被"启动遥操作"的
        对话框调用，一份坏配置不该让对话框打不开。
        """
        return list(load_calibrations(self.teleop_config_path))

    def find_calibration_key(self, username: str) -> Optional[str]:
        """按**归一化**名字在当前配置里找人，返回文件里的原始键名。

        返回键名而不是布尔值：写回 `teleop.calibration` 时要用**文件里那个写法**，
        否则机器人侧按 `calibrations[<名字>]` 取值会取不到。
        """
        wanted = normalize_user(username)
        if not wanted:
            return None
        for key in load_calibrations(self.teleop_config_path):
            if normalize_user(key) == wanted:
                return key
        return None

    def get_user_calibration(self, username: str) -> Optional[dict]:
        """这个人的标定参数；没这个人返回 None（名字按 `normalize_user` 比对）。"""
        key = self.find_calibration_key(username)
        if key is None:
            return None
        return load_calibrations(self.teleop_config_path).get(key)

    def import_calibration(self, username: str, source: Path) -> bool:
        """把 source 配置里 username 那条标定复制进**当前工作空间**的配置。

        为什么必须复制而不能只改 `teleop.calibration`：机器人侧的 teleop_node 只读
        它自己那份 `${workspace}/install/config/teleop_config.yaml`，光把名字切过去、
        参数不在文件里，遥操作就会退回默认值（实测日志里是 `Calibration [default]`）。

        已有的键**不覆盖** —— 当前工作空间里那份是权威，别被旧版本目录里的旧参数盖掉。
        """
        entry = load_calibrations(source).get(username)
        if entry is None:
            return False
        try:
            cfg = self.get_teleop_config()
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(f"导入 {username} 的标定失败：当前配置不可读（{exc}）")
            return False
        if not isinstance(cfg, dict):
            logger.warning("导入标定失败：当前配置不是映射结构")
            return False
        calibs = cfg.setdefault("calibrations", {})
        if not isinstance(calibs, dict):
            logger.warning("导入标定失败：calibrations 段不是映射结构")
            return False
        if username in calibs:
            return False
        calibs[username] = entry
        try:
            self.save_teleop_config(cfg)
        except OSError as exc:
            logger.warning(f"导入 {username} 的标定失败：{exc}")
            return False
        logger.info(f"已把 {username} 的标定从 {source} 导入 {self.teleop_config_path}")
        return True

    def teleop_config_candidates(self) -> List[Path]:
        """其它**可能存着标定数据**的遥操作配置，按修改时间从新到旧。

        为什么需要跨目录找：标定数据是**按工作空间**存放的，而「版本下载」把新包解到
        当前工作空间的兄弟目录、默认还会自动切过去（`version_auto_switch`）。升级一次
        版本，新目录里那份是包里带的出厂配置 —— 于是所有人都会变成"没标定过"，
        现场看到的正是"不管这个人是否已经标定过，切用户都弹标定流程"。

        覆盖现场两种目录布局（两种都在 `test_ws/` 这种下载根下）：
          test_ws/<工作空间>/install/config/...           （下载根的直接子目录）
          test_ws/<版本目录>/<工作空间>/install/config/... （新包解出来的布局）
        只扫两层、只认约定的相对路径：这是"找回自己的标定"，不是全局搜索。
        """
        rel = self._teleop_config_rel()
        if rel is None:
            return []                     # 配置里写的是绝对路径：只认那一个文件
        bases = [self.workspace.parent]
        try:
            download_dir = self.get_version_repo()["download_dir"]
        except Exception:                 # noqa: BLE001 (配置坏也要能切用户)
            download_dir = None
        if download_dir is not None and Path(download_dir) not in bases:
            bases.append(Path(download_dir))

        workspaces: List[Path] = []
        for base in bases:
            for child in _safe_subdirs(base):
                workspaces.append(child)
                for grand in _safe_subdirs(child):
                    workspaces.append(grand)

        found: List[Path] = []
        for ws in workspaces:
            if ws == self.workspace:
                continue
            path = ws / rel
            if path.is_file() and path not in found:
                found.append(path)
        found.sort(key=_mtime, reverse=True)
        return found

    def _teleop_config_rel(self) -> Optional[Path]:
        """配置项相对工作空间的路径；写的是绝对路径时返回 None。"""
        try:
            return self.teleop_config_path.relative_to(self.workspace)
        except ValueError:
            return None

    def set_teleop_calibration_user(self, username: str) -> bool:
        cfg = self.get_teleop_config()
        if username not in cfg.get("calibrations", {}):
            return False
        cfg.setdefault("teleop", {})["calibration"] = username
        self.save_teleop_config(cfg)
        return True

    def add_calibration_user(self, username: str, tracker: str, sw: float, al: float, elbow: float = 0.35):
        cfg = self.get_teleop_config()
        cfg.setdefault("calibrations", {})[username] = {
            "tracker_index": tracker,
            "shoulder_width": sw,
            "arm_length": al,
            "elbow_length": elbow
        }
        cfg.setdefault("teleop", {})["calibration"] = username
        self.save_teleop_config(cfg)

    # ---------- 统一脚本目录（scripts/） ----------
    def get_scripts_dir(self) -> Path:
        """统一脚本目录：配置项 scripts_dir，相对路径按项目根目录解析。"""
        raw = self.app_config.get("scripts_dir") or DEFAULT_SCRIPTS_DIR
        path = Path(str(raw)).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def resolve_script_path(self, value: str) -> Path:
        """解析脚本路径，兼容四种配置写法：

        - 绝对路径：原样使用（现场要指定别的副本时走这条）；
        - 项目内相对路径（如 scripts/record_bag.sh）：按项目根目录解析；
        - 裸文件名（如 record_bag.sh）：按统一脚本目录 scripts/ 解析；
        - 工作空间脚本：run_nodes.sh / connect_sequence.py 这类**只在机器侧维护**的，
          直接用工作空间里的那份（根目录或 scripts/ 子目录），上位机不放镜像。

        候选顺序即优先级：**统一脚本目录排在工作空间之前**。所以录制脚本
        （record_bag/vla/cmd.sh）我们自己在 `scripts/` 下维护一份，起录制用的就是它 ——
        机器侧那三个文件不在版本控制里（重刷会退回"忽略 BAG_DIR"的老写法，落盘位置就
        会静默失效）。机器侧那份只作派生来源与副本缺失时的回落。
        `tests/test_record_script_copies.py` 钉住这个顺序，也钉住副本没被改坏。
        """
        if not value:
            raise ValueError("脚本配置为空")
        path = Path(str(value)).expanduser()
        if path.is_absolute():
            return path
        scripts_dir = self.get_scripts_dir()
        for candidate in (PROJECT_ROOT / path, scripts_dir / path):
            if candidate.exists():
                return candidate
        # 机器侧脚本（run_nodes.sh 等）：只在机器人工作空间里，
        # 根目录与 scripts/ 子目录都找一遍。
        for candidate in (self.workspace / path.name,
                          self.workspace / "scripts" / path.name):
            if candidate.exists():
                return candidate
        # 都不存在时按统一约定返回 scripts/ 下的同名脚本，便于报错时给出预期路径。
        return scripts_dir / path.name

    def get_script_path(self, key: str, default: str = "") -> Path:
        value = self.app_config.get(key) or default
        if not value:
            raise KeyError(f"配置项缺失: {key}")
        return self.resolve_script_path(value)

    def get_teleop_script_path(self) -> Path:
        # 旧配置文件可能缺少该键，给出默认值避免 KeyError。
        return self.get_script_path("run_teleop_script", "run_teleop.sh")

    def get_terminal(self) -> str:
        return self.app_config.get("terminal_command", "gnome-terminal")

    def get_nerv_dir(self) -> Path:
        """脚本侧 NERV_DIR：优先配置 nerv_dir，默认取 log_dir 的上一级（/var/nerv）。"""
        raw = self.app_config.get("nerv_dir")
        if raw:
            return Path(str(raw)).expanduser()
        return self.get_log_dir().parent

    def get_script_env(self) -> dict:
        """传给 scripts/ 下脚本的统一环境变量，与各脚本头部的约定一致。"""
        return {
            "ROBOT_WORKSPACE": str(self.workspace),
            "NERV_DIR": str(self.get_nerv_dir()),
            "BAG_DIR": str(self.get_bag_dir()),
            "LOG_DIR": str(self.get_log_dir()),
            "ROBOT_STATUS_FILE": str(self.get_robot_status_file()),
        }

    # ---------- 机器人状态监控 / SDK 服务 ----------
    def get_robot_status_file(self) -> Path:
        """机器人状态 JSON：优先配置，其次 ${nerv_dir}/status/robot_status.json。"""
        raw = self.app_config.get("robot_status_file")
        if raw:
            return Path(str(raw)).expanduser()
        return self.get_nerv_dir() / "status" / "robot_status.json"

    def get_motion_presets_file(self) -> Path:
        """上位机自己维护的姿势预设（`configs/motion_presets.yaml`）。

        **刻意不写回机器人侧**的 `install/product/config/*_motion.json`：那两份是产品交付物
        （示教脚本、手柄、其它版本的界面都读它），现场在上位机里改一个预设不该把它们的语义
        一起改掉，也不该被下一次交付覆盖。上位机的自定义预设另存一份，页面里两者并排显示。
        """
        raw = self.app_config.get("motion_presets_file")
        if raw:
            return Path(str(raw)).expanduser()
        return PROJECT_ROOT / "configs" / "motion_presets.yaml"

    def get_sdk_service_name(self) -> str:
        """SDK 系统服务名（默认 wheel_controller.service，随系统自启）。"""
        return str(self.app_config.get("sdk_service_name") or "wheel_controller.service")

    def get_robot_monitor_status_ttl(self) -> float:
        """状态文件超过该秒数视为监控已停（界面据此判断监控是否还在跑）。"""
        return float(self.app_config.get("robot_monitor_ttl") or 5.0)

    # ---------- 末端手类型（权威来源：工作空间里的 rviz 目录 = hand_plugins.keys()） ----------
    def get_hand_rviz_dir(self) -> Path:
        raw = self.app_config.get("hand_rviz_dir") or "install/share/robot_description/rviz"
        path = Path(str(raw)).expanduser()
        return path if path.is_absolute() else self.workspace / path

    def get_hand_aliases(self) -> dict:
        """历史短名 -> 合法手型名。

        界面上曾经用 hangkai/yinshi 这类短名，但 launch 的 hand 取值只接受
        hand_plugins 的键（hangkai_tg9801/yinshi_rh56d/...），传短名会导致
        rviz 配置与手控制器 yaml 都找不到。这里做一次归一化兜底。
        """
        aliases = {
            "hangkai": "hangkai_tg9801",
            "yinshi": "yinshi_rh56d",
            "yushu": "yushu_dex11",
            "zhixing": "empty",       # SDK 里无 zhixing 的 rviz/控制器配置，退回空手
        }
        aliases.update(self.app_config.get("hand_type_aliases") or {})
        return aliases

    def get_hand_types(self) -> List[str]:
        """扫描 rviz 目录得到合法手型列表（与 launch 的 hand choices 同源）。

        目录不存在时退回内置列表，保证开发机上界面仍能用。
        """
        rviz_dir = self.get_hand_rviz_dir()
        try:
            found = sorted(
                d.name for d in rviz_dir.iterdir()
                if d.is_dir() and (d / "wheel_robot.rviz").is_file()
            )
        except OSError:
            found = []
        if found:
            return found
        logger.warning(f"未找到手型配置目录（{rviz_dir}），使用内置列表")
        return ["empty", "yushu_dex11", "hangkai_tg9801", "yinshi_rh56d"]

    def normalize_hand_type(self, hand_type: str) -> str:
        """把历史短名/空值归一化成合法手型名；无法识别时返回空串。"""
        value = str(hand_type or "").strip()
        if not value:
            return ""
        value = self.get_hand_aliases().get(value, value)
        return value if value in self.get_hand_types() else ""

    # ---------- 采集任务单（录制时选中的任务名，写进 info.json 的 label） ----------
    # 独立文件存储、可增删。定位是"数据采集任务单管理"的雏形：
    # 现在只用于给一段数据打上任务名，后续的任务单（状态、负责人、进度）都以它为主键。
    # ---------- 采集任务单（委托 ActionLabelStore） ----------
    # 保留同名方法是为了不破坏既有调用点与测试；真正的逻辑在 ActionLabelStore 里。
    def get_action_labels_file(self) -> Path:
        return self.action_labels.file_path()

    def get_point_labels_file(self) -> Path:
        return self.point_labels.file_path()

    def get_point_labels(self) -> List[dict]:
        """打点标签**全部**清单（管理界面用）：[{"name", "action_id", "task"}]。"""
        return self.point_labels.get()

    def get_point_label_flow(self, task: str = "") -> List[dict]:
        """打点面板里显示的标签 —— **已合并成一条有顺序的流程**。

        顺序即面板里显示的 1、2、3…；合并规则见 `merge_point_flow`。
        """
        return merge_point_flow(self.point_labels.get_for_task(task),
                                ActionLabelStore.COMMON_TASK)

    def get_point_label_number(self, task: str, name: str) -> Optional[int]:
        """标签在流程里的序号（1 起）—— 写进 points.csv 的「标签编号」就是它。

        必须与面板显示的序号是同一个数：现场按编号对齐动作时，两边不一致就是错的
        （第一版写的是"组内编号"，面板显示 6 而记录里写 2，对不上）。
        还不在库里的标签（手输的新标签）返回 None。
        """
        for position, item in enumerate(self.get_point_label_flow(task), start=1):
            if item["name"] == name:
                return position
        return None

    def get_point_labels_for(self, task: str = "") -> List[dict]:
        """打点窗口要显示的标签：通用组 + 该任务那一组。

        task 为空（本次录制没选采集任务）时只给通用组 —— 见
        `ActionLabelStore.get_for_task` 的说明。
        """
        return self.point_labels.get_for_task(task)

    def get_action_labels(self) -> List[dict]:
        return self.action_labels.get()

    def save_action_labels(self, items: List[dict]) -> bool:
        return self.action_labels.save(items)

    def upsert_action_label(self, name: str, action_id=None,
                            original_name: str = "") -> Tuple[bool, str]:
        return self.action_labels.upsert(name, action_id, original_name)

    def remove_action_label(self, name: str) -> Tuple[bool, str]:
        return self.action_labels.remove(name)

    def _seed_action_labels(self) -> List[dict]:
        """（内部）初始标签清单，保留供测试与旧调用使用。"""
        return self.action_labels.seed()

    # ---------- 界面主题 ----------
    def get_ui_theme(self) -> str:
        """界面主题：light（默认，现场/办公环境）/ dark（暗光环境）。"""
        value = str(self.app_config.get("ui_theme") or "").strip().lower()
        return value if value in ("dark", "light") else "light"

    # ---------- 会话身份（顶栏：机型 / 序列号 / 固件 / 末端 / 远端） ----------
    def get_product_info(self) -> dict:
        """Product.json（机型 + 序列号）。缺失时返回空 dict，界面里显示"—"。"""
        path = self.get_product_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning(f"读取 Product.json 失败（{path}）：{exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def get_product_file(self) -> Path:
        """Product.json 路径。

        权威位置是 `${nerv_dir}/product/Product.json`——机器人侧的
        `install/share/product/product.sh` 读的就是这个文件（手型/腕部/相机序列号都来自它），
        上位机跟着读同一个文件才不会和机器人自己的判断打架。
        其余候选位置只是为了在开发机上也能跑起来。
        """
        raw = self.app_config.get("product_file")
        if raw:
            return Path(str(raw)).expanduser()
        candidates = (self.get_nerv_dir() / "product" / "Product.json",
                      PROJECT_ROOT / "Product.json",
                      self.workspace / "Product.json",
                      self.workspace / "install" / "config" / "Product.json")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def get_product_hand(self) -> str:
        """末端手型：Product.json 的 hand 字段（缺失按 empty，与 product.sh 一致）。"""
        value = str(self.get_product_info().get("hand") or "").strip()
        return "" if value in ("", "null") else value

    def get_product_wrist(self) -> str:
        """腕部组件型号：Product.json 的 wrist 字段（可能为空）。"""
        value = str(self.get_product_info().get("wrist") or "").strip()
        return "" if value in ("", "null") else value

    def is_product_configured(self) -> bool:
        """Product.json 是否已配置：文件存在且 hand 字段非空。

        手型是启动/数采的前提（机器人侧 product.sh 就靠它），所以 hand 空即视为
        未配置。判定结果用于"启动时自动运行产品配置向导 setup_product.sh"。
        """
        return bool(self.get_product_info()) and bool(self.get_product_hand())

    def get_version_file(self) -> Path:
        """版本文件：配置项 version_file > ${workspace}/doc/version.txt > ${workspace}/VERSION。"""
        raw = self.app_config.get("version_file")
        if raw:
            return Path(str(raw)).expanduser()
        doc_file = self.workspace / "doc" / "version.txt"
        return doc_file if doc_file.is_file() else self.workspace / "VERSION"

    def get_version_string(self) -> str:
        """版本号（顶栏「版本」）。

        以机器人包里的 `doc/version.txt` 为准 —— 它是打包时生成的构建说明，
        分支字段里带着 release/vX.Y.Z；取不到才回退到 VERSION 文件首行。
        """
        try:
            text = self.get_version_file().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

        version = parse_version_text(text)
        if version:
            return version
        # doc/version.txt 里没有版本号形态的内容时，按 VERSION 文件首行处理
        first = text.strip().splitlines()
        return first[0].strip() if first else ""

    def get_version_details(self) -> dict:
        """构建元信息（打包人/时间/分支/commit），给顶栏做 tooltip。"""
        try:
            text = (self.workspace / "doc" / "version.txt").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return {}
        return parse_version_details(text)

    # ---------- 路径 / 远程主机配置（带默认值，兼容旧配置文件） ----------
    def get_configured_bag_dir(self) -> Path:
        """**配置里**的数据目录：优先 `bag_dir`；否则相对 `nerv_dir`（默认 /var/nerv）。

        刻意与 `get_bag_dir()` 分开：本方法是"这台机器平时把数据放哪"（配置事实），
        后者是"**这一次**录到哪"（可能被本次会话的选择覆盖）。
        「保存位置」选项里「本机数据盘」显示的必须是**本**方法的值 ——
        否则选过一次移动硬盘之后，那个选项会跟着漂成盘上的路径。
        """
        raw = self.app_config.get("bag_dir")
        if raw:
            return Path(str(raw)).expanduser()
        nerv_dir = self.app_config.get("nerv_dir")
        if nerv_dir:
            return Path(str(nerv_dir)).expanduser() / "bag"
        return DEFAULT_BAG_DIR

    def get_bag_dir(self) -> Path:
        """**有效**的数据/落盘目录 = 本次会话的选择 > `bag_dir` 配置 > 默认。

        录制脚本的 `BAG_DIR`（`get_script_env`）、打点目录、数据列表、回放列表、
        可用空间预检、清单（manifest）**全部**走它 —— 单一出口，接入处不会漏。

        ## 为什么是"会话级覆盖"而不是写回 `app_config.yaml`

        `bag_dir` 描述的是"这台机器的数据目录"；而"这一次录到移动硬盘上"是**一次
        会话的决定**。写回配置有个真陷阱：**盘拔了配置还指着它** → 下一次起录直接
        失败，而且失败点在最不该出问题的地方（连界面都还没起来就该能录了）。
        放在内存里则天然自愈：程序重开就回到配置值，界面再按"有没有可用数据盘"给默认。
        """
        if self._record_dir_override is not None:
            return self._record_dir_override
        return self.get_configured_bag_dir()

    def set_record_target(self, key: str, path: Optional[Path] = None) -> None:
        """记住本次会话选的落盘位置（`key` 见 `disk_export.record_targets`）。

        选「本机数据盘」时把覆盖清回 `None` —— 于是它**不会被上一次的选择黏住**，
        取值回到配置里的 `bag_dir`。

        外接盘的键是 `disk:<挂载点>`（v6.5.1 起可能有多块盘，键必须能区分），
        所以判定"是不是选了外接盘"要看**前缀**而不是 `== "disk"`。
        （前缀写成本地常量而不引 `disk_export.DISK_TARGET`：`disk_export` 已经
        `from .core import logger`，反向再引就是循环导入。）
        """
        self._record_target_key = str(key)
        if str(key).startswith("disk:") and path is not None:
            self._record_dir_override = Path(path)
        else:
            self._record_dir_override = None

    def record_target_key(self) -> Optional[str]:
        """本次会话选过的落盘位置键；`None` = 没选过（界面按检测给默认）。"""
        return self._record_target_key

    def clear_record_target(self) -> None:
        """放弃本次会话的选择，回到"按检测给默认"。"""
        self._record_target_key = None
        self._record_dir_override = None

    def get_log_dir(self) -> Path:
        """日志目录：优先 log_dir；否则相对 nerv_dir（默认 /var/nerv）。"""
        raw = self.app_config.get("log_dir")
        if raw:
            return Path(str(raw)).expanduser()
        nerv_dir = self.app_config.get("nerv_dir")
        if nerv_dir:
            return Path(str(nerv_dir)).expanduser() / "log"
        return DEFAULT_LOG_DIR

    def get_local_log_dir(self) -> Path:
        """上位机本地日志目录：优先 local_log_dir，默认 **项目根/logs**。

        为什么要有这个 getter：这里曾经写成 `Path(__file__).parent.parent / "logs"`，
        而它所在的文件从 `data_collector/ui.py` 挪进了 `data_collector/ui/main_window.py`，
        `parent.parent` 的含义就跟着变了 —— 日志被写进**包目录** `data_collector/logs/`，
        于是"打包时把 logs 删干净"这条规矩总是漏掉一半（实测 301 个日志文件躺在包里被打进交付包）。
        路径解析必须集中在一处（这里），调用方不要再自己拼。

        与 `get_log_dir()` 的区别：那个是**机器人侧**日志（SFTP 抓取源），这个是本机目录。
        """
        raw = self.app_config.get("local_log_dir")
        if raw:
            return Path(str(raw)).expanduser()
        return PROJECT_ROOT / "logs"

    def get_version_repo(self) -> dict:
        """版本下载配置（「版本管理」对话框用）。

        `url` 为空表示该功能未启用 —— 界面据此把下载区置为不可用并说明原因，
        而不是点下去才报"连不上"（有的部署不接这台内网文件服务器）。

        下载目录留空时取 workspace_root 的**父目录**：现场布局一直是
        `test_ws/<版本目录>/wheeled_robot`，新包解到 workspace 的兄弟位置，
        切过去时才不会覆盖正在使用的那个目录。
        """
        raw_url = str(self.app_config.get("version_repo_url") or "").strip()
        raw_dir = str(self.app_config.get("version_download_dir") or "").strip()
        try:
            timeout = float(self.app_config.get("version_repo_timeout", 20))
        except (TypeError, ValueError):
            timeout = 20.0
        return {
            "url": raw_url,
            "download_dir": Path(raw_dir).expanduser() if raw_dir else self.workspace.parent,
            "auto_switch": bool(self.app_config.get("version_auto_switch", True)),
            "keep_archive": bool(self.app_config.get("version_keep_archive", True)),
            "timeout": timeout if timeout > 0 else 20.0,
        }

    def get_ui_scale(self):
        """界面缩放系数 —— **留空 = 自动**（按屏幕宽算，见 `theme.scale_for_screen`）。

        现场换屏（1080P → 4K）或换站位时，可以在这里手填一档而不必重新打包；
        填 0 / 空 / 非法值都退回自动 —— 配置写坏不该让界面起不来。

        返回值原样透传给 `theme.scale_for_screen(width, override=...)`，
        由它做范围钳制与回落，这里只负责"把配置读出来"。
        """
        return self.app_config.get("ui_scale")

    def get_vision_calib_dir(self) -> Path:
        """相机标定结果目录：优先 vision_calib_dir；否则 ${nerv_dir}/vision/calib。

        标定文件由机器人侧 `calibration_node` 写出，上位机只读它 —— 不写、不改名、
        不清理，避免上位机成为标定数据的第二个写入者。
        """
        raw = self.app_config.get("vision_calib_dir")
        if raw:
            return Path(str(raw)).expanduser()
        nerv_dir = self.app_config.get("nerv_dir")
        if nerv_dir:
            return Path(str(nerv_dir)).expanduser() / "vision/calib"
        return DEFAULT_VISION_CALIB_DIR

    def get_ros_node(self, key: str, default: str = "") -> str:
        """ROS 包名/节点名/服务名的配置项读取（节点由机器人侧提供，上位机只拉起）。

        配置项是"可选覆盖"：没写就用默认值，保证老配置文件升级后功能照样可用。
        """
        value = str(self.app_config.get(key) or "").strip()
        return value or default

    # 标定 / 戳点 / 视频流节点的默认值（配置里可覆盖）
    DEFAULT_ROS_NODES = {
        "camera_calib_package": "calibration",
        "camera_calib_node": "calibration_node",
        "camera_calib_service": "/calibration/start_srv",
        "pokepoint_package": "pokepoint",
        "pokepoint_node": "pokepoint_node",
        "video_streamer_package": "teleop_video_streamer",
        "video_streamer_node": "teleop_video_streamer_node",
    }

    def ros_node(self, key: str) -> str:
        """带默认值的 ROS 节点配置读取（见 DEFAULT_ROS_NODES）。"""
        return self.get_ros_node(key, self.DEFAULT_ROS_NODES.get(key, ""))

    def teleop_calibration_timeout(self) -> float:
        """遥操作（VR tracker）标定的等待上限，秒。

        写死值的问题：现场动作慢一点就只能干等到超时，而且没法按需放宽。
        """
        try:
            value = float(self.app_config.get("teleop_calibration_timeout") or 120)
        except (TypeError, ValueError):
            return 120.0
        return value if value > 0 else 120.0

    def vr_bridge_log_path(self) -> Path:
        """vr_bridge 的日志文件路径（`launch_vr_bridge` 的落盘位置）。

        界面要靠它把 bridge 的输出 tail 进日志区 —— 它是常驻后台节点，
        没有终端可看。
        """
        return Path(self.app_config.get("vr_bridge_log_file")
                    or (self.get_log_dir() / "vr_bridge.log"))

    def has_camera_calibration(self) -> bool:
        """是否已有相机标定文件。

        目录不存在或为空都算没有 —— 空目录通常是标定跑了一半失败留下的，
        它同样会让采集到的图像没有可用的外参，不能当"已标定"放行。
        """
        calib_dir = self.get_vision_calib_dir()
        try:
            return calib_dir.is_dir() and any(calib_dir.iterdir())
        except OSError as exc:
            logger.debug(f"读取标定目录失败（按未标定处理）：{calib_dir}：{exc}")
            return False

    def calibration_files(self) -> List[Path]:
        """标定目录下的文件（按名称排序），供界面展示。"""
        calib_dir = self.get_vision_calib_dir()
        try:
            if not calib_dir.is_dir():
                return []
            return sorted((p for p in calib_dir.iterdir() if p.is_file()),
                          key=lambda p: p.name)
        except OSError as exc:
            logger.debug(f"读取标定目录失败：{calib_dir}：{exc}")
            return []

    def get_sudo_password(self) -> str:
        """sudo 密码：只从环境变量或配置文件读取。

        刻意不内置默认密码——把凭据写在源码里等于随仓库一起泄露。
        两处都没配置时返回空串，调用方应跳过 `echo ... | sudo -S`，
        改用已缓存的 sudo 凭据（`sudo -v` 之后 15 分钟内免密）；
        若机器人上 sudo 需要密码，请设置：
            export DATA_COLLECTOR_SUDO_PASSWORD=xxx
        """
        return self._strip_secret(
            os.environ.get("DATA_COLLECTOR_SUDO_PASSWORD")
            or self.app_config.get("sudo_password")
        )

    @staticmethod
    def _strip_secret(value) -> str:
        """空值（含纯空白）一律视为未配置，避免空串被当成密码去认证。"""
        return str(value).strip() if value else ""

    def get_remote_settings(self) -> dict:
        """SFTP 目标配置：密码只从环境变量或配置文件读取，不内置默认值。

        未配置时 password 为空串，`DataUploader` 会在连接前给出可操作的报错。
        """
        return {
            "host": self.app_config.get("remote_host") or DEFAULT_REMOTE_HOST,
            "port": int(self.app_config.get("remote_port") or DEFAULT_REMOTE_PORT),
            "user": self.app_config.get("remote_user") or DEFAULT_REMOTE_USER,
            "password": self._strip_secret(
                os.environ.get("DATA_COLLECTOR_REMOTE_PASSWORD")
                or self.app_config.get("remote_password")
            ),
            "base": self.app_config.get("remote_base") or DEFAULT_REMOTE_BASE,
        }

    def get_upload_settings(self) -> dict:
        """数据上传（打包 + 传输）策略。

        大数据量下最省事、最不容易失败的是“不压缩 + 边打包边直传”：
        MCAP/视频等本身已压缩，再套 DEFLATE 只耗 CPU 不省体积；而先打包到本地
        再上传需要两倍以上临时空间（最常见的大数据上传失败原因就是临时盘写满）。

        - mode: auto | stream | tempfile
          stream   = 边打包边写远端（不占本地临时空间）
          tempfile = 先打包到本地临时目录再上传（旧行为，兼容无 exec 权限的服务器）
          auto     = 优先 stream，远端不支持时自动回退 tempfile
        - format: zip | tar（zip 保留下游解包习惯；tar 对超大/超多文件更省心）
        - compression: store | deflate（store = 不压缩，最快）
        - temp_dir: tempfile 模式的打包目录，留空用系统临时目录
        - temp_margin_mb: tempfile 模式的额外空间余量（默认 200MB），
          避免打包过程中写满盘；临时目录很小（如 tmpfs）时需要调小
        - retries: 失败自动重试次数（用户主动取消不重试）
        - space_check: 上传前后是否检查本地/远端可用空间
        """
        raw_mode = (self.app_config.get("upload_mode") or "auto").strip().lower()
        raw_format = (self.app_config.get("upload_format") or "zip").strip().lower()
        raw_compression = (self.app_config.get("upload_compression") or "store").strip().lower()
        mode = raw_mode if raw_mode in ("auto", "stream", "tempfile") else "auto"
        fmt = raw_format if raw_format in ("zip", "tar") else "zip"
        compression = raw_compression if raw_compression in ("store", "deflate") else "store"
        if mode != raw_mode:
            logger.warning(f"upload_mode 取值无效（{raw_mode}），回退 auto")
        if fmt != raw_format:
            logger.warning(f"upload_format 取值无效（{raw_format}），回退 zip")
        if compression != raw_compression:
            logger.warning(f"upload_compression 取值无效（{raw_compression}），回退 store")

        space_check = self.app_config.get("upload_space_check")
        verify_content = self.app_config.get("verify_content")
        return {
            "mode": mode,
            "format": fmt,
            "compression": compression,
            "temp_dir": self.app_config.get("upload_temp_dir") or "",
            "temp_margin_mb": int(self.app_config.get("upload_temp_margin_mb") or 200),
            "retries": int(self.app_config.get("upload_retries") or 1),
            "space_check": True if space_check is None else bool(space_check),
            "verify_content": False if verify_content is None else bool(verify_content),
        }

    EXPORT_TARGETS = ("server", "disk", "both")

    def get_export_settings(self) -> dict:
        """导出目标（数据往哪儿送）。

        - server: 只送远端服务器（**默认值，保持既有行为不变**）
        - disk:   只写本地挂载的移动硬盘（现场离线场景）
        - both:   两边都写（内网环境兜底）

        为什么单独一段`disk_export` 而不复用 `remote_base`：
        `remote_base` 带**越界校验语义**（`_check_remote_path` 用前缀匹配
        限制"只能写在它下面"）。本地挂载点不套这套规则，两套语义混在一个键上
        会让"哪些路径算合法"变得无法推理。
        """
        raw = (self.app_config.get("export_target") or "server").strip().lower()
        target = raw if raw in self.EXPORT_TARGETS else "server"
        if target != raw:
            logger.warning(f"export_target 取值无效（{raw}），回退 server")

        disk = self.app_config.get("disk_export") or {}
        if not isinstance(disk, dict):
            logger.warning(f"disk_export 应为映射结构，实际是 {type(disk).__name__}，已忽略")
            disk = {}

        # 盘标识只在**确实配了 disk_export 段**时才生效。
        # 不这么做的话：默认 target=server（根本不写硬盘），标识校验却开着，
        # 启动自检会报一个与本次操作无关的"盘不对"。默认值要跟"有没有在用"绑定，
        # 而不是各自独立取默认。
        volume_id_file = str(disk.get("volume_id_file") or ".nerv_volume")
        require_id = disk.get("require_volume_id")
        return {
            "target": target,
            "mount_point": str(disk.get("mount_point") or "").strip(),
            "volume_id_file": volume_id_file,
            # 配了 disk_export 段 → 默认要求标识（防把数据写进错盘）；
            # 完全没配 → 不要求（根本没在用硬盘）。
            "require_volume_id": bool(disk) if require_id is None else bool(require_id),
            "min_free_margin_mb": int(disk.get("min_free_margin_mb") or 2048),
            "warn_file_bytes": int(disk.get("warn_file_bytes") or 3_800_000_000),
        }

    def save_export_settings(self, target: str, mount_point: Optional[str] = None,
                             backup_dir: Optional[Path] = None) -> dict:
        """把导出目标写回 `app_config.yaml`：**备份 → 按行改 → 回读校验**。

        三步都必要（与 `core_operations.save_end_effector_config` 同一套理由）：

        1. **备份**：现场那份配置是手工维护的，改错了要能一键找回。
           备份失败**不拦写入**，但如实报出来 —— 否则现场会以为有备份。
        2. **按行改**（`patch_export_settings_text`）：只动目标行，注释逐字保留。
           那份文件的注释是现场唯一的键说明，整份 `yaml.dump` 会把它们全丢掉。
        3. **回读校验**：写文件没有退出码可看，**唯一可信的判据是重新解析后值相等**。

        Args:
            target: `server` / `disk` / `both`；非法值直接拒绝（不写盘）。
            mount_point: 挂载点；`None` 表示不动它。
            backup_dir: 备份目录；不传用 `<本地上位机日志>/config_backup`。

        Returns:
            dict: ``{"ok": bool, "lines": [...], "errors": [...], "backup": Path|None}``。
            `ok=False` 时**没有写盘**（或写了但回读不符，此时 lines 会点明哪个值）。
        """
        lines: List[str] = []
        errors: List[str] = []
        backup_path: Optional[Path] = None

        if target not in self.EXPORT_TARGETS:
            errors.append(f"导出目标取值非法：{target}（只能是 {'/'.join(self.EXPORT_TARGETS)}）")
            return {"ok": False, "lines": lines, "errors": errors, "backup": None}

        try:
            original = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"读取配置文件失败：{self.config_path}（{exc}）")
            return {"ok": False, "lines": lines, "errors": errors, "backup": None}

        # ---------- ① 备份 ----------
        try:
            folder = backup_dir or (self.get_local_log_dir() / "config_backup")
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = folder / f"app_config_{stamp}.yaml"
            backup_path.write_text(original, encoding="utf-8")
            lines.append(f"已备份原配置：{backup_path}")
        except OSError as exc:
            backup_path = None
            lines.append(f"备份失败（不拦写入，请自行留意）：{exc}")

        # ---------- ② 按行改 ----------
        patched = patch_export_settings_text(original, target, mount_point)
        if not patched["applied"]:
            errors.append("没有找到可改的键，已放弃写入（配置结构与预期不符）")
            return {"ok": False, "lines": lines, "errors": errors, "backup": backup_path}
        for key in patched["applied"]:
            lines.append(f"已写入 {key}")
        new_text = patched["text"]

        # 语法自检：写进坏掉的 YAML 会让**整个界面打不开**，比不改严重得多
        try:
            parsed = yaml.safe_load(new_text)
            if not isinstance(parsed, dict):
                raise ValueError("解析结果不是映射")
        except Exception as exc:
            errors.append(f"改后的内容不是合法 YAML，已放弃写入：{exc}")
            return {"ok": False, "lines": lines, "errors": errors, "backup": backup_path}

        # ---------- ③ 回读校验 ----------
        try:
            self.config_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            errors.append(f"写入配置失败：{self.config_path}（{exc}）")
            return {"ok": False, "lines": lines, "errors": errors, "backup": backup_path}

        try:
            check = load_yaml(self.config_path)
        except Exception as exc:
            errors.append(f"回读配置失败：{exc}")
            return {"ok": False, "lines": lines, "errors": errors, "backup": backup_path}

        problems = []
        if check.get("export_target") != target:
            problems.append(f"export_target 期望 {target}，实际 {check.get('export_target')}")
        if mount_point is not None:
            actual = (check.get("disk_export") or {}).get("mount_point")
            if actual != mount_point:
                problems.append(f"mount_point 期望 {mount_point}，实际 {actual}")
        if problems:
            errors.append("回读校验不通过：" + "；".join(problems))
            return {"ok": False, "lines": lines, "errors": errors, "backup": backup_path}

        # 热更新内存里的配置：否则界面显示的还是旧值，直到重启
        self.app_config["export_target"] = target
        if mount_point is not None:
            self.app_config.setdefault("disk_export", {})
            if isinstance(self.app_config["disk_export"], dict):
                self.app_config["disk_export"]["mount_point"] = mount_point

        lines.append("回读校验通过")
        return {"ok": True, "lines": lines, "errors": errors, "backup": backup_path}

    def check_workspace(self) -> bool:
        return self.workspace.exists()

    # ---------- 启动自检 ----------
    # 把"运行到一半才发现"的问题提前到打开界面时暴露：工作空间、ROS 环境、
    # 脚本可达性、录制目录可写、产品配置、远端凭据。
    _DOCTOR_SCRIPT_KEYS = (
        ("run_nodes_script", "主程序"),
        ("run_teleop_script", "遥操作"),
        ("record_bag_script", "录制 bag"),
        ("record_vla_script", "录制 VLA"),
        ("record_cmd_script", "录制 cmd"),
        ("play_bag_script", "回放"),
        ("bag_rate_stat_script", "频率统计"),
        ("monitor_performance_script", "性能监控"),
        ("robot_monitor_script", "状态监控"),
    )

    # 相机标定 / 戳点走的 ROS 节点（机器人侧提供，上位机只负责拉起）。
    # 键名对应配置里的 <key>_package / <key>_node。
    _DOCTOR_ROS_NODES = (
        ("camera_calib", "相机标定"),
        ("pokepoint", "戳点"),
        ("video_streamer", "视频流"),
    )

    def check_environment(self) -> List[dict]:
        """启动自检。返回 [{name, ok, level, detail}]，level ∈ {"fail", "warn"}。

        只做只读探测、不产生任何副作用：自检本身不能成为新的故障源。
        配置类问题（产品未配置、远端无凭据）标为 warn —— 它们不阻塞启动，
        但会让某个功能在用到时才失败。
        """
        results: List[dict] = []

        def add(name: str, ok: bool, detail: str = "", level: str = "fail") -> None:
            results.append({"name": name, "ok": bool(ok), "level": level,
                            "detail": detail})

        workspace_ok = self.workspace.is_dir()
        add("工作空间", workspace_ok, str(self.workspace))
        if not workspace_ok:
            # 工作空间都不存在，后面的路径检查全部没有意义
            return results

        setup_bash = self.workspace / "install/setup.bash"
        add("ROS 环境 (install/setup.bash)", setup_bash.exists(), str(setup_bash))

        for key, label in self._DOCTOR_SCRIPT_KEYS:
            try:
                path = self.get_script_path(key)
            except Exception as exc:
                add(f"脚本 · {label}", False, str(exc))
                continue
            add(f"脚本 · {label}", path.exists(), str(path))

        bag_dir = self.get_bag_dir()
        writable, detail = False, str(bag_dir)
        try:
            if bag_dir.is_dir():
                writable = os.access(bag_dir, os.W_OK)
            else:
                # 用父目录判断能否创建（不真的建，避免自检产生副作用）
                parent = bag_dir.parent
                writable = parent.is_dir() and os.access(parent, os.W_OK)
                detail = f"{bag_dir}（尚不存在，首次录制时会创建）"
        except OSError as exc:
            detail = f"{bag_dir}：{exc}"
        add("录制目录可写", writable, detail)

        add("产品配置 (Product.json)", self.is_product_configured(),
            "未配置手型，请先运行产品配置向导", level="warn")

        # 相机标定：缺标定文件不影响启动，但采集到的图像没有外参可用 ——
        # 提示由界面决定怎么展示，自检本身只做只读探测并标 warn（不强制）。
        calib_dir = self.get_vision_calib_dir()
        if self.has_camera_calibration():
            add("相机标定文件", True, str(calib_dir))
        else:
            add("相机标定文件", False,
                f"{calib_dir} 下还没有标定文件，建议先做三相机标定（不阻塞启动）",
                level="warn")

        # 标定/戳点节点是否编译安装。标 warn：节点路径布局（install/lib/<pkg>/）
        # 由 colcon 决定，写死成 fail 会在某些部署形态下误伤。
        for node_key, label in self._DOCTOR_ROS_NODES:
            package = self.ros_node(f"{node_key}_package")
            node = self.ros_node(f"{node_key}_node")
            if not package or not node:
                continue
            exe = self.workspace / "install" / "lib" / package / node
            add(f"节点 · {label}", exe.is_file(), str(exe), level="warn")

        password = ""
        try:
            password = (self.get_remote_settings() or {}).get("password") or ""
        except Exception:
            pass
        add("远端 SFTP 凭据", bool(password),
            "未配置密码，数据回传会失败",
            level="warn")

        # 数据转换工具（nerv_lerobot，独立仓、不随交付包走）。标 warn 而不是 fail：
        # 它只影响"录完之后转成训练格式"这一步，缺了完全不影响采集与回传 ——
        # 写成 fail 会让没装它的机器在启动时弹一个"环境自检未通过"的红框。
        target = convert_tool.probe_convert_tool(convert_tool.convert_tool_repo(self))
        add("数据转换工具", target.available,
            target.reason or str(target.repo), level="warn")

        return results

# ============ 进程管理器 ============
class ProcessManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._processes = []
        return cls._instance

    def register(self, proc: subprocess.Popen, name: str = ""):
        self._processes.append((proc, name))
        logger.debug(f"进程注册: {name} PID={proc.pid}")

    def unregister(self, proc: subprocess.Popen):
        self._processes = [(p, n) for p, n in self._processes if p != proc]

    def find_by_name(self, name_pattern: str) -> Optional[subprocess.Popen]:
        """返回首个名字匹配且仍在运行的进程（界面用来判断“是否在运行”）。"""
        for proc, name in self._processes:
            if name_pattern in name and proc.poll() is None:
                return proc
        return None

    def terminate_by_name(self, name_pattern: str):
        for proc, name in self._processes[:]:
            if name_pattern in name:
                logger.info(f"终止进程: {name} PID={proc.pid}")
                terminate(proc)
                self.unregister(proc)

    def prune(self) -> int:
        """回收已退出的子进程（wait 掉僵尸），返回清理数量。

        `register` 只增不删，而 ProcessManager 是单例、长期持有 Popen 引用：
        子进程自行退出后既不会被 GC 也不会被 wait，僵尸会随会话累积。
        周期调用即可，不要指望某个"退出时机"兜底。
        """
        alive = []
        cleaned = 0
        for proc, name in self._processes:
            if proc.poll() is None:
                alive.append((proc, name))
                continue
            try:
                proc.wait(timeout=0)
            except Exception:
                pass
            cleaned += 1
        self._processes = alive
        return cleaned

    def terminate_all(self):
        for proc, name in self._processes[:]:
            terminate(proc)
        self._processes.clear()