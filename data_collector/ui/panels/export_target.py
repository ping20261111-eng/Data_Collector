"""导出目标（数据往哪儿送）：芯片 + 展开面板 + 挂盘探测。

从 `actions.py` 拆出来（v6.4.5）。原因很直接：这一段有 **700 行**，
把 `actions.py` 顶过了"界面文件不超过 1000 行"的上限守卫。
**拆出来而不是调高上限** —— 那条守卫的存在就是为了拦住这种膨胀，
放宽它等于把尺子改短来显得自己没长高。

为什么做成 `Mixin` 而不是独立控件类：这一段与 `ActionsMixin` 共享同一批
界面对象（`self.append` 日志、`self.config` 配置、`self.ed_*` 控件），
拆成独立类就要把十几个引用逐个透传，收益不抵改动量。
Mixin 只是把"同一批代码"换个文件放，`MainWindow` 那边一行都不用改。
"""

from html import escape as html_escape

from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
                             QLineEdit, QPushButton, QComboBox, QToolButton,
                             QMessageBox, QSizePolicy,
                             QGraphicsDropShadowEffect)
from PyQt5.QtCore import Qt, QPoint, QTimer
from PyQt5.QtGui import QColor, QGuiApplication

from ... import theme
from ...core import logger
from ..common import SP
from ..widgets import RadioDot, StatusGlyph


class TargetCard(QWidget):
    """导出目标面板里的一张卡片：整张可点，hover 时联动指示器。

    ## 为什么要一个控件类，而不是「裸 QWidget + 全局事件过滤器」

    v6.4.4 那版是"给卡片和它的每个子控件都装一个事件过滤器"，
    靠 `objectName` 逐级往上回溯找归属 —— 能跑，但有两个代价：
      ① 子控件一增删就得同步改安装点，往卡片里加个图标就漏装一个，
         表现为"点那一小块没反应"；
      ② **hover 没法联动指示器**：QSS 的 `:hover` 管不到子控件，
         而 Qt 的 QSS 不支持"父 hover 影响子"这种选择器。

    改成一个类之后，`enterEvent` / `leaveEvent` 天然覆盖"鼠标落在某个子控件上"
    的情形（Qt 的 Enter/Leave 语义是**含子控件**的），一个类把两件事都解决了。
    """

    def __init__(self, value: str, dot, on_click, parent=None):
        super().__init__(parent)
        self.setObjectName("TargetOption")
        self.setProperty("target", value)
        self.setCursor(Qt.PointingHandCursor)
        # QSS 的 `:hover` 要有这个属性才会被跟踪 —— 没有它那条规则不生效。
        self.setAttribute(Qt.WA_Hover, True)
        self._value = value
        self._dot = dot
        self._on_click = on_click

    def enterEvent(self, event):          # noqa: N802 (Qt 命名)
        self._dot.set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):          # noqa: N802 (Qt 命名)
        self._dot.set_hover(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):     # noqa: N802 (Qt 命名)
        # 显式接受左键按下：Qt 由此建立**到本卡片的隐式 grab**，之后的 release
        # 一定回到这里 —— 而不是指望"子控件 ignore、事件往上冒泡"那条路。
        # （卡片里的 QLabel 默认 ignore，冒泡确实能到，但显式接受更稳，
        #   也让 press / release 一定落在同一个控件上。）
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):   # noqa: N802 (Qt 命名)
        # 在卡片内抬起才算"点了这张"：按下后拖到外面松手不算（拖拽取消的通用语义）。
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self._on_click(self._value)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ExportTargetMixin:
    # ---------- 导出目标（数据往哪儿送） ----------
    # 逻辑层在 v6.3.6 就接好了（`DataUploader` 按 `export_target` 分流），
    # 这里只解决"让现场知道能选、并且选完记得住"。
    #
    # 放在「数据」行尾而不是新起一行 / 弹窗：那行右侧本来就整片空白，
    # 而导出目标是**偶发改一次**的设置 —— 弹窗会打断手头的事，
    # 新起一行则要为它长期占一行的位置。

    #: 芯片左右 padding 之和（与 theme.py 里 `QToolButton#ExportChip` 的
    #: `padding: 5px 10px 5px 11px` 对应）。改那条 QSS 时这里要一起改——
    #: 不然内容会被 padding 挤掉一截（实测「移动硬盘」被截成「移动硬:」）。
    CHIP_PADDING = 21

    #: 目标选项：值 →(芯片/选项上的短名, 面板里的标题, 一句"这对你意味着什么")
    #: 三段文案而不是一行标题，是因为现场非研发人员不看配置文件、只认界面，
    #: 而「两者都送」这类词不解释就不知道是先硬盘还是先服务器。
    EXPORT_TARGET_ITEMS = (
        ("server", "服务器", "送到：服务器", "走 SFTP 送到远端"),
        ("disk", "移动硬盘", "送到：移动硬盘", "写本地挂载点，完全不连服务器"),
        ("both", "两者都送", "送到：服务器 + 移动硬盘", "先写硬盘，再送服务器"),
    )

    def _build_export_target_selector(self) -> QWidget:
        """导出目标芯片（v6.4.4 取代原来那个裸 `QComboBox`）。

        **为什么不是下拉**：下拉适合「从若干同类值里选一个」，而这里要选的是
        目标、要填的是路径、还要显示一个随时间变化的状态 —— 三件事塞进一个控件，
        就必然要外挂一行（原来那个挂载点行就是这么来的），而外挂的一行会把
        「数据」这一组从中间劈开，读起来像两件事。

        芯片只做两件事：显示当前目标 + 显示盘的状态。配置放到面板里，
        点它才展开。`cb_export_target` 仍然保留（藏在面板里），
        既有守卫断言的是它的 `currentData()`，不用改。
        """
        self.chip_export_target = QToolButton()
        self.chip_export_target.setObjectName("ExportChip")
        self.chip_export_target.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.chip_export_target.setPopupMode(QToolButton.InstantPopup)
        self.chip_export_target.setCursor(Qt.PointingHandCursor)
        # 不用 `setMenu(InstantPopup)`：`QMenu` 给 `QWidgetAction` 的高度**不够**
        # （实测 holder 只有 276px，内容需要 426px），内部布局被压扁 ——
        # 后面的控件 geometry 停在 (0,0,640,480)（= 从未被排布），
        # 表现为文字互相重叠的花屏。改成自己管的 `Qt.Popup` 窗口（见 `_build_export_panel`）。
        self.chip_export_target.setPopupMode(QToolButton.DelayedPopup)
        self.chip_export_target.setToolTip(
            "这批数据送到哪儿（写入 app_config.yaml，重启后仍然有效）：\n"
            "· 服务器：走 SFTP 送到远端（缺省，与以前一致）\n"
            "· 移动硬盘：写本地挂载点，完全不连服务器（离线现场用）\n"
            "· 两者都送：先写硬盘、再送服务器\n"
            "\n点这个按钮展开设置。")
        self._build_export_panel()
        self.chip_export_target.clicked.connect(self._toggle_export_panel)

        # 芯片内部：状态图标 + 目标名 + 可用空间，三段不同样式的文字并排。
        # **为什么不用 `setText`**：QToolButton 的 text 只能是一整串，
        # 单一字体、单一颜色 —— 想让「移动硬盘」是正文色而「128GB 可用」是次要色，
        # 就只能自己排。容器铺满按钮本身，QSS 的 padding 仍然作用在按钮上。
        self.chip_body = QWidget(self.chip_export_target)
        self.chip_body.setObjectName("ChipBody")
        chip_row = QHBoxLayout(self.chip_body)
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(6)
        self.chip_glyph = StatusGlyph("none", self.chip_body)
        chip_row.addWidget(self.chip_glyph)
        self.lb_chip_title = QLabel("服务器")
        self.lb_chip_title.setObjectName("ChipTitle")
        chip_row.addWidget(self.lb_chip_title)
        self.lb_chip_caption = QLabel("")
        self.lb_chip_caption.setObjectName("ChipCaption")
        chip_row.addWidget(self.lb_chip_caption)
        chip_row.addStretch()
        # 宽度按内容算，见 `_refit_chip` 的注释（三个坑都在那儿）。
        self._refit_chip()
        outer = QHBoxLayout(self.chip_export_target)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.chip_body)
        self._set_chip_title("服务器")
        self._refit_chip()
        return self.chip_export_target

    def _build_export_panel(self) -> QFrame:
        """芯片展开的面板（v6.4.4）。

        ## 为什么是 `Qt.Popup` 窗口，而不是 `QMenu` 或 `QDialog`

        - **不是 `QDialog`**：模态会锁住主窗口，而这里只是选个设置，不该打断
          手头的事 —— 现场常常是「设置好导出目标 → 立刻开始录制」。
        - **也不是 `QMenu`**（这是先用过、又换掉的方案）。`QMenu` 给
          `QWidgetAction` 的尺寸**不够**：实测容器只拿到 276px 高，而内容需要
          426px，于是内部布局被压扁，后几个控件的 geometry 停在
          `(0,0,640,480)`（Qt 的初始默认值 = **从未被排布**）——
          现场看到的就是选项文字互相重叠的"花屏"。
          `Qt.Popup` 的尺寸完全由 `adjustSize()` 按内容算，不受这种启发式影响。

        `Qt.Popup` 自带「点窗口外关闭」与「不抢焦点」，正是要的行为。

        `WA_TranslucentBackground` 是**真圆角的前提**（见 theme.py 弹层那节）：
        Qt 的 QSS 只把 `border-radius` 画成边框线、不裁底色矩形，
        四角会被底色填满 —— 除非窗口本身透明。

        面板里的控件（`cb_export_target` / `ed_disk_mount` / `lb_disk_state` /
        `btn_disk_mark` / `btn_disk_recheck`）**都是原有的那些对象**，
        只是换了爹妈（挂到面板而不是主卡片）。这是刻意的：既有守卫断言的正是
        这些对象（`win.cb_export_target` / `win.ed_disk_mount.text()`），
        复用它们意味着那批用例一行都不用改。
        """
        panel = QFrame(self, Qt.Popup | Qt.FramelessWindowHint)
        panel.setObjectName("ExportPanelShell")
        # 顺序要紧：透明属性必须在**第一次显示之前**设好，
        # 否则窗口已经按不透明建过一次，再设也不生效（四角仍是方块）。
        panel.setAttribute(Qt.WA_TranslucentBackground, True)
        panel.setAutoFillBackground(False)
        self.export_panel = panel

        # 外壳只负责给投影留出绘制空间：**顶层窗口画不出自己 rect 之外**，
        # 不留边距的话阴影会被直接裁掉（看起来就是"没有阴影"，还查不出原因）。
        shell = QVBoxLayout(panel)
        shell.setContentsMargins(*([self.PANEL_SHADOW_MARGIN] * 4))

        card = QFrame()
        card.setObjectName("ExportPanel")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(self.PANEL_SHADOW_BLUR)
        shadow.setOffset(0, self.PANEL_SHADOW_DY)
        shadow.setColor(QColor(*self.PANEL_SHADOW_RGBA))
        card.setGraphicsEffect(shadow)
        shell.addWidget(card)
        self.export_panel_card = card

        panel.setMinimumWidth(self.PANEL_MIN_WIDTH + 2 * self.PANEL_SHADOW_MARGIN)
        body = QVBoxLayout(card)
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(SP["sp2"])

        title = QLabel("数据送到哪儿")
        title.setObjectName("PanelTitle")
        body.addWidget(title)

        # --- 三个目标选项（卡片式，每项带一句含义） ---
        self.target_option_widgets = {}
        for value, _short, label, hint in self.EXPORT_TARGET_ITEMS:
            # 指示器自绘（RadioDot）：QSS 的 `::indicator` 只有"边框 + 底色"
            # 两个通道，而底色会填满整个方块 —— 表达不出"外环 + 中心点"。
            # 上一版用 4px 边框去凑那个点，画出来是方块套方块（现场最刺眼那处）。
            dot = RadioDot()
            opt = TargetCard(value, dot, self.on_target_option_clicked)
            # 内边距：水平 13、垂直 11（QHBoxLayout 的四个参数是 左/上/右/下，
            # 别按"水平×垂直"去写 —— 写成 (11,13,13,11) 就成了四边都不对称）。
            row = QHBoxLayout(opt)
            row.setContentsMargins(13, 11, 13, 11)
            row.setSpacing(11)
            row.addWidget(dot, 0, Qt.AlignTop)
            texts = QVBoxLayout()
            texts.setSpacing(1)
            t1 = QLabel(label)
            t1.setObjectName("TargetOptionTitle")
            t2 = QLabel(hint)
            t2.setObjectName("TargetOptionHint")
            t2.setWordWrap(True)
            texts.addWidget(t1)
            texts.addWidget(t2)
            row.addLayout(texts, 1)
            # 点击由 `TargetCard` 自己处理（mousePress/Release），
            # 不再给卡片和它的每个子控件逐个装事件过滤器 —— 那套补丁在
            # 子控件增删时会漏装，而且管不了 hover 与指示器的联动。
            self.target_option_widgets[value] = (opt, dot)
            body.addWidget(opt)

        # --- 分隔：下面那块只在选了硬盘时才有意义 ---
        self.panel_divider = QFrame()
        self.panel_divider.setObjectName("Divider")
        self.panel_divider.setFrameShape(QFrame.HLine)
        # **必须给固定高**：`QFrame` 当 HLine 用时 `sizeHint()` 的宽度是 `-1`
        # （水平的线没有"固有宽度"），纵向布局拿到 -1 就算不出该分多少，
        # 实测它的 geometry 会停在 Qt 的初始默认值上（= 从未被排布）。
        # 分隔线就 1px，给死比让布局去猜稳。
        self.panel_divider.setFixedHeight(1)
        self.panel_divider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        body.addWidget(self.panel_divider)

        # --- 挂载点配置：原有的控件原样搬进来 ---
        self.panel_disk_title = QLabel("挂载点")
        self.panel_disk_title.setObjectName("PanelTitle")
        body.addWidget(self.panel_disk_title)
        self.disk_mount_row = self._build_disk_mount_row(in_panel=True)
        body.addWidget(self.disk_mount_row)

        # --- 落点预览（v6.4.3 三层落点的直接收益） ---
        self.lb_landing_preview = QLabel("")
        self.lb_landing_preview.setObjectName("LandingPreview")
        self.lb_landing_preview.setWordWrap(True)
        self.lb_landing_preview.setVisible(False)
        body.addWidget(self.lb_landing_preview)

        # `cb_export_target` 是**数据载体**，不是界面控件：面板上的选择由三张
        # 卡片承担，下拉只保存"当前值"。既有守卫断言的正是 `win.cb_export_target`，
        # 所以它必须还在（只是不给用户看）。挂成面板的子控件并隐藏即可 ——
        # 不再需要 `QWidgetAction` 那层间接（那是 `QMenu` 才有的限制）。
        self.cb_export_target = QComboBox(panel)
        self.cb_export_target.setObjectName("ExportTarget")
        for value, _short, label, _hint in self.EXPORT_TARGET_ITEMS:
            self.cb_export_target.addItem(label, value)
        self.cb_export_target.currentIndexChanged.connect(self.on_export_target_changed)
        self.cb_export_target.setVisible(False)
        return panel

    def _toggle_export_panel(self) -> None:
        """点芯片：展开 / 收起面板。

        定位规则：贴在芯片**下方**、与芯片**右对齐**（芯片本身就在行尾，
        左对齐会让面板伸到窗口外）。
        还要做屏幕边界兜底：面板比芯片宽得多，靠近屏幕右边时要往回拉。
        """
        panel = self.export_panel
        if panel.isVisible():
            panel.hide()
            return
        panel.adjustSize()                      # 先按内容定尺寸，再算位置
        chip = self.chip_export_target
        top_left = chip.mapToGlobal(QPoint(0, chip.height() + 6))
        # 对齐的是**卡片**的右边缘，不是外壳的：外壳比卡片多一圈投影留白
        # （PANEL_SHADOW_MARGIN），直接按 panel.width() 对齐会整体左偏那一圈。
        top_left.setX(top_left.x() + chip.width() - panel.width()
                      + self.PANEL_SHADOW_MARGIN)
        # 用 `QGuiApplication.screenAt()`，不再走 `QDesktopWidget` 那条老路：
        # 它在 Qt 5.15 已废弃。`screenAt` + `primaryScreen` 兜底是现代写法，
        # 语义与原来一致（芯片落在哪个屏幕上，就按那个屏幕的可用区域兜底）。
        target_screen = QGuiApplication.screenAt(chip.mapToGlobal(QPoint(0, 0)))
        if target_screen is None:
            target_screen = QGuiApplication.primaryScreen()
        screen = target_screen.availableGeometry()
        if top_left.x() < screen.left():
            top_left.setX(screen.left() + 8)
        if top_left.x() + panel.width() > screen.right():
            top_left.setX(screen.right() - panel.width() - 8)
        if top_left.y() + panel.height() > screen.bottom():
            # 下方放不下就翻到芯片上方 —— 总比被屏幕裁掉一截好
            top_left.setY(chip.mapToGlobal(QPoint(0, 0)).y() - panel.height() - 6)
        panel.move(top_left)
        # **不要在这里直接 `show()`，推到事件循环下一轮**（v6.4.6 修 bug）。
        # 这个 slot 是 `QToolButton.clicked` 的接收者，而 `clicked` 是在
        # **鼠标release 的处理过程中**发出的 —— 此刻这次点击还没走完。
        # 在"点击尚未结束"时显示一个 `Qt.Popup` 窗口，Qt 会让它进入
        # "等按键全部释放再抓鼠标"的过渡态，现场表现就是
        # **面板弹出来了，但第一下点选项没反应**（用户会以为是"要先点一下别的"）。
        # 推到下一轮事件循环，这次点击已经彻底结束，popup 从第一次点击起就是正常的。
        QTimer.singleShot(0, self._show_export_panel)

    def _show_export_panel(self) -> None:
        """真正把面板显示出来（由 `_toggle_export_panel` 延后一轮调用）。"""
        panel = self.export_panel
        panel.show()
        panel.raise_()

    def _set_chip_title(self, short: str) -> None:
        """芯片中间那段文字（目标名）。"""
        self.lb_chip_title.setText(short)

    def _build_disk_mount_row(self, in_panel: bool = False) -> QWidget:
        """挂载点配置块（v6.4.4 起住在芯片面板里）。

        布局从「横排一行」改成「竖排两行」：面板宽度有限，横排要把路径输入、
        状态文案、「创建标识」、「重新检测」四个东西塞进 400px，
        实测会挤成两行且按钮被裁。竖排让每个控件都能拿到整宽。
        """
        self.disk_mount_row = QWidget()
        # `DiskConfig` = 面板里的一块配置（见 theme.py）：透明背景，
        # 与面板同一张底 —— 嵌套灰底（灰底里的灰底）在浅色系里会显得脏。
        self.disk_mount_row.setObjectName("DiskConfig")
        outer = QVBoxLayout(self.disk_mount_row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SP["sp2"])

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP["sp2"])
        outer.addLayout(row)

        # 行内不再放「挂载点」标签：面板上方已经有同名小标题，
        # 两个"挂载点"叠着出现是纯冗余（实拍看出来的）。
        self.lb_disk_mount = QLabel("")
        self.lb_disk_mount.setVisible(False)

        self.ed_disk_mount = QLineEdit()
        self.ed_disk_mount.setObjectName("DiskMount")
        self.ed_disk_mount.setPlaceholderText("/media/robot/NERV")
        # 面板宽度固定（设计稿400px），所以让输入框吃掉整行富余即可 ——
        # 主卡片里那个「固定 300px」的教训是"别让 Expanding 抢走状态文案的
        # 位置"，而这里状态文案已经换到下一行，不存在那个问题。
        self.ed_disk_mount.setMinimumWidth(260)
        self.ed_disk_mount.setToolTip("移动硬盘挂载到哪个目录（填设备目录本身，不是它下面的子目录）")
        # 失焦才存：边打边存会在路径打到一半时写入一个不存在的路径
        self.ed_disk_mount.editingFinished.connect(self.on_disk_mount_edited)
        row.addWidget(self.ed_disk_mount)

        row.addStretch()

        # 第二行：状态 + 两个按钮（横排，整块宽）
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(SP["sp2"])
        self.lb_disk_state = QLabel("")
        self.lb_disk_state.setObjectName("FactValue")
        self.lb_disk_state.setWordWrap(True)
        state_row.addWidget(self.lb_disk_state, 1)

        # 只在「盘已挂载但缺标识文件」时出现（见 _probe_disk_mount）。
        # 紧跟在状态文案后面：文案说清缺什么，按钮就是解法。
        self.btn_disk_mark = QPushButton("创建标识")
        self.btn_disk_mark.setProperty("variant", "ghost-compact")
        self.btn_disk_mark.setToolTip(
            "在移动硬盘根目录创建一个空的标识文件，确认这块盘就是数据盘。\n"
            "用于防止把数据写进插错的盘；该文件不含数据，可随时删除。")
        self.btn_disk_mark.clicked.connect(self.on_create_volume_marker)
        self.btn_disk_mark.setVisible(False)
        state_row.addWidget(self.btn_disk_mark, 0)

        self.btn_disk_recheck = QPushButton("重新检测")
        self.btn_disk_recheck.setProperty("variant", "ghost-compact")
        self.btn_disk_recheck.setToolTip("盘是后来才插上的时候点它")
        self.btn_disk_recheck.clicked.connect(lambda: self._probe_disk_mount())
        state_row.addWidget(self.btn_disk_recheck, 0)

        outer.addLayout(state_row)
        self.disk_mount_row.setVisible(False)
        return self.disk_mount_row

    #: 面板最小宽度（px）。**给它是为了让"就绪"那条状态说明不折行**。
    #:
    #: 宽度用 `QFontMetrics` 实测，不靠估：就绪态那句
    #: 「✔ 盘已就绪 · exFAT · 可用 29.0GB」单行要 **271px**，
    #: 加「重新检测」按钮 66 + 间距 8 + 左右边距 28 = **373**。
    #: 只按三张卡片的宽度算出来是 290 —— 差这 80 多像素，
    #: 「可用 29.0GB」就会折到第二行（实拍里看出来的）。
    #:
    #: 注意 `QLabel` 开了 `wordWrap` 之后 `sizeHint()` 给的是**折行后的推荐宽**，
    #: 不是真实单行宽（实测它报 135，而真实是 271）——
    #: 所以这里必须用 `QFontMetrics.horizontalAdvance()` 量。
    #:
    #: 异常态的文案更长（「✖ 该路径不是已挂载的设备目录（…）」单行要 444px），
    #: 那种情况**保持折行**——为了它把面板撑到 550px 宽并不划算。
    PANEL_MIN_WIDTH = 380

    #: 面板投影：外层留白 / 模糊半径 / 向下偏移 / 颜色（rgba）。
    #: 留白是必需的 —— 顶层窗口画不出自己 rect 之外，不留就被裁掉。
    PANEL_SHADOW_MARGIN = 14
    PANEL_SHADOW_BLUR = 24
    PANEL_SHADOW_DY = 6
    #: 32/255 ≈ 12% 黑。够看出"浮起来"，又不抢内容。
    PANEL_SHADOW_RGBA = (29, 43, 40, 32)

    def _sync_export_target_widgets(self, probe: bool = False) -> None:
        """按当前配置回填两个控件。**不发信号**（回填不是用户改的）。"""
        try:
            settings = self.config.get_export_settings()
        except Exception as exc:                      # 配置读不了不该让界面起不来
            logger.warning(f"读取导出设置失败：{exc}")
            settings = {"target": "server", "mount_point": ""}
        target = settings.get("target", "server")

        self.cb_export_target.blockSignals(True)
        index = self.cb_export_target.findData(target)
        self.cb_export_target.setCurrentIndex(index if index >= 0 else 0)
        self.cb_export_target.blockSignals(False)

        self.ed_disk_mount.setText(settings.get("mount_point", "") or "")
        self._set_disk_row_visible(target in ("disk", "both"))
        if probe and target in ("disk", "both"):
            self._probe_disk_mount()

        # **回填之后必须补一次卡片的选中态同步**（v6.4.6 修 bug）。
        # 回填走的是 `blockSignals(True)`，所以 `on_export_target_changed`
        # → `_sync_option_visuals()` 从头到尾一次都不会跑 —— 三张卡片的
        # `selected` 属性一直停在"从未设置"（实测值就是 `None`）。
        # 后果不是"点不动"，而是**面板一打开，三张卡长得一模一样、
        # 没有一张显示为当前值**，而芯片上明明写着目标名 —— 两边自相矛盾。
        # 现场的反应就是"挨个点一遍找当前是哪个"，而第一次点的那个
        # 会把配置改掉（配置本来是 server，一点就变 disk）。
        self._sync_option_visuals()

    def _set_disk_row_visible(self, visible: bool) -> None:
        """挂载点配置块的显隐（只在面板内部用）。

        v6.4.4 起它住在芯片面板里，所以"显隐"影响的是面板内容的高度，
        不再是主卡片的行数 —— 面板会自动收窄到只剩目标选择。
        """
        self.disk_mount_row.setVisible(bool(visible))
        for w in (self.panel_disk_title, self.panel_divider):
            w.setVisible(bool(visible))

    def _refresh_chip(self) -> None:
        """把芯片刷成当前状态（目标名 + 图标 + 可用空间）。

        **默认态就要显示状态**是这一版的核心：盘没插好时不用点开面板就能看见。
        原来的实现只在展开那一刻探一次，默认态与异常态长得一模一样 ——
        而「以为导成功了、其实没上盘」正是从这个缝里钻出来的。
        """
        target = self.cb_export_target.currentData()
        short = next((s for v, s, _l, _h in self.EXPORT_TARGET_ITEMS if v == target),
                     "服务器")
        self._set_chip_title(short)

        if target not in ("disk", "both"):
            # 服务器：没有盘的概念，灰点即可。别在这里显示"未插好" ——
            # 那是"你选了移动硬盘但盘没插好"的提醒，跟这个目标无关。
            self.chip_glyph.set_kind("none")
            self.lb_chip_caption.setText("")
            theme.set_state(self.chip_export_target, "none")
            self._refit_chip()
            return

        ok = bool(getattr(self, "_disk_ok", False))
        cap = str(getattr(self, "_disk_caption", "") or "")
        if ok:
            self.chip_glyph.set_kind("ok")
            theme.set_state(self.chip_export_target, "ok")
        else:
            self.chip_glyph.set_kind("warn")
            theme.set_state(self.chip_export_target, "warn")
            cap = cap or "未插好"
        self.lb_chip_caption.setText(cap)
        # 文案变了宽度就得跟着变（「服务器」→「移动硬盘」要长 8px）
        self._refit_chip()

    def _refit_chip(self) -> None:
        """按当前三段内容重算芯片宽度。

        三个坑叠在一起，每一个都得单独处理：

        1. **要��按钮本身设宽度**，不是只给容器设。实测 `chip_body` 已经 132，
           而 `QToolButton` 仍然只有 42 —— 它的 `sizeHint` 只算自己的图标与
           text（这里两个都是空），**完全不采纳子控件的尺寸**。
        2. **容器还要再减去 padding**。QSS 的 padding 不算进 `sizeHint`，
           不减就会把内容挤掉（实测「移动硬盘」被截成「移动硬:」）。
        3. **宽度随文案变**。「服务器」和「635GB 可用」长度不同，固定宽度要么
           空一块、要么截断。所以每次内容变了都重算，而不是拍一个常数。

        量法：图标 + 标题 + 次要文案 + 两段间距（各6px）+ 左右 padding。
        「635GB 可用」是最长的情况（实测 69px），三位数容量也够用。
        """
        need = (self.chip_glyph.width()
                + self.lb_chip_title.sizeHint().width()
                + self.lb_chip_caption.sizeHint().width()
                + 6 * 2)
        width = need + self.CHIP_PADDING
        self.chip_export_target.setFixedWidth(width)
        self.chip_body.setFixedWidth(width - self.CHIP_PADDING)

    def on_target_option_clicked(self, value: str) -> None:
        """面板里点了某张目标卡片。

        走 `cb_export_target.setCurrentIndex` 而不是直接写配置 ——
        让它复用 `on_export_target_changed` 那条既有路径（写回配置 + 探盘），
        不在这里重复一遍"选完之后该做什么"。
        """
        index = self.cb_export_target.findData(value)
        if index >= 0:
            self.cb_export_target.setCurrentIndex(index)
        self._sync_option_visuals()
        # 面板选择完就收起：现场是「设置好 → 立刻开始录」，面板留着挡视线。
        self.export_panel.hide()

    def _sync_option_visuals(self) -> None:
        """把三张卡片的选中态与可点性刷成当前值。"""
        current = self.cb_export_target.currentData()
        for value, (opt, dot) in self.target_option_widgets.items():
            selected = value == current
            dot.set_checked(selected)
            opt.setProperty("selected", "true" if selected else "false")
            theme.repolish(opt)
            # 整张卡片可点；选中那张换成箭头光标（点了也不会变，
            # 「点了没反应」比「光标不是手型」更让人困惑）
            opt.setCursor(Qt.PointingHandCursor if not selected else Qt.ArrowCursor)

    def on_export_target_changed(self) -> None:
        """切换导出目标：立刻写回配置，并（选了硬盘时）探一次盘。

        为什么切换就写：这个设置决定"数据送到哪儿"，只放在内存里的话，
        现场下次开程序会退回服务器 —— 而那时没人会记得自己改过。
        """
        target = self.cb_export_target.currentData()
        uses_disk = target in ("disk", "both")
        self._set_disk_row_visible(uses_disk)
        if uses_disk:
            self._probe_disk_mount()
        self._refresh_chip()
        self._sync_option_visuals()

        mount = self.ed_disk_mount.text().strip() if uses_disk else None
        self._save_export_settings(target, mount,
                                   label=self.cb_export_target.currentText())

    def on_disk_mount_edited(self) -> None:
        """挂载点改完（失焦）才存 —— 边打边存会写入打到一半的路径。"""
        target = self.cb_export_target.currentData()
        if target not in ("disk", "both"):
            return
        self._probe_disk_mount()
        self._save_export_settings(target, self.ed_disk_mount.text().strip(),
                                   label="挂载点")

    def _save_export_settings(self, target: str, mount_point, label: str) -> None:
        """写回 `app_config.yaml`，把结果如实报到日志区。

        写失败**必须说出来**：静默失败会让人以为设置生效了，
        而下次上传还按旧目标走 —— 那是最难查的一类问题。
        """
        try:
            result = self.config.save_export_settings(target, mount_point)
        except Exception as exc:
            logger.error(f"保存导出设置异常：{exc}")
            self.append(f"✖ 导出设置没保存成功：{exc}")
            return
        for line in result["lines"]:
            self.append(line)
        if result["ok"]:
            self.append(f"✔ {label} 已保存")
        else:
            for err in result["errors"]:
                self.append(f"✖ 导出设置没保存成功：{err}")

    def _probe_disk_mount(self, quiet: bool = False) -> None:
        """探一次挂载点，把结果写成一句人话。

        `quiet=True` 供定时探测调用：**更新状态但不弹窗**。
        用户没点任何东西的时候弹对话框，只会被当成程序抽风。

        **为什么值得单独探**：`disk_export` 的检查只在"点上传那一刻"跑，
        而现场最贵的事故是"以为导成功了、其实没上盘"—— 路径写错时若那个目录
        恰好存在（本机磁盘上的同名目录），数据就落在本机了，界面上却是成功。
        把结果提前显示出来，工程师在**录之前**就知道盘有没有认出来。
        """
        from ...disk_export import DiskExportTarget      # 延迟导入，避免加载期耦合

        mount = self.ed_disk_mount.text().strip()
        if not mount:
            self.lb_disk_state.setText("请填写移动硬盘挂载点")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_recheck.setVisible(False)
            # 上一轮探测若显示过"创建标识"，这里必须一并收掉：
            # 按钮是"针对某个具体挂载点"的出路，路径都空了它就没有指代对象。
            self.btn_disk_mark.setVisible(False)
            return

        try:
            settings = self.config.get_export_settings()
            probe = DiskExportTarget(
                mount_point=mount,
                volume_id_file=settings.get("volume_id_file", ".nerv_volume"),
                require_volume_id=bool(settings.get("require_volume_id")),
            )
            path_exists = probe.mount_point.exists()
            mounted = probe.is_mounted() if path_exists else False
            has_marker = probe.has_volume_marker() if mounted else False
            fs = probe.filesystem_type() if mounted else ""
            free = probe.free_space() if mounted else 0
        except Exception as exc:
            logger.warning(f"探测挂载点失败：{exc}")
            self.lb_disk_state.setText(f"探测失败：{exc}")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_recheck.setVisible(True)
            # 探测失败时不能提供"创建标识"——那等于在不知道盘状态时往盘上写东西
            self.btn_disk_mark.setVisible(False)
            # 早退前必须刷芯片：否则上一次探测的"就绪"会留在芯片上，
            # 而面板已经红字了 —— 界面自相矛盾，比不显示更糟。
            self._disk_ok, self._disk_caption = False, ""
            self._refresh_chip()
            self._refresh_landing_preview()
            return

        if not path_exists:
            self.lb_disk_state.setText("✖ 该路径不存在。插上硬盘后点「重新检测」")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_mark.setVisible(False)
            self._disk_ok, self._disk_caption = False, ""
        elif not mounted:
            self.lb_disk_state.setText("✖ 该路径不是已挂载的设备目录（数据会写在本机磁盘上）")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_mark.setVisible(False)
            self._disk_ok, self._disk_caption = False, ""
        elif probe.require_volume_id and not has_marker:
            self.lb_disk_state.setText(
                f"✖ 该盘不是数据盘（缺少标识文件 {probe.volume_marker_path()}）")
            theme.set_state(self.lb_disk_state, "warn")
            # 现场第一次用时盘上必然没有这个文件。原来只有一句"请手动创建"，
            # 而现场既不知道文件名对不对、也不确定写完要怎么确认，所以做成一个
            # 按钮：点一下就建，然后立刻重测，状态当场变绿。
            self.btn_disk_mark.setVisible(True)
            self._disk_ok, self._disk_caption = False, ""
        else:
            parts = ["✔ 盘已就绪"]
            if fs:
                parts.append(fs)
            if free:
                parts.append(f"可用 {free / 1024 ** 3:.1f}GB")
            self.lb_disk_state.setText(" · ".join(parts))
            theme.set_state(self.lb_disk_state, "ok")
            self.btn_disk_mark.setVisible(False)
            self._disk_ok = True
            # 芯片上**常显**可用空间：29GB 的盘录满这件事，
            # 现场必须不点开面板就知道（省掉它等于把"盘快满了"藏起来）。
            self._disk_caption = f"{free / 1024 ** 3:.0f}GB 可用" if free else "已就绪"

        self.btn_disk_recheck.setVisible(True)
        # 状态与落点都从**探测这一处**统一出去。分头在别处刷必然漏掉某一态
        # —— 漏「路径不存在」那条时，芯片会显示"就绪"而面板是红字，界面自相矛盾。
        self._refresh_chip()
        self._refresh_landing_preview()

    def _refresh_landing_preview(self) -> None:
        """面板底部的「将保存到」预览。

        这是 v6.4.3 三层落点（序列号 / 日期 / bag 名）的直接收益：导出前就
        把盘上的最终位置摆出来。现场不用导出完再去盘上翻目录找文件。

        只在选了硬盘且路径已填时显示 —— 没有挂载点时预览是空的，
        给一块空白框不如不给。
        """
        uses_disk = self.cb_export_target.currentData() in ("disk", "both")
        mount = self.ed_disk_mount.text().strip() if uses_disk else ""
        if not uses_disk or not mount:
            self.lb_landing_preview.setVisible(False)
            return
        try:
            # 复用逻辑层那套分层规则（`_disk_subpath`），不在这儿另写一遍
            # —— 两处各算一次的话，改了一处另一处就会说错话，
            # 而界面说错落点的后果正是"人按界面去找、找不到"。
            #
            # 落点里那个 bag 名**故意写成占位**：真实的 bag 名要到导出那一刻
            # 才有，而那一刻之前界面不该替它假定一个名字。
            from ...core_data import DataUploader
            # 不传 folder → 按"今天"算（本次采集还没发生，不该拿上次的时间顶替）
            span = DataUploader(self.config)._disk_subpath()
        except Exception as exc:
            logger.warning(f"生成落点预览失败：{exc}")
            self.lb_landing_preview.setVisible(False)
            return
        # 分层着色：**本次会新建的那两层（序列号 / 日期）用强调色**，
        # 挂载点与占位名用次要色 —— 现场一眼就看得出这次会落到哪一层。
        colors = theme.palette(theme.active_mode())
        parts = str(span).split("/")
        serial = parts[0] if parts else str(span)
        date = parts[1] if len(parts) > 1 else ""
        self.lb_landing_preview.setText(
            f'<span style="color:{colors["muted"]}">将保存到</span><br>'
            f'<span style="color:{colors["muted2"]}">{html_escape(mount)}/</span>'
            f'<span style="color:{colors["accent"]}">{html_escape(serial)}</span>'
            f'<span style="color:{colors["muted2"]}">/</span>'
            f'<span style="color:{colors["accent"]}">{html_escape(date)}</span>'
            f'<span style="color:{colors["muted2"]}">/&lt;本次采集名&gt;/</span>')
        self.lb_landing_preview.setVisible(True)

    def _start_disk_probe_timer(self) -> None:
        """低频探测：让芯片上的状态**跟着盘的实际状态变**。

        ## 为什么需要定时器

        芯片在**默认态**就显示状态（这是这一版的核心），而现场是"盘插上/
        拔掉"这种物理动作 —— 不可能指望用户点一下才知道。原来的实现只在
        展开面板那一刻探一次，芯片上的状态会一直是第一次探出来的结果。

        ## 为什么只在变化时记日志

        每3s 记一条的话，日志区会被「盘已就绪」刷满，**真正的故障
        （掉线、拔盘、写失败）会被淹没** —— 而那些才是要回头查的。
        所以 `_disk_ok` / `_disk_caption` 变了才记。
        """
        if getattr(self, "_disk_timer", None) is not None:
            return
        self._last_disk_ok = None
        self._last_disk_caption = None
        timer = QTimer(self)
        timer.setInterval(3000)
        timer.timeout.connect(self._tick_disk_probe)
        timer.start()
        self._disk_timer = timer

    def _tick_disk_probe(self) -> None:
        """定时探一次，只在状态真的变了才说话。"""
        if self.cb_export_target.currentData() not in ("disk", "both"):
            return
        if not self.ed_disk_mount.text().strip():
            return
        # 探测本身会把面板里的状态文案与按钮刷好（它就是那一处的真相来源），
        # 但探测期间**不该弹窗** —— 用户没点任何东西。
        before = (getattr(self, "_disk_ok", None), getattr(self, "_disk_caption", None))
        try:
            self._probe_disk_mount(quiet=True)
        except Exception as exc:
            logger.debug(f"定时探测挂载点失败：{exc}")
            return
        after = (getattr(self, "_disk_ok", None), getattr(self, "_disk_caption", None))
        if after == before:
            return
        if after[0]:
            self.append(f"移动硬盘已就绪：{after[1]}")
        else:
            self.append(f"⚠ 移动硬盘状态变化：{self.lb_disk_state.text()}")

    def on_create_volume_marker(self) -> None:
        """在移动硬盘根目录创建盘标识文件。

        ## 为什么这道检查要保留，而不是直接放行

        `volume_id_file`（默认 `.nerv_volume`）防的是**把数据写进错盘**：
        现场桌上常有若干 U 盘 / 移动盘，工程师选错一个，这批数据就散在
        一台没人认领的机器上，而当时不会有任何人察觉。缺标识就放行，
        等于把这个唯一的确认环节关掉—— 代价远大于多点一次鼠标。

        ## 为什么要再确认一次

        这一次写入是**唯一一处程序主动往盘上写东西**的地方（其余都是写数据），
        而它写的是盘根目录 —— 一个被用户当成"盘里没有我的东西"的地方。
        所以确认框里必须出现完整路径：让用户看清"到底要动哪个盘的哪"，
        而不是只问一句"确定吗"。

        **写完立刻重测**（而不是提示"已创建，请点重新检测"）：让用户看到
        状态从红变绿，闭环在自己眼前完成，不需要他判断"到底成功没有"。
        """
        from ...disk_export import DiskExportTarget      # 延迟导入，避免加载期耦合

        mount = self.ed_disk_mount.text().strip()
        settings = self.config.get_export_settings()
        probe = DiskExportTarget(
            mount_point=mount,
            volume_id_file=settings.get("volume_id_file", ".nerv_volume"),
            require_volume_id=bool(settings.get("require_volume_id")),
        )
        marker = probe.volume_marker_path()

        # 前置校验放在**写入之前**逐条判，而不是让写操作去撞文件系统：
        # 撞出来的错是 errno，现场看不懂，而这里每一条都能说清"为什么不行"。
        if not mount or not probe.mount_point.exists():
            QMessageBox.warning(self, "无法创建", f"路径不存在：{mount or '(空)'}")
            return
        if not probe.is_mounted():
            QMessageBox.warning(
                self, "无法创建",
                f"{mount} 不是已挂载的设备目录。\n"
                f"在普通目录里建标识文件没有意义——那说明硬盘根本没挂上，"
                f"数据会写进本机磁盘。")
            return
        if marker and marker.exists():
            self._probe_disk_mount()
            return

        answer = QMessageBox.question(
            self, "确认创建盘标识",
            f"将在移动硬盘根目录创建一个空文件：\n\n    {marker}\n\n"
            f"用于确认「这块盘就是数据盘」，防止把数据写进插错的盘。\n"
            f"该文件不含任何数据，可以随时删除。\n\n确认创建？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        try:
            marker.touch(exist_ok=True)
        except OSError as exc:
            # 不静默：这正是"盘是只读 / 已满 / 挂载掉线"三类问题的汇合点，
            # 原文带上 errno 现场才能判断是换盘还是修权限。
            QMessageBox.critical(
                self, "创建失败",
                f"无法创建标识文件：\n{marker}\n\n{exc}\n\n"
                f"常见原因：盘已写满、挂载为只读、挂载点掉线。")
            logger.warning(f"创建盘标识失败：{marker}（{exc}）")
            return

        self.append(f"已创建盘标识文件：{marker}")
        self._probe_disk_mount()
