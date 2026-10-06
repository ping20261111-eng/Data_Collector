#!/usr/bin/env python3
"""量「启动遥操作」对话框里用户名下拉的排版：文字是否垂直居中、箭头多大/多显眼。

为什么要有这个脚本：这类"看着不对"的反馈（"文字不居中""下拉不够明显"）靠肉眼下结论
容易吵——量一次就有数了。**每个方案必须单独开一个进程**：同一进程里反复
`setStyleSheet` 再量，结果会随"第几次"变（踩过）。

用法：
    python3 tests/manual/probe_combo_alignment.py light
    python3 tests/manual/probe_combo_alignment.py dark
    # 复现"改前"的样子做 A/B（把内层内距加回去）：
    python3 tests/manual/probe_combo_alignment.py light "QComboBox QLineEdit { padding: 7px 10px; }"

输出：控件的行/列量测 + 两张放大图（存到 `$TMPDIR` 或 /tmp）：
    combo_<mode>[_before|_after].png   控件本体 3× 放大
    dialog_<mode>[_before|_after].png  整个对话框
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from data_collector import theme  # noqa: E402
from data_collector.ui.dialogs import TeleopDialog  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "light"
# 第二个参数：附加 CSS（用于在同一套量测代码下复现"改前"的样子，做 A/B）
EXTRA = sys.argv[2] if len(sys.argv) > 2 else ""
SHOT_DIR = Path(os.environ.get("PROBE_SHOT_DIR", tempfile.gettempdir()))

# 箭头位图是源码里的资源，改不了 QSS 来复现 —— 用这个开关临时用旧尺寸（例如 9x6），
# 才能和现在的尺寸放在同一套量测下比。
_arrow_env = os.environ.get("PROBE_ARROW")
if _arrow_env:
    theme.ARROW_SIZE = tuple(int(v) for v in _arrow_env.lower().split("x"))

app = QApplication([])
theme.apply_theme(app, MODE)
if EXTRA:
    app.setStyleSheet(app.styleSheet() + "\n" + EXTRA)

dlg = TeleopDialog(users=["yyt", "aaa", "bbb"], current="yyt")
dlg.show()
for _ in range(4):
    app.processEvents()

combo = dlg.combo
img = combo.grab().toImage()
w, h = img.width(), img.height()

# 落图：人眼复核比只看数字可靠
suffix = "_before" if (EXTRA or _arrow_env) else "_after"
combo.grab().scaled(w * 3, h * 3).save(str(SHOT_DIR / f"combo_{MODE}{suffix}.png"))
dlg.grab().save(str(SHOT_DIR / f"dialog_{MODE}{suffix}.png"))


def lum(x, y):
    """按最大通道取亮度：浅灰边框的亮度也 < 140，用明度判文字会把边框算进来（踩过）。"""
    c = img.pixelColor(x, y)
    return max(c.red(), c.green(), c.blue())


def row_is_border(y):
    vals = [lum(x, y) for x in range(3, w - 3, 5)]
    return bool(vals) and sum(1 for v in vals if v < 215) >= len(vals) * 0.9


border_rows = [y for y in range(h) if row_is_border(y)]
top = min(border_rows) if border_rows else 0
bottom = max(border_rows) if border_rows else h - 1
if bottom <= top:
    # 只扫到上边框：两层内距叠加时下边框会被压得更淡，扫不出整行"边框色"
    # （"改前"就是这种形态）—— 那时若沿用 bottom=top，行区间会变成空集，
    # 量出来是"没扫到文字像素"，看着像界面问题，其实是探针自己的坑。
    bottom = h - 1

# 文字行：避开左侧光标（x≥26）与右侧箭头/边框。
# 阈值取 160 而不是 120：提示字是 `muted` 中灰（最大通道 109），但 CJK 小字号下笔画
# 普遍带抗锯齿，核心像素才到 109、其余在 120~160 —— 用 120 会漏掉大半行（踩过：
# 判成"没扫到文字像素"）。左右边框（x=0/347）与光标（x≈13）已被窗口排除。
TEXT_THRESHOLD = 160
TEXT_X0, TEXT_X1 = 26, max(27, w - 46)
text_rows = [y for y in range(top + 1, bottom)
             if any(lum(x, y) < TEXT_THRESHOLD for x in range(TEXT_X0, TEXT_X1))]

# 箭头：右侧 42..8px 之间、明显暗于底色**且是灰色**的像素。
# 为什么要加"灰色"这一条：聚焦态的外框是 accent（#0E7C86，最大通道 134）也满足
# "暗于底色"，只按亮度筛会把**右外框**算成箭头 —— 量出来就是"箭头 11 x 34 像素"
# 这种明显不对的数。箭头用的是 muted（灰，通道差 < 30），用饱和度就能把两者分开。
bg = lum(w // 2, top + 2)


def _arrow_pixel(x, y):
    c = img.pixelColor(x, y)
    channels = (c.red(), c.green(), c.blue())
    return max(channels) < bg - 25 and (max(channels) - min(channels)) < 30


arrow_px = [(x, y) for y in range(top + 1, bottom)
            for x in range(max(0, w - 42), w - 6)
            if _arrow_pixel(x, y)]

print(f"===== {MODE}{'（附加 CSS：' + EXTRA.strip() + '）' if EXTRA else ''} =====")
print(f"控件尺寸            : {w} x {h}")
print(f"外框 rows           : top={top} bottom={bottom} (高 {bottom - top + 1})")
if text_rows:
    t0, t1 = min(text_rows), max(text_rows)
    box_mid, text_mid = (top + bottom) / 2, (t0 + t1) / 2
    print(f"文字 rows           : {t0}..{t1}（高 {t1 - t0 + 1}）")
    print(f"框中心 {box_mid:.1f} / 文字中心 {text_mid:.1f} → 偏移 {text_mid - box_mid:+.1f}px"
          f"（正=文字偏下）")
else:
    print("文字 rows           : （没扫到文字像素）")

if arrow_px:
    xs = [p[0] for p in arrow_px]
    ys = [p[1] for p in arrow_px]
    print(f"箭头 bbox           : {max(xs) - min(xs) + 1} x {max(ys) - min(ys) + 1} 像素, "
          f"x={min(xs)}..{max(xs)}")
    print(f"箭头最深处亮度 {min(lum(x, y) for x, y in arrow_px)}（底色 {bg}）"
          f"→ 对比差 {bg - min(lum(x, y) for x, y in arrow_px)}")
    print(f"箭头像素数          : {len(arrow_px)}")
else:
    print("箭头 bbox           : （右侧窗口内没扫到比底色深的像素）")
print(f"放大图              : {SHOT_DIR}/combo_{MODE}{suffix}.png")
