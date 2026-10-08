"""数据浏览对话框：bag/日志列表、播放、上传、远端浏览入口。"""
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
                    human_size, dir_size, is_within)
from ..core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError


from .common import (SP, describe_export_target, ensure_remote_password,
                    make_panel_head, remote_subpath_arg, resolve_upload_subpath,
                    _warn_if_main_program_running, _play_bag_folder)
from .threads import (UploadThread, LocalTaskThread)
from .remote_browser import (RemoteDirectoryDialog)



class DataViewDialog(QDialog):
    """查看数据：左边机器人上的日志文件，右边本机已录制的 bag 目录。

    2026-09-18 重构：去掉遗留的「放大窗口 / 缩小窗口」按钮（那是设计稿之前的过渡
    方案——窗口本来就能拖边缩放，再放两个按钮只会让人以为不点就看不到内容），
    版式统一走主题层，并改成左右两列，免得在小窗口里上下翻。
    """

    def __init__(self, uploader, log_fetcher, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看数据")
        self.setModal(False)
        self.resize(1040, 620)
        self.setMinimumSize(780, 460)
        self.uploader = uploader
        self.log_fetcher = log_fetcher
        self.fetch_dir = log_fetcher.fetch_dir
        self.upload_thread = None
        self.upload_in_progress = False
        # 批量删除的状态：统计体积 → 确认 → 删除，两段都在后台线程里跑
        self._local_threads = []
        self._delete_ctx = None
        self.delete_in_progress = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])
        layout.addLayout(self._build_toolbar())

        columns = QHBoxLayout()
        columns.setSpacing(SP["sp3"])
        columns.addWidget(self._build_log_panel(), 1)
        columns.addWidget(self._build_bag_panel(), 1)
        layout.addLayout(columns, 1)

        layout.addLayout(self._build_footer())

        self.refresh_lists()

    # ---------- 版式 ----------
    def _build_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])

        title = QLabel("查看数据")
        title.setObjectName("PanelTitle")
        title.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"], letter_spacing=1.0))
        row.addWidget(title)

        # 本地磁盘空间：紧挨标题，打开就能看到"还够不够录"。数据落盘慢和删不删
        # 都取决于这个数字，所以放在最前面，而不是塞进某列的底部小字里。
        self.local_space_label = QLabel("")
        self.local_space_label.setObjectName("Hint")
        self.local_space_label.setToolTip("数据与日志所在分区的本地磁盘占用")
        row.addWidget(self.local_space_label)
        row.addStretch()

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setProperty("variant", "ghost")
        self.btn_refresh.setToolTip("重新读取日志与数据目录")
        self.btn_refresh.clicked.connect(self.refresh_lists)

        self.btn_remote_browser = QPushButton("远程目录")
        self.btn_remote_browser.setProperty("variant", "ghost")
        self.btn_remote_browser.setToolTip("浏览/管理远端 SFTP 目录")
        self.btn_remote_browser.clicked.connect(self.open_remote_browser)

        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_remote_browser)
        return row

    @staticmethod
    def _list_widget():
        """统一的列表控件：等宽字体、多选、支持双击打开。

        多选（ExtendedSelection）是为批量删除准备的：Ctrl/Shift 点选与框选都可用。
        单选语义的操作（上传/播放/打开目录/抓取/查看）各自做"只能选一个"的校验 ——
        多选后沿用 currentItem 会让人以为"选中的都上传了"。
        """
        widget = QListWidget()
        widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        widget.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        widget.setAlternatingRowColors(True)
        return widget

    def _build_log_panel(self):
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.btn_fetch_selected = QPushButton("抓取选中日志")
        self.btn_fetch_selected.setProperty("variant", "primary")
        self.btn_fetch_selected.clicked.connect(self.fetch_selected_log)
        self.btn_view_selected_log = QPushButton("查看选中日志")
        self.btn_view_selected_log.setProperty("variant", "ghost")
        self.btn_view_selected_log.clicked.connect(self.view_selected_log)
        # 日志列原先只能看、不能删 —— 抓取的日志会一直堆在本地
        self.btn_delete_selected_log = QPushButton("删除选中")
        self.btn_delete_selected_log.setProperty("variant", "danger")
        self.btn_delete_selected_log.setToolTip("删除本地已抓取的日志文件（支持多选）")
        self.btn_delete_selected_log.clicked.connect(self.delete_selected_logs)

        head, _ = make_panel_head("日志文件", [self.btn_fetch_selected,
                                               self.btn_view_selected_log,
                                               self.btn_delete_selected_log])
        outer.addWidget(head)

        self.log_list = self._list_widget()
        self.log_list.itemDoubleClicked.connect(self._on_log_double_click)
        outer.addWidget(self.log_list, 1)

        hint = QLabel(f"源目录 {self.log_fetcher.log_dir} · 抓取到 {self.fetch_dir}")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return panel

    def _build_bag_panel(self):
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.btn_upload_selected = QPushButton("上传选中")
        self.btn_upload_selected.setProperty("variant", "primary")
        self.btn_upload_selected.clicked.connect(self.upload_selected_bag)
        self.btn_play_bag = QPushButton("播放选中")
        self.btn_play_bag.setProperty("variant", "ghost")
        self.btn_play_bag.clicked.connect(self.play_selected_bag)
        self.btn_view_selected_bag = QPushButton("打开目录")
        self.btn_view_selected_bag.setProperty("variant", "ghost")
        self.btn_view_selected_bag.clicked.connect(self.view_selected_bag)
        self.btn_delete_selected_bag = QPushButton("删除选中")
        self.btn_delete_selected_bag.setProperty("variant", "danger")
        self.btn_delete_selected_bag.setToolTip("删除本地数据文件夹（支持多选，删除后不可恢复）")
        self.btn_delete_selected_bag.clicked.connect(self.delete_selected_bags)

        head, _ = make_panel_head("数据文件夹", [self.btn_upload_selected,
                                                self.btn_play_bag,
                                                self.btn_view_selected_bag,
                                                self.btn_delete_selected_bag])
        outer.addWidget(head)

        self.bag_list = self._list_widget()
        self.bag_list.itemDoubleClicked.connect(self._on_bag_double_click)
        outer.addWidget(self.bag_list, 1)

        hint = QLabel(f"目录 {self.uploader.bag_dir} · 双击打开")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return panel

    def _build_footer(self):
        row = QHBoxLayout()
        row.setSpacing(SP["sp3"])

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)

        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("variant", "ghost")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_close)
        return row

    def open_remote_browser(self):
        if not ensure_remote_password(self.uploader, self):
            self.append_log("已取消：未提供远端密码")
            return
        dlg = RemoteDirectoryDialog(self.uploader, self, select_mode=False)
        dlg.exec_()

    def refresh_lists(self):
        self.log_list.clear()
        for f in self.log_fetcher.get_log_files():
            item = QListWidgetItem(f.name)
            item.setData(1, str(f))
            # 颜色/字体交给主题 QSS：这里写死 Qt.blue 会顶掉主题色（遗留问题）
            self.log_list.addItem(item)

        self.bag_list.clear()
        for d in self.uploader.get_bag_folders():
            item = QListWidgetItem(d.name)
            item.setData(1, str(d))
            self.bag_list.addItem(item)

        self._refresh_local_space()

    # ---------- 本地磁盘空间 ----------
    @staticmethod
    def _existing_anchor(path: Path) -> Path:
        """找一个存在的祖先用于查分区空间。

        首次录制前 bag 目录还不存在，直接 disk_usage 会抛 OSError —— 那时
        用户恰恰最需要知道"这块盘还剩多少"。
        """
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        return p

    def _refresh_local_space(self):
        """显示数据 / 日志目录所在分区的占用。

        按**设备号**去重：bag 与 log 通常在同一分区，同分区只显示一次 —— 否则
        同一块盘的数字在界面上出现两遍，反而让人怀疑是不是两块盘。

        目录还不存在时（首次录制前）用最近的已存在祖先查询：那时用户恰恰最需要
        知道"这块盘还够不够录"。标签用实际查询的路径，避免"数据分区"这种说法
        在同盘时把日志的占用也算进去却只提到数据。
        """
        parts, seen = [], set()
        for path in (self.uploader.bag_dir, self.log_fetcher.log_dir):
            anchor = self._existing_anchor(path)
            try:
                dev = os.stat(str(anchor)).st_dev
            except OSError:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            try:
                usage = shutil.disk_usage(str(anchor))
            except OSError as exc:
                logger.debug(f"读取本地磁盘空间失败（{anchor}）：{exc}")
                continue
            parts.append(
                f"本地空间（{anchor}）已用 {human_size(usage.used)}"
                f" / {human_size(usage.total)} · 可用 {human_size(usage.free)}")

        self.local_space_label.setText("    ".join(parts) if parts else "本地空间：暂不可用")

    @staticmethod
    def _selected_paths(widget):
        """列表里选中的全部路径（批量操作用）。"""
        return [Path(i.data(1)) for i in widget.selectedItems()]

    def _one_or_warn(self, widget, noun):
        """单选类操作的取值入口：没选或多选都给明确提示。

        列表改成多选之后，继续沿用 currentItem 会让人以为"选中的都被处理了" ——
        上传 / 播放 / 打开目录这类操作只作用于一个对象，必须说清楚。
        """
        items = widget.selectedItems()
        if not items:
            QMessageBox.warning(self, "提示", f"请先选择{noun}")
            return None
        if len(items) > 1:
            QMessageBox.warning(
                self, "提示",
                f"该操作一次只能选一个{noun}（当前选中 {len(items)} 个）")
            return None
        return Path(items[0].data(1))

    def _get_selected_log(self):
        return self._one_or_warn(self.log_list, "一个日志文件")

    def _get_selected_bag(self):
        return self._one_or_warn(self.bag_list, "一个数据文件夹")

    def fetch_selected_log(self):
        file_path = self._get_selected_log()
        if not file_path:
            return
        success = self.log_fetcher.fetch_specific_log(file_path)
        if success:
            QMessageBox.information(self, "成功", f"日志已抓取到 {self.fetch_dir}")
        else:
            QMessageBox.critical(self, "失败", "日志抓取失败，请查看日志")

    def view_selected_log(self):
        file_path = self._get_selected_log()
        if not file_path:
            return
        self._open_file(file_path)

    def _get_launcher(self):
        """复用主窗口的 Launcher（脚本路径/环境变量统一由它提供）。"""
        parent = self.parent()
        launcher = getattr(parent, "launcher", None)
        if launcher is not None:
            return launcher
        config = getattr(parent, "config", None)
        return Launcher(config) if config is not None else None

    def play_selected_bag(self):
        """播放选中的 bag 数据（统一走 Launcher -> scripts/play_bag.sh）"""
        launcher = self._get_launcher()
        if launcher is None:
            QMessageBox.critical(self, "错误", "无法获取配置，请检查")
            return
        if _warn_if_main_program_running(self, launcher.config):
            return

        folder_path = self._get_selected_bag()
        if not folder_path:
            return

        _play_bag_folder(self, launcher, folder_path, self.append_log)

    def upload_selected_bag(self):
        if self.delete_in_progress:
            QMessageBox.information(self, "提示", "删除进行中，暂不能上传")
            return
        if self.upload_in_progress:
            self._cancel_upload_in_dialog()
            return
        folder_path = self._get_selected_bag()
        if not folder_path:
            return
        # 与「上传数据」按钮走同一个分流点：disk 目标不问密码、不连远端
        subpath = resolve_upload_subpath(self.uploader, self)
        if subpath is None:
            self.append_log("已取消上传")
            return
        self.append_log(f"开始导出数据，目标：{describe_export_target(self.uploader)}")

        # --- 新增：显示父窗口的进度控件 ---
        if self.parent() and hasattr(self.parent(), 'show_upload_progress'):
            self.parent().show_upload_progress(True)

        self._set_busy_controls(True)
        self.btn_upload_selected.setText("取消上传")
        theme.set_variant(self.btn_upload_selected, "danger")
        self.upload_in_progress = True
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(0)
        if self.parent() and hasattr(self.parent(), "progress_signal"):
            self.parent().progress_signal.emit("准备打包并上传...")

        def progress_msg(text):
            if self.parent() and hasattr(self.parent(), 'progress_signal'):
                self.parent().progress_signal.emit(text)

        self.upload_thread = UploadThread(
            self.uploader,
            lambda **kw: self.uploader.upload_specific_bag(
                folder_path, remote_subpath_arg(subpath), **kw),
            progress_msg)
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.upload_thread.progress_signal.connect(self.parent().update_upload_progress)
        self.upload_thread.finished.connect(self._on_upload_finished)
        self.upload_thread.start()

    # ---------- 上传期间的按钮联动 ----------
    def _busy_controls(self):
        """上传进行中需要禁用的按钮。

        原先这组按钮在「开始上传 / 取消上传 / 上传结束」三处各写一遍，既容易失步，
        也确实漏掉了 btn_play_bag 与 btn_remote_browser —— 上传中还能启播放、
        打开远端浏览，等于边传边改同一批数据。
        """
        return (self.btn_refresh, self.btn_fetch_selected,
                self.btn_view_selected_log, self.btn_view_selected_bag,
                self.btn_delete_selected_bag, self.btn_delete_selected_log,
                self.btn_play_bag, self.btn_remote_browser)

    def _set_busy_controls(self, busy: bool) -> None:
        for button in self._busy_controls():
            if button is not None:
                button.setEnabled(not busy)

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
            theme.set_variant(self.btn_upload_selected, "warn")
            self._set_busy_controls(False)
            if self.parent() and hasattr(self.parent(), "update_upload_progress"):
                self.parent().update_upload_progress(0)
            self.append_log("上传取消请求已发送，等待当前传输块结束...")

    def _on_upload_finished(self, success, msg):
        self.btn_upload_selected.setEnabled(True)
        self._set_busy_controls(False)
        self.upload_in_progress = False
        self.btn_upload_selected.setText("上传选中")
        theme.set_variant(self.btn_upload_selected, "default")
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

    # ---------- 批量删除 ----------
    def delete_selected_bags(self):
        """删除选中的本地数据文件夹（支持多选）。"""
        self._batch_delete("bag")

    def delete_selected_logs(self):
        """删除选中的本地日志文件（支持多选）。"""
        self._batch_delete("log")

    def _delete_target(self, kind):
        if kind == "log":
            # 日志列列的是 log_dir（源日志目录）里的文件 —— **所见即所删**。
            # 抓取副本（fetch_dir）不在列表里，所以不能拿它当删除目标：
            # 那会让用户删掉看不见的东西，或者对着一列"删不动"的文件发愣。
            return self.log_list, Path(self.log_fetcher.log_dir), "日志文件"
        return self.bag_list, Path(self.uploader.bag_dir), "数据文件夹"

    def _batch_delete(self, kind):
        """批量删除：越界校验 → 后台统计体积 → 确认 → 后台删除。

        刻意分两段后台任务：递归统计几十 GB 的目录要几百毫秒到几秒，`shutil.rmtree`
        也是秒级 —— 跑在主线程上界面会明显卡住。确认框夹在中间，是因为"到底要删掉
        多少"必须让用户先看到再决定，这也是唯一能防住手滑的地方。
        """
        if self.delete_in_progress:
            QMessageBox.information(self, "提示", "上一次删除还在进行中")
            return
        if self.upload_in_progress:
            # 正在上传的正是这批数据，边传边删会得到莫名其妙的失败
            QMessageBox.information(self, "提示", "上传进行中，暂不能删除数据")
            return
        widget, root, noun = self._delete_target(kind)
        targets = self._selected_paths(widget)
        if not targets:
            QMessageBox.warning(self, "提示", f"请先选择要删除的{noun}")
            return

        # 越界校验：只允许删配置目录里的东西。路径可能来自配置被改错或列表数据被
        # 污染，先拦住比删完再后悔便宜得多。
        outside = [p for p in targets if not is_within(p, root)]
        if outside:
            QMessageBox.critical(
                self, "拒绝删除",
                f"以下路径不在允许的目录（{root}）内，已中止：\n\n"
                + "\n".join(str(p) for p in outside[:5]))
            return

        self._delete_ctx = {"targets": targets, "noun": noun}
        self._set_delete_busy(True)
        self.status_label.setText(f"正在统计 {len(targets)} 项的占用…")
        self._start_local_task(lambda: self._measure(targets), self._on_delete_measured)

    @staticmethod
    def _measure(targets):
        """统计选中项的总占用（阻塞，必须在后台线程里跑）。"""
        total = 0
        for path in targets:
            try:
                total += dir_size(path) if path.is_dir() else path.stat().st_size
            except OSError:
                continue    # 统计不准好过弹异常：用户要的是"大概多大"
        return total

    @staticmethod
    def _remove(targets):
        """逐项删除并把结果分开汇报。

        一项失败不影响其余：中途放弃会让用户只能反复试，还得自己记住哪些删掉了。
        """
        done, failed = [], []
        for path in targets:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                done.append(path.name)
            except OSError as exc:
                failed.append(f"{path.name}：{exc}")
        return done, failed

    def _start_local_task(self, task, on_finished):
        """启动一个本地后台任务。

        两个必须守住的点：
        1. **不把线程挂到窗口上当子对象**（LocalTaskThread 因此不接收 parent）。
           挂上去之后，Qt 销毁窗口时会连带销毁线程对象；若那一刻线程还在收尾，
           就是 `processEvents()` 里的 C 层段错误 —— 实测只在跑了数十个界面测试
           之后复现（单跑不崩），靠 gdb 也抓不到（沙箱禁 ptrace），最终用"同步执行
           版的对照实验"定位到线程归属。
        2. **线程对象一直被引用到完全退出为止**：用列表持有，并在启动新任务前回收
           已结束的线程。这条路径会连跑两段后台任务（统计 → 删除），引用管理一乱
           就容易在中间态丢引用。
        """
        self._reap_local_threads()
        thread = LocalTaskThread(task)
        thread.finished.connect(on_finished)
        self._local_threads.append(thread)
        thread.start()

    def _reap_local_threads(self):
        """回收已结束的线程（此时释放引用是安全的）。"""
        for thread in list(self._local_threads):
            if thread.isFinished():
                thread.wait(1000)
                self._local_threads.remove(thread)

    def _on_delete_measured(self, ok, payload, message):
        if not ok:
            self._finish_delete()
            QMessageBox.critical(self, "失败", f"统计占用失败：{message}")
            return
        ctx = self._delete_ctx or {}
        targets = ctx.get("targets", [])
        noun = ctx.get("noun", "项目")
        shown = "\n".join(p.name for p in targets[:10])
        if len(targets) > 10:
            shown += f"\n… 其余 {len(targets) - 10} 项"

        reply = QMessageBox.question(
            self, "确认删除",
            f"将删除 {len(targets)} 个{noun}，共占 {human_size(payload)}：\n\n{shown}\n\n"
            f"此操作不可恢复（不走回收站），确定继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self._finish_delete()
            self.append_log(f"已取消删除 {len(targets)} 个{noun}")
            return

        self.status_label.setText(f"正在删除 {len(targets)} 个{noun}…")
        self._start_local_task(lambda: self._remove(targets), self._on_delete_done)

    def _on_delete_done(self, ok, payload, message):
        self._finish_delete()
        if not ok:
            QMessageBox.critical(self, "删除失败", message)
            return
        done, failed = payload
        self.append_log(f"已删除 {len(done)} 项；失败 {len(failed)} 项")
        self.refresh_lists()            # 顺带刷新本地磁盘空间
        if failed:
            QMessageBox.warning(
                self, "部分删除失败",
                f"成功 {len(done)} 项，失败 {len(failed)} 项：\n\n"
                + "\n".join(failed[:10]))
        else:
            QMessageBox.information(self, "删除完成", f"已删除 {len(done)} 项")

    def _set_delete_busy(self, busy: bool) -> None:
        self.delete_in_progress = busy
        self._set_busy_controls(busy)
        # 删除进行中不允许关窗：线程还没跑完就销毁对话框，轻则报错重则半删状态
        self.btn_close.setEnabled(not busy)

    def _finish_delete(self):
        self._delete_ctx = None
        self.status_label.setText("")
        self._set_delete_busy(False)

    # ---------- 关闭保护 ----------
    def _refuse_close_while_deleting(self) -> bool:
        """删除进行中不允许关窗：对话框被销毁时后台线程还在跑，会直接崩。

        关闭按钮在 `_set_delete_busy` 里已经禁用，但 Esc 与窗口的 X 绕得过去，
        所以两个入口都要挡一次。
        """
        if not self.delete_in_progress:
            return False
        QMessageBox.information(self, "提示", "删除正在进行，请稍候")
        return True

    def closeEvent(self, event):
        if self._refuse_close_while_deleting():
            event.ignore()
            return
        for thread in list(self._local_threads):
            if thread.isRunning():
                thread.wait(3000)
        super().closeEvent(event)

    def reject(self):
        if self._refuse_close_while_deleting():
            return
        super().reject()

    def _open_file(self, file_path: Path):
        try:
            subprocess.Popen(["gedit", str(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.debug(f"gedit 不可用（{exc}），回退到系统默认程序")
            QDesktopServices.openUrl(file_path.as_uri())

    def _open_folder(self, folder_path: Path):
        try:
            subprocess.Popen(["nautilus", str(folder_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.debug(f"nautilus 不可用（{exc}），回退到系统默认程序")
            QDesktopServices.openUrl(folder_path.as_uri())

    def _on_log_double_click(self, item):
        file_path = Path(item.data(1))
        self._open_file(file_path)

    def _on_bag_double_click(self, item):
        folder_path = Path(item.data(1))
        self._open_folder(folder_path)

    def append_log(self, text):
        if self.parent() and hasattr(self.parent(), 'append'):
            self.parent().append(text)
