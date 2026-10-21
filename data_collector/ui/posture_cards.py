"""「姿势控制」页的**四张卡**与底部按钮行的构造（页面的"铺版面"那一半）。

拆出来的原因：`posture_widgets.py` 到 **1002 行**，顶过项目「单文件 1000 行」的红线
（`test_posture_modules_stay_under_the_line_limit`）—— 边界在哪本来就很清楚：
那个文件装的是**常驻组装件**（一行轴、预设卡、模块条），都是"被摆上去的东西"；
这里装的是**怎么摆**（四张卡的骨架、卡头里放什么、按钮行有哪几个）。

依赖是单向的：这一册 import 组装件，组装件不 import 这一册。
`posture.py` 把两册的名字都再导出一遍，所以 `posture.PostureCardsMixin` 这类取用照旧有效。
"""
from PyQt5.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import theme
from ..core_motion import ARM_NAMES
from .common import SP, badge, dialog_card
from .posture_widgets import (GROUP_TITLES, HAND_TOOLTIP, UNKNOWN, AxisRow, ModuleEnableRow,
                              _ElidedLabel, scaled, specs_for)
from .widgets import LogView


class PostureCardsMixin:
    """`PostureDialog` 的四张卡 + 底部按钮行的构造方法。"""

    #: 三列走轴表与预设列表的固定高度（这一页要压进现场那块屏；超出的部分滚动）
    LIST_HEIGHT = 214
    #: 预设区的高度与列数（v6.3.0 改上下排列后重排）：126 = 两行卡片（2×56 + 8 缝）+ 余量。
    #: 躯干 3 列 × 2 行正好放下 6 条；手臂 2 列，默认 2 条占一行，勾"显示全部"后向下滚。
    #: 预设区定高（两行卡）：卡高 60 × 2 + 行距 4 = 124（v6.3.2，卡高 56 → 60）
    PRESET_LIST_HEIGHT = 124
    PRESET_COLUMNS = {"trunk": 3, "arm": 2}
    #: 撑高行的行号（比任何真实行都大）：保证卡片靠上对齐
    PRESET_MAX_ROWS = 99
    #: 一列走轴表的最小宽度（轴名 26 + 当前 62 + → 12 + 目标 74 + 间距与内距 ≈ 190）。
    #: v6.3.0 从 212 收到 190 —— 紧凑版页宽 1040 下每列实得 319px，这个下限只兜底。
    #: ⚠️ v6.5.3 起「当前 / 目标」两格的宽度**不再写死**（改成按字体度量算，见
    #: `posture_widgets.angle_column_width`）：字号随 `theme.ui_scale()` 放大后，
    #: 写死的 62 / 74 装不下 `-42.942` 这种字面量，Qt 会**从左边**裁掉文本、负号消失
    #: （现场报的「躯干角度显示不全」）。两格变宽后，一列的真实最小宽由内部控件的
    #: `setFixedWidth` 决定（Qt 取内容宽），所以**改那两格不需要同步改这里的数字**。
    AXIS_COLUMN_MIN = 190
    #: 预设卡那一列的最小宽度（名字 + 摘要 + 「改」+ 内距）
    #: 预设卡那一列的最小宽度。**这里必须留出滚动条那 ~10px**：躯干有 6 张卡，
    #: 列表一定会出竖向滚动条，viewport 会比 area 窄 —— 只按卡片本身算的话，
    #: 卡片右边缘的「改」会被裁掉（实测：area 158 / viewport 148，而卡片最小 186）。
    #: v6.3.0：`_ElidedLabel.minimumSizeHint` 修好之后卡片最小宽只剩 118（原来 186），
    #: 所以这里跟着从 200 收到 186（= 卡片最小宽 + 滚动条那 10px + 余量）。
    PRESET_COLUMN_MIN = 186

    def _build_head_card(self) -> QFrame:
        card, head_row, body = dialog_card("姿势控制", head_pad=SP["sp1"], body_pad=SP["sp2"])
        # 顶栏只放**整机级**的三项（v6.3.0）：监控在不在、本页按哪种模式下发、急停。
        # 「躯干 已使能 / 双臂 已使能」两个徽章删掉了 —— 它们与下面模块条里的状态徽章
        # 完全重复（同一个状态，46px 内并列两遍），而模块条紧贴着上/下使能按钮，
        # 那里才是"这个状态决定我能不能点"的地方。
        self.monitor_badge = badge("监控 —")
        self.mode_badge = badge("非实时模式 —")
        self.estop_badge = badge("急停 未知")
        for widget in (self.monitor_badge, self.mode_badge, self.estop_badge):
            head_row.addWidget(widget)

        # 躯干与双臂**并排一行**（现场要求）：两者本来就是同一件事的两个对象，
        # 并排才能一眼比较；中间一条 1px 竖线分隔，别让两组读起来连成一句。
        self.module_rows = {}
        self.module_frame = QFrame()
        self.module_frame.setObjectName("ModuleStrip")
        strip = QHBoxLayout(self.module_frame)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(0)
        for index, (module, name) in enumerate((("trunk", "躯干"), ("arms", "双臂"))):
            if index:
                divider = QFrame()
                divider.setObjectName("Divider")
                divider.setFixedWidth(1)
                divider.setFixedHeight(22)
                divider.setContentsMargins(0, 0, 0, 0)
                strip.addWidget(divider)
            row = ModuleEnableRow(module, name, self)
            row.action_requested.connect(self.on_module_action)
            # 自锁只有躯干那一行有按钮（`btn_lock` 在别的模块上是 None）——
            # 挂信号本身是幂等的，不必判断模块。
            row.lock_requested.connect(self.on_trunk_lock_action)
            strip.addWidget(row, 1)
            self.module_rows[module] = row
        body.addWidget(self.module_frame)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("Hint")
        self.hint_label.setWordWrap(True)
        # 话题名 / 判据这些"排障要看、平时不用记"的东西一律进悬停（v6.3.0）：
        # 常驻行只留给操作员当场要用的话。
        self.hint_label.setToolTip(
            "角度来自机器人侧的 state 话题：躯干 trunk_state、手臂 arm_state；\n"
            "手没有回读话题，所以只显示本次会话最后下发的那组值。\n"
            "本页按非实时（点位）模式下发：躯干一条点位目标、手臂一条 ArmMove，\n"
            "都由控制节点内部插值；下发后靠回读关节角确认到位（躯干 1°、手臂 2°）。")
        body.addWidget(self.hint_label)

        # 「躯干模式需要确认」时**才出现**的一条（v6.2.0）：遥操作的上电时序会把躯干切成
        # 实时模式，而实时模式下关节按最大速度追目标、要上位机自己插值 —— 本页只发单条
        # 点位目标，那就会"动作又快又硬"。切回来需要 connect（**会抬头**），是一次动机构的
        # 动作，所以**不自动做**、也不常驻占版面：只在"确实可能不对"时露出来。
        self.mode_row = QFrame()
        self.mode_row.setObjectName("ModuleStrip")
        mode_layout = QHBoxLayout(self.mode_row)
        mode_layout.setContentsMargins(SP["sp2"], SP["sp1"], SP["sp2"], SP["sp1"])
        mode_layout.setSpacing(SP["sp2"])
        self.mode_notice = QLabel("")
        self.mode_notice.setObjectName("Hint")
        self.mode_notice.setWordWrap(True)
        mode_layout.addWidget(self.mode_notice, 1)
        # 按钮不加括号解释了（v6.3.0）："那一下会抬头" 在同一行的正文里已经写明，
        # 按钮里再塞一遍只会把它变长、把提示行挤窄；完整说明留在悬停提示里。
        self.btn_ensure_mode = QPushButton("切回非实时")
        self.btn_ensure_mode.setProperty("variant", "ghost-compact")
        self.btn_ensure_mode.setToolTip("向躯干下发：切非实时模式（motor_mode 0）→ 重新 connect。\n"
                                        "会二次确认 —— 那一下躯干会抬头回默认姿态。")
        self.btn_ensure_mode.clicked.connect(
            lambda _checked=False: self.on_ensure_trunk_mode())
        mode_layout.addWidget(self.btn_ensure_mode)
        self.mode_row.setVisible(False)
        body.addWidget(self.mode_row)

        card.setToolTip("预设来源见「姿势预设」卡片；这一页走非实时（点位）模式"
                        "—— 躯干一条点位目标、手臂一条 ArmMove，都由控制节点内部插值。"
                        "下发走常驻命令通道（等订阅者、必要时重发、并回读关节角确认到位）；"
                        "通道起不来时自动退回一次性下发，并在日志里标明「未经确认」")
        return card

    def _build_axis_card(self) -> QFrame:
        """三列：躯干（下 → 上）、左臂、右臂（从肩起）。每行 = 轴名 │ 当前 │ 目标。

        卡头 = **状态**（目标差 / 手型最后下发），卡底 = **动作**（三个整组下发）。
        v6.3.0 把"手（最后下发）"从按钮行挪到卡头：它是只读读数，和三个动作按钮挤在
        同一行时，"哪个能点"只能靠颜色去猜；而且卡头那个「手 无回读」徽章已经在说
        "手"这件事了，两处各说一半。挪过来后同一个信息只出现一次，按钮行也只剩动作。
        读数宽了就**截断**（`_ElidedLabel`），完整值在悬停提示里。
        """
        card, head_row, body = dialog_card("角度与各轴调节", head_pad=SP["sp1"], body_pad=SP["sp2"])
        # 「读数 x.xs 前」这个徽章删掉了：它和顶栏的「监控 运行中」报的是同一个秒数，
        # 现在合并成顶栏那一个（`_apply_head`）。
        self.adjust_badge = badge("目标 —")
        head_row.addWidget(self.adjust_badge)
        self.hand_badge = badge("手 无回读")
        theme.set_state(self.hand_badge, "warn")
        self.hand_badge.setToolTip(HAND_TOOLTIP)
        head_row.addWidget(self.hand_badge)

        self.hand_labels = {}
        for side in ("left", "right"):
            label = _ElidedLabel(f"{ARM_NAMES[side]} {UNKNOWN}")
            label.setObjectName("FactValue")
            label.setToolTip(HAND_TOOLTIP)
            label.setMinimumWidth(64)
            # 不给伸缩：卡头只有一个 stretch（在标题后面），读数跟着徽章走就行 ——
            # 给 stretch 会把「左臂 …」和「右臂 …」拉到卡头两头，看着像两个不相干的字段。
            # 窄了就自己截断（`_ElidedLabel`），前面那个 stretch 会先让位。
            head_row.addWidget(label)
            self.hand_labels[side] = label

        self.btn_take_all = QPushButton("全部取当前值")
        self.btn_take_all.setProperty("variant", "ghost-compact")
        self.btn_take_all.setToolTip("把读到的真实角度全部填进目标（读不到的轴不动；只是回填输入，不运动）")
        self.btn_take_all.clicked.connect(self.on_take_all)
        head_row.addWidget(self.btn_take_all)

        self.btn_undo = QPushButton("恢复上次下发值")
        self.btn_undo.setProperty("variant", "ghost-compact")
        # 措辞是安全的一部分：**运动不可撤销**（机构已经动过了），这个名字必须让人明白
        # 恢复的是"输入框里的目标值"，不是让机构退回去。旧名「撤销上一步」会让现场点下去
        # 以为能退，结果机构一动不动（v6.3.0 改名）。
        self.btn_undo.setToolTip("把各轴的目标值恢复成上一次真正下发过的那组；"
                                 "只回填输入框，不产生任何运动（机构不会退回原位）")
        self.btn_undo.clicked.connect(self.on_undo)
        head_row.addWidget(self.btn_undo)

        columns = QHBoxLayout()
        columns.setSpacing(SP["sp3"])
        self.axis_areas = {}
        for group in ("trunk", "left", "right"):
            block = QVBoxLayout()
            block.setSpacing(SP["sp1"])
            title = QLabel(GROUP_TITLES[group])
            title.setObjectName("SectionTitle")
            block.addWidget(title)
            holder = QWidget()
            rows_layout = QVBoxLayout(holder)
            rows_layout.setContentsMargins(0, 0, 0, 0)
            rows_layout.setSpacing(1)
            for spec in specs_for(group):
                row = AxisRow(spec, self)
                row.changed.connect(self.on_row_changed)
                row.select_requested.connect(self.on_select_row)
                row.take_requested.connect(self.on_take_row)
                row.move_requested.connect(self.on_move_axis)
                row.invalid.connect(self.on_row_invalid)
                rows_layout.addWidget(row)
                self.rows.append(row)
                self.row_of[spec[0]] = row
            rows_layout.addStretch()
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidget(holder)
            area.setFixedHeight(scaled(self.LIST_HEIGHT))
            # QScrollArea 的最小宽度是"不设"，不显式给的话三列会被压到 160px 左右，
            # 目标数字被裁成"-42."（实测）。列宽 = 轴名 + 当前 + → + 目标 + 间距。
            area.setMinimumWidth(self.AXIS_COLUMN_MIN)
            block.addWidget(area)
            self.axis_areas[group] = area
            columns.addLayout(block, 1)
        #: 轴表列表**当前**的高度（最后手段压缩时按它算压多少，见 `set_axis_list_height`）
        self._axis_height_now = scaled(self.LIST_HEIGHT)
        body.addLayout(columns)

        groups = QHBoxLayout()
        groups.setSpacing(SP["sp2"])
        self.group_buttons = {}
        for group in ("trunk", "left", "right"):
            text = {"trunk": "躯干 6 轴下发", "left": "左臂 7 轴下发",
                    "right": "右臂 7 轴下发"}[group]
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, g=group: self.on_group_move(g))
            # **给足按钮的文字宽度**：QPushButton 的最小宽度默认小到可以压成一条，
            # 现场截图里"躯干 6 轴一起走"曾被压成"干 6 轴一起走"。按钮是操作入口，
            # 宁可挤掉别的也不能让人认不出按钮。
            button.setMinimumWidth(button.sizeHint().width())
            groups.addWidget(button)
            self.group_buttons[group] = button
        groups.addStretch()
        body.addLayout(groups)
        return card

    def _build_preset_card(self) -> QFrame:
        card, head_row, body = dialog_card("姿势预设", head_pad=SP["sp1"], body_pad=SP["sp2"])
        self.preset_badge = badge("预设 —")
        head_row.addWidget(self.preset_badge)
        self.chk_show_all = QCheckBox("显示全部手臂预设")
        # 标签写全作用域：这个勾选框**只对手臂列有效**（躯干那 6 条一直全列）。
        # 旧名「显示全部」会让人勾完发现躯干没变，以为坏了（v6.3.0 改名）。
        self.chk_show_all.setToolTip("手臂预设默认只列 A / LB（现场用的那两个），"
                                     "其余收在这里；躯干的 6 个一直全列")
        self.chk_show_all.toggled.connect(
            lambda checked: self.on_show_all("arm", checked))
        head_row.addWidget(self.chk_show_all)
        self.btn_save_preset = QPushButton("存为预设")
        self.btn_save_preset.setProperty("variant", "ghost-compact")
        self.btn_save_preset.setToolTip("把各轴目标值存成一条自定义预设（写上位机自己的 "
                                        "configs/motion_presets.yaml；机器人侧 JSON 不动）")
        self.btn_save_preset.clicked.connect(
            lambda _checked=False: self.on_save_preset("arm"))
        head_row.addWidget(self.btn_save_preset)

        # 预设区 = **两组并排、每组内部是横向栅格**（v6.3.0，随"角度/预设改上下"一起调）。
        # 两张卡上下排列之后预设卡拿满 1148px，如果还沿用"两组各一条竖直长列表"，
        # 卡片会变成 541px 宽、右端孤零零一个「编辑」，而列表只显示 3 张就要滚 —— 又宽又费高。
        # 改成栅格后：躯干 6 张 = 3 列 × 2 行、手臂默认 2 张 = 2 列 × 1 行，
        # 卡宽 247 / 185（都在"名字 + 摘要不截断"的舒适区），整块高度 214 → 126。
        holder = QHBoxLayout()
        holder.setSpacing(SP["sp3"])
        self.preset_areas = {}
        # 两组按 **3:2** 分宽（原来是 2:1）：窄页上手臂组只有 1/3 时，两张卡各 ~158px，
        # 摘要 `1 步 · 双臂 · 含手型` 会被截；3:2 之后躯干 188 / 手臂 190，两边都不截。
        for kind, title, stretch in (("trunk", "躯干（6 个）", 3),
                                     ("arm", "手臂（默认只列 A / LB）", 2)):
            block = QVBoxLayout()
            block.setSpacing(SP["sp1"])
            label = QLabel(title)
            label.setObjectName("SectionTitle")
            label.setToolTip("躯干：install/product/config/trunk_motion.json\n"
                             "手臂：install/product/config/arm_motion.json（只读）")
            block.addWidget(label)
            inner = QWidget()
            inner_layout = QGridLayout(inner)
            inner_layout.setContentsMargins(0, 0, 0, 0)
            inner_layout.setHorizontalSpacing(SP["sp2"])
            inner_layout.setVerticalSpacing(SP["sp2"])
            # 末尾留一个"撑高行"：某一组卡片不够铺满时，卡片靠上对齐而不是各自居中
            inner_layout.setRowStretch(self.PRESET_MAX_ROWS, 1)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidget(inner)
            area.setFixedHeight(scaled(self.PRESET_LIST_HEIGHT))
            area.setMinimumWidth(self.PRESET_COLUMN_MIN)
            block.addWidget(area)
            self.preset_areas[kind] = (area, inner_layout)
            holder.addLayout(block, stretch)
        body.addLayout(holder)

        #: 预设区**当前**高度（屏幕装不下时会被 `set_preset_list_height` 改小）
        self._preset_height_now = scaled(self.PRESET_LIST_HEIGHT)

        self.preset_note = QLabel("")
        self.preset_note.setObjectName("Hint")
        self.preset_note.setWordWrap(True)
        body.addWidget(self.preset_note)
        return card

    #: 轴表列表的**保底可见行数**（v6.4.16，最后手段压缩时的下限）。
    #:
    #: 7 行是"20 个轴一屏看全"（v5.6.0 的承诺）；压到 5 行仍能一眼看到大半，
    #: 再低就该改形态了（内容可滚动 + 底栏固定）。**只在结果区与预设区都压完
    #: 还不够时才会走到这里** —— 见 `PostureDialog._shrink_for_screen`。
    AXIS_MIN_ROWS = 5

    #: 轴表保底线的缓存（None = 还没算过；见 `axis_list_floor`）
    _axis_floor = None

    def axis_row_height(self) -> int:
        """一行轴的实际高度（**拿真实控件量**，别硬编码：它随字号与缩放走）。"""
        rows = [r for r in self.axis_areas["trunk"].findChildren(QFrame)
                if type(r).__name__ == "AxisRow"]
        return rows[0].height() if rows else 0

    def axis_list_floor(self) -> int:
        """轴表列表能压到的**最低**高度：`AXIS_MIN_ROWS` 行 + 表头那一行。

        表头高度不单独量，从"基准高度 = 表头 + 最多那列的轴数 × 行高"反推 ——
        各列轴数不同（躯干 6 / 臂 7），取最多的那列才是列表真正被撑到的高度。

        ⚠️ **算一次就缓存**：压过之后行高本身会变（列表矮了，行被重新布局），
        再算一遍会得出一个**比刚才用的下限还大**的数 —— `set_axis_list_height` 按
        旧下限夹住了高度，断言却按新下限去比，就成了"明明是它自己夹的值，却说不达标"。
        缓存的取值点必须落在"列表还是满高"的那一刻，所以第一次算通常就是对的。
        """
        if self._axis_floor is None:
            base = scaled(self.LIST_HEIGHT)
            row = self.axis_row_height()
            if row <= 0:
                self._axis_floor = base
            else:
                counts = [len([r for r in area.findChildren(QFrame)
                               if type(r).__name__ == "AxisRow"])
                          for area in self.axis_areas.values()]
                rows_max = max(counts) if counts else 7
                header = max(0, base - rows_max * row)
                self._axis_floor = min(base, header + self.AXIS_MIN_ROWS * row)
        return self._axis_floor

    def set_axis_list_height(self, height: int) -> None:
        """改三列轴表的高度 —— **最后手段**（结果区、预设区都压完还不够时才用）。

        ⚠️ 压它等于动"20 个轴一屏看全"的承诺，所以有两道闸：
        ① 下限是 `axis_list_floor()`（保底 `AXIS_MIN_ROWS` 行仍可见然后才滚动）；
        ② 调用点只有 `_shrink_for_screen` 的第三步。
        """
        height = max(self.axis_list_floor(), int(height))
        if height >= self._axis_height_now:
            return
        self._axis_height_now = height
        for area in self.axis_areas.values():
            area.setFixedHeight(height)

    def set_preset_list_height(self, height: int) -> None:
        """改预设区的高度（屏幕装不下时用）。

        预设区**本来就有滚动条**，矮了只是"一次看到的条数少"，6 个躯干预设仍然都够得到
        —— 所以它是页面上除了结果区之外唯一敢压的地方（轴表不能压，见
        `PostureDialog._shrink_for_screen`）。
        """
        height = max(1, int(height))
        if height == self._preset_height_now:
            return
        self._preset_height_now = height
        for area, _grid in self.preset_areas.values():
            area.setFixedHeight(height)

    @property
    def preset_height(self) -> int:
        """预设区现在多高（`_shrink_for_screen` 用它算"还差多少"）。"""
        return int(self._preset_height_now)

    def _build_result_card(self) -> QFrame:
        card, _head_row, body = dialog_card("操作结果", head_pad=SP["sp1"], body_pad=SP["sp2"])
        self.result = LogView()
        self.result.setObjectName("Console")
        self.result.setReadOnly(True)
        self.result.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self._fit_console()
        body.addWidget(self.result)
        return card

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        self.btn_reload = QPushButton("重新读取数据")
        self.btn_reload.setProperty("variant", "ghost")
        self.btn_reload.setToolTip("重读 robot_status.json 与两份预设文件"
                                   "（含上位机自定义预设 —— 刚存的那条也会出现在卡片里）")
        self.btn_reload.clicked.connect(self.on_reload)
        row.addWidget(self.btn_reload)

        self.btn_abort = QPushButton("中止动作")
        self.btn_abort.setProperty("variant", "danger")
        self.btn_abort.setToolTip("停在当前位置（已下发的那一段不会回退）")
        self.btn_abort.clicked.connect(self.on_abort)
        self.btn_abort.setVisible(False)
        row.addWidget(self.btn_abort)

        row.addStretch()
        note = QLabel("急停按钮仍在主界面（本页只显示急停状态）")
        note.setObjectName("Hint")
        row.addWidget(note)

        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("variant", "ghost")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        return row
