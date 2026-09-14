import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont
from PyQt5.Qt import QDesktopServices
import time
import traceback
import shutil
from pathlib import Path
from .core import ConfigManager, logger, ProcessManager
from .core_operations import Launcher, TeleopManager
from .core_data import RecorderManager, DataUploader, LogFetcher, DataPlayer

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
        toolbar.addWidget(self.btn_refresh)
        toolbar.addWidget(self.btn_zoom_in)
        toolbar.addWidget(self.btn_zoom_out)
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

        bag_btn_layout = QHBoxLayout()
        self.btn_upload_selected = QPushButton("上传选中")
        self.btn_upload_selected.clicked.connect(self.upload_selected_bag)
        self.btn_view_selected_bag = QPushButton("查看选中")
        self.btn_view_selected_bag.clicked.connect(self.view_selected_bag)
        self.btn_delete_selected_bag = QPushButton("删除选中")
        self.btn_delete_selected_bag.setStyleSheet("QPushButton { background-color: #f44336; color: white; }")
        self.btn_delete_selected_bag.clicked.connect(self.delete_selected_bag)
        bag_btn_layout.addWidget(self.btn_upload_selected)
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

    def upload_selected_bag(self):
        if self.upload_in_progress:
            self._cancel_upload_in_dialog()
            return
        folder_path = self._get_selected_bag()
        if not folder_path:
            return
        subpath, ok = QInputDialog.getText(self, "上传数据", "请输入远程子路径 (如 '实验1'):")
        if not ok:
            return
        # 上传期间保持“上传选中”按钮可点击，用于随时取消；仅锁定其他会与上传冲突的操作。
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
            # 速度/剩余时间显示在主窗口底部状态栏，不刷日志。
            self.parent().progress_signal.emit(text) if self.parent() and hasattr(self.parent(), 'progress_signal') else None

        self.upload_thread = UploadSpecificThread(self.uploader, folder_path, subpath, progress_msg)
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.upload_thread.progress_signal.connect(self.parent().update_upload_progress)
        self.upload_thread.finished.connect(self._on_upload_finished)
        self.upload_thread.start()

    def _cancel_upload_in_dialog(self):
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
        self.uploader = DataUploader()
        self.log_fetcher = LogFetcher(Path(__file__).parent.parent / "logs")
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
        row2.addWidget(self.btn_upload)
        row2.addWidget(self.btn_fetch_log)
        row2.addWidget(self.btn_view_data)
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

    # ---------- 上传最新数据 ----------
    def on_upload(self):
        if hasattr(self, 'upload_in_progress') and self.upload_in_progress:
            self.on_cancel_upload()
            return
        subpath, ok = QInputDialog.getText(self, "上传数据", "请输入远程子路径 (如 '实验1'):")
        if not ok:
            return
        self.append(f"开始上传数据，远程子路径: {subpath}")
        # 上传期间按钮保持可点击，点击同一个按钮即可随时取消。
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("取消上传")
        self.btn_upload.setStyleSheet("QPushButton { background-color: #f44336; color: white; font-weight: bold; }")
        self.upload_in_progress = True
        self.upload_progress_bar.setValue(0)
        self.progress_label.setText("准备压缩...")

        # 进度回调：只更新标签，不追加日志
        def progress_msg(text):
            self.progress_signal.emit(text)   # 更新进度标签

        self.upload_thread = UploadThread(self.uploader, subpath, progress_msg)
        self.upload_thread.progress_signal.connect(self.update_upload_progress)
        self.upload_thread.finished.connect(self.on_upload_finished)
        self.upload_thread.start()

    def on_cancel_upload(self):
        if self.upload_in_progress:
            self.append("正在取消上传...")
            if self.upload_thread and self.upload_thread.isRunning():
                self.upload_thread.cancel()
                # 不阻塞 GUI，后台线程会在当前压缩/传输检查点退出。
            # 取消请求发送后，按钮仍保持可点击，但显示“正在取消...”
            # 不要在这里修改 upload_in_progress，等待工作线程 finished 信号统一收尾。
            self.btn_upload.setEnabled(True)
            self.btn_upload.setText("正在取消...")
            self.btn_upload.setStyleSheet("QPushButton { background-color: #ff9800; color: white; font-weight: bold; }")
            self.append("已发送上传取消请求，等待当前传输块结束...")

    def on_upload_finished(self, success, msg):
        self.upload_in_progress = False
        self.btn_upload.setEnabled(True)
        self.btn_upload.setText("上传数据")
        self.btn_upload.setStyleSheet("")
        self.progress_label.setText("上传完成" if success else "上传已取消/失败")
        self.upload_progress_bar.setValue(100 if success else 0)
        if success:
            QMessageBox.information(self, "上传成功", msg)
            self.append(msg)           # 关键信息记录到日志
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