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
    """**只有不合格**用 danger 色。

    一屏几十行里如果合格也染色，红色就不跳了 —— 而"哪几条要返工"
    正是这个列表最该一眼看出来的东西。
    """
    from PyQt5.QtGui import QColor

    colors = theme.palette(theme.active_mode())
    danger, normal = QColor(colors["danger"]).name(), QColor(colors["text"]).name()

    assert bag_verdict.foreground(_verdict(ok=False)).name() == danger
    assert bag_verdict.foreground(_verdict(ok=True)).name() == normal
    assert bag_verdict.foreground(None).name() == normal


def test_a_pending_row_is_not_red():
    """待验证不是不合格：批量刚开始时所有行都标待验证，
    若也染红，用户会以为"这一批全坏了"。
    """
    from PyQt5.QtGui import QColor

    normal = theme.palette(theme.active_mode())["text"]
    assert bag_verdict.foreground(_verdict(ok=False), pending=True).name() == \
        QColor(normal).name()
