"""左侧日志列表的绘制测试：文件名靠左、**时间靠右**（v6.9.7）。

判据只用像素：这版的意义就是"看起来对齐"，纯函数证明不了。
"""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"      # 必须在导入 PyQt5 之前

import sys                                        # noqa: E402
from pathlib import Path                          # noqa: E402

import pytest                                     # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtCore import QRect, QSize                                 # noqa: E402
from PyQt5.QtGui import QColor, QFontMetrics, QImage, QPainter           # noqa: E402
from PyQt5.QtWidgets import (QApplication, QListWidget, QListWidgetItem,   # noqa: E402
                             QStyle, QStyleOptionViewItem)

from data_collector import theme                                        # noqa: E402
from data_collector.ui import log_row_delegate                          # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.configure_scale(1.0)
    return app


def _row(name, stamp="10-29 09:33:12"):
    return f"{name}{log_row_delegate.SEPARATOR}{stamp}"


def _list(rows, width=520, height=200):
    widget = QListWidget()
    widget.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
    widget.setItemDelegate(log_row_delegate.LogRowDelegate(widget))
    for text in rows:
        widget.addItem(QListWidgetItem(text))
    widget.resize(width, height)
    widget.show()
    QApplication.processEvents()
    return widget


def _item_width(widget):
    return widget.visualRect(widget.model().index(0, 0)).width()


def _option(widget, row=0):
    option = QStyleOptionViewItem()
    option.font = widget.font()          # 视图是这样把字体交给 delegate 的
    widget.itemDelegate().initStyleOption(option, widget.model().index(row, 0))
    option.widget = widget
    return option


def _render(widget, row, height=40):
    width = _item_width(widget)
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor(theme.palette(theme.active_mode())["surface"]))
    painter = QPainter(image)
    try:
        option = _option(widget, row)
        option.rect = QRect(0, 0, width, height)
        widget.itemDelegate().paint(painter, option, widget.model().index(row, 0))
    finally:
        painter.end()
    return image


def _ink_columns(image):
    """逐列判断"有没有字"（与主题底色的 RGB 不同即算）。"""
    background = QColor(theme.palette(theme.active_mode())["surface"])
    hits = []
    for x in range(image.width()):
        for y in range(image.height()):
            if image.pixelColor(x, y).rgb() != background.rgb():
                hits.append(x)
                break
    return hits


# ---------- 核心：时间贴右边缘 ----------

def test_time_right_edge_is_the_same_for_any_file_name_length(qapp):
    """文件名长短不同，**时间的右边缘必须一致**（这就是"时间从右开始排"）。

    旧实现是 `文件名 + 四个空格 + 时间` 拼一串，名字一长，时间就整体右移；
    一屏看下来时间是斜的（和「数据文件夹」列表当初同一个毛病）。
    """
    rows = [_row("ui_error.log"),
            _row("teleop_20260922_134400.log"),
            _row("robot_monitor.log")]
    widget = _list(rows)
    right_ink = []
    for row in range(len(rows)):
        hits = _ink_columns(_render(widget, row))
        if not hits:
            pytest.skip("此环境画不出文字（offscreen 也不渲染），像素校验无从进行")
        right_ink.append(hits[-1])

    # 最后一列墨就是时间最后一个字符的右边缘：字形右侧留白 ≤2px
    assert max(right_ink) - min(right_ink) <= 2, f"时间右边缘不一致：{right_ink}"


def test_time_is_fully_drawn_not_truncated(qapp):
    """时间**不许被省略/截断**：它是这一列的意义，完整的 `MM-DD HH:MM:SS` 必须画得下。"""
    stamp = "10-29 09:33:12"
    widget = _list([_row("teleop_20260922_134400.log", stamp=stamp)])
    option = _option(widget)
    metrics = QFontMetrics(option.font)
    stamp_w = metrics.horizontalAdvance(stamp)
    hpad = theme.px(log_row_delegate.HPAD)

    hits = _ink_columns(_render(widget, 0))
    if not hits:
        pytest.skip("此环境画不出文字（offscreen 也不渲染），像素校验无从进行")

    expected_right = _item_width(widget) - hpad
    assert abs(hits[-1] - expected_right) <= 3, (
        f"时间右边缘落在 {hits[-1]}，应贴住 {expected_right}")
    # 时间那段墨的宽度要接近完整时间戳的宽度（被截断会明显变窄）
    time_hits = [x for x in hits if x >= expected_right - stamp_w - 3]
    assert max(time_hits) - min(time_hits) >= stamp_w - 8, (
        f"时间被截断了：实测墨宽 {max(time_hits) - min(time_hits)}，需要 {stamp_w}")


def test_paint_uses_the_font_it_draws_with(qapp):
    """画字必须用自己的字体（`painter.setFont(opt.font)`）—— v6.9.6 隔壁列表踩过的坑。"""
    widget = _list([_row("ui_error.log")])
    option = _option(widget)
    option.rect = QRect(0, 0, _item_width(widget), 40)

    image = QImage(option.rect.width(), 40, QImage.Format_ARGB32)
    painter = QPainter(image)
    try:
        widget.itemDelegate()._paint(painter, option, widget.model().index(0, 0))
        used = painter.font().key()
    finally:
        painter.end()

    assert used == option.font.key(), (
        f"画字用的是 {used}，量宽用的是 {option.font.key()}（见 painter.setFont）")


def test_row_height_meets_the_touch_minimum(qapp):
    widget = _list([_row("ui_error.log")])
    option = _option(widget)
    size = widget.itemDelegate().sizeHint(option, widget.model().index(0, 0))
    assert isinstance(size, QSize)
    assert size.height() >= theme.px(log_row_delegate.MIN_ROW_H), size.height()


# ---------- 健壮性 ----------

def test_split_row_tolerates_a_row_without_a_stamp():
    assert log_row_delegate.split_row("只有文件名.log") == ("只有文件名.log", "")
    assert log_row_delegate.split_row("") == ("", "")
    assert log_row_delegate.split_row(_row("a.log", "10-29 09:33:12")) == \
        ("a.log", "10-29 09:33:12")


def test_paint_survives_a_row_without_separator(qapp):
    """人为塞一段没有分隔符的字也要画得出来（不能把整列表拖崩）。"""
    widget = _list(["随便一段没有分隔符的字"])
    image = _render(widget, 0)
    assert image.width() == _item_width(widget)
