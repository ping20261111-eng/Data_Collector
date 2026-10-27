"""把「数据验证判定」渲染成列表行（纯渲染层，v6.9.0）。

## 为什么单独一个模块

`ui/data_view.py` 已经贴着项目 1000 行的硬上限（`test_no_single_ui_file_balloons`）。
判定列这一版往里加了约一百行就超了 —— 而超限的后果不是"难看"，是那条守卫直接变红。

拆出来的都是**不依赖控件状态**的东西：给一个 verdict，返回该显示的文本 / 提示 / 颜色。
`data_view` 那边只留"找到哪一行、把结果贴上去"这几行。

## 三种判定 + 一种临时态

| 状态 | 文案 | 颜色 |
| --- | --- | --- |
| 合格 | `合格` | 正文色 |
| 不合格 | `不合格` | **danger（整行转红）** |
| 未验证 | `未验证` | 正文色 |
| 待验证 | `待验证` | 正文色（批量验证进行中，还没轮到它） |

**只给不合格上色**：一屏几十行里如果合格也染绿，红色就不再跳眼 ——
而"哪几条要返工"正是这个列表最该一眼看出来的东西。
"""
from pathlib import Path
from typing import Optional

from PyQt5.QtGui import QColor

from .. import theme

#: 三种判定文案。**只有这三种**（`待验证` 是批量进行中的临时态，不算判定）。
PASS_TEXT = "合格"
FAIL_TEXT = "不合格"
NONE_TEXT = "未验证"
PENDING_TEXT = "待验证"


def label(verdict: Optional[dict], pending: bool = False) -> str:
    """判定列要显示的字。`verdict` 为 None 表示目录里没有报告。"""
    if pending:
        return PENDING_TEXT
    if verdict is None:
        return NONE_TEXT
    return PASS_TEXT if verdict["ok"] else FAIL_TEXT


def tooltip(verdict: Optional[dict]) -> str:
    """鼠标悬停看原因。列表只有三个字，详情必须查得到。"""
    if verdict is None:
        return ("数据验证：未验证\n"
                "这个目录里还没有验证报告。选中后点「批量验证」即可生成。")
    lines = [f"数据验证：{label(verdict)}"]
    if verdict["detail"]:
        lines.append(f"原因：{verdict['detail']}")
    lines.append(f"报告：{Path(verdict['report']).name}")
    if verdict.get("inferred"):
        lines.append("（旧格式报告，结论按「二、结论」段落推断）")
    return "\n".join(lines)


def row_text(name: str, stamp: str, verdict_label: str) -> str:
    """`名字    时间    判定`。三段用四个空格分隔（与既有列表风格一致）。

    列对齐刻意不做：名字里有中文，等宽字段要算字符显示宽度，算错的成本
    （错位、被截断）比"名字长短不齐"更难看，而这一列本来就不需要对齐。
    """
    return f"{name}    {stamp or '—'}    {verdict_label}"


def foreground(verdict: Optional[dict], pending: bool = False) -> QColor:
    """整行前景色：只有**不合格**用 danger，其余用正文色。

    颜色从主题取（`theme.palette(theme.active_mode())`），不写死色值 ——
    写死的话切主题后这一列会停留在旧配色上。
    """
    colors = theme.palette(theme.active_mode())
    bad = (not pending) and verdict is not None and not verdict["ok"]
    return QColor(colors["danger"] if bad else colors["text"])
