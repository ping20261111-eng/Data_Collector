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
from collections import namedtuple
from pathlib import Path
from typing import Optional

from PyQt5.QtGui import QColor

from .. import theme

#: 三段的间隔。`row_text` 与 `split_row` **共用这一个常量** ——
#: 两边各写一份"四个空格"，改了一处就会静默地拆不开（表现是整行只剩名字，
#: 而列对齐看起来"突然失效"）。
SEPARATOR = "    "

#: 四种可能的判定文案。绘制层要按**最长的那一个**预留列宽，
#: 否则新增一个更长的文案（比如「需要复查」）会把右侧挤出去而没人发现。
#: （定义在下面那几个标签常量之后 —— 这里引用不到还没赋值的名字。）

#: 三列的位置与宽度。用命名元组而不是位置元组：绘制代码里 `layout.stamp_x`
#: 比 `layout[3]` 一眼能看懂，也不会因为加一列就整体错位。
ColumnLayout = namedtuple(
    "ColumnLayout", "name_x name_w stamp_x stamp_w verdict_x verdict_w")

#: 三种判定文案。**只有这三种**（`待验证` 是批量进行中的临时态，不算判定）。
PASS_TEXT = "合格"
FAIL_TEXT = "不合格"
NONE_TEXT = "未验证"
PENDING_TEXT = "待验证"

#: 四种文案里最长的那一个决定判定列宽度（`ALL_LABELS` 见下）。
ALL_LABELS = (PASS_TEXT, FAIL_TEXT, NONE_TEXT, PENDING_TEXT)


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
    """`名字    时间    判定`。

    ## 这串文本是**数据**，不是排版结果

    它是 item 的纯文本表示：tooltip、复制、无障碍、测试都读它。
    真正的列位置由绘制层（`ui/bag_row_delegate.py`）按容器宽度算固定列位 ——
    早先的版本指望"名字 + 四个空格 + 时间"在等宽字体下自然对齐，但**名字长短
    每家不同**，时间列的起点就跟着漂，一屏几十行看起来是斜的（v6.9.1 修掉）。
    """
    return f"{name}{SEPARATOR}{stamp or '—'}{SEPARATOR}{verdict_label}"


def split_row(text: str):
    """把行文本拆回 `(名字, 时间, 判定)` —— `row_text` 的逆函数。

    ## 为什么拆不出来时不抛异常

    这段代码在**每一行的每一次重绘**里跑。将来若有人手写了一个不合格式的 item
    （或用 `addItem("随便一段字")`），抛异常会让整个列表绘制失败 ——
    而那种现象（列表一片空白）离病因很远。所以拆不开就把整段当名字、其余留空：
    这一行至少显示得出来，且问题一眼可见。

    ⚠️ 名字里本身含有四个连续空格时会被拆错。数据文件夹名是"前缀_时间戳"，
    不会出现；这条限制写在这里，改命名规则时要回来看。
    """
    parts = str(text or "").split(SEPARATOR, 2)
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def column_layout(total_width: int, stamp_w: int, verdict_w: int,
                  hpad: int, gap: int, name_cap: Optional[int] = None) -> ColumnLayout:
    """按容器宽度算三列的固定列位（**与内容无关**，这就是"列对齐"）。

    ## 为什么由容器宽度算，而不是按最长的名字补空格

    补空格有三个躲不掉的问题：得先知道"这一屏最长的名字"（逐条刷新时它会变，
    于是已画好的行要全部重排）；中文与 ASCII 的显示宽度不同，算错就整列串位；
    名字超过预留宽度时补不住，只能截断，而截断点又得自己算。

    固定列位把这些问题一次解决：时间列与判定列的 x 只由**容器宽度**和两列的
    固有宽度决定。名字多长都不影响它们；名字太长时压缩名字列并用省略号。

    Args:
        total_width: 列表视口的宽度（每个 item 在 ListMode 下就是整行宽度）。
        stamp_w: 时间列的固有宽度（等宽字体下所有时间戳同宽）。
        verdict_w: 判定列的固有宽度（按**最长**文案预留）。
        hpad: 左右留白。
        gap: 列间距。
        name_cap: 名字列的**上限**（调用方按当前最长名字量出来的）。
            给了它，宽窗口下多出来的宽度就留白，而不是摊进名字与时间的空当里 ——
            否则窗口一宽，几十行看下来像两个互不相干的列表。
            它只封顶、不兜底：窗口变窄时照常压缩名字列。

    Returns:
        `ColumnLayout(name_x, name_w, stamp_x, stamp_w, verdict_x, verdict_w)`。
        空间不够时**先压名字列**（可以截断），不挤时间与判定 —— 判定列是这一版的
        全部意义所在，把它挤出视口等于功能没做。
    """
    available = max(int(total_width) - 2 * int(hpad), 0)
    reserve = 2 * int(gap) + int(stamp_w) + int(verdict_w)
    name_w = max(available - reserve, 0)
    if name_cap is not None:
        name_w = min(name_w, max(int(name_cap), 0))
    name_x = int(hpad)
    stamp_x = name_x + name_w + int(gap)
    verdict_x = stamp_x + int(stamp_w) + int(gap)
    return ColumnLayout(name_x, name_w, stamp_x, int(stamp_w),
                        verdict_x, int(verdict_w))


def foreground(verdict: Optional[dict], pending: bool = False) -> QColor:
    """整行前景色：只有**不合格**用 danger，其余用正文色。

    颜色从主题取（`theme.palette(theme.active_mode())`），不写死色值 ——
    写死的话切主题后这一列会停留在旧配色上。
    """
    colors = theme.palette(theme.active_mode())
    bad = (not pending) and verdict is not None and not verdict["ok"]
    return QColor(colors["danger"] if bad else colors["text"])
