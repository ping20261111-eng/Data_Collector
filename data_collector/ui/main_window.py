"""主窗口本体。

界面构建与交互已按面板拆到 panels/ 下的 Mixin（见 panels/__init__.py）；
这里只保留：类级信号、__init__（装配与接线）、生命周期（关闭回收）、日志与提示输出。
"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication, QShortcut)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygon
from PyQt5.Qt import QDesktopServices
import time
import traceback
import shutil
import zipfile
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from .. import theme
from ..core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ..core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError

from .common import (SP, TELEOP_STARTUP_TIMEOUT_MS, file_link, schedule_once)


from .panels import (TopbarMixin, LayoutMixin, ActionsMixin, StatusMixin,
                    LaunchMixin, TeleopMixin, DataOpsMixin, UploadMixin,
                    VisionMixin)
from .safety import guard_slot


class MainWindow(TopbarMixin, LayoutMixin, ActionsMixin, StatusMixin, LaunchMixin,
                 TeleopMixin, DataOpsMixin, UploadMixin, VisionMixin, QMainWindow):
    """主窗口：装配各面板、持有配置与 Launcher、管理生命周期。"""

    progress_signal = pyqtSignal(str)   # 进度文字更新信号

    log_signal = pyqtSignal(str)        # 日志更新信号

    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        self.config = config_manager
        self.launcher = Launcher(config_manager)
        # 本地日志目录统一由 config 解析（默认 <项目根>/logs）。
        # 千万不要在这里按 __file__ 往上数层级 —— 这个文件从 ui.py 挪进 ui/ 之后，
        # parent.parent 就从"项目根"变成了"包目录"，日志全写进了包里跟着被打包。
        self.log_fetcher = LogFetcher(config_manager.get_local_log_dir(), config_manager)
        # 遥操作状态文件与日志同目录：界面重启后靠它接管/停止后台进程
        self.teleop = TeleopManager(
            config_manager, self.launcher,
            state_path=self.log_fetcher.local_log_dir / "teleop.state.json"
        )
        self.recorder = RecorderManager(config_manager, self.launcher)
        self.uploader = DataUploader(config_manager)
        self.sdk_service = SdkServiceManager(config_manager)
        self.calib_thread = None
        # 本次录制的打点会话（core_points.PointRecorder）；录制开始时建立、
        # 停止或中断时收尾。None 表示本会话没有进行中的录制
        self.point_recorder = None
        # 打点面板（非模态，录制开始时自动弹出）。它是个独立的顶层窗口，
        # 不销毁的话主窗口关掉后进程不会退出（Qt 的 lastWindowClosed 不成立）
        self.point_panel = None

        # 相机标定：下发采集指令的后台线程（可取消）+ 节点日志 tail + 节点进程
        self.vision_thread = None
        self.vision_tail_thread = None
        self.vision_node_proc = None
        self.vision_calib_started = False     # 指令已下发：用于拦截重复点击
        # 关闭主程序（含残留节点清理）的后台任务
        self._stop_main_thread = None
        self.upload_thread = None
        self.upload_in_progress = False

        # 遥操作运行状态（后台进程 + 日志回显 + 停止）
        self.teleop_thread = None
        self.teleop_proc = None
        self.teleop_running = False
        self.teleop_runs = 0                # 遥操作启动过几次（姿势控制页据此提示模式）
        self.teleop_stopped_by_user = False
        self.teleop_seconds = 0
        self.teleop_startup_timeout_ms = TELEOP_STARTUP_TIMEOUT_MS
        self.teleop_timer = QTimer()
        self.teleop_timer.timeout.connect(self.update_teleop_time)
        self.teleop_startup_timer = QTimer()
        self.teleop_startup_timer.setSingleShot(True)
        self.teleop_startup_timer.timeout.connect(self.on_teleop_startup_timeout)

        # 录制计时器
        self.record_timer = QTimer()
        self.record_timer.timeout.connect(self.update_record_time)
        self.record_seconds = 0
        self.record_start_time = None
        self._record_health_warned = False   # 录制异常只报一次，避免每秒刷屏

        # 机器人状态 / SDK 服务刷新
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.refresh_robot_status)
        self.sdk_service_running = False
        self.robot_monitor_proc = None
        self.estop_thread = None
        self.last_estop_state = None
        self._low_battery_warned = False
        self._status_stale_warned = False
        self._source_lost_warned = False      # 数据源断开只提示一次（见 status 面板）
        self.gripper_action_in_flight = False  # 夹爪动作进行中（夹爪页面置位）
        self._gripper_dialog = None            # 「夹爪控制」独立页面（单例）
        self.end_effector_action_in_flight = False  # 写末端参数进行中（末端参数页面置位）
        self._end_effector_dialog = None            # 「末端参数」独立页面（单例）
        self.posture_action_in_flight = False  # 姿势动作进行中（姿势控制页面置位）
        self._posture_dialog = None                  # 「姿势控制」独立页面（单例）
        # 躯干自锁的**记录**（None = 本次运行没下发过 / True = 已下发自锁 / False = 已下发解除）。
        # 放在主窗口上是刻意的：它有两个入口（工具行「机器人控制 ▾」那一项、姿势控制页里
        # 躯干那一行的按钮），两处必须读同一个值 —— 各记一份就会互相矛盾。
        # 语义是「下发过什么」，不是「现在是不是自锁」：ROS 侧没有自锁状态话题。
        self.trunk_lock_sent = None
        self._trunk_lock_thread = None               # 自锁下发的后台线程（见 status.py）
        # 头部至 -32°（采数）的后台线程，与自锁同一条理由：引用自己留着，
        # `LocalTaskThread` 刻意不接收 parent（见 status.py / threads.py 的说明）。
        self._head_stream_thread = None

        # 连接信号
        self.progress_signal.connect(self.update_progress_label)
        self.log_signal.connect(self.append_safe)

        self.setWindowTitle("NERV ROBOT ASSISTANT")
        self.setGeometry(100, 100, 940, 860)
        # 最小宽度按"参数行 + 急停柱"的实际下限定：再窄下去布局只能裁字，
        # 所以这里拦住，而不是让用户缩到一个读不出参数的大小。
        self.setMinimumSize(860, 660)

        #: F11 切换全屏（与「机器人控制 ▾」→「全屏显示」共用同一处实现）。
        #: 快捷键挂在窗口上，默认是 `Qt.WindowShortcut` 作用域 —— 子对话框拿到焦点时
        #: 不会误触发；反过来在全屏里按 F11 也一定退得出来，不必先找到标题栏。
        self._fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fullscreen_shortcut.activated.connect(self.on_toggle_fullscreen)

        # 主题：设计稿的令牌一次性铺到整个应用（各对话框一并生效，不用逐个设置）
        self.theme_mode = self.config.get_ui_theme()
        theme.apply_theme(QApplication.instance(), self.theme_mode)
        self.hazard_strip = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(SP["sp3"], SP["sp3"], SP["sp3"], SP["sp3"])
        root.setSpacing(SP["sp3"])

        # ---------- 顶栏：会话身份（机型 / 序列号 / 固件 / 末端 / 远端） ----------
        root.addWidget(self._build_topbar())

        # ---------- 主体：左侧急停柱（记忆点）+ 右侧主区三段 ----------
        body = QHBoxLayout()
        body.setSpacing(SP["sp3"])
        body.addWidget(self._build_pillar())
        body.addLayout(self._build_main_column(), 1)
        root.addLayout(body, 1)

        # ---------------- 构建完成后的初始化 ----------------
        self.refresh_topbar_facts()
        self.refresh_hand_display()

        # 状态先刷一次，并开始周期刷新（服务状态 / 电量 / 急停 / 定位 / 地图）
        self.refresh_robot_status()
        self.status_timer.start(1000)

        # 界面可能是在遥操作运行中被关掉又打开的：接管残留的后台进程
        self._adopt_running_teleop()

        # 产品未配置时，进入事件循环后提示运行 setup_product.sh
        # （延迟到启动后，避免阻塞初始化，也便于测试主动触发）
        #
        # 用 schedule_once 而不是 QTimer.singleShot：定时器要挂在窗口上，
        # 随窗口销毁一起取消（理由见 common.schedule_once 的说明）。
        schedule_once(self, 0, self._maybe_setup_product)
        # 启动自检稍后进行：避开首帧渲染，也让"产品未配置"的提示先出现
        schedule_once(self, 1500, self._run_startup_doctor)

    def append_safe(self, text):
        """在主线程中追加日志"""
        self.log.append(text)

    def append(self, text):
        """外部调用日志接口（发送信号到主线程）"""
        self.log_signal.emit(text)

    def append_link(self, text, path):
        """在日志区追加一条可点击的快捷入口，点了用系统默认程序打开该目录/文件。"""
        target = Path(path)
        label = f"{text}：{target}"
        try:
            exists = target.exists()
        except OSError:
            exists = False
        if not exists:
            # 目录还不存在时给纯文本，避免点开一个打不开的链接
            self.append(f"{label}（暂不存在）")
            return
        self.log.append(file_link(label, target))

    @guard_slot
    def update_progress_label(self, text):
        """更新进度标签（主线程）"""
        self.progress_label.setText(text)

    @guard_slot
    def update_upload_progress(self, value):
        """更新压缩/上传总进度（主线程）。压缩 0~50%，上传 50~100%。"""
        self.upload_progress_bar.setValue(max(0, min(100, value)))

    def show_error(self, title, msg):
        logger.error(msg)
        QMessageBox.critical(self, title, msg)

    @guard_slot
    def on_toggle_fullscreen(self):
        """全屏 / 退出全屏（F11 与「机器人控制 ▾」里那一项共用）。

        全屏不只是"看着大"：它省掉标题栏与窗口边框（约 40px 高），而姿势控制页在
        1080P + 1.33 缩放下正卡在这个高度上 —— 见 `PostureDialog._shrink_for_screen`。

        **这个状态可回读**（`isFullScreen()`），所以菜单项文案据实显示，不像自锁那样
        只能声明"本页下发过什么"。
        """
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        logger.info("窗口已切到全屏" if self.isFullScreen() else "窗口已退出全屏")

    def _shutdown_threads(self, msec: int = 2000) -> list:
        """关闭前回收本界面的定时器与工作线程，返回未能按时停止的对象名。

        漏掉任何一个，都可能在退出时销毁仍在运行的 QThread —— Qt 会直接 abort
        整个进程（上传大文件时关窗最容易命中）。集中处理，避免以后靠记忆逐个补。
        """
        for timer in (getattr(self, "status_timer", None),
                      getattr(self, "teleop_timer", None),
                      getattr(self, "teleop_startup_timer", None),
                      getattr(self, "record_timer", None)):
            if timer is not None:
                timer.stop()

        workers = (
            ("遥操作日志", "teleop_thread", "stop"),
            ("标定", "calib_thread", "cancel"),
            ("上传", "upload_thread", "cancel"),
            ("数据抓取", "fetch_thread", "cancel"),
            ("相机标定", "vision_thread", "cancel"),
            ("标定日志", "vision_tail_thread", "stop"),
            ("关闭主程序", "_stop_main_thread", None),   # 收尾动作不打断，等它跑完
            ("急停回执", "estop_thread", None),   # ProcessExitThread 自带超时，等它自然结束
        )
        pending = []
        for label, attr, stopper in workers:
            thread = getattr(self, attr, None)
            if thread is None or not thread.isRunning():
                continue
            if stopper:
                try:
                    getattr(thread, stopper)()
                except Exception as exc:
                    logger.warning(f"停止{label}线程失败：{exc}")
            if not thread.wait(msec):
                pending.append(label)
        return pending

    def closeEvent(self, event):
        """关闭界面：回收线程与定时器 → 问一次遥操作 → 收掉自己拉起的辅助节点。

        退出约束（现场要求）：**上位机退出后，机器人上不能留下我们拉起的节点**。
        遥操作是唯一例外 —— 它有状态文件、支持界面重启后接管，所以问一句而不是直接杀。
        机器人主程序不在清理范围内：关个界面不该把整台机器人停了。
        """
        # 打点面板必须先收掉：它是独立顶层窗口，留着会让"关掉主窗口 = 退出程序"
        # 这个动作失效（进程挂在面板上）；顺带把没写进录制目录的打点落成文件，
        # 而不是随进程退出静默丢掉（与 _finish_points 的既有语义一致）
        self._finish_points()

        pending = self._shutdown_threads()

        if self._robot_monitor_running():
            self.launcher.stop_robot_monitor()
            self.robot_monitor_proc = None

        if pending:
            # 等不到就不假装没事：让用户自己决定，而不是留一个"运行中被销毁"的线程
            answer = QMessageBox.question(
                self, "仍有任务未结束",
                "以下后台任务在等待时间内没有结束：\n    " + "、".join(pending) +
                "\n\n强行关闭可能中断正在进行的操作（例如上传到一半）。确定要关闭吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return

        if self.teleop_running:
            # 遥操作是唯一刻意支持"界面重启后接管"的东西（状态文件 + 停止按钮），
            # 所以退出前问一句而不是直接杀；默认按钮是"停止"，因为现场反馈的原话是
            # "确保我们的程序退出之后不要有残留节点"。
            answer = QMessageBox.question(
                self, "遥操作仍在运行",
                "遥操作进程仍在后台运行。\n\n"
                "· 是：一并结束遥操作（机器人上不留残留节点）\n"
                "· 否：保留运行，下次打开界面可继续监控或停止\n\n"
                "是否停止遥操作？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self.teleop.stop_teleop()
                self.teleop_running = False
                logger.info("界面关闭：已停止遥操作")
            else:
                logger.warning("界面关闭，遥操作保留运行（重新打开界面即可接管）")

        # 退出兜底：本会话由上位机拉起的辅助节点（相机标定 / 戳点 / 视频流 / VR 桥）
        # 必须收干净。实测踩过：做完标定没点「停止相机标定」就关界面，机器人上仍然
        # 挂着 /ascamera/* /vision/* 等话题 —— 下次启动看到的是一半旧一半新的节点表。
        for line in self.launcher.stop_aux_nodes():
            logger.info(line)
        self.vision_node_proc = None
        super().closeEvent(event)
