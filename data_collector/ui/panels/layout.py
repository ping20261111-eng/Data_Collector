"""界面骨架：急停柱 / 主列 / 状态条 / 控制台（MainWindow 的一个面板）。

方法自原 MainWindow 原样搬迁，逻辑未改 —— 拆分只为把 3455 行的单文件
拆回可读规模。改动这个面板时只需要打开这一个文件。
"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication)
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
from ... import theme
from ...core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ...core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ...core_data import RecorderManager, DataUploader, LogFetcher
from ...core_data import UploadCancelledError

from ..safety import guard_slot
from ..common import (SP, make_panel_head)
from ..widgets import (LogView)




class LayoutMixin:
    """界面骨架：急停柱 / 主列 / 状态条 / 控制台。"""

    def _build_pillar(self):
        """左侧急停柱：工业控制柜上实体急停按钮的隐喻，也是全界面唯一的破格元素。

        顶部警示斜纹横向冲出柱体，在满屏发丝边框和等宽数字里刻意"压"住主区——
        因为它本来就该是最不该被忽略的那个东西。
        """
        pillar = QFrame()
        pillar.setObjectName("Pillar")
        pillar.setFixedWidth(168)
        self.pillar = pillar

        layout = QVBoxLayout(pillar)
        layout.setContentsMargins(0, 0, 0, SP["sp3"])
        layout.setSpacing(SP["sp3"])

        self.hazard_strip = theme.HazardStrip(self.theme_mode)
        layout.addWidget(self.hazard_strip)

        # 柱体整体留白 + 上下弹性：让"急停 + 状态"这一组垂直居中，
        # 而不是按钮拉到满高、状态文字被挤到柱底（那样状态离按钮太远，读不到一起）。
        layout.addStretch(1)

        self.btn_estop = QPushButton("急\n停")
        self.btn_estop.setObjectName("EstopFace")
        self.btn_estop.setFont(theme.mono_font(theme.TYPE_SCALE["fs_pillar"],
                                               QFont.DemiBold, letter_spacing=3.0))
        # 保持一个"整只手拍得下去"的点击面积：急停按钮不该需要瞄准
        self.btn_estop.setMinimumHeight(200)
        # 覆盖范围必须写在按钮上：手臂没有软件急停，用户不能以为按下去就"全停了"
        self.btn_estop.setToolTip(
            "按下立即制动（不弹确认框）。\n"
            f"同时下发：{'、'.join(f'{n} {t}' for n, t in EMERGENCY_STOP_TARGETS)}\n"
            "注意：手臂没有软件急停接口，需人工确认机械臂状态"
        )
        self.btn_estop.clicked.connect(self.on_emergency_stop)
        layout.addWidget(self.btn_estop)

        self.emergency_label = QLabel("急停 未知")
        self.emergency_label.setObjectName("EstopState")
        self.emergency_label.setAlignment(Qt.AlignCenter)
        self.emergency_label.setWordWrap(True)
        self.emergency_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        self.emergency_label.setToolTip(
            "这里是机器人回读的状态，不是按下按钮的回执。\n"
            "按了急停后以这里变成「已急停」为准。")
        layout.addWidget(self.emergency_label)

        layout.addStretch(1)

        self.btn_estop_release = QPushButton("解除急停")
        self.btn_estop_release.setObjectName("EstopRelease")
        self.btn_estop_release.setMinimumHeight(44)      # 触摸目标 ≥ 44px
        self.btn_estop_release.clicked.connect(self.on_release_emergency_stop)
        layout.addWidget(self.btn_estop_release)
        return pillar

    def _build_main_column(self):
        """右侧主区：操作 / 运行日志，纵向两段。

        2026-09-18 收敛：原来的"生命体征"网格拆掉了 —— 电量与充电合并进顶栏的电池
        图标（充电加闪电），定位/地图由调试平台负责，SDK 服务变成顶栏一行只读状态。
        省下来的整行高度留给日志区，那才是测试与数采时真正一直在看的地方。
        """
        column = QVBoxLayout()
        column.setSpacing(SP["sp3"])
        column.addWidget(self._build_actions())
        column.addWidget(self._build_console(), 1)

        # 上传/压缩进度固定放最底部：它是"这次操作进行到哪了"，不该混进日志流里
        self.progress_label = QLabel("就绪")
        self.progress_label.setObjectName("StatusLine")
        self.progress_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.progress_label.setMinimumHeight(22)
        self.progress_label.hide()
        column.addWidget(self.progress_label)

        self.upload_progress_bar = QProgressBar()
        self.upload_progress_bar.setRange(0, 100)
        self.upload_progress_bar.setValue(0)
        self.upload_progress_bar.setTextVisible(True)
        self.upload_progress_bar.setFormat("%p%")
        self.upload_progress_bar.setMinimumHeight(18)
        self.upload_progress_bar.hide()
        column.addWidget(self.upload_progress_bar)
        return column

    def _build_status_line(self):
        """顶栏第二行：SDK 服务运行状态 + 机器人状态数据是否新鲜。

        这两条都属于"启动前扫一眼"的环境信息。合成一行放顶栏，比单独占一整行
        生命体征网格省地方；过期时这里会明确说明原因，不会默默显示旧读数。
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP["sp4"])

        self.sdk_service_label = QLabel("SDK 服务 —")
        self.sdk_service_label.setObjectName("FactValue")
        self.sdk_service_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        lay.addWidget(self.sdk_service_label)

        # 不换行：这是一行"扫一眼"的说明，换行只会把顶栏撑成两行、挤掉日志区
        self.robot_status_label = QLabel("机器人状态 未获取")
        self.robot_status_label.setObjectName("StatusNote")
        lay.addWidget(self.robot_status_label)

        # ---- 模块是否已上使能（二期）----
        # 为什么是**独立标签**而不是拼进上面那行文字：双臂状态不一致时要能分别上色，
        # 而一个 QLabel 只有一种颜色。现场定的规则：两臂一致就合并成一句，
        # 不一致就分开写 L / R —— 单侧没准备好时，那一侧的 follow 会被静默丢弃。
        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedWidth(1)
        divider.setFixedHeight(16)
        lay.addWidget(divider)

        self.module_label = QLabel("模块")
        self.module_label.setObjectName("FactLabel")
        self.module_label.setFont(
            theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))
        lay.addWidget(self.module_label)

        self.trunk_state_label = QLabel("躯干 —")
        self.trunk_state_label.setObjectName("FactValue")
        self.trunk_state_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        lay.addWidget(self.trunk_state_label)

        self.arm_state_label = QLabel("手臂 —")
        self.arm_state_label.setObjectName("FactValue")
        self.arm_state_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        lay.addWidget(self.arm_state_label)

        lay.addStretch()
        return row

    def _build_console(self):
        """运行日志：等宽控制台。标题行放状态监控开关与清空。"""
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.robot_monitor_label = QLabel("状态监控 未启动")
        self.robot_monitor_label.setObjectName("FactValue")
        self.robot_monitor_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self.btn_robot_monitor = QPushButton("启动状态监控")
        self.btn_robot_monitor.setProperty("variant", "ghost")
        self.btn_robot_monitor.setToolTip("订阅电量/急停/充电并落盘（状态数据的新鲜度来源）；"
                                          "点「启动程序」后会自动拉起，也可在这里手工启停")
        self.btn_robot_monitor.clicked.connect(self.on_toggle_robot_monitor)
        self.btn_clear_log = QPushButton("清空")
        self.btn_clear_log.setProperty("variant", "ghost")
        self.btn_clear_log.clicked.connect(self.on_clear_log)
        # 实时帧率开关（v6.7.5）：默认关闭 —— 不打开就一分资源都不占。
        # 读的是**录制中 bag 的索引计数**，不是订阅话题（理由见 live_rate 模块说明）。
        self.btn_live_rate = QPushButton("显示帧率")
        self.btn_live_rate.setProperty("variant", "ghost")
        self.btn_live_rate.setCheckable(True)
        self.btn_live_rate.setToolTip(
            "在日志区上方显示 7 个数据验证目标话题的实时帧率\n"
            "读的是录制中 bag 的落盘计数 —— 反映「真的录进去了多少帧」，"
            "开销约 2~3 MB 内存、每 2 秒不到 1 ms")
        self.btn_live_rate.clicked.connect(self.on_toggle_live_rate)

        head, _ = make_panel_head("运行日志", [self.robot_monitor_label,
                                               self.btn_live_rate,
                                               self.btn_robot_monitor,
                                               self.btn_clear_log])
        outer.addWidget(head)

        # 帧率读数行：只在开关打开时出现。等宽字体方便逐列比较；
        # 分组最多 11 个（6 相机 + 5 关节），会折成最多 3 行 ——
        # 所以这里**不设固定高度**，让 QLabel 按文本行数自己长。
        self.live_rate_label = QLabel("")
        self.live_rate_label.setObjectName("Hint")
        self.live_rate_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self.live_rate_label.setContentsMargins(theme.px(10), theme.px(2),
                                                theme.px(10), theme.px(2))
        self.live_rate_label.setWordWrap(False)
        self.live_rate_label.hide()
        outer.addWidget(self.live_rate_label)

        self.log = LogView()
        self.log.setObjectName("Console")
        self.log.setReadOnly(True)
        self.log.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        outer.addWidget(self.log, 1)
        return panel

    @guard_slot
    def on_clear_log(self):
        self.log.clear()
