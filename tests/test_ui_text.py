"""界面文案的静态检查：不许出现 markdown 的 `**`。

`QLabel` / `QMessageBox` / 日志区（`QTextEdit`）都不渲染 markdown —— 文案里写 `**着重**`
现场看到的就是一堆星号。本项目已经在弹窗提示、标签说明、日志三处各踩过一次，所以做成
静态用例：以后谁再往控件文本里写 `**`，CI 直接红，不靠人记得。

用 AST 而不是正则：控件文本常常是多行字符串拼接或 f-string，
逐行正则既会漏（拼接的后半行没有 `QLabel(`）也会错（docstring 里的 `**` 是给开发者看的，
必须放过）。

**扫描范围是整个 `data_collector/` 包，不只是 `ui/`**（v6.3.0 扩的）：
日志文案的一大半来自 `motion_runner.py` 的 `_note(...)`，而它不在 `ui/` 下 ——
只扫 `ui/` 时那 14 条带星号的判据文案一条都看不见，实拍里却明明白白在日志区。
"""
import ast
from pathlib import Path

import pytest

#: 整个包都要扫：日志文案的来源不只在 `ui/`（`motion_runner.py` 就是一例）
PKG_DIR = Path(__file__).resolve().parent.parent / "data_collector"

#: 只认"把文本交给用户的那些调用"，文档串 / 注释不在其中
TEXT_METHODS = {
    "setText", "setToolTip", "setPlaceholderText", "setInformativeText",
    "setWindowTitle", "setStatusTip", "setWhatsThis",
}
TEXT_CLASSES = {"QLabel", "QMessageBox", "QInputDialog", "append", "show_error"}
#: 各页 / 各执行器自己的"往日志区写一行"的口子（`#Console` 那个只读文本框）。
#: **v6.3.0 补**：这些口的实参是普通字符串 / f-string，而星号真正落进的是它们内部那次
#: `self.result.append(...)` / `self.progress(...)` —— AST 只看得到内部那一次调用的实参
#: （是个 f-string，星号不在里面），所以 `_report("运动方式：**非实时模式**…")` 从缝里
#: 漏了过去，现场在日志里看到一对星号。落点名单必须覆盖"最后一跳"。
#: `_note` = `MotionRunner` 的口子（它把每个判据拼成人话传给 `progress`）。
TEXT_SINKS = {"_report", "_note"}


def _literal_parts(node) -> str:
    """把字符串常量 / f-string 里的字面量部分拼起来（变量插值部分忽略）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_parts(part) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_parts(node.left) + _literal_parts(node.right)
    return ""


def _is_text_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return (func.attr in TEXT_METHODS or func.attr in TEXT_SINKS
                or (isinstance(func.value, ast.Name) and func.value.id in TEXT_CLASSES))
    if isinstance(func, ast.Name):
        return func.id in TEXT_CLASSES
    return False


@pytest.mark.parametrize("path", sorted(PKG_DIR.rglob("*.py")),
                         ids=lambda p: str(p.relative_to(PKG_DIR)))
def test_widget_text_has_no_markdown(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_text_call(node):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            text = _literal_parts(arg)
            if "**" in text:
                rel = path.relative_to(PKG_DIR)
                offenders.append(f"{rel}:{node.lineno}  {text.strip()[:70]}")
    assert offenders == [], "控件文本里出现了 markdown 星号（不会渲染，现场只看到星号）：\n" \
                            + "\n".join(offenders)


def test_report_is_a_covered_text_sink():
    """`_report` 必须在文本落点清单里（v6.3.0 补的，这里单独钉一下别被删掉）。

    为什么它天生看不见：`_report` 的实参是普通字符串 / f-string，而星号真正落进的
    是它内部那次 `self.result.append("[stamp] " + line)`。所以 AST 扫 `append` 那一次调用
    永远扫不到星号 —— 把 `_report` 从 `TEXT_SINKS` 里拿掉，
    `test_widget_text_has_no_markdown` 依然全绿，而"日志里出现一对星号"这条又会漏回去。
    """
    source = ("class D:\n"
              "    def f(self):\n"
              "        self._report(\"运动方式：**非实时模式**（点位目标）\")\n")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)]
    assert calls, "样例源码没解析出调用"
    assert all(_is_text_call(node) for node in calls), \
        "`_report` 不在文本落点里 —— 日志里的星号守不住"
    assert "**" in _literal_parts(calls[0].args[0])
