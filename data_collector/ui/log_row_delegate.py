"""左侧「日志」列表的两列绘制：**文件名靠左、时间靠右**（v6.9.7）。

## 为什么也要自绘

日志行原来是 `f"{文件名}    {时间}"` 拼成一串，靠四个空格对齐 —— 而文件名长短不一
（`teleop_20260922_134400.log` 与 `ui_error.log` 差十几个字符），时间列的起点就跟着漂，
一屏看下来时间是斜的。和「数据文件夹」列表当初的问题一模一样。

现场要求：**时间从右开始排**（时间贴着右边缘），左边全留给文件名。

## 与「数据文件夹」列表的差别

那个列表有三列（名字/时间/判定），列位要从右边推算；这里只有两列，
所以更简单：时间用 `AlignRight` 画在右边缘，文件名占剩下的宽度并按需省略。
**时间不省略**（它是这一列的意义），文件名可以省略（它只是标识，且日志通常按时间看）。

## 🔴 两条必须遵守的（都是踩过的）

1. **`painter.setFont(opt.font)`**：自绘时 painter 用的是调用方给的字体，而宽度是按
   `opt.font` 量的 —— 两者同机能差 20%，文字就会溢出/被切（v6.9.6 在隔壁列表踩过）。
2. **QPainter 走 try/finally**：中途抛异常来不及 `end()`，整个列表可能画不出来。
"""
from PyQt5.QtCore import QRect, QSize, Qt
from PyQt5.QtGui import QColor, QFontMetrics, QPen
from PyQt5.QtWidgets import (QApplication, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem)

from .. import theme
from ..core import logger
from .bag_row_delegate import GAP, HPAD, MIN_ROW_H, VPAD

#: 两段之间的分隔（与 `data_view` 里拼日志行的写法一致）。
SEPARATOR = "    "


def split_row(text: str):
    """把 `文件名␣␣␣␣时间` 拆成 `(文件名, 时间)`。

    名字里不含四个连续空格（都是 `xxx.log`），所以按**最后一个**分隔符拆最稳：
    万一将来文件名里出现空格，也不会把时间拆坏。
    """
    raw = str(text or "")
    if SEPARATOR in raw:
        name, stamp = raw.rsplit(SEPARATOR, 1)
        return name, stamp
    return raw, ""


class LogRowDelegate(QStyledItemDelegate):
    """左侧日志列表：文件名左对齐、时间右对齐。"""

    def paint(self, painter, option, index):  # noqa: N802 (Qt 命名)
        painter.save()
        try:
            self._paint(painter, option, index)
        except Exception as exc:                      # noqa: BLE001
            logger.warning(f"日志行绘制失败（已跳过这一行）：{exc}")
        finally:
            painter.restore()

    def _paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""                                  # 背景交样式画，不重复画字
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        name, stamp = split_row(text)
        # 量宽与画字必须是同一套字体（v6.9.6 隔壁列表的真根因）
        painter.setFont(opt.font)
        metrics = QFontMetrics(opt.font)
        painter.setPen(QPen(QColor(theme.palette(theme.active_mode())["text"])))

        rect = opt.rect
        hpad, gap = theme.px(HPAD), theme.px(GAP)
        stamp_w = metrics.horizontalAdvance(stamp) if stamp else 0

        # 时间：贴右边缘、不省略（宽度就是它自己的宽度）
        if stamp:
            painter.drawText(
                QRect(rect.right() - hpad - stamp_w, rect.y(), stamp_w, rect.height()),
                int(Qt.AlignRight | Qt.AlignVCenter), stamp)

        # 文件名：占满其余宽度，放不下才省略
        name_w = max(rect.width() - 2 * hpad - (stamp_w + gap if stamp else 0), 0)
        if name_w > 0 and name:
            painter.drawText(
                QRect(rect.left() + hpad, rect.y(), name_w, rect.height()),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                metrics.elidedText(name, Qt.ElideRight, name_w))

    def sizeHint(self, option, index):  # noqa: N802 (Qt 命名)
        """行高：字体高 + 内边距，且不低于触摸门槛。"""
        height = QFontMetrics(option.font).height() + 2 * theme.px(VPAD)
        return QSize(0, max(height, theme.px(MIN_ROW_H)))
