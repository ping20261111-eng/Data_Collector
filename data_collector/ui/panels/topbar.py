"""顶栏 / 主题 / 手型展示（MainWindow 的一个面板）。

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
from ..common import (SP)
from ..widgets import (ClickableLabel, BatteryIndicator)




class TopbarMixin:
    """顶栏 / 主题 / 手型展示。"""

    def _build_topbar(self):
        """顶栏：会话身份（机型 / 序列号 / 版本）+ 电量 + 主题开关。

        机型/序列号取自 ${nerv_dir}/product/Product.json（机器人侧 product.sh 读的同一个
        文件），版本取自 ${workspace}/doc/version.txt。这三项是排障时第一眼要看的东西。
        """
        bar = QFrame()
        bar.setObjectName("Topbar")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(SP["sp4"], SP["sp2"], SP["sp4"], SP["sp2"])
        outer.setSpacing(SP["sp1"])

        row = QHBoxLayout()
        row.setSpacing(SP["sp5"])

        brand = QLabel("NERV ROBOT ASSISTANT")
        brand.setObjectName("Brand")
        brand.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"], QFont.DemiBold,
                                      letter_spacing=1.4))
        row.addWidget(brand)

        self.fact_labels = {}
        # 不放"远端"：上传目标只在真正上传时才有意义，常驻显示是噪音
        for key, title in (("model", "机型"), ("sn", "序列号"), ("version", "版本")):
            self.fact_labels[key] = self._make_fact(row, title, clickable=(key == "version"))

        # 版本可点击：切换机器人工作空间（workspace_root）
        self.fact_labels["version"].setCursor(Qt.PointingHandCursor)
        self.fact_labels["version"].clicked.connect(self.on_select_version)

        row.addStretch()

        self.battery = BatteryIndicator()
        self.battery.set_mode(self.theme_mode)
        row.addWidget(self.battery)

        self.btn_theme = QPushButton()
        self.btn_theme.setProperty("variant", "ghost")
        self.btn_theme.clicked.connect(self.on_toggle_theme)
        self._sync_theme_button()
        row.addWidget(self.btn_theme)
        outer.addLayout(row)

        outer.addWidget(self._build_status_line())
        return bar

    def _make_fact(self, parent_layout, title, clickable=False):
        """顶栏的一项：小号等宽标题 + 等宽值。返回值标签便于后续刷新。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP["sp2"])

        dt = QLabel(title)
        dt.setObjectName("FactLabel")
        dt.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))
        value = ClickableLabel("—") if clickable else QLabel("—")
        value.setObjectName("FactValue")
        value.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))

        row.addWidget(dt)
        row.addWidget(value)
        parent_layout.addWidget(holder)
        return value

    def refresh_topbar_facts(self):
        """刷新顶栏：机型 / 序列号（Product.json）、版本（doc/version.txt）。"""
        product = self.config.get_product_info()
        values = {
            "model": product.get("hardware_model") or "—",
            "sn": str(product.get("serial_number") or "—"),
            "version": self.config.get_version_string() or "—",
        }
        for key, label in self.fact_labels.items():
            label.setText(str(values.get(key, "—")))

        # 版本 tooltip 带上打包信息：出问题时不用再登机器人翻 doc/version.txt
        details = self.config.get_version_details()
        tooltip = "\n".join(
            f"{title}：{details[key]}"
            for key, title in (("branch", "分支"), ("packer", "打包人"),
                               ("built_at", "打包时间"), ("commit", "commit"))
            if details.get(key))
        self.fact_labels["version"].setToolTip(
            (tooltip or "未找到 doc/version.txt") + "\n点击切换工作空间（workspace_root）"
        )

    @guard_slot
    def on_select_version(self):
        """点击顶栏「版本」→ 打开「版本管理」。

        入口从"直接弹目录选择器"改成打开对话框：版本这件事的全部含义就是
        "现在用哪个版本 + 去哪拿新版本"，做成两个入口就得让人记住"切目录点这里、
        下版本点那里"。原「选择本地目录…」能力移进对话框首屏，一点没少。

        对话框里切换成功后立刻回调刷新顶栏，不用等关窗。
        """
        from ..release_dialog import ReleaseDialog

        dialog = ReleaseDialog(self.config, self)
        dialog.workspace_changed.connect(self.on_workspace_changed)
        dialog.exec_()
        self.refresh_topbar_facts()
        self.refresh_hand_display()

    @guard_slot
    def on_workspace_changed(self, path: str):
        """工作空间被切走（对话框里切换本地目录 / 下载完自动切换）后的统一刷新。"""
        self.append(f"工作空间已切换为: {path}")
        self.refresh_topbar_facts()
        self.refresh_hand_display()

    def _sync_theme_button(self):
        self.btn_theme.setText("暗色" if self.theme_mode == "light" else "明色")
        self.btn_theme.setToolTip("切换明/暗主题（默认明色，暗光环境可切暗色）")

    @guard_slot
    def on_toggle_theme(self):
        """切主题：只换令牌，不动任何布局与逻辑。"""
        self.theme_mode = "dark" if self.theme_mode == "light" else "light"
        theme.apply_theme(QApplication.instance(), self.theme_mode)
        for widget in (self.hazard_strip, self.battery):
            if widget is not None:
                widget.set_mode(self.theme_mode)
        self._sync_theme_button()
        self.append(f"界面主题已切换为{'暗色' if self.theme_mode == 'dark' else '明色'}")

    def refresh_hand_display(self):
        """手型展示：真机与仿真都只读 Product.json 的 hand 字段，界面不做二次判断。

        机器人的 product.sh 就是从 ${nerv_dir}/product/Product.json 的 hand 字段
        取手型的，上位机跟着读同一个字段，才不会出现"界面显示 A、实际加载 B"。
        """
        hand = self.config.get_product_hand()
        wrist = self.config.get_product_wrist()
        product_file = self.config.get_product_file()

        if hand:
            self.hand_product_label.setText(hand)
            # 腕部型号放 tooltip：这行还要塞 joy/rviz/use_sim/rgb_only，显示不下
            tip = [f"来自 {product_file} 的 hand 字段"]
            if wrist:
                tip.append(f"腕部组件：{wrist}")
            self.hand_product_label.setToolTip("\n".join(tip))
            theme.set_state(self.hand_product_label, "none")
        else:
            self.hand_product_label.setText("未配置")
            self.hand_product_label.setToolTip(
                f"未在 {product_file} 读到 hand 字段；启动时以机器人侧为准")
            theme.set_state(self.hand_product_label, "warn")
