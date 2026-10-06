"""远端 SFTP 目录浏览器（原 ui.py 中最大的一类）。"""
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
from .. import theme
from ..core import (ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS,
                    human_size)
from ..core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError


from .threads import (RemoteTaskThread)
from .common import schedule_once



class RemoteDirectoryDialog(QDialog):
    """完整 SFTP 远程文件管理器。

    功能：
    - 浏览/返回/刷新
    - 当前目录搜索、递归搜索
    - 新建目录
    - 单选/多选删除
    - 多文件上传
    - 文件下载及进度
    - 目录大小统计
    - 远程磁盘空间
    - 右键菜单
    - ZIP 基本信息
    - 选择当前目录作为机器人数据上传目标
    """
    def __init__(self, uploader, parent=None, select_mode=False):
        super().__init__(parent)
        self.uploader = uploader
        self.select_mode = select_mode
        self.base_path = uploader._normalize_remote_path(
            uploader.remote_base
        )
        self.current_path = self.base_path
        self.selected_path = None
        self.remote_entries = []
        self.task_thread = None
        self.upload_queue = []
        self.upload_queue_index = 0

        self.resize(1050, 720)
        self.setWindowTitle(
            "选择远程目录" if select_mode else "远程文件管理器"
        )

        layout = QVBoxLayout(self)

        # 路径工具栏
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("当前路径："))
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        path_row.addWidget(self.path_edit, 1)

        self.btn_root = QPushButton("根目录")
        self.btn_up = QPushButton("上一级")
        self.btn_refresh = QPushButton("刷新")
        self.btn_root.clicked.connect(self.go_root)
        self.btn_up.clicked.connect(self.go_up)
        self.btn_refresh.clicked.connect(self.refresh)
        path_row.addWidget(self.btn_root)
        path_row.addWidget(self.btn_up)
        path_row.addWidget(self.btn_refresh)
        layout.addLayout(path_row)

        # 搜索
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索："))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索当前目录；点击“递归搜索”搜索所有子目录"
        )
        self.search_edit.textChanged.connect(self.apply_filter)
        self.search_edit.returnPressed.connect(self.recursive_search)
        search_row.addWidget(self.search_edit, 1)

        self.btn_recursive_search = QPushButton("递归搜索")
        self.btn_recursive_search.clicked.connect(self.recursive_search)
        search_row.addWidget(self.btn_recursive_search)
        layout.addLayout(search_row)

        # 文件列表
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.ExtendedSelection
        )
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(
            self.show_context_menu
        )
        self.list_widget.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.list_widget, 1)

        # 任务进度
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        progress_row.addWidget(self.progress_bar, 1)

        self.btn_cancel_task = QPushButton("取消任务")
        self.btn_cancel_task.clicked.connect(self.cancel_task)
        self.btn_cancel_task.hide()
        progress_row.addWidget(self.btn_cancel_task)
        layout.addLayout(progress_row)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Hint")
        self.status_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        layout.addWidget(self.status_label)

        self.space_label = QLabel("远程空间：读取中...")
        self.space_label.setObjectName("Hint")
        self.space_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        layout.addWidget(self.space_label)

        # 操作按钮
        btn_row = QHBoxLayout()
        self.btn_new_folder = QPushButton("新建目录")
        self.btn_upload_file = QPushButton("上传文件")
        self.btn_download = QPushButton("下载")
        self.btn_delete = QPushButton("删除")
        self.btn_size = QPushButton("统计目录大小")
        self.btn_info = QPushButton("详细信息")

        self.btn_new_folder.clicked.connect(self.create_folder)
        self.btn_upload_file.clicked.connect(self.upload_file)
        self.btn_download.clicked.connect(self.download_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_size.clicked.connect(self.calculate_selected_size)
        self.btn_info.clicked.connect(self.show_file_info)

        for b in (
            self.btn_new_folder,
            self.btn_upload_file,
            self.btn_download,
            self.btn_delete,
            self.btn_size,
            self.btn_info,
        ):
            btn_row.addWidget(b)

        btn_row.addStretch()

        if self.select_mode:
            self.btn_select = QPushButton("选择当前目录")
            # 该对话框的主操作：走高亮层级，而不是自己写一套绿色
            self.btn_select.setProperty("variant", "primary")
            self.btn_select.clicked.connect(self.select_current)
            btn_row.addWidget(self.btn_select)

        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        self.refresh()
        schedule_once(self, 100, self.refresh_space)

    @staticmethod
    def human_size(size):
        """体积格式化：统一走 core.human_size（本项目唯一实现）。

        保留这个 staticmethod 只为不破坏既有调用点与测试；逻辑不再在这里重复。
        """
        return human_size(size)

    def _relative_path(self, remote_path):
        remote_path = self.uploader._normalize_remote_path(remote_path)
        if remote_path == self.base_path:
            return ""
        return remote_path[len(self.base_path):].lstrip("/")

    def refresh(self):
        try:
            self.path_edit.setText(self.current_path)
            self.remote_entries = self.uploader.list_remote_dir(
                self.current_path
            )
            self.apply_filter()
            self.status_label.setText(
                f"当前目录共 {len(self.remote_entries)} 项"
            )
            self.refresh_space()
        except Exception as e:
            self.status_label.setText("读取失败")
            QMessageBox.critical(self, "远程目录读取失败", str(e))

    def apply_filter(self):
        keyword = self.search_edit.text().strip().lower()
        self.list_widget.clear()
        for entry in self.remote_entries:
            if keyword and keyword not in entry["name"].lower():
                continue
            icon = "📁" if entry["is_dir"] else "📄"
            size = (
                ""
                if entry["is_dir"]
                else f"    ({self.human_size(entry['size'])})"
            )
            item = QListWidgetItem(
                f"{icon} {entry['name']}{size}"
            )
            item.setData(Qt.UserRole, entry)
            self.list_widget.addItem(item)

    def refresh_space(self):
        try:
            info = self.uploader.get_remote_disk_usage(self.current_path)
            self.space_label.setText(
                f"远程空间：已用 {self.human_size(info['used'])} / "
                f"{self.human_size(info['total'])}    "
                f"可用 {self.human_size(info['free'])}"
            )
        except Exception as e:
            # 磁盘空间不是目录浏览的必要功能。
            # 某些 SFTP 服务端不支持 statvfs 时，只显示不可用，
            # 不影响目录浏览、上传、下载、删除等核心功能。
            self.space_label.setText(
                f"远程空间：暂不可用（{type(e).__name__}）"
            )

    def go_root(self):
        self.current_path = self.base_path
        self.search_edit.clear()
        self.refresh()

    def go_up(self):
        if self.current_path == self.base_path:
            return
        parent = self.uploader._normalize_remote_path(
            os.path.dirname(self.current_path)
        )
        if not (
            parent == self.base_path
            or parent.startswith(self.base_path + "/")
        ):
            parent = self.base_path
        self.current_path = parent
        self.search_edit.clear()
        self.refresh()

    def on_double_click(self, item):
        entry = item.data(Qt.UserRole)
        if not entry:
            return

        if entry["is_dir"]:
            self.current_path = entry["path"]
            self.search_edit.clear()
            self.refresh()
            return

        # ZIP 双击查看信息，其它文件双击下载。
        if entry["name"].lower().endswith(".zip"):
            self._start_task(
                "zip_info",
                (entry["path"],),
                on_done=self._zip_info_done
            )
        else:
            self.download_selected()

    def create_folder(self):
        name, ok = QInputDialog.getText(
            self, "新建目录", "请输入目录名称："
        )
        if not ok:
            return

        name = name.strip().strip("/")
        if (
            not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
        ):
            QMessageBox.warning(
                self, "输入错误",
                "目录名称不能为空，且不能包含 /、\\、. 或 .."
            )
            return

        new_path = self.uploader._normalize_remote_path(
            self.current_path + "/" + name
        )
        try:
            self.uploader.create_remote_dir(new_path)
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "创建失败", str(e))

    def _selected_entries(self):
        return [
            item.data(Qt.UserRole)
            for item in self.list_widget.selectedItems()
            if item.data(Qt.UserRole)
        ]

    def delete_selected(self):
        entries = self._selected_entries()
        if not entries:
            QMessageBox.warning(
                self, "提示", "请先选择要删除的文件或目录"
            )
            return

        names = "\n".join(
            f"{i + 1}. {e['path']}" for i, e in enumerate(entries)
        )
        if QMessageBox.question(
            self,
            "确认删除",
            f"确定删除以下 {len(entries)} 项吗？\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        failed = []
        for entry in entries:
            try:
                self.uploader.delete_remote_path(entry["path"])
            except Exception as e:
                failed.append(f"{entry['path']}: {e}")

        self.refresh()

        if failed:
            QMessageBox.warning(
                self,
                "删除完成但有失败",
                "\n".join(failed),
            )
        else:
            QMessageBox.information(
                self, "删除完成", f"已删除 {len(entries)} 项"
            )

    def download_selected(self):
        entries = self._selected_entries()
        if len(entries) != 1 or entries[0]["is_dir"]:
            QMessageBox.warning(
                self, "提示", "请选择一个文件进行下载"
            )
            return

        entry = entries[0]
        local_path, ok = QFileDialog.getSaveFileName(
            self,
            "保存远程文件",
            str(Path.home() / entry["name"]),
        )
        if not ok or not local_path:
            return

        self._start_task(
            "download",
            (entry["path"], local_path),
        )

    def upload_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件"
        )
        if not files:
            return

        self.upload_queue = [Path(x) for x in files]
        self.upload_queue_index = 0
        self._upload_next_file()

    def _upload_next_file(self):
        if self.upload_queue_index >= len(self.upload_queue):
            self.refresh()
            QMessageBox.information(
                self, "上传完成",
                f"已处理 {len(self.upload_queue)} 个文件"
            )
            self.upload_queue = []
            return

        local = self.upload_queue[self.upload_queue_index]
        remote = self.uploader._normalize_remote_path(
            self.current_path + "/" + local.name
        )
        self.status_label.setText(
            f"正在上传：{local.name} "
            f"({self.upload_queue_index + 1}/{len(self.upload_queue)})"
        )

        self._start_task(
            "upload_file",
            (str(local), remote),
            on_done=self._upload_file_done,
        )

    def _upload_file_done(self, success, result, msg):
        current = (
            self.upload_queue[self.upload_queue_index]
            if self.upload_queue_index < len(self.upload_queue)
            else None
        )

        if not success:
            # 用 uploader 的取消标志判断，而不是匹配提示文案：文案一旦调整（或网络
            # 错误信息里恰好含"取消"二字），控制流就会静默走错，把整个上传队列清空。
            if getattr(self.uploader, "upload_cancelled", False):
                self.upload_queue = []
                self.status_label.setText("上传已取消")
                return
            if current:
                QMessageBox.warning(
                    self, "上传失败",
                    f"{current.name}: {msg}"
                )

        self.upload_queue_index += 1
        self._upload_next_file()

    def calculate_selected_size(self):
        entries = self._selected_entries()
        if len(entries) != 1 or not entries[0]["is_dir"]:
            QMessageBox.warning(
                self, "提示", "请选择一个目录"
            )
            return

        self._start_task(
            "dir_size",
            (entries[0]["path"],),
            on_done=self._size_done,
        )

    def _size_done(self, success, result, msg):
        if success:
            QMessageBox.information(
                self,
                "目录大小",
                f"统计完成\n\n总大小：{self.human_size(result)}",
            )
        else:
            QMessageBox.warning(self, "统计失败", msg)

    def recursive_search(self):
        keyword = self.search_edit.text().strip()
        if not keyword:
            QMessageBox.warning(
                self, "提示", "请输入搜索关键字"
            )
            return

        self._start_task(
            "search",
            (self.current_path, keyword),
            on_done=self._search_done,
        )

    def _search_done(self, success, result, msg):
        if not success:
            QMessageBox.warning(self, "搜索失败", msg)
            return

        self.remote_entries = result or []
        self.list_widget.clear()

        for entry in self.remote_entries:
            icon = "📁" if entry["is_dir"] else "📄"
            size = (
                ""
                if entry["is_dir"]
                else f"    ({self.human_size(entry['size'])})"
            )
            item = QListWidgetItem(
                f"{icon} {entry['name']}{size}\n"
                f"    {entry['path']}"
            )
            item.setData(Qt.UserRole, entry)
            self.list_widget.addItem(item)

        self.status_label.setText(msg)

    def show_file_info(self):
        entries = self._selected_entries()
        if len(entries) != 1:
            QMessageBox.warning(
                self, "提示", "请选择一个文件或目录"
            )
            return

        entry = entries[0]
        if (
            not entry["is_dir"]
            and entry["name"].lower().endswith(".zip")
        ):
            self._start_task(
                "zip_info",
                (entry["path"],),
                on_done=self._zip_info_done,
            )
            return

        try:
            info = self.uploader.get_remote_file_info(entry["path"])
            mtime = (
                datetime.fromtimestamp(info["mtime"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if info["mtime"]
                else "-"
            )
            QMessageBox.information(
                self,
                "远程详细信息",
                "\n".join([
                    f"名称：{info['name']}",
                    f"路径：{info['path']}",
                    f"类型：{'目录' if info['is_dir'] else '文件'}",
                    f"大小：{self.human_size(info['size'])}",
                    f"修改时间：{mtime}",
                ]),
            )
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))

    def _zip_info_done(self, success, result, msg):
        if not success:
            QMessageBox.warning(self, "ZIP 信息读取失败", msg)
            return

        QMessageBox.information(
            self,
            "ZIP 信息",
            "\n".join([
                "ZIP 文件信息",
                "",
                f"成员数量：{result['members']}",
                f"文件数量：{result['files']}",
                f"目录数量：{result['dirs']}",
                f"解压后大小：{self.human_size(result['unpacked'])}",
            ]),
        )

    def _start_task(self, task, args, on_done=None):
        if self.task_thread and self.task_thread.isRunning():
            QMessageBox.warning(self, "提示", "已有远程任务正在执行")
            return

        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.btn_cancel_task.show()
        self._set_controls(False)

        self.task_thread = RemoteTaskThread(
            task, args, self.uploader, self
        )
        self.task_thread.progress.connect(self._task_progress)
        self.task_thread.finished.connect(
            lambda ok, result, msg:
            self._task_finished(ok, result, msg, on_done)
        )
        self.task_thread.start()

    def _task_progress(self, done, total, stage):
        if total > 0:
            self.progress_bar.setValue(
                min(100, int(done * 100 / total))
            )
            self.status_label.setText(
                f"{stage}：{self.human_size(done)} / "
                f"{self.human_size(total)}"
            )
        else:
            # 搜索/目录统计没有可靠总量时，进度条显示忙碌状态。
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(stage)

    def _task_finished(self, success, result, msg, on_done):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        self.btn_cancel_task.hide()
        self._set_controls(True)
        self.task_thread = None

        if on_done:
            on_done(success, result, msg)
        elif success:
            self.status_label.setText(msg)
            self.refresh()
        else:
            self.status_label.setText(msg)
            QMessageBox.warning(self, "远程操作失败", msg)

    def _set_controls(self, enabled):
        for button in (
            self.btn_new_folder,
            self.btn_upload_file,
            self.btn_download,
            self.btn_delete,
            self.btn_size,
            self.btn_info,
            self.btn_root,
            self.btn_up,
            self.btn_refresh,
            self.btn_recursive_search,
        ):
            button.setEnabled(enabled)

        if hasattr(self, "btn_select"):
            self.btn_select.setEnabled(enabled)

        # 任务运行期间允许关闭，关闭时会先发取消请求。
        self.btn_close.setEnabled(True)

    def cancel_task(self):
        if self.task_thread and self.task_thread.isRunning():
            self.status_label.setText("正在取消任务...")
            self.btn_cancel_task.setEnabled(False)
            self.task_thread.cancel()

    def show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        entry = item.data(Qt.UserRole)
        menu = QMenu(self)

        if entry["is_dir"]:
            act_open = menu.addAction("打开目录")
        else:
            act_open = menu.addAction(
                "查看 ZIP 信息"
                if entry["name"].lower().endswith(".zip")
                else "下载文件"
            )

        act_info = menu.addAction("详细信息")
        act_upload = menu.addAction("上传文件到当前目录")
        menu.addSeparator()
        act_size = menu.addAction("统计目录大小")
        act_delete = menu.addAction("删除")

        chosen = menu.exec_(
            self.list_widget.mapToGlobal(pos)
        )

        if chosen == act_open:
            self.on_double_click(item)
        elif chosen == act_info:
            self.show_file_info()
        elif chosen == act_upload:
            self.upload_file()
        elif chosen == act_size:
            self.calculate_selected_size()
        elif chosen == act_delete:
            self.delete_selected()

    def select_current(self):
        self.selected_path = self.current_path
        self.accept()

    def get_selected_subpath(self):
        if not self.selected_path:
            return None
        return self._relative_path(self.selected_path)

    def closeEvent(self, event):
        if self.task_thread and self.task_thread.isRunning():
            self.task_thread.cancel()
            self.task_thread.wait(1500)
        event.accept()
