"""触摸屏点按容错（v6.7.2）。

现场 2026-10-22：「使用触摸屏的时候偶发几次无法点击按键」。

这里钉两件事：
1. **判定规则**（纯函数，四种情形）—— 容差内救、超容差不救、按下点在控件外不救；
2. **真的装上之后行为变了** —— 装过滤器前后，同一串 press / release 的 `clicked`
   结果不同。只测纯函数是不够的：接线断了（没装、装晚了、被别的过滤器抢先吞掉）
   纯函数照样全绿。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PyQt5.QtCore import QEvent, QPoint, QRect, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication, QPushButton

from data_collector import theme
from data_collector.ui import touch

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qapp():
    """离屏 QApplication，并把缩放松回 1.0（容差换算依赖它）。"""
    app = QApplication.instance() or QApplication([])
    theme.configure_scale(1.0)
    touch._TOUCH_FILTER = None          # 每个用例从"没装过"开始
    yield app
    touch._TOUCH_FILTER = None
    theme.configure_scale(1.0)


def _send(app, widget, kind, pos, button=Qt.LeftButton):
    """往控件丢一条鼠标事件（走 app 的分发路径，过滤器才会经过）。"""
    event = QMouseEvent(kind, QPoint(pos.x(), pos.y()), button, button, Qt.NoModifier)
    app.sendEvent(widget, event)


# ==================== 1. 判定规则（纯函数） ====================

def test_forgiving_release_keeps_an_inside_release_untouched():
    """正常命中时不插手 —— 返回 None 表示"原样交给控件"。

    这一条防的是"顺手把所有 release 都改写"：那样拖拽、滑动选择全会变形。
    """
    rect = QRect(0, 0, 88, 28)

    assert touch.forgiving_release_pos(rect, QPoint(44, 14), QPoint(44, 20), 12) is None


def test_forgiving_release_rescues_a_release_just_outside():
    """抬手漂出 6px（实测旧行为会丢点击）→ 应当救回按下点。"""
    rect = QRect(0, 0, 88, 28)
    press, release = QPoint(44, 14), QPoint(44, 34)     # 下边缘 27，漂出 7px

    got = touch.forgiving_release_pos(rect, press, release, 12)

    assert got == press, "容差内应当按「在按下点抬起」处理"


def test_forgiving_release_refuses_a_long_drag():
    """拖走太远 = 用户本来就想取消 —— 不能救。

    这条是安全边界：救错了等于"按下去拖走也照样执行"，比"偶发点了没反应"危险。
    """
    rect = QRect(0, 0, 88, 28)

    assert touch.forgiving_release_pos(rect, QPoint(44, 14), QPoint(44, 74), 12) is None
    assert touch.forgiving_release_pos(rect, QPoint(44, 14), QPoint(44, 41), 12) is None


def test_forgiving_release_ignores_corner_overflow():
    """拐角按切比雪夫算：右下角同时溢出 8px、8px 仍在容差内；溢出 20px 就不救。"""
    rect = QRect(0, 0, 88, 28)
    press = QPoint(80, 20)

    assert touch.forgiving_release_pos(rect, press, QPoint(96, 36), 12) == press
    assert touch.forgiving_release_pos(rect, press, QPoint(108, 48), 12) is None


def test_forgiving_release_requires_the_press_to_be_inside():
    """按下点都不在控件里 → 不救（那是"先按在旁边、再滑进来"，补按会变成误点）。"""
    rect = QRect(0, 0, 88, 28)

    assert touch.forgiving_release_pos(rect, QPoint(200, 200), QPoint(44, 30), 12) is None


# ==================== 2. 真的装上之后（行为层） ====================

def test_filter_rescues_a_release_that_drifted_out(qapp):
    """装上之后：press 在按钮内、抬手漂出 7px → **仍然 emit clicked**。"""
    filt = touch.install_touch_taps(qapp)
    btn = QPushButton("漂移")
    btn.resize(88, 28)
    hits = []
    btn.clicked.connect(lambda: hits.append(1))

    _send(qapp, btn, QEvent.MouseButtonPress, QPoint(44, 22))
    _send(qapp, btn, QEvent.MouseButtonRelease, QPoint(44, 34))     # 下边缘 27，漂出 7px

    assert hits == [1], "抬手漂出容差内必须仍然算一次点击"
    assert filt.rescued == 1, "救回来的次数要能数出来（现场复现时靠它确认）"
    btn.deleteLater()


def test_filter_still_cancels_a_drag(qapp):
    """装上之后：拖走 40px 仍然不触发（保持"拖出取消"的语义）。"""
    filt = touch.install_touch_taps(qapp)
    btn = QPushButton("拖走")
    btn.resize(88, 28)
    hits = []
    btn.clicked.connect(lambda: hits.append(1))

    _send(qapp, btn, QEvent.MouseButtonPress, QPoint(44, 14))
    _send(qapp, btn, QEvent.MouseButtonRelease, QPoint(44, 60))

    assert hits == [], "拖出容差之外不能算点击"
    assert filt.rescued == 0
    btn.deleteLater()


def test_filter_does_not_steal_a_release_from_another_widget(qapp):
    """press 在 A、release 落在 B → **一次都不许救**（宁可不救，也不能执行错的命令）。

    参数是精心选的：A 的按下点 (44,26) 也在 B 的范围内、B 收到的抬起点 (44,34)
    距 B 下边缘只 7px —— 如果不做"只能救按下时那个控件"的判断，这条会被当成
    "B 的漂移点击"救回来（`rescued` 变 1）。所以这里断言的是 `rescued == 0`，
    而不是只看"A、B 都没触发"：B 没有 down 状态，本来就不会触发，光看 hits 抓不住这个变异。
    """
    filt = touch.install_touch_taps(qapp)
    first, second = QPushButton("A"), QPushButton("B")
    first.resize(88, 28)
    second.resize(88, 28)
    hits = []
    first.clicked.connect(lambda: hits.append("A"))
    second.clicked.connect(lambda: hits.append("B"))

    _send(qapp, first, QEvent.MouseButtonPress, QPoint(44, 26))
    _send(qapp, second, QEvent.MouseButtonRelease, QPoint(44, 34))

    assert hits == [], f"不能把 A 的按下算到 B 头上：{hits}"
    assert filt.rescued == 0, "跨控件的抬起不该被救（等于替 B 补了一次点击）"
    first.deleteLater()
    second.deleteLater()


def test_filter_ignores_a_long_press(qapp, monkeypatch):
    """按住超过 `TAP_PRESHOLD_SEC` 之后的抬手不算点击（位置与按下点无关）。"""
    touch.install_touch_taps(qapp)
    # 冻结时间再往后退：press 记 0 秒，release 时已过 5 秒
    clock = {"now": 100.0}
    monkeypatch.setattr(touch.time, "monotonic", lambda: clock["now"])
    btn = QPushButton("长按")
    btn.resize(88, 28)
    hits = []
    btn.clicked.connect(lambda: hits.append(1))

    _send(qapp, btn, QEvent.MouseButtonPress, QPoint(44, 14))
    clock["now"] += 5.0
    _send(qapp, btn, QEvent.MouseButtonRelease, QPoint(44, 30))

    assert hits == [], "长按后的抬手不该被当成点击"
    btn.deleteLater()


# ==================== 3. 接线：启动时真的装了 ====================

def test_app_installs_the_filter_before_the_window_is_built():
    """`app.py` 必须在**建窗之前**装过滤器。

    装晚了不算错（app 级过滤器对之后的事件照样生效），但"窗口已建、控件已建"时
    再装，会让排查的人以为某些早期控件不生效 —— 判据写成顺序，让这条约束显式化。
    """
    source = (PROJECT / "data_collector" / "app.py").read_text(encoding="utf-8")

    assert "install_touch_taps(app)" in source, "启动流程里没有武装点按容错"
    assert source.index("install_touch_taps(app)") < source.index("win = MainWindow("), \
        "要点按容错应当在建窗之前装"
