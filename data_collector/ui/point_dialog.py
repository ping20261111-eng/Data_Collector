"""打点面板：录制期间常驻，点一下标签就记下此刻。

## 交互契约（v4.11 起，别改回去）

录制一开始**自动弹出非模态面板**、整场不关；面板里**点一下标签就以那一刻记一个点**。
操作区的「打点 (N)」是"收起之后再打开"的入口。时间是 `record_now()` 里第一件事取的 ——
"先锁时间"没变，只是"锁"的动作落在点标签上，反而少了一次开窗的延迟，时刻更准。

面板必须非模态：录制中操作员随时要按主窗口的「停止录制」或急停柱，被模态框挡住就是事故。

## 版面重做（v4.14）：先分主次，再谈美不美

v4.13 之前的版式是「一个列表框 + 一大排控件」，现场用下来暴露两个真问题：

1. **主次反了**。这个面板 90% 的用途是"点一下已有标签"，那是高频动作；而视觉上最显眼的
   是那颗实心强调色的「打点」按钮 —— 它是低频的（手输新标签 / 先记个待命名的点）。
   操作员的视线被拉到低频动作上，高频动作反而像一块只读内容（就是个普通列表行）。
2. **框太多、字太多**。面板里再套两层 GroupBox 就是"框中框"；同一件事（已打几条、写到哪）
   在顶部一行和底部一行各说一遍；编号用空格拼在文本里，读起来是一条字符串而不是一列数。

所以这一版把版面按**使用频率**重排，并让形状本身表达"可以按"：

- **标签做成大卡片网格**（`_LabelCardDelegate` 自绘）：序号是独立徽章、名字是正文，
  整张卡片就是热区（56px 高，现场戴薄手套也点得准）。两列自适应，标签多了也不会变成一条
  要滚动的长列表 —— 现场要的是"一眼看全"。
- **「打点」按钮留在原位并保持最大**（现场明确要过：它要显眼、按一下就直接出一个点），
  但和旁边的输入框**等高**：同一行里两颗控件高矮不一会显得"不是一套"。
- **去掉嵌套框线**，改用分区小标题 + 细分割线分三段：状态 / 打点 / 核对。少一层框，
  就少一次"这是什么容器"的辨认成本。
- **状态行只留一条**：点数做成徽章（`已写入 4 条` / `待写入 2 条`），后面跟记录位置。
  底部那行只负责"最近一次操作的回执"，不再重复说写入位置。
- **已打点列表**改等宽单行读数（序号 / 时刻 / 相对秒 / 标签 / 备注），改删按钮挪到**
  分区标题行右侧 —— 少占一整行，也让"这块能改能删"和标题在一起。

## 标签是一条流程，不是两段

通用组装的是"开始 / 结束"这类夹在流程两端的标签。面板里**不再把通用单独列一段**，
而是合并成一条顺序：通用首项 → 本任务的标签 → 通用末项（见 `core.merge_point_flow`），
卡片上的序号就是位置 1、2、3…。原先拆两段会出现"通用 1、2 + 任务 1、2、3、4"两套编号，
现场看到会以为编号重复了。

写进 `points.csv` 的「标签编号」用的也是这个流程序号 —— **面板显示什么编号、记录里就是什么
编号**，现场按编号对齐动作时两边必须一致。

## 落点

记录写进本次录制的 `points.csv`（在 bag 目录里，跟着数据一起上传远端）。录制目录还没被
定位到时，记录先攒在内存里、下次记录时补写，顶部徽章会把这件事说出来 —— 不静默丢。
"""
import math
import time
from typing import Optional

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QListView,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QStyle, QStyledItemDelegate,
                             QVBoxLayout)

from .. import theme
from ..core import logger
from ..core_points import (PointRecord, is_placeholder_label,
                           next_placeholder_label)
from .common import SP
from .point_labels_dialog import PointLabelDialog
from .widgets import HintLineEdit
from .safety import guard_slot

#: 同标签连点去重窗口（秒）。
#: Qt 会把一次双击拆成 `itemClicked` ×2 + `itemActivated`：不去重的话**一次双击
#: 会记下三个点**。按"同一个标签 + 时间窗"判定 —— 人手连点同一个标签不会快到 0.6 秒。
DOUBLE_CLICK_GUARD = 0.6

#: 打点行里"输入框 / 打点按钮"的统一高度。
#: 写死在代码里而不是靠 QSS 的 padding 累加：字号或字体一换，padding 的算法就会漂，
#: 而"同一行两颗控件等高"是这里唯一的意图，用固定值表达最直白。
ROW_H = 48
#: 字段名（"新标签" / "备注"）的列宽：两行文字左边缘对齐要靠它。
FIELD_LABEL_W = 46

#: 标签卡片：高 56 保证戴薄手套也点得准；最小宽 176 决定一列还是两列。
CARD_H = 56
CARD_MIN_W = 176
#: 标签区最多长到 3 行，再多就滚动 —— 面板高度不能被标签数量牵着走。
LABEL_MAX_ROWS = 3
#: 网格高度里刻意多给的余量（px）。高度正好等于内容高度时，Qt 会在"要不要显示滚动条"
#: 之间摇摆，而滚动条一出现就挤窄视图、可能把网格从两列挤成一列（见 `_relayout_grid`）。
LABEL_HEIGHT_SLACK = 4

#: 已打点区：空的时候按 2 行算（不留一大块空地），最多 5 行。
RECORD_MIN_ROWS = 2
RECORD_MAX_ROWS = 5
#: 列表还是空的时候 `sizeHintForRow` 没有值，用它兜底量高度。
RECORD_ROW_FALLBACK = 28


# ---------- 标签查询（面板与"改标签"对话框共用） ----------
def _point_flow(config, task: str):
    """面板里的标签顺序：已合并成一条流程（通用首项 → 本任务 → 通用末项）。"""
    return config.get_point_label_flow(task)


def _is_in_flow(config, task: str, name: str) -> bool:
    return any(item["name"] == name for item in _point_flow(config, task))


def _point_label_number(config, task: str, name: str) -> Optional[int]:
    """标签在流程里的序号（1 起）—— 面板显示的就是它，写进 points.csv 的也是它。

    必须是同一个数：现场按编号对齐动作时，"面板显示 6、记录里写 2"就是错的。
    """
    return config.get_point_label_number(task, name)


def _ensure_point_label(config, task: str, name: str) -> bool:
    """把手输的新标签沉淀进**当前任务那一组**（通用组是显式维护的，不塞手输内容）。

    Returns:
        是否真的新增了一条（已存在则返回 False；占位名一律不新增）。
    """
    if is_placeholder_label(name):
        # 占位名（点N（待命名））不进标签库：它是"还没想好"，进了库只会把标签列表越堆越乱，
        # 而且这个名字只在这一场录制里有意义。事后在「改选中…」里补真名时会正常入库。
        return False
    if _is_in_flow(config, task, name):
        return False
    ok, _message = config.point_labels.upsert(name, task=task)
    return bool(ok)


# ---------- 标签卡片 ----------
class _LabelCardDelegate(QStyledItemDelegate):
    """把标签画成一张可按的大卡片：序号徽章 + 名字。

    为什么自绘而不是用 QSS 给列表行上色：

    1. **要两段式布局**。序号对现场是有意义的（它要和 `points.csv` 的「标签编号」对齐），
       所以得是一个独立看得见的徽章，而不是拼在字符串里的第一个字符；QSS 只能给整行
       一个样式，画不出"徽章 + 正文"。
    2. **要让形状本身说"可以按"**。行高 56、圆角、hover 变色 —— 这些在列表模式里
       是"内容"，在卡片模式里才是"按钮"。现场反馈"看不出能点"就出在这里。

    item 上的 text（`"1    开始任务"`）保留着纯文本表示，给无障碍与测试用；
    视觉一律走这里的数据（UserRole=名字，UserRole+1=流程序号）。

    ⚠️ 这个类**不持有面板的任何引用**，主题从 `theme.active_mode()` 现读、尺寸走 item
    自己的 `sizeHint`。原因见 `theme.active_mode` 的说明：delegate 反向引用宿主窗口会
    形成 Python 引用环，环被回收的顺序不定，实测会让之后任意一次 `setStyleSheet` 段错误。
    """

    #: 卡片之间的缝。它从格子宽里扣，所以卡片自己算尺寸时只画"实心那块"。
    GAP = 8
    #: 序号徽章边长
    BADGE = 26
    RADIUS = 12

    def paint(self, painter, option, index):  # noqa: N802 (Qt 命名)
        # 自绘必须保证 QPainter 收尾：中途抛异常会让它来不及 end()，之后整个进程可能
        # 以段错误收场（本项目踩过一次，崩点离病因很远）。绘制失败也不能把 GUI 拖崩。
        painter.save()
        try:
            self._paint(painter, option, index, theme.palette(theme.active_mode()))
        except Exception as exc:             # noqa: BLE001
            logger.warning(f"标签卡片绘制失败（已跳过这张卡）：{exc}")
        finally:
            painter.restore()

    def _paint(self, painter, option, index, colors) -> None:
        rect = QRect(option.rect)
        rect.setWidth(max(rect.width() - self.GAP, 1))
        rect.setHeight(max(rect.height() - self.GAP, 1))

        name = str(index.data(Qt.UserRole) or "")
        number = index.data(Qt.UserRole + 1)
        hovered = bool(option.state & QStyle.State_MouseOver)
        selected = bool(option.state & QStyle.State_Selected)
        active = hovered or selected

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(colors["accent"] if active else colors["line_strong"]), 1))
        painter.setBrush(QColor(colors["accent_soft"] if selected else colors["surface"]))
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), self.RADIUS, self.RADIUS)

        badge = QRect(rect.left() + 12, rect.top() + (rect.height() - self.BADGE) // 2,
                      self.BADGE, self.BADGE)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors["accent_soft"] if active else colors["surface3"]))
        painter.drawRoundedRect(badge, 8, 8)
        painter.setPen(QColor(colors["accent"] if active else colors["muted"]))
        painter.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        painter.drawText(badge, Qt.AlignCenter,
                         "·" if number is None else str(number))

        text_left = badge.right() + 12
        text_rect = QRect(text_left, rect.top(),
                          max(rect.right() - 12 - text_left, 10), rect.height())
        font = QFont()
        font.setPixelSize(theme.TYPE_SCALE["fs_base"])
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(colors["accent"] if selected else colors["text"]))
        elided = painter.fontMetrics().elidedText(name, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)


class PointDialog(QDialog):
    """打点面板：录制期间常驻，点标签即记录此刻；也能改 / 删本次已打的点。"""

    #: 点数发生变化（新增/修改/删除）时发一次，主窗口据此更新「打点 (N)」计数
    points_changed = pyqtSignal()

    def __init__(self, config, recorder, parent=None):
        """
        Args:
            config: ConfigManager（读打点标签库、打开标签管理）。
            recorder: `core_points.PointRecorder`（本次录制会话）。
            parent: 主窗口（面板贴它右侧显示）。
        """
        super().__init__(parent)
        self.setObjectName("PointPanel")
        self.config = config
        self.recorder = recorder
        self._last_label = ""
        self._last_record_at = 0.0
        self._placed = False
        self._grid_cols = 2
        self._grid_key = None
        self._cell_size = QSize(CARD_MIN_W, CARD_H)

        self.setWindowTitle(f"打点 · {self._task() or '未选采集任务'}")
        # 非模态：录制中操作员还要按主窗口的「停止录制」与急停柱
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self._build_ui()
        self.reload_labels()
        self._refresh_records()
        self._refresh_where()
        self.resize(theme.px(496), self._preferred_height())

    # ---------- 界面 ----------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_label_section())
        layout.addWidget(self._divider())
        layout.addLayout(self._build_input_section())
        layout.addWidget(self._divider())
        # 只有已打点区随窗口长高：它是唯一"越用越长"的部分
        layout.addWidget(self._build_records_section(), 1)
        layout.addLayout(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        """一行说清"这场录制是谁的、点记到哪去了"，右侧是标签库入口。

        点数用徽章而不是句子：`已写入 4 条` / `待写入 2 条` 一眼可辨，
        而且状态色（绿 / 黄）能直接顶上 —— 现场不必读完整句话就知道有没有写进文件。
        """
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])

        self.where_badge = QLabel("")
        self.where_badge.setObjectName("Badge")
        row.addWidget(self.where_badge)

        self.where_label = QLabel("")
        self.where_label.setObjectName("Hint")
        self.where_label.setWordWrap(True)
        row.addWidget(self.where_label, 1)

        self.btn_manage_labels = QPushButton("管理标签…")
        self.btn_manage_labels.setProperty("variant", "ghost")
        self.btn_manage_labels.setToolTip(
            f"增删改打点标签（存到 {self.config.get_point_labels_file()}）")
        self.btn_manage_labels.clicked.connect(self.on_manage_labels)
        row.addWidget(self.btn_manage_labels, 0, Qt.AlignTop)
        return row

    def _build_label_section(self) -> QFrame:
        """标签区：分区标题 + 卡片网格。

        标题右侧挂两个东西：`共 N 个`（弱提示）与本次录制的任务徽章 —— 标签是**按任务
        分组**给的，把"这批标签属于谁"放在它们旁边，比放在窗口标题里更不容易看漏。
        """
        frame = QFrame()
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(SP["sp2"])

        head = QHBoxLayout()
        head.setSpacing(SP["sp2"])
        self.label_title = QLabel("标签 · 点一下即记录此刻")
        self.label_title.setObjectName("SectionTitle")
        self.label_title.setFont(
            theme.mono_font(theme.TYPE_SCALE["fs_sm"], letter_spacing=1.0))
        head.addWidget(self.label_title)
        head.addStretch()

        self.label_hint = QLabel("")
        self.label_hint.setObjectName("SectionNote")
        head.addWidget(self.label_hint)

        self.label_task = QLabel("")
        self.label_task.setObjectName("Badge")
        head.addWidget(self.label_task)
        box.addLayout(head)

        self.label_list = QListWidget()
        self.label_list.setObjectName("PointLabels")
        # 图标模式的"网格流"：左到右排、放不下换行、随宽度重排。
        # 用视图模式而不是自己摆一堆按钮，是为了保住 itemClicked / itemActivated /
        # setCurrentRow 这套既有语义（键盘 Enter 打点、双击去重都挂在上面）。
        self.label_list.setViewMode(QListView.IconMode)
        self.label_list.setFlow(QListView.LeftToRight)
        self.label_list.setWrapping(True)
        self.label_list.setResizeMode(QListView.Adjust)
        self.label_list.setMovement(QListView.Static)
        self.label_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.label_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.label_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.label_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 刻意不开 uniformItemSizes：它会把第一张卡片的 sizeHint 缓存下来复用，
        # 而这里的卡片宽度是随面板宽度变的（见 _relayout_grid），缓存住就再也跟不上。
        # 标签只有几个，逐个量尺寸的开销可以忽略。
        # delegate 用 item 自己的 sizeHint（Qt 的 SizeHintRole），所以它不需要知道面板
        self.label_list.setItemDelegate(_LabelCardDelegate(self.label_list))
        # 悬停要变色就得真的收到 hover：视图默认只在按住时跟踪鼠标
        self.label_list.setMouseTracking(True)
        self.label_list.viewport().setAttribute(Qt.WA_Hover, True)
        # 卡片是"点一下就打点"的，光标要明确起来
        self.label_list.viewport().setCursor(Qt.PointingHandCursor)
        self.label_list.itemClicked.connect(self.on_label_clicked)
        self.label_list.itemActivated.connect(self.on_label_activated)
        box.addWidget(self.label_list)
        return frame

    def _build_input_section(self) -> QVBoxLayout:
        """手输一行：新标签 + 「打点」，下一行是下一个点的备注。

        「打点」是整屏唯一的实心强调色控件（现场要求：它要最显眼、按一下就出一个点）。
        它与输入框等高，`variant="point"` 的样式在 theme 里按 `#PointPanel` 收窄过。
        """
        box = QVBoxLayout()
        box.setSpacing(SP["sp2"])

        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        row.addWidget(self._field_label("新标签"))
        # 提示文字用 HintLineEdit 自绘（浅色）：QSS 里的 color 会压掉 Qt 自带的占位色，
        # 自带占位只能渲染成与正文相同的深色 —— 现场反馈"提示字太重"。
        # 文案要短到**一行放得下**：这一行右边还站着打点按钮，放不下就会被截成半句话
        self.edit_label = HintLineEdit("留空就记「点N（待命名）」")
        self.edit_label.setFixedHeight(ROW_H)
        self.edit_label.returnPressed.connect(self.on_record_input)
        row.addWidget(self.edit_label, 1)

        self.btn_record_input = QPushButton("打点")
        self.btn_record_input.setProperty("variant", "point")
        self.btn_record_input.setFixedHeight(ROW_H)
        self.btn_record_input.setToolTip(
            "记一个点：用「新标签」里填的名字；没填就记成「点N（待命名）」，事后可改")
        self.btn_record_input.clicked.connect(self.on_record_input)
        row.addWidget(self.btn_record_input)
        box.addLayout(row)

        note_row = QHBoxLayout()
        note_row.setSpacing(SP["sp2"])
        note_row.addWidget(self._field_label("备注"))
        self.edit_note = HintLineEdit("可空；记在下一次打点上，记完自动清空")
        self.edit_note.setFixedHeight(ROW_H)
        note_row.addWidget(self.edit_note, 1)
        box.addLayout(note_row)
        return box

    def _build_records_section(self) -> QFrame:
        """核对区：改 / 删的入口放在分区标题行右侧，与它管的那个列表贴在一起。

        不把按钮放在列表下方的好处是少一行 —— 更重要的是"选中 → 去下面找按钮"
        这条视线往返没有了，按钮就在标题那行，眼睛不用离开列表。

        一条点都没有时用一句提示替掉空列表：空白卡片占着最显眼的位置却不说明任何事，
        现场会以为"这里坏了"。
        """
        frame = QFrame()
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(SP["sp2"])

        head = QHBoxLayout()
        head.setSpacing(SP["sp2"])
        self.records_title = QLabel("本次已打点（0）")
        self.records_title.setObjectName("SectionTitle")
        self.records_title.setFont(
            theme.mono_font(theme.TYPE_SCALE["fs_sm"], letter_spacing=1.0))
        head.addWidget(self.records_title)
        head.addStretch()

        self.btn_edit = QPushButton("改选中…")
        self.btn_edit.setProperty("variant", "ghost")
        self.btn_edit.setToolTip("改选中那条的标签与备注（时间不会变）")
        self.btn_edit.setEnabled(False)
        self.btn_edit.clicked.connect(self.on_edit_record)
        head.addWidget(self.btn_edit)

        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.setProperty("variant", "ghost-danger")
        self.btn_delete.setToolTip(
            "删掉选中那条；打错的直接删，序号会自动重排、文件同步更新")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self.on_delete_record)
        head.addWidget(self.btn_delete)
        box.addLayout(head)

        self.records_empty = QLabel("这场录制还没有打点：点上面的标签，或用「打点」记一个")
        self.records_empty.setObjectName("Hint")
        self.records_empty.setAlignment(Qt.AlignCenter)
        self.records_empty.setFixedHeight(RECORD_ROW_FALLBACK * RECORD_MIN_ROWS)
        box.addWidget(self.records_empty)

        self.records_list = QListWidget()
        self.records_list.setObjectName("PointRecords")
        self.records_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.records_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.records_list.itemSelectionChanged.connect(self.on_record_selected)
        self.records_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.records_list.customContextMenuRequested.connect(self.on_record_menu)
        box.addWidget(self.records_list)
        return frame

    def _build_footer(self) -> QHBoxLayout:
        """底部只负责"最近一次操作的回执" —— 写入位置已经在顶部说过了，不重复。"""
        row = QHBoxLayout()
        row.setSpacing(SP["sp3"])
        self.status_label = QLabel("点上面的标签，即以那一刻记一个点")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)

        # 关掉不是"结束打点"：面板只是收起来，录制中随时能从操作区的「打点」
        # 再打开；录制结束时面板会被自动关掉
        self.btn_close = QPushButton("收起")
        self.btn_close.setProperty("variant", "ghost")
        self.btn_close.setToolTip("收起面板（录制中随时可从操作区「打点」再打开）")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        return row

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        label.setFixedWidth(theme.px(FIELD_LABEL_W))
        return label

    # ---------- 网格排版 ----------
    def resizeEvent(self, event):        # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._relayout_quietly()

    def _relayout_quietly(self):
        """重排是"锦上添花"的动作，出错绝不能把界面带走。

        它跑在 Qt 的虚函数里（`resizeEvent` / `showEvent`），而 PyQt5 对虚函数里的
        未捕获异常是**直接 abort 整个进程**，且不走 `sys.excepthook` ——
        `logs/ui_error.log` 里一个字都不会留，现场只剩"界面忽然没了"。

        所以这里自己吞掉并记日志，而不是套 `guard_slot`：那个在首次异常时会弹一个
        模态框，而模态框出现在布局过程中比不出效果更糟（会挡住正在进行的录制操作）。
        """
        try:
            self._relayout_grid()
        except Exception as exc:             # noqa: BLE001
            logger.warning(f"标签网格重排失败（已跳过本次）：{exc}")

    def _relayout_grid(self):
        """按可用宽度决定列数并算卡片宽度，让网格永远填满整行、不留半列空隙。

        两个都是实测出来的坑（现象都是"标签排成一列、右边空一大块、还要滚动"，
        从现象几乎推不回原因）：

        1. **`gridSize` 的宽度不能带卡片缝**。Qt 是按格子宽决定一行放几个的，
           把缝算进去就会出现 `2 × (223 + 8) > 454` 这种"明明够两列却只排一列"。
           缝交给 delegate 在卡片矩形上内缩（见 `_LabelCardDelegate.GAP`）。
        2. **必须预留竖滚动条的宽度**。滚动条是否出现取决于内容高度，而它的出现会挤掉
           一条宽度 → 第二列放不下 → 行数翻倍 → 更需要滚动条。这是个自我强化的死结：
           实测面板宽 496 时刚好卡在门槛上，于是永远只有一列。
           一律预留之后，有没有滚动条都算出同一个列数，状态就稳了。

        ⚠️ 另外**不要**给视图装事件过滤器来盯它的宽度：实测那样会在用例里留下
        "对话框已被垃圾回收、过滤器还挂在子控件上"的状态，下一次 `setStyleSheet`
        直接段错误（崩在主题层，离病因很远）。这里改成由面板自己的 resizeEvent 触发，
        加上构造后与 show 前各算一次（见 `__init__` / `showEvent`），够用了。

        列数只取 1 或 2：卡片最小宽 176，两列在 496 宽的面板里刚好；三列会把
        "双爪夹住第一个角"这类长标签压到要省略。
        """
        width = self.label_list.viewport().width()
        if width <= 0:
            return
        gap = _LabelCardDelegate.GAP
        reserve = self.label_list.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        usable = max(width - reserve, 1)
        cols = 2 if usable >= CARD_MIN_W * 2 + gap else 1
        cell = max((usable - gap * (cols - 1)) // cols, 1)
        if (cols, cell) == self._grid_key:
            return                       # 重排会引起新一轮 resize，靠这个出口收敛
        self._grid_key = (cols, cell)
        self._grid_cols = cols
        self._cell_size = QSize(cell, CARD_H)
        self._apply_cell_size()
        self.label_list.setGridSize(QSize(cell, CARD_H + gap))
        self._fit_label_list_height()

    def _apply_cell_size(self):
        """把当前格子尺寸写到每张卡片上。

        卡片要**填满整格**：图标模式下 Qt 会把小于格子的卡片居中放，两侧留出等宽空隙
        （实测格子 223、卡片 176 时每边空 23px，整片网格看着很散）。

        尺寸走 item 自己的 sizeHint（Qt 的 `SizeHintRole`）而不是 delegate 的 `sizeHint()` ——
        这样 delegate 不必知道面板的存在，也就不会有"delegate 反向引用窗口"的引用环
        （见 `theme.active_mode` 的说明）。
        """
        size = self._cell_size
        for index in range(self.label_list.count()):
            item = self.label_list.item(index)
            if item.sizeHint() != size:
                item.setSizeHint(size)

    def _fit_label_list_height(self):
        """把网格高度调到刚好放下全部标签（上下限内）。

        写死高度会在行数不整除时切出半张卡 —— 现场看到卡片被截一半只会以为界面坏了，
        而这里本来就是要让人一眼看全的。

        `LABEL_HEIGHT_SLACK` 那几 px 是**故意给多出来的**：高度正好等于内容高度时，
        Qt 会在"要不要显示滚动条"之间摇摆，而滚动条一出现就挤窄视图、可能把网格从
        两列挤成一列（见 `_relayout_grid` 第 2 条）。多这几 px，放得下就绝不出现滚动条。
        """
        gap = _LabelCardDelegate.GAP
        step = CARD_H + gap
        # 上限也带上那 4px：否则"刚好三行"会被封顶到内容高度，又变成要滚动的边界情况
        capped = LABEL_MAX_ROWS * step + LABEL_HEIGHT_SLACK
        count = self.label_list.count()
        rows = max(1, math.ceil(count / max(self._grid_cols, 1)))
        needed = rows * step + LABEL_HEIGHT_SLACK
        self.label_list.setFixedHeight(max(min(needed, capped), step + LABEL_HEIGHT_SLACK))

    # ---------- 显示位置 ----------
    def showEvent(self, event):        # noqa: N802 (Qt 命名)
        super().showEvent(event)
        # 构造时视图宽度还是默认值，真正的宽度要到布局跑完才知道：临显示前再算一次，
        # 免得万一这次没有触发 resizeEvent 就把"按错误宽度排的网格"带上去
        self._relayout_quietly()
        if not self._placed:
            self._placed = True
            self._place_beside_parent()

    def _place_beside_parent(self):
        """贴主窗口右侧显示：录制中操作员还要按「停止录制」和急停柱，能不压住就不压住。

        屏幕放不下（主窗口最大化等）就靠屏幕右边缘 clamp，绝不跑到屏幕外 ——
        跑出去比压住更糟：面板会变得完全够不着。

        纵向按"面板还能长到多高"来留位置：已打点列表会随着点数变多长高，
        只按当前高度算位置，长高之后底部就会掉出屏幕。
        撞到屏幕高度上限时把面板的高度也卡住（宁可列表内滚动，也不要够不着）。
        """
        parent = self.parentWidget()
        if parent is None:
            return
        screen = QApplication.desktop().availableGeometry(self)

        growth = 0
        if self.records_list.isVisible():
            # 列表控件的 maximumHeight 默认是个天文数字（等于"没有上限"），
            # 直接拿来算会把"最高能长到多少"算成屏幕高度，所以先夹到"最多长满 5 行"
            growth = max(self.records_list.maximumHeight() - self.records_list.height(), 0)
            growth = min(growth, RECORD_MAX_ROWS * RECORD_ROW_FALLBACK + 16)
        room = screen.height() - 16
        if self.height() + growth > room:
            self.setMaximumHeight(room)
        tallest = min(self.height() + growth, self.maximumHeight())

        frame = parent.frameGeometry()
        x = frame.right() + SP["sp3"]
        if x + self.width() > screen.right():
            x = max(screen.left() + 8, screen.right() - self.width() - 8)
        y = min(max(frame.top(), screen.top() + 8), screen.bottom() - tallest - 8)
        self.move(x, max(y, screen.top() + 8))

    def _preferred_height(self) -> int:
        """面板的默认高度：不高过屏幕，也不为"内容还没长出来"留大片空白。

        直接写死高度的话，刚起录（一个点都还没打）时下面会空出一大块 ——
        而这一屏的留白本来就不宽裕。
        """
        layout = self.layout()
        wanted = layout.sizeHint().height() if layout is not None else 0
        screen = QApplication.desktop().availableGeometry(self)
        return int(max(min(wanted, screen.height() - 24), 420))

    # ---------- 刷新 ----------
    def _refresh_where(self):
        """顶部那行：点数徽章 + 写到哪（或"还没找到目录、先攒着"）。"""
        count = self.recorder.count
        csv_path = self.recorder.csv_path()
        if csv_path is not None:
            self.where_badge.setText(f"已写入 {count} 条")
            theme.set_state(self.where_badge, "ok")
            # 只留目录名 + 文件名：完整路径太长，会把徽章和入口挤到换行；完整路径进 tooltip
            self.where_label.setText(f"{csv_path.parent.name}/{csv_path.name}")
            self.where_label.setToolTip(f"打点写在这个文件里，随数据一起上传：\n{csv_path}")
        else:
            self.where_badge.setText(f"待写入 {count} 条")
            theme.set_state(self.where_badge, "warn")
            self.where_label.setText("还没找到录制目录，先攒着、下次打点时补写")
            self.where_label.setToolTip(
                f"{self.recorder.last_error or '目录尚未出现'}\n"
                "找不到目录不影响打点：记录留在内存里，等目录出现就补写；"
                "整场都没关联上会额外落一个 points_unlinked_*.csv 供对账。")

    @guard_slot
    def reload_labels(self):
        """刷新标签网格：**一条流程按顺序显示**（通用首项 → 本任务标签 → 通用末项）。

        不把"通用"单独列一段：它装的是开始 / 结束这类夹在流程两端的标签，拆两段会出现
        "通用 1、2 + 任务 1、2、3、4"两套编号，看着像重复。合并后现场看到的就是
        `1 开始任务 … 6 结束任务` 一条顺序，**序号就是位置**。
        """
        self.label_list.clear()
        task = self._task()
        flow = _point_flow(self.config, task)

        for position, item in enumerate(flow, start=1):
            # text 是纯文本表示（无障碍与用例用），卡片外观由 delegate 画
            entry = QListWidgetItem(f"{position}    {item['name']}")
            entry.setData(Qt.UserRole, item["name"])
            entry.setData(Qt.UserRole + 1, position)      # 面板序号 = 写进 CSV 的编号
            entry.setToolTip(f"{position}. {item['name']}\n点一下即以此刻记一个点")
            self.label_list.addItem(entry)

        self.label_title.setText("标签 · 点一下即记录此刻")
        self._sync_task_badge(task)
        self._apply_cell_size()
        self._fit_label_list_height()
        if flow:
            self.label_hint.setText(f"共 {len(flow)} 个")
        elif task:
            self.label_hint.setText(f"「{task}」还没有标签，在下面输入会加进这一组")
        else:
            self.label_hint.setText("还没有通用标签，在下面输入即可")

    def _sync_task_badge(self, task: str):
        """任务徽章：说清"这批标签属于哪一次采集任务"。

        没选任务时用 warn 色明说 —— 那种情况只列通用标签（"开始任务 / 结束任务"），
        不说清的话现场会以为"标签丢了"。
        """
        if task:
            self.label_task.setText(task)
            self.label_task.setToolTip(f"本次录制的采集任务：{task}\n标签就是按它给的")
            theme.set_state(self.label_task, "accent")
        else:
            self.label_task.setText("未选采集任务")
            self.label_task.setToolTip(
                "本次录制没选采集任务，所以只列通用标签；要按任务区分请在录制时选一个任务")
            theme.set_state(self.label_task, "warn")

    def _refresh_records(self):
        """已打点列表：等宽单行读数，列对齐靠等宽字体（序号 / 时刻 / 相对秒 / 标签）。

        重建列表会丢选中状态。改完标签后操作员往往还要接着改下一条 —— 这里把
        原先选中的序号恢复回去，避免"改一次就得重新点一次选中"。
        """
        keep = self._selected_index()
        self.records_list.blockSignals(True)
        self.records_list.clear()
        restore = None
        for record in self.recorder.records:
            text = (f"#{record.index:<2} {record.clock_ms}"
                    f"  +{record.rel_seconds:>6.1f}s  {record.label or '（未填标签）'}")
            if record.note:
                text += f"   · {record.note}"
            entry = QListWidgetItem(text)
            entry.setData(Qt.UserRole, record.index)
            entry.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
            entry.setToolTip(f"序号 {record.index} · {record.wall_time}\n"
                             f"标签 {record.label or '（未填）'}"
                             + (f"\n备注 {record.note}" if record.note else "")
                             + "\n\n选中这一行可以用右侧的按钮改或删")
            self.records_list.addItem(entry)
            if keep is not None and record.index == keep:
                restore = self.records_list.count() - 1
        self.records_list.blockSignals(False)
        if restore is not None:
            self.records_list.setCurrentRow(restore)
        self.records_list.scrollToBottom()

        self.records_title.setText(f"本次已打点（{self.recorder.count}）")
        self._fit_records_height()
        self.on_record_selected()

    def _fit_records_height(self):
        """已打点区的高度随条数走（2~5 行）：空的时候不留一大块空地。

        一条都没有时直接换成一句提示 —— 空白卡片占着最显眼的位置却不说明任何事。
        """
        count = self.records_list.count()
        self.records_empty.setVisible(count == 0)
        self.records_list.setVisible(count > 0)
        if count == 0:
            return
        row = self.records_list.sizeHintForRow(0)
        if row <= 0:
            row = RECORD_ROW_FALLBACK
        chrome = 2 * self.records_list.frameWidth() + 4
        self.records_list.setMinimumHeight(
            max(RECORD_MIN_ROWS, min(count, RECORD_MAX_ROWS)) * row + chrome)
        self.records_list.setMaximumHeight(RECORD_MAX_ROWS * row + chrome)

    def _task(self) -> str:
        """本次录制的采集任务名（打点标签按它分组）。"""
        return getattr(self.recorder, "task", "") or ""

    def _selected_index(self) -> Optional[int]:
        item = self.records_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    # ---------- 打点 ----------
    @guard_slot
    def on_label_clicked(self, item):
        """单击标签 → 立刻以这一刻记一个点。"""
        name = item.data(Qt.UserRole)
        if not name:
            return
        self.record_now(name)

    @guard_slot
    def on_label_activated(self, item):
        """键盘 Enter 激活（双击也会走这里）→ 与单击同一条路径。"""
        name = item.data(Qt.UserRole)
        if not name:
            return
        self.record_now(name)

    @guard_slot
    def on_record_input(self):
        """打一个点：用「新标签」里填的名字；**没填就用占位名**。

        现场常有"先记下来、回头再命名"的那一刻（这一下等不起你想名字），所以空输入不再
        报错拦下，而是记成 `点N（待命名）`；事后在「本次已打点」里选中 → 「改选中…」补名字。
        """
        label = self.edit_label.text().strip()
        if not label:
            label = next_placeholder_label(self.recorder.records)
        if self.record_now(label) is not None:
            self.edit_label.clear()

    def record_now(self, label: str) -> Optional[PointRecord]:
        """以**调用这一刻**为准记一个点：先把时间锁死，再做界面工作。

        这条顺序是整个打点功能的地基 —— 先弹窗、等选完再取时间，记下的是"选完的时间"。

        Returns:
            写入的记录；被去重拦下或标签为空时返回 None。
        """
        label = str(label or "").strip()
        if not label:
            self._set_status("标签不能为空", "danger")
            return None

        moment = time.time()                      # ← 这就是"打点那一刻"，先取
        if (label == self._last_label
                and moment - self._last_record_at < DOUBLE_CLICK_GUARD):
            # 一次双击 = itemClicked×2 + itemActivated，不拦会记下三个点。
            # 只认第一次：人手连点同一个标签不会快到 0.6 秒。
            return None
        self._last_label, self._last_record_at = label, moment

        note = self.edit_note.text().strip()
        added = _ensure_point_label(self.config, self._task(), label)
        if added:
            self.reload_labels()                  # 新标签立刻进网格，下次直接点

        record = self.recorder.add(label,
                                   _point_label_number(self.config, self._task(), label),
                                   note, epoch=moment)
        self.edit_note.clear()                    # 备注只作用于"下一个点"

        parts = [f"已记 #{record.index} {record.clock_ms} {label}"]
        if note:
            parts.append("含备注")
        if added:
            parts.append(f"新标签已加进「{self._task()}」这一组" if self._task()
                         else "新标签已加进通用标签")
        elif is_placeholder_label(label):
            parts.append("占位名，稍后可在「改选中…」里补名字")
        self._set_status(" · ".join(parts), "ok")

        self._refresh_records()
        self._refresh_where()
        self._log(f"打点 #{record.index} {record.clock_ms} {label}"
                  + ("" if self.recorder.pending_count == 0
                     else "（还没找到录制目录，已先攒着）"))
        self.points_changed.emit()
        return record

    # ---------- 改 / 删 ----------
    @guard_slot
    def on_record_selected(self):
        """选中变化只切换按钮可用性 —— **不改打点行为**。

        v4.9 里"选中一行"会进入编辑模式、把点标签变成"填入"，于是操作员点完一行再点
        标签就什么都不发生（还以为坏了）。现在改/删都必须显式点按钮。
        """
        index = self._selected_index()
        self.btn_delete.setEnabled(index is not None)
        self.btn_edit.setEnabled(index is not None)

    @guard_slot
    def on_record_menu(self, pos):
        """右键一条已打点：改 / 删。

        这是"显式按钮"之外的一条短路（鼠标用户不必先选中再跑到标题行去找按钮）。
        刻意不做"双击即改"：编辑框是模态的，误触会把它弹在「停止录制」前面 ——
        录制中任何可能挡住主窗口的东西都要谨慎。
        """
        item = self.records_list.itemAt(pos)
        if item is None:
            return
        self.records_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("修改标签 / 备注…", self.on_edit_record)
        menu.addAction("删除这个点…", self.on_delete_record)
        menu.exec_(self.records_list.mapToGlobal(pos))

    @guard_slot
    def on_edit_record(self):
        index = self._selected_index()
        if index is None:
            self._set_status("先在下面选中一条要修改的记录", "danger")
            return
        dialog = PointEditDialog(self.config, self.recorder, index, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self._set_status(f"已更新 #{index}", "ok")
        self._refresh_records()
        self._refresh_where()
        self._log(f"打点 #{index} 已修改为 {dialog.edit_label.text().strip()}")
        self.points_changed.emit()

    @guard_slot
    def on_delete_record(self):
        index = self._selected_index()
        if index is None:
            self._set_status("先在下面选中一条要删除的记录", "danger")
            return
        if QMessageBox.question(
                self, "删除打点",
                f"确定删掉 #{index} 这个点吗？\n序号会重排，文件里的记录同步更新。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if self.recorder.remove(index):
            self._set_status(f"已删除 #{index}", "ok")
        else:
            self._set_status(self.recorder.last_error or "删除失败", "danger")
        self._refresh_records()
        self._refresh_where()
        self._log(f"打点 #{index} 已删除")
        self.points_changed.emit()

    @guard_slot
    def on_manage_labels(self):
        """打开打点标签管理（按任务分组，只作用于打点库）。改完回来刷新网格。"""
        PointLabelDialog(self.config, self).exec_()
        self.reload_labels()

    # ---------- 小工具 ----------
    def _set_status(self, text: str, state=None):
        self.status_label.setText(text)
        if state:
            theme.set_state(self.status_label, state)

    def _log(self, text: str):
        """同步一行到主窗口日志区：打点是数据的一部分，日志里留痕便于事后核对。"""
        append = getattr(self.parentWidget(), "append", None)
        if callable(append):
            append(text)


class PointEditDialog(QDialog):
    """改一条已打的点（标签 / 备注）。**时间不动** —— 时间才是打点的本体。"""

    def __init__(self, config, recorder, index: int, parent=None):
        super().__init__(parent)
        self.setObjectName("PointPanel")
        self.config = config
        self.recorder = recorder
        self.index = int(index)
        record = next((item for item in recorder.records if item.index == self.index), None)
        if record is None:
            raise ValueError(f"没有 #{self.index} 这条打点记录")
        self.record = record
        self.saved = False

        self.setWindowTitle(f"修改打点 #{self.index}")
        self.setModal(True)
        self.resize(theme.px(460), theme.px(220))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])

        # 时刻做成徽章：它是这一屏里唯一不可改的东西，值得有独立形状（也提醒"别动它"）
        moment_row = QHBoxLayout()
        moment_row.setSpacing(SP["sp2"])
        moment = QLabel(f"{record.clock_ms}  ·  录制开始后 {record.rel_seconds:.1f} 秒")
        moment.setObjectName("Badge")
        moment_row.addWidget(moment)
        moment_row.addStretch()
        layout.addLayout(moment_row)

        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        label = QLabel("标签")
        label.setObjectName("FieldLabel")
        label.setFixedWidth(theme.px(FIELD_LABEL_W))
        row.addWidget(label)
        self.edit_label = QLineEdit(record.label)
        self.edit_label.returnPressed.connect(self.on_save)
        row.addWidget(self.edit_label, 1)
        layout.addLayout(row)

        note_row = QHBoxLayout()
        note_row.setSpacing(SP["sp2"])
        note = QLabel("备注")
        note.setObjectName("FieldLabel")
        note.setFixedWidth(theme.px(FIELD_LABEL_W))
        note_row.addWidget(note)
        self.edit_note = QLineEdit(record.note)
        self.edit_note.returnPressed.connect(self.on_save)
        note_row.addWidget(self.edit_note, 1)
        layout.addLayout(note_row)

        self.status_label = QLabel("时间不会变；要删掉这个点请用面板上的「删除选中」")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_cancel)
        self.btn_save = QPushButton("保存")
        self.btn_save.setProperty("variant", "launch")
        self.btn_save.clicked.connect(self.on_save)
        buttons.addWidget(self.btn_save)
        layout.addLayout(buttons)

    @guard_slot
    def on_save(self):
        label = self.edit_label.text().strip()
        if not label:
            self._set_status("标签不能为空（要删掉这个点就用「删除选中」）", "danger")
            return
        _ensure_point_label(self.config, getattr(self.recorder, "task", "") or "", label)
        if not self.recorder.update(self.index, label,
                                    _point_label_number(
                                        self.config,
                                        getattr(self.recorder, "task", "") or "", label),
                                    self.edit_note.text()):
            self._set_status(self.recorder.last_error or "更新失败", "danger")
            return
        # 记录已改成功；目录还没出现时改动会暂存待补写，如实说一声而不是静默
        if self.recorder.last_error:
            self._set_status(f"已更新（{self.recorder.last_error}）", "warn")
        else:
            self._set_status("已更新", "ok")
        self.saved = True
        self.accept()

    def _set_status(self, text: str, state=None):
        self.status_label.setText(text)
        if state:
            theme.set_state(self.status_label, state)
