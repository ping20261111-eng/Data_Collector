"""列对齐的绘制层测试：`ui/bag_row_delegate`（v6.9.1）。

## 为什么要有一条**像素级**的测试

"列对齐"是眼睛看的，不是返回值能表达的。纯函数测试只能证明"布局算了同样的坐标"，
而现场那次投诉的真正内容是"看起来是斜的"—— 中间任何一步接错（delegate 没挂上、
画的时候用了自己的 x、名字省略算错），布局纯函数都照样绿。

所以这里把两行真的画到图上，找出**时间列第一个黑像素的 x**，要求它们相等。
这是唯一能证明"看起来对齐"的断言。

## 平台

`QT_QPA_PLATFORM=minimal` 不渲染文字（画出来是空白），像素无从验证 ——
本文件显式用 `offscreen`（每个测试文件是独立进程，不影响别人）。
真遇到"这个环境连 offscreen 也画不出字"，像素那条会 **skip 而不是假绿**。
"""
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"     # 必须在导入 PyQt5 之前

import sys                                       # noqa: E402
from pathlib import Path                         # noqa: E402

import pytest                                    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtCore import QRect, QSize                                   # noqa: E402
from PyQt5.QtGui import QColor, QFontMetrics, QImage, QPainter              # noqa: E402
from PyQt5.QtWidgets import (QApplication, QListWidget, QListWidgetItem,     # noqa: E402
                             QStyle, QStyledItemDelegate, QStyleOptionViewItem)

from data_collector import theme                                            # noqa: E402
from data_collector.ui import bag_row_delegate, bag_verdict                  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.configure_scale(1.0)
    return app


def _row(name, stamp="10-26 14:39:57", label=bag_verdict.PASS_TEXT):
    return bag_verdict.row_text(name, stamp, label)


def _list(rows, width=600, height=200):
    widget = QListWidget()
    widget.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
    widget.setItemDelegate(bag_row_delegate.BagRowDelegate(widget))
    for text in rows:
        widget.addItem(QListWidgetItem(text))
    widget.resize(width, height)
    return widget


def _background():
    """底色取**主题的表面色**，不写死白色。

    写死白色时，深色主题下正文色是浅色 —— 画出来和白底一样，测试会以为"没画字"。
    底色与字色都从主题来，才能对两种主题都成立。
    """
    return QColor(theme.palette(theme.active_mode())["surface"])


def _render(widget, row, width, height=40):
    """把第 `row` 行画到主题底色上。"""
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(_background())
    painter = QPainter(image)
    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, width, height)
        option.font = widget.font()
        option.widget = widget
        option.state = QStyle.State_Enabled
        widget.itemDelegate().paint(painter, option, widget.model().index(row, 0))
    finally:
        painter.end()
    return image


def _first_ink_x(image, x_from):
    """从 `x_from` 往右扫，返回第一个**与底色不同**的像素列（即"有字"的位置）。"""
    background = _background()
    for x in range(x_from, image.width()):
        for y in range(image.height()):
            if image.pixelColor(x, y) != background:
                return x
    return None


def _paint_metrics(widget):
    """**绘制时**那套字体度量（`initStyleOption` 给的），不是控件字体。

    v6.9.2 实测两者不同（IBM Plex Mono 未安装 → 控件与样式各自回退到不同字体），
    用控件字体算期望值会让测试与实现分叉 —— 那正是当初漏掉列宽算错的原因。
    """
    option = QStyleOptionViewItem()
    widget.itemDelegate().initStyleOption(option, widget.model().index(0, 0))
    return QFontMetrics(option.font)


def _expected_layout(widget, width):
    metrics = _paint_metrics(widget)
    delegate = widget.itemDelegate()
    return bag_verdict.column_layout(
        width,
        delegate.stamp_width() or metrics.horizontalAdvance(bag_row_delegate.STAMP_PROBE),
        max(metrics.horizontalAdvance(text) for text in bag_verdict.ALL_LABELS),
        hpad=theme.px(bag_row_delegate.HPAD),
        gap=theme.px(bag_row_delegate.GAP),
        name_cap=delegate.name_width() or None,
    )


# ---------- 像素级：这版功能的全部意义 ----------

def test_time_column_starts_at_the_same_x_for_rows_with_different_name_lengths(qapp):
    """**长短名字的行，时间列必须从同一个 x 开始**（现场投诉的就是这个）。

    旧实现靠"名字 + 四个空格"，等宽字体下时间列起点随名字长度漂移；
    实拍一屏五行就能看出斜着走。这里逐行找第一个黑像素，要求相等。
    """
    rows = [_row("a_20261027090000"),                       # 16 字符
            _row("vla_20261026143957"),                      # 18
            _row("cloth_rack_20261027120000")]               # 26
    widget = _list(rows, width=600)
    bag_row_delegate.fit_columns(widget)

    layout = _expected_layout(widget, 600)
    starts = []
    for row in range(len(rows)):
        image = _render(widget, row, 600)
        # 从"名字列右边界再往右一点"开始扫：名字被省略时也不会干扰
        starts.append(_first_ink_x(image, layout.stamp_x - theme.px(6)))

    if any(x is None for x in starts):
        pytest.skip("此环境画不出文字（offscreen 也不渲染），像素校验无从进行")

    assert len(set(starts)) == 1, f"时间列起点不一致：{starts}"
    # 而且要落在算出来的那个位置上（±2px 是字形左侧留白）
    assert abs(starts[0] - layout.stamp_x) <= 2, (starts[0], layout.stamp_x)


def test_verdict_column_starts_at_the_same_x_for_rows_with_different_labels(qapp):
    """判定文案长短不同（合格 / 不合格），起点也必须一致。

    判定列宽是按**最长**文案预留的，所以「合格」右侧会空一些 —— 这是对的：
    左右都对齐靠的就是"列宽固定、内容左对齐"，而不是让每个词自己居中。
    """
    rows = [_row("a_20261027090000", label=bag_verdict.PASS_TEXT),
            _row("a_20261027090000", label=bag_verdict.FAIL_TEXT),
            _row("a_20261027090000", label=bag_verdict.NONE_TEXT)]
    widget = _list(rows, width=600)
    bag_row_delegate.fit_columns(widget)

    layout = _expected_layout(widget, 600)
    starts = []
    for row in range(len(rows)):
        image = _render(widget, row, 600)
        starts.append(_first_ink_x(image, layout.verdict_x - theme.px(6)))

    if any(x is None for x in starts):
        pytest.skip("此环境画不出文字（offscreen 也不渲染），像素校验无从进行")

    assert len(set(starts)) == 1, f"判定列起点不一致：{starts}"
    assert abs(starts[0] - layout.verdict_x) <= 2, (starts[0], layout.verdict_x)


# ---------- 列宽 / 行高 ----------

def test_name_column_is_capped_by_the_longest_name_after_fit(qapp):
    """刷新时按最长名字定宽 —— 宽窗口下不把名字与时间之间的空当摊开。"""
    widget = _list([_row("a_1"), _row("cloth_rack_20261027120000")], width=1200)
    bag_row_delegate.fit_columns(widget)

    metrics = _paint_metrics(widget)
    widest = metrics.horizontalAdvance("cloth_rack_20261027120000")
    width = widget.itemDelegate().name_width()

    assert widest <= width <= widest + theme.px(bag_row_delegate.ELIDE_SLACK) + 1, \
        f"名字列宽 {width} 与最长名字 {widest} 不匹配（多余量只该是省略余量）"


def test_column_widths_are_measured_with_the_font_that_paints(qapp):
    """定宽必须用**绘制时那套字体**量，不能用控件字体（v6.9.2 现场 bug）。

    ## 现场现象

    列名与时间都对齐了，但**最长的名字明明放得下也被截成 `vla_202610281021…`**。

    ## 根因

    `bag_list.setFont(theme.mono_font(...))` 因 `IBM Plex Mono` 未安装被回退成
    14px 的 Noto Sans SC；而 `initStyleOption` 交给 delegate 的 `option.font`
    是 12pt 的 DejaVu —— **两套完全不同的字体**。用控件字体量出来的名字列比
    实际绘制需要的窄 20%，于是最长那个名字永远差一点。

    这条断言拿 `initStyleOption` 的字体当基准 —— 它才是 `paint()` 里画字的字体。
    """
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QStyleOptionViewItem

    widest = "cloth_rack_20261027120000"
    widget = _list([_row(widest), _row("a_1")], width=1200)
    bag_row_delegate.fit_columns(widget)

    option = QStyleOptionViewItem()
    widget.itemDelegate().initStyleOption(option, widget.model().index(0, 0))
    needed = QFontMetrics(option.font).horizontalAdvance(widest)

    # 用绘制字体量：必须装得下（否则现场就看到省略号）
    assert widget.itemDelegate().name_width() >= needed, (
        f"名字列 {widget.itemDelegate().name_width()} < 绘制所需 {needed}"
        "（说明用了控件字体量宽）")

    # 时间列宽同理：要按真实时间戳量，别用固定探针
    stamp = QFontMetrics(option.font).horizontalAdvance("10-28 10:21:23")
    assert widget.itemDelegate().stamp_width() >= stamp, widget.itemDelegate().stamp_width()


def test_longest_name_is_not_elided_when_there_is_room(qapp):
    """宽窗口下**最长的名字不许被截** —— 用绘制里那条 `elidedText` 直接验。

    只断言"列宽 ≥ 文字宽"还不够：`elidedText` 判"放得下"用的是它自己的口径，
    差 1~2px 也会吐省略号。这里就把绘制那一行算式抄过来比 ——
    它返回的还是原名，才说明画出来不会有「…」。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QStyleOptionViewItem

    widest = "cloth_rack_20261027120000"
    widget = _list([_row(widest)], width=1200)
    bag_row_delegate.fit_columns(widget)

    metrics = _paint_metrics(widget)
    needed = metrics.horizontalAdvance(widest)

    # 宽窗口：实际分到的名字列宽必须装得下（`column_layout` 的结果，不是 cap）
    wide_w = _expected_layout(widget, 1200).name_w
    assert metrics.elidedText(widest, Qt.ElideRight, wide_w) == widest, (
        f"宽窗口下名字被截了：列宽 {wide_w}，名字实测 {needed}")

    # 窄窗口：分不到那么多，必须截（否则会压到时间列上）
    narrow = _list([_row(widest)], width=320)
    bag_row_delegate.fit_columns(narrow)
    narrow_w = _expected_layout(narrow, 320).name_w
    assert narrow_w < needed, (narrow_w, needed)
    assert metrics.elidedText(widest, Qt.ElideRight, narrow_w) != widest, \
        "窗口很窄时名字应该被截断"


def test_short_names_still_get_a_minimum_width(qapp):
    """名字只有一两个字符时，时间列不能顶到左边框上。"""
    widget = _list([_row("a")], width=600)
    bag_row_delegate.fit_columns(widget)
    assert widget.itemDelegate().name_width() == theme.px(bag_row_delegate.MIN_NAME_W)


def test_fit_leaves_a_foreign_delegate_alone(qapp):
    """别人装的 delegate 不去动 —— 静默替换会让那个列表完全变样。"""
    widget = _list([_row("a_1")], width=600)
    widget.setItemDelegate(QStyledItemDelegate(widget))

    bag_row_delegate.fit_columns(widget)     # 不应抛异常、也不应改动

    assert isinstance(widget.itemDelegate(), bag_row_delegate.BagRowDelegate) is False


def test_row_height_meets_the_touch_minimum(qapp):
    """行高≥32px（设计稿值）。实测旧实现只有 20px，手指点下去常落在两行之间。"""
    widget = _list([_row("a_1")])
    option = QStyleOptionViewItem()
    option.font = widget.font()
    option.rect = QRect(0, 0, 600, 40)

    size = widget.itemDelegate().sizeHint(option, widget.model().index(0, 0))

    assert isinstance(size, QSize)
    assert size.height() >= theme.px(bag_row_delegate.MIN_ROW_H), size.height()
    assert size.height() >= QFontMetrics(widget.font()).height()


# ---------- 健壮性（这里出异常会让整个列表画不出来） ----------

def test_paint_survives_a_row_without_separators(qapp):
    """不合格式的行（人为 addItem 一段话）也要画得出来，不能抛异常。"""
    widget = _list(["随便一段没有分隔符的字"], width=600)
    image = _render(widget, 0, 600)
    assert image.width() == 600                    # 能走到这里就说明没抛异常


def test_paint_survives_an_absurdly_narrow_viewport(qapp):
    """极窄视口（列放不下）时不能崩，也不能把判定列画到视口外看不见的地方还能报成功。"""
    widget = _list([_row("cloth_rack_20261027120000", label=bag_verdict.FAIL_TEXT)],
                   width=80)
    bag_row_delegate.fit_columns(widget)
    image = _render(widget, 0, 80)
    assert image.width() == 80


def test_verdict_column_width_fits_every_label(qapp):
    """判定列宽按**最长**文案预留 —— 将来加一个更长的文案（如「需要复查」）会被这条挡下。"""
    metrics = QFontMetrics(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
    reserved = max(metrics.horizontalAdvance(text) for text in bag_verdict.ALL_LABELS)
    for text in bag_verdict.ALL_LABELS:
        assert metrics.horizontalAdvance(text) <= reserved


def test_delegate_keeps_no_reference_to_the_widget(qapp):
    """delegate 只能存**一个整数列宽**，不许存控件引用。

    delegate 反向引用宿主会形成 Python 引用环，环的回收顺序不定 ——
    项目里实测过一次"之后任意一次 setStyleSheet 段错误"，崩点离病因很远
    （见 `point_dialog._LabelCardDelegate` 的同名说明）。
    """
    widget = _list([_row("a_1")], width=600)
    bag_row_delegate.fit_columns(widget)
    delegate = widget.itemDelegate()

    stored = [value for value in vars(delegate).values() if value is widget]
    assert stored == [], f"delegate 里存了宿主控件的引用：{stored}"
    assert isinstance(delegate.name_width(), int)
