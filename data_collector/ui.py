import os
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from PyQt5.Qt import QDesktopServices
import time
import traceback
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from .core import ConfigManager, logger, ProcessManager , run_in_terminal
from .core_operations import Launcher, TeleopManager
from .core_data import RecorderManager, DataUploader, LogFetcher, DataPlayer
from .core_data import UploadCancelledError

# ============ 对话框 ============
class TeleopDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("启动遥操作")
        self.setModal(True)
        layout = QVBoxLayout()
        layout.addWidget(QLabel("请输入用户名:"))
        self.edit = QLineEdit()
        layout.addWidget(self.edit)
        btn = QPushButton("确定")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)
        self.setLayout(layout)

    def get_username(self) -> str:
        return self.edit.text().strip()


class CalibrationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("标定设置")
        self.setModal(True)
        layout = QVBoxLayout()
        layout.addWidget(QLabel("请输入 tracker 组别号 (数字):"))
        self.edit = QLineEdit()
        layout.addWidget(self.edit)
        layout.addWidget(QLabel("请按照终端提示操作，标定完成后自动继续"))
        hbox = QHBoxLayout()
        ok = QPushButton("开始标定")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        hbox.addWidget(ok)
        hbox.addWidget(cancel)
        layout.addLayout(hbox)
        self.setLayout(layout)

    def get_tracker_number(self) -> str:
        return self.edit.text().strip()


class RecordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("录制设置")
        self.setModal(True)
        layout = QVBoxLayout()
        layout.addWidget(QLabel("录制类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["bag", "cmd"])
        layout.addWidget(self.type_combo)
        layout.addWidget(QLabel("名称 (name):"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("可选")
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("描述 (desc):"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("可选")
        layout.addWidget(self.desc_edit)
        hbox = QHBoxLayout()
        ok = QPushButton("开始录制")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        hbox.addWidget(ok)
        hbox.addWidget(cancel)
        layout.addLayout(hbox)
        self.setLayout(layout)

    def get_settings(self):
        return {
            "type": self.type_combo.currentText(),
            "name": self.name_edit.text().strip(),
            "desc": self.desc_edit.text().strip()
        }


# ============ 远程目录浏览对话框 ============
class RemoteTaskThread(QThread):
    """远程 SFTP 长任务线程，避免搜索/下载/上传/统计阻塞 GUI。"""
    finished = pyqtSignal(bool, object, str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, task, args, uploader, parent=None):
        super().__init__(parent)
        self.task = task
        self.args = args
        self.uploader = uploader
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.uploader.cancel_upload()

    def running(self):
        return not self._cancelled

    def run(self):
        try:
            if self.task == "download":
                remote, local = self.args

                def cb(done, total):
                    if not self.running():
                        raise UploadCancelledError("用户取消下载")
                    self.progress.emit(int(done), int(total), "下载")

                ok = self.uploader.download_remote_file(
                    remote, Path(local), cb
                )
                self.finished.emit(
                    ok, None, "下载完成" if ok else "下载已取消"
                )

            elif self.task == "upload_file":
                local, remote = self.args

                def cb(done, total):
                    if not self.running():
                        raise UploadCancelledError("用户取消上传")
                    self.progress.emit(int(done), int(total), "上传")

                ok = self.uploader.upload_local_file(
                    Path(local), remote, self.running, cb
                )
                self.finished.emit(
                    ok, None, "上传完成" if ok else "上传已取消"
                )

            elif self.task == "dir_size":
                remote = self.args[0]

                def cb(done, count):
                    self.progress.emit(
                        int(done), 0, f"已扫描 {count} 个文件"
                    )

                size = self.uploader.get_remote_tree_size(
                    remote, self.running, cb
                )
                self.finished.emit(True, size, "统计完成")

            elif self.task == "search":
                remote, keyword = self.args

                def cb(scanned, matches):
                    self.progress.emit(
                        int(scanned), 0,
                        f"正在搜索：已扫描 {scanned} 项，命中 {matches} 项"
                    )

                result = self.uploader.search_remote(
                    remote, keyword, self.running, cb
                )
                self.finished.emit(
                    True, result, f"搜索完成，共找到 {len(result)} 项"
                )

            elif self.task == "zip_info":
                remote = self.args[0]
                # 远程 ZIP 的详细信息：下载到临时文件后只读取 ZIP 中央目录。
                import tempfile
                transport, sftp = self.uploader._connect_sftp()
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=".zip", delete=False
                    ) as tmp:
                        tmp_path = Path(tmp.name)
                    sftp.get(remote, str(tmp_path))
                    with zipfile.ZipFile(tmp_path, "r") as zf:
                        members = zf.infolist()
                        file_count = sum(1 for x in members if not x.is_dir())
                        dir_count = sum(1 for x in members if x.is_dir())
                        unpacked = sum(x.file_size for x in members)
                    self.finished.emit(
                        True,
                        {
                            "members": len(members),
                            "files": file_count,
                            "dirs": dir_count,
                            "unpacked": unpacked,
                        },
                        "ZIP 信息读取完成"
                    )
                finally:
                    if tmp_path:
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                    sftp.close()
                    transport.close()

            else:
                raise ValueError(f"未知远程任务: {self.task}")

        except UploadCancelledError as e:
            self.finished.emit(False, None, str(e))
        except Exception as e:
            self.finished.emit(False, None, str(e))


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
        self.status_label.setStyleSheet(
            "QLabel { color: gray; padding: 3px; }"
        )
        layout.addWidget(self.status_label)

        self.space_label = QLabel("远程空间：读取中...")
        self.space_label.setStyleSheet(
            "QLabel { color: #555; padding: 3px; }"
        )
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
            self.btn_select.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: white; "
                "font-weight: bold; }"
            )
            self.btn_select.clicked.connect(self.select_current)
            btn_row.addWidget(self.btn_select)

        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        self.refresh()
        QTimer.singleShot(100, self.refresh_space)

    @staticmethod
    def human_size(size):
        size = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} EB"

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
            if "取消" in msg:
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


# ============ 查看数据对话框 ============
class DataViewDialog(QDialog):
    def __init__(self, uploader, log_fetcher, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看数据")
        self.setModal(False)
        self.resize(900, 600)
        self.uploader = uploader
        self.log_fetcher = log_fetcher
        self.local_log_dir = log_fetcher.local_log_dir
        self.upload_thread = None
        self.upload_in_progress = False

        layout = QVBoxLayout()

        # 工具栏
        toolbar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_lists)
        self.btn_zoom_in = QPushButton("放大窗口")
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        self.btn_zoom_out = QPushButton("缩小窗口")
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        self.btn_remote_browser = QPushButton("远程目录")
        self.btn_remote_browser.clicked.connect(self.open_remote_browser)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addWidget(self.btn_zoom_in)
        toolbar.addWidget(self.btn_zoom_out)
        toolbar.addWidget(self.btn_remote_browser)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # ---------- 日志文件 ----------
        log_group = QGroupBox("日志文件 ( /var/nerv/log )")
        log_layout = QVBoxLayout()
        self.log_list = QListWidget()
        self.log_list.setSelectionMode(QAbstractItemView.SingleSelection)
        log_layout.addWidget(self.log_list)

        log_btn_layout = QHBoxLayout()
        self.btn_fetch_selected = QPushButton("抓取选中日志")
        self.btn_fetch_selected.clicked.connect(self.fetch_selected_log)
        self.btn_view_selected_log = QPushButton("查看选中日志")
        self.btn_view_selected_log.clicked.connect(self.view_selected_log)
        log_btn_layout.addWidget(self.btn_fetch_selected)
        log_btn_layout.addWidget(self.btn_view_selected_log)
        log_btn_layout.addStretch()
        log_layout.addLayout(log_btn_layout)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

        # ---------- 数据文件夹 ----------
        bag_group = QGroupBox("数据文件夹 ( /var/nerv/bag )")
        bag_layout = QVBoxLayout()
        self.bag_list = QListWidget()
        self.bag_list.setSelectionMode(QAbstractItemView.SingleSelection)
        bag_layout.addWidget(self.bag_list)

        # --- 新增播放按钮 ---
        self.btn_play_bag = QPushButton("播放选中数据")
        self.btn_play_bag.clicked.connect(self.play_selected_bag)

        bag_btn_layout = QHBoxLayout()
        self.btn_upload_selected = QPushButton("上传选中")
        self.btn_upload_selected.clicked.connect(self.upload_selected_bag)
        self.btn_view_selected_bag = QPushButton("查看选中")
        self.btn_view_selected_bag.clicked.connect(self.view_selected_bag)
        self.btn_delete_selected_bag = QPushButton("删除选中")
        self.btn_delete_selected_bag.setStyleSheet("QPushButton { background-color: #f44336; color: white; }")
        self.btn_delete_selected_bag.clicked.connect(self.delete_selected_bag)
        bag_btn_layout.addWidget(self.btn_upload_selected)
        bag_btn_layout.addWidget(self.btn_play_bag)   
        bag_btn_layout.addWidget(self.btn_view_selected_bag)
        bag_btn_layout.addWidget(self.btn_delete_selected_bag)
        bag_btn_layout.addStretch()
        bag_layout.addLayout(bag_btn_layout)
        bag_group.setLayout(bag_layout)
        layout.addWidget(bag_group)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.setLayout(layout)

        self.log_list.itemDoubleClicked.connect(self._on_log_double_click)
        self.bag_list.itemDoubleClicked.connect(self._on_bag_double_click)

        self.refresh_lists()

    def open_remote_browser(self):
        dlg = RemoteDirectoryDialog(self.uploader, self, select_mode=False)
        dlg.exec_()

    def refresh_lists(self):
        self.log_list.clear()
        for f in self.log_fetcher.get_log_files():
            item = QListWidgetItem(f.name)
            item.setData(1, str(f))
            item.setForeground(Qt.blue)
            item.setFont(QFont("", 10))
            self.log_list.addItem(item)

        self.bag_list.clear()
        for d in self.uploader.get_bag_folders():
            item = QListWidgetItem(d.name)
            item.setData(1, str(d))
            item.setForeground(Qt.blue)
            item.setFont(QFont("", 10))
            self.bag_list.addItem(item)

    def _get_selected_log(self):
        item = self.log_list.currentItem()
        if not item:
            QMessageBox.warning(self, "提示", "请先选择一个日志文件")
            return None
        return Path(item.data(1))

    def _get_selected_bag(self):
        item = self.bag_list.currentItem()
        if not item:
            QMessageBox.warning(self, "提示", "请先选择一个数据文件夹")
            return None
        return Path(item.data(1))

    def fetch_selected_log(self):
        file_path = self._get_selected_log()
        if not file_path:
            return
        success = self.log_fetcher.fetch_specific_log(file_path)
        if success:
            QMessageBox.information(self, "成功", f"日志已抓取到 {self.local_log_dir}")
        else:
            QMessageBox.critical(self, "失败", "日志抓取失败，请查看日志")

    def view_selected_log(self):
        file_path = self._get_selected_log()
        if not file_path:
            return
        self._open_file(file_path)

    def play_selected_bag(self):
        """播放选中的 bag 数据（调用 play_bag.sh）"""
        import subprocess
        # 1. 检查主程序是否正在运行
        result = subprocess.run(["pgrep", "-f", "run_nodes.sh"], capture_output=True)
        if result.returncode == 0:
            QMessageBox.warning(
                self,
                "主程序正在运行",
                "主程序正在运行，请先关闭主程序（点击主界面“关闭程序”按钮）再播放数据。"
            )
            return

        # 2. 获取选中的 bag 文件夹
        folder_path = self._get_selected_bag()
        if not folder_path:
            return

        # 3. 尝试确保 bag 目录及其子目录可写（解决解压时权限不足问题）
        try:
            subprocess.run(["chmod", "-R", "u+w", str(folder_path)], check=False, timeout=5)
        except Exception as e:
            self.append_log(f"警告：无法修改权限 {folder_path}，播放可能失败：{e}")

        # 4. 获取工作空间配置
        if not self.parent() or not hasattr(self.parent(), 'config'):
            QMessageBox.critical(self, "错误", "无法获取配置，请检查")
            return

        config = self.parent().config
        workspace = config.workspace
        terminal = config.get_terminal()

        # 5. 构建启动命令（新终端，source 环境，执行脚本）
        cmd = (
            f"cd {workspace} && "
            f"source install/setup.bash && "
            f"bash scripts/play_bag.sh rviz bag=\"{folder_path}\" ; "
            f"echo '=== play_bag.sh 已退出，退出码: $? ===' ; "
            f"read -p '按 Enter 键关闭窗口...'"
        )
        try:
            run_in_terminal(cmd, terminal)
            self.append_log(f"已启动数据播放：{folder_path}")
        except Exception as e:
            QMessageBox.critical(self, "启动失败", f"无法启动播放脚本：{e}")

    def upload_selected_bag(self):
        if self.upload_in_progress:
            self._cancel_upload_in_dialog()
            return
        folder_path = self._get_selected_bag()
        if not folder_path:
            return
        remote_dlg = RemoteDirectoryDialog(self.uploader, self, select_mode=True)
        if remote_dlg.exec_() != QDialog.Accepted:
            return
        subpath = remote_dlg.get_selected_subpath()
        if subpath is None:
            return

        # --- 新增：显示父窗口的进度控件 ---
        if self.parent() and hasattr(self.parent(), 'show_upload_progress'):
            self.parent().show_upload_progress(True)

        self.btn_refresh.setEnabled(False)
        self.btn_fetch_selected.setEnabled(False)
        self.btn_view_selected_log.setEnabled(False)
        self.btn_view_selected_bag.setEnabled(False)
        self.btn_delete_selected_bag.setEnabled(False)
        self.btn_upload_selected.setText("取消上传")
        self.btn_upload_selected.setStyleSheet("QPushButton { background-color: #f44336; color: white; font-weight: bold; }")
        self.upload_in_progress = True
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(0)
        if self.parent() and hasattr(self.parent(), "progress_signal"):
            self.parent().progress_signal.emit("准备压缩...")

        def progress_msg(text):
            if self.parent() and hasattr(self.parent(), 'progress_signal'):
                self.parent().progress_signal.emit(text)

        self.upload_thread = UploadSpecificThread(self.uploader, folder_path, subpath, progress_msg)
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.upload_thread.progress_signal.connect(self.parent().update_upload_progress)
        self.upload_thread.finished.connect(self._on_upload_finished)
        self.upload_thread.start()

    def _cancel_upload_in_dialog(self):
        """取消上传（上传过程中“上传选中”按钮会变为“取消上传”）。

        注意：本方法必须定义在 DataViewDialog 类层级，调用处是
        self._cancel_upload_in_dialog()，不要再缩进成 upload_selected_bag 的嵌套函数。
        """
        if self.upload_in_progress:
            self.append_log("正在取消上传...")
            if self.upload_thread and self.upload_thread.isRunning():
                self.upload_thread.cancel()
                # 不在 GUI 线程中 wait，避免取消时界面短暂卡死。
            # 取消请求发送后，按钮仍保持可点击；真正恢复“上传选中”状态
            # 由 finished 信号统一处理。
            self.btn_upload_selected.setEnabled(True)
            self.btn_upload_selected.setText("正在取消...")
            self.btn_upload_selected.setStyleSheet("QPushButton { background-color: #ff9800; color: white; font-weight: bold; }")
            self.btn_refresh.setEnabled(True)
            self.btn_fetch_selected.setEnabled(True)
            self.btn_view_selected_log.setEnabled(True)
            self.btn_view_selected_bag.setEnabled(True)
            self.btn_delete_selected_bag.setEnabled(True)
            if self.parent() and hasattr(self.parent(), "update_upload_progress"):
                self.parent().update_upload_progress(0)
            self.append_log("上传取消请求已发送，等待当前传输块结束...")

    def _on_upload_finished(self, success, msg):
        self.btn_upload_selected.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.btn_fetch_selected.setEnabled(True)
        self.btn_view_selected_log.setEnabled(True)
        self.btn_view_selected_bag.setEnabled(True)
        self.btn_delete_selected_bag.setEnabled(True)
        self.upload_in_progress = False
        self.btn_upload_selected.setText("上传选中")
        self.btn_upload_selected.setStyleSheet("")
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(100 if success else 0)
        # --- 新增：隐藏父窗口的进度控件 ---
        if self.parent() and hasattr(self.parent(), 'show_upload_progress'):
            self.parent().show_upload_progress(False)
        if success:
            QMessageBox.information(self, "成功", msg)
            self.append_log(msg)
        else:
            self.append_log(f"上传失败: {msg}")
            QMessageBox.critical(self, "失败", msg)
        self.refresh_lists()

    def view_selected_bag(self):
        folder_path = self._get_selected_bag()
        if not folder_path:
            return
        self._open_folder(folder_path)

    def delete_selected_bag(self):
        folder_path = self._get_selected_bag()
        if not folder_path:
            return
        reply = QMessageBox.question(self, "确认删除", f"确定要删除 {folder_path.name} 吗？此操作不可恢复！",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                shutil.rmtree(folder_path)
                QMessageBox.information(self, "成功", f"已删除 {folder_path.name}")
                self.refresh_lists()
            except Exception as e:
                QMessageBox.critical(self, "失败", f"删除失败: {e}")

    def _open_file(self, file_path: Path):
        try:
            subprocess.Popen(["gedit", str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            QDesktopServices.openUrl(file_path.as_uri())

    def _open_folder(self, folder_path: Path):
        try:
            subprocess.Popen(["nautilus", str(folder_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            QDesktopServices.openUrl(folder_path.as_uri())

    def zoom_in(self):
        self.resize(self.width() + 100, self.height() + 80)

    def zoom_out(self):
        w = max(400, self.width() - 100)
        h = max(300, self.height() - 80)
        self.resize(w, h)

    def _on_log_double_click(self, item):
        file_path = Path(item.data(1))
        self._open_file(file_path)

    def _on_bag_double_click(self, item):
        folder_path = Path(item.data(1))
        self._open_folder(folder_path)

    def append_log(self, text):
        if self.parent() and hasattr(self.parent(), 'append'):
            self.parent().append(text)
    

# ============ 工作线程 ============
class LaunchThread(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, launcher, options):
        super().__init__()
        self.launcher = launcher
        self.options = options

    def run(self):
        try:
            proc = self.launcher.launch_run_nodes(self.options)
            time.sleep(0.5)
            if proc.poll() is not None:
                self.finished.emit(False, "主程序启动后进程立即退出，请检查日志")
            else:
                self.finished.emit(True, "主程序已后台启动")
        except Exception as e:
            err_msg = traceback.format_exc()
            logger.error(err_msg)
            self.finished.emit(False, f"启动主程序失败: {str(e)}\n{err_msg}")


class CalibrationThread(QThread):
    finished = pyqtSignal(bool, str, float, float)
    output_signal = pyqtSignal(str)

    def __init__(self, teleop_mgr, username, tracker_num):
        super().__init__()
        self.teleop_mgr = teleop_mgr
        self.username = username
        self.tracker_num = tracker_num

    def run(self):
        try:
            ok, err, sw, al = self.teleop_mgr.run_calibration(
                self.username, self.tracker_num,
                output_callback=self.output_signal.emit
            )
            self.finished.emit(ok, err, sw if sw else 0.0, al if al else 0.0)
        except Exception as e:
            err_msg = traceback.format_exc()
            logger.error(err_msg)
            self.finished.emit(False, f"标定过程异常: {str(e)}\n{err_msg}", 0.0, 0.0)


class UploadThread(QThread):
    finished = pyqtSignal(bool, str)
    progress_signal = pyqtSignal(int)

    def __init__(self, uploader, remote_subpath, progress_cb=None):
        super().__init__()
        self.uploader = uploader
        self.remote_subpath = remote_subpath
        self.progress_cb = progress_cb
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.uploader.cancel_upload()  # 终止 paramiko 传输

    def run(self):
        try:
            # 传递 running 检查
            def running():
                return not self._cancelled

            # 封装进度回调，同时更新标签和（可选）日志
            def progress_cb_wrapper(text):
                if self.progress_cb:
                    self.progress_cb(text)  # 更新标签
                # 不再追加日志，减少刷屏

            def progress_percent_wrapper(value):
                self.progress_signal.emit(value)

            success = self.uploader.upload_latest_bag(
                self.remote_subpath,
                progress_cb=progress_cb_wrapper,
                progress_percent=progress_percent_wrapper
            )
            if success:
                self.finished.emit(True, "上传成功")
            else:
                self.finished.emit(False, "上传失败或已取消")
        except Exception as e:
            self.finished.emit(False, str(e))


class FetchLogThread(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, fetcher):
        super().__init__()
        self.fetcher = fetcher

    def run(self):
        success = self.fetcher.fetch_latest_log()
        if success:
            self.finished.emit(True, "日志抓取成功")
        else:
            self.finished.emit(False, "日志抓取失败，请查看日志")


class UploadSpecificThread(QThread):
    finished = pyqtSignal(bool, str)
    progress_signal = pyqtSignal(int)

    def __init__(self, uploader, folder_path, subpath, progress_cb=None):
        super().__init__()
        self.uploader = uploader
        self.folder_path = folder_path
        self.subpath = subpath
        self.progress_cb = progress_cb
        self._cancelled = False

    def run(self):
        try:
            def progress_percent_wrapper(value):
                self.progress_signal.emit(value)

            success = self.uploader.upload_specific_bag(
                self.folder_path,
                self.subpath,
                progress_cb=self.progress_cb,
                progress_percent=progress_percent_wrapper
            )
            if success:
                self.finished.emit(True, "上传成功")
            else:
                self.finished.emit(False, "上传失败或已取消")
        except Exception as e:
            self.finished.emit(False, str(e))

    def cancel(self):
        self._cancelled = True
        self.uploader.cancel_upload()


# ============ 主窗口 ============
class MainWindow(QMainWindow):
    progress_signal = pyqtSignal(str)   # 进度文字更新信号
    log_signal = pyqtSignal(str)        # 日志更新信号

    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        self.config = config_manager
        self.launcher = Launcher(config_manager)
        self.teleop = TeleopManager(config_manager, self.launcher)
        self.recorder = RecorderManager(config_manager, self.launcher)
        self.uploader = DataUploader(config_manager)
        self.log_fetcher = LogFetcher(Path(__file__).parent.parent / "logs", config_manager)
        self.player = DataPlayer()
        self.launch_thread = None
        self.calib_thread = None
        self.upload_thread = None
        self.upload_in_progress = False

        # 录制计时器
        self.record_timer = QTimer()
        self.record_timer.timeout.connect(self.update_record_time)
        self.record_seconds = 0
        self.record_start_time = None

        # 连接信号
        self.progress_signal.connect(self.update_progress_label)
        self.log_signal.connect(self.append_safe)

        self.setWindowTitle("数据采集工具")
        self.setGeometry(100, 100, 650, 600)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # --------------------- 操作控制区域 ---------------------
        group_ops = QGroupBox("操作控制")
        ops_layout = QVBoxLayout()

        # 启动主程序子区域
        launch_widget = QWidget()
        launch_layout = QHBoxLayout(launch_widget)
        launch_layout.setContentsMargins(0, 0, 0, 0)

        self.cb_joy = QCheckBox("joy")
        self.cb_rviz = QCheckBox("rviz")

        hand_widget = QWidget()
        hand_layout = QHBoxLayout(hand_widget)
        hand_layout.setSpacing(0)
        hand_layout.setContentsMargins(0, 0, 0, 0)
        hand_label = QLabel("hand=")
        hand_label.setStyleSheet("padding: 0px; margin: 0px;")
        hand_label.setIndent(0)
        hand_layout.addWidget(hand_label)
        self.hand_combo = QComboBox()
        self.hand_combo.addItem("")
        self.hand_combo.addItem("yushu_dex11")
        self.hand_combo.addItem("hangkai")
        self.hand_combo.addItem("yinshi")
        self.hand_combo.setCurrentIndex(0)
        hand_layout.addWidget(self.hand_combo)

        self.cb_sim = QCheckBox("use_sim")

        self.btn_launch = QPushButton("启动程序")
        self.btn_launch.setFixedWidth(80)
        self.btn_launch.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }")
        self.btn_launch.clicked.connect(self.on_launch)

        self.btn_stop_main = QPushButton("关闭程序")
        self.btn_stop_main.setFixedWidth(100)
        self.btn_stop_main.setStyleSheet("QPushButton { background-color: #c62828; color: white; font-weight: bold; }")
        self.btn_stop_main.clicked.connect(self.on_stop_main)

        launch_layout.addWidget(self.cb_joy)
        launch_layout.addWidget(self.cb_rviz)
        launch_layout.addWidget(hand_widget)
        launch_layout.addWidget(self.cb_sim)
        launch_layout.addWidget(self.btn_launch)
        launch_layout.addWidget(self.btn_stop_main)
        launch_layout.addStretch()

        ops_layout.addWidget(launch_widget)

        ops_btns = QHBoxLayout()
        self.btn_teleop = QPushButton("启动遥操作")
        self.btn_teleop.clicked.connect(self.on_teleop)
        ops_btns.addWidget(self.btn_teleop)
        ops_btns.addStretch()
        ops_layout.addLayout(ops_btns)

        group_ops.setLayout(ops_layout)
        main_layout.addWidget(group_ops)

        # --------------------- 数据处理区域 ---------------------
        group_data = QGroupBox("数据处理")
        data_main_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        self.btn_record = QPushButton("数据录制")
        self.btn_record.clicked.connect(self.on_record)
        self.btn_stop = QPushButton("停止录制")
        self.btn_stop.clicked.connect(self.on_stop_record)
        self.btn_stop.setEnabled(False)
        self.record_status_label = QLabel("未录制")
        self.record_status_label.setStyleSheet("QLabel { color: gray; font-weight: bold; }")
        row1.addWidget(self.btn_record)
        row1.addWidget(self.btn_stop)
        row1.addWidget(self.record_status_label)
        row1.addStretch()
        data_main_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_upload = QPushButton("上传数据")
        self.btn_upload.setFixedWidth(100)
        self.btn_upload.clicked.connect(self.on_upload)
        self.btn_fetch_log = QPushButton("抓取日志")
        self.btn_fetch_log.clicked.connect(self.on_fetch_log)
        self.btn_view_data = QPushButton("查看数据")
        self.btn_view_data.clicked.connect(self.on_view_data)
        # 新增播放数据按钮
        self.btn_play_data = QPushButton("播放数据")
        self.btn_play_data.setFixedWidth(100)
        self.btn_play_data.clicked.connect(self.on_play_data)
        row2.addWidget(self.btn_upload)
        row2.addWidget(self.btn_fetch_log)
        row2.addWidget(self.btn_view_data)
        row2.addWidget(self.btn_play_data)   # 新增
        row2.addStretch()
        data_main_layout.addLayout(row2)

        group_data.setLayout(data_main_layout)
        main_layout.addWidget(group_data)

        # 日志显示区域
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        main_layout.addWidget(self.log)

        # 上传/压缩进度固定放在窗口最底部。
        self.progress_label = QLabel("就绪")
        self.progress_label.setMinimumHeight(26)
        self.progress_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.progress_label.setStyleSheet(
            "QLabel { color: blue; font-weight: bold; padding: 4px 6px; "
            "border-top: 1px solid #cccccc; }"
        )
        main_layout.addWidget(self.progress_label)

        self.upload_progress_bar = QProgressBar()
        self.upload_progress_bar.setRange(0, 100)
        self.upload_progress_bar.setValue(0)
        self.upload_progress_bar.setTextVisible(True)
        self.upload_progress_bar.setFormat("%p%")
        self.upload_progress_bar.setMinimumHeight(22)
        main_layout.addWidget(self.upload_progress_bar)
        self.progress_label.hide()
        self.upload_progress_bar.hide()

    # --------------------- 线程安全的日志方法 ---------------------
    def append_safe(self, text):
        """在主线程中追加日志"""
        self.log.append(text)

    def append(self, text):
        """外部调用日志接口（发送信号到主线程）"""
        self.log_signal.emit(text)

    def update_progress_label(self, text):
        """更新进度标签（主线程）"""
        self.progress_label.setText(text)

    def update_upload_progress(self, value):
        """更新压缩/上传总进度（主线程）。压缩 0~50%，上传 50~100%。"""
        self.upload_progress_bar.setValue(max(0, min(100, value)))

    def show_error(self, title, msg):
        logger.error(msg)
        QMessageBox.critical(self, title, msg)

    # ---------- 录制计时 ----------
    def update_record_time(self):
        self.record_seconds += 1
        minutes = self.record_seconds // 60
        seconds = self.record_seconds % 60
        self.record_status_label.setText(f"录制中 {minutes:02d}:{seconds:02d}")

    # ---------- 启动主程序 ----------
    def on_launch(self):
        opts = []
        if self.cb_joy.isChecked():
            opts.append("joy")
        if self.cb_rviz.isChecked():
            opts.append("rviz")
        hand_val = self.hand_combo.currentText()
        if hand_val:
            opts.append(f"hand={hand_val}")
        if self.cb_sim.isChecked():
            opts.append("use_sim")
        self.btn_launch.setEnabled(False)
        self.append("正在启动主程序...")
        try:
            proc = self.launcher.launch_run_nodes(opts)
            self.append("主程序已在新终端启动")
            self.btn_launch.setEnabled(True)
        except Exception as e:
            self.btn_launch.setEnabled(True)
            self.show_error("启动失败", str(e))

    # ---------- 关闭主程序 ----------
    def on_stop_main(self):
        ProcessManager().terminate_by_name("run_nodes")
        self.append("已发送关闭信号到主程序")
        QMessageBox.information(self, "提示", "已尝试关闭主程序，请查看日志")

    # ---------- 遥操作 ----------
    def _launch_teleop(self):
        try:
            self.teleop.launch_teleop()
            self.append("遥操作已后台启动")
            QMessageBox.information(self, "提示", "遥操作已后台启动，请查看终端或日志")
        except Exception as e:
            self.show_error("启动遥操作失败", str(e))

    def _enter_calibration_mode(self):
        self.btn_teleop.setText("取消标定")
        self.btn_teleop.clicked.disconnect(self.on_teleop)
        self.btn_teleop.clicked.connect(self.on_cancel_calibration)
        self.btn_teleop.setStyleSheet("QPushButton { background-color: #f44336; color: white; font-weight: bold; }")

    def _exit_calibration_mode(self):
        self.btn_teleop.setText("启动遥操作")
        self.btn_teleop.clicked.disconnect(self.on_cancel_calibration)
        self.btn_teleop.clicked.connect(self.on_teleop)
        self.btn_teleop.setStyleSheet("")

    def on_teleop(self):
        dlg = TeleopDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        username = dlg.get_username()
        if not username:
            self.show_error("输入错误", "用户名不能为空")
            return
        if self.teleop.check_user(username):
            if self.teleop.activate_user(username):
                self.append(f"已激活用户 {username}，正在启动遥操作...")
                self._launch_teleop()
            else:
                self.show_error("激活失败", "无法更新配置文件，请检查权限")
        else:
            calib_dlg = CalibrationDialog(self)
            if calib_dlg.exec_() != QDialog.Accepted:
                return
            tracker = calib_dlg.get_tracker_number()
            if not tracker.isdigit() or int(tracker) < 1 or int(tracker) > 4:
                self.show_error("输入错误", "请输入1-4的数字")
                return
            self.append(f"开始标定，用户 {username}, tracker {tracker}")
            self.btn_teleop.setEnabled(False)
            self.calib_thread = CalibrationThread(self.teleop, username, int(tracker))
            self.calib_thread.output_signal.connect(self.append)
            self.calib_thread.finished.connect(self.on_calib_finished)
            self.calib_thread.start()
            self._enter_calibration_mode()
            self.btn_teleop.setEnabled(True)

    def on_cancel_calibration(self):
        if self.calib_thread and self.calib_thread.isRunning():
            self.append("正在取消标定...")
            self.teleop.cancel_calibration()
            self.calib_thread.wait(2000)
            if self.calib_thread.isRunning():
                self.calib_thread.terminate()
            self.calib_thread = None
            self.append("标定已取消")
            self._exit_calibration_mode()
            QMessageBox.information(self, "提示", "标定已取消")
        else:
            QMessageBox.warning(self, "提示", "没有正在进行的标定")

    def on_calib_finished(self, ok, err, sw, al):
        self._exit_calibration_mode()
        self.btn_teleop.setEnabled(True)
        if ok:
            msg = f"标定完成！\nshoulder_width = {sw:.4f}\narm_length = {al:.4f}\n配置已更新，正在启动遥操作..."
            QMessageBox.information(self, "标定成功", msg)
            self.append(f"标定完成: shoulder={sw}, arm={al}")
            self._launch_teleop()
        else:
            self.show_error("标定失败", err)

    # ---------- 数据录制 ----------
    def on_record(self):
        dlg = RecordDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        settings = dlg.get_settings()
        if self.recorder.is_active():
            self.show_error("录制冲突", "已有录制任务正在进行")
            return
        if self.recorder.start(settings["type"], settings["name"], settings["desc"]):
            self.append(f"录制已后台启动: {settings['type']}, name={settings['name']}, desc={settings['desc']}")
            self.record_seconds = 0
            self.record_status_label.setText("录制中 00:00")
            self.record_status_label.setStyleSheet("QLabel { color: green; font-weight: bold; }")
            self.record_timer.start(1000)
            self.btn_record.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.record_start_time = time.time()
            QMessageBox.information(self, "提示", "录制已后台启动，可通过“停止录制”结束")
        else:
            self.show_error("录制启动失败", "请检查日志或配置文件路径")

    def on_stop_record(self):
        if self.recorder.stop():
            self.append("录制已停止")
            self.record_timer.stop()
            self.record_status_label.setText("已停止")
            self.record_status_label.setStyleSheet("QLabel { color: gray; font-weight: bold; }")
            self.btn_record.setEnabled(True)
            self.btn_stop.setEnabled(False)

            if self.record_start_time is not None:
                bag_folders = self.uploader.get_bag_folders()
                latest_folder = None
                for folder in bag_folders:
                    if folder.stat().st_mtime > self.record_start_time:
                        latest_folder = folder
                        break
                if latest_folder:
                    self.append(f"录制文件已保存至: {latest_folder}")
                else:
                    self.append("未找到录制生成的文件，请检查录制是否成功")
                self.record_start_time = None

            QMessageBox.information(self, "提示", "录制已停止")
        else:
            self.show_error("停止失败", "没有活动的录制任务")

    #数据播放
    def on_play_data(self):
        """播放数据：选择 bag 文件夹，调用 play_bag.sh"""
        import subprocess
        # 1. 检查主程序是否正在运行
        result = subprocess.run(["pgrep", "-f", "run_nodes.sh"], capture_output=True)
        if result.returncode == 0:
            QMessageBox.warning(
                self,
                "主程序正在运行",
                "主程序正在运行，请先关闭主程序（点击“关闭程序”按钮）再播放数据。"
            )
            return

        # 2. 弹出目录选择对话框
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "选择要播放的 bag 文件夹",
            str(self.config.get_bag_dir()),
            QFileDialog.ShowDirsOnly
        )
        if not folder_path:
            return

        folder_path = Path(folder_path)

        # 3. 尝试确保 bag 目录及其子目录可写
        try:
            subprocess.run(["chmod", "-R", "u+w", str(folder_path)], check=False, timeout=5)
        except Exception as e:
            self.append(f"警告：无法修改权限 {folder_path}，播放可能失败：{e}")

        # 4. 获取工作空间配置
        config = self.config
        workspace = config.workspace
        terminal = config.get_terminal()

        # 5. 构建启动命令
        cmd = (
            f"cd {workspace} && "
            f"source install/setup.bash && "
            f"bash scripts/play_bag.sh rviz bag=\"{folder_path}\" ; "
            f"echo '=== play_bag.sh 已退出，退出码: $? ===' ; "
            f"read -p '按 Enter 键关闭窗口...'"
        )
        try:
            run_in_terminal(cmd, terminal)
            self.append(f"已启动数据播放：{folder_path}")
        except Exception as e:
            self.show_error("启动失败", f"无法启动播放脚本：{e}")
    # ---------- 上传最新数据 ----------
    def show_upload_progress(self, show: bool):
        if show:
            self.progress_label.show()
            self.upload_progress_bar.show()
        else:
            self.progress_label.hide()
            self.upload_progress_bar.hide()
    def on_upload(self):
        if hasattr(self, 'upload_in_progress') and self.upload_in_progress:
            self.on_cancel_upload()
            return
        remote_dlg = RemoteDirectoryDialog(self.uploader, self, select_mode=True)
        if remote_dlg.exec_() != QDialog.Accepted:
            return
        subpath = remote_dlg.get_selected_subpath()
        if subpath is None:
            return
        self.append(f"开始上传数据，远程子路径: {subpath or '(根目录)'}")
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("取消上传")
        self.btn_upload.setStyleSheet("QPushButton { background-color: #f44336; color: white; font-weight: bold; }")
        self.upload_in_progress = True
        # --- 修改开始：显示进度控件 ---
        self.show_upload_progress(True)
        self.upload_progress_bar.setValue(0)
        self.progress_label.setText("准备压缩...")
        # --- 修改结束 ---

        def progress_msg(text):
            self.progress_signal.emit(text)

        self.upload_thread = UploadThread(self.uploader, subpath, progress_msg)
        self.upload_thread.progress_signal.connect(self.update_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()

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
        self.btn_upload.setStyleSheet(
            "QPushButton { background-color: #ff9800; color: white; font-weight: bold; }"
        )
        self.progress_label.setText("正在取消上传，请等待当前数据块结束...")

    def on_upload_finished(self, success, msg):
        self.upload_in_progress = False
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("上传数据")
        self.btn_upload.setStyleSheet("")
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
            QMessageBox.critical(self, "上传失败", msg)
    # ---------- 抓取最新日志 ----------
    def on_fetch_log(self):
        self.append("开始抓取日志...")
        self.btn_fetch_log.setEnabled(False)
        self.fetch_thread = FetchLogThread(self.log_fetcher)
        self.fetch_thread.finished.connect(self.on_fetch_log_finished)
        self.fetch_thread.start()

    def on_fetch_log_finished(self, success, msg):
        self.btn_fetch_log.setEnabled(True)
        if success:
            QMessageBox.information(self, "抓取成功", msg)
            self.append(msg)
        else:
            self.show_error("抓取失败", msg)

    # ---------- 查看数据 ----------
    def on_view_data(self):
        dlg = DataViewDialog(self.uploader, self.log_fetcher, self)
        dlg.setModal(False)
        dlg.show()