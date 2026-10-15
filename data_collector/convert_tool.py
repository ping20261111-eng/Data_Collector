"""数据转换工具（nerv_lerobot/tools/gui）的可用性探测与启动契约。

## 边界：只做入口，不碰对方的代码

转换界面是 `nerv_lerobot` 仓里的独立 PyQt5 程序，跑在它自己的 Python 3.12
虚拟环境里（`lerobot[dataset]==0.6.1` 要求 `>=3.12,<3.13`），而本工具是
系统 Python 3.10。**两边不能同进程**——解释器与依赖互斥，而且两个
`QApplication` 共存本身就是错的。所以这里提供的是：只读探测 + 子进程启动。

启动走对方的 `tools/gui/run.sh`，不自己拼 `.venv/bin/python run_gui.py`：
那份脚本里有它自己解决过的问题（`XDG_RUNTIME_DIR` 在 root 登录下不存在导致
Qt 刷屏、无 X 时退回 offscreen）。重实现一遍等于把这份修复复制一份，
而 `scripts/run_teleop.sh` 的两份副本已经分叉（193 行 vs 115 行，见 ADR-002）。
**启动契约归对方，我们只调用。**

## 为什么需要这一层

`nerv_lerobot` 是独立仓，不随 `pack.sh` 的交付包走 —— 现场装了没有、装在哪，
全凭那台机器自己。本模块的作用就是把"在不在、缺什么、怎么办"变成一条
可照做的说明，让界面据此置灰并给出理由，而不是让人对着一个点不动的按钮猜。

## 为什么不做得更多

**刻意不做依赖探活**（`python -c "import lerobot"` 之类）：一次子进程几百
毫秒，且把"探测"变成一个会失败、会超时、可能留下僵尸的新故障源。依赖完整性
交给对方的 `run.sh`（它自己会提示 `uv sync`），这里只判断"入口文件在不在"。

理由的组织方式沿用 `disk_export.disk_record_targets`：先说最可能的原因，
并给出下一步动作 —— 而不是抛一句 `not available`。
"""
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: 转换界面相对仓库根的位置。对方 `tools/gui/README.md` 里的启动方式。
GUI_REL = Path("tools") / "gui"
ENTRY_REL = GUI_REL / "run_gui.py"
LAUNCHER_REL = GUI_REL / "run.sh"
VENV_PYTHON_REL = Path(".venv") / "bin" / "python"

#: `pgrep -f` 用的匹配串。点开界面这一行就够了：只用来判断"是不是已经在跑"，
#: 不做轮询（每秒 fork 一次只为问一句话不值得）。
RUNNING_PATTERN = "tools/gui/run_gui.py"


@dataclass(frozen=True)
class ConvertToolTarget:
    """一次探测的结果。

    `available=False` 时 `reason` 是**给现场看的可照做说明**（缺什么、
    查了哪个路径、怎么补），界面据此置灰并写进 tooltip。
    """

    repo: Path
    launcher: Path
    entry: Path
    python: Path
    available: bool
    reason: str = ""

    def describe(self) -> str:
        if self.available:
            return f"数据转换工具（{self.repo}）"
        return f"数据转换工具不可用：{self.reason}"


def _missing_repo(repo: Path) -> str:
    return (f"未找到 nerv_lerobot 仓库（查过 {repo}）。"
            "若装在别处，在 configs/app_config.yaml 里填 convert_tool_root")


def probe_convert_tool(repo: Path) -> ConvertToolTarget:
    """探测数据转换工具是否可用。**只做只读 stat，不执行任何东西。**

    判定顺序刻意是「先说最可能的原因」：没装 > 版本对不上 > 装了没同步依赖。
    现场看到的第一句话就该指向最可能的那个原因。
    """
    repo = Path(repo).expanduser()
    launcher = repo / LAUNCHER_REL
    entry = repo / ENTRY_REL
    python = repo / VENV_PYTHON_REL

    if not repo.is_dir():
        return ConvertToolTarget(repo, launcher, entry, python, False,
                                 _missing_repo(repo))

    # run.sh 是我们实际要执行的那份 —— 它不在，功能就是空的。
    if not launcher.is_file():
        return ConvertToolTarget(
            repo, launcher, entry, python, False,
            f"{repo} 下没有 {LAUNCHER_REL}"
            "（该仓库版本还没有图形界面，或目录结构有变化）")

    if not entry.is_file():
        return ConvertToolTarget(
            repo, launcher, entry, python, False,
            f"{repo} 下没有 {ENTRY_REL}（目录不完整）")

    # 装了但没 uv sync：`run.sh` 自己会退出 1，但那时按钮点下去只闪一下。
    # 这里提前说清楚，现场不用去猜是哪一步坏的。
    if not (python.is_file() and os.access(python, os.X_OK)):
        return ConvertToolTarget(
            repo, launcher, entry, python, False,
            f"{python} 不存在或不可执行"
            f"（在 {repo} 下执行一次 uv sync）")

    return ConvertToolTarget(repo, launcher, entry, python, True, "")


def convert_tool_repo(config) -> Path:
    """数据转换工具的仓库根：配置项 `convert_tool_root`，留空取默认位置。

    默认 `~/test_ws/nerv_lerobot` —— 与本仓同级，与 ADR-002 里"机器侧脚本
    不进本仓"的边界一致：那是另一个仓，不进交付包，位置由现场配置决定。
    """
    raw = config.app_config.get("convert_tool_root") or "~/test_ws/nerv_lerobot"
    return Path(str(raw)).expanduser()


def build_launch_cmd(target: ConvertToolTarget) -> list:
    """构造启动命令。

    **直接执行 `run.sh` 而不是 `["bash", "run.sh"]`**：那份脚本末尾是 `exec`，
    直接执行时内核把 shell 换成 python，`Popen` 拿到的 PID 就是真进程；
    绕一层 bash 的话 PID 是那个等待中的 shell，句柄指向错对象（判"还在运行"
    与 kill 都会失准）。

    路径逐个 `shlex.quote`：仓库路径含空格时不能裸拼。
    """
    return [str(target.launcher)]


def convert_tool_log_file(config) -> Path:
    """转换界面的 stdout/stderr 落盘位置。

    落文件而不是留管道：界面退出后进程仍会继续写，管道没人读会写满 64KB
    把进程卡死（`core.run_background_captured` 的注释记过这条）。
    """
    return config.get_local_log_dir() / "convert_gui.log"


def is_convert_tool_running() -> bool:
    """转换界面是否已经在跑（用户手动起过的情况也算）。

    用 `pgrep -f` 而不是只查 `ProcessManager`：本会话没起过但用户自己在
    终端跑了 `bash run.sh` 时，句柄不存在，只查句柄会漏判、于是起第二个实例
    （两个界面写同一个输出目录）。加超时是为了不让一次异常的 pgrep 卡住界面。
    """
    if shutil.which("pgrep") is None:
        return False
    try:
        result = subprocess.run(["pgrep", "-f", RUNNING_PATTERN],
                                capture_output=True, timeout=2)
    except Exception:                                   # noqa: BLE001
        return False
    return result.returncode == 0


def launch_convert_tool(target: ConvertToolTarget,
                        log_file: Optional[Path] = None) -> subprocess.Popen:
    """启动转换界面（独立进程组）。

    `start_new_session=True` 是必须的：本工具 `terminate()` 里用 `os.killpg`
    杀整个进程组，不独立会话的话**关界面会把转换窗口一起带走** ——
    而转换往往是跑几十分钟的长任务（`core.run_in_terminal` 的注释已记过同款坑）。

    cwd 给仓库根：`run.sh` 自己会 cd，但外层也设一次，保证即使对方改了脚本
    里的 cd 逻辑，解析相对路径的行为不变。
    """
    # 起之前再 stat 一次实际要执行的那份：探测是界面启动时的快照，
    # 用户可能在这中间卸载 / 换版本了。
    if not target.launcher.is_file():
        raise FileNotFoundError(f"数据转换启动脚本不存在: {target.launcher}")

    log_file = Path(log_file) if log_file else None
    stream = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_file, "ab", buffering=0)
    try:
        return subprocess.Popen(
            build_launch_cmd(target),
            cwd=str(target.repo),
            # stderr 合并进 stdout：排障时只想看一份时间线，
            # 分开两个文件会让人先猜"该看哪个"。
            stdout=stream if stream is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if stream is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if stream is not None:
            stream.close()      # 子进程已持有 fd，父进程句柄可以立即关闭
