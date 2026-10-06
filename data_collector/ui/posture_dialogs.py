"""「姿势控制」页的两个**模态框**：运动前二次确认、预设编辑/另存。

从 `posture_widgets.py` 拆出来的（v6.3.0）：那一册到 1022 行，过了项目「单文件 1000 行」
的红线（`test_ui_structure.py::test_no_single_ui_file_balloons` 钉着它）。拆的缝选在
**"弹一个框"和"铺一个页"之间**：

- 这里的两类都 `setModal(True)`、都有 `QDialogButtonBox`、都用 `exec_()`
  —— 它们的生命周期是"阻塞一句话、拿一个 yes/no 或一份 payload 回来"；
- `posture_widgets.py` 剩下的（轴行 / 预设卡 / 使能行 / 卡片骨架）全是**常驻在页面上**、
  跟着 500ms 周期刷新的件。

`posture.py` 依旧把这两个名字再导出一遍，所以 `posture._MoveConfirmDialog.exec_`、
`posture.PresetEditDialog.exec_` 这些 monkeypatch 的写法照旧有效。
"""
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QWidget)

from .. import theme
from ..core_motion import (ARM_AXES, ARM_NAMES, DEFAULT_SCALE, HAND_FINGERS,
                           TRUNK_AXES, check_axis_values)
from .common import SP, field_label

#: 预设编辑框里两列的小标题。**不带单位** —— 那个框里每个格子自己带 `°` 后缀，
#: 标题再写一遍 `· °` 是重复；页面上那三列轴表才需要标题带单位（21 行裸数字没法标）。
ARM_COLUMN_TITLES = {"left": "左臂 L1~L7（从肩起）",
                     "right": "右臂 R1~R7（从肩起）"}


# ============================================================
# 运动确认框（带示意图）
# ============================================================
class _MoveConfirmDialog(QDialog):
    """运动前二次确认：动作摘要 + 「当前 → 目标（差值）」+ 示意图。

    为什么自己搭一个而不是用 `QMessageBox`：这里要放**示意图** —— "会运动到什么位置"
    正是最容易出事的地方，光靠文字描述姿势，现场判断不了。默认按钮是「取消」
    （与换末端类型同一条理由：闭眼回车不该动机器人）。
    """

    def __init__(self, parent, title: str, summary, diagram: QWidget, extra: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        hint = diagram.sizeHint()
        diagram.setMinimumSize(max(hint.width(), 190), max(hint.height(), 190))
        # 宽图（双臂是"正视 + 侧视"两块，472px）**走上下**：并排时图会把文字挤成一条窄柱
        # （实测 609px 的框里文字每行只剩 4~5 个字，读不了）。窄高图（躯干侧视）仍走左右。
        wide = hint.width() > 340
        body = QVBoxLayout() if wide else QHBoxLayout()
        body.setSpacing(SP["sp4"])
        body.addWidget(diagram)

        text_box = QVBoxLayout()
        text_box.setSpacing(SP["sp1"])
        for line in summary:
            label = QLabel(line)
            label.setObjectName("Hint")
            label.setWordWrap(True)
            text_box.addWidget(label)
        if extra:
            note = QLabel(extra)
            note.setObjectName("Hint")
            note.setWordWrap(True)
            theme.set_state(note, "warn")
            text_box.addWidget(note)
        caption = QLabel("示意图：按 URDF 正运动学算出来的形状（不是几何精确的渲染）—— "
                         "虚线的灰影是当前位置，实线是目标")
        caption.setObjectName("Hint")
        caption.setWordWrap(True)
        text_box.addWidget(caption)
        text_box.addStretch()
        body.addLayout(text_box, 1)
        layout.addLayout(body)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        confirm = buttons.addButton("确认运动", QDialogButtonBox.AcceptRole)
        cancel.setDefault(True)
        confirm.setProperty("variant", "primary")
        theme.repolish(confirm)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# ============================================================
# 预设编辑框
# ============================================================
class PresetEditDialog(QDialog):
    """新建 / 修改一条预设（写到**上位机自己**的 `configs/motion_presets.yaml`）。

    为什么机器人侧的预设也只能"另存成上位机预设"：`install/product/config/*_motion.json`
    是产品交付物 —— 示教脚本、手柄、别的版本界面都读它，从上位机里改一个预设会静默改掉
    它们的语义，也会被下次交付覆盖。所以对机器人侧的预设点是"另存为"，来源如实标注。
    """

    def __init__(self, parent, kind: str, name: str, trunk_values, arm_values,
                 hand_text: str = ""):
        super().__init__(parent)
        self.kind = kind
        self.setWindowTitle("预设 · " + ("躯干" if kind == "trunk" else "手臂"))
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        head = QHBoxLayout()
        head.setSpacing(SP["sp2"])
        head.addWidget(field_label("预设名"))
        self.name_edit = QLineEdit(name or "")
        self.name_edit.setPlaceholderText("例如：我的站直")
        head.addWidget(self.name_edit, 1)
        layout.addLayout(head)

        note = QLabel("保存到上位机自己的 configs/motion_presets.yaml；"
                      "机器人侧的 JSON 一个字都不会改。值都是「度」。")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        if kind == "trunk":
            self.spins = {}
            grid = QGridLayout()
            grid.setSpacing(SP["sp2"])
            for index, (key, low, high, hint) in enumerate(TRUNK_AXES):
                cell = QVBoxLayout()
                cell.setSpacing(2)
                cell.addWidget(field_label(f"{key} · {hint}"))
                spin = self._spin(low, high)
                spin.setValue(float((trunk_values or [0.0] * 6)[index]))
                cell.addWidget(spin)
                self.spins[key] = spin
                grid.addLayout(cell, index % 3, index // 3)
            layout.addLayout(grid)
            self.hand_edit = None
        else:
            self.arm_spins = {"left": {}, "right": {}}
            columns = QHBoxLayout()
            columns.setSpacing(SP["sp4"])
            for side in ("left", "right"):
                block = QVBoxLayout()
                block.setSpacing(SP["sp2"])
                title = QLabel(ARM_COLUMN_TITLES[side])
                title.setObjectName("SectionTitle")
                block.addWidget(title)
                values = (arm_values or {}).get(side) or [0.0] * 7
                for index, (key, low, high, hint) in enumerate(ARM_AXES[side]):
                    line = QHBoxLayout()
                    line.setSpacing(SP["sp2"])
                    label = field_label(f"{key} · {hint}")
                    label.setFixedWidth(64)
                    line.addWidget(label)
                    spin = self._spin(low, high)
                    spin.setValue(float(values[index]))
                    self.arm_spins[side][key] = spin
                    line.addWidget(spin, 1)
                    block.addLayout(line)
                columns.addLayout(block, 1)
            layout.addLayout(columns)
            head2 = QHBoxLayout()
            head2.setSpacing(SP["sp2"])
            head2.addWidget(field_label("手型（6 个 0~1000）"))
            self.hand_edit = QLineEdit(hand_text or "")
            self.hand_edit.setPlaceholderText("留空 = 这个预设不动手型；例如 100, 100, 100, 100, 500, 600")
            head2.addWidget(self.hand_edit, 1)
            layout.addLayout(head2)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        save = buttons.addButton("保存到上位机", QDialogButtonBox.AcceptRole)
        cancel.setDefault(True)
        save.setProperty("variant", "primary")
        theme.repolish(save)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _spin(low: float, high: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        spin.setSuffix("°")
        spin.setFixedWidth(88)
        # `NoButtons`：主题给输入框的 7px 内距会把上下箭头压扁（NOTES-gui 里记着这个坑），
        # 定宽 88px 下最明显 —— 纯数值格用键盘上下键与滚轮一样能调
        spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
        return spin

    def payload(self):
        """返回 ``(名字, 值, 问题列表)``；`值`：躯干 = 6 个度，手臂 = 步骤数组。"""
        name = (self.name_edit.text() or "").strip()
        if not name:
            return "", None, ["预设名不能为空"]
        if self.kind == "trunk":
            values = [float(self.spins[key].value()) for key, _l, _h, _hint in TRUNK_AXES]
            problems = check_axis_values(TRUNK_AXES, values)
            return name, values, problems
        step = {}
        problems = []
        for side in ("left", "right"):
            values = [float(self.arm_spins[side][key].value())
                      for key, _l, _h, _hint in ARM_AXES[side]]
            problems += check_axis_values(ARM_AXES[side], values,
                                          f"{ARM_NAMES[side]}")
            step[f"{side}_arm"] = values
        text = (self.hand_edit.text() or "").strip()
        if text:
            items = [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
            try:
                positions = [int(round(float(item))) for item in items]
            except ValueError:
                problems.append(f"手型里不是数字：{text!r}")
                positions = []
            if positions:
                if len(positions) != len(HAND_FINGERS):
                    problems.append(f"手型应有 {len(HAND_FINGERS)} 个值，收到 {len(positions)} 个")
                for index, value in enumerate(positions, start=1):
                    if value < 0 or value > 1000:
                        problems.append(f"手型第 {index} 个值 {value} 超出 0~1000")
                if not problems:
                    step["left_end_effector"] = positions
                    step["right_end_effector"] = list(positions)
        step["scale"] = DEFAULT_SCALE
        return name, [step], problems
