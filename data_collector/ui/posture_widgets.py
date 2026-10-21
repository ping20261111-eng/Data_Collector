"""「姿势控制」页面的**常驻组装件**：一行轴、预设卡、模块使能条、四张卡的骨架。

拆出来的原因很实际：这一页连自绘带交互 1500 行以上，超过项目「单文件 1000 行」的红线
（`test_no_ui_file_balloons` 就是钉这条的）；而这些件之间是**一组**关系 ——
它们都只认"轴表 + 显示令牌 + FK 示意图"，不碰窗口、不碰 launcher。

v6.3.0 又拆出第三册：两个**模态框**（运动确认 / 预设编辑）搬去了 `posture_dialogs.py`
—— 那一册到 1022 行、过了红线，而"弹一个框"和"铺一个页"本来就是两种东西
（前者 `exec_()` 阻塞取结果，后者常驻并跟着 500ms 周期刷新）。

示意图（正运动学）在 `posture_draw.py`；真正的页面（`posture.py`）负责拼装、接状态与下发。
`posture.py` 把这三册的名字再导出一遍，所以 `posture_module.TrunkDiagram`、
对 `posture._MoveConfirmDialog.exec_` 的 monkeypatch 这些写法照旧有效。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QVBoxLayout)

from .. import theme
from ..core_motion import (ARM_AXES, HAND_FINGERS,
                           HAND_FINGER_HINTS, TRUNK_AXES, fmt_number)
from .common import SP, badge
from .posture_draw import (ArmDiagram, TrunkDiagram, paint_arm,  # noqa: F401
                           paint_trunk)

#: 角度读不到时的显示（**不用 0 冒充**）
UNKNOWN = "—"
#: 每个轴组的轴表：`(键, 下限, 上限, 实体含义)`
#: 末尾的 `· °` 是**单位**（v6.3.0 补）：机器人内部的角度是弧度，表里只有裸数字时
#: 没人敢断定这是度还是弧度 —— 猜错一次就是几十度的误差。逐格加 `°` 会在 20 行 × 2 列里
#: 多出 40 个符号，反而更吵，所以写在列标题上。
GROUP_TITLES = {"trunk": "躯干 H1~H6（下 → 上）· °",
                "left": "左臂 L1~L7（从肩起）· °",
                "right": "右臂 R1~R7（从肩起）· °"}
HAND_TOOLTIP = ("灵巧手没有回读话题（消息包里只有命令）—— 这里显示的是本次会话"
                "最后下发的那组值，不是实时读数。\n顺序：" +
                "、".join(f"{key}={hint}" for key, hint in
                         zip(HAND_FINGERS, HAND_FINGER_HINTS)))
#: 键盘步长（读只读态下按上下键）
KEY_STEPS = {"plain": 0.1, "shift": 5.0, "alt": 0.5}


def specs_for(group: str):
    """一个轴组的轴记录列表（4 元组：键 / 下限 / 上限 / 实体含义）。"""
    if group == "trunk":
        return list(TRUNK_AXES)
    return list(ARM_AXES[group])


def group_of(key: str) -> str:
    """从轴名反查所属组（`H*` → 躯干，`L*`/`R*` → 左右臂）。"""
    if key.startswith("H"):
        return "trunk"
    return "left" if key.startswith("L") else "right"


# ============================================================
# 目标值输入框：只读时长得像读数，双击才可编辑
# ============================================================
class TargetEdit(QLineEdit):
    """目标角度的输入框。

    "单轴调节做得像当前角度那样"（现场意见）：所以**不编辑时它就是一个右对齐的等宽读数**，
    双击才变成输入框。这样 20 行的版面里不会再塞进滑杆与 ± 按钮，一屏能看全。

    键盘（可编辑态）：回车提交、Esc 撤销、失焦提交。
    键盘（只读态）：↑↓ ±0.1°、Shift+↑↓ ±5°、Alt+↑↓ ±0.5°。
    """

    edit_committed = pyqtSignal(str)
    edit_cancelled = pyqtSignal()
    nudge_requested = pyqtSignal(float)
    move_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AxisTarget")
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("editing", False)
        self._editing = False

    def start_edit(self) -> None:
        if self._editing or not self.isEnabled():
            return
        self._editing = True
        self.setReadOnly(False)
        self.setProperty("editing", True)
        theme.repolish(self)
        self.setCursor(Qt.IBeamCursor)
        self.selectAll()
        self.setFocus(Qt.OtherFocusReason)

    def finish_edit(self, commit: bool) -> None:
        if not self._editing:
            return
        self._editing = False
        self.setReadOnly(True)
        self.setProperty("editing", False)
        theme.repolish(self)
        self.setCursor(Qt.PointingHandCursor)
        if commit:
            self.edit_committed.emit(self.text())
        else:
            self.edit_cancelled.emit()

    def is_editing(self) -> bool:
        return self._editing

    # ---------- 事件 ----------
    def mouseDoubleClickEvent(self, event):                    # noqa: N802 (Qt 命名)
        self.start_edit()
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):                            # noqa: N802 (Qt 命名)
        key = event.key()
        if self._editing:
            if key == Qt.Key_Escape:
                self.finish_edit(commit=False)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.finish_edit(commit=True)
                return
            super().keyPressEvent(event)
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            step = KEY_STEPS["plain"]
            if event.modifiers() & Qt.ShiftModifier:
                step = KEY_STEPS["shift"]
            elif event.modifiers() & Qt.AltModifier:
                step = KEY_STEPS["alt"]
            self.nudge_requested.emit(step if key == Qt.Key_Up else -step)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.move_requested.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):                            # noqa: N802 (Qt 命名)
        if self._editing:
            self.finish_edit(commit=True)
        super().focusOutEvent(event)


class _ElidedLabel(QLabel):
    """按可用宽度截断的标签（完整文字靠 `setToolTip` 给）。

    为什么要它：卡片里的名字/摘要是"外部输入"（预设名可能是现场起的任意长度），
    不截断时 QLabel 的最小宽度等于整行文字宽度，会把卡片顶宽、把右边缘的「编辑」挤出可视区。

    **v6.3.0 修了一个会让进程段错误的回环**（把它放进卡头那行之后才暴露）：
    原来 `_apply` 无条件 `setText`，而它又挂在 `resizeEvent` 上 —— 于是
    「截断后的文字 → QLabel 的 sizeHint → 父布局重排 → 本控件 resize → 再截断」形成回路。
    在"按剩余空间分配宽度"的伸缩布局里，这个回路不收敛（宽度给大→文字变长→sizeHint 变大
    →布局收回宽度→文字变短…），而它是 **Qt 在 C++ 里递归**，所以既不抛 RecursionError
    也走不到 excepthook，直接段错误。两处修：
    ① **重入保护**：`_apply` 只在真的改了文字时才 `setText`（setText 会触发几何失效）；
    ② **不参与宽度协商**：`minimumSizeHint` 恒为一个小宽度 —— 这正是"可截断"该有的语义，
       顺带把"文字顶宽卡片"这个原始问题从根上治了（不再依赖调用方去估最小宽）。
    """

    #: 截断后最多占这么宽（小于它就没法看，布局应当先压缩别的东西）
    MIN_HINT_WIDTH = 24

    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._applying = False
        self._full = str(text)
        self._apply()

    def setText(self, text):                                   # noqa: N802 (Qt 命名)
        self._full = str(text)
        self._apply()

    def fullText(self) -> str:                                 # noqa: N802 (Qt 命名)
        """完整文字（显示的是截断版，完整值在悬停提示里）。"""
        return self._full

    def minimumSizeHint(self):                                 # noqa: N802 (Qt 命名)
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), self.MIN_HINT_WIDTH))
        return hint

    def resizeEvent(self, event):                              # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        if self._applying:                                     # 挡住 setText → 重排 → resize 的重入
            return
        self._applying = True
        try:
            metrics = self.fontMetrics()
            text = metrics.elidedText(self._full, Qt.ElideRight,
                                      max(self.width(), self.MIN_HINT_WIDTH))
            if text != QLabel.text(self):                      # 没变就别 setText（它会让布局失效）
                QLabel.setText(self, text)
        finally:
            self._applying = False


class _ClickLabel(_ElidedLabel):
    """双击可点的标签（"当前"那一格：双击 = 把这个值取到目标）。

    **v6.5.3 起继承 `_ElidedLabel`**（原先直接继承 `QLabel`）。原因是一处会误导读数的
    缺陷（现场报「躯干角度显示不全」）：纯 QLabel 配上 `setFixedWidth` + 右对齐时，
    文本比控件宽的部分会被 Qt **从左边裁掉** —— 于是

        -10.093  →  显示成  0.093
        -42.942  →  显示成  2.942

    **负号被吃掉**，读数会被反着理解（这在"照着界面填目标值"的场景里是直接的错误来源）。
    继承 `_ElidedLabel` 之后走 `Qt.ElideRight`：保留左侧（负号 + 整数部分）、省略号在右端，
    既**保得住符号**、又**看得出被截断了**（不是静默丢字符）。
    """

    double_clicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event):                    # noqa: N802 (Qt 命名)
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


# ============================================================
# 一行轴
# ============================================================
class AxisRow(QFrame):
    """一行轴 = 「轴名 │ 当前 │ 目标（双击改）」。

    与 v5.6.0 的差别就是"像当前角度那样"：**没有滑杆、没有 ± 按钮、没有精调条**。
    粗调用预设或"整组一起走"，精调用双击输入或键盘，一屏能看全 20 行。
    """

    changed = pyqtSignal(str)
    select_requested = pyqtSignal(str)
    take_requested = pyqtSignal(str)
    move_requested = pyqtSignal(str)
    invalid = pyqtSignal(str, str)

    def __init__(self, spec, parent=None):
        super().__init__(parent)
        self.setObjectName("AxisRow")
        self.key, self.low, self.high, self.hint = spec
        self._current = None
        self._syncing = False

        line = QHBoxLayout(self)
        # 上下留白 1 → 0（v6.3.0）：一行轴的上下各 1px 是 v5.6.0 为"选中行浅底"留的呼吸，
        # 但三张卡改成上下排列之后页高吃紧（一行轴的**行距** × 7 行直接进页高）。
        # 行距由 `rows_layout.setSpacing(1)` 那 1px 保留，选中行仍有分隔。
        line.setContentsMargins(SP["sp2"], 0, SP["sp2"], 0)
        line.setSpacing(SP["sp2"])

        name = QLabel(self.key)
        name.setObjectName("AxisName")
        name.setFixedWidth(scaled(26))       # 轴名两三个字符，字号一放大就得跟着放
        name.setToolTip(f"{self.key}（{self.hint}），量程 {self.low:g}~{self.high:g}°")
        line.addWidget(name)
        # 富余宽度**分成对称的两段**（v6.3.2，现场截图里轴名右侧那块空白太大）：
        # 一段留在轴名与读数之间、一段留在读数之后。原先全部收在轴名后一处，
        # 一列 320px 里就有 100 多 px 的空档堆在同一个位置，看着像"这一格没内容"；
        # 拆成两半之后每边只剩一半，视觉上就是普通的表格列间距。
        # **不能把两段都去掉** —— 那样 Qt 会把空档平摊到每两个格子之间，
        # `当前` 与「→ 目标」会被拆开，读数看起来像两列不相干的数。
        line.addStretch()

        self.current_label = _ClickLabel(UNKNOWN)
        self.current_label.setObjectName("AxisAngle")
        # 宽度**按当前字体度量算**，不再写死 62（v6.5.3）：写死的话，字号随
        # `theme.ui_scale()` 放大之后 `-42.942` 就装不下，Qt 会**从左边**裁掉文本、
        # 负号消失（现场报的「躯干角度显示不全」就是这个）。
        theme.repolish(self.current_label)          # 先把 QSS 落到字体上，再量宽度
        self.current_label.setFixedWidth(angle_column_width(self.current_label, 62))
        self.current_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.current_label.setToolTip("机器人侧 state 话题报的当前角度（不是目标值）；"
                                      "双击 = 把它取到目标")
        self.current_label.double_clicked.connect(lambda: self.take_requested.emit(self.key))
        line.addWidget(self.current_label)

        arrow = QLabel("→")
        arrow.setObjectName("AxisUnit")
        arrow.setFixedWidth(scaled(12))
        arrow.setAlignment(Qt.AlignCenter)
        line.addWidget(arrow)

        self.edit = TargetEdit()
        # 与"当前"那一格同一套宽度（目标值也是同一批字面量 —— 现场会填 `-32` 这类负数，
        # 同样是硬编码 74 在放大字号下装不下的问题）
        theme.repolish(self.edit)
        self.edit.setFixedWidth(angle_column_width(self.edit, 74))
        # 无障碍：屏幕阅读器读到的是"L4 目标角度"，不是"一个只读编辑框"
        self.edit.setAccessibleName(f"{self.key} 目标角度")
        self.edit.setAccessibleDescription(
            f"{self.hint}，量程 {self.low:g}~{self.high:g} 度。双击进入编辑，回车生效、Esc 取消。")
        self.edit.setToolTip(
            f"目标角度，量程 {self.low:g}~{self.high:g}°。「双击」改；回车提交、Esc 撤销。\n"
            f"只读时：↑↓ ±0.1°、Shift+↑↓ ±5°、Alt+↑↓ ±0.5°；回车 = 这一组一起走。")
        self.edit.edit_committed.connect(self._on_commit)
        self.edit.edit_cancelled.connect(self._restore_text)
        self.edit.nudge_requested.connect(self.nudge)
        self.edit.move_requested.connect(lambda: self.move_requested.emit(self.key))
        line.addWidget(self.edit)
        line.addStretch()                    # 富余的另一半（见上面轴名后那一段的说明）

        self._target = 0.0
        self._set_text(0.0)
        self.set_available(False, "状态未知")

    # ---------- 取值 / 设值 ----------
    def target(self) -> float:
        return float(self._target)

    def set_target(self, value: float) -> None:
        number = min(max(float(value), self.low), self.high)
        self._target = number
        self._set_text(number)

    def nudge(self, delta: float) -> None:
        if not self.edit.isEnabled():
            return
        self.set_target(self._target + float(delta))
        self.changed.emit(self.key)

    def current_value(self):
        return self._current

    def set_current(self, value) -> None:
        """写当前角度：`None` = 读不到（显示「—」并降级字号，**不显示 0**）。

        格式与目标列**共用 `fmt_number`**（v6.3.0 修）：左边曾经走 `f"{v:.1f}"`、
        右边走 `fmt_number`，于是同一行会出现 `0.0 → 0`、`12.0 → 12` 而下一行又是
        `-42.1 → -42.1` —— 相邻两列两套规则，读的人会怀疑这两个数不是一个精度。
        统一到 `fmt_number` 是最小的改法：不动数值、不吃掉用户输入的精度
        （填 `45.25` 仍显示 45.25），只统一"怎么写"。
        """
        self._current = None if value is None else float(value)
        if self._current is None:
            self.current_label.setText(UNKNOWN)
            theme.set_state(self.current_label, "unknown")
        else:
            self.current_label.setText(fmt_number(round(self._current, 4)))
            theme.set_state(self.current_label, "none")

    def delta(self):
        if self._current is None:
            return None
        return self._target - self._current

    def on_take_current(self) -> bool:
        if self._current is None:
            return False
        self.set_target(self._current)
        self.changed.emit(self.key)
        return True

    # ---------- 文本与状态 ----------
    def _set_text(self, value: float) -> None:
        self._syncing = True
        try:
            self.edit.setText(f"{fmt_number(round(value, 4))}")
        finally:
            self._syncing = False

    def _restore_text(self) -> None:
        self._set_text(self._target)

    def _on_commit(self, text: str) -> None:
        raw = (text or "").strip().replace("°", "")
        try:
            number = float(raw)
        except ValueError:
            self._restore_text()
            self.invalid.emit(self.key, f"{self.key} 不是数字：{text!r}")
            return
        if number < self.low or number > self.high:
            # 越界只拦下并说明（不静默钳位），输入框退回原值免得看着像生效了
            self._restore_text()
            self.invalid.emit(self.key, f"{self.key}（{self.hint}）{fmt_number(number)} "
                                        f"超出范围 {self.low:g}~{self.high:g}°")
            return
        self._target = number
        self._set_text(number)
        self.changed.emit(self.key)

    def refresh_style(self) -> None:
        """按"目标 vs 当前"上色（与当前角度区同一套：不一样就用强调色）。"""
        delta = self.delta()
        changed = delta is not None and abs(delta) > 0.05
        theme.set_state(self.edit, "changed" if changed else "none")
        tip = (f"目标 {self._target:.1f}°（当前 {self._current:.1f}°，差 {delta:+.1f}°）"
               if changed else f"目标 {self._target:.1f}°（与当前一致）")
        self.edit.setToolTip(tip + "\n双击改；回车提交、Esc 撤销；只读时 ↑↓ ±0.1°")

    def is_available(self) -> bool:
        return self.edit.isEnabled()

    def set_available(self, available: bool, reason: str = "") -> None:
        """能不能改 / 能不能动；不给理由地灰着会被读成"坏了"（与其它控制页同一条）。"""
        self.edit.setEnabled(available)
        if not available:
            self.edit.finish_edit(commit=False)
        suffix = "" if available else f"不可用：{reason}"
        self.setToolTip(suffix)
        if available:
            self.refresh_style()

    def set_selected(self, selected: bool) -> None:
        theme.set_state(self, "selected" if selected else "none")

    # ---------- 事件 ----------
    def mousePressEvent(self, event):                          # noqa: N802 (Qt 命名)
        self.select_requested.emit(self.key)
        super().mousePressEvent(event)


#: 兼容旧名字（用例与旧文档里出现的是 `_AxisRow`）
_AxisRow = AxisRow


# ============================================================
# 预设卡
# ============================================================
class PresetCard(QFrame):
    """一张预设卡：「名字 + 摘要 + 编辑」。

    为什么是卡片而不是一行小按钮（现场意见）：卡片能把"这条预设到底会做什么"（几轴 /
    几步 / 含不含手型 / 来源）一起说清，按钮只能塞下名字。点卡片 = 运行，点「编辑」= 编辑/另存。

    **卡上不放迷你示意图**（设计稿里提过、实现两次都去掉）：卡片是并排的，
    图每宽 1px 就把整页顶宽 1px —— 而"会运动到什么位置"这件事在**确认框**里已经
    用大图说清了，那里才是做决定的地方。

    **键盘可达（v6.3.0 补）**：以前这张卡只能鼠标点 —— 键盘用户根本跑不了预设
    （WCAG 2.1.1 键盘可达性的一个真缺口）。现在卡片可 `Tab` 聚焦、`回车/空格` 运行、
    `Esc` 移开焦点，聚焦态由 QSS 的 `#PresetCard:focus` 画强调描边。
    在卡片上按回车**不会直接动机器人** —— 它走的是与鼠标点击同一条路（`run_requested`
    → 运动前二次确认），而确认框的默认按钮是「取消」。
    """

    #: 三个信号都带 ``(kind, key)``：卡片自己知道是躯干还是手臂，调用方不必再反查
    run_requested = pyqtSignal(str, str)
    edit_requested = pyqtSignal(str, str)
    delete_requested = pyqtSignal(str, str)

    #: 卡片定高：一列里高度不齐会让人以为"下面那条还没加载出来"。
    #: 56 → 60（v6.3.2）：摘要折成两行（`preset_summary` 里那个 `\n`），
    #: 名字一行 + 摘要两行需要约 54px，60 留一点余量。
    HEIGHT = 60

    @classmethod
    def height_scaled(cls) -> int:
        """卡片在当前缩放下的实际高度。

        `HEIGHT` 是**基准值**（开发机上的 60）。抽成方法而不是在 `__init__` 里现算：
        姿势页的 `_shrink_for_screen` 也要拿"一行卡片多高"去算压缩量，
        两边必须是同一个数 —— 各算各的迟早会错开。
        """
        return scaled(cls.HEIGHT)

    def __init__(self, kind: str, item: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("PresetCard")
        self.kind = kind
        self.key = item["key"]
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(item.get("tooltip") or "")
        self.setFixedHeight(self.height_scaled())
        # 键盘可达：Tab 能停在卡片上，回车/空格 = 跑这一条
        self.setFocusPolicy(Qt.StrongFocus)
        name = item.get("name", "?")
        self.setAccessibleName(f"预设 {name}")
        self.setAccessibleDescription(
            f"{item.get('summary', '')}。回车运行（会二次确认），"
            f"右侧按钮改这一条，右键删除自定义预设。")

        row = QHBoxLayout(self)
        row.setContentsMargins(SP["sp3"], SP["sp1"], SP["sp1"], SP["sp1"])
        row.setSpacing(SP["sp2"])

        # 名字与摘要**各占一行**、等宽左对齐；来源标记单独一列靠右 ——
        # 挤进同一列会把名字压到只剩几个字（实测：带「上位机」标记时缩成"我…"）
        text = QVBoxLayout()
        text.setSpacing(0)
        text.setContentsMargins(0, 0, 0, 0)
        # 名字用 `_ElidedLabel`：它自带重入保护、且 `minimumSizeHint` 恒为小宽度，
        # **不参与宽度协商**。手写"在 resizeEvent 里按宽度截断"会踩死循环 ——
        # 名字文字变短 → 卡的最小宽变小 → 网格重排 → 再触发 resizeEvent
        # （v6.3.2 实测：直接 `Fatal Python error: Segmentation fault`，
        # 与 v6.3.0 `_ElidedLabel` 那次是同一个坑，只是这次从卡片这头进来的）。
        self.name_label = _ElidedLabel(item.get("name", "?"))
        self.name_label.setObjectName("PresetName")
        # 摘要**完整显示、不截断**（v6.3.2）：它说的是"这一下会做什么"，截掉一半等于没说。
        # 宽度靠**把卡片给够**解决，不靠换行 —— 见 `MIN_WIDTH` 里那段推算。
        #
        # **刻意不设 `setWordWrap`**：QLabel 一旦开自动换行，它的高度就依赖宽度
        # （`heightForWidth`），而卡片是定高的 —— 在网格里会和布局反复协商，
        # 实测直接把进程搞成段错误（`Fatal Python error: Segmentation fault`，
        # 与 v6.3.0 `_ElidedLabel` 那次是同一类回环）。
        self.sum_label = QLabel(item.get("summary", ""))
        self.sum_label.setObjectName("PresetSum")
        text.addWidget(self.name_label)
        text.addWidget(self.sum_label)
        row.addLayout(text, 1)

        if item.get("source") == "custom":
            tag = QLabel("上位机")
            tag.setObjectName("PresetSource")
            tag.setToolTip("这条来自上位机自己的 configs/motion_presets.yaml（右键可删）")
            row.addWidget(tag)
        if item.get("problems"):
            theme.set_state(self, "warn")

        self.btn_edit = QPushButton("编辑")
        self.btn_edit.setObjectName("PresetEdit")
        self.btn_edit.setProperty("variant", "ghost-compact")
        # 34×22：原来 30px 宽只放得下单字「改」——中文界面里的单字按钮像缩写，
        # 而且"改什么"没有宾语。两个字才是完整动词（v6.3.0）。宽度只加 4px：
        # `#PresetEdit` 这条 QSS 已经把内距清零、字号压到 micro，两个字（≈22px）放得下。
        self.btn_edit.setFixedSize(34, 22)
        self.btn_edit.setToolTip("改这一条：机器人侧的预设会另存成上位机自定义预设"
                                                 "（机器人侧文件一个字都不动）")
        self.btn_edit.clicked.connect(
            lambda: self.edit_requested.emit(self.kind, self.key))
        row.addWidget(self.btn_edit)

    def set_selected(self, selected: bool) -> None:
        theme.set_state(self, "selected" if selected else "none")

    # 这里原先有一个 `resizeEvent` + `_elide`：按可用宽度给名字与摘要加省略号。
    # v6.3.2 全部撤掉 ——
    #   * 名字交给 `_ElidedLabel`（自带重入保护，且 `minimumSizeHint` 恒为小宽度）；
    #   * 摘要改成**显式折行、不截断**（`preset_summary` 里那个 `\n`）。
    # 撤掉的原因是那个 `resizeEvent` 会把进程搞成段错误：它改了文字 → 文字宽变 →
    # 卡的最小宽变 → 网格重排 → 再次 `resizeEvent`，不收敛（Qt 在 C++ 里递归）。
    def mousePressEvent(self, event):                          # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            # 点到这张卡也把它变成键盘焦点：否则 Tab 的起点和鼠标不在同一处，
            # 点完再按 Tab 会跳到别的地方（键盘/鼠标混用时的老毛病）。
            self.setFocus(Qt.MouseFocusReason)
            self.run_requested.emit(self.kind, self.key)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):                            # noqa: N802 (Qt 命名)
        """回车 / 空格 = 跑这一条；Esc = 移开焦点。

        **和鼠标点击走同一条路**（`run_requested` → 运动前二次确认），所以键盘不会
        "绕过确认"；确认框的默认按钮还是「取消」，闭眼回车不会动机器人。
        """
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.run_requested.emit(self.kind, self.key)
            event.accept()
            return
        if key == Qt.Key_Escape:
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):                         # noqa: N802 (Qt 命名)
        self.delete_requested.emit(self.kind, self.key)
        event.accept()


# ============================================================
# 模块使能（并入本页）
# ============================================================
class ModuleEnableRow(QFrame):
    """一个模块的使能行：名字 + 状态徽章 + 按钮（躯干 / 双臂并排放在一行里）。

    **为什么平时只给一个按钮**（现场要求）：检测到"已使能"就只显示「下使能」，未使能就只
    显示「上使能」。两个按钮都摆出来，等于让人自己判断该点哪个 —— 判断错了就是**往已使能
    的状态上再发一次 connect**，或者在下完之后又点一次下使能；而且"哪个能点"这件事
    界面明明已经知道（它每 400ms 读一次状态），不该推给现场。

    **唯一的例外是「只通了一侧」**（v6.4.10，现场报的）：那种状态下主按钮按上面的规则
    显示「上使能」（补的是没通的那一侧），于是**已经通上的那一侧没有下电出口** ——
    现场只能去关程序或直接断电。所以这一态额外给一个「全部下电」，其余两态不显示，
    "该点哪个"依然不推给现场。

    并入姿势控制的原因很直接：**上使能是这一页的前置条件**（没使能就没有真实角度、
    也不能运动）。独立页在 v6.0.0 已按现场要求删除，判据与步骤仍走 `MODULE_STEPS` /
    `module_state_of`（与「关闭程序」前的下电序列是同一批话题）。
    """

    action_requested = pyqtSignal(str, bool)
    #: 躯干**自锁**：True = 自锁（仅上电 / 退出使能），False = 解除自锁。
    #: 单独一个信号而不是塞进 `action_requested` —— 后者是"使能/下使能"那条
    #: `MODULE_STEPS` 路径（两个模块共用），自锁只有躯干有（SDK 只给躯干这个接口）。
    #: 点「自锁」按钮触发。**无参** —— 这个动作只有一个方向（见 `set_lock_state`）。
    lock_requested = pyqtSignal()

    def __init__(self, module: str, name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ModuleEnableRow")
        self.module = module
        self.name = name
        #: 下一次点击的含义：True = 上使能，False = 下使能。由页面按检测到的状态设置。
        self._turn_on = True
        row = QHBoxLayout(self)
        row.setContentsMargins(SP["sp2"], SP["sp1"], SP["sp2"], SP["sp1"])
        row.setSpacing(SP["sp2"])

        title = QLabel(name)
        title.setObjectName("AxisName")
        row.addWidget(title)

        self.state_badge = badge(f"{name} —")
        row.addWidget(self.state_badge, 1)

        # 躯干专属的「自锁」（v6.4.13）：把 H1~H3 切成"仅上电"（退出使能）——
        # 还能读角度，但不接受运动指令。**只给 trunk 造**：SDK 里只有躯干有
        # `switch_power_on`，双臂没有对应接口，给双臂摆一个按不动的按钮是误导。
        # 默认不显示，由页面按"检测到的使能状态 + 本页下发过什么"决定（见 mixin）。
        self.btn_lock = None
        if module == "trunk":
            self.btn_lock = QPushButton("自锁")
            self.btn_lock.setProperty("variant", "ghost-compact")
            self.btn_lock.setVisible(False)
            self.btn_lock.clicked.connect(self._on_lock_clicked)
            row.addWidget(self.btn_lock)

        #: 「全部下电」只在"只通了一侧"时出现（见类注释）。它走的是**下使能**那条
        #: 既有路径，不新增话题、不新增动作 —— 现场验证过的链路不变。
        self.btn_disable = QPushButton("全部下电")
        self.btn_disable.setProperty("variant", "ghost-compact")
        self.btn_disable.setToolTip(
            f"断开{name}（两侧一起，机构失去保持力）\n"
            "只在「只通了一侧」时出现：那种状态下主按钮是「上使能」，没有下电出口")
        self.btn_disable.clicked.connect(self._on_disable_clicked)
        self.btn_disable.setVisible(False)
        row.addWidget(self.btn_disable)

        self.btn = QPushButton("上使能")
        self.btn.setProperty("variant", "ghost-compact")
        self.btn.clicked.connect(self._on_clicked)
        self.set_next_action(True)
        row.addWidget(self.btn)

    def set_next_action(self, turn_on: bool) -> None:
        """设置"这一次点击会做什么"，按钮文案跟着走。"""
        self._turn_on = bool(turn_on)
        self.btn.setText("上使能" if self._turn_on else "下使能")

    def set_partial(self, partial: bool) -> None:
        """「只通了一侧」时多给一个下电出口；其余状态收起来。"""
        self.btn_disable.setVisible(bool(partial))

    def set_lock_state(self, visible: bool, enabled: bool, why: str) -> None:
        """自锁按钮的一处收口：显示 / 文案 / 可用 / 提示语，一次定完。

        **文案恒为「自锁」，不再有两态。** 原因是一条架构判断（2026-10-19 现场教会的）：

        自锁状态既**读不回来**（`getMotorBrakeState()` 只在 C++ SDK 里），它的
        **生命周期也不归上位机** —— 现场明确「重启遥操作，自锁状态就重置」。
        所以上位机"记得自己下发过什么"并不能推出"现在是不是自锁"；按那个记录把按钮
        翻成「解除自锁」，就可能在**真实未锁**的时候发出解除指令（那一下会
        `connect true`、躯干抬头）—— 这正是"点了一下、机构反而动了"的来源。

        自锁指令本身**幂等**（重复发只是让电机再退出一次使能），所以"永远只发自锁"
        既安全又不会让现场困惑。解除方式：重启遥操作。
        """
        if self.btn_lock is None:
            return
        self.btn_lock.setText("自锁")
        self.btn_lock.setVisible(bool(visible))
        self.btn_lock.setEnabled(bool(enabled))
        self.btn_lock.setToolTip(why)

    def _on_lock_clicked(self) -> None:
        self.lock_requested.emit()

    def _on_clicked(self) -> None:
        self.action_requested.emit(self.module, self._turn_on)

    def _on_disable_clicked(self) -> None:
        # 下电走既有的下使能路径（connect false 断的是两侧，与文案一致）
        self.action_requested.emit(self.module, False)



# ============================================================
# 卡片构造（mixin）
# ============================================================
def scaled(value: int) -> int:
    """把**基准像素**按当前全局缩放系数换算成实际像素。

    为什么要它：这一页的列表是**定高**的（超出滚动）。行高会随字号放大
    （轴行 27 → 36 @1.33），而定高不跟的话，手臂那 8 个轴就会被藏到滚动条后面 ——
    那不是"变大"，是"变少"。所以定高必须与行高同步放大。

    基准值（类常量）保持原样：`theme.ui_scale()` 在未配置缩放的机器上返回 1.0，
    于是开发机与既有守卫量的都还是 214 / 124 / 60。
    """
    return max(1, round(value * theme.ui_scale()))


#: 读数那一格要放得下的**最坏情况字面量**。读数是 `fmt_number(round(v, 4))` 出来的，
#: 最长形态就是"负号 + 3 位整数 + 小数点 + 4 位小数"—— 躯干 H2 的量程是 ±150，
#: 所以是 `-150.0929`（9 个字符）。宽度只跟**字体**有关，与当时的具体数值无关。
ANGLE_WIDEST_TEXT = "-150.0929"


def angle_column_width(widget, base: int) -> int:
    """「当前 / 目标」这两格该多宽：按**当前字体**量最宽的字面量，再套 `scaled()`。

    为什么不能硬编码（v6.5.3 修的缺陷）：这一页的行高、卡片高都跟着
    `theme.ui_scale()` 一起放大，而 `setFixedWidth(62)` / `setFixedWidth(74)` 这种
    硬编码**不跟** —— 字号一大，`-42.942` 就装不下，Qt 会**从左边**把文本裁掉：

        -42.942  →  2.942      -10.093  →  0.093      （负号消失）

    现场报的「躯干角度显示不全」就是这个。修法有两层：宽度按字体度量算（这里），
    以及控件本身改成带省略号的截断（`_ClickLabel`）—— 后者保证**即使还装不下，
    也绝不会把负号丢掉**。

    `base` 是原硬编码的基准值，用 `max` 兜住：新算出来的只会比它宽，不会更窄。
    """
    metrics = widget.fontMetrics()
    needed = metrics.horizontalAdvance(ANGLE_WIDEST_TEXT) + scaled(10)
    return max(scaled(base), needed)
