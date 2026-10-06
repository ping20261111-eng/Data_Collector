"""「末端参数」独立页面：改 `arm_control_config.yaml` 里末端的两组指令（张开 / 握紧）。

与「模块使能」「夹爪控制」同一族（工具行入口、独立非模态、单例、动作中拦「关闭程序」），
但有一条本质区别：

    另两页是"向机器人下发一条指令"，这一页是"改机器人侧节点**启动时读**的配置"。

于是三条性质与它绑定：① 保存成功必须提醒**重启主程序**；② 写的是别人维护的配置文件，
**按行精准替换、注释与其它手型段逐字保留**（见 `core_operations.patch_arm_control_text`）；
③ 保存后**回读校验** —— 写文件没有退出码，唯一可信的判据是重新读出来相等。

**末端类型是左右共用的**（现场 2026-09-28 指出：末端控制器只能左右用一个类型）：
所以页面只有**一个**类型下拉，保存时把 `left_end_effector_type` 与
`right_end_effector_type` 一起写成同一个值；两臂各自的**指令值**仍然分开
（文件里本来就是四组）。文件里两侧不一致时页面上挂一条 warn，保存即统一。

版面照 `ui-proposals/末端参数调整_设计稿.html`：白卡片（`QFrame#Panel` + `PanelHead`）
+ 药丸徽章（`QLabel#Badge`）+ 三段（头部信息 / 左右两臂 / 操作结果），按钮行在最下面。
"""
from datetime import datetime

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractSpinBox, QComboBox, QDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox,
                             QVBoxLayout)

from .. import theme
from ..core_operations import (END_EFFECTOR_CMDS, END_EFFECTOR_CMD_TEXT,
                              END_EFFECTOR_DEFAULTS, END_EFFECTOR_EDITABLE,
                              END_EFFECTOR_GROUPS, END_EFFECTOR_NAMES,
                              end_effector_label,
                              END_EFFECTOR_SECTIONS, end_effector_slot_ranges)
from .common import (SP, badge, dialog_card, field_label, fit_console,
                     fit_dialog_to_screen)
from .safety import guard_slot
from .threads import LocalTaskThread
from .widgets import LogView


class EndEffectorDialog(QDialog):
    """顶部一张卡（共用的末端类型 + 配置文件路径）+ 左右两臂各一张卡 + 结果卡。

    | 手型 | 格子数 | 含义 |
    | --- | --- | --- |
    | 灵巧手（yinshi） | 12 | 前 6 手指角度(0~1000) + 后 6 力控(0~3000) |
    | 宇树夹爪（yushu） | 3 | 开合速度 · 开合位置 · 力矩，千分比 0~1000（位置越大越闭合）|

    四条与"改配置"打交道时踩得出的约定：

    1. **读不出来就不给改**：文件缺失 / 解析失败 / 结构不对时，输入与保存全部禁用，
       页面上写明原因（灰着不给理由会被读成"坏了"）；
    2. **类型下拉只列文件里真实存在的四种**，其中 zhixing / hangkai **置灰不可选**
       （本期不做，文件里保留原样）。文件里当前就是那两种时下拉留空、由徽章说明
       "文件里：…" —— 不猜、不假装已经读到了；
    3. **换类型要二次确认**（默认「取消」）：类型决定节点按哪套协议驱动末端，装错就是不动
       甚至报错；而只调数值是现场最常做的调参，每次都弹框会让人闭眼点"是"；
    4. **保存后必须说"需重启主程序生效"**：`arm_control_node` 只在启动时读一次参数。

    输入框按类型给范围（越界根本填不进去），**不做静默钳位** —— 钳位会让现场以为自己填的值
    生效了，实际被悄悄改掉。格子用 `NoButtons` 的 QSpinBox：12 个格子各带一对上下箭头时
    既挤又吵（主题给输入框的右内距会把箭头压扁，实测就是"每个数字后面挂一个残缺的小方块"），
    与设计稿里的"纯数值格"也不一致；键盘上下键与滚轮仍然可用。
    """

    #: 内容宽度：指令名 96 + 组名 72 + 6 个格子 76×6 + 间距 + 单位说明
    MIN_WIDTH = 860
    #: 单个数值格的宽度（`NoButtons` 之后 76px 足够放下 4 位数 + 两侧内距）
    SLOT_WIDTH = 76
    #: 指令名列宽度（「张开指令」四个字 + 余量）
    CMD_WIDTH = 96
    #: 组名列宽度（「速度 · 位置 · 力矩」这种长组名要放得下）
    GROUP_WIDTH = 84
    #: 结果区的高度范围（行数）：下限让它在空白时也像个"结果区"，上限之外在框内滚动。
    #: 固定高度（原先 140px）在"只有两行"的那一屏里会剩一大片空白 —— 结果区没有边框、
    #: 底色又与卡片同色，那片空白看起来就是"卡片空了一块"，现场会读成"内容没显示出来"。
    CONSOLE_MIN_ROWS = 3
    CONSOLE_MAX_ROWS = 9

    #: 保存动作进行中置真，供主窗口拦「关闭程序」
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None, config=None, launcher=None):
        super().__init__(parent)
        self.config = config
        self.launcher = launcher
        self.state = {}                       # 最近一次 read_end_effector_config() 的结果
        self._saving = False
        self._loading = False                 # 程序化填值期间不置"未保存修改"
        self._dirty = False
        self._file_types = {"left": None, "right": None}
        self.file_badges = {}
        self.unit_labels = {}

        self.setWindowTitle("末端参数")
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        layout.addWidget(self._build_head_card())
        self.sides = {"left": self._add_side_card(layout, "left"),
                      "right": self._add_side_card(layout, "right")}
        layout.addWidget(self._build_result_card())
        layout.addLayout(self._build_button_row())

        self.setMinimumWidth(self.MIN_WIDTH)
        #: 是否已经按屏幕高度收过一次（只做一次；用户手动拖过大小之后不该再动它）
        self._screen_fitted = False
        self.refresh()

    # ---------- 版面 ----------
    @staticmethod
    def _card(title: str):
        """一张白卡片 = `Panel` + `PanelHead` + 内容区（实现挪到 `common.dialog_card`，
        三个控制页面共用同一套版面）。"""
        return dialog_card(title)

    @staticmethod
    def _badge(text: str):
        """药丸徽章（`QLabel#Badge`）—— 与顶栏、打点面板同一套形状语言。"""
        return badge(text)

    def _build_head_card(self) -> QFrame:
        """头部卡：标题 + 三个徽章 + 共用的末端类型 + 配置文件路径。"""
        card, head_row, body = self._card("末端参数")

        self.hand_badge = self._badge("机型末端 —")
        head_row.addWidget(self.hand_badge)

        self.state_badge = self._badge("配置未读取")
        head_row.addWidget(self.state_badge)

        self.dirty_badge = self._badge("未修改")
        head_row.addWidget(self.dirty_badge)

        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        row.addWidget(field_label("末端类型"))
        self.type_combo = QComboBox()
        self.type_combo.setInsertPolicy(QComboBox.NoInsert)
        self.type_combo.setMinimumWidth(190)
        for section in END_EFFECTOR_SECTIONS:
            self.type_combo.addItem(end_effector_label(section), section)
            if section not in END_EFFECTOR_EDITABLE:
                # 不可选的项照样列出来：现场需要看到"文件里现在是哪一种"，但本期不给改
                item = self.type_combo.model().item(self.type_combo.count() - 1)
                item.setEnabled(False)
                item.setToolTip("本期暂不支持编辑（文件里保留原样）")
        self.type_combo.currentIndexChanged.connect(
            lambda _index: self.on_type_changed())
        row.addWidget(self.type_combo)

        self.depth_badge = self._badge("深度控制 —（只读）")
        row.addWidget(self.depth_badge)
        row.addStretch()

        self.type_note = QLabel("")
        self.type_note.setObjectName("Hint")
        self.type_note.setWordWrap(True)
        row.addWidget(self.type_note)
        body.addLayout(row)

        self.path_label = QLabel("配置文件 —")
        self.path_label.setObjectName("Hint")
        self.path_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        self.path_label.setWordWrap(True)
        body.addWidget(self.path_label)
        return card

    def _add_side_card(self, layout, side: str) -> dict:
        """一侧一张卡：卡头（侧名 + 文件里那种类型 + 写哪个段）+ 两组指令的格子。"""
        card, head_row, body = self._card("左臂" if side == "left" else "右臂")

        file_badge = self._badge("文件里：—")
        self.file_badges[side] = file_badge
        head_row.addWidget(file_badge)

        note = QLabel("")
        note.setObjectName("Hint")
        note.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        head_row.addWidget(note)

        grid = QGridLayout()
        grid.setHorizontalSpacing(SP["sp2"])
        grid.setVerticalSpacing(SP["sp1"])
        cmd_labels, tags, spins_box = {}, {}, {}
        for cmd_index, cmd in enumerate(END_EFFECTOR_CMDS):
            label = QLabel(END_EFFECTOR_CMD_TEXT[cmd])
            label.setObjectName("Hint")
            label.setFixedWidth(self.CMD_WIDTH)
            cmd_labels[cmd] = label
            spins = []
            for group_index in (0, 1):
                row = cmd_index * 2 + group_index
                if group_index == 0:
                    grid.addWidget(label, row, 0)
                tag = QLabel("")
                tag.setObjectName("Hint")
                tag.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
                # 用最小宽度而不是固定宽度：宇树的组名「速度 · 位置 · 力矩」比别的长，
                # 定死 84px 会被裁成「速度 · 位置 ·」（截图里实测过）
                tag.setMinimumWidth(self.GROUP_WIDTH)
                grid.addWidget(tag, row, 1)
                tags[(cmd, group_index)] = tag
                for offset in range(6):
                    spin = QSpinBox()
                    spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
                    spin.setFixedWidth(self.SLOT_WIDTH)
                    spin.setRange(0, 1000)
                    spin.setAlignment(Qt.AlignCenter)
                    spin.valueChanged.connect(
                        lambda _value, s=side: self.on_value_changed(s))
                    grid.addWidget(spin, row, 2 + offset)
                    spins.append(spin)
            spins_box[cmd] = spins

        unit = QLabel("")
        unit.setObjectName("Hint")
        unit.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        unit.setContentsMargins(self.CMD_WIDTH + SP["sp2"] + self.GROUP_WIDTH
                                + SP["sp2"], 0, 0, 0)
        self.unit_labels[side] = unit
        grid.setColumnStretch(8, 1)
        body.addLayout(grid)
        # 单位说明放在表格**下面单独一行**（而不是挤在最后一列）：灵巧手那 6 列之后
        # 剩余宽度不足，挤在右边会被裁成"前 6 个 = 手指…"（离屏截图里实测就是这样）
        body.addWidget(unit)

        layout.addWidget(card)
        return {"badge": file_badge, "note": note, "grid": grid, "cmd_labels": cmd_labels,
                "tags": tags, "spins": spins_box, "card": card, "unit": unit}

    def _build_result_card(self) -> QFrame:
        """结果卡：读取 / 保存 / 校验的原文都落在这里。"""
        card, _head_row, body = self._card("操作结果")
        self.result = LogView()
        self.result.setObjectName("Console")
        self.result.setReadOnly(True)
        self.result.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self._console_width = 0
        self._fitting = False
        self._fit_console()
        body.addWidget(self.result)
        return card

    def _fit_console(self):
        """结果区高度**跟着内容走**（3~9 行）—— 实现见 `common.fit_console`。"""
        if self._fitting:
            return
        self._fitting = True
        try:
            self._console_width = max(1, self.result.viewport().width())
            fit_console(self.result, self.CONSOLE_MIN_ROWS, self.CONSOLE_MAX_ROWS)
        finally:
            self._fitting = False

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        self.btn_reload = QPushButton("重新读取")
        self.btn_reload.setProperty("variant", "ghost")
        self.btn_reload.setToolTip("丢弃当前改动，重新从配置文件读一遍")
        self.btn_reload.clicked.connect(self.on_reload)
        row.addWidget(self.btn_reload)

        self.btn_defaults = QPushButton("恢复默认值")
        self.btn_defaults.setProperty("variant", "ghost")
        self.btn_defaults.setToolTip("把输入框填回出厂值（不会写进配置文件，仍要点「保存到配置」）")
        self.btn_defaults.clicked.connect(self.on_restore_defaults)
        row.addWidget(self.btn_defaults)

        row.addStretch()
        self.effect_label = QLabel("改完需重启主程序才生效")
        self.effect_label.setObjectName("Hint")
        row.addWidget(self.effect_label)

        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("variant", "ghost")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)

        self.btn_save = QPushButton("保存到配置")
        self.btn_save.setProperty("variant", "primary")
        self.btn_save.setDefault(True)
        self.btn_save.setToolTip("写入机器人侧配置文件（先备份到上位机 logs/config_backup/，写后回读校验）")
        self.btn_save.clicked.connect(self.on_save)
        row.addWidget(self.btn_save)
        return row

    # ---------- 状态刷新 ----------
    @guard_slot
    def refresh(self):
        """按最近一次读取结果刷新（**不**重新读文件 —— 那是「重新读取」的事）。"""
        self._apply_head()
        self._apply_sides()
        self._apply_buttons()
        self._apply_dirty()

    def _apply_head(self):
        hand = self.state.get("product_hand") or ""
        section = self.state.get("product_section") or ""
        if not self.state:
            self.hand_badge.setText("机型末端 未读取")
            theme.set_state(self.hand_badge, "none")
        elif hand:
            name = END_EFFECTOR_NAMES.get(section, "")
            self.hand_badge.setText(f"机型末端 {hand}" + (f"（{name}）" if name else ""))
            theme.set_state(self.hand_badge, "ok" if name else "none")
        else:
            self.hand_badge.setText("机型末端 未知")
            theme.set_state(self.hand_badge, "none")

        if not self.state:
            self.state_badge.setText("配置未读取")
            theme.set_state(self.state_badge, "none")
        elif self.state.get("available"):
            self.state_badge.setText("配置可读")
            theme.set_state(self.state_badge, "ok")
        else:
            self.state_badge.setText("配置不可读")
            theme.set_state(self.state_badge, "danger")

        depth = self.state.get("depth_control") or {}
        left, right = depth.get("left"), depth.get("right")
        if left is None and right is None:
            text = "深度控制 —（只读）"
        elif left == right:
            text = f"深度控制 {'开' if left else '关'}（只读）"
        else:
            text = f"深度控制 左右不一致（只读：左 {'开' if left else '关'}）"
        self.depth_badge.setText(text)
        theme.set_state(self.depth_badge, "none")

        # 读不出来时：路径那行直接换成**原因**（它可能很长，走自己那一行 + 自动折行；
        # 早先塞在类型行右边，被裁成三行还溢出卡片 —— 截图里实测过）
        if self.state and not self.state.get("available"):
            self.path_label.setText(self.state.get("reason") or "配置文件读不出来")
            theme.set_state(self.path_label, "danger")
        else:
            self.path_label.setText("配置文件：" + (self.state.get("path") or "—"))
            theme.set_state(self.path_label, "none")

    def _apply_sides(self):
        """两张侧卡的徽章、段名提示、单位说明与格子（类型共用，两侧同时变）。"""
        section = self._section()
        types = self.state.get("types") or {}
        for side in ("left", "right"):
            widgets = self.sides[side]
            file_section = types.get(side)
            if file_section:
                name = END_EFFECTOR_NAMES.get(file_section, file_section)
                widgets["badge"].setText(f"文件里：{name}")
                theme.set_state(widgets["badge"],
                                "ok" if file_section in END_EFFECTOR_EDITABLE else "warn")
            else:
                widgets["badge"].setText("文件里：未识别")
                theme.set_state(widgets["badge"], "none")
            widgets["note"].setText(
                f"写 {section}.{side}_open_cmd / {side}_fist_cmd" if section else "")
            if section:
                widgets["unit"].setText(self._unit_text(section))
                theme.set_state(widgets["unit"], "none")
            else:
                # 没得编辑时，让卡片里留一句"为什么"而不是空着（空卡片会被读成坏了）
                widgets["unit"].setText("配置读不出来，无法编辑 —— 先点「重新读取」")
                theme.set_state(widgets["unit"], "danger")
            self._layout_slots(side, section)

        left, right = types.get("left"), types.get("right")
        if not self.state.get("available"):
            # 原因已经在上面那行（路径行）与侧卡里说清了，这里不再重复一遍
            self.type_note.setVisible(False)
        elif left and right and left != right:
            self.type_note.setVisible(True)
            self.type_note.setText(
                f"文件里左右不一致（左 {END_EFFECTOR_NAMES.get(left, left)}、"
                f"右 {END_EFFECTOR_NAMES.get(right, right)}）—— 保存后会统一成这一种")
            theme.set_state(self.type_note, "warn")
        else:
            # 短句常驻 + 完整理由进悬停：这行在 860px 宽的卡片里只剩一百多像素，
            # 写全了会折成两行（实测），折行比短句更难扫
            self.type_note.setVisible(True)
            self.type_note.setText("左右臂共用一种类型")
            self.type_note.setToolTip("末端控制器只按一种类型驱动末端，Product.json 的 hand 也是单值 ——"
                                      "所以两臂的类型一起改、一起写")
            theme.set_state(self.type_note, "none")

    @staticmethod
    def _unit_text(section) -> str:
        if section == "yinshi":
            return "前 6 个 = 手指角度（0~1000），后 6 个 = 力控（0~3000）"
        if section == "yushu":
            return "千分比 0~1000，位置越大越闭合"
        return ""

    def _layout_slots(self, side: str, section):
        """按类型摆格子：灵巧手两行（角度 / 力控），宇树一行三个。

        **必须先设范围、再填值**（`_fill_side` 就是先调它）：反过来的话，桥接旧值会被新范围
        夹一下 —— 灵巧手力控 0~3000 的 3000 落到 0~1000 的输入框里会变成 1000，现场看到的是
        "我填的 3000 自己变了"。
        """
        widgets = self.sides[side]
        ranges = end_effector_slot_ranges(section) if section else ()
        groups = END_EFFECTOR_GROUPS.get(section, ()) if section else ()

        for cmd_index, cmd in enumerate(END_EFFECTOR_CMDS):
            # 没选类型（配置读不出来）时连指令名一起收起来：只留空壳卡片比"什么都不显示"更难看
            widgets["cmd_labels"][cmd].setVisible(bool(section))
            for group_index in (0, 1):
                tag = widgets["tags"][(cmd, group_index)]
                usable = group_index < len(groups)
                tag.setVisible(usable)
                if usable:
                    tag.setText(groups[group_index][0])
                for offset, spin in enumerate(widgets["spins"][cmd][group_index * 6:
                                                                   group_index * 6 + 6]):
                    slot = group_index * 6 + offset
                    show = usable and slot < len(ranges)
                    spin.setVisible(show)
                    if slot < len(ranges):
                        spin.setRange(*ranges[slot])

    def _apply_buttons(self):
        """能不能改、能不能存，以及为什么不能（禁用必须给理由）。"""
        available = bool(self.state.get("available"))
        section = self._section()
        if self._saving:
            reason = "正在保存，等它出结果"
        elif not self.state:
            reason = "还没读取配置文件（点「重新读取」）"
        elif not available:
            reason = self.state.get("reason") or "配置文件不可读"
        elif not section:
            reason = "还没有选末端类型"
        else:
            reason = ""

        file_section = (self.state.get("types") or {}).get("left")
        self.type_combo.setEnabled(bool(available and not self._saving))
        if not self.type_combo.isEnabled():
            self.type_combo.setToolTip(f"不可用：{reason}")
        elif file_section and file_section not in END_EFFECTOR_EDITABLE:
            self.type_combo.setToolTip(
                f"文件里当前是 {END_EFFECTOR_NAMES.get(file_section, file_section)}，"
                f"本期不支持编辑；选一项会把两臂的类型一起改掉")
        else:
            self.type_combo.setToolTip("末端类型（左右臂共用；右上角是 Product.json 的手型对照）")

        for side in ("left", "right"):
            for cmd in END_EFFECTOR_CMDS:
                for spin in self.sides[side]["spins"][cmd]:
                    spin.setEnabled(bool(available and not self._saving and section))
                    spin.setToolTip(
                        f"{END_EFFECTOR_CMD_TEXT[cmd]}的其中一个值"
                        f"（范围 {spin.minimum()}~{spin.maximum()}）"
                        if spin.isEnabled() else f"不可用：{reason or '先选末端类型'}")

        self.btn_reload.setEnabled(not self._saving)
        self.btn_defaults.setEnabled(bool(available and not self._saving))
        self.btn_defaults.setToolTip(
            "把输入框填回出厂值（不会写进配置文件，仍要点「保存到配置」）"
            if self.btn_defaults.isEnabled() else f"不可用：{reason}")
        self.btn_save.setEnabled(bool(available and not self._saving and not reason))
        self.btn_save.setToolTip(
            "写入机器人侧配置文件（先备份到上位机 logs/config_backup/，写后回读校验）"
            if self.btn_save.isEnabled() else f"不可用：{reason}")

    def _apply_dirty(self):
        if self._dirty:
            self.dirty_badge.setText("有未保存修改")
            theme.set_state(self.dirty_badge, "warn")
        else:
            self.dirty_badge.setText("未修改")
            theme.set_state(self.dirty_badge, "none")

    def _section(self):
        """下拉当前选中的段名；没选（-1）返回 None。"""
        index = self.type_combo.currentIndex()
        return self.type_combo.itemData(index) if index >= 0 else None

    # ---------- 读取 / 改动 ----------
    @guard_slot
    def on_reload(self):
        """重新读配置文件并把值填进输入框（丢弃未保存的改动，所以要先说清楚）。"""
        if self._dirty:
            if QMessageBox.question(
                self, "放弃未保存的修改",
                "当前有还没写入配置的修改，重新读取会丢掉它们。是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                self._report("已取消重新读取（改动未丢）")
                return
        self._load("重新读取")

    def _load(self, label: str):
        """读一次并填表（本地文件读，毫秒级，不必开线程）。"""
        self.state = self.launcher.read_end_effector_config()
        self._loading = True
        try:
            for side in ("left", "right"):
                self._file_types[side] = (self.state.get("types") or {}).get(side)
            # 类型共用：以左臂那一种为准（两侧不一致时页面上挂一条 warn，保存即统一）
            file_section = self._file_types["left"]
            chosen = file_section if file_section in END_EFFECTOR_EDITABLE else None
            index = self.type_combo.findData(chosen) if chosen else -1
            self.type_combo.setCurrentIndex(index)
            # `_loading` 为真时 `on_type_changed` 直接返回（那是给用户点击用的），
            # 所以这里的摆格子与填值都要**显式**做一遍，别指望信号带过去
            for side in ("left", "right"):
                self._layout_slots(side, chosen)
                if chosen:
                    self._fill_side(side, chosen)
        finally:
            self._loading = False
        self._dirty = False

        if self.state.get("available"):
            self._report(f"{label}：{self.state['path']}")
            for side in ("left", "right"):
                section = self._file_types[side]
                name = END_EFFECTOR_NAMES.get(section, section or "未识别")
                values = (self.state.get("values") or {}).get(side) or {}
                self._report(f"  {self._side_name(side)}：类型 {name}"
                             f"，张开 {values.get('open')}，握紧 {values.get('fist')}")
        else:
            self._report(f"{label}失败：{self.state.get('reason') or '没有取到结果'}")
        self.refresh()

    def _fill_side(self, side: str, section: str):
        """把**这一种手型**在配置里的值（缺的用出厂值）填进输入框。

        注意取的是 `state["sections"][section][side]`，不是 `state["values"][side]`
        —— 后者是"文件里当前那一种"的值。换成另一种时若用它，就会把灵巧手的
        12 个值塞进宇树的 3 个格子里（前 3 个还恰好是 1000、1000、1000）。
        """
        widgets = self.sides[side]
        self._layout_slots(side, section)          # 先定范围，再填值（见 _layout_slots）
        stored = ((self.state.get("sections") or {}).get(section) or {}).get(side) or {}
        for cmd in END_EFFECTOR_CMDS:
            values = stored.get(cmd)
            if not values:
                # 配置里缺这一组（或不是整数列表）→ 用出厂值占位，**并说出来**：
                # 安静地填 0 会让现场以为"配置里本来就是 0"，保存下去就是错的
                values = END_EFFECTOR_DEFAULTS.get(section, {}).get(cmd, [])
                if values:
                    self._report(f"  注意：{section}.{side}_{cmd}_cmd 在配置里读不出来，"
                                 f"输入框先填出厂值，请核对后再保存")
            self._set_spins(widgets["spins"][cmd], values)

    def _set_spins(self, spins, values):
        """往一组输入框里填值；`values` 用不到的格子保持原样（它们本来就被隐藏）。"""
        for index, spin in enumerate(spins):
            if index < len(values):
                spin.setValue(int(values[index]))

    @guard_slot
    def on_type_changed(self):
        """换末端类型：两侧的格子、组名与范围一起换，值取该类型的出厂值（那是另一套参数）。"""
        if self._loading:
            return
        section = self._section()
        for side in ("left", "right"):
            if section:
                self._fill_side(side, section)
            else:
                self._layout_slots(side, None)
        self._dirty = True
        self.refresh()

    @guard_slot
    def on_value_changed(self, side: str):
        if self._loading:
            return
        self._dirty = True
        self._apply_dirty()

    @guard_slot
    def on_restore_defaults(self):
        """把输入框填回出厂值（**不写文件** —— 写不写由「保存到配置」决定）。"""
        section = self._section()
        if not section:
            self._report("还没有选末端类型，没有可填的默认值")
            return
        for side in ("left", "right"):
            for cmd in END_EFFECTOR_CMDS:
                self._set_spins(self.sides[side]["spins"][cmd],
                                END_EFFECTOR_DEFAULTS.get(section, {}).get(cmd, []))
        self._dirty = True
        self.refresh()
        self._report(f"已填入出厂值：左右臂 {END_EFFECTOR_NAMES.get(section, section)}"
                     f"（还没写入配置，核对后点「保存到配置」）")

    # ---------- 保存 ----------
    @guard_slot
    def on_save(self):
        """写入配置文件：先确认（只在换类型时）→ 后台写 → 回读校验。"""
        if self._saving:
            self._report("上一次保存还在进行中，等它出结果")
            return
        if not self.state.get("available"):
            self._report(f"{self.state.get('reason') or '配置文件不可读'}，没有写入任何内容")
            return
        section = self._section()
        if not section:
            self._report("还没选末端类型，没有写入任何内容")
            return

        # 类型左右共用：两臂写同一个段；各自的指令值仍然分开
        selections = {"left": section, "right": section}
        slots = end_effector_slot_ranges(section)
        values = {side: {cmd: [self.sides[side]["spins"][cmd][index].value()
                               for index in range(len(slots))]
                         for cmd in END_EFFECTOR_CMDS}
                  for side in ("left", "right")}

        changed = [side for side in ("left", "right")
                   if self._file_types.get(side) != section]
        if changed:
            detail = "、".join(
                f"{self._side_name(side)}："
                f"{END_EFFECTOR_NAMES.get(self._file_types.get(side), '未识别')} → "
                f"{END_EFFECTOR_NAMES.get(section, section)}"
                for side in changed)
            if QMessageBox.question(
                self, "确认更换末端类型",
                f"这次会改掉末端类型：{detail}。\n"
                "类型决定 arm_control_node 按哪套协议驱动末端 —— 装机上是什么就得写什么，\n"
                "写错的话末端不动或者报错。是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                self._report("已取消保存（末端类型未改动）")
                return

        self._saving = True
        self.busy_changed.emit(True)
        self._apply_buttons()
        self._report("正在写入配置…")
        thread = LocalTaskThread(
            lambda: self.launcher.save_end_effector_config(selections, values))
        self._save_thread = thread
        thread.finished.connect(self._on_saved)
        thread.start()

    @guard_slot
    def _on_saved(self, ok: bool, payload, error: str):
        self._saving = False
        self.busy_changed.emit(False)
        payload = payload if isinstance(payload, dict) else {}
        for line in (payload.get("lines") or []):
            self._report(line)
        if ok:
            self._report("保存成功。（参数要重启主程序后才生效）")
        else:
            for line in (payload.get("errors") or [f"保存失败：{error or '未知错误'}"]):
                self._report(line)
        # 无论成败都重读一次：成功要显示新值，失败要说清"文件现在到底是什么"
        self._load("保存后重读")
        # 重读会把 dirty 清掉；失败时改动还在页面上，不该说"未修改"
        if not ok:
            self._dirty = True
            self._apply_dirty()

    # ---------- 结果区 ----------
    @staticmethod
    def _side_name(side: str) -> str:
        return "左臂" if side == "left" else "右臂"

    def _report(self, line: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.result.append(f"[{stamp}] {line}")
        self._fit_console()                     # 行数变了，高度跟着变（多出来的在框内滚动）
        bar = self.result.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---------- 生命周期 ----------
    def showEvent(self, event):
        super().showEvent(event)
        if not self.state:
            self._load("首次读取")
        else:
            self.refresh()
        self._fit_console()                     # 首次显示时才知道真实宽度（折行数取决于它）
        if not self._screen_fitted:             # 屏幕装不下就收进屏幕（只做一次）
            self._screen_fitted = True
            fit_dialog_to_screen(self, shrink=self._shrink_for_screen)


    def _shrink_for_screen(self, room: int) -> None:
        """屏幕装不下时收结果区（`fit_dialog_to_screen` 的回调）。

        本页除了结果区之外都是**内容定高**的卡片（每一格多高由字段本身决定），
        没有可压的余地 —— 结果区本来就在 `CONSOLE_MIN_ROWS` ~ `CONSOLE_MAX_ROWS`
        之间按内容自适应，收到下限只是"同时少看几条日志"，不动任何操作。
        """
        if room <= 0:
            return
        fit_console(self.result, self.CONSOLE_MIN_ROWS, self.CONSOLE_MIN_ROWS)

    def resizeEvent(self, event):
        """窗口变宽/变窄后重新算一次高度：宽度变了，折行数就变了。"""
        super().resizeEvent(event)
        if self.result.viewport().width() != self._console_width:
            self._fit_console()

    def closeEvent(self, event):
        # 没有周期定时器；保存线程结束时会自己放开 busy，所以中途关页面不会卡住闸门。
        super().closeEvent(event)
