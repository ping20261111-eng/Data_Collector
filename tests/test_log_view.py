"""日志区：行数封顶（内存）、翻历史不被拽走、以及「回到最新」。

## 为什么必须是 `QPlainTextEdit`

先按"在 `QTextEdit` 上加行数上限"做过一版，实测**封不住**：自己用
`QTextCursor.removeSelectedText()` 裁掉旧 block 之后，内存仍随**累计写入量**涨 ——
3 万行 +18.8 MB、6 万行 +22.8 MB、12 万行 +34.9 MB（`QTextDocument` 的
fragment / layout / 格式表不随删除回收）。连 Qt 自己的 `setMaximumBlockCount`
在 `QTextEdit` 上也是 +32.5 MB。

`QPlainTextEdit` + `setMaximumBlockCount(5000)`：写 6 万行 +8.8 MB，写 20 万行
**还是 +8.8 MB** —— 这才是封顶。

所以这里有一条**容器类型**守卫：谁把它改回 `QTextEdit`，内存会重新失控，
而界面看起来一模一样、功能测试也全绿 —— 只有这条能拦住。

## 用例怎么控制规模

生产上限 5000 行，写那么多太慢。守卫把上限 monkeypatch 成 50，行为一致。
"""
import pytest

from data_collector.ui.widgets import LogView


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qapp, monkeypatch):
    """一个真正显示出来的日志区（不显示就没有滚动条 range）。"""
    monkeypatch.setattr(LogView, "MAX_BLOCKS", 50)
    widget = LogView()
    widget.setMaximumBlockCount(LogView.MAX_BLOCKS)   # 类属性被改，实例要跟上
    widget.resize(600, 200)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _fill(view, qapp, count, prefix="line", start=0):
    for i in range(start, start + count):
        view.append(f"{prefix} {i:05d} " + "x" * 60)
    qapp.processEvents()


def _bar(view):
    return view.verticalScrollBar()


def _scroll_to_history(view, qapp):
    """模拟用户往上翻：滚到中间，并让滚动信号处理完。"""
    _bar(view).setValue(_bar(view).maximum() // 3)
    qapp.processEvents()
    assert view.is_at_bottom() is False, "用例前提：应当已经离开底部"


# ---------- 容器选型（内存这一项的地基） ----------


def test_log_view_is_a_plain_text_edit():
    """**必须是 `QPlainTextEdit`**（v6.7.4）。

    改回 `QTextEdit` 的后果不是报错，而是"内存悄悄重新线性增长"：
    界面、链接、翻历史全都正常，功能测试也全绿。实测数据见模块 docstring。
    """
    from PyQt5.QtWidgets import QPlainTextEdit

    assert issubclass(LogView, QPlainTextEdit), (
        "日志区一旦换回 QTextEdit，行数上限就封不住内存（实测 12 万行 +34.9 MB）")


def test_console_stylesheet_covers_plain_text_edit():
    """QSS 必须同时匹配新类名，否则背景/等宽字体/内边距会一起失效。

    这是个"换控件时最容易漏、且看起来最像样式坏了"的连带修改。
    """
    from pathlib import Path

    from data_collector import theme

    source = Path(theme.__file__).read_text(encoding="utf-8")
    assert "QPlainTextEdit#Console" in source, (
        "换容器后 QSS 选择器要跟着扩，否则日志区丢掉等宽字体与背景")


# ---------- 行数封顶（内存） ----------


def test_lines_beyond_the_limit_are_trimmed(view, qapp):
    """写超上限：块数停在上限，不会继续长。

    `QPlainTextEdit` 的内存与块数成正比，块数封住 = 内存封住（实测写 20 万行不变）。
    """
    _fill(view, qapp, 200)

    assert view.document().blockCount() == LogView.MAX_BLOCKS


def test_trim_keeps_the_newest_lines_and_drops_the_oldest(view, qapp):
    """裁掉的必须是**最旧**的行：留下最旧的等于把现场最需要的近况丢了。"""
    _fill(view, qapp, 200)
    lines = view.toPlainText().splitlines()

    assert lines[-1].startswith("line 00199"), "最后一行必须是最新那条"
    assert not any(line.startswith("line 00000") for line in lines), "最早的行应当被裁掉"


def test_block_count_stays_flat_however_much_is_written(view, qapp):
    """写 4 倍上限之后块数仍然不变 —— 内存封顶的直接证据。"""
    _fill(view, qapp, 50)
    first = view.document().blockCount()

    _fill(view, qapp, 150, start=1000)

    assert view.document().blockCount() == first == LogView.MAX_BLOCKS


def test_undo_history_is_disabled(view):
    """日志区必须关掉撤销栈。

    只读控件默认仍记录撤销步骤（实测写 3 万行攒 14 万步），被裁掉的文本还被
    undo 记录引用着 —— 等于给"上限"开了一个后门。日志区不需要撤销。
    """
    assert view.document().isUndoRedoEnabled() is False


def test_undo_steps_do_not_accumulate_while_writing(view, qapp):
    """行为版本：写一堆日志之后撤销步数必须是 0（只看开关不够）。"""
    _fill(view, qapp, 300)

    assert view.document().availableUndoSteps() == 0


# ---------- 翻历史不被拽走（体验） ----------


def test_view_still_follows_while_at_bottom(view, qapp):
    """停在底部时：追加之后仍然停在最新（不能让"不跟随"误伤正常跟随）。"""
    _fill(view, qapp, 100)

    assert view.is_at_bottom() is True


def test_appending_while_reading_history_does_not_jump_to_bottom(view, qapp):
    """**核心回归**：翻历史时来新日志，视图必须留在原处。

    现场原话「每次查日志往上滑，有新的日志之后就需要滑到底部」。
    `QPlainTextEdit.appendPlainText()` 原生就是"只在底部才跟随"，
    所以这条同时钉住"别把容器换回 `QTextEdit`"（它会 `ensureCursorVisible()`）。
    """
    _fill(view, qapp, 200)
    _scroll_to_history(view, qapp)

    view.append("NEW LINE WHILE READING HISTORY " + "y" * 40)
    qapp.processEvents()

    assert view.is_at_bottom() is False, "追加不该把视图拽回底部"
    assert "NEW LINE WHILE READING HISTORY" in view.toPlainText(), "但内容必须真的写进去了"


def test_history_survives_several_new_lines_while_reading(view, qapp):
    """连续来一串日志也拽不走 —— 一次追加没问题、十次才出问题的那种 bug 很常见。"""
    _fill(view, qapp, 200)
    _scroll_to_history(view, qapp)

    for i in range(10):
        view.append(f"burst {i}")
    qapp.processEvents()

    assert view.is_at_bottom() is False


# ---------- 「回到最新」按钮 ----------


def test_jump_button_shows_only_when_scrolled_away(view, qapp):
    """按钮只在"离开了底部"时出现 —— 停在最新时它没有意义，会白占眼睛。"""
    _fill(view, qapp, 200)
    assert view._jump_btn.isVisible() is False

    _scroll_to_history(view, qapp)
    assert view._jump_btn.isVisible() is True

    view.jump_to_latest()
    qapp.processEvents()
    assert view._jump_btn.isVisible() is False


def test_jump_to_latest_returns_to_the_bottom(view, qapp):
    """点一下：回到最新，并且之后的新日志重新跟随。"""
    _fill(view, qapp, 200)
    _scroll_to_history(view, qapp)

    view.jump_to_latest()
    qapp.processEvents()

    assert view.is_at_bottom() is True

    view.append("after jump")
    qapp.processEvents()
    assert view.is_at_bottom() is True, "回到底部后应当重新跟随最新"


def test_clear_hides_the_jump_button(view, qapp):
    """「清空」之后按钮不能还挂着 —— 那会是一个点了没反应的按钮。"""
    _fill(view, qapp, 200)
    _scroll_to_history(view, qapp)
    assert view._jump_btn.isVisible() is True

    view.clear()
    qapp.processEvents()

    assert view._jump_btn.isVisible() is False


def test_short_content_has_no_jump_button(view, qapp):
    """内容不足一屏时没有滚动条，也就没有按钮。"""
    _fill(view, qapp, 2)

    assert view._jump_btn.isVisible() is False


# ---------- 既有能力不能被改坏 ----------


def test_html_links_survive_the_new_append(view, qapp):
    """`append_link` 传的是 HTML（可点击目录）：换容器后必须仍然可点。

    用 `document().toHtml()` 而不是控件自己的 `toHtml()` —— `QPlainTextEdit`
    没有那个方法，取文档的才是通用写法。
    """
    from PyQt5.QtCore import QPoint

    from data_collector.ui.common import file_link

    view.append(file_link("打开目录", "/tmp"))
    qapp.processEvents()

    assert "href=" in view.document().toHtml(), "链接要作为锚点保留"
    assert view.anchorAt(QPoint(2, 2)) == "file:///tmp", "点击要能取回地址"
    assert "打开目录" in view.toPlainText()


def test_plain_text_with_angle_brackets_is_not_eaten(view, qapp):
    """纯文本里的 `<...>` 不能被当成 HTML 吃掉（日志里这类内容很常见）。"""
    view.append("读取 <workspace>/install 失败")
    qapp.processEvents()

    assert "<workspace>" in view.toPlainText(), (
        "纯文本行必须原样保留尖括号 —— append 要按 mightBeRichText 分流")
