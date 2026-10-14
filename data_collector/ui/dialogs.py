"""轻量对话框：遥操作用户名、tracker 选择、数据录制、任务标签管理。"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication, QDoubleSpinBox,
                             QSpinBox)
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
from ..core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ..core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES,
                              MODULE_NAMES, MODULE_STEPS, MODULE_STATE_TEXT,
                              MODULE_STATE_ON, MODULE_STATE_OFF, MODULE_STATE_PARTIAL,
                              MODULE_STATE_UNKNOWN, module_state_of,
                              GRIPPER_NAMES, GRIPPER_JOINTS, GRIPPER_HAND_TYPE,
                              GRIPPER_TAU_MIN_NM, GRIPPER_TAU_MAX_NM,
                              GRIPPER_SPEED_MAX_MMS, GRIPPER_RAW_OPEN, GRIPPER_RAW_CLOSED,
                              GRIPPER_DEFAULT_TAU_RAW, GRIPPER_DEFAULT_SPEED_RAW,
                              gripper_tau_nm, gripper_speed_mms, gripper_tau_raw,
                              gripper_speed_raw, gripper_opening_text)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError


from .common import (SP, ARM_MOTION_LABELS, field_label, module_state_texts)
from .safety import guard_slot
from .threads import LocalTaskThread
from .widgets import HintLineEdit, LogView



class TeleopDialog(QDialog):
    """选/填遥操作用户名。

    从 QLineEdit 换成**可编辑下拉**：已标定过的用户直接选，不用打字。现场原来的
    流程是盲打一个名字，打错（多一个空格、大小写不对、输入法留了不可见字符）就会被
    判成"这个没标定过"、弹一整套 VR 标定流程 —— 一个字符的差错换一次重新标定，
    代价完全不成比例。输入框仍然保留（新人第一次标定得填名字）。

    版面按"一个决定"来组织，三段 + 一行操作：

        用户名                       ← 字段名（等宽小字，与全站字段标签同一套）
        [ 输入或选择用户名       ▾ ]
        已标定 6 人，可直接选；输入新名字会进入 VR 标定流程   ← 说明"输进去会发生什么"
        机器人侧当前生效：yyt        ← 换人时的对照

                          [ 取消 ]  [ 确定 ]
    """

    #: 对话框最小宽度：比"字段名 + 下拉"的自然宽度宽一点 —— 否则右侧会空出一大块，
    #  而且说明文字会被压到折行（实测自然宽度只有 282，说明那行根本放不下）。
    MIN_WIDTH = 380

    def __init__(self, parent=None, users=None, current: str = ""):
        """
        Args:
            users: 已经能直接切过去的用户名（含其它工作空间里的），列进下拉。
            current: 机器人侧当前生效的标定用户，显示出来做对照。
        """
        super().__init__(parent)
        self.setWindowTitle("启动遥操作")
        self.setModal(True)

        names = [str(u) for u in (users or [])]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        # 标签与它管的控件算一组（8px），组与组之间再拉开（下面 addSpacing）
        layout.setSpacing(SP["sp2"])

        # ---- 字段 ----
        self.name_label = field_label("用户名")
        layout.addWidget(self.name_label)

        self.combo = QComboBox()
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        # 提示文字用 HintLineEdit 自绘（浅色）：QSS 给所有控件写了 color，会把 Qt 自带的
        # 占位色压掉 —— 自带占位渲染出来与**已输入的名字**几乎一样亮（实测），
        # 现场会以为名字已经填好了、直接点确定，然后收到一句"用户名不能为空"。
        self.combo.setLineEdit(HintLineEdit("输入或选择用户名"))
        self.combo.addItems(names)
        # 默认**留空**，不预填当前用户：预填之后"顺手按回车"就会用上一个人的参数启动，
        # 而现场点这个按钮往往是来换人的。当前是谁由下面那行提示说清楚，不必替人做选择。
        self.combo.setCurrentIndex(-1)
        self.name_label.setBuddy(self.combo)     # 点字段名即可聚焦输入框
        layout.addWidget(self.combo)

        # ---- 说明：先回答"输进去会发生什么" ----
        if names:
            note = f"已标定 {len(names)} 人，可直接选；输入新名字会进入 VR 标定流程"
        else:
            note = "还没有任何标定记录 —— 输入名字后会进入 VR 标定流程"
        self.note_label = QLabel(note)
        self.note_label.setObjectName("Hint")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        self.current_label = None
        if current:
            self.current_label = QLabel(f"机器人侧当前生效：{current}")
            self.current_label.setObjectName("Hint")
            layout.addWidget(self.current_label)

        # ---- 操作 ----
        layout.addSpacing(SP["sp3"])
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        row.addStretch()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("variant", "ghost")
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setProperty("variant", "primary")
        self.btn_ok.setDefault(True)             # 回车即确定
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_ok)
        layout.addLayout(row)

        self.setMinimumWidth(self.MIN_WIDTH)
        # 空着就没有可确定的东西：把"点确定 → 弹一句用户名不能为空"这条路直接关掉。
        # 输入后按钮才亮 —— 现场不必先撞一次错误框才知道要填。
        self.combo.editTextChanged.connect(self._sync_ok_enabled)
        self._sync_ok_enabled(self.combo.currentText())
        self.combo.setFocus()

    def _sync_ok_enabled(self, text: str) -> None:
        self.btn_ok.setEnabled(bool(str(text).strip()))

    def get_username(self) -> str:
        return self.combo.currentText().strip()


class CalibrationDialog(QDialog):
    """VR tracker 标定的入参：这是**给谁**标、用哪一组 tracker。

    两点改动都是为了消掉"本可以避免的错误路径"：

    - **tracker 组别从输入框换成下拉（1~4）**。原来是自由输入 + 主界面校验，
      输错就弹一个"请输入 1-4 的数字"错误框 —— 而合法取值一共四个，直接列出来
      比事后报错省一个来回。
    - **把用户名显示出来**。这个对话框是和上一个对话框连着用的，但原来看不出
      正在给谁标定：现场标错人只能靠日志回查。
    """

    #: 可选的 tracker 组别（与机器人侧约定一致：1~4）
    TRACKERS = ("1", "2", "3", "4")

    def __init__(self, parent=None, username: str = ""):
        super().__init__(parent)
        self.setWindowTitle("标定设置")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp2"])

        if username:
            who = QLabel(f"为「{username}」标定")
            who.setObjectName("SectionTitle")
            layout.addWidget(who)
            layout.addSpacing(SP["sp1"])

        self.tracker_label = field_label("tracker 组别")
        layout.addWidget(self.tracker_label)
        self.combo = QComboBox()
        self.combo.addItems(self.TRACKERS)
        self.tracker_label.setBuddy(self.combo)
        layout.addWidget(self.combo)

        tip = QLabel("按终端提示依次摆位，标定完成后会自动继续启动遥操作")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addSpacing(SP["sp3"])
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        row.addStretch()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("variant", "ghost")
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("开始标定")
        self.btn_ok.setProperty("variant", "primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_ok)
        layout.addLayout(row)

        self.setMinimumWidth(340)
        self.combo.setFocus()
        # 兼容旧调用方（历史上这个属性叫 edit）
        self.edit = self.combo

    def get_tracker_number(self) -> str:
        return self.combo.currentText().strip()


class RecordDialog(QDialog):
    """录制设置。采集任务可下拉选，也可以点「管理任务」增删自己的任务。

    任务名与编号分开存（编号放 combo 的 UserRole，纯名字放 NAME_ROLE），
    这样写进 info.json 的 label 是干净的任务名，不会带上"4 "这种编号前缀。
    """

    NAME_ROLE = Qt.UserRole + 1

    #: 最小宽度。**「保存位置」下面那行提示必须能换行显示完整落点** ——
    #: 实测按内容自适应时只有 269px 宽，那句"本次将写入：<长路径>/"会被裁掉，
    #: 还会和下一行的「名称（name）」标签叠在一起（自截图里一眼能看到）。
    #: 与本文件另一个对话框的 MIN_WIDTH 同性质。
    MIN_WIDTH = 520

    def __init__(self, parent=None, config=None, action_labels=None):
        super().__init__(parent)
        self.setWindowTitle("录制设置")
        self.setModal(True)
        self.config = config
        # 兼容入参：不传 config 时用传入的映射（或 SDK 默认表）做只读下拉
        self._fallback = dict(action_labels) if action_labels else dict(ARM_MOTION_LABELS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        layout.addWidget(field_label("录制类型"))
        self.type_combo = QComboBox()
        # bag/vla/cmd 对应配置项 record_<type>_script（scripts/record_bag.sh 等）
        self.type_combo.addItems(["bag", "vla", "cmd"])
        layout.addWidget(self.type_combo)

        # ---- 保存位置（v6.4.17）----
        #
        # 为什么放在这个对话框里：它和「录制类型 / 名称 / 采集任务」是同一类问题 ——
        # "**这一次**怎么录"。默认值按"有没有插数据盘"自动定，插上就不用再点
        # （现场的主要场景就是"后续采集可能直接存到移动硬盘"）。
        #
        # 两个候选**始终都列出来**（不可用的置灰 + 写明原因）：隐藏会让人以为
        # 程序不支持；而置灰不说明原因，现场只会反复点它。
        layout.addWidget(field_label("保存位置"))
        self.target_combo = QComboBox()
        self.target_combo.setToolTip(
            "这次录制把数据写到哪里。插了数据盘默认就是盘（可直接交出去，"
            "不必再导一遍）；拔了自动回到本机数据盘 —— 选择不写进配置文件，"
            "所以不会出现「盘拔了、程序还指着盘」而起录失败")
        layout.addWidget(self.target_combo)
        self.target_hint = QLabel()
        self.target_hint.setObjectName("Hint")
        self.target_hint.setWordWrap(True)
        layout.addWidget(self.target_hint)
        self._reload_targets()
        self.target_combo.currentIndexChanged.connect(self._sync_target_hint)

        layout.addWidget(field_label("名称（name）"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("可选")
        layout.addWidget(self.name_edit)

        layout.addWidget(field_label("描述（desc）"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("可选")
        layout.addWidget(self.desc_edit)

        # ---- 采集任务 ----
        label_head = QHBoxLayout()
        label_head.addWidget(field_label("采集任务（label）"))
        label_head.addStretch()
        self.btn_manage_labels = QPushButton("管理任务")
        self.btn_manage_labels.setProperty("variant", "ghost")
        self.btn_manage_labels.clicked.connect(self.open_label_manager)
        self.btn_manage_labels.setEnabled(config is not None)
        self.btn_manage_labels.setToolTip(
            "新增/改名/删除采集任务（保存到 configs/action_labels.yaml）" if config is not None
            else "未提供配置，无法管理任务")
        label_head.addWidget(self.btn_manage_labels)
        layout.addLayout(label_head)

        self.label_combo = QComboBox()
        self.label_combo.setEditable(True)      # 也允许临时填一个不在表里的动作
        self.label_combo.lineEdit().setPlaceholderText("可选，可自由填写")
        layout.addWidget(self.label_combo)
        self.reload_labels()

        layout.addSpacing(SP["sp2"])
        hbox = QHBoxLayout()
        hbox.addStretch()
        cancel = QPushButton("取消")
        cancel.setProperty("variant", "ghost")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("开始录制")
        ok.setProperty("variant", "primary")
        ok.clicked.connect(self.accept)
        hbox.addWidget(cancel)
        hbox.addWidget(ok)
        layout.addLayout(hbox)
        self.setMinimumWidth(self.MIN_WIDTH)

    # ---------- 保存位置（本机数据盘 / 移动硬盘） ----------

    def _reload_targets(self):
        """重建「保存位置」下拉。

        默认值优先级：**本次运行选过的** > 按"有没有可用数据盘"自动定。
        选过的那个若已经不可用（盘拔了），自动回落到默认 —— 不能把一个已不可用的
        选项留在选中态，否则要到点「开始录制」时才发现写不进去。
        """
        from ..disk_export import (default_record_target_key, record_targets,
                                   target_by_key)

        self._targets = record_targets(self.config)
        remembered = self.config.record_target_key() if self.config is not None else None
        chosen = target_by_key(self.config, remembered) if remembered else None
        chosen_key = (chosen.key if chosen is not None and chosen.available
                      else default_record_target_key(self.config))

        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in self._targets:
            # 下拉里只放**短标签**：路径与不可用原因都放下面那行提示。
            # （实测：把路径拼进选项文字时，QComboBox 会按最长项撑开 ——
            #   本对话框在"插盘/拔盘"两态之间宽度从 560 跳到 816，很难看。
            #   而且两块盘路径长短不同时，同一个界面宽度还会再变。）
            suffix = "（已插入）" if (target.key == "disk" and target.available) else ""
            if target.key == "disk" and not target.available:
                suffix = "（不可用）"
            self.target_combo.addItem(f"{target.label}{suffix}", target.key)
            item = self.target_combo.model().item(self.target_combo.count() - 1)
            if item is not None:
                # 不可用项**列出来但不可选**：原因写在下面那行提示里，不必去猜
                item.setEnabled(target.available)
        index = self.target_combo.findData(chosen_key)
        self.target_combo.setCurrentIndex(index if index >= 0 else 0)
        self.target_combo.blockSignals(False)
        self._sync_target_hint()

    def current_target(self):
        """当前选中的落盘目标（`disk_export.RecordTarget`）；没有候选时返回 None。"""
        key = self.target_combo.currentData()
        for target in getattr(self, "_targets", []):
            if target.key == key:
                return target
        return None

    def _sync_target_hint(self, *_args):
        """把"这次会写到哪"说全 —— 目录是拼出来的，别让现场去猜。

        不可用的那一项，**原因也写在这里**（下拉里只放短标签，见 `_reload_targets`）。
        """
        if getattr(self, "target_hint", None) is None:
            return
        target = self.current_target()
        if target is None:
            self.target_hint.setText("")
            return
        lines = [f"本次将写入：{target.path}/ —— "
                 + ("直接写在移动硬盘上，录完可直接拔盘交付" if target.key == "disk"
                    else "本机数据盘")]
        blocked = [t for t in getattr(self, "_targets", [])
                   if t.key != target.key and not t.available]
        for other in blocked:
            lines.append(f"{other.label}不可用：{other.reason}")
        self.target_hint.setText("\n".join(lines))
        self._fit_target_hint()

    def _fit_target_hint(self) -> None:
        """按当前宽度重算提示行的高度。

        **必须自己算**：`QLabel` 开了 `setWordWrap(True)` 之后 `sizeHint()` 的高度
        只按**一行**给，而布局照这个高度排 —— 多出来的第 2、3 行会被裁掉、压在下一行
        的标签上（实测：「移动硬盘不可用：…」那行的末尾就被「名称（name）」盖住了；
        代码里没有任何东西会报错，只有截图能看出来）。
        """
        label = self.target_hint
        margins = self.layout().contentsMargins()
        width = max(200, self.width() - margins.left() - margins.right())
        rect = label.fontMetrics().boundingRect(0, 0, width, 0, Qt.TextWordWrap, label.text())
        label.setMinimumHeight(rect.height())

    def resizeEvent(self, event):        # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._fit_target_hint()          # 宽度变了，换行数也变

    def label_items(self):
        """当前可选任务：[{name, action_id}]。"""
        if self.config is not None:
            return self.config.get_action_labels()
        return [{"name": name, "action_id": action_id}
                for action_id, name in sorted(self._fallback.items())]

    def reload_labels(self):
        """重建下拉项，尽量保留当前已输入的内容（管理完任务回来不该丢选择）。"""
        current = self.label_combo.currentText().strip()
        self.label_combo.blockSignals(True)
        self.label_combo.clear()
        self.label_combo.addItem("", None)
        for item in self.label_items():
            action_id = item.get("action_id")
            text = f"{action_id}  {item['name']}" if action_id is not None else item["name"]
            self.label_combo.addItem(text, action_id)
            self.label_combo.setItemData(self.label_combo.count() - 1,
                                         item["name"], self.NAME_ROLE)
        self.label_combo.setEditText(current)
        self.label_combo.blockSignals(False)

    def open_label_manager(self):
        if self.config is None:
            return
        ActionLabelDialog(self.config, self).exec_()
        self.reload_labels()

    def get_settings(self):
        """返回录制参数。label 是纯任务名，action_id 是可选的编号（一般为 None）。

        额外带出「保存位置」：`record_target` 是键（`local` / `disk`），
        `record_dir` 是**要落到的目录**（`local` 时为 None —— 表示用配置里的
        `bag_dir`，而不是"某个具体路径"，这样"选本机"不会被上一次的选择黏住）。
        """
        text = self.label_combo.currentText().strip()
        index = self.label_combo.currentIndex()
        action_id = self.label_combo.currentData()
        if index >= 0 and self.label_combo.itemText(index).strip() == text:
            name = self.label_combo.itemData(index, self.NAME_ROLE) or text
        else:
            name = text          # 用户手输的自由文本，没有对应编号
            action_id = None
        target = self.current_target()
        return {
            "type": self.type_combo.currentText(),
            "name": self.name_edit.text().strip(),
            "desc": self.desc_edit.text().strip(),
            "label": name,
            "action_id": action_id,
            "record_target": target.key if target is not None else "local",
            "record_dir": (target.path if target is not None and target.key == "disk"
                           else None),
        }


#: 同一个管理界面服务两套**互不相干**的标签库（采集任务 / 打点标签），
#: 靠这一段把各自的文案分开 —— 否则会出现"打点标签的窗口里写着会写进 info.json"。
_LABEL_KIND_COPY = {
    "采集任务": {
        "hint": "录制时选中的名称会写进 info.json",
        "name_label": "任务名",
        "name_placeholder": "例如：倒水",
        "id_label": "任务编号（可空）",
        "delete_note": "已录制数据里的 label 不受影响。",
    },
    "打点标签": {
        "hint": "打点时选中的名称会写进 points.csv 的「标签」列",
        "name_label": "标签名",
        "name_placeholder": "例如：第一个角上夹具",
        "id_label": "编号（可空）",
        "delete_note": "已经写进 points.csv 的记录不受影响。",
    },
}


class ActionLabelDialog(QDialog):
    """采集任务管理：新增 / 改名 / 删除。

    改动立即写盘（configs/action_labels.yaml），不做"确定/取消"——任务名是低风险的
    元数据，即时生效比"改完忘了保存"更不容易出错。
    """

    def __init__(self, config, parent=None, store=None, kind="采集任务"):
        """标签管理界面。

        Args:
            store: 要管理的标签库（默认采集任务库）。打点标签传 `config.point_labels` ——
                两套库互不读写，管理界面只是"长得一样"，不是"同一个列表"。
            kind: 文案用（"采集任务" / "打点标签"）。
        """
        super().__init__(parent)
        self.config = config
        self.store = store if store is not None else config.action_labels
        self.kind = kind
        self._copy = _LABEL_KIND_COPY.get(kind, _LABEL_KIND_COPY["采集任务"])
        self.setWindowTitle(f"管理{kind}")
        self.setModal(True)
        self.resize(480, 470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        hint = QLabel(f"存到 {self.store.file_path()}；{self._copy['hint']}")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list, 1)

        form = QHBoxLayout()
        form.setSpacing(SP["sp2"])

        name_box = QVBoxLayout()
        name_box.setSpacing(SP["sp1"])
        name_box.addWidget(field_label(self._copy["name_label"]))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(self._copy["name_placeholder"])
        name_box.addWidget(self.name_edit)
        form.addLayout(name_box, 2)

        id_box = QVBoxLayout()
        id_box.setSpacing(SP["sp1"])
        id_box.addWidget(field_label(self._copy["id_label"]))
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("一般留空")
        self.id_edit.setMaximumWidth(140)
        id_box.addWidget(self.id_edit)
        form.addLayout(id_box, 1)
        layout.addLayout(form)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("新增")
        self.btn_add.setProperty("variant", "primary")
        self.btn_add.clicked.connect(self.add_label)
        self.btn_update = QPushButton("保存修改")
        self.btn_update.clicked.connect(self.update_label)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.setProperty("variant", "danger")
        self.btn_delete.clicked.connect(self.delete_label)
        close = QPushButton("关闭")
        close.setProperty("variant", "ghost")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(self.btn_update)
        buttons.addWidget(self.btn_delete)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.reload()

    # ---------- 内部 ----------
    def reload(self, keep=""):
        """刷新列表；keep 指定要重新选中的任务名。"""
        self.list.blockSignals(True)
        self.list.clear()
        for item in self.store.get():
            action_id = item.get("action_id")
            text = (f"{action_id}    {item['name']}" if action_id is not None
                    else f"{item['name']}    （自定义）")
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, item["name"])
            entry.setData(Qt.UserRole + 1, action_id)
            self.list.addItem(entry)
            if item["name"] == keep:
                self.list.setCurrentItem(entry)
        self.list.blockSignals(False)
        self._sync_buttons()

    def _selected_name(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else ""

    def _on_selection_changed(self, current, _previous):
        if current is not None:
            self.name_edit.setText(current.data(Qt.UserRole))
            action_id = current.data(Qt.UserRole + 1)
            self.id_edit.setText("" if action_id is None else str(action_id))
            self.status_label.setText("")
        self._sync_buttons()

    def _sync_buttons(self):
        has_selection = self.list.currentItem() is not None
        self.btn_update.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)

    def _report(self, ok, message):
        self.status_label.setText(("✓ " if ok else "✗ ") + message)
        theme.set_state(self.status_label, "ok" if ok else "danger")

    # ---------- 操作 ----------
    def add_label(self):
        ok, message = self.store.upsert(
            self.name_edit.text(), self.id_edit.text() or None)
        self._report(ok, message)
        if ok:
            self.reload(keep=self.name_edit.text().strip())
            self.name_edit.clear()
            self.id_edit.clear()

    def update_label(self):
        original = self._selected_name()
        if not original:
            self._report(False, f"请先在列表里选中一个{self.kind}")
            return
        ok, message = self.store.upsert(
            self.name_edit.text(), self.id_edit.text() or None, original_name=original)
        self._report(ok, message)
        if ok:
            self.reload(keep=self.name_edit.text().strip())

    def delete_label(self):
        name = self._selected_name()
        if not name:
            self._report(False, f"请先在列表里选中一个{self.kind}")
            return
        if QMessageBox.question(
            self, f"删除{self.kind}",
            f"确定删除「{name}」吗？\n{self._copy['delete_note']}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        ok, message = self.store.remove(name)
        self._report(ok, message)
        if ok:
            self.name_edit.clear()
            self.id_edit.clear()
            self.reload()
