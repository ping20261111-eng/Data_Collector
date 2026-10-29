"""判定列的渲染层（`ui/bag_verdict`）测试。

## 为什么单独测这一层

它被从 `data_view.py` 拆出来，唯一的原因是那个文件已经贴到 1000 行硬上限
（`test_no_single_ui_file_balloons`）。拆模块这个动作本身**不该改变行为** ——
所以这里把"三种判定 + 待验证"的文案、提示、颜色逐条钉死，
再由 `test_ui_wiring` 那几条走真实控件验证"列表上确实这么显示"。

两层都覆盖的理由：只有界面层的话，一个文案改错要靠建窗口才能发现；
只有这一层的话，`data_view` 忘了调用它也照样绿。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector import theme                    # noqa: E402
from data_collector.ui import bag_verdict           # noqa: E402


def _verdict(ok=True, detail="", inferred=False):
    return {"ok": ok, "detail": detail,
            "report": Path("/bags/a/数据验证结果_20261027-100000.txt"),
            "when": None, "inferred": inferred}


def test_label_covers_the_three_states_and_the_pending_one():
    """四种文案各归各位，不许混。"""
    assert bag_verdict.label(_verdict(ok=True)) == "合格"
    assert bag_verdict.label(_verdict(ok=False)) == "不合格"
    assert bag_verdict.label(None) == "未验证"
    # 待验证优先级最高：批量进行中它还没出结果，不能显示上一次的旧判定
    assert bag_verdict.label(_verdict(ok=False), pending=True) == "待验证"
    assert bag_verdict.label(None, pending=True) == "待验证"


def test_tooltip_explains_why_it_failed():
    """不合格必须能查到原因 —— 列表只有三个字。"""
    text = bag_verdict.tooltip(_verdict(ok=False, detail="缺 2 个话题"))
    assert "不合格" in text
    assert "缺 2 个话题" in text
    assert "数据验证结果_20261027-100000.txt" in text, "要指出用的是哪份报告"


def test_tooltip_says_so_when_there_is_no_report():
    text = bag_verdict.tooltip(None)
    assert "未验证" in text
    assert "批量验证" in text, "要告诉现场怎么把它变成已验证"


def test_tooltip_marks_a_legacy_report_as_inferred():
    """旧报告的结论是推断出来的，必须标出来。

    不标的话，现场会以为这是脚本当场算出的判定 —— 而它其实是从
    「二、结论」段的中文措辞猜的，措辞一改就失效。
    """
    text = bag_verdict.tooltip(_verdict(ok=True, inferred=True))
    assert "推断" in text


def test_row_text_keeps_the_three_columns():
    """`名字    时间    判定`，四空格分隔（与列表既有风格一致）。"""
    text = bag_verdict.row_text("vla_20261027100000", "10-27 10:00:00", "合格")
    assert text == "vla_20261027100000    10-27 10:00:00    合格"
    assert text.rsplit("    ", 1)[-1] == "合格"


def test_row_text_handles_a_missing_timestamp():
    """时间拿不到时用破折号占位，不能留空（留空会让三列看起来只有两列）。"""
    assert bag_verdict.row_text("a", "", "未验证") == "a    —    未验证"


def test_only_a_failed_verdict_turns_red():
    """**只有不合格**用红。

    一屏几十行里如果合格也用红，红色就不跳了 —— 而"哪几条要返工"
    正是这个列表最该一眼看出来的东西。合格用的是**绿**（判定列），不是红。
    """
    from PyQt5.QtGui import QColor

    colors = theme.palette(theme.active_mode())
    # 文字用 `*_text` 那一档：`danger` 是面色，压白底 4.38:1 不过 AA（见 theme 注释）
    danger, normal = QColor(colors["danger_text"]).name(), QColor(colors["text"]).name()

    assert bag_verdict.foreground(_verdict(ok=False)).name() == danger
    assert bag_verdict.foreground(_verdict(ok=True)).name() == normal
    assert bag_verdict.foreground(None).name() == normal
    assert danger != QColor(colors["ok_text"]).name(), "不合格与合格不能用同一个色"


def test_state_is_derived_from_the_row_text_itself():
    """三档状态从**判定文案**反推 —— 绘制层不再单独存一份状态。

    这样"存的状态"与"显示的字"不可能分叉；代价是文案表必须齐（下面那条钉住）。
    """
    assert bag_verdict.state(bag_verdict.PASS_TEXT) == bag_verdict.STATE_PASS
    assert bag_verdict.state(bag_verdict.FAIL_TEXT) == bag_verdict.STATE_FAIL
    assert bag_verdict.state(bag_verdict.NONE_TEXT) == bag_verdict.STATE_NONE
    assert bag_verdict.state(bag_verdict.PENDING_TEXT) == bag_verdict.STATE_PENDING
    # 认不出（旧报告/手写行/空串）一律按未验证：乱标绿比标成黑更危险
    assert bag_verdict.state("") == bag_verdict.STATE_NONE
    assert bag_verdict.state("需要复查") == bag_verdict.STATE_NONE


def test_column_colors_follow_the_design_table():
    """三列各自的颜色（设计表见 `bag_verdict` 模块头）。

    重点钉两件容易被"顺手统一"改掉的事：
    1. 时间列**不跟着红**（整行糊成红会失去"对时间线"的功能）；
    2. 合格只在**判定列**是绿的，名字列仍是正文色（否则一屏全绿，名字看不清）。
    """
    from PyQt5.QtGui import QColor

    colors = theme.palette(theme.active_mode())
    text = QColor(colors["text"]).name()
    ok, bad = QColor(colors["ok_text"]).name(), QColor(colors["danger_text"]).name()
    P, F, U = (bag_verdict.STATE_PASS, bag_verdict.STATE_FAIL,
               bag_verdict.STATE_NONE)
    name, stamp, verdict = (bag_verdict.COLUMN_NAME, bag_verdict.COLUMN_STAMP,
                            bag_verdict.COLUMN_VERDICT)

    assert bag_verdict.column_color(P, name).name() == text
    assert bag_verdict.column_color(P, stamp).name() == text
    assert bag_verdict.column_color(P, verdict).name() == ok

    assert bag_verdict.column_color(U, name).name() == text
    assert bag_verdict.column_color(U, stamp).name() == text
    assert bag_verdict.column_color(U, verdict).name() == text

    assert bag_verdict.column_color(F, name).name() == bad
    assert bag_verdict.column_color(F, stamp).name() == text, "时间列被染红了"
    assert bag_verdict.column_color(F, verdict).name() == bad

    # 选中态一律让位给样式的 highlightedText（红绿压强调色底都不好读）
    picked = QColor("#123456")
    for state_name in (P, U, F):
        for column in (name, stamp, verdict):
            assert bag_verdict.column_color(state_name, column, picked).name() == "#123456"


def test_only_the_failed_row_gets_a_tint():
    """底纹只给不合格；其余返回 None（由样式原样画，不叠任何东西）。"""
    tint = bag_verdict.row_tint(bag_verdict.STATE_FAIL)
    assert tint is not None and 0 < tint.alpha() < 64
    assert bag_verdict.row_tint(bag_verdict.STATE_PASS) is None
    assert bag_verdict.row_tint(bag_verdict.STATE_NONE) is None
    assert bag_verdict.row_tint(bag_verdict.STATE_PENDING) is None


def test_a_pending_row_is_not_red():
    """待验证不是不合格：批量刚开始时所有行都标待验证，
    若也染红，用户会以为"这一批全坏了"。
    """
    from PyQt5.QtGui import QColor

    normal = theme.palette(theme.active_mode())["text"]
    assert bag_verdict.foreground(_verdict(ok=False), pending=True).name() == \
        QColor(normal).name()

# ==================== 行文本与列位（v6.9.1 列对齐） ====================

def test_split_row_round_trips_row_text():
    """`split_row` 必须是 `row_text` 的逆函数。

    绘制层拿到的只有 item 的 `text()`，靠它拆回三段。两边各写一份分隔符的话，
    改了一处就静默拆不开 —— 表现是整行只显示名字，而看起来像"列对齐失效"。
    """
    text = bag_verdict.row_text("vla_20261026143957", "10-26 14:39:57", "不合格")
    assert bag_verdict.split_row(text) == ("vla_20261026143957", "10-26 14:39:57", "不合格")


def test_split_row_keeps_a_malformed_row_visible_instead_of_raising():
    """拆不开的行**不抛异常**，整段当名字显示。这一步在每一次重绘里跑。"""
    assert bag_verdict.split_row("随便一段没有分隔符的字") == ("随便一段没有分隔符的字", "", "")
    assert bag_verdict.split_row("") == ("", "", "")
    assert bag_verdict.split_row("只 有 空 格")[0] == "只 有 空 格"


def test_all_labels_lists_every_possible_verdict_text():
    """判定列的宽度要按**最长**文案预留，所以这张表必须齐。"""
    assert set(bag_verdict.ALL_LABELS) == {
        bag_verdict.PASS_TEXT, bag_verdict.FAIL_TEXT,
        bag_verdict.NONE_TEXT, bag_verdict.PENDING_TEXT}


def test_column_layout_ignores_the_name_length():
    """列位的输入里**根本没有某一个具体名字** —— 名字只以"自然宽度"这个数进来。

    时间列与判定列的 x 只由行宽、名字列宽（全场最长那个）与两列的固有宽度决定；
    无论哪一行、名字多长，算出来的坐标都是同一组。
    """
    a = bag_verdict.column_layout(600, 109, 59, hpad=10, gap=16, name_w=180)
    b = bag_verdict.column_layout(600, 109, 59, hpad=10, gap=16, name_w=180)
    assert a == b
    assert a.stamp_x > a.name_x and a.verdict_x > a.stamp_x
    assert a.stamp_x == a.name_x + a.name_w + 16
    assert a.verdict_x == a.stamp_x + 109 + 16


def test_column_layout_puts_the_last_two_columns_at_the_right_edge():
    """**时间和判定靠右，左边剩下的全给名字**（v6.9.6 现场要求）。

    这条就是"从右往左排"的契约：判定列右边缘必须贴住视口右边缘（减 hpad），
    名字列吃掉中间所有空间 —— 而不是像早先那样让名字列封顶、右边留一片空白。
    """
    for total in (600, 900, 1400):
        layout = bag_verdict.column_layout(total, 109, 59, hpad=10, gap=16, name_w=200)
        assert layout.verdict_x + layout.verdict_w == total - 10, (
            f"行宽 {total}: 判定列没有贴住右边（{layout.verdict_x}+{layout.verdict_w}）")
        assert layout.name_w == total - 20 - 32 - 109 - 59, (
            f"行宽 {total}: 名字列没有吃掉剩余空间（{layout.name_w}）")
        assert layout.name_w > 200, "宽窗口下名字列应当比自然宽更宽"


def test_column_layout_never_shrinks_the_name_column():
    """窗口窄到装不下时**不压名字列**，改由整行变宽（视图横向滚动）承担。

    改口记录：v6.9.2 那版是"先压名字列、压不下就省略"，现场截图里三行名字全成了
    `vla_202610…` —— 相同前缀的行彼此无法区分，这一列等于没信息。
    现在的契约是：`name_w` 是**下限**，任何视口宽度下都不低于它；装不下就溢出。
    """
    wide = bag_verdict.column_layout(500, 109, 59, hpad=10, gap=16, name_w=400)
    narrow = bag_verdict.column_layout(200, 109, 59, hpad=10, gap=16, name_w=400)

    assert narrow.name_w == wide.name_w == 400, (narrow.name_w, wide.name_w)
    assert narrow.stamp_x == wide.stamp_x
    # 行宽需求大于视口 —— 这就是"该横向滚动"的信号
    required = bag_verdict.required_row_width(400, 109, 59, hpad=10, gap=16)
    assert required > 200
    assert required == 10 + 400 + 16 + 109 + 16 + 59 + 10, "右侧留白要算进去"


def test_column_layout_takes_the_natural_width_as_a_floor():
    """`name_w` 是**下限**：宽窗口下名字列比它宽（把右侧两列顶到右边），窄窗口下就是它。"""
    roomy = bag_verdict.column_layout(1200, 109, 59, hpad=10, gap=16, name_w=200)
    assert roomy.name_w == 1200 - 20 - 32 - 109 - 59

    tight = bag_verdict.column_layout(300, 109, 59, hpad=10, gap=16, name_w=200)
    assert tight.name_w == 200

    # 还没量过（name_w=None）：当作没有下限，剩余多少给多少
    unmeasured = bag_verdict.column_layout(1200, 109, 59, hpad=10, gap=16)
    assert unmeasured.name_w == roomy.name_w


def test_row_roles_live_after_qt_user_role():
    """行数据必须存在 `Qt.UserRole` 之后的角色号里（v6.9.2 踩过的坑）。

    低位角色是 Qt 自己的语义：0=Display、1=Decoration、2=Edit。
    实测 `setData(2, 时间)` 之后 `setText()` 会让 `data(2)` **回退返回整行文本**，
    二次渲染把整行当时间列 —— 现场看到的就是三列内容错位。
    这条断言很便宜，但它挡住的是一整类"把业务数据塞进 Qt 保留角色"的错误。
    """
    from PyQt5.QtCore import Qt

    assert bag_verdict.PATH_ROLE >= int(Qt.UserRole), bag_verdict.PATH_ROLE
    assert bag_verdict.STAMP_ROLE > int(Qt.UserRole), bag_verdict.STAMP_ROLE
    assert bag_verdict.PATH_ROLE != bag_verdict.STAMP_ROLE
