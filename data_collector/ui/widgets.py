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
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygon
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
    """13×13 状态图标：圆点 / 对勾 / 警示三角（导出目标芯片用）。

    ## 为什么自绘而不是用字符或图片

    - **字符（● √ !）**：不同平台字体里字形与基线都不一样，深色主题下
      尤其容易糊；而且警示三角没有能对齐的等宽字符。
    - **图片资源**：换深色主题就要重备一套（浅色图标在深底上看不清），
      而这个项目的主题是**可切换**的，为三个小图形背两套资源不值得。
    - 项目里已有 `BatteryIndicator` 这个自绘状态控件的先例，三条纪律照它走：
      取色必须在 try 内、异常绝不外放、`finally: painter.end()`
      （paintEvent 中途退出会让 QPainter 来不及 end()，进程以段错误收场）。

    几何：绘制区域比控件尺寸**内缩 1px**。QPainter 的描边以路径为中心向两侧各扩
    pen/2，画到边界会被控件裁掉 —— 现场看到的就是"图标缺一块"。
    """

    ICON = 13
    PEN = 2

    #: 三种形态。名字与 `theme.set_state` 的取值对齐（none/ok/warn）。
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
            kind = self._kind
            if kind == "ok":
                self._paint_check(painter, QColor(colors["ok"]))
            elif kind in ("warn", "danger"):
                self._paint_warn(painter, QColor(colors["warn" if kind == "warn" else "danger"]))
            else:
                self._paint_dot(painter, QColor(colors["muted2"]))
        except Exception as exc:                     # noqa: BLE001
            # 同 BatteryIndicator：自绘控件的异常绝不能从 paintEvent 漏出去。
            logger.warning(f"状态图标绘制失败：{exc}")
        finally:
            painter.end()

    def _ink(self, painter: QPainter, color: QColor, width: int) -> None:
        pen = QPen(color)
        pen.setWidth(width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

    def _paint_dot(self, painter: QPainter, color: QColor) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRect(3, 3, 7, 7))       # 内缩，留出抗锯齿余量

    def _paint_check(self, painter: QPainter, color: QColor) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._ink(painter, color, self.PEN)
        painter.drawPolyline(QPolygon([
            QPoint(2, 7), QPoint(5, 10), QPoint(11, 3),
        ]))

    def _paint_warn(self, painter: QPainter, color: QColor) -> None:
        """警示三角（圆圈 + 感叹号）。

        用**圆圈**而不是真三角：Qt 画多边形要自己算三个顶点，描边又必然溢出边界，
        而圆圈 + 两条直线在 13px 上稳得多，也和圆点同族（视觉上一套）。
        """
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._ink(painter, color, 2)
        # 圆心在 (6.5,6.5)，半径 4 → QRect(3,3,8,8)。描边以路径为中心向两侧各扩1px，
        # 所以正好落在 1..12 的框内；用 QRect(2,2,9,9) 会顶到边界被裁，实拍看是扁的。
        painter.drawEllipse(QRect(3, 3, 8, 8))
        # 坐标**必须取整**：`QPoint` 只接受 int，传 6.5 会抛 TypeError。
        # 而这里抛异常的后果不是少画一根线 —— 是整个 paintEvent 中断，
        # 三种图标**一块都画不出来**（实测踩过，日志里只有一句"绘制失败"）。
        painter.drawLine(QPoint(6, 5), QPoint(6, 8))
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRect(6, 10, 1, 1))     # 感叹号的点
