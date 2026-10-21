"""「版本管理」对话框：看当前工作空间 → 列远端版本 → 下载解压 → 按需切换。

## 为什么这四件事放在同一个框里

现场的心智模型只有一句"我要换一个版本"。拆成"先选目录 → 再看列表 → 再下载 →
再切换"四个弹窗，换一次版本要点四遍，中间漏一步就会得到"下好了但没切过去"
"切过去了但看的是旧目录"这类说不清的状态。这里一次点开、选一行、点一次按钮。

## 几个刻意的取舍

- **打开时才拉列表**，不在启动时拉：网络不通不该拖慢开界面；且版本列表几分钟就过期，
  启动时拉的到用时已经旧了。
- **界面线程只做渲染**：下载 290MB + 解压 bzip2 是分钟级操作，全在后台线程，
  主线程不碰网络也不碰 tarfile。
- **列表里的"已下载"是直接切换**，不是重新下载：目录已经在本地了，再点一次只会撞上
  `TARGET_EXISTS`（程序拒绝覆盖 —— 那极可能就是正在使用的工作空间）。
- **默认选中第一个"还没下过"的版本**，而不是永远选最新：最新那个往往正是当前在用的，
  预选它等于预选一个点下去会报错的按钮。
"""
import time
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QProgressBar, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import theme
from ..core_release import human_size, inspect_local
from .common import SP
from .safety import guard_slot
from .threads import ReleaseFetchThread, ReleaseTaskThread

COLUMNS = ("版本", "类型", "打包时间", "大小", "本地状态")
#: 列对齐。**表头必须跟着数据走** —— QTableWidget 的表头默认居中、数据默认左对齐，
#: 不显式统一就会出现"版本"标题居中、"wheeled_robot…"左对齐的错位观感。
COLUMN_ALIGN = (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignHCenter,
                Qt.AlignHCenter, Qt.AlignHCenter)


def _fmt_duration(seconds: float) -> str:
    """剩余时间。只到"分"的粒度 —— 现场看的是"还要不要等"，不是秒表。"""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes:02d} 分"


class ReleaseDialog(QDialog):
    """版本管理：查看 / 下载 / 解压 / 切换工作空间。"""

    workspace_changed = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.repo = config.get_version_repo()
        # 表格行要用主题色（"使用中"那行给个底色），而对话框自己不知道该用哪套主题：
        # 问父窗口要，拿不到就按明色（默认主题）
        self.theme_mode = getattr(parent, "theme_mode", "light") or "light"
        self.releases = []
        self.fetch_thread = None
        self.task_thread = None
        self._stage = ""
        self._task_started_at = 0.0
        self._session_base = 0        # 续传时本次任务开始处的偏移，用来算真实速度

        self.setWindowTitle("版本管理")
        self.setModal(True)
        self.resize(theme.px(1000), theme.px(760))
        self._build_ui()
        self._refresh_current()
        self.reload()

    # ---------- 界面 ----------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])
        layout.addWidget(self._build_current_box())
        layout.addWidget(self._build_remote_box(), 1)
        layout.addWidget(self._build_target_box())
        layout.addWidget(self._build_progress_box())
        layout.addLayout(self._build_buttons())

    def _build_current_box(self) -> QGroupBox:
        box = QGroupBox("当前工作空间")
        grid = QGridLayout(box)
        grid.setSpacing(SP["sp2"])

        self.current_path_label = QLabel("—")
        self.current_path_label.setObjectName("FactValue")
        self.current_path_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self.current_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current_path_label.setWordWrap(True)
        grid.addWidget(self.current_path_label, 0, 0, 1, 3)

        self.current_meta_label = QLabel("—")
        self.current_meta_label.setObjectName("FactLabel")
        self.current_meta_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        grid.addWidget(self.current_meta_label, 1, 0)

        self.btn_pick_local = QPushButton("切换本地目录…")
        self.btn_pick_local.setToolTip("指向本地已有的工作空间目录（含 install/ 与 doc/version.txt）")
        self.btn_pick_local.clicked.connect(self.on_pick_local_workspace)
        grid.addWidget(self.btn_pick_local, 1, 1)

        self.btn_rescan = QPushButton("重新检测")
        self.btn_rescan.setToolTip("重新读取版本号与本地状态（在别处改过工作空间后用）")
        self.btn_rescan.clicked.connect(self.on_rescan)
        grid.addWidget(self.btn_rescan, 1, 2)
        grid.setColumnStretch(0, 1)
        return box

    def _build_remote_box(self) -> QGroupBox:
        box = QGroupBox("远端可用版本")
        outer = QVBoxLayout(box)
        outer.setSpacing(SP["sp2"])

        head = QHBoxLayout()
        self.remote_status_label = QLabel("正在读取…")
        self.remote_status_label.setObjectName("FactValue")
        head.addWidget(self.remote_status_label)
        head.addStretch()
        self.repo_label = QLabel(self.repo["url"] or "未配置 version_repo_url")
        self.repo_label.setObjectName("FactLabel")
        self.repo_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        head.addWidget(self.repo_label)
        outer.addLayout(head)

        # 读取失败时的**多行**说明（原文 / 归属 / 建议）。
        #
        # ⚠️ 这段**不能塞进上面那一行**：`describe()` 的文案是**三行**、200+ 字
        # （实测 218 字），原先把它 `.replace("\n", "　")` 压成一行塞进 `repo_label` ——
        # 后果是那个标签需要 1905px，把 503px 的对话框**撑到 1085px**（现场那台机上
        # 更是整行溢出到窗口外，且"读取版本列表失败"与原因粘成一句话分不清）。
        #
        # 现在：`repo_label` **一直显示仓库地址**（它是"这是哪个地址"，不该被错误覆盖），
        # 失败原因另起一段、允许换行。
        self.remote_error_label = QLabel()
        self.remote_error_label.setObjectName("FactLabel")
        # 纯文本：错误原文里有 `<urlopen error ...>` 这种尖括号，富文本会自动吞掉它
        self.remote_error_label.setTextFormat(Qt.PlainText)
        self.remote_error_label.setWordWrap(True)
        self.remote_error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.remote_error_label.setVisible(False)
        outer.addWidget(self.remote_error_label)

        filters = QHBoxLayout()
        filters.setSpacing(SP["sp2"])
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("搜索时间戳或后缀，如 0917 / end / test")
        self.edit_filter.textChanged.connect(self.on_filter_changed)
        filters.addWidget(self.edit_filter, 1)

        self.cmb_kind = QComboBox()
        self.cmb_kind.addItem("全部类型")
        self.cmb_kind.setMinimumWidth(theme.px(120))
        self.cmb_kind.currentIndexChanged.connect(self.on_filter_changed)
        filters.addWidget(self.cmb_kind)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        filters.addWidget(self.btn_refresh)
        outer.addLayout(filters)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for column in range(len(COLUMNS)):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(COLUMN_ALIGN[column] | Qt.AlignVCenter)
        outer.addWidget(self.table, 1)
        return box

    def _build_target_box(self) -> QGroupBox:
        box = QGroupBox("下载与解压")
        outer = QVBoxLayout(box)
        outer.setSpacing(SP["sp2"])

        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        row.addWidget(QLabel("下载到"))
        self.edit_target = QLineEdit(str(self.repo["download_dir"]))
        self.edit_target.setReadOnly(True)
        self.edit_target.setToolTip("归档下到这里，解压成 <目录>/<包名>/<工作空间>")
        row.addWidget(self.edit_target, 1)
        self.btn_pick_target = QPushButton("浏览…")
        self.btn_pick_target.clicked.connect(self.on_pick_download_dir)
        row.addWidget(self.btn_pick_target)
        outer.addLayout(row)

        self.cb_auto_switch = QCheckBox("解压后自动切换到该版本（不勾选则只解压，当前工作空间不动）")
        self.cb_auto_switch.setChecked(bool(self.repo["auto_switch"]))
        self.cb_auto_switch.stateChanged.connect(self.on_selection_changed)
        outer.addWidget(self.cb_auto_switch)

        self.cb_keep_archive = QCheckBox("解压成功后保留压缩包（每个约 290MB；远端仍在，可再下）")
        self.cb_keep_archive.setChecked(bool(self.repo["keep_archive"]))
        outer.addWidget(self.cb_keep_archive)
        return box

    def _build_progress_box(self) -> QFrame:
        self.progress_box = QFrame()
        self.progress_box.setObjectName("Panel")
        outer = QVBoxLayout(self.progress_box)
        outer.setSpacing(SP["sp1"])

        row = QHBoxLayout()
        row.setSpacing(SP["sp3"])
        self.stage_label = QLabel("准备中…")
        row.addWidget(self.stage_label)
        row.addStretch()
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("FactLabel")
        self.detail_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        row.addWidget(self.detail_label)
        self.btn_cancel_task = QPushButton("取消")
        self.btn_cancel_task.setProperty("variant", "warn")
        self.btn_cancel_task.clicked.connect(self.cancel_task)
        row.addWidget(self.btn_cancel_task)
        outer.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        # 进度条里不再画一遍百分比：同一个数字在右侧已有（还带速度与剩余时间），
        # 画在填充色上既重复又难看
        self.progress_bar.setTextVisible(False)
        outer.addWidget(self.progress_bar)

        self.progress_box.hide()
        return self.progress_box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        self.btn_download = QPushButton("下载并解压")
        self.btn_download.setProperty("variant", "launch")
        self.btn_download.clicked.connect(self.on_primary_action)
        row.addWidget(self.btn_close)
        row.addStretch()
        row.addWidget(self.btn_download)
        return row

    # ---------- 当前工作空间 ----------
    def _refresh_current(self):
        workspace = self.config.workspace
        self.current_path_label.setText(str(workspace))
        details = self.config.get_version_details()
        parts = [f"版本 {self.config.get_version_string() or '—'}"]
        if details.get("built_at"):
            parts.append(f"打包 {details['built_at']}")
        if details.get("branch"):
            parts.append(str(details["branch"]))
        self.current_meta_label.setText(" · ".join(parts))

    @guard_slot
    def on_rescan(self):
        self._refresh_current()
        self.render_table()

    @guard_slot
    def on_pick_local_workspace(self):
        """切换本地已有目录（原来是顶栏「版本」的直接行为，收进对话框后能力不变）。"""
        folder = QFileDialog.getExistingDirectory(
            self, "选择机器人工作空间根目录（含 install/ 与 doc/version.txt）",
            str(self.config.workspace), QFileDialog.ShowDirsOnly)
        if not folder:
            return
        self._activate(Path(folder))

    def _activate(self, workspace: Path):
        """把 workspace_root 切过去，并让主窗口跟上。

        这个方法同时被"切换本地目录"和"下载完成自动切换"复用 —— 切工作空间只有
        一条路径，才不会出现"某处切了但没刷新顶栏"的不一致。
        """
        self.config.set_workspace_root(str(workspace))
        self._refresh_current()
        self.render_table()
        self.workspace_changed.emit(str(workspace))

    # ---------- 远端列表 ----------
    @guard_slot
    def reload(self):
        """拉远端版本列表（后台线程）。打开对话框时调一次，之后靠「刷新」。"""
        if not self.repo["url"]:
            self.remote_status_label.setText("未配置版本服务器，下载功能不可用")
            self.repo_label.setText("在 app_config.yaml 里填 version_repo_url")
            self._set_remote_error("")
            self.table.setRowCount(0)
            self.on_selection_changed()
            return
        if self.fetch_thread is not None and self.fetch_thread.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.remote_status_label.setText("正在读取远端版本列表…")
        self._set_remote_error("")
        self.fetch_thread = ReleaseFetchThread(
            self.repo["url"], self.repo["timeout"], self)
        self.fetch_thread.finished.connect(self.on_fetch_done)
        self.fetch_thread.start()

    @guard_slot
    def on_fetch_done(self, ok, payload, error):
        self.fetch_thread = None
        self.btn_refresh.setEnabled(True)
        if not ok:
            self.releases = []
            self.table.setRowCount(0)
            self.remote_status_label.setText("读取版本列表失败")
            # 仓库地址**留在原处**（不再被错误文案覆盖 —— 那是"地址"那一格的本职），
            # 详细原因另起一段多行显示（保留 describe() 自己的换行）
            self._set_remote_error(error or "读取版本列表失败")
            self.on_selection_changed()
            return
        self.releases = list(payload or [])
        self._rebuild_kind_filter()
        self.render_table()
        self.remote_status_label.setText(f"远端共 {len(self.releases)} 个版本")
        self._set_remote_error("")

    def _set_remote_error(self, text: str) -> None:
        """设置 / 清空远端的失败说明（空串即隐藏）。

        单独一个方法是为了让四处调用点都不必各自记住"空串要隐藏"这件事 ——
        漏掉一处就会留下上一次的旧错误。
        """
        cleaned = (text or "").strip()
        self.remote_error_label.setText(cleaned)
        self.remote_error_label.setVisible(bool(cleaned))

    def _rebuild_kind_filter(self):
        """类型选项来自实际列表：后缀是打包约定（test/end/fix…），
        硬编码一份清单迟早会漏掉新后缀（`end2` 就是这么冒出来的）。"""
        current = self.cmb_kind.currentText()
        kinds = []
        for release in self.releases:
            if release.kind not in kinds:
                kinds.append(release.kind)
        self.cmb_kind.blockSignals(True)
        self.cmb_kind.clear()
        self.cmb_kind.addItem("全部类型")
        for kind in kinds:
            self.cmb_kind.addItem(kind)
        index = self.cmb_kind.findText(current)
        self.cmb_kind.setCurrentIndex(index if index >= 0 else 0)
        self.cmb_kind.blockSignals(False)

    def visible_releases(self):
        """当前过滤条件下的版本（搜索框 + 类型下拉）。"""
        keyword = self.edit_filter.text().strip().lower()
        kind = self.cmb_kind.currentText()
        result = []
        for release in self.releases:
            if kind and kind != "全部类型" and release.kind != kind:
                continue
            if keyword and keyword not in release.name.lower():
                continue
            result.append(release)
        return result

    @guard_slot
    def on_filter_changed(self):
        self.render_table()

    @guard_slot
    def render_table(self):
        visible = self.visible_releases()
        self.table.setRowCount(0)
        self.table.setRowCount(len(visible))

        current = self.config.workspace
        target = self._target_dir()
        colors = theme.palette(self.theme_mode)
        accent_soft, accent = colors["accent_soft"], colors["accent"]
        first_unused = None
        for row, release in enumerate(visible):
            state = inspect_local(release, target, current)
            values = (release.dir_name, release.kind, release.stamp_text,
                      release.size_text, state.label(release))
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(COLUMN_ALIGN[column] | Qt.AlignVCenter)
                if column == 0:
                    item.setData(Qt.UserRole, release)
                if state.is_current:
                    # 当前在用的那一行给底色 + 强调色文字：现场第一眼要回答
                    # "我现在跑的是哪个版本"
                    item.setBackground(QColor(accent_soft))
                    if column == len(COLUMNS) - 1:
                        item.setForeground(QColor(accent))
                self.table.setItem(row, column, item)
            if state.is_current:
                first_item = self.table.item(row, 0)
                font = first_item.font()
                font.setBold(True)
                first_item.setFont(font)
            # 默认落在第一个"没下过"的版本上：最新那个通常正是当前在用的，
            # 预选它等于预选一个点下去只会报错的按钮
            if first_unused is None and not state.extracted:
                first_unused = row

        if visible:
            self.table.selectRow(first_unused if first_unused is not None else 0)
        else:
            self.on_selection_changed()

    def selected_release(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    @guard_slot
    def on_selection_changed(self):
        """按选中项的状态决定主按钮说什么、能不能点。

        三种语义必须分开，否则用户点下去得到一个"目录已存在"的错误：
        未下载 → 下载并解压；已下载 → 切换到此版本；正在用 → 什么都不做。
        """
        release = self.selected_release()
        if release is None:
            self.btn_download.setText("下载并解压")
            self.btn_download.setEnabled(False)
            return

        state = inspect_local(release, self._target_dir(), self.config.workspace)
        if state.is_current:
            self.btn_download.setText("当前正在使用")
            self.btn_download.setEnabled(False)
        elif state.extracted:
            self.btn_download.setText("切换到此版本")
            self.btn_download.setEnabled(self.task_thread is None)
        else:
            suffix = "并切换" if self.cb_auto_switch.isChecked() else ""
            self.btn_download.setText(f"下载、解压{suffix}")
            self.btn_download.setEnabled(
                bool(self.repo["url"]) and self.task_thread is None)

    # ---------- 下载 / 解压 ----------
    def _target_dir(self) -> Path:
        """落点以输入框为准：用户可能当场改过（改了只影响本次会话）。"""
        return Path(self.edit_target.text()).expanduser()

    @guard_slot
    def on_pick_download_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择下载与解压的目录", str(self._target_dir()),
            QFileDialog.ShowDirsOnly)
        if not folder:
            return
        self.edit_target.setText(str(Path(folder)))
        self.render_table()

    @guard_slot
    def on_primary_action(self):
        release = self.selected_release()
        if release is None or self.task_thread is not None:
            return
        state = inspect_local(release, self._target_dir(), self.config.workspace)
        if state.extracted and state.workspace is not None:
            self._activate(state.workspace)
            return
        if not self.repo["url"]:
            QMessageBox.information(self, "未启用版本下载",
                                    "app_config.yaml 里没有配置 version_repo_url。")
            return

        self._set_controls_enabled(False)
        self.progress_box.show()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.stage_label.setText(f"正在下载 {release.name}")
        self.detail_label.setText("")
        self._stage = "download"
        self._session_base = state.part_bytes
        self._task_started_at = time.time()

        self.task_thread = ReleaseTaskThread(
            release, self._target_dir(), self.cb_keep_archive.isChecked(), self)
        self.task_thread.stage_changed.connect(self.on_stage_changed)
        self.task_thread.progress.connect(self.on_task_progress)
        self.task_thread.finished.connect(self.on_task_finished)
        self.task_thread.start()

    @guard_slot
    def on_stage_changed(self, stage):
        self._stage = stage
        if stage == "extract":
            # tar.bz2 的总解压量要读完整条压缩流才知道，所以这里用不确定进度
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText("")
            self.stage_label.setText("下载完成，正在解压（bzip2 较慢，请稍候）")

    @guard_slot
    def on_task_progress(self, stage, done, total, note):
        if stage == "download" and total:
            percent = int(done * 100 / total)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            elapsed = max(time.time() - self._task_started_at, 0.001)
            # 速度按"本次真正下了多少"算：续传时 done 里含上次的偏移，
            # 直接拿 done/elapsed 会得到一个漂亮但没用的速度
            speed = max(done - self._session_base, 0) / elapsed
            remain = (total - done) / speed if speed > 0 else 0
            self.detail_label.setText(
                f"{percent}% · {human_size(done)}/{human_size(total)} · "
                f"{human_size(speed)}/s · 剩余 {_fmt_duration(remain)}")
        else:
            self.progress_bar.setRange(0, 0)
            self.detail_label.setText(f"{note} · 已写出 {human_size(done)}")

    @guard_slot
    def cancel_task(self):
        if self.task_thread is None:
            return
        self.btn_cancel_task.setEnabled(False)
        self.stage_label.setText("正在取消（下载断点会保留，下次可续传）…")
        self.task_thread.cancel()

    @guard_slot
    def on_task_finished(self, ok, payload, error):
        self.task_thread = None
        self._set_controls_enabled(True)
        self.progress_bar.setRange(0, 100)

        if not ok:
            self.progress_bar.setValue(0)
            self.stage_label.setText("未完成")
            self.detail_label.setText("")
            QMessageBox.warning(self, "版本下载未完成", error or "原因未知，请看运行日志")
            self.render_table()
            return

        self.progress_bar.setValue(100)
        workspace = payload.get("workspace") if isinstance(payload, dict) else None
        archive = payload.get("archive") if isinstance(payload, dict) else None
        self.stage_label.setText(f"已完成：{workspace}")
        if archive is not None and Path(archive).exists():
            self.detail_label.setText(f"压缩包 {Path(archive).name}")
        else:
            self.detail_label.setText("压缩包已按设置删除")

        if self.cb_auto_switch.isChecked() and workspace is not None:
            self._activate(Path(workspace))
        else:
            self.render_table()

    def _set_controls_enabled(self, enabled: bool):
        self.btn_download.setEnabled(enabled)
        self.btn_refresh.setEnabled(enabled)
        self.btn_pick_target.setEnabled(enabled)
        self.btn_pick_local.setEnabled(enabled)
        self.btn_rescan.setEnabled(enabled)
        self.table.setEnabled(enabled)
        self.btn_cancel_task.setEnabled(not enabled)
        if enabled:
            # 复位后按钮文案要按当前选中项重算（不能停在"下载、解压并切换"）
            self.on_selection_changed()

    # ---------- 关闭 ----------
    def closeEvent(self, event):
        """任务进行中关窗要先问：直接关会留下一个半成品目录，而用户不知道。"""
        if self.task_thread is not None and self.task_thread.isRunning():
            answer = QMessageBox.question(
                self, "任务仍在进行",
                "版本下载/解压还没结束。关闭会取消任务：\n"
                "· 下载断点保留（下次可续传）\n"
                "· 解压到一半的临时内容会被清理\n\n确定关闭？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.task_thread.cancel()
            self.task_thread.wait(5000)
        if self.fetch_thread is not None and self.fetch_thread.isRunning():
            self.fetch_thread.wait(2000)
        super().closeEvent(event)
