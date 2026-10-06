"""应用入口：读配置 → 建 MainWindow → 进事件循环。

启动时打印自检信息（代码位置 / 界面文件时间），出问题时不用猜
"跑的是哪份副本" —— 这类问题绝大多数是跑到了部署目录或备份目录里的旧代码。
"""
import os
import sys
from datetime import datetime
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from . import __version__
from .core import ConfigManager, logger, PROJECT_ROOT
from .core_operations import read_self_cgroup, runs_under_service
from .ui import MainWindow
from .ui.safety import append_note, install_excepthook, install_signal_handlers

WINDOW_TITLE = "NERV ROBOT ASSISTANT"

# 配置文件固定在项目根的 configs/ 下（run.sh 已 cd 到项目根）
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "app_config.yaml"


def main():
    config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        example = config_path.with_suffix(".example.yaml")
        if example.exists():
            import shutil
            shutil.copy(example, config_path)
            logger.info(f"Created default config from {example}")
        else:
            logger.error(f"Config not found: {config_path}")
            sys.exit(1)

    cfg_mgr = ConfigManager(config_path)

    # ---- 启动自检：把"跑的是哪份代码"直接打到终端 ----
    ui_path = Path(sys.modules[MainWindow.__module__].__file__).resolve()
    try:
        stamp = datetime.fromtimestamp(ui_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        stamp = "读取失败"
    logger.info(f"Data_Collector v{__version__} 启动 | 项目根目录 {PROJECT_ROOT}")
    logger.info(f"界面代码 {ui_path}（修改于 {stamp}）")
    logger.info(f"配置文件 {config_path}")
    logger.info(f"窗口已显示「{WINDOW_TITLE}」：顶栏（机型/序列号/版本/电量）+ 左侧急停柱 + 操作与运行日志")

    app = QApplication(sys.argv)
    # 兜底护栏：未捕获异常留堆栈（logs/ui_error.log），别让界面"静默退出"
    install_excepthook()
    win = MainWindow(cfg_mgr)
    # 外部信号（systemctl stop / pkill / 会话结束）默认会**静默杀掉**进程，
    # 现场表现就是"界面忽然没了、日志一个字没有"。这里让它变成可留证据的干净退出。
    install_signal_handlers(app, get_window=lambda: win)
    _log_session_identity(cfg_mgr)
    win.show()
    sys.exit(app.exec_())


def _log_session_identity(cfg_mgr: ConfigManager) -> None:
    """把"我是谁、被谁拉起、属于哪个 cgroup"写进日志与 logs/ui_error.log。

    为什么值得单独写一段：现场报"停止服务的操作会让界面崩溃、而且没有日志"时，
    需要先分清两种情况 ——
    1. 界面被外部信号（SIGKILL 无法捕获）收走：那这行身份信息就是唯一的线索，
       配 `journalctl` / `dmesg` 的时间点能立刻对上是 OOM 还是服务级联；
    2. 界面自己抛异常：`logs/ui_error.log` 会有堆栈（guard_slot 负责写）。
    这两种混在一起排查会绕远路，所以启动时就把身份钉在文件里。
    """
    cgroup = read_self_cgroup().strip().replace("\n", " | ")
    service = cfg_mgr.get_sdk_service_name()
    logger.info(f"进程 PID={os.getpid()} PPID={os.getppid()} | cgroup: {cgroup or '不可读'}")
    append_note(f"启动 Data_Collector v{__version__} PID={os.getpid()} "
                f"PPID={os.getppid()} cgroup={cgroup or '不可读'}")
    if runs_under_service(service):
        logger.warning(
            f"本界面运行在 {service} 的 cgroup 内：停止该服务会**同时结束本界面**。"
            "停止机器人程序请用界面上的「关闭程序」（只停节点，不碰服务）。")
