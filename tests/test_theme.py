"""主题层测试：设计令牌 -> QSS -> 控件状态。

这一层是"界面唯一的色值来源"，一旦漏填令牌或忘了重新抛光，表现是"颜色悄悄不对"
而不是报错，所以必须有用例钉住。
"""

import re
from pathlib import Path

import pytest

from data_collector import theme


# ---------- 令牌与 QSS ----------
def test_stylesheet_generates_for_both_modes():
    dark = theme.build_stylesheet("dark")
    light = theme.build_stylesheet("light")

    assert dark and light
    assert dark != light                      # 两套主题必须真的不同
    assert theme.palette("dark")["bg"] in dark
    assert theme.palette("light")["bg"] in light


def test_stylesheet_has_no_unsubstituted_placeholders():
    """Template 漏填一个令牌会直接抛错，但写错的 $name 可能悄悄留在输出里。"""
    for mode in ("dark", "light"):
        qss = theme.build_stylesheet(mode)
        leftover = re.findall(r"\$\{?[a-z_]+\}?", qss)
        assert not leftover, f"{mode} 主题里残留未替换的占位符: {leftover[:5]}"

def test_stylesheet_has_balanced_comments():
    """QSS 注释必须配平 —— 多一个 `*/` 会把后面的说明文字顶成"选择器"。

    实测踩过：往主题里加规则时按"规则体"锚定替换，把上一段注释的 `*/` 留在了它
    前面 —— 后面那段说明就掉到注释外面，成了非法 QSS（Qt 静默丢弃，样式看着
    "莫名其妙没生效"）。这里钉住：`/*` 与 `*/` 成对，且不允许「没开就闭」。
    """
    for mode in ("dark", "light"):
        qss = theme.build_stylesheet(mode)
        opened, closed = qss.count("/*"), qss.count("*/")
        assert opened == closed, f"{mode} 主题的 QSS 注释不配平：/* {opened} 个，*/ {closed} 个"

        depth = 0
        for i in range(len(qss)):
            if qss.startswith("/*", i):
                depth += 1
            elif qss.startswith("*/", i):
                depth -= 1
                assert depth >= 0, (
                    f"{mode} 主题里有「没开就闭」的注释，位置 {i}：{qss[max(0, i - 70):i + 20]!r}"
                )
        assert depth == 0, f"{mode} 主题里有未闭合的注释（depth={depth}）"

def test_no_selector_is_defined_twice():
    """同一个选择器不许出现两次 —— 后一条会**静默覆盖**前一条的同名属性。

    实测踩过：给导出面板加「分组标签」样式时顺手改了**共用**的 `PanelTitle`，
    于是 QSS 里出现两条 `QLabel#PanelTitle` —— 字号/颜色按后者生效，
    而 `font-family`/`font-weight` 仍从前者漏过来，**一半新一半旧**。
    这种"看着生效了、其实只生效一半"最难查，所以直接钉住"不许重名"。
    真要分场景，就换一个 objectName（例如 `GroupLabel`）。
    """
    import collections

    for mode in ("dark", "light"):
        qss = re.sub(r"/\*.*?\*/", "", theme.build_stylesheet(mode), flags=re.S)
        selectors = [s.strip() for s in re.findall(r"(?m)^([^\n{}]+?)\s*\{", qss)]
        dup = {k: v for k, v in collections.Counter(selectors).items() if v > 1}
        assert not dup, f"{mode} 主题里有重名选择器（后者会静默覆盖前者）：{dup}"
        assert len(selectors) > 150, f"选择器只解析出 {len(selectors)} 个 —— 正则可能失效了"




def test_no_hardcoded_hex_outside_token_blocks():
    """ui/ 目录里不该再出现硬编码色值 —— 全都应该走令牌。

    扫描整个 ui/ 包（原先只有单文件 ui.py，2026-09-18 拆成包后须覆盖全部子模块）。
    """
    ui_dir = Path(theme.__file__).parent / "ui"
    source = "\n".join(f.read_text(encoding="utf-8")
                        for f in sorted(ui_dir.rglob("*.py")))
    # 允许出现在注释里（说明历史行为），所以只看 setStyleSheet 调用
    offenders = [line.strip() for line in source.splitlines()
                 if "setStyleSheet" in line and "#" in line]
    assert not offenders, f"ui/ 仍有内联色值: {offenders[:3]}"


def test_light_and_dark_define_same_semantic_tokens():
    """明色主题必须覆盖全部语义令牌，否则切过去会出现"某处还是暗色"。"""
    missing = sorted(set(theme.DARK) - set(theme.LIGHT))
    assert not missing, f"明色主题缺令牌: {missing}"


def test_active_mode_follows_apply_theme():
    """自绘控件（列表 delegate、电量环）要按主题取色，靠这个全局事实而不是持有窗口引用。

    不这么做就得让它们各自存一个"宿主窗口"的引用去问当前主题，而那会形成 Python 引用环，
    实测会让之后任意一次 setStyleSheet 段错误（见 `theme.active_mode` 的说明）。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    try:
        theme.apply_theme(app, "dark")
        assert theme.active_mode() == "dark"
        theme.apply_theme(app, "light")
        assert theme.active_mode() == "light"
        # 未知模式按暗色处理，和 palette() 的口径一致
        theme.apply_theme(app, "nonsense")
        assert theme.active_mode() == "dark"
    finally:
        theme.apply_theme(app, "light")
        # 让后面所有用例都从"明色"这个已知状态开始
        assert theme.active_mode() == "light"


def test_unknown_mode_falls_back_to_dark():
    assert theme.palette("nonsense") == theme.palette("dark")
    assert theme.build_stylesheet("nonsense") == theme.build_stylesheet("dark")


# ---------- 状态 / 变体（动态属性） ----------
class _FakeWidget:
    """够用的替身：只需要 property / style().unpolish / polish / update。"""

    class _Style:
        def __init__(self):
            self.calls = []

        def unpolish(self, widget):
            self.calls.append("unpolish")

        def polish(self, widget):
            self.calls.append("polish")

    def __init__(self):
        self._props = {}
        self._style = _FakeWidget._Style()
        self.updated = 0
        self.children = []

    def property(self, name):
        return self._props.get(name)

    def setProperty(self, name, value):        # noqa: N802 (Qt 命名)
        self._props[name] = value

    def style(self):
        return self._style

    def update(self):
        self.updated += 1

    def findChildren(self, _type):            # noqa: N802
        return self.children


def test_set_state_repolishes_children():
    """QSS 里有依赖父级属性的后代选择器，只抛光父控件会出现"状态改了颜色不变"。"""
    parent = _FakeWidget()
    child = _FakeWidget()
    parent.children = [child]

    theme.set_state(parent, "danger")

    assert parent.property("state") == "danger"
    assert "polish" in parent.style().calls
    assert "polish" in child.style().calls          # 子控件也被重新抛光


def test_set_state_is_idempotent():
    widget = _FakeWidget()

    theme.set_state(widget, "ok")
    first = len(widget.style().calls)
    theme.set_state(widget, "ok")

    assert len(widget.style().calls) == first       # 状态没变就不重复抛光（每秒调用一次）


def test_set_state_tolerates_none():
    theme.set_state(None, "ok")                     # 不该抛异常
    theme.set_variant(None, "primary")


def test_set_variant_marks_button():
    widget = _FakeWidget()
    theme.set_variant(widget, "primary")
    assert widget.property("variant") == "primary"


# ---------- 下拉箭头资源 ----------
def test_arrow_asset_is_generated_and_referenced():
    """QSS 的 image 只认 url()，所以箭头必须真的落一张图并被 QSS 引用。"""
    path = theme.arrow_asset("dark")

    assert path, "箭头图标生成失败"
    assert Path(path).is_file()
    assert Path(path).stat().st_size > 0

    assert f"url({path})" in theme.build_stylesheet("dark")
    # 两套主题各一份，避免明色主题里留一个深色箭头
    assert theme.arrow_asset("light") != path


def test_arrow_bitmap_matches_qss_size():
    """位图尺寸必须与 QSS 里写的 width/height 一致。

    不一致时 Qt 会缩放那张小图 —— 现场看到的就是"箭头边缘发虚、还是看不清"，
    而这正是当初要把箭头做大一档要解决的问题（9×6 → 13×8）。
    """
    from PyQt5.QtGui import QImage

    width, height = theme.ARROW_SIZE
    image = QImage(theme.arrow_asset("light"))
    assert (image.width(), image.height()) == (width, height)

    qss = theme.build_stylesheet("light")
    assert f"width: {width}px; height: {height}px" in qss
    # 下拉区要留得下箭头（右边距不至于贴着边框）
    assert "QComboBox::drop-down { border: none; width: 26px; }" in qss


def test_editable_combo_inner_edit_has_no_padding():
    """可编辑下拉**内层**输入框的内距必须清零。

    它同时在通用 `QLineEdit` 规则的命中范围内，于是自带 7px 内距；而 QComboBox 又把
    编辑区放在**自己的** 7px 内距里 —— 两层叠加把文字压到框的下半部分
    （现场反馈"选择框内的文字不居中"，实测偏下 8px）。这条钉住清零这一条。
    """
    qss = theme.build_stylesheet("light")
    block = qss.split("QComboBox QLineEdit {", 1)[1].split("}", 1)[0]
    assert "padding: 0;" in block


def test_font_helpers():
    font = theme.mono_font(12, letter_spacing=1.0)
    assert font.family() == "IBM Plex Mono"
    assert font.pointSizeF() > 0
    assert font.letterSpacing() > 0                 # QSS 不支持 letter-spacing，只能走字体


# ============ 自绘控件：绘制异常不能拖崩进程 ============
def test_hazard_strip_stripe_geometry_stays_integral():
    """斜纹的笔宽与步长必须是整数。

    实测踩过的坑：步长写成 `width * 2.4` 后变成 float，`painter.drawLine` 抛
    TypeError，paintEvent 中途退出导致 QPainter 来不及 end()，**整个进程段错误**，
    而且崩在离病因很远的地方（测试里表现为 processEvents 崩）。这条钉住类型。
    """
    from data_collector import theme

    strip = theme.HazardStrip
    assert isinstance(strip.STRIPE_WIDTH, int)
    assert isinstance(strip.STRIPE_STEP, int)
    # 周期必须大于笔宽，否则实纹与空纹等宽，在 10px 高的条里会糊成一片
    assert strip.STRIPE_STEP > strip.STRIPE_WIDTH


def test_selection_states_use_the_accent_color():
    """「选中」态一律用强调色（青碧），不用 Qt 默认的蓝、也不是"浅灰"。

    现场："选中的选项要是蓝绿色，和启动等其他颜色一致"。只给底色不给字色时，
    浅青底 + 深色字看起来接近"浅灰"，会被读成"没选中"。

    **文字色用 `accent_on_soft` 而不是 `accent`**：后者压在浅底上只有 4.32:1（亮）/
    4.46:1（暗），不到 AA 的 4.5:1，见 `test_popup_selected_text_meets_wcag_aa`。

    用正则取**规则体**而不是 `split("}")`：样式表里同一段注释也提到过选择器名，
    按字符串切会把注释当规则体，断言就虚了。
    """
    import re

    qss = theme.build_stylesheet("light")
    palette = theme.palette("light")

    menu_rule = re.search(r"QMenu::item:selected\s*\{([^}]*)\}", qss)
    assert menu_rule, "样式表里没有 QMenu::item:selected 规则"
    body = menu_rule.group(1)
    assert f"background-color: {palette['accent_soft']}" in body
    assert f"color: {palette['accent_on_soft']}" in body
    assert "blue" not in body and "highlight" not in body     # 没退回 Qt 默认选中色

    combo_rule = re.search(r"QComboBox QAbstractItemView\s*\{([^}]*)\}", qss)
    assert combo_rule, "样式表里没有 QComboBox QAbstractItemView 规则"
    body = combo_rule.group(1)
    assert f"selection-background-color: {palette['accent_soft']}" in body
    assert f"selection-color: {palette['accent_on_soft']}" in body


def test_stylesheet_rules_are_not_swallowed_by_comments():
    """注释少一个收尾符会把后面整段规则吃掉，而**没有报错**。

    这次的现场表现是"改了样式却完全没生效"：弹出层那一大段的收尾符被改注释时带走了，
    于是 QMenu / 下拉 / 滚动条的所有规则都成了注释，菜单退回通用 QWidget 底色。
    写成用例：把每条规则的 `{` 找出来，检查它不在注释里。
    """
    for mode in ("dark", "light"):
        qss = theme.build_stylesheet(mode)
        depth, index, in_comment, swallowed = 0, 0, False, []
        while index < len(qss):
            if not in_comment and qss.startswith("/*", index):
                in_comment = True
                index += 2
                continue
            if in_comment and qss.startswith("*/", index):
                in_comment = False
                index += 2
                continue
            char = qss[index]
            if in_comment:
                if char == "{":
                    swallowed.append(qss[max(0, index - 40):index + 10])
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        assert not in_comment, f"{mode} 主题有没闭合的注释"
        assert depth == 0, f"{mode} 主题大括号不配平（多/少 {depth} 个）"
        assert not swallowed, f"{mode} 主题里这些规则被注释吃了：{swallowed[:2]}"


def test_popup_layer_uses_its_own_tokens():
    """弹出层（菜单 / 下拉 / 提示）是独立的一层：自己的底色、描边、圆角、内距。

    现场："机器人控制的二级菜单和其他的下拉菜单太丑，重新设计一下，包括圆角，底色 分隔等"。
    这一层与卡片分开的理由：弹层浮在卡片**之上**，白卡片上开一个白弹层时，
    只有专属的描边（popup_line）能说明"这是一层浮起来的东西"。
    """
    import re

    for mode in ("dark", "light"):
        qss = theme.build_stylesheet(mode)
        palette = theme.palette(mode)
        assert palette["popup"] and palette["popup_line"]

        menu = re.search(r"\nQMenu\s*\{([^}]*)\}", qss).group(1)
        assert f"background-color: {palette['popup']}" in menu
        assert f"border: 1px solid {palette['popup_line']}" in menu
        assert f"border-radius: {theme.RADIUS['r2']}px" in menu      # 圆角
        assert f"padding: {theme.POPUP['pad']}px" in menu

        item = re.search(r"QMenu::item\s*\{([^}]*)\}", qss).group(1)
        assert f"border-radius: {theme.RADIUS['r1']}px" in item      # 选中块的圆角
        assert f"margin: {theme.POPUP['item_gap']}px 2px" in item    # 行间留缝
        assert f"min-width: {theme.POPUP['min_w']}px" in item

        combo = re.search(r"QComboBox QAbstractItemView\s*\{([^}]*)\}", qss).group(1)
        assert f"background-color: {palette['popup']}" in combo      # 与菜单同一张卡片
        assert f"border-radius: {theme.RADIUS['r2']}px" in combo
        assert f"padding: {theme.POPUP['pad']}px" in combo

        separator = re.search(r"QMenu::separator\s*\{([^}]*)\}", qss).group(1)
        assert f"background: {palette['popup_line']}" in separator
        assert "margin:" in separator                                # 分隔线两侧内缩（分组感）
        assert "QMenu QScrollBar" in qss                             # 弹层里的滚动条另配色


def test_popup_selected_text_meets_wcag_aa():
    """选中态文字必须过 AA：4.5:1（正文）—— 用令牌算，不靠眼估。

    `accent` 压在 `accent_soft` 上只有 4.32:1（亮）/ 4.46:1（暗），差一点；
    `accent_on_soft` 是"同一色相、换一档明度"的解，6.44:1 / 6.33:1。
    """

    def luminance(color: str, backdrop: str) -> float:
        """把 #rrggbb 或 rgba(r,g,b,a) 折算成相对亮度（rgba 先与底合成）。"""
        value = color.strip()
        if value.startswith("rgba"):
            parts = [float(item) for item in value[value.index("(") + 1:-1].split(",")]
            red, green, blue, alpha = parts
            base = [int(backdrop[i:i + 2], 16) for i in (1, 3, 5)]
            red = red * alpha + base[0] * (1 - alpha)
            green = green * alpha + base[1] * (1 - alpha)
            blue = blue * alpha + base[2] * (1 - alpha)
        else:
            red, green, blue = (int(value[i:i + 2], 16) for i in (1, 3, 5))

        channels = []
        for channel in (red, green, blue):
            scaled = channel / 255
            channels.append(scaled / 12.92 if scaled <= 0.04045
                            else ((scaled + 0.055) / 1.055) ** 2.4)
        return (0.2126 * channels[0] + 0.7152 * channels[1]
                + 0.0722 * channels[2])

    for mode in ("dark", "light"):
        palette = theme.palette(mode)
        # 浅底先压在弹层底色上（暗色的 accent_soft 是半透明的）
        background = palette["accent_soft"]
        if background.startswith("rgba"):
            parts = [float(item) for item in
                     background[background.index("(") + 1:-1].split(",")]
            base = [int(palette["popup"][i:i + 2], 16) for i in (1, 3, 5)]
            blended = "#" + "".join(
                "%02X" % round(parts[channel] * parts[3] + base[channel] * (1 - parts[3]))
                for channel in (0, 1, 2))
        else:
            blended = background
        lighter = max(luminance(palette["accent_on_soft"], blended),
                      luminance(blended, palette["popup"]))
        darker = min(luminance(palette["accent_on_soft"], blended),
                     luminance(blended, palette["popup"]))
        ratio = (lighter + 0.05) / (darker + 0.05)
        assert ratio >= 4.5, f"{mode} 主题的选中态对比度只有 {ratio:.2f}:1"


def test_popup_rounding_can_be_switched_off(monkeypatch):
    """弹层圆角可以关掉（目标机缺合成器时的退路）：`DC_POPUP_ROUNDING=0`。

    真圆角要求弹层窗口透明；没有合成器的机器上开了透明会看到四角发黑 ——
    留一个环境变量开关，现场不用改代码。
    """
    monkeypatch.delenv(theme.POPUP_ROUNDING_ENV, raising=False)
    assert theme.popup_rounding_enabled() is True           # 默认开

    for value in ("0", "false", "NO", "off"):
        monkeypatch.setenv(theme.POPUP_ROUNDING_ENV, value)
        assert theme.popup_rounding_enabled() is False
    monkeypatch.setenv(theme.POPUP_ROUNDING_ENV, "1")
    assert theme.popup_rounding_enabled() is True


def test_popup_rounding_tolerates_a_dead_filter():
    """上一个过滤器已经是"已销毁的包装对象"时，再装一次不许炸。

    全量跑时每个用例换一个 QApplication，旧的那个一销毁，`_POPUP_ROUNDER.parent()` 就会抛
    `RuntimeError: wrapped C/C++ object of type _PopupRounder has been deleted` ——
    那是在**启动路径**上（`MainWindow.__init__` → `apply_theme`），一炸就是几十个用例集体失败。
    """
    from PyQt5.QtCore import QObject
    from PyQt5.QtWidgets import QApplication

    from data_collector import theme

    # 这个用例不依赖 qapp 夹具（test_theme 里没有它）：有就用现成的，没有就自己建
    app = QApplication.instance() or QApplication([])

    try:
        from PyQt5 import sip
    except ImportError:                       # PyQt5 打包方式不同时的退路
        import sip                            # type: ignore[no-redef]

    dead = QObject()
    sip.delete(dead)
    theme._POPUP_ROUNDER = dead
    theme.apply_popup_rounding(app)           # 不许抛
    assert theme._POPUP_ROUNDER is not dead
    theme.apply_popup_rounding(app)           # 同一个 app 再装一次：直接返回，不重复装
    first = theme._POPUP_ROUNDER
    theme.apply_popup_rounding(app)
    assert theme._POPUP_ROUNDER is first


def test_combo_box_itself_carries_the_popup_selection_colors():
    """下拉**弹层**的选中色要同时给组合框自己一份（不只是视图）。

    弹层的 delegate 用的是 `QComboBox::viewOptions()` 造出来的调色板 —— 那是**组合框的**
    调色板，不是视图的。只给 `QComboBox QAbstractItemView` 设 `selection-*` 时，
    选中行会被画成组合框底色那种浅灰（离屏实测 #F4F6F5），看着像"没选中"。
    给 `QComboBox` 自己也设一份，弹层调色板里的 highlight 才是强调色（实测 #E3F1F2）。
    """
    import re

    for mode in ("dark", "light"):
        qss = theme.build_stylesheet(mode)
        palette = theme.palette(mode)
        rule = re.search(r"\nQComboBox\s*\{([^}]*)\}", qss)
        assert rule, "样式表里没有单独的 QComboBox 规则"
        body = rule.group(1)
        assert f"selection-background-color: {palette['accent_soft']}" in body
        assert f"selection-color: {palette['accent_on_soft']}" in body
