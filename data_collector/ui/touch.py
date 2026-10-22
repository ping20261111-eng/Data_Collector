"""触摸屏的点按容错 —— 让「按下去、抬起时漂出去了」仍然算一次点击。

## 为什么需要这一层（现场 2026-10-22）

现场：「使用触摸屏的时候偶发几次无法点击按键」。

根因不在事件绑定，而在 Qt 的**命中判定**：`QAbstractButton::mouseReleaseEvent()`
只在 `hitButton(e->pos())` 为真时才 `click()`，而默认的 `hitButton()` 就是
`rect().contains(pos)` —— **抬起点只要漂到控件外一个像素，这次点击就整个丢掉**。

实测（离屏，逐帧发 press / move / release）：

| 手势 | 结果 |
| --- | --- |
| 精确点击中心 | 触发 |
| 按下后漂移 2px（仍在控件内） | 触发 |
| 按下后漂移 6px（出界，28px 高按钮） | **丢失** |
| 按下在底部边缘 2px、抬起漂 4px | **丢失** |
| 按下在右侧边缘 2px、抬起漂 4px | **丢失** |

鼠标不会这样：指针位置是精确的，而且"按下后拖出去松手"本来就是要取消。
**手指会**：接触面积大、按下与抬起之间坐标天然漂移几像素到十几像素，
按在按钮边缘时抬起点几乎必然出界。于是同一个按钮，鼠标百发百中、手指偶发失效。

## 这一层做什么

只做**一个方向**的补救：

    press 落在控件内  →  release 漂到控件外，但漂出距离 ≤ 容差  →  当成"在按下点抬起"

具体做法是用 `QMouseEvent` 重发一条 release、位置取**按下点**。此时控件的
`down` 仍是 True（press 已被它收下，并且它 grab 了鼠标），所以 `click()` 正常发生。

## 这一层**不做**什么

**不**处理"press 落在控件外、release 滑进控件"：那需要凭空补一个 press，
而"在列表上按住、拖到按钮上松手"原本是不触发的 —— 补上之后会把拖放变成误点
（最坏是一次拖到「删除」上松手）。少救一种手势，比多出一种误操作划算。

## 为什么不直接把控件加高

量过：全项目**短边 < 44px 的可点击控件有 84 个**（WCAG 2.1 的触摸目标下限），
其中姿势页 20 个目标角度输入框只有 25px 高、行高 27px —— 加高会把三列 20 行
撑出 1080 屏幕。而容差在**效果上等价于"点击区外扩一圈"**，却不需要任何版面改动。
只有 18px 高那 4 个启动参数复选框（版面里有余量）单独加高，见 `actions.py`。
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication, QComboBox,
                             QLineEdit)

from .. import theme

#: 抬起点允许漂出控件多远（**设计稿像素**，运行时过 `theme.px()`）。
#:
#: 12 的来历：实测 28px 高按钮漂 6px 就会丢；手指抬起时的典型漂移是 5~15px。
#: 再大就会跨到相邻控件上 —— 相邻按钮的间距是 8px（`SP["sp2"]`），
#: 容差必须小于"间距 + 目标控件自身尺寸"，否则会把 A 的按下算到 B 头上。
#: 这个值配上 25~44px 的控件高度，等效点击区约 49~68px，够指尖用。
TAP_TOLERANCE = 12

#: 超过这个时长（秒）就算"长按"，不再容错。
#:
#: 长按在本程序里没有绑定动作，但"按住看 tooltip / 按住读一遍再决定拖走"是存在的；
#: 这种手势结束时的 release 位置与按下点无关，不该被当成点击。
TAP_PRESHOLD_SEC = 3.0


def outside_distance(rect, pos) -> int:
    """点到矩形外部的**切比雪夫**距离；点在内部返回 0。

    用切比雪夫（`max(dx, dy)`）而不是欧氏距离：容错区应该是"矩形向外扩一圈"
    （拐角处是方的），这样判据与"点击区外扩"在语义上完全一致。
    """
    dx = max(rect.left() - pos.x(), 0, pos.x() - rect.right())
    dy = max(rect.top() - pos.y(), 0, pos.y() - rect.bottom())
    return max(dx, dy)


def forgiving_release_pos(rect, press_pos: QPoint, release_pos: QPoint,
                          tolerance: int) -> Optional[QPoint]:
    """抬起点漂出矩形但在容差内 → 返回该用的抬起位置（按下点）；否则 `None`。

    抽成纯函数是为了能直接单测四种情形（内部 / 容差内出界 / 超容差 / 拐角），
    而不必去伪造一次真实的鼠标序列。
    """
    if rect.contains(release_pos):
        return None                       # 正常命中，别插手
    if tolerance < 0:
        return None
    if outside_distance(rect, release_pos) > tolerance:
        return None                       # 拖走太远 = 用户本来就想取消
    if not rect.contains(press_pos):
        return None                       # 按下点都不在控件里，说明不是这次点击
    return press_pos


def _is_tappable(widget) -> bool:
    """哪些控件值得为它做容错。

    只收"按一下就产生动作"的控件。列表 / 表格刻意不收：它们的 release 会改选中项，
    把抬起点挪回按下点会让"按住滑动选择"的行为变怪 —— 而那正是触摸屏上的正常操作。
    """
    return isinstance(widget, (QAbstractButton, QLineEdit, QAbstractSpinBox, QComboBox))


class TouchTapFilter(QObject):
    """app 级事件过滤器：把"漂出界的抬起"救回按下点。

    装在 `QApplication` 上而不是逐个控件上：控件是散布在十几个文件里造的，
    漏掉一个就漏掉一处 —— 而这类缺陷恰恰是"偶发、难复现"的那种。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press: Optional[tuple] = None      # (widget, 按下点, 单调时间)
        #: 救回来的点击数。**只用于诊断**（现场复现时能确认这一层真的在起作用），
        #: 不参与任何业务判断。
        self.rescued = 0

    def tolerance(self) -> int:
        """当前缩放下的容差像素。运行时取，允许运行中切缩放。"""
        return theme.px(TAP_TOLERANCE)

    def eventFilter(self, obj, event):            # noqa: N802 (Qt 命名)
        kind = event.type()
        # 双击的**第二下**在 Qt 里发的是 `MouseButtonDblClick`（不先发 Press），
        # 所以这里要一起收 —— 否则"连点两下"的第二下享受不到容错。
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if event.button() == Qt.LeftButton and _is_tappable(obj):
                self._press = (obj, QPoint(event.pos()), time.monotonic())
            else:
                self._press = None
            return False
        if kind != QEvent.MouseButtonRelease:
            return False

        saved, self._press = self._press, None
        if saved is None or event.button() != Qt.LeftButton:
            return False
        widget, press_pos, pressed_at = saved
        if widget is not obj or not _is_tappable(obj):
            # 抬起落在**别的**控件上：不插手。宁可少救一次，也不能把 A 的点按
            # 算到 B 头上（那比"点了没反应"更糟：会执行错的命令）。
            return False
        if time.monotonic() - pressed_at > TAP_PRESHOLD_SEC:
            return False                          # 长按之后的抬起，位置与按下点无关
        target = forgiving_release_pos(obj.rect(), press_pos, QPoint(event.pos()),
                                       self.tolerance())
        if target is None:
            return False

        # 重发一条"在按下点抬起"的 release。控件此时仍是 down 状态（press 已收下、
        # 且它 grab 了鼠标），所以这条会正常走到 `click()`。
        fixed = QMouseEvent(QEvent.MouseButtonRelease, QPointF(target),
                            QPointF(obj.mapToGlobal(target)),
                            Qt.LeftButton, Qt.NoButton, event.modifiers())
        QApplication.sendEvent(obj, fixed)
        self.rescued += 1
        return True                               # 原事件已由上面这条替代


#: 进程内唯一实例（同一个 app 只装一次）。
_TOUCH_FILTER: Optional[TouchTapFilter] = None


def install_touch_taps(app) -> TouchTapFilter:
    """给 app 装「点按容错」过滤器（幂等）。

    与 `theme.apply_popup_rounding()` 同样的顾虑：上一次的过滤器可能属于
    **已经销毁的 QApplication**（测试里每个用例换一个），那时连 `parent()`
    都会抛 `RuntimeError`，所以每一步都要挡住。
    """
    global _TOUCH_FILTER
    if app is None:
        return None
    if _TOUCH_FILTER is not None:
        try:
            previous = _TOUCH_FILTER.parent()
        except RuntimeError:
            previous = None                   # 包装对象已失效，当作没装过
        if previous is app:
            return _TOUCH_FILTER
        if previous is not None:
            try:
                previous.removeEventFilter(_TOUCH_FILTER)
            except (AttributeError, RuntimeError):
                pass                          # 上一个 app 已经销毁（测试里常见）
    _TOUCH_FILTER = TouchTapFilter(app)
    app.installEventFilter(_TOUCH_FILTER)
    return _TOUCH_FILTER
