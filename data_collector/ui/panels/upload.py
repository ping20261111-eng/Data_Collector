"""上传 / 日志抓取（MainWindow 的一个面板）。

方法自原 MainWindow 原样搬迁，逻辑未改 —— 拆分只为把 3455 行的单文件
拆回可读规模。改动这个面板时只需要打开这一个文件。
"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QLineEdit, QComboBox,
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
from ..common import (describe_export_target, remote_subpath_arg,
                      resolve_upload_subpath)
from ..threads import (UploadThread, FetchLogThread)




class UploadMixin:
    """上传 / 日志抓取。"""

    def show_upload_progress(self, show: bool):
        if show:
            self.progress_label.show()
            self.upload_progress_bar.show()
        else:
            self.progress_label.hide()
            self.upload_progress_bar.hide()

    @guard_slot
    def on_upload(self):
        if hasattr(self, 'upload_in_progress') and self.upload_in_progress:
            self.on_cancel_upload()
            return
        # 传到哪儿由导出目标决定（server/both 才问密码并选远端目录；
        # disk 一条 SSH 都不建）。返回 None=用户取消，直接中止。
        subpath = resolve_upload_subpath(self.uploader, self)
        if subpath is None:
            self.append("已取消上传")
            return
        self.append(f"开始导出数据，目标：{describe_export_target(self.uploader)}")
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("取消上传")
        theme.set_variant(self.btn_upload, "danger")
        self.upload_in_progress = True
        # --- 修改开始：显示进度控件 ---
        self.show_upload_progress(True)
        self.upload_progress_bar.setValue(0)
        self.progress_label.setText("准备打包并上传...")
        # --- 修改结束 ---

        def progress_msg(text):
            self.progress_signal.emit(text)

        self.upload_thread = UploadThread(
            self.uploader,
            lambda **kw: self.uploader.upload_latest_bag(
                remote_subpath_arg(subpath), **kw),
            progress_msg)
        self.upload_thread.progress_signal.connect(self.update_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()

    @guard_slot
    def on_cancel_upload(self):
        """上传开始后任意时间都可以取消，不阻塞 GUI。"""
        if not self.upload_in_progress:
            return

        self.append("正在取消上传...")
        thread = self.upload_thread
        if thread is not None and thread.isRunning():
            thread.cancel()

        # 不 wait/terminate；等待 worker 自己退出并统一走 finished。
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("正在取消...")
        theme.set_variant(self.btn_upload, "warn")
        self.progress_label.setText("正在取消上传，请等待当前数据块结束...")

    @guard_slot
    def on_upload_finished(self, success, msg):
        self.upload_in_progress = False
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("上传数据")
        # 复原成"数据"行的统一描边样式（不是幽灵药丸：数据行现在是普通按钮）
        theme.set_variant(self.btn_upload, "default")
        self.progress_label.setText("上传完成" if success else "上传已取消/失败")
        self.upload_progress_bar.setValue(100 if success else 0)
        # --- 修改开始：隐藏进度控件 ---
        self.show_upload_progress(False)
        # --- 修改结束 ---
        if success:
            QMessageBox.information(self, "上传成功", msg)
            self.append(msg)
        else:
            self.append(f"上传失败: {msg}")
            # 密码错导致的认证失败：清掉本次会话记住的密码，下次操作重新询问
            if "Authentication" in msg or "认证" in msg or "auth" in msg.lower():
                self.uploader.password = ""
                self.append("远端密码可能不正确，已清除；下次上传会重新询问")
            QMessageBox.critical(self, "上传失败", msg)

    @guard_slot
    def on_fetch_log(self):
        self.append("开始抓取日志...")
        self.btn_fetch_log.setEnabled(False)
        self.fetch_thread = FetchLogThread(self.log_fetcher)
        self.fetch_thread.finished.connect(self.on_fetch_log_finished)
        self.fetch_thread.start()

    @guard_slot
    def on_fetch_log_finished(self, success, msg):
        self.btn_fetch_log.setEnabled(True)
        if success:
            QMessageBox.information(self, "抓取成功", msg)
            self.append(msg)
            # 抓完给一个能直接点开的入口：源目录与落盘目录
            self.append_link("打开日志源目录", self.log_fetcher.log_dir)
            self.append_link("打开日志存放目录", self.log_fetcher.fetch_dir)
        else:
            self.show_error("抓取失败", msg)
