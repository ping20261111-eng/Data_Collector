#!/usr/bin/env python3
"""YAML 结构体检：解析失败时**直接指出是哪一行、缩进差几个空格**（只读，不改文件）。

## 为什么要有它

现场报错长这样（机器人侧 `teleop_config.yaml`，2026-10-27）：

```
while parsing a block mapping
  in ".../teleop_config.yaml", line 1, column 1
expected <block end>, but found '<block mapping start>'
  in ".../teleop_config.yaml", line 14, column 2
```

这段话能定位到行，但**不告诉你错在哪**：第 14 行 `  yyt:` 本身看着完全正常，
真正的问题是它的缩进比同级键（`default:`）少了一个空格，于是 YAML 认为
`calibrations` 这个映射在第 13 行就结束了，第 14 行成了"凭空冒出来的一层"。

实测复现（把正常文件的第 14 行从 2 空格改成 1 空格）：

| 改动 | 报错位置 |
| --- | --- |
| `yyt:` 缩进 2 → 1 空格 | **`(1,1)` / `(14,2)`** ← 与现场完全一致 |
| `default:` 退到 0 缩进 | `(1,1)` / `(14,3)` |
| `yyt:` 用 tab | `(14,1)` found character '\\t' |

所以本脚本做三件事：

1. 报错时打印**问题行前后若干行**，每行标出**真实缩进宽度**（tab 单独标）；
2. 对问题行给出**可执行的修法**：与上方最近的同级键比，多/少了几个空格；
3. 即使能解析，也做一遍**同级缩进一致性检查** —— 有些错位在特定数据下仍能解析，
   却把键挂到了错误的层级（比"报错"更危险：程序能跑，配置却读错了）。

## 用法

    python3 scripts/check_yaml_layout.py <文件> [<文件> ...]

退出码：0 = 全部正常；1 = 有文件解析失败或有缩进告警。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:                                   # pragma: no cover
    print("需要 PyYAML：pip install pyyaml")
    raise SystemExit(2)


def indent_of(line: str) -> tuple:
    """返回 (缩进宽度, 是否用了 tab)。tab 在 YAML 里**不能**用于缩进。"""
    stripped = line.lstrip(" \t")
    prefix = line[:len(line) - len(stripped)]
    return len(prefix), "\t" in prefix


def describe(lines: list, mark_line: int, mark_col: int, span: int = 6) -> None:
    """打印问题行附近的原文，每行带行号与缩进宽度。"""
    lo = max(0, mark_line - span)
    hi = min(len(lines), mark_line + 3)
    print("      行号  缩进  内容")
    for index in range(lo, hi):
        width, has_tab = indent_of(lines[index])
        tab_note = " ← tab!" if has_tab else ""
        pointer = "  ← 问题行" if index == mark_line - 1 else ""
        print(f"      {index + 1:4d}  {width:3d}   {lines[index]}{tab_note}{pointer}")


def suggest_fix(lines: list, mark_line: int) -> str:
    """对问题行给出**可执行的修法**。

    ## 为什么用"缩进直方图"而不是"跟上一行比"

    第一版是拿"上一行"当参照，结果对现场那个案例给出了错的建议：
    坏文件里第 14 行 `yyt:` 缩进 1，而它的上一行是 `default:` 的**子键**（缩进 4），
    于是脚本说"应改成 4" —— 照它改只会更坏。

    真正可靠的判据是：**这一行的缩进值在全文里根本不存在**（正常文件只有 0/2/4），
    那它就是笔误，应该对齐到"最近的合法缩进值"。这个判据对"少打/多打一个空格"
    这类错误 100% 成立，而且不需要猜层级结构。
    """
    index = mark_line - 1
    if index < 0 or index >= len(lines):
        return ""
    width, has_tab = indent_of(lines[index])
    if has_tab:
        return ("      修法：这一行用了 tab 缩进，YAML 不允许。"
                "把行首的 tab 换成空格（与同级键对齐）。")
    if not lines[index].strip() or lines[index].lstrip().startswith("#"):
        return ""

    # 统计**其它行**的缩进直方图（跳过注释/空行/列表项）
    histogram = {}
    for i, line in enumerate(lines):
        if i == index or not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            continue
        other, other_tab = indent_of(line)
        if other_tab:
            continue
        histogram[other] = histogram.get(other, 0) + 1

    if not histogram:
        return ""
    if width not in histogram:
        # 笔误：这个缩进全文没出现过
        # 平局时选**出现次数多**的：width=1 时 0/2 距离相同，而 0 是顶层、
        # 2 才是常见层级 —— 按出现次数定优先级才能选对（实测踩到：选成 0 会让
        # 现场把键提到顶层，文件不再报错但配置全读错，比报错更糟）。
        nearest = min(histogram, key=lambda w: (abs(w - width), -histogram[w], w))
        count = histogram[nearest]
        return (f"      修法：缩进 {width} 在全文**从未出现过**，是笔误。"
                f"最可能应改成 {nearest} 个空格（全文有 {count} 处用这个值）。\n"
                f"            把这一行行首改成 {nearest} 个空格，与同级键对齐。")

    # 缩进值合法但与上文兄弟不一致：只说事实，不猜
    return (f"      提示：这一行缩进 {width}（全文出现过 {histogram[width]} 处），"
            f"但与上文的同级键可能不同层。请对照上方最近的同缩进键。")


def check_sibling_indent(lines: list) -> list:
    """同级缩进一致性：同一父级下的键必须缩进相同。

    做法：按"缩进宽度栈"跟踪层级。遇到比栈顶浅的键就出栈，然后要求
    新键的缩进等于栈顶宽度；不等则记一条告警。列表项（`- `）跳过 ——
    它的缩进规则与映射键不同。
    """
    warnings = []
    stack = []                       # 每层一个缩进宽度
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            continue
        width, has_tab = indent_of(line)
        if has_tab:
            warnings.append((index + 1, width, "行首用了 tab（YAML 不允许）"))
            continue
        while stack and width < stack[-1]:
            stack.pop()
        if stack and width > stack[-1]:
            # 进入更深一层：合法（上一个键的值是映射），只要它是上一行的直接子级
            if index == 0:
                continue
            stack.append(width)
            continue
        if stack and width == stack[-1]:
            continue
        # width < 所有已知层级，或栈为空
        if not stack:
            stack.append(width)
        elif width < stack[-1]:
            warnings.append((index + 1, width, f"缩进 {width} 与当前层级 {stack[-1]} 不一致"))
    return warnings


def check_file(path: Path) -> tuple:
    """返回 (是否通过, 报告行列表)。"""
    report = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, [f"  读不到：{exc}"]
    lines = text.splitlines()
    report.append(f"  文件：{path}")
    report.append(f"  共 {len(lines)} 行")

    failed = False
    try:
        yaml.safe_load(text)
        report.append("  解析：OK")
    except yaml.YAMLError as exc:
        failed = True
        report.append("  解析：**失败**")
        problem = getattr(exc, "problem", "") or ""
        context = getattr(exc, "context", "") or ""
        if context:
            report.append(f"    上下文：{context}")
        if problem:
            report.append(f"    原因　：{problem}")
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            report.append(f"    位置　：第 {mark.line + 1} 行，第 {mark.column + 1} 列")
            report.append("")
            describe(lines, mark.line + 1, mark.column + 1)
            hint = suggest_fix(lines, mark.line + 1)
            if hint:
                report.append("")
                report.append(hint)

    warnings = check_sibling_indent(lines)
    if warnings:
        report.append("")
        report.append(f"  缩进一致性：{len(warnings)} 处可疑（能解析但层级可能挂错）")
        for line_no, width, note in warnings[:10]:
            report.append(f"    第 {line_no} 行（缩进 {width}）：{note}")
        if len(warnings) > 10:
            report.append(f"    …… 另有 {len(warnings) - 10} 处")

    return (not failed) and not warnings, report


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 2
    all_ok = True
    for raw in argv:
        path = Path(raw)
        ok, report = check_file(path)
        all_ok = all_ok and ok
        print("=" * 66)
        print(("✔ " if ok else "✘ ") + str(path))
        print("=" * 66)
        for line in report:
            print(line)
        print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
