"""数据文件夹列表的三列绘制：名字 / 时间 / 判定（v6.9.1）。

## 为什么自绘

`ui/bag_verdict.row_text` 产出的是「名字 + 四个空格 + 时间 + 四个空格 + 判定」，
指望在等宽字体下自然对齐。但**名字长短每家不同**（`vla_2026…` 与
`linetest_2026…` 差好几个字符），时间列的起点就跟着漂，判定列更漂 ——
一屏几十行看下来是斜的。现场一句「列对齐」指的就是这个。

补空格做不到稳：得先知道这一屏最长的名字（逐条刷新时会变，已画的行要重排）、
中文与 ASCII 显示宽度不同（算错就整列串位）、超长名字补不住只能截断。

固定列位把这些一次解决：时间列与判定列的 x 只由**容器宽度**算出来，
名字多长都不影响；名字太长就压缩它并用省略号。

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
from PyQt5.QtGui import QColor, QFontMetrics, QPen
from PyQt5.QtWidgets import (QApplication, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem)

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

    纯函数（只吃度量与字符串），便于单测；用到它的地方见 `fit_name_column`。
    """
    widest = 0
    for name in names:
        widest = max(widest, metrics.horizontalAdvance(str(name or "")))
    return max(widest + theme.px(ELIDE_SLACK), theme.px(MIN_NAME_W))


def fit_name_column(bag_list) -> None:
    """按列表里当前**最长的目录名**给名字列定宽。

    ## 为什么只在刷新后量一次

    "验证完成一条"只改文本后两段，名字不变 —— 所以列位在逐条刷新期间是稳的。
    如果每次重绘都重新量，验证跑到一半列宽会突然变一下，正在看的人会以为列表
    重排了。

    ## 为什么这件事不能写进 delegate 自己

    delegate 不能持有列表引用（会形成引用环，实测能让之后的 `setStyleSheet`
    段错误）。所以由这里以**参数**拿到控件、量完把结果（一个整数）交给它。
    """
    delegate = bag_list.itemDelegate()
    if not isinstance(delegate, BagRowDelegate):
        return                      # 别人的 delegate，不去动它的设定
    metrics = QFontMetrics(bag_list.font())
    names = (bag_verdict.split_row(bag_list.item(i).text())[0]
             for i in range(bag_list.count()))
    delegate.set_name_width(measure_name_width(metrics, names))


class BagRowDelegate(QStyledItemDelegate):
    """把一行画成三列。item 的 `text()` 仍是 `row_text` 的产出（数据层不变）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        #: 名字列的上限（像素）。由 `fit_name_column()` 在刷新后量出来。
        #: 这里存的是**一个整数**，不是控件引用 —— 见上面第 1 条硬约束。
        self._name_width = 0

    def set_name_width(self, width: int) -> None:
        """设置名字列的上限。量法见 `fit_name_column()`。"""
        self._name_width = max(int(width), 0)

    def name_width(self) -> int:
        return self._name_width

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
        metrics = QFontMetrics(opt.font)
        layout = bag_verdict.column_layout(
            opt.rect.width(),
            metrics.horizontalAdvance(STAMP_PROBE),
            max(metrics.horizontalAdvance(item) for item in bag_verdict.ALL_LABELS),
            hpad=theme.px(HPAD),
            gap=theme.px(GAP),
            name_cap=self._name_width or None,
        )

        # 颜色取 item 的前景色（`setForeground` 设的就是它）：不合格整行转红，
        # 选中时**也保持红** —— 判定是这个列表最要紧的信息，选中一下就不红
        # 反而会让人以为它变成合格了。
        brush = index.data(Qt.ForegroundRole)
        color = brush.color() if hasattr(brush, "color") else None
        if color is None or not color.isValid():
            color = QColor(theme.palette(theme.active_mode())["text"])
        painter.setPen(QPen(color))

        rect = opt.rect
        if layout.name_w > 0:
            # 名字可以截断（它只是标识），但时间与判定不能 —— 后两者是判据。
            painter.drawText(
                QRect(layout.name_x, rect.y(), layout.name_w, rect.height()),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                metrics.elidedText(name, Qt.ElideRight, layout.name_w))
        for x, width, value in ((layout.stamp_x, layout.stamp_w, stamp),
                                (layout.verdict_x, layout.verdict_w, label)):
            if width <= 0 or not value:
                continue
            painter.drawText(QRect(x, rect.y(), width, rect.height()),
                             int(Qt.AlignLeft | Qt.AlignVCenter), value)

    def sizeHint(self, option, index):  # noqa: N802 (Qt 命名)
        """行高：字体高度 + 内边距，且不低于触摸门槛（32px 设计稿值）。"""
        height = QFontMetrics(option.font).height() + 2 * theme.px(VPAD)
        return QSize(0, max(height, theme.px(MIN_ROW_H)))
