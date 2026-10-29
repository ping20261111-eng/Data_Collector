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
    # 真显示一次：列表行宽（visualRect）由视图布局决定，不 show 的话
    # viewport 会停在默认 640 —— 期望值就会与 delegate 实际用的行宽不一致。
    widget.show()
    QApplication.processEvents()
    return widget


def _background():
    """底色取**主题的表面色**，不写死白色。

    写死白色时，深色主题下正文色是浅色 —— 画出来和白底一样，测试会以为"没画字"。
    底色与字色都从主题来，才能对两种主题都成立。
    """
    return QColor(theme.palette(theme.active_mode())["surface"])


def _render(widget, row, height=40, painter_font=None):
    """把第 `row` 行画到主题底色上（宽度 = 控件里真实的行宽）。

    `painter_font` 用来**故意给 painter 一套不相干的字体**：自绘代码必须自己把
    `painter.setFont()` 设成量宽那套，不能指望调用方给对（现场就是这么错的）。
    """
    width = _item_width(widget)
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(_background())
    painter = QPainter(image)
    if painter_font is not None:
        painter.setFont(painter_font)
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


def _first_ink_x(image, x_from, color=None):
    """从 `x_from` 往右扫，返回第一个"有字"的像素列。

    默认判据是"与底色不同"；给定 `color` 时判"等于这个字色"——
    不合格行有淡红底，整行的底色都不等于主题底色，按底色判会把底纹当成字。
    """
    background = _background()
    target = color.name() if color is not None else None
    for x in range(x_from, image.width()):
        for y in range(image.height()):
            pixel = image.pixelColor(x, y)
            hit = (pixel.name() == target) if target else (pixel != background)
            if hit:
                return x
    return None


def _paint_option(widget, index=0):
    """**视图真正传给 delegate 的那个 option**。

    关键细节：视图是拿自己已经填好的 option（`font` = 控件字体）交给 delegate，
    而 `QStyledItemDelegate.initStyleOption` 只在 item 设了 `FontRole` 时才改字体 ——
    所以"新建一个空 `QStyleOptionViewItem` 再 init"得到的是 `QApplication` 的默认字体
    （DejaVu 12pt），**不是绘制时那套**。v6.9.6 才发现测试与实现一直各自量各自的字体，
    表现就是"实现修好了、测试却红"。
    """
    option = QStyleOptionViewItem()
    option.font = widget.font()
    widget.itemDelegate().initStyleOption(option, widget.model().index(index, 0))
    option.widget = widget
    return option


def _paint_metrics(widget):
    """**绘制时**那套字体度量，与 `_paint_option` 同源。

    v6.9.2 实测控件字体与样式字体不同（IBM Plex Mono 未安装 → 各自回退），
    用错一套量宽，名字列就会偏窄/偏宽。测试的度量基准必须与实现同源。
    """
    return QFontMetrics(_paint_option(widget).font)


def _expected_layout(widget):
    """按**控件真实的 item 宽度**算列位 —— 与 delegate 绘制时用的是同一个数。

    ⚠️ 不能拿"请求的窗口宽度"算期望值：`initStyleOption` 会用视图里的真实
    `visualRect` 覆盖 delegate 收到的 rect。两者不一致时测试就会假红
    （v6.9.6 改成"名字列吃剩余宽度"后，列位开始依赖行宽，这个坑才暴露出来）。
    """
    metrics = _paint_metrics(widget)
    delegate = widget.itemDelegate()
    # 先让 delegate 按"画这一刻的字体"核对一遍（与 paint 里的第一步一致），
    # 否则拿 fit 时期的旧宽度算期望值会比实际绘制窄/宽。
    delegate.sizeHint(_paint_option(widget), widget.model().index(0, 0))
    return bag_verdict.column_layout(
        _item_width(widget),
        delegate.stamp_width() or metrics.horizontalAdvance(bag_row_delegate.STAMP_PROBE),
        max(metrics.horizontalAdvance(text) for text in bag_verdict.ALL_LABELS),
        hpad=theme.px(bag_row_delegate.HPAD),
        gap=theme.px(bag_row_delegate.GAP),
        name_w=delegate.name_width() or None,
    )


def _item_width(widget) -> int:
    """控件里一行的实际宽度（列表在 ListMode 下每行都是这个宽度）。"""
    return widget.visualRect(widget.model().index(0, 0)).width()


def _has_color(image, x0, x1, expected: QColor) -> bool:
    """在 `[x0, x1)` 这段横向范围里找**完全等于** `expected` 的像素。

    抗锯齿会让字形边缘变成混合色，但笔画内部仍是纯色 —— 判定一列的几个字里
    总有足够多的"芯"像素。用精确相等而不是"接近"，是为了避免两种语义色
    （红/绿）互相被误认。
    """
    target = expected.name()
    for x in range(max(int(x0), 0), min(int(x1), image.width())):
        for y in range(image.height()):
            if image.pixelColor(x, y).name() == target:
                return True
    return False


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

    layout = _expected_layout(widget)
    starts = []
    for row in range(len(rows)):
        image = _render(widget, row)
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

    layout = _expected_layout(widget)
    starts = []
    for row, label in enumerate((bag_verdict.PASS_TEXT, bag_verdict.FAIL_TEXT,
                                 bag_verdict.NONE_TEXT)):
        image = _render(widget, row)
        # 按**该行判定文字的颜色**扫：不合格行整行有淡红底，按"非底色"判会误把底纹当字
        color = bag_verdict.column_color(bag_verdict.state(label),
                                         bag_verdict.COLUMN_VERDICT)
        starts.append(_first_ink_x(image, layout.verdict_x - theme.px(6), color))

    if any(x is None for x in starts):
        pytest.skip("此环境画不出文字（offscreen 也不渲染），像素校验无从进行")

    # 容差 4px：汉字各自的左侧留白（bearing）不同，「未」比「合」靠左 1px 是字形，
    # 不是列位。列位错了会差出几个字宽，那才是这条要抓的。
    assert max(starts) - min(starts) <= 4, f"判定列起点不一致：{starts}"
    assert 0 <= starts[0] - layout.verdict_x <= 4, (starts[0], layout.verdict_x)


# ---------- 列宽 / 行高 ----------

def test_name_column_is_capped_by_the_longest_name_after_fit(qapp):
    """名字列的"自然宽度" = 最长名字 + 省略余量（再小就会被截）。

    `fit_columns` 量与绘制可能不是同一套字体（现场就是），所以这里先让 delegate
    按绘制字体核对一遍（`sizeHint` 里的 `_ensure_widths`），再比对 —— 那才是
    真正生效的那个宽度。
    """
    widget = _list([_row("a_1"), _row("cloth_rack_20261027120000")], width=1200)
    bag_row_delegate.fit_columns(widget)
    widget.itemDelegate().sizeHint(_paint_option(widget), widget.model().index(0, 0))

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


def test_longest_name_is_not_elided_at_any_viewport_width(qapp):
    """**任何宽度下名字都不许被截**（v6.9.5 现场要求：名称要显示全部）。

    改口记录：v6.9.2 那版在窄窗口里会压缩名字列并吐省略号 ——
    现场截图里三行都显示成 `vla_202610…`，**彼此无法区分**，等于这一列没信息。
    现在名字列按最长名字的自然宽度定死，装不下就横向滚动。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QStyleOptionViewItem

    widest = "cloth_rack_20261027120000"
    metrics = QFontMetrics(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
    needed = _paint_metrics(_list([_row(widest)])).horizontalAdvance(widest)

    for viewport in (1200, 600, 320, 80):
        widget = _list([_row(widest)], width=viewport)
        bag_row_delegate.fit_columns(widget)
        layout = _expected_layout(widget)

        assert metrics.elidedText(widest, Qt.ElideRight, layout.name_w) == widest, (
            f"视口 {viewport} 下名字被截了：列宽 {layout.name_w}，名字实测 {needed}")

        # 装不下时必须**变宽**（视图据此出横向滚动条），而不是截断
        option = QStyleOptionViewItem()
        widget.itemDelegate().initStyleOption(option, widget.model().index(0, 0))
        option.widget = widget
        hint = widget.itemDelegate().sizeHint(option, widget.model().index(0, 0))
        assert hint.width() >= layout.name_w + 2 * theme.px(bag_row_delegate.HPAD), (
            f"视口 {viewport}: sizeHint 宽 {hint.width()} 装不下名字列 "
            f"{layout.name_w}，名字会被裁掉")


def test_content_wider_than_viewport_produces_a_horizontal_scrollbar(qapp):
    """窄窗口里应当是**能横向滚动看到整行**，而不是把内容裁掉。"""
    widget = _list([_row("cloth_rack_20261027120000")], width=240)
    bag_row_delegate.fit_columns(widget)
    widget.show()
    qapp.processEvents()

    assert widget.horizontalScrollBar().maximum() > 0, (
        "内容比视口宽时应当出现横向滚动条（否则右侧的时间/判定列直接看不到）")


def test_stamp_column_has_room_for_seconds(qapp):
    """时间列必须按**带秒**的时间戳量宽（现场要求时间精确到秒）。

    只断言"列宽>0"不够：`mm-dd HH:MM` 也大于 0，但画出来就是 `10-28 18` 这种
    半截时间 —— 现场截图上出现的正是这个。
    """
    stamp = "10-27 12:00:00"
    widget = _list([_row("a_20261027090000", stamp=stamp)], width=600)
    bag_row_delegate.fit_columns(widget)

    metrics = _paint_metrics(widget)
    with_seconds = metrics.horizontalAdvance(stamp)
    without = metrics.horizontalAdvance("10-27 12:00")

    assert widget.itemDelegate().stamp_width() >= with_seconds, (
        f"时间列 {widget.itemDelegate().stamp_width()} < 带秒时间戳 {with_seconds}")
    assert with_seconds > without, "夹具本身不成立：带秒的时间戳并不比不带秒的宽"


def test_widths_are_remeasured_when_the_paint_font_changes(qapp):
    """绘制字体变了就必须重量（v6.9.6 现场 bug 的自愈点）。

    ## 现场现象

    `rosbag2_20261027144…` 加 `04-12 07:47`（少了 `:xx`）—— 两列都窄了约 20%，
    正好是"另一套字体"的比例。

    ## 根因

    列宽是 `fit_columns()` 在**窗口显示前**量的一遍；之后样式 / DPI 把绘制字体
    换大了，宽度没跟着变 → 按旧宽度画就截。

    这里直接把绘制字体放大 1.5 倍，再让 delegate 走一遍"画之前核对"，要求它
    按新字体重算 —— 光断言"和旧值不同"不够，得能装下按新字体量出来的名字。
    """
    from PyQt5.QtGui import QFont

    widest = "rosbag2_20261027144500"
    widget = _list([_row(widest, stamp="10-26 18:16:54")], width=900)
    bag_row_delegate.fit_columns(widget)
    delegate = widget.itemDelegate()
    before = delegate.name_width()

    option = QStyleOptionViewItem()
    delegate.initStyleOption(option, widget.model().index(0, 0))
    bigger = QFont(option.font)
    bigger.setPointSizeF(max(bigger.pointSizeF(), 9.0) * 1.5)
    option.font = bigger
    option.widget = widget

    delegate._ensure_widths(option, widget.model().index(0, 0))

    needed = QFontMetrics(bigger).horizontalAdvance(widest)
    assert delegate.name_width() >= needed, (
        f"换了更大的绘制字体仍用旧宽度：{delegate.name_width()} < {needed}")
    assert delegate.name_width() > before, "字体变大后名字列宽没跟着变"


def test_paint_checks_the_font_before_drawing(qapp):
    """`paint` 里**必须**先核对字体 —— 上面那条只证明了"重量这件事是对的"，
    没证明"画之前真的会调它"。字体一变就省略号的现场 bug，缺的正是这一步。"""
    widget = _list([_row("rosbag2_20261027144500")], width=900)
    bag_row_delegate.fit_columns(widget)
    delegate = widget.itemDelegate()

    calls = []
    original = delegate._ensure_widths
    delegate._ensure_widths = lambda *args: (calls.append(args[1].row()),
                                             original(*args))[1]
    _render(widget, 0)
    assert calls and calls[0] == 0, f"paint 没有核对字体（调用记录 {calls}）"


def test_verdict_color_follows_the_three_states(qapp):
    """三档配色（v6.9.5）：合格绿 / 不合格红 / 未验证黑。

    用**像素**验，不是只看常量表：状态是从行文本第三段反推出来的，
    中间任何一步接错（把 label 传成 name、状态表 key 写错）纯函数测试都照样绿。
    """
    palette = theme.palette(theme.active_mode())
    ok_color = QColor(palette["ok_text"])
    bad_color = QColor(palette["danger_text"])
    plain = QColor(palette["text"])

    cases = [(bag_verdict.PASS_TEXT, ok_color),
             (bag_verdict.FAIL_TEXT, bad_color),
             (bag_verdict.NONE_TEXT, plain)]
    for text, expected in cases:
        widget = _list([_row("a_20261027090000", label=text)], width=600)
        bag_row_delegate.fit_columns(widget)
        layout = _expected_layout(widget)
        image = _render(widget, 0)

        assert _has_color(image, layout.verdict_x, layout.verdict_x + layout.verdict_w,
                          expected), (
            f"「{text}」的判定列没画成 {expected.name()}")

    # 反过来也要钉住：合格的判定列里**不许**出现红，不合格的里不许出现绿
    widget = _list([_row("a_20261027090000", label=bag_verdict.PASS_TEXT)], width=600)
    bag_row_delegate.fit_columns(widget)
    layout = _expected_layout(widget)
    image = _render(widget, 0)
    assert not _has_color(image, layout.verdict_x, layout.verdict_x + layout.verdict_w,
                          bad_color), "合格行里出现了红字"

    widget = _list([_row("a_20261027090000", label=bag_verdict.FAIL_TEXT)], width=600)
    bag_row_delegate.fit_columns(widget)
    layout = _expected_layout(widget)
    image = _render(widget, 0)
    assert not _has_color(image, layout.verdict_x, layout.verdict_x + layout.verdict_w,
                          ok_color), "不合格行里出现了绿字"


def test_failed_row_turns_the_name_red_but_leaves_the_time_neutral(qapp):
    """不合格：**名字**跟判定一起红，**时间**保持中性（设计要求）。

    时间列跟着红会让整行糊成一片红，而时间相对时间线时不该带判定含义。
    """
    palette = theme.palette(theme.active_mode())
    bad_color = QColor(palette["danger_text"])
    plain = QColor(palette["text"])

    widget = _list([_row("linetest_20261027103000", label=bag_verdict.FAIL_TEXT)],
                   width=600)
    bag_row_delegate.fit_columns(widget)
    layout = _expected_layout(widget)
    image = _render(widget, 0)

    assert _has_color(image, layout.name_x, layout.name_x + layout.name_w, bad_color), \
        "不合格行的名字没转红"
    assert _has_color(image, layout.stamp_x, layout.stamp_x + layout.stamp_w, plain), \
        "不合格行的时间列应当保持正文色（不应该跟着红）"
    assert not _has_color(image, layout.stamp_x, layout.stamp_x + layout.stamp_w,
                          bad_color), "时间列被染红了"


def test_failed_row_gets_a_tinted_background(qapp):
    """不合格行有一层淡红底（参考图里能看到），合格/未验证行不加。

    取列间距那一块空白采样：那里没有字，颜色就是行底。
    """
    background = _background()

    def gap_pixel(label, name="a_20261027090000"):
        widget = _list([_row(name, label=label)], width=600)
        bag_row_delegate.fit_columns(widget)
        layout = _expected_layout(widget)
        image = _render(widget, 0)
        x = layout.name_x + layout.name_w + theme.px(bag_row_delegate.GAP) // 2
        return image.pixelColor(x, 20)

    failed = gap_pixel(bag_verdict.FAIL_TEXT)
    passed = gap_pixel(bag_verdict.PASS_TEXT)

    assert passed.name() == background.name(), "合格行不该改行底"
    assert failed.name() != background.name(), "不合格行没有底纹"
    # 红底：红通道更高、蓝绿通道更低（别只比"不相等"，那样换成绿色底也能过）
    assert failed.red() >= failed.green() and failed.red() > failed.blue(), (
        f"底纹不是淡红：{failed.name()}")


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
    image = _render(widget, 0)
    assert image.width() == _item_width(widget)     # 能走到这里就说明没抛异常


def test_paint_survives_an_absurdly_narrow_viewport(qapp):
    """极窄视口（列放不下）时不能崩，而且**判定列不能落在行外**。

    v6.9.6 起窄窗口的处理是"整行变宽、横向滚动"，所以这里的判据是
    「判定列完整落在行内」，而不是"整行被压进 80px"。
    """
    widget = _list([_row("cloth_rack_20261027120000", label=bag_verdict.FAIL_TEXT)],
                   width=80)
    bag_row_delegate.fit_columns(widget)
    image = _render(widget, 0)
    layout = _expected_layout(widget)

    assert image.width() == _item_width(widget)
    assert layout.verdict_x + layout.verdict_w <= image.width(), (
        f"判定列画到行外了：{layout.verdict_x}+{layout.verdict_w} > {image.width()}")


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


def test_paint_leaves_the_painter_on_the_measuring_font(qapp):
    """自绘结束时 painter 的字体必须**就是 `opt.font`**（v6.9.6 现场 bug 的真根因）。

    绘制里所有宽度都是按 `opt.font` 量的；`painter` 若还用调用方给的那套字体
    （视图/控件的，与 `opt.font` 能差 20%），文字就会比列宽更宽、右边被切掉 ——
    现场看到的就是 `rosbag2_20261027144…` 与 `04-12 07:47`。

    这条不依赖"画出来像不像"：直接把 `_paint` 走一遍，再问 painter 用的是什么字体。
    """
    from PyQt5.QtGui import QImage, QPainter

    widget = _list([_row("a_20261027090000", stamp="10-26 18:16:57",
                         label=bag_verdict.NONE_TEXT)], width=900)
    bag_row_delegate.fit_columns(widget)
    option = _paint_option(widget)
    option.rect = QRect(0, 0, _item_width(widget), 40)

    image = QImage(max(option.rect.width(), 1), 40, QImage.Format_ARGB32)
    painter = QPainter(image)
    try:
        widget.itemDelegate()._paint(painter, option, widget.model().index(0, 0))
        used = painter.font().key()
    finally:
        painter.end()

    assert used == option.font.key(), (
        f"画字用的是 {used}，量宽用的是 {option.font.key()} —— "
        f"两套字体时列宽必然对不上（见 `painter.setFont(opt.font)`）")
