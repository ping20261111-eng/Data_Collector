"""打点标签管理：**按采集任务分组**维护。

## 为什么单独一个界面

原来的标签管理界面是"一列标签 + 增删改"（扁平），而打点标签是**两层**：
任务 → 该任务的标签。把两层硬塞进那个界面会让两边都别扭，所以单独做一屏；
但存储仍复用同一个 `ActionLabelStore`（`config.point_labels`，分组模式），
不另写一套增删改。

## 左侧的组从哪来

① **通用组**（固定第一位，`*`）：不属于任何任务，每个任务下打点都会出现。它装的是
   "开始 / 结束"这类夹在流程两端的标签 —— 面板里首项排最前、末项排最后；
② **采集任务列表**：即使还没配标签也列出来，方便直接往里加；
③ **打点标签里已有、但采集任务里已经没有的组**（任务被改名或删除）——
   标注"已不在采集任务里"，而不是静默丢掉。丢掉的话现场会觉得"我加过的标签
   怎么没了"，而改名/删除是随时可能发生的。
"""
from typing import List, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QVBoxLayout)

from .. import theme
from ..core import ActionLabelStore
from .common import SP
from .safety import guard_slot


class PointLabelDialog(QDialog):
    """打点标签管理：左侧选任务分组，右侧维护该组的标签。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.store = config.point_labels
        self._group = ActionLabelStore.COMMON_TASK

        self.setWindowTitle("管理打点标签")
        self.setModal(True)
        self.resize(820, 580)
        self._build_ui()
        self.reload_groups()
        self.reload_labels()

    # ---------- 界面 ----------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        hint = QLabel(
            f"存到 {self.store.file_path()}。打点面板里显示为一条流程：通用组的首项排最前、"
            "末项排最后（开始任务 / 结束任务 就放通用组），中间是本次录制那个任务的标签，"
            "序号从 1 顺着编到最后。下面的「组内编号」只决定组内先后，只要求组内唯一 —— "
            "不同任务各有各的编号体系。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(SP["sp3"])
        body.addWidget(self._build_groups_box())
        body.addWidget(self._build_labels_box(), 1)
        layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        bottom.addStretch()
        bottom.addWidget(close)
        layout.addLayout(bottom)

    def _build_groups_box(self) -> QGroupBox:
        box = QGroupBox("任务分组")
        outer = QVBoxLayout(box)
        outer.setSpacing(SP["sp2"])

        self.groups_list = QListWidget()
        self.groups_list.setMaximumWidth(240)
        self.groups_list.currentItemChanged.connect(self.on_group_changed)
        outer.addWidget(self.groups_list)

        tip = QLabel("通用组的首项排在流程最前、末项排最后；中间是各任务自己的标签")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        outer.addWidget(tip)
        return box

    def _build_labels_box(self) -> QGroupBox:
        self.labels_box = QGroupBox("标签")
        outer = QVBoxLayout(self.labels_box)
        outer.setSpacing(SP["sp2"])

        self.labels_list = QListWidget()
        self.labels_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.labels_list.currentItemChanged.connect(self.on_label_changed)
        outer.addWidget(self.labels_list, 1)

        form = QHBoxLayout()
        form.setSpacing(SP["sp2"])
        form.addWidget(QLabel("标签名"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：第一个角上夹具")
        form.addWidget(self.name_edit, 2)
        form.addWidget(QLabel("组内编号"))
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("可空")
        self.id_edit.setMaximumWidth(90)
        form.addWidget(self.id_edit, 1)
        outer.addLayout(form)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("新增")
        self.btn_add.setProperty("variant", "primary")
        self.btn_add.clicked.connect(self.add_label)
        self.btn_update = QPushButton("保存修改")
        self.btn_update.clicked.connect(self.update_label)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_delete.clicked.connect(self.delete_label)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(self.btn_update)
        buttons.addWidget(self.btn_delete)
        buttons.addStretch()
        outer.addLayout(buttons)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        return self.labels_box

    # ---------- 分组 ----------
    def _groups(self) -> List[Tuple[str, str]]:
        """(组键, 显示名)。通用 → 采集任务 → 只存在于打点标签里的组。"""
        groups = [(ActionLabelStore.COMMON_TASK, "通用（流程首尾）")]
        seen = {ActionLabelStore.COMMON_TASK}
        for item in self.config.get_action_labels():
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                groups.append((name, name))
                seen.add(name)
        for task in self.store.tasks():
            if task not in seen:
                # 任务被改名或删掉了，标签组还在 —— 标出来，别让现场以为是丢了
                groups.append((task, f"{task}（已不在采集任务里）"))
                seen.add(task)
        return groups

    @guard_slot
    def reload_groups(self):
        self.groups_list.blockSignals(True)
        self.groups_list.clear()
        for key, text in self._groups():
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, key)
            self.groups_list.addItem(entry)
        self.groups_list.blockSignals(False)

        for row in range(self.groups_list.count()):
            if self.groups_list.item(row).data(Qt.UserRole) == self._group:
                self.groups_list.setCurrentRow(row)
                return
        self.groups_list.setCurrentRow(0)
        self._group = self.groups_list.currentItem().data(Qt.UserRole)

    @guard_slot
    def on_group_changed(self, current, _previous):
        if current is None:
            return
        self._group = current.data(Qt.UserRole)
        self.name_edit.clear()
        self.id_edit.clear()
        self.reload_labels()

    # ---------- 标签 ----------
    @guard_slot
    def reload_labels(self, keep: str = ""):
        """刷新右侧列表：只显示**当前分组**里的标签。"""
        self.labels_list.blockSignals(True)
        self.labels_list.clear()
        for item in self.store.get():
            if (item.get("task") or ActionLabelStore.COMMON_TASK) != self._group:
                continue
            action_id = item.get("action_id")
            text = (f"{action_id}    {item['name']}" if action_id is not None
                    else f"{item['name']}    （无编号）")
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, item["name"])
            entry.setData(Qt.UserRole + 1, action_id)
            self.labels_list.addItem(entry)
            if item["name"] == keep:
                self.labels_list.setCurrentItem(entry)
        self.labels_list.blockSignals(False)

        title = self._group_label()
        self.labels_box.setTitle(f"标签 · {title}（{self.labels_list.count()}）")
        self._sync_buttons()

    def _group_label(self) -> str:
        for key, text in self._groups():
            if key == self._group:
                return text
        return self._group

    def _selected_name(self):
        item = self.labels_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else ""

    @guard_slot
    def on_label_changed(self, current, _previous):
        if current is not None:
            self.name_edit.setText(current.data(Qt.UserRole))
            action_id = current.data(Qt.UserRole + 1)
            self.id_edit.setText("" if action_id is None else str(action_id))
            self.status_label.setText("")
        self._sync_buttons()

    def _sync_buttons(self):
        has_selection = self.labels_list.currentItem() is not None
        self.btn_update.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)

    def _report(self, ok, message):
        self.status_label.setText(("✓ " if ok else "✗ ") + message)
        theme.set_state(self.status_label, "ok" if ok else "danger")

    # ---------- 操作 ----------
    @guard_slot
    def add_label(self):
        ok, message = self.store.upsert(
            self.name_edit.text(), self.id_edit.text() or None, task=self._group)
        self._report(ok, message)
        if ok:
            self.name_edit.clear()
            self.id_edit.clear()
            self.reload_labels()
            self.reload_groups()      # 新组可能出现（例如往一个还没配过标签的任务里加）

    @guard_slot
    def update_label(self):
        original = self._selected_name()
        if not original:
            self._report(False, "请先在右侧选中一个标签")
            return
        ok, message = self.store.upsert(
            self.name_edit.text(), self.id_edit.text() or None,
            original_name=original, task=self._group)
        self._report(ok, message)
        if ok:
            self.reload_labels(keep=self.name_edit.text().strip())

    @guard_slot
    def delete_label(self):
        name = self._selected_name()
        if not name:
            self._report(False, "请先在右侧选中一个标签")
            return
        if QMessageBox.question(
                self, "删除打点标签",
                f"确定从「{self._group_label()}」里删除「{name}」吗？\n"
                f"已经写进 points.csv 的记录不受影响。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        ok, message = self.store.remove(name, task=self._group)
        self._report(ok, message)
        if ok:
            self.name_edit.clear()
            self.id_edit.clear()
            self.reload_labels()
