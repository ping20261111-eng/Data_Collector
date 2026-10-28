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
from ..core_data import (UploadBatchQueue, UploadCancelledError,
                         sort_bags_by_capture_time)
from ..verify import read_bag_verdict
from . import bag_verdict
from . import bag_row_delegate


from .common import (SP, describe_export_target, ensure_remote_password,
                    make_panel_head, remote_subpath_arg, resolve_upload_subpath,
                    _warn_if_main_program_running, _play_bag_folder)
from .threads import (UploadThread, LocalTaskThread)
from .remote_browser import (RemoteDirectoryDialog)



class DataViewDialog(QDialog):
    #: 批量验证「每完成一条」的通知（v6.9.0）。
    #:
    #: 在工作线程里 emit（`verify_bag_folders(on_bag_done=...)` 的调用点），
    #: 主线程的槽只做一件事：**把那一行**的判定重画。
    #:
    #: 为什么不用 `finished` 一把刷完：一次几十份，串行跑就是十几分钟，
    #: 期间列表一个标记都不动，用户没法知道"跑到哪了、已经判了几条不合格"。
    #: 也不能在回调里直接改 item —— 那是工作线程碰控件，Qt 会直接崩。
    bag_verified = pyqtSignal(object)

    """查看数据：左边机器人上的日志文件，右边本机已录制的 bag 目录。

    2026-09-18 重构：去掉遗留的「放大窗口 / 缩小窗口」按钮（那是设计稿之前的过渡
    方案——窗口本来就能拖边缩放，再放两个按钮只会让人以为不点就看不到内容），
    版式统一走主题层，并改成左右两列，免得在小窗口里上下翻。
    """

    def __init__(self, uploader, log_fetcher, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看数据")
        self.setModal(False)
        # 宽度的下限定在**内容最小需要的宽度之上**：1.0 缩放下这一页的内容最小是
        # 1049（左右两列各 432 + 间距），原来给 1040 —— 差 9px，Qt 就把「上传选中」
        # 这类按钮各挤掉 4px（文字贴边）。留出余量，别让"刚好差一点"变成默认状态。
        self.resize(theme.px(1100), theme.px(620))
        self.setMinimumSize(theme.px(1060), theme.px(470))
        self.uploader = uploader
        self.log_fetcher = log_fetcher
        self.fetch_dir = log_fetcher.fetch_dir
        self.upload_thread = None
        self.upload_in_progress = False
        # 批量上传的队列状态机（v6.6.0；纯逻辑在 `core_data.UploadBatchQueue`）。
        # 单个上传也走它（队列长度 1）—— 只有"弹不弹逐条提示"不同，见 `_finish_batch`。
        self._upload_batch = None
        self._upload_subpath = ""
        # 批量删除的状态：统计体积 → 确认 → 删除，两段都在后台线程里跑
        self._local_threads = []
        self._delete_ctx = None
        self.delete_in_progress = False
        # 批量数据验证（v6.7.0）：串行跑，每份报告写回它自己的目录。
        # `_verify_stop` 只会在**两个 bag 之间**被检查 —— 打断正在跑的那一份
        # 只会留下半截报告，比"多等一分钟"糟得多。
        self._verify_thread = None
        self._verify_stop = False
        #: 本批"还没出结果"的 bag 路径。验证开始时全部填入，每完成一条去掉一条。
        #: 收尾时按它把剩余行恢复成磁盘上的真实判定 —— 取消/中断后那些行还挂着
        #: 临时的「待验证」，不恢复就成了假信息。
        self._verify_pending = set()
        self.bag_verified.connect(self._on_bag_verified)
        self.verify_in_progress = False

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
        self.btn_verify_selected = QPushButton("批量验证")
        self.btn_verify_selected.setProperty("variant", "ghost")
        self.btn_verify_selected.setToolTip(
            "对选中的数据文件夹逐个做数据验证（话题帧率 / 时间戳），\n"
            "结果写到各自的数据目录里（数据验证结果_<时间>.txt）；支持多选")
        self.btn_verify_selected.clicked.connect(self.verify_selected_bags)
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
                                                self.btn_verify_selected,
                                                self.btn_play_bag,
                                                self.btn_view_selected_bag,
                                                self.btn_delete_selected_bag])
        outer.addWidget(head)

        self.bag_list = self._list_widget()
        # 三列按固定列位画（名字 / 时间 / 判定）。**必须挂在列表上而不是靠
        # 空格对齐**：名字长短不一，时间与判定的起点会跟着漂（v6.9.1 现场反馈）。
        self.bag_list.setItemDelegate(bag_row_delegate.BagRowDelegate(self.bag_list))
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
        # 这是本对话框里**唯一**真正需要远端密码的动作（浏览服务器上的目录）。
        # 其余功能（看本地 bag、看本地日志、播放、导出到移动硬盘）都不联网，
        # 所以问密码只放在这里，而不是打开对话框就先问一遍。
        if not ensure_remote_password(self.uploader, self):
            self.append_log("已取消：未提供远端密码（浏览远端目录需要）")
            return
        dlg = RemoteDirectoryDialog(self.uploader, self, select_mode=False)
        dlg.exec_()

    def refresh_lists(self):
        """重扫两个列表：**最新在最上面**，并且每条都带时间（v6.7.7）。

        ## 为什么排序不能沿用"目录枚举顺序"

        `get_bag_folders()` 是按 **mtime** 倒序的。而 mtime 是"目录最后被改动的
        时间"，与录制开始时间不是一回事：长录制时它≈结束时间，从别的机器拷来的
        会被改成拷贝时间 —— 于是列表顺序会把"较晚录的"排到下面，时间也显示错的。

        现在统一按 `resolve_capture_time()` 得到的**录制时间**排序（metadata 的
        `starting_time` → 目录名时间戳 → mtime 兜底），并把时间直接写在条目里，
        操作员不用再靠目录名猜。

        日志列表同理：文件名的修改时间就是它的写入时间，直接显示即可。

        ## 数据列表末尾还有判定列（v6.9.0）

        每行是 `名字    录制时间    判定`，判定取该目录里**最新一份**验证报告的结论
        （合格 / 不合格 / 未验证）。它由 `_apply_bag_row` 统一渲染 —— 逐条更新
        （`_on_bag_verified`）和整表刷新都走同一个函数，不会出现两种口径。
        """
        self.log_list.clear()
        for f in self.log_fetcher.get_log_files():
            try:
                stamp = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M:%S")
            except OSError:
                stamp = "—"
            item = QListWidgetItem(f"{f.name}    {stamp}")
            item.setData(bag_verdict.PATH_ROLE, str(f))
            # 颜色/字体交给主题 QSS：这里写死 Qt.blue 会顶掉主题色（遗留问题）
            self.log_list.addItem(item)

        self.bag_list.clear()
        # 按录制时间倒序：最新的在最上面（排序逻辑在 core_data 里，可单测）
        for captured, folder in sort_bags_by_capture_time(self.uploader.get_bag_folders()):
            self._add_bag_row(folder, captured)
        # 按当前最长名字给"名字列"定宽（只在整表刷新时量一次，逐条更新不重排）。
        bag_row_delegate.fit_columns(self.bag_list)

        self._refresh_local_space()

    # ---------- 数据行的判定列（v6.9.0） ----------

    def _add_bag_row(self, folder, captured):
        """新建一行。

        `data(2)` 存**已格式化的时间文本**，不是 datetime 对象：Qt 的 `setData`
        会把它过一遍 QVariant，取回来已经不是 Python 的 datetime 了
        （实测 `f"{value:%H:%M}"` 直接抛 `Invalid format specifier`）。
        逐条更新时要的只是这段文本，存文本最省事也最不容易再踩。
        """
        item = QListWidgetItem()
        item.setData(bag_verdict.PATH_ROLE, str(folder))
        item.setData(bag_verdict.STAMP_ROLE, f"{captured:%m-%d %H:%M:%S}")
        self._apply_bag_row(item, Path(folder), item.data(bag_verdict.STAMP_ROLE))
        self.bag_list.addItem(item)
        return item

    def _apply_bag_row(self, item, folder, stamp, pending: bool = False) -> None:
        """重设一行的文本 / 提示 / 颜色。渲染细节都在 `ui.bag_verdict` 里。"""
        verdict = read_bag_verdict(folder)
        item.setText(bag_verdict.row_text(Path(folder).name, stamp,
                                          bag_verdict.label(verdict, pending)))
        item.setToolTip(bag_verdict.tooltip(verdict))
        item.setForeground(bag_verdict.foreground(verdict, pending))

    def _find_bag_row(self, folder):
        """按 `data(1)` 里存的路径找行 —— 绝不按文本匹配（文本里现在有判定列）。"""
        target = str(folder)
        for index in range(self.bag_list.count()):
            item = self.bag_list.item(index)
            if item.data(bag_verdict.PATH_ROLE) == target:
                return item
        return None

    def _refresh_bag_row(self, folder) -> None:
        item = self._find_bag_row(folder)
        if item is None:
            return
        self._apply_bag_row(item, Path(folder), item.data(bag_verdict.STAMP_ROLE))

    def _mark_row_pending(self, folder) -> None:
        item = self._find_bag_row(folder)
        if item is None:
            return
        self._apply_bag_row(item, Path(folder), item.data(bag_verdict.STAMP_ROLE), pending=True)

    def _on_bag_verified(self, result) -> None:
        """主线程槽：批量验证每完成一条就更新那一行。

        只动这一行，不 `clear()` 重建列表 —— 重建会丢选中、丢滚动位置，
        而用户在验证期间往往正拿这个列表做别的判断。
        """
        path = str((result or {}).get("bag") or "")
        if not path:
            return
        self._verify_pending.discard(path)
        self._refresh_bag_row(path)

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
        return [Path(i.data(bag_verdict.PATH_ROLE)) for i in widget.selectedItems()]

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
        return Path(items[0].data(bag_verdict.PATH_ROLE))

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
        """上传选中的数据文件夹 —— **支持多选 = 批量**（v6.6.0）。

        批量不是"把按钮改成循环"这么简单，三处必须一次想清：

        1. **远端目标只问一次**：`resolve_upload_subpath` 会弹密码/选目标，
           每个目录问一次等于让现场输 N 遍密码。整批复用同一个 subpath。
        2. **失败不中断**：一个目录失败不代表后面的不能用 —— 传完再汇总。
           否则"传到第 4 个断了"，剩下 20 个要重新一个个点。
        3. **成功不逐条弹窗**：5 个目录弹 5 个模态框，每次都要点"确定"、
           每次都挡住进度。批量只在结束时弹一次汇总。
        """
        if self.delete_in_progress:
            QMessageBox.information(self, "提示", "删除进行中，暂不能上传")
            return
        if self.upload_in_progress:
            self._cancel_upload_in_dialog()
            return
        folders = self._selected_paths(self.bag_list)
        if not folders:
            QMessageBox.warning(self, "提示", "请先选择数据文件夹")
            return
        # 与「上传数据」按钮走同一个分流点：disk 目标不问密码、不连远端
        subpath = resolve_upload_subpath(self.uploader, self)
        if subpath is None:
            self.append_log("已取消上传")
            return

        self._upload_batch = UploadBatchQueue(folders)
        self._upload_subpath = subpath

        self.append_log(f"开始上传 {len(folders)} 个数据文件夹，"
                        f"目标：{describe_export_target(self.uploader)}")
        if self.parent() and hasattr(self.parent(), 'show_upload_progress'):
            self.parent().show_upload_progress(True)
        self._set_busy_controls(True)
        self._start_next_upload()

    # ---------- 批量上传（v6.6.0） ----------
    def _start_next_upload(self):
        """从队列取下一个开始传；队列空了就收尾（**串行，不并行**）。"""
        batch = self._upload_batch
        folder = batch.next() if batch is not None else None
        if folder is None:
            self._finish_batch()
            return
        multi = batch.total > 1
        self.upload_in_progress = True
        self.btn_upload_selected.setText("取消上传")
        self.btn_upload_selected.setEnabled(True)
        theme.set_variant(self.btn_upload_selected, "danger")
        if multi:
            self.append_log(f"（{batch.done + 1}/{batch.total}）正在上传：{folder.name}")
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(batch.percent(0))
        if self.parent() and hasattr(self.parent(), "progress_signal"):
            self.parent().progress_signal.emit(
                f"上传 {batch.done + 1}/{batch.total}：{folder.name}" if multi
                else "准备打包并上传...")

        def progress_msg(text):
            if self.parent() and hasattr(self.parent(), 'progress_signal'):
                self.parent().progress_signal.emit(text)

        self.upload_thread = UploadThread(
            self.uploader,
            lambda **kw: self.uploader.upload_specific_bag(
                folder, remote_subpath_arg(self._upload_subpath), **kw),
            progress_msg)
        self.upload_thread.progress_signal.connect(self._on_batch_progress)
        self.upload_thread.finished.connect(self._on_upload_finished)
        self.upload_thread.start()

    def _on_batch_progress(self, value):
        if self._upload_batch is None:
            return
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(self._upload_batch.percent(value))

    def _finish_batch(self):
        """一整批结束（跑完 / 被取消）：恢复控件 + 汇总 + 刷新列表。"""
        batch = self._upload_batch
        self.upload_in_progress = False
        self.btn_upload_selected.setEnabled(True)
        self.btn_upload_selected.setText("上传选中")
        theme.set_variant(self.btn_upload_selected, "default")
        self._set_busy_controls(False)
        if batch is None:
            self.refresh_lists()
            return
        summary = batch.summary()
        all_ok = summary["failed"] == 0
        if self.parent() and hasattr(self.parent(), "update_upload_progress"):
            self.parent().update_upload_progress(100 if all_ok else 0)
        if self.parent() and hasattr(self.parent(), 'show_upload_progress'):
            self.parent().show_upload_progress(False)
        for folder, success, msg in batch.results:
            self.append_log(("✔ " if success else "✖ ") + f"{folder.name}：{msg}")
        if summary["cancelled"] and summary["unstarted"]:
            self.append_log(f"已取消上传：还剩 {summary['unstarted']} 个未开始")

        if summary["total"] <= 1:
            # 单个目录：保持原有行为（现场习惯"传完弹一下"）
            if batch.results:
                _folder, success, msg = batch.results[0]
                if success:
                    QMessageBox.information(self, "成功", msg)
                else:
                    QMessageBox.critical(self, "失败", msg)
        else:
            # 批量：**只弹一次汇总**；失败清单进日志（弹窗放不下长清单）
            if summary["cancelled"]:
                head = (f"已取消。完成 {summary['done']} 个："
                        f"成功 {summary['ok']} 个，失败 {summary['failed']} 个；"
                        f"未开始 {summary['unstarted']} 个。")
            else:
                head = (f"共 {summary['total']} 个：成功 {summary['ok']} 个，"
                        f"失败 {summary['failed']} 个。")
            if summary["failed"]:
                names = "、".join(folder.name for folder, _s, _m in batch.failed[:5])
                more = "…" if summary["failed"] > 5 else ""
                QMessageBox.warning(self, "批量上传完成",
                                    f"{head}\n\n失败：{names}{more}\n"
                                    "（每个失败的具体原因见日志区）")
            else:
                QMessageBox.information(self, "批量上传完成", head)
        self._upload_batch = None
        self.refresh_lists()

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
                self.btn_play_bag, self.btn_remote_browser,
                self.btn_verify_selected)

    def _set_busy_controls(self, busy: bool) -> None:
        for button in self._busy_controls():
            if button is not None:
                button.setEnabled(not busy)

    def _cancel_upload_in_dialog(self):
        """取消上传（上传过程中“上传选中”按钮会变为“取消上传”）。

        批量时它取消的是**整批**：当前这一条中止后不再推进队列（`UploadBatchQueue.cancel`）。
        取消不该被理解成"跳过这一条、继续下一个" —— 那样用户按几次都停不下来。

        注意：本方法必须定义在 DataViewDialog 类层级，调用处是
        self._cancel_upload_in_dialog()，不要再缩进成 upload_selected_bag 的嵌套函数。
        """
        if self.upload_in_progress:
            if self._upload_batch is not None:
                self._upload_batch.cancel()      # 清空**未开始**的，已传完的结果保留
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
        """一条传完：记结果，然后推进队列（队列空了才收尾）。"""
        batch = self._upload_batch
        if batch is not None:
            batch.record(success, msg)
        self.upload_in_progress = False
        if batch is not None and batch.pending:
            # 换引用前先等旧线程真正退出：finished 已发出、run() 正在收尾，
            # 直接覆盖会让运行中的 QThread 被回收（C 层 abort，不是异常）
            if self.upload_thread is not None and self.upload_thread.isRunning():
                self.upload_thread.wait(1500)
            self._start_next_upload()
            return
        self._finish_batch()

    # ---------- 批量数据验证（v6.7.0） ----------
    def verify_selected_bags(self):
        """对选中的数据文件夹**逐个**做数据验证，报告写回各自目录。

        与「数据验证」（工具行那个按钮）的分工：那个是**单份、带图**，用来细看
        某一批的帧率曲线；这里是**多份、只看报告**，用来把一天采的十几批批量过一遍。
        两者跑的是同一个脚本（`bag_rate_stat.py`），只是批量这边带 `--no-plot`。
        """
        if self.verify_in_progress:
            # 按钮在验证期间就是"取消"：只置标志，让它在两个 bag 之间停下。
            self._verify_stop = True
            self.append_log("已请求停止：当前这一份做完就不再继续")
            return
        if self.upload_in_progress or self.delete_in_progress:
            QMessageBox.information(self, "提示", "上传 / 删除进行中，暂不能做数据验证")
            return
        folders = self._selected_paths(self.bag_list)
        if not folders:
            QMessageBox.warning(self, "提示", "请先选择数据文件夹")
            return
        launcher = getattr(self.parent(), "launcher", None) if self.parent() else None
        if launcher is None:
            QMessageBox.warning(self, "提示", "数据验证不可用：主窗口没有提供启动器")
            return

        self._verify_stop = False
        self.verify_in_progress = True
        self.btn_verify_selected.setText("取消验证")
        theme.set_variant(self.btn_verify_selected, "danger")
        self.btn_verify_selected.setEnabled(True)      # 它就是"取消"，必须可点
        self._set_busy_controls(True)
        self.append_log(f"开始数据验证：{len(folders)} 个数据文件夹（结果写回各自目录）")

        # 先把这一批全部标成「待验证」：用户一眼看到范围，也知道自己没选错。
        self._verify_pending = {str(path) for path in folders}
        for path in folders:
            self._mark_row_pending(path)

        def progress(text):
            # **在工作线程里被调**：只能走信号，不能碰控件
            if self.parent() and hasattr(self.parent(), "progress_signal"):
                self.parent().progress_signal.emit(text)

        def on_bag_done(result):
            # 同上，工作线程里被调 —— emit 到主线程由 `_on_bag_verified` 改那一行。
            self.bag_verified.emit(result)

        thread = LocalTaskThread(
            lambda: launcher.verify_bag_folders(
                folders, progress, lambda: self._verify_stop,
                on_bag_done=on_bag_done))
        self._verify_thread = thread
        thread.finished.connect(self._on_verify_finished)
        thread.start()

    def _on_verify_finished(self, ok, payload, message):
        """批量验证收尾：恢复控件 → 逐条写日志 → 弹一次汇总。"""
        self.verify_in_progress = False
        self._verify_stop = False
        self.btn_verify_selected.setText("批量验证")
        theme.set_variant(self.btn_verify_selected, "ghost")
        self._set_busy_controls(False)
        if not isinstance(payload, dict):
            self.append_log(f"数据验证失败：{message or '没有返回结果'}")
            QMessageBox.critical(self, "数据验证失败", message or "没有返回结果")
            return

        results = payload.get("results") or []
        for item in results:
            name = Path(item.get("bag", "")).name
            if item.get("reused"):
                self.append_log(f"↺ {name}：复用已有结果 {Path(item['report']).name}")
            elif item.get("ok"):
                self.append_log(f"✔ {name}：报告 {Path(item['report']).name}")
            else:
                self.append_log(f"✖ {name}：{item.get('reason') or '未完成'}")

        total = int(payload.get("total") or 0)
        done = int(payload.get("done") or 0)
        succeeded = int(payload.get("succeeded") or 0)
        failed = int(payload.get("failed") or 0)
        reused = int(payload.get("reused") or 0)
        stopped = bool(payload.get("stopped"))
        # 跑完的刚才已逐条更新过；这里只把**没跑到**的（取消 / 中断）恢复成磁盘上
        # 的真实判定 —— 否则它们会一直挂着临时的「待验证」，那是假信息。
        # 刻意**不** `refresh_lists()`：重建列表会丢选中与滚动位置，而验证既不改
        # bag 集合、也不改录制时间，没有重建的必要。
        for path in list(self._verify_pending):
            self._refresh_bag_row(path)
        self._verify_pending = set()

        if total and not done and not reused and not stopped:
            QMessageBox.critical(self, "数据验证未开始",
                                 payload.get("reason") or "数据验证没能启动")
            return
        failed_names = [Path(item["bag"]).name for item in results if not item.get("ok")]
        if stopped:
            head = f"已停止：验证 {done} 个（成功 {succeeded} 个、失败 {failed} 个）"
        else:
            head = f"共 {total} 个：验证 {done} 个（成功 {succeeded} 个、失败 {failed} 个）"
        if reused:
            # 复用的要单独说：否则"共 N 个、只验了 3 个"看着像漏了一批。
            head += f"，复用已有结果 {reused} 个（未重跑）"
        if reused and not done:
            # 一条都没跑，必须说清楚原因 —— 否则现场看到"进度条一闪就结束"，
            # 会以为按钮坏了或验证没生效。
            QMessageBox.information(
                self, "全部复用已有结果",
                head + "\n\n这些目录里已有「不早于数据」的验证报告，因此没有重复验证。\n"
                "如需重验，删掉目录里的「数据验证结果_*.txt」再点批量验证。")
            return

        detail = "\n\n报告已写入各自的数据目录：" +                  "\n".join(Path(item["report"]).name for item in results if item.get("ok")) \
                   if succeeded else ""
        if failed_names:
            more = "…" if len(failed_names) > 5 else ""
            QMessageBox.warning(self, "数据验证完成",
                                f"{head}\n\n失败：{'、'.join(failed_names[:5])}{more}"
                                "\n（原因见日志区）" + detail)
        else:
            QMessageBox.information(self, "数据验证完成", head + detail)

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
        file_path = Path(item.data(bag_verdict.PATH_ROLE))
        self._open_file(file_path)

    def _on_bag_double_click(self, item):
        folder_path = Path(item.data(bag_verdict.PATH_ROLE))
        self._open_folder(folder_path)

    def append_log(self, text):
        if self.parent() and hasattr(self.parent(), 'append'):
            self.parent().append(text)
