"""基础控件：可点击标签、浅色提示输入框、日志视图、电量指示器。"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication,
                             QStyle, QStyleOptionFrame)
from PyQt5.QtCore import (Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect,
                          QRectF, QPointF)
from PyQt5.QtGui import (QColor, QFont, QPainter, QPalette, QPen, QPolygon,
                           QPolygonF)
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
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError


from .common import (LOW_BATTERY_PERCENT, LOW_BATTERY_CRITICAL_PERCENT)



class HintLineEdit(QLineEdit):
    """带浅色提示文字的输入框。

    ## 为什么不用 QLineEdit 自带的占位文字

    QSS 没有 `:placeholder` 选择器，而项目的基础样式给所有控件写了 `color`（`QWidget` 那条）；
    **实测**它是压着占位色走的：给控件设 `QPalette::PlaceholderText` 完全不起作用，自带占位
    只能渲染成与正文一样的深色（现场反馈"提示字太重"就是这个）。

    所以这里让控件**不设**占位文字，改在 `paintEvent` 之后自己补一句：

    - 颜色取正文字色的半透明（`HINT_ALPHA`）—— 半透明天然与控件实际背景混色，
      亮色主题下呈浅灰、暗色主题下呈暗灰，两边都"淡"；
    - 每次绘制都从 palette 读 → 顶栏切主题时自动跟随，不需要额外的通知机制。

    只在**内容为空**时画，输入过内容就完全不参与（与自带占位的语义一致）。
    """

    #: 提示文字的不透明度：与背景混出"能看清但不抢眼"的一层
    HINT_ALPHA = 0.45

    def __init__(self, hint: str = "", parent=None):
        super().__init__(parent)
        self._hint = str(hint or "")
        self.setPlaceholderText("")      # 自带那层不用，见类文档

    def hint(self) -> str:
        return self._hint

    def set_hint(self, hint: str) -> None:
        self._hint = str(hint or "")
        self.update()

    def paintEvent(self, event):        # noqa: N802 (Qt 命名)
        super().paintEvent(event)
        if self.text() or not self._hint:
            return
        painter = QPainter(self)
        try:
            color = self.palette().color(QPalette.Text)
            color.setAlphaF(self.HINT_ALPHA)
            painter.setPen(color)
            # 内容矩形交给样式算：QSS 里的 border / padding 都会体现在这里，
            # 所以自绘的提示与正文的左边距天然对齐（不必手抄内距数值）
            option = QStyleOptionFrame()
            self.initStyleOption(option)
            rect = self.style().subElementRect(QStyle.SE_LineEditContents, option, self)
            painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, self._hint)
        finally:
            # 自绘必须保证 QPainter 收尾：中途异常会让它来不及 end()，之后整个进程
            # 可能以段错误收场（本项目踩过一次）
            painter.end()


class ClickableLabel(QLabel):
    """可点击标签：顶栏「版本」用它来切换机器人工作空间。"""
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class LogView(QTextEdit):
    """只读日志区，额外支持点击其中的链接（用于"打开产物目录"这类快捷入口）。

    QTextEdit 不像 QTextBrowser 会发 anchorClicked，所以自己处理按下事件：
    命中锚点就交给系统默认程序打开，未命中则不拦截（保留正常选中/复制行为）。
    """

    def mousePressEvent(self, event):
        anchor = self.anchorAt(event.pos())
        if anchor:
            QDesktopServices.openUrl(QUrl(anchor))
            return
        super().mousePressEvent(event)


class BatteryIndicator(QWidget):
    """电量环（B 方案）：一圈进度环 + 右侧百分比，充电时环心叠一个闪电。

    为什么自绘而不是用 QProgressBar 拼：
    1. 充电与否是"同一个图标上的第二种状态"，两个控件拼在小尺寸下必然对不齐；
    2. 环的形状比条形表更省宽度，顶栏那点地方要留给机型/序列号/版本；
    3. "数据过期"可以把整圈画成灰色，让"读数不可信"在图形上看得出来，
       而不是只靠颜色（颜色在强光下会失真）。

    几何：环直径 = 控件高度（保持原有占位，不改动顶栏布局）。
    """

    ICON_W = 44
    ICON_H = 26          # 环直径 + 描边预留（见 _paint 里的内缩计算）
    RING_PEN = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.ICON_W + 54, self.ICON_H)
        self._percent = None          # None = 还没收到电量话题
        self._charging = False
        self._stale = False
        self._theme = "light"

    def set_mode(self, mode: str) -> None:
        self._theme = mode
        self.update()

    def set_state(self, percent=None, charging=False, stale=False) -> None:
        """percent 传 None 表示未知；stale 表示读数已过期（画成不可信样式）。"""
        if (percent, charging, stale) == (self._percent, self._charging, self._stale):
            return
        self._percent, self._charging, self._stale = percent, charging, stale
        self.update()

    def _level_color(self, colors: dict) -> QColor:
        if self._stale or self._percent is None:
            return QColor(colors["muted2"])
        percent = self._percent if isinstance(self._percent, (int, float)) else None
        if percent is None:                       # 类型不对就按"不可信"画，不抛异常
            return QColor(colors["muted2"])
        if percent < LOW_BATTERY_CRITICAL_PERCENT:
            return QColor(colors["danger"])
        if percent < LOW_BATTERY_PERCENT:
            return QColor(colors["warn"])
        return QColor(colors["ok"])

    def paintEvent(self, event):        # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        try:
            # 取色也算绘制的一部分：它原来在 try 之外，一旦读到类型不对的电量值，
            # 异常会从 paintEvent 里漏出去 —— QPainter 来不及 end()，进程以段错误收场。
            colors = theme.palette(self._theme)
            color = self._level_color(colors)
            self._paint(painter, colors, color)
        except Exception as exc:                     # noqa: BLE001
            # 自绘控件绝不能把异常放出去：paintEvent 中途退出会让 QPainter 来不及
            # end()，之后整个进程会以段错误收场（实测踩过，崩点离病因很远）。
            logger.warning(f"电量环绘制失败：{exc}")
        finally:
            painter.end()

    def _paint(self, painter: QPainter, colors: dict, color: QColor) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 环要**整个画得下**：drawArc 的描边以路径为中心向两侧各扩 pen/2，
        # 所以路径矩形必须从边界内缩半个笔宽 —— 否则描边会被控件裁掉，
        # 现场看到的就是"环缺一块 / 显示不全"（实测反馈）。
        half_pen = self.RING_PEN // 2
        diameter = min(self.height() - 2 * half_pen, self.ICON_H - 2 * half_pen)
        ring = QRect(half_pen, (self.height() - diameter) // 2,
                     diameter, diameter)

        # 底环：整圈灰色轨道
        track = QPen(QColor(colors["line_strong"]))
        track.setWidth(self.RING_PEN)
        track.setCapStyle(Qt.RoundCap)
        painter.setPen(track)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(ring, 90 * 16, -360 * 16)

        # 电量弧：从 12 点方向顺时针（Qt 的角度是 1/16 度，逆时针为正）
        if self._percent is not None:
            ratio = max(0.0, min(1.0, self._percent / 100.0))
            span = -int(round(ratio * 360 * 16))
            if span != 0:
                pen = QPen(color)
                pen.setWidth(self.RING_PEN)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawArc(ring, 90 * 16, span)

        # 充电闪电：画在环心
        if self._charging:
            center = ring.center()
            bolt = QPolygon([
                QPoint(center.x() + 3, ring.top() + 3),
                QPoint(center.x() - 3, center.y() + 1),
                QPoint(center.x() + 1, center.y() + 1),
                QPoint(center.x() - 2, ring.bottom() - 3),
                QPoint(center.x() + 4, center.y() - 1),
                QPoint(center.x(), center.y() - 1),
            ])
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["accent"]))
            painter.drawPolygon(bolt)

        # 百分比文字
        text_rect = QRect(self.ICON_W, 0, self.width() - self.ICON_W, self.height())
        painter.setPen(QPen(QColor(colors["muted"]) if self._percent is None else color))
        painter.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        label = "—" if self._percent is None else f"{self._percent}%"
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, label)


class StatusGlyph(QWidget):
    """13×13 状态**圆点**（导出目标芯片用）：常态 / 就绪 / 未就绪**只差颜色**。

    ## 为什么三态统一成一个圆点（v6.4.9）

    设计稿里三颗芯片前面都是**同一个实心圆点**，只有颜色不同：灰 / 绿 / 琥珀。
    此前这里画的是"就绪 = 对勾、未就绪 = 圆环 + 感叹号"—— 同一颗芯片上三种形状，
    扫视时要先认形状、再读颜色；现场截的图就是那个对勾（与设计稿不一致）。
    形状统一之后，"这颗芯片什么状态"只需要读颜色一件事，而**原因**由芯片文案
    （「未就绪」）与日志区负责 —— 圆环、感叹号是第二套语言，去掉。

    ## 尺寸不是拍脑袋

    直径 12px（控件 13px，四周留 0.5px 抗锯齿余量）。设计稿量到的比例是
    `圆点 13px / 药丸高 31px ≈ 0.42`，芯片高 30 → 12px。此前是 7px，
    只有设计稿的六成 —— 现场看着"点太小、像脏点"。

    ## 为什么自绘而不是用字符或图片

    - **字符（●）**：不同平台字体里字形与基线都不一样，深色主题下尤其容易糊。
    - **图片资源**：换深色主题就要重备一套（浅色图标在深底上看不清），
      而这个项目的主题是**可切换**的，为一个小图形背两套资源不值得。
    - 项目里已有 `BatteryIndicator` 这个自绘状态控件的先例，三条纪律照它走：
      取色必须在 try 内、异常绝不外放、`finally: painter.end()`
      （paintEvent 中途退出会让 QPainter 来不及 end()，进程以段错误收场）。

    几何：圆心落在像素中心（用 `QRectF`，0.5 的偏移正好对上半像素），
    边缘因此是干净的一圈抗锯齿，不会出现"半边糊、半边硬"。
    """

    ICON = 13          #: 控件边长（`_refit_chip` 按它算芯片宽度）
    DOT = 12.0         #: 圆点直径

    #: 状态名 → 色板键。名字与 `theme.set_state` 的取值对齐（none/ok/warn/danger）。
    COLORS = {"none": "muted2", "ok": "ok", "warn": "warn", "danger": "danger"}

    def __init__(self, kind: str = "none", parent=None):
        super().__init__(parent)
        self.setFixedSize(self.ICON, self.ICON)
        self._kind = kind
        self._theme = "light"

    def set_mode(self, mode: str) -> None:
        self._theme = mode
        self.update()

    def set_kind(self, kind: str) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        self.update()

    def paintEvent(self, event):        # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        try:
            colors = theme.palette(self._theme)
            color = QColor(colors[self.COLORS.get(self._kind, "muted2")])
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            inset = (self.ICON - self.DOT) / 2.0
            painter.drawEllipse(QRectF(inset, inset, self.DOT, self.DOT))
        except Exception as exc:                     # noqa: BLE001
            # 同 BatteryIndicator：自绘控件的异常绝不能从 paintEvent 漏出去。
            logger.warning(f"状态圆点绘制失败：{exc}")
        finally:
            painter.end()


class RadioDot(QWidget):
    """16px 单选指示器：空环（未选）/ 环 + 实心点（选中），全部自绘。

    ## 为什么自绘而不是 QSS

    QSS 的 `QRadioButton::indicator` 只有「边框 + 底色」两个通道，而底色会
    **填满整个方块** —— 「外环 + 中心小点」这个形状在 QSS 里表达不出来。
    上一版用 `border: 4px solid accent` 去凑那个点，结果画出来是一个
    **方块套方块**（现场截图里最刺眼的那处），与未选态的「细边小圆」形状对不上：
    同一个控件，选与不选是两种形状。

    项目里已有 `StatusGlyph` / `BatteryIndicator` 两个自绘控件的先例，三条纪律
    照它们走：取色必须在 try 内、异常绝不外放、`finally: painter.end()`
    （paintEvent 中途退出会让 QPainter 来不及 end()，进程以段错误收场）。

    ## 为什么设 `WA_TransparentForMouseEvents`

    它住在卡片里，只负责"显示选中与否"，**点击该由整张卡片承担**。
    如果它自己吃鼠标事件，点在它头上的那一下就到不了卡片 ——
    又得回到"给每个子控件都装事件过滤器"那套补丁。让事件直接穿过去最省事。
    """

    SIZE = 16          #: 外径
    RING = 1.5         #: 环宽（1px 在现场屏幕上偏弱，2px 又显笨）
    CORE = 8           #: 选中时中心实心点的直径

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._checked = bool(checked)
        self._hover = False
        self._mode = "light"

    def set_checked(self, checked: bool) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.update()

    def is_checked(self) -> bool:
        """当前是否选中。守卫用它断言，免得去读私有属性。"""
        return self._checked

    def set_hover(self, hover: bool) -> None:
        """鼠标是否停在所属卡片上（由卡片驱动，QSS 管不到这一步）。"""
        hover = bool(hover)
        if hover == self._hover:
            return
        self._hover = hover
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def paintEvent(self, event):        # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        try:
            colors = theme.palette(self._mode)
            if self._checked:
                ring, core = QColor(colors["accent"]), QColor(colors["accent"])
            elif self._hover:
                ring, core = QColor(colors["muted2"]), None
            else:
                ring, core = QColor(colors["line_strong"]), None

            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(ring)
            pen.setWidthF(self.RING)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            # 环的路径矩形要**内缩半个笔宽**：描边以路径为中心向两侧各扩 pen/2，
            # 不内缩就会顶到控件边界被裁掉（表现是"环缺一角"，13px 的状态图标
            # 上次就踩过同一件事）。
            half = self.RING / 2.0
            span = self.SIZE - self.RING
            painter.drawEllipse(QRectF(half, half, span, span))

            if core is not None:
                painter.setPen(Qt.NoPen)
                painter.setBrush(core)
                off = (self.SIZE - self.CORE) / 2.0
                painter.drawEllipse(QRectF(off, off, self.CORE, self.CORE))
        except Exception as exc:                     # noqa: BLE001
            # 同 StatusGlyph：自绘控件的异常绝不能从 paintEvent 漏出去。
            logger.warning(f"单选指示器绘制失败：{exc}")
        finally:
            painter.end()


class CaretGlyph(QWidget):
    """12px 的展开指示（一个向下的折角），放在芯片右侧。

    ## 为什么加它

    芯片是"点开才展开面板"的入口，但此前它身上**没有任何指示**说"能点开" ——
    现场第一次用的人会当成一个静态标签。折角是这个动作的通用记号。

    ## 为什么要跟着状态变色

    一颗芯片上不该并存两套颜色语言。芯片有常态 / 就绪 / 异常三态，折角跟着走：
    常态用次要色、就绪跟绿、异常跟琥珀。为此单独取出色，而不是写死一个灰。

    ## 与同族控件一样的纪律

    自绘、取色必须在 try 内、异常绝不外放、`finally: painter.end()`
    （`paintEvent` 中途退出会让 QPainter 来不及 end()，进程以段错误收场）。

    几何：12px 见方里画一个 12px 宽、3px 落差的折角，笔宽 1.5。
    **用 `QPointF` 而不是 `QPoint`** —— 后者只接受整型，传 5.5 会抛 TypeError，
    而这里抛异常的后果是整个 `paintEvent` 中断、什么都不画。
    """

    SIZE = 12          #: 控件边长
    PEN = 1.5          #: 笔宽
    HALF = 3.5         #: 折角的半宽
    DROP = 3.0         #: 折角的落差

    #: 色调名 → 色板键。与芯片的 `state` 值对齐（none / ok / warn / danger）。
    TONES = {"none": "muted2", "ok": "ok", "warn": "warn", "danger": "danger"}

    def __init__(self, tone: str = "none", parent=None):
        super().__init__(parent)
        self.setObjectName("ChipCaret")
        self.setFixedSize(self.SIZE, self.SIZE)
        # 它只是个记号，点击该由芯片承担（跟 RadioDot 同样的理由）。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._tone = tone
        self._mode = "light"

    def set_tone(self, tone: str) -> None:
        if tone == self._tone:
            return
        self._tone = tone
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def paintEvent(self, event):        # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        try:
            colors = theme.palette(self._mode)
            color = QColor(colors[self.TONES.get(self._tone, "muted2")])
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(color)
            pen.setWidthF(self.PEN)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            # 折角整体垂直居中：左肩 → 顶点 → 右肩。
            center_x = self.SIZE / 2.0
            top_y = (self.SIZE - self.DROP) / 2.0
            painter.drawPolyline(QPolygonF([
                QPointF(center_x - self.HALF, top_y),
                QPointF(center_x, top_y + self.DROP),
                QPointF(center_x + self.HALF, top_y),
            ]))
        except Exception as exc:                     # noqa: BLE001
            logger.warning(f"展开指示绘制失败：{exc}")
        finally:
            painter.end()
