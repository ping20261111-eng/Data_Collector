"""把「数据验证判定」渲染成列表行（纯渲染层，v6.9.0）。

## 为什么单独一个模块

`ui/data_view.py` 已经贴着项目 1000 行的硬上限（`test_no_single_ui_file_balloons`）。
判定列这一版往里加了约一百行就超了 —— 而超限的后果不是"难看"，是那条守卫直接变红。

拆出来的都是**不依赖控件状态**的东西：给一个 verdict，返回该显示的文本 / 提示 / 颜色。
`data_view` 那边只留"找到哪一行、把结果贴上去"这几行。

## 四种文案 + 三档配色（v6.9.5 改口）

| 状态 | 文案 | 判定列 | 名字列 | 行底 |
| --- | --- | --- | --- | --- |
| 合格 | `合格` | **ok_text（绿）** | 正文色 | 不涂 |
| 不合格 | `不合格` | **danger_text（红）** | **danger_text（红）** | 淡红（10% danger） |
| 未验证 / 待验证 | `未验证` / `待验证` | 正文色（黑） | 正文色 | 不涂 |

**v6.9.0 那版是「只给不合格上色」**，理由是"一屏里合格也染绿，红色就不再跳眼"。
现场 2026-10-28 明确要求三档可辨（合格绿 / 不合格红 / 未验证黑）—— 于是改回三色，
但把**红色的饱和度提上来**（`danger_text` 比 `danger` 深一档）保住"不合格最先跳眼"：
红绿两色在白底上的对比度分别是 5.98:1 与 5.56:1，都过 WCAG AA。

⚠️ `ok` / `danger` 是**面**色（按钮、急停），压白底只有 3.39:1 / 4.38:1，拿它写小字
不过 AA。文字一律用 `ok_text` / `danger_text` —— 这两组键就是为此分出来的。
"""
from collections import namedtuple
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor

from .. import theme

#: 列表行里存"这一行代表哪个目录"。**必须用 `Qt.UserRole` 及以上的角色** ——
#: 低位角色是 Qt 自己的语义（0=Display、1=Decoration、2=Edit），拿它们存业务数据
#: 会与控件行为打架（v6.9.2 踩到：`data(2)` 是 EditRole，而 `setText()` 之后
#: `data(2)` 会**回退成整行文本**，于是二次渲染把整行文本当成了时间列）。
PATH_ROLE = int(Qt.UserRole)
#: 行里存的"已格式化的录制时间"文本。同上，只在 UserRole 之后取号。
STAMP_ROLE = int(Qt.UserRole) + 1

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

#: 三列的**语义名**。取色函数按它区分"这一格该用什么颜色"——
#: 用字符串而不是 0/1/2：调用处 `column_color(state, COLUMN_VERDICT)` 一眼看得懂。
COLUMN_NAME = "name"
COLUMN_STAMP = "stamp"
COLUMN_VERDICT = "verdict"

#: 三档状态。**从行文本的第三段反推**（`state()`）—— 这样绘制层不需要再多存一个字段，
#: 也就不可能出现"存的状态与显示的字不一致"。
STATE_PASS = "pass"
STATE_FAIL = "fail"
STATE_NONE = "none"
STATE_PENDING = "pending"

_STATE_BY_LABEL = {PASS_TEXT: STATE_PASS,
                   FAIL_TEXT: STATE_FAIL,
                   NONE_TEXT: STATE_NONE,
                   PENDING_TEXT: STATE_PENDING}


def state(verdict_label: str) -> str:
    """从判定文案反推状态。认不出的文案按**未验证**处理（不该乱标绿/红）。"""
    return _STATE_BY_LABEL.get(str(verdict_label or "").strip(), STATE_NONE)


def state_of(verdict: Optional[dict], pending: bool = False) -> str:
    """从 verdict 字典取状态（给不经过文本的调用方用，如 `foreground`）。"""
    return state(label(verdict, pending))


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
                  hpad: int, gap: int, name_w: Optional[int] = None) -> ColumnLayout:
    """按**内容**算三列的固定列位（v6.9.5 起不再压缩名字）。

    ## 为什么由内容算，而不是按容器宽度摊

    补空格有三个躲不掉的问题：得先知道"这一屏最长的名字"（逐条刷新时它会变，
    于是已画好的行要全部重排）；中文与 ASCII 的显示宽度不同，算错就整列串位；
    名字超过预留宽度时补不住，只能截断，而截断点又得自己算。

    固定列位把这些问题一次解决：三列的 x 只由**几个宽度**决定，与某一行无关。

    ## v6.9.6 为什么改成"从右往左排"

    现场要求：**判定与时间靠右，左边剩下的地方全部给包名**。这不只是好看 ——
    它把"名字够不够宽"从"扣掉两列后剩多少"变成了"整个左边的宽度"，宽窗口下
    名字列能宽出一倍，`rosbag2_20261027144500` 这种长名字自然就放得下了。

    所以位置从**右边**推：`verdict_x = total - hpad - verdict_w`，
    `stamp_x = verdict_x - gap - stamp_w`，名字列占 `hpad .. stamp_x-gap`。
    等价写法是 `name_w = total - reserve`，但 `name_w` 还有个**下限** = 名字的自然宽度：
    窗口窄到装不下时宁可整行变宽（视图横向滚动），也不截断名字。

    Args:
        total_width: 列表视口的宽度（行宽）。
        stamp_w: 时间列的固有宽度（按真实时间戳 `mm-dd HH:MM:SS` 量）。
        verdict_w: 判定列的固有宽度（按**最长**文案预留）。
        hpad: 左右留白。
        gap: 列间距。
        name_w: 名字的**自然宽度**（最长名字 + 余量）。它是最小值，不是固定值 ——
            宽窗口下名字列会更宽（把右侧两列顶到右边去）。
            为 `None` 时（还没量过，例如空列表）不做下限限制。

    Returns:
        `ColumnLayout(name_x, name_w, stamp_x, stamp_w, verdict_x, verdict_w)`。
    """
    total = max(int(total_width), 0)
    reserve = 2 * int(hpad) + 2 * int(gap) + int(stamp_w) + int(verdict_w)
    leftover = max(total - reserve, 0)
    natural = 0 if name_w is None else max(int(name_w), 0)
    name_w = max(leftover, natural)
    name_x = int(hpad)
    stamp_x = name_x + name_w + int(gap)
    verdict_x = stamp_x + int(stamp_w) + int(gap)
    return ColumnLayout(name_x, name_w, stamp_x, int(stamp_w),
                        verdict_x, int(verdict_w))


def required_row_width(name_w: int, stamp_w: int, verdict_w: int,
                       hpad: int, gap: int) -> int:
    """一行**完整显示**三列所需的宽度（不截断任何一列）。

    这个数交给 delegate 的 `sizeHint`：视口比它窄时列表出现横向滚动条，
    而不是把名字截成 `vla_202610…`。反过来，视口更宽时行宽仍取视口宽 ——
    否则行底纹会在右边空出一截，看起来像列表被切断了。
    """
    return (2 * int(hpad) + max(int(name_w), 0) + 2 * int(gap)
            + int(stamp_w) + int(verdict_w))


def foreground(verdict: Optional[dict], pending: bool = False) -> QColor:
    """**整行**的前景色（item 的 `ForegroundRole`）：不合格用红，其余用正文色。

    绘制层不再直接用它上色（三列各有各的颜色，见 `column_color`），但这条要留着：
    它是"这一行是什么状态"在一处能被程序读到的表示（无障碍、复制、以及既有测试）。

    颜色从主题取（`theme.palette(theme.active_mode())`），不写死色值 ——
    写死的话切主题后这一列会停留在旧配色上。
    """
    colors = theme.palette(theme.active_mode())
    bad = state_of(verdict, pending) == STATE_FAIL
    return QColor(colors["danger_text"] if bad else colors["text"])


#: 不合格行的底纹透明度（0~255）。10% 左右：能一眼分出"这一行有问题"，
#: 又不至于把行底压成粉色块、抢掉文字的对比度（红字压上去仍 5.11:1）。
ROW_TINT_ALPHA = 26


def column_color(state_name: str, column: str,
                 selected: Optional[QColor] = None) -> QColor:
    """某一格该用的文字色。三态三色，**按列分别取**。

    | 列 | 合格 | 不合格 | 未验证 / 待验证 |
    | --- | --- | --- | --- |
    | 名字 | 正文 | **红** | 正文 |
    | 时间 | 正文 | 正文 | 正文 |
    | 判定 | **绿** | **红** | 正文（黑） |

    时间列**不跟着变红**：它是用来对时间线的，跟着染色只会让整行糊成一片红；
    而且三种状态下时间都是"中性事实"，本身不含判定含义。

    Args:
        state_name: `STATE_*` 之一。
        column: `COLUMN_NAME` / `COLUMN_STAMP` / `COLUMN_VERDICT`。
        selected: 选中态该用的颜色（通常是样式的 `highlightedText`）。
            给了它就**直接用它** —— 选中底色是主题强调色，红绿压上去都不好读。
    """
    if selected is not None:
        return QColor(selected)
    colors = theme.palette(theme.active_mode())
    if state_name == STATE_FAIL and column in (COLUMN_NAME, COLUMN_VERDICT):
        # 名字与判定一起红：名字红是"该返工的是这一条"，判定红是"为什么"。
        # 时间列**不在**这里 —— 见上面那张表。
        return QColor(colors["danger_text"])
    if state_name == STATE_PASS and column == COLUMN_VERDICT:
        return QColor(colors["ok_text"])
    return QColor(colors["text"])


def row_tint(state_name: str) -> Optional[QColor]:
    """行底纹：**只有不合格**加一层淡红，其余返回 None（交样式原样画）。

    用 danger 色叠 10% 透明度，而不是新加一个 `#FBE9E9` 之类的色值 ——
    这样亮/暗两套主题各自算出正确的底，不会出现"暗色主题下淡红底刺眼"。
    """
    if state_name != STATE_FAIL:
        return None
    color = QColor(theme.palette(theme.active_mode())["danger"])
    color.setAlpha(ROW_TINT_ALPHA)
    return color
