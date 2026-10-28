"""数据文件夹列表的三列绘制：名字 / 时间 / 判定（v6.9.1）。

## 为什么自绘

`ui/bag_verdict.row_text` 产出的是「名字 + 四个空格 + 时间 + 四个空格 + 判定」，
指望在等宽字体下自然对齐。但**名字长短每家不同**（`vla_2026…` 与
`linetest_2026…` 差好几个字符），时间列的起点就跟着漂，判定列更漂 ——
一屏几十行看下来是斜的。现场一句「列对齐」指的就是这个。

补空格做不到稳：得先知道这一屏最长的名字（逐条刷新时会变，已画的行要重排）、
中文与 ASCII 显示宽度不同（算错就整列串位）、超长名字补不住只能截断。

固定列位把这些一次解决：三列的 x 只由**几个宽度**算出来，名字多长都不影响。
v6.9.5 起名字列按**最长名字的自然宽度**定死，装不下时出现横向滚动条 ——
现场明确要求"名称要显示全部"，截断成 `vla_202610…` 的行彼此无法区分，等于没名字。

## 🔴 三条硬约束（都踩过，别改）

1. **不持有列表 / 窗口的引用**。只在 `paint` 时用 `option`，主题从
   `theme.active_mode()` 现读。delegate 反向引用宿主会形成 Python 引用环，
   环的回收顺序不定，实测会让之后任意一次 `setStyleSheet` 段错误
   （与 `point_dialog._LabelCardDelegate` 同一个坑）。
2. **QPainter 必须收尾**。中途抛异常会让它来不及 `end()`，之后整个进程可能
   以段错误收场，而且崩点离病因很远。所以 save/restore 走 try/finally，
   绘制异常吞掉并记日志 —— 一行画不出来也不能让整个列表画不出来。
3. **背景交给样式画**。把 `option.text` 临时清空后 `drawControl(CE_ItemViewItem)`：
   选中态、hover、隔行底色都还是主题那套。自己涂色迟早与 QSS 分叉。
"""
from PyQt5.QtCore import QRect, QSize, Qt
from PyQt5.QtGui import QFontMetrics, QPalette, QPen
from PyQt5.QtWidgets import (QApplication, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem)
from typing import Optional

from .. import theme
from ..core import logger
from . import bag_verdict

#: 时间列的宽度基准。用一串"最宽的形状"探出来（等宽字体下与真实时间戳同宽），
#: 而不是去遍历每一行取最大值 —— 那样逐条刷新时列宽会跳。
STAMP_PROBE = "00-00 00:00:00"

#: 左右留白与列间距（设计稿 1.0 下的像素，用前过 `theme.px()`）。
HPAD = 10
GAP = 16

#: 行高下限。列表行是可点目标，项目规矩是短边 ≥32px（`NOTES-gui` 的触摸一节）；
#: 实测原来的行高只有 20px —— 手指点上去经常落到相邻两行之间。
MIN_ROW_H = 32
#: 上下内边距（字体本身高度之外再加这些）。
VPAD = 8

#: 名字列的最小宽度（设计稿像素）。只有一两行、名字很短时别把时间列顶到左边框上。
MIN_NAME_W = 96

#: 量名字宽时留的余量（设计稿像素）。见 `measure_name_width` 的说明。
ELIDE_SLACK = 4


def measure_name_width(metrics: QFontMetrics, names) -> int:
    """名字列该多宽：**最长名字**的像素宽（留一点余量），且不低于 `MIN_NAME_W`。

    留余量不是为了"好看一点"：`horizontalAdvance` 会按整数取整，而
    `elidedText` 判"放不放得下"用的是同一套度量的另一种算法 —— 实测不留余量时
    最长的那个名字仍会被截成「…」，看起来像列宽算错了。`ELIDE_SLACK` 就是这 1~2px。

    纯函数（只吃度量与字符串），便于单测；用到它的地方见 `fit_columns`。
    """
    widest = 0
    for name in names:
        widest = max(widest, metrics.horizontalAdvance(str(name or "")))
    return max(widest + theme.px(ELIDE_SLACK), theme.px(MIN_NAME_W))


def _paint_metrics(delegate, bag_list) -> Optional[QFontMetrics]:
    """**绘制时真正用的那套字体度量**。

    ## 为什么不能用 `bag_list.font()`

    v6.9.2 实测：控件字体是 14px Noto Sans SC（`setFont` 设的等宽字体因
    `IBM Plex Mono` 未安装被回退），而 `initStyleOption` 交给 delegate 的
    `option.font` 是 12pt DejaVu —— **两套完全不同的字体**。
    用控件字体量宽度，名字列就会偏窄：最长的名字明明放得下也被截成「…」，
    时间列宽也会差 10%。

    所以一律以 `initStyleOption` 的 `font` 为准 —— 那才是 `paint()` 里画的字体。
    """
    if bag_list.count() == 0:
        return None
    option = QStyleOptionViewItem()
    delegate.initStyleOption(option, bag_list.model().index(0, 0))
    return QFontMetrics(option.font)


def measure_stamp_width(metrics: QFontMetrics, stamps) -> int:
    """时间列宽度：取**实际时间戳**里最宽的那个。

    不用固定的探针（`00-00 00:00:00`）而不是因为懒：本机列表其实是**比例字体**
    （等宽字体没装），探针宽度与真实时间戳能差 10%，多出来的那点就会被判定列吃掉。
    """
    return max((metrics.horizontalAdvance(str(s or "")) for s in stamps), default=0)


def fit_columns(bag_list) -> None:
    """按列表里当前的内容给**名字列与时间列**定宽。

    ## 为什么只在刷新后量一次

    "验证完成一条"只改文本后两段，名字与时间都不变 —— 所以列位在逐条刷新期间是稳的。
    如果每次重绘都重新量，验证跑到一半列宽会突然变一下，正在看的人会以为列表重排了。

    ## 为什么这件事不能写进 delegate 自己

    delegate 不能持有列表引用（会形成引用环，实测能让之后的 `setStyleSheet`
    段错误）。所以由这里以**参数**拿到控件、量完把结果（两个整数）交给它。
    """
    delegate = bag_list.itemDelegate()
    if not isinstance(delegate, BagRowDelegate):
        return                      # 别人的 delegate，不去动它的设定
    metrics = _paint_metrics(delegate, bag_list)
    if metrics is None:
        delegate.set_name_width(0)
        delegate.set_stamp_width(0)
        return
    rows = [bag_verdict.split_row(bag_list.item(i).text())
            for i in range(bag_list.count())]
    delegate.set_name_width(measure_name_width(metrics, (r[0] for r in rows)))
    delegate.set_stamp_width(measure_stamp_width(metrics, (r[1] for r in rows)))


class BagRowDelegate(QStyledItemDelegate):
    """把一行画成三列。item 的 `text()` 仍是 `row_text` 的产出（数据层不变）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        #: 名字列的上限 / 时间列的宽度（像素）。都由 `fit_columns()` 在刷新后量。
        #: 这里存的是**整数**，不是控件引用 —— 见上面第 1 条硬约束。
        self._name_width = 0
        self._stamp_width = 0

    def set_name_width(self, width: int) -> None:
        """设置名字列的上限。量法见 `fit_columns()`。"""
        self._name_width = max(int(width), 0)

    def name_width(self) -> int:
        return self._name_width

    def set_stamp_width(self, width: int) -> None:
        """设置时间列宽度。量法见 `fit_columns()`。"""
        self._stamp_width = max(int(width), 0)

    def stamp_width(self) -> int:
        return self._stamp_width

    def paint(self, painter, option, index):  # noqa: N802 (Qt 命名)
        # 自绘必须保证 QPainter 收尾：中途抛异常会让它来不及 end()，之后整个进程
        # 可能以段错误收场（本项目踩过，崩点离病因很远）。绘制失败也不能把 GUI 拖崩。
        painter.save()
        try:
            self._paint(painter, option, index)
        except Exception as exc:                      # noqa: BLE001
            logger.warning(f"数据行绘制失败（已跳过这一行）：{exc}")
        finally:
            painter.restore()

    def _paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        # 背景（选中 / hover / 隔行）交给样式：清空 text 只是不让它按默认位置
        # 把整段字符串再画一遍。
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        name, stamp, label = bag_verdict.split_row(text)
        # 状态由**判定文案**反推（`row_text` 的第三段）：绘制层不再单独存一份状态，
        # 也就不可能出现"存的状态与显示的字不一致"。
        state = bag_verdict.state(label)
        selected = bool(opt.state & QStyle.State_Selected)

        # 不合格：先叠一层 10% 的红底（选中时不叠 —— 选中底色本身就是强调色）。
        tint = None if selected else bag_verdict.row_tint(state)
        if tint is not None:
            painter.fillRect(opt.rect, tint)

        metrics = QFontMetrics(opt.font)
        layout = bag_verdict.column_layout(
            opt.rect.width(),
            self._stamp_width or metrics.horizontalAdvance(STAMP_PROBE),
            max(metrics.horizontalAdvance(item) for item in bag_verdict.ALL_LABELS),
            hpad=theme.px(HPAD),
            gap=theme.px(GAP),
            name_w=self._name_width or None,
        )

        # 选中态一律用样式的 highlightedText：选中底色是强调色，红绿压上去都不好读，
        # 而"合格/不合格"这几个字本身已经把语义说清了。
        selected_color = (opt.palette.color(QPalette.HighlightedText)
                          if selected else None)

        rect = opt.rect
        for column, x, width, value in (
                (bag_verdict.COLUMN_NAME, layout.name_x, layout.name_w, name),
                (bag_verdict.COLUMN_STAMP, layout.stamp_x, layout.stamp_w, stamp),
                (bag_verdict.COLUMN_VERDICT, layout.verdict_x, layout.verdict_w, label)):
            if width <= 0 or not value:
                continue
            painter.setPen(QPen(bag_verdict.column_color(state, column, selected_color)))
            # 名字列已按最长名字定宽，正常路径下 `elidedText` 会原样返回；
            # 留着它只是兜底（列宽还没量过时也不至于糊出边界）。
            if column == bag_verdict.COLUMN_NAME:
                value = metrics.elidedText(value, Qt.ElideRight, width)
            painter.drawText(QRect(x, rect.y(), width, rect.height()),
                             int(Qt.AlignLeft | Qt.AlignVCenter), value)

    def sizeHint(self, option, index):  # noqa: N802 (Qt 命名)
        """行高按触摸门槛，行宽按**内容需要**（不够宽就横向滚动，不截名字）。

        宽度这里只读 `option.widget`（视图）当场量，**不存下来** ——
        delegate 持有宿主引用会形成引用环，实测能让之后的 `setStyleSheet` 段错误。
        """
        height = QFontMetrics(option.font).height() + 2 * theme.px(VPAD)
        return QSize(self._row_width(option), max(height, theme.px(MIN_ROW_H)))

    def _row_width(self, option) -> int:
        """这一行要画多宽：**内容需要**与**视口宽**取大者。

        - 内容更宽（窄窗口）→ 返回内容宽度，视图出现横向滚动条，名字仍然完整；
        - 视口更宽 → 返回视口宽度，行底纹铺满整行，不会在右侧空出一条。
        """
        metrics = QFontMetrics(option.font)
        needed = bag_verdict.required_row_width(
            self._name_width,
            self._stamp_width or metrics.horizontalAdvance(STAMP_PROBE),
            max(metrics.horizontalAdvance(item) for item in bag_verdict.ALL_LABELS),
            hpad=theme.px(HPAD),
            gap=theme.px(GAP),
        )
        widget = getattr(option, "widget", None)
        viewport = 0
        if widget is not None:
            try:
                viewport = widget.viewport().width()
            except AttributeError:            # 不是视图（某些测试直接给 QWidget）
                viewport = 0
        return max(needed, viewport)
