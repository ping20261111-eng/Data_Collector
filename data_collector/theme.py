"""界面主题：设计令牌与 QSS。

**视觉语言：B 方案「自然洁净 · 临床白」**（设计稿在
`<工作区>/ui-proposals/B_自然洁净临床白.html`）。场景是布草洗涤工厂，借"洁净"
做隐喻：白卡片 + 冷灰描边 + 靠近"干净的水"的青碧强调色 + 大圆角 + 药丸徽章
+ 圆形红色急停钮。这里的每个令牌与设计稿一一对应，**是界面唯一的色值/字阶/
间距来源**——`ui/` 目录里不允许再出现硬编码色值。要调色、改字阶、改圆角，只改这一个文件。

三个刻意遵守的纪律（来自 B 方案）：
1. 强调色（青碧）只用于"当前该按的那一个主操作"；红/黄/绿是语义色，只表达状态，
   不参与"引导点击"。两套颜色职责分离，避免整个界面到处是彩色按钮。
2. 红只属于急停与失败：急停钮是红色实心圆钮，其余危险操作只用红描边。
3. Qt 的 QSS 不支持 CSS 变量，所以这里用 Template 把令牌插值成静态字符串。
   因此改主题必须走 build_stylesheet()，不能手写零散 setStyleSheet。
"""

import os
from pathlib import Path
from string import Template
from typing import Dict

from PyQt5.QtCore import QEvent, QObject, Qt, QPoint
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygon
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QFrame, QMenu, QWidget

from .core import PROJECT_ROOT, logger

# ============ 设计令牌 ============
# 视觉语言来自 <工作区>/ui-proposals/B_自然洁净临床白.html（B 方案）：
# 临床白卡片 + 青碧强调色 + 大圆角 + 药丸徽章 + 圆形急停。
# 场景是布草洗涤工厂，借"洁净"做隐喻：白卡片、冷灰描边、靠近"干净的水"的青碧。
DARK: Dict[str, str] = {
    # B 的暗色变体：深墨绿灰底 + 提亮的青碧（暗底上 #0e7c86 会发闷）
    "bg": "#0F1513",
    "surface": "#16201E",
    "surface2": "#1D2A27",
    "surface3": "#26332F",
    "line": "rgba(230, 238, 234, 0.10)",
    "line_strong": "rgba(230, 238, 234, 0.22)",

    "text": "#E6EEEA",
    "muted": "#8FA39E",
    "muted2": "#6B7D78",

    "accent": "#35B3BD",
    "accent_ink": "#06201F",
    "accent_soft": "rgba(53, 179, 189, 0.16)",
    # 浅底（accent_soft）上的文字色：直接压 accent 只有 4.46:1，不到 AA 的 4.5:1
    # —— 暗底上要往上提亮一档
    "accent_on_soft": "#5FD3DC",

    # 弹出层（菜单 / 下拉列表 / 悬停提示）：浮在卡片**之上**的一层。
    # 暗底上"越高越亮"，所以弹层用 surface2 这一档，与卡片（surface）分开。
    "popup": "#1D2A27",
    "popup_line": "rgba(230, 238, 234, 0.26)",

    "ok": "#4CC084",
    "ok_soft": "rgba(76, 192, 132, 0.16)",
    "warn": "#E0A63A",
    "warn_soft": "rgba(224, 166, 58, 0.16)",
    "danger": "#E56B6B",
    "danger_deep": "#7D2B2B",
    "danger_soft": "rgba(229, 107, 107, 0.16)",

    # 急停钮（B 的红色圆形按钮：白描边 + 径向渐变）
    "estop_face": "#C93838",
    "estop_face_hi": "#FF7070",
    "estop_ring": "#0F1513",
}

LIGHT: Dict[str, str] = {
    "bg": "#E8ECEA",
    "surface": "#FFFFFF",
    "surface2": "#F4F6F5",
    "surface3": "#E3E8E6",
    "line": "#E3E8E6",
    "line_strong": "#D3DAD7",

    "text": "#1D2B28",
    "muted": "#5F716D",
    "muted2": "#93A3A0",

    "accent": "#0E7C86",
    "accent_ink": "#FFFFFF",
    "accent_soft": "#E3F1F2",
    # 浅底（accent_soft）上的文字色：直接压 accent 只有 4.32:1，不到 AA 的 4.5:1
    # —— 换成同一色相、深一档的青碧，6.44:1
    "accent_on_soft": "#0A5F67",

    # 弹出层（菜单 / 下拉列表 / 悬停提示）：浮在卡片**之上**的一层。
    # 亮色系里"越高越白"，所以弹层是纯白，描边要比发丝线（line_strong）再实一点 ——
    # 白弹层落在白卡片上时，只有这条线能说明"这是一层浮起来的东西"。
    "popup": "#FFFFFF",
    "popup_line": "#C9D2CF",

    "ok": "#2F9E63",
    "ok_soft": "#E6F4EC",
    "warn": "#D9930D",
    "warn_soft": "#FBF1DE",
    "danger": "#D64545",
    "danger_deep": "#A02D2D",
    "danger_soft": "#FBE9E9",

    # 急停钮（B 的红色圆形按钮：白描边 + 径向渐变）
    "estop_face": "#C93838",
    "estop_face_hi": "#FF7070",
    "estop_ring": "#FFFFFF",
}

# 字阶（1.25 比例）与间距阶梯，与 tokens.css 一致
TYPE_SCALE = {
    "fs_micro": 11, "fs_sm": 12, "fs_base": 14, "fs_md": 17,
    "fs_lg": 22, "fs_read": 34, "fs_pillar": 44,
}
SPACING = {"sp1": 4, "sp2": 8, "sp3": 12, "sp4": 16, "sp5": 24, "sp6": 32, "sp7": 48}

# ================= 全局 UI 缩放（v6.4.16） =================
#
# 现场是大屏（1080P 起）且站位较远，1440 宽的基准尺寸在那里显得小、点不准。
# `configure_scale()` 在**启动时**按屏幕算一个系数，原地放大字号与控件高度。
#
# ⚠️ 三条纪律（都是实测/约束推出来的，改之前先读）：
#
# 1. **间距（SPACING）与圆角（RADIUS）不缩放。**
#    实测：只放字号时姿态页 1021 → 1173；连间距一起放 → **1264**（多 91px）。
#    而现场同时要求姿态页"纵向排列缩减一点"—— 两个要求方向相反，所以间距保持原值：
#    放大的是"看得清、点得中"（字号 + 触摸目标），不是"版面松紧"。
# 2. **RADIUS 不缩放**：药丸令牌按"配套的控件高度"命名（`pill_27/30/32`，值恒为
#    半高），`test_every_pill_radius_fits_its_height` 会去核 `半径 == 名字里的高度/2`。
#    跟着缩会让令牌名与值脱钩。代价：放大后药丸更像圆角矩形（半径 ≤ 半高仍然成立）。
# 3. **`setFixedHeight` 等 41 处写死尺寸不动**：多数是装饰件（1px 分隔线、12px 箭头、
#    30px 芯片），跟着放会把它们撑变形。放大靠"字号 + 下面这三个控件高度令牌"传下去。
SCALE = 1.0

#: 缩放前的字阶原值。`configure_scale` 每次都从这份**原值**重算，
#: 不在上一次结果上再乘 —— 否则反复调用会累积舍入误差。
_BASE_TYPE_SCALE = dict(TYPE_SCALE)

#: 控件高度令牌（原先散在 QSS 里写死成 `min-height: 22px` 这类）。
#:
#: 这三个值决定"按键/输入框有多高"，是触摸目标的主体；与字号一起缩放。
#: 名字里的语义：`ch_main` 操作区主按钮、`ch_start` 启动/关闭那类更大的钮、`ch_input` 输入控件。
CONTROL_HEIGHT = {"ch_main": 22, "ch_start": 26, "ch_input": 20}
_BASE_CONTROL_HEIGHT = dict(CONTROL_HEIGHT)

#: 缩放基准屏宽：窗口在这块屏幕上按 1.0 渲染。
#:
#: 取 1440 而不是 860（最小宽），是为了让**开发机与既有的 54 条尺寸守卫**都落在 1.0
#: ——守卫断言的是"某个按钮比另一个高"这类关系，基准一动就全要重写，而且重写完
#: 就再也守不住"某个按钮是不是变矮了"。缩小方向一律不缩（下限 1.0）。
BASE_SCREEN_WIDTH = 1440
SCALE_MAX = 1.5


def configure_scale(scale: float) -> None:
    """设定全局缩放系数（原地更新令牌，**必须在 `apply_theme` 之前调用**）。

    原地 `update()` 而不是重新赋值：全项目 129 处 `SP[...]` / `TYPE_SCALE[...]`
    拿的都是同一个 dict 的引用，原地改即可全部生效，不用改调用点。
    """
    global SCALE
    SCALE = max(1.0, min(SCALE_MAX, float(scale)))
    TYPE_SCALE.update({k: max(1, round(v * SCALE)) for k, v in _BASE_TYPE_SCALE.items()})
    CONTROL_HEIGHT.update(
        {k: max(1, round(v * SCALE)) for k, v in _BASE_CONTROL_HEIGHT.items()})


def px(value: int) -> int:
    """把**按 1.0 设计稿写的像素**换算成当前缩放下的像素（v6.7.1）。

    ## 为什么需要它（现场 2026-10-21：「很多界面文字显示不全」）

    `configure_scale()` 只放大了**字号与控件高度令牌**，而窗口最小宽度、固定列宽
    仍然是写死的 860 / 26 / 12。1920 宽的屏上 `scale_for_screen()` 给 1.33：
    字号大 33%，窗口却还是 860 —— 姿势页三列 20 行的「目标角度」输入被压到 0，
    查看数据的 6 个按钮每个少 23px，夹爪的力矩框少一半。

    所以**凡是"按设计稿量出来的横竖尺寸"都要过这里**；装饰性的 1px 分隔线、
    12px 箭头这类不承载文字的（下面 `scaled()` 也在用的）可以自己决定要不要跟。

    用例在 1.0 缩放上跑（未配置缩放），值不变 —— 既有尺寸守卫不受影响。
    """
    return max(1, round(value * SCALE))


def ui_scale() -> float:
    """当前缩放系数（自绘/定高控件用它算自己的尺寸，见 `posture_widgets`）。"""
    return SCALE


def scale_for_screen(width: int, override=None) -> float:
    """按屏幕可用宽度算缩放系数：`clamp(宽/1440, 1.0, 1.5)`。

    1080P（1920）→ **1.33**；1440 及以下 → 1.0（不缩）；4K 封顶 1.5
    （再大就不是"看得清"的问题，而是这个按 860 最小宽设计的版面会散架）。

    `override` 是 `app_config.yaml` 的 `ui_scale`（留空 / 非法 / ≤0 → 自动算）。
    做成纯函数是为了能被用例直接喂数值验证，不必去造一块屏幕。
    """
    if override not in (None, ""):
        try:
            value = float(override)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return max(1.0, min(SCALE_MAX, value))
    try:
        width = int(width)
    except (TypeError, ValueError):
        return 1.0
    if width <= 0:
        return 1.0
    return max(1.0, min(SCALE_MAX, width / BASE_SCREEN_WIDTH))
# B 方案是"卡片"语言：圆角明显大于工业风的发丝边框（原 2/4/7）。
# pill 给徽章与幽灵按钮用，做成全圆角。
#
# `r0` 是**给 15px 级小方块**的（复选框指示器），它**不跟 r1 走**：
# r1=8 是卡片的圆角，放到 15px 见方的指示器上半径就超过了一半，
# Qt 会把它直接画成**圆形**（实测）—— 而复选框该是圆角方块、单选才是圆。
# B 方案把 r1 从 2 放大到 8 时，漏了小控件这一处。
#
# ⚠️ **药丸半径必须与控件高度配套** —— Qt 的 QSS 对 `border-radius` 有一条硬规则：
#
#     半径 > ⌊控件高度 / 2⌋  →  **整条声明被忽略**（不是截断到一半）→ 画成方角
#
#   实测（`render(..., DrawChildren)` 量四角，逐高度扫最大可用半径）：
#
#     高度   24  26  28  30  31  32  34  36
#     上限   12  13  14  15  15  16  17  18      ← 恰好是 ⌊高度/2⌋
#
#   所以**一个常量不可能对所有高度都是药丸**：同一个 16，在 h=32 的幽灵钮上是
#   完美胶囊，在 h=27 的徽章和 h=31 的芯片上却直接退回方角。
#   （v6.4.5 把 `pill` 从 999 改成 16 时没意识到这条，于是"改了还是方角"——
#     现场就在导出目标芯片上看到了这个方角。）
#
#   → 因此按**高度分档命名**：令牌名里的数字就是它配套的控件高度，
#     半径 = ⌊那个高度/2⌋。守卫 `test_every_pill_radius_fits_its_height`
#     拿真实控件高度复核，再出这种事会当场报红。
RADIUS = {
    "r0": 4, "r1": 8, "r2": 12, "r3": 16,
    "pill_27": 13,   # 徽章 / 急停状态标签（实测高 27）
    "pill_30": 15,   # 导出目标芯片（代码里 setFixedHeight(30)，半径恰为半高）
    "pill_32": 16,   # 幽灵钮 / 紧凑钮 / 危险钮 / 急停复位（实测高 32）
}

#: 弹出层（菜单 / 下拉列表 / 悬停提示）的**布局尺寸**：全站弹层共用这几个数，只写一次。
#: 圆角、底色、描边走既有令牌（`r2` / `popup` / `popup_line`），这里只放尺寸。
POPUP = {
    "pad": 6,          # 弹层四周内距：条目与弹层边缘之间留出"呼吸"
    "item_vpad": 7,    # 条目上下内距（合计行高约 31px，鼠标与触控都稳）
    "item_hpad": 14,   # 条目左右内距
    "item_gap": 1,     # 条目之间（以及条目与内边缘）的留缝，配合圆角做"块状"选中
    # 菜单的选中块高度由上面那条 `padding` 决定（7+7 + 行高 17 ≈ 31px）。
    # **下拉的行高不在这里**：`QListView` 的行高由 Qt 按字体度量算（14px 字 ≈ 22px），
    # QSS 的 `::item`（padding / min-height）控不住 —— min-height 给大了会把弹层压塌成
    # 一条（实测 245×32），给小的没反应。两个弹层因此行高不同（31 / 22），但**节奏对齐**：
    # 下拉开 `item_gap` 的缝，行距比例与菜单一致。要严格等高只能给每个下拉装 delegate，
    # 那条路试过：`QComboBox` 弹出时会换掉视图的 delegate，值不稳，遂放弃。
    "min_w": 168,      # 弹层最小宽度：短条目（"戳点"）不至于窄成一条
    "sep_v": 6,        # 分隔线上下留白
    "sep_h": 12,       # 分隔线左右内缩，与条目文字左边缘对齐
}

# 中文走系统栈（思源黑体/苹方/雅黑），拉丁与数字走等宽
FONT_SANS = '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
FONT_MONO = '"IBM Plex Mono", "DejaVu Sans Mono", "Noto Sans Mono CJK SC", Consolas, monospace'


def palette(mode: str = "dark") -> Dict[str, str]:
    """取某套主题的色板（程序里需要直接拿到色值时用，不要另写字面量）。"""
    return dict(LIGHT if str(mode).lower() == "light" else DARK)


# ============ 下拉箭头图标 ============
# QSS 的 image 只认 url()，不支持 data URI，所以只能落一张小图。
# 为什么不沿用"用 border 拼三角形"的老写法：在部分平台样式下它会退化成一个小方块
# （实测就是），而箭头缺失会让下拉框看起来像纯文本框。落一张 PNG 最稳。
#
# 尺寸从 9×6 提到 11×7：现场反馈"下拉菜单可以再明显一点"。原尺寸在 36px 高的框里
# 只占一条小尖角，加上它在亮色主题里用的是 `muted`（中灰）而不是黑，扫视时基本看不见。
# 放大一级后不用换色就够辨认（见交付说明里的量测），也就不会让箭头抢过正文。
_ARROW_CACHE: Dict[str, str] = {}

#: 箭头位图尺寸（QSS 里的 width/height 必须与它一致，否则会被缩放、边缘发虚）
ARROW_SIZE = (13, 8)


def arrow_asset(mode: str = "dark") -> str:
    """生成（并缓存）下拉箭头图标，返回绝对路径；生成失败返回空串。

    刻意用 QImage 而不是 QPixmap：QPixmap 必须在 QGuiApplication 之后才能构造，
    而 build_stylesheet() 有可能在 QApplication 之前被调用（例如工具脚本）。
    """
    key = "light" if str(mode).lower() == "light" else "dark"
    cached = _ARROW_CACHE.get(key)
    if cached and Path(cached).is_file():
        return cached

    directory = PROJECT_ROOT / "logs" / "theme"
    path = directory / f"combo_arrow_{key}.png"
    color = QColor(palette(key)["muted"])

    width, height = ARROW_SIZE
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, False)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    # 顶边留 1px、左右各留 0/1px 的空隙：与 36px 高的输入框对齐后视觉上居中
    painter.drawPolygon(QPolygon([QPoint(0, 1), QPoint(width - 1, 1),
                                  QPoint(width // 2, height - 1)]))
    painter.end()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not image.save(str(path), "PNG"):
            return ""
    except OSError:
        return ""
    _ARROW_CACHE[key] = str(path)
    return str(path)


# ============ QSS ============
_QSS = Template("""
/* ================= 基础 ================= */
QWidget {
    background-color: ${bg};
    color: ${text};
    font-family: $font_sans;
    font-size: ${fs_base}px;
}
QMainWindow, QDialog { background-color: ${bg}; }
QLabel { background: transparent; }
/* 主界面里"按需展开的一行"（如导出目标的挂载点行）：它是个裸 QWidget，
   会被上面的全局 `QWidget` 规则涂上 `${bg}` —— 而它坐在**白色卡片**里，
   于是显示成一条与卡片不同色的灰带。装饰性容器一律透明，由父卡片提供底色。 */
QWidget#InlineRow { background: transparent; }
QToolTip {
    background-color: ${popup}; color: ${text};
    border: 1px solid ${popup_line}; padding: 6px 10px;
    border-radius: ${r2}px;
    font-size: ${fs_sm}px;
}

/* ================= 顶栏：会话身份（B：一张白卡片） ================= */
QFrame#Topbar {
    background-color: ${surface};
    border: 1px solid ${line};
    border-radius: ${r3}px;
}
QLabel#Brand {
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    font-weight: 600;
    color: ${text};
}
QLabel#FactLabel {
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    color: ${muted2};
}
QLabel#FactValue {
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    color: ${muted};
}
/* 顶栏/控制台头部的状态文字：用语义色表达"运行中 / 启动中 / 未运行" */
QLabel#FactValue[state="ok"]     { color: ${ok}; }
QLabel#FactValue[state="warn"]   { color: ${warn}; }
QLabel#FactValue[state="danger"] { color: ${danger}; }
QFrame#Divider { background-color: ${line}; border: none; }

/* ================= 急停卡（B：白卡片 + 红色圆钮） ================= */
/* B 方案里急停不再用工业警示斜纹，而是"白色卡片上的一颗红色圆钮"：
   红底 + 白描边 + 径向渐变，靠颜色和形状成为全界面最醒目的东西。 */
QFrame#Pillar {
    background-color: ${surface};
    border: 1px solid ${line};
    border-radius: ${r3}px;
}
QFrame#Pillar[state="engaged"] {
    border: 2px solid ${danger};
    background-color: ${danger_soft};
}
QPushButton#EstopFace {
    background-color: qradialgradient(cx:0.35, cy:0.30, radius:0.85,
                                     fx:0.35, fy:0.30,
                                     stop:0 ${estop_face_hi}, stop:0.72 ${estop_face});
    border: 4px solid ${estop_ring};
    border-radius: ${r3}px;
    font-family: $font_mono;
    font-size: ${fs_pillar}px;
    font-weight: 700;
    color: #FFFFFF;
    padding: 0px;
}
QPushButton#EstopFace:hover { border-color: ${danger_soft}; }
QPushButton#EstopFace:pressed { background-color: ${danger_deep}; }
QPushButton#EstopRelease {
    background-color: ${surface2};
    border: 1px solid ${line_strong};
    border-radius: ${pill_32}px;
    color: ${muted};
    padding: 10px 12px;
    font-size: ${fs_sm}px;
}
QPushButton#EstopRelease:hover { color: ${danger}; border-color: ${danger}; }
QPushButton#EstopRelease:disabled { color: ${muted2}; border-color: ${line}; }
/* 急停状态做成药丸徽章（B 的 badge）：状态靠底色+字色一起表达 */
QLabel#EstopState {
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    color: ${muted2};
    background-color: ${surface2};
    border: 1px solid ${line};
    border-radius: ${pill_27}px;
    padding: 4px 10px;
}
QLabel#EstopState[state="engaged"] {
    color: #FFFFFF;
    background-color: ${danger};
    border-color: ${danger};
    font-weight: 600;
}
QLabel#EstopState[state="released"] {
    color: ${ok};
    background-color: ${ok_soft};
    border-color: ${ok_soft};
}
QLabel#EstopState[state="warn"] {
    color: ${warn};
    background-color: ${warn_soft};
    border-color: ${warn_soft};
}

/* ================= 面板容器（B：白卡片 + 冷灰描边） ================= */
QFrame#Panel {
    background-color: ${surface};
    border: 1px solid ${line};
    border-radius: ${r3}px;
}
QFrame#PanelHead {
    background-color: transparent;
    border: none;
    border-bottom: 1px solid ${line};
}
QLabel#PanelTitle {
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    font-weight: 500;
    color: ${muted};
}

/* ================= 生命体征：发丝网格 ================= */
QFrame#Vitals {
    background-color: ${line};
    border: 1px solid ${line};
    border-radius: ${r2}px;
}
QFrame#Vital {
    background-color: ${surface};
    border: none;
    border-left: 3px solid transparent;
}
QFrame#Vital[state="ok"]     { border-left: 3px solid ${ok}; }
QFrame#Vital[state="warn"]   { border-left: 3px solid ${warn}; }
QFrame#Vital[state="danger"] { border-left: 3px solid ${danger}; }
QFrame#Vital[state="none"],
QFrame#Vital[state="unknown"] { border-left: 3px solid ${line_strong}; }

QLabel#VitalLabel {
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    color: ${muted2};
}
QLabel#VitalReadout {
    font-family: $font_mono;
    font-size: ${fs_read}px;
    font-weight: 500;
    color: ${text};
}
QFrame#Vital[state="ok"]    QLabel#VitalReadout { color: ${ok}; }
QFrame#Vital[state="warn"]  QLabel#VitalReadout { color: ${warn}; }
QFrame#Vital[state="danger"] QLabel#VitalReadout { color: ${danger}; }
QFrame#Vital[state="none"]  QLabel#VitalReadout,
QFrame#Vital[state="unknown"] QLabel#VitalReadout { color: ${muted}; }

QLabel#VitalNote {
    font-size: ${fs_sm}px;
    color: ${muted};
}
/* 通用次级提示文字（对话框里的"就绪 / 远程空间"等） */
QLabel#Hint { color: ${muted2}; font-size: ${fs_sm}px; }
/* 反馈态：提示文字也会被当成"结果回执"用（打点面板的"已记 #3 …"），
   没有这三条时 set_state() 是空转 —— 现场看到的就是"操作成功 / 失败一个颜色"。 */
QLabel#Hint[state="ok"]     { color: ${ok}; }
QLabel#Hint[state="warn"]   { color: ${warn}; }
QLabel#Hint[state="danger"] { color: ${danger}; }

/* ================= 姿势控制：角度读数（大号等宽） ================= */
/* 现场是**站在机器旁边扫一眼**就要读到数，不是读句子 —— 所以角度单独一套字：
   轴名小号次级色、角度大号加粗右对齐、"°" 只做更小的脚标（每行都重复一个 ° 会抢视线）。
   `state="changed"` = 目标与当前不一样，用强调色标出来（一眼看出这次会动哪几个轴）；
   `state="unknown"` = 读不到真实角度，字号降级并变灰 —— **不显示 0**（0° 是个合法角度，
   用它冒充"读不到"会让人以为机器正立着）。 */
QLabel#AxisName { font-family: $font_mono; font-size: ${fs_sm}px; color: ${muted}; }
QLabel#AxisUnit { font-family: $font_mono; font-size: ${fs_micro}px; color: ${muted2}; }
QLabel#AxisAngle {
    font-family: $font_mono;
    font-size: ${fs_md}px;
    font-weight: 600;
    color: ${text};
}
QLabel#AxisAngle[state="changed"] { color: ${accent}; }
QLabel#AxisAngle[state="unknown"] { color: ${muted2}; font-weight: 400; }
/* 一行轴（方案 C 的"铺开"）：选中那行给个浅底，让键盘焦点有归属 */
QFrame#AxisRow { border: 1px solid transparent; border-radius: ${r1}px; }
QFrame#AxisRow[state="selected"] {
    border-color: ${accent_soft};
    background-color: ${surface2};
}

/* 目标值格：**不编辑时它就是一个右对齐的等宽读数**（现场意见：「单轴调节做得像当前角度
   那样，双击再改」）。所以只读态必须彻底去掉输入框长相（底色 / 描边 / 内距都归零），
   否则 20 行里会出现 20 个"小方框"，与隔壁的当前角度读数对不齐。 */
QLineEdit#AxisTarget {
    font-family: $font_mono;
    font-size: ${fs_md}px;
    font-weight: 600;
    color: ${text};
    background: transparent;
    border: 1px solid transparent;
    border-radius: ${r1}px;
    padding: 0 4px;
}
QLineEdit#AxisTarget[state="changed"] { color: ${accent}; }
/* 静止态**保持纯读数**（上面那三条的用意），只在鼠标移上去时给一点点"这里能点"的暗示
   （v6.3.0）。刻意不做虚线框/底色：那会让 20 行里重新冒出 20 个小方框 —— 正是 v6.0.0
   现场把滑杆与 ± 按钮拿掉时要避免的东西。悬停态零成本，移开就消失。 */
QLineEdit#AxisTarget[editing="false"]:hover { border-color: ${line_strong}; }
QLineEdit#AxisTarget[editing="true"] {
    background: ${surface};
    border-color: ${accent};
    color: ${text};
}
QLineEdit#AxisTarget:disabled { color: ${muted2}; font-weight: 400; }

/* 预设卡：现场要求"还是用卡片形式" —— 卡片能把"这条预设会做什么"一起说清，
   一行小按钮只塞得下名字。点卡片 = 运动，点「改」= 编辑。 */
QFrame#PresetCard {
    border: 1px solid ${line};
    border-radius: ${r2}px;
    background: ${surface};
}
QFrame#PresetCard:hover { border-color: ${line_strong}; background: ${surface2}; }
/* 键盘焦点（v6.3.0）：预设卡可 Tab 聚焦，必须有一条"焦点在这儿"的可见指示
   （WCAG 2.4.7）。用强调色描边 + 浅底，与 hover 的灰描边区分开；`:focus` 在
   Qt 里对"鼠标点击获得的焦点"同样成立，所以点过之后那张卡会保持高亮 —— 这是
   想要的效果（它就是键盘回车会作用的那一张）。 */
QFrame#PresetCard:focus {
    border-color: ${accent};
    background: ${accent_soft};
}
QFrame#PresetCard[state="selected"] { border-color: ${accent}; background: ${accent_soft}; }
QFrame#PresetCard[state="warn"] { border-color: ${warn}; }
QLabel#PresetName { font-size: ${fs_sm}px; font-weight: 600; color: ${text}; }
QLabel#PresetSum { font-family: $font_mono; font-size: ${fs_micro}px; color: ${muted}; }
QLabel#PresetSource { font-family: $font_mono; font-size: ${fs_micro}px; color: ${accent_on_soft}; }
/* 预设卡上的「编辑」。`ghost-compact` 的 8px 内距在 34px 宽里会把两个字裁掉，所以这一个
   小按钮把内距清零、字号降到 micro —— 它只是"打开编辑器"的入口，别抢卡片的视线。
   （v6.3.0：按钮从单字「改」改成「编辑」，宽度 30 → 34；中文界面里的单字按钮像缩写。） */
QPushButton#PresetEdit {
    padding: 0;
    font-size: ${fs_micro}px;
    color: ${muted};
}
QPushButton#PresetEdit:hover { color: ${accent}; border-color: ${accent}; }

/* 模块使能条（并入姿势控制页）：一整条浅底，两行分别放躯干 / 双臂。
   上使能是这一页的前置条件，所以它贴着卡头、不做成第五张卡。 */
QFrame#ModuleStrip {
    border: 1px solid ${line};
    border-radius: ${r2}px;
    background: ${surface2};
}

/* 分区小标题：用来代替"面板里再套一层 GroupBox 框线"。
   B 是卡片语言，卡片里再画框就是框中框 —— 层次交给标题的字号与留白表达。 */
QLabel#SectionTitle {
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    font-weight: 500;
    color: ${muted};
}
/* 分区标题右侧的弱提示（"共 4 个" / 空状态说明） */
QLabel#SectionNote { font-size: ${fs_micro}px; color: ${muted2}; }
/* 字段名（"新标签" / "备注"）：比正文弱一档，让输入内容成为视觉主体 */
QLabel#FieldLabel { font-size: ${fs_sm}px; color: ${muted2}; }

/* 药丸徽章：一行里并列表达"是什么 + 什么状态"（药剂名、点数、任务名）。
   同一形状语言贯穿急停状态、打点面板与版本对话框。 */
QLabel#Badge {
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    color: ${muted};
    background-color: ${surface2};
    border: 1px solid ${line};
    border-radius: ${pill_27}px;
    padding: 4px 10px;
}
QLabel#Badge[state="accent"] {
    color: ${accent_on_soft}; background-color: ${accent_soft}; border-color: ${accent_soft};
}
QLabel#Badge[state="ok"] {
    color: ${ok}; background-color: ${ok_soft}; border-color: ${ok_soft};
}
QLabel#Badge[state="warn"] {
    color: ${warn}; background-color: ${warn_soft}; border-color: ${warn_soft};
}
QLabel#Badge[state="danger"] {
    color: ${danger}; background-color: ${danger_soft}; border-color: ${danger_soft};
}

/* 顶栏第二行的状态说明（数据新鲜度 / 为什么没数据） */
QLabel#StatusNote {
    font-size: ${fs_micro}px;
    color: ${muted2};
    padding: 0px 2px;
}
QLabel#StatusNote[state="warn"] { color: ${warn}; }
/* 电量条形表：远距离看不清数字时靠形状判断 */
QProgressBar#BatteryBar {
    background-color: ${surface3};
    border: none;
    border-radius: 2px;
    max-height: 6px;
    min-height: 6px;
    text-align: center;
}
QProgressBar#BatteryBar::chunk { background-color: ${ok}; border-radius: 2px; }
QProgressBar#BatteryBar[state="warn"]::chunk { background-color: ${warn}; }
QProgressBar#BatteryBar[state="danger"]::chunk { background-color: ${danger}; }

/* ================= 按钮 ================= */
QPushButton {
    background-color: ${surface2};
    border: 1px solid ${line_strong};
    border-radius: ${r2}px;
    color: ${text};
    padding: 9px 16px;
    font-size: ${fs_base}px;
}
QPushButton:hover { background-color: ${surface3}; border-color: ${muted2}; }
QPushButton:pressed { background-color: ${surface}; }
QPushButton:disabled { color: ${muted2}; border-color: ${line}; background-color: ${surface}; }

/* 启动程序：整个界面唯一的主操作。
   给最实的强调色 + 更大的尺寸与字号 —— 它是这个工具的第一步动作，
   现场闭着眼也该能按对。区别于 primary（录制/上传这类阶段性主操作），
   全屏只会有一个 launch。 */
QPushButton[variant="launch"] {
    background-color: ${accent};
    border: 1px solid ${accent};
    border-radius: ${r2}px;
    color: ${accent_ink};
    font-size: 15px;
    font-weight: 700;
    padding: 14px 28px;
}
QPushButton[variant="launch"]:hover { border-color: ${text}; }
QPushButton[variant="launch"]:pressed {
    background-color: ${accent_soft}; color: ${accent}; border-color: ${accent};
}
QPushButton[variant="launch"]:disabled {
    background-color: ${surface}; border-color: ${line}; color: ${muted2};
}

/* 打点：打点面板里唯一的动作，且要盲按 —— 实心强调色 + 大一号内距。
   与 launch 的区别：launch 是"整个工具的第一步"，这个是"每记一个点按一次"的高频动作，
   所以尺寸同档但更宽一些，方便在面板里一眼找到、一次按准。 */
QPushButton[variant="point"] {
    background-color: ${accent};
    border: 1px solid ${accent};
    border-radius: ${r2}px;
    color: ${accent_ink};
    font-size: 16px;
    font-weight: 700;
    padding: 13px 30px;
    min-width: 116px;
}
QPushButton[variant="point"]:hover { border-color: ${text}; }
QPushButton[variant="point"]:pressed {
    background-color: ${accent_soft}; color: ${accent}; border-color: ${accent};
}
QPushButton[variant="point"]:disabled {
    background-color: ${surface}; border-color: ${line}; color: ${muted2};
}

/* ================= 打点面板 ================= */
/* 现场场景：戴着手套/边看机器人边点，所以这一屏的字号与热区都比主界面**低一档密度**
   （间距给足、主要动作放大）。但不靠"更大的字"来做 —— 靠留白与形状，
   保持与主界面同一套字阶，避免看上去像另一个软件。 */
/* 输入框与打点按钮按同一个行高对齐（高度由面板代码设成固定值 ROW_H=48）：
   同一行里两个控件高矮不一，现场一眼就能看出"这两颗不是一套"（本项目踩过一次）。
   纵向内距因此交给"固定高度 + 居中"，不靠 padding 算 —— 字号或字体一换，
   padding 算法就会漂，而固定高度不会。 */
QDialog#PointPanel QLineEdit {
    padding: 0px 12px;
    border-radius: ${r2}px;
}
QDialog#PointPanel QPushButton[variant="point"] {
    padding: 0px 26px;
    min-width: 100px;
}
/* 标签网格：卡片由 delegate 自绘，视图本身不该再画一层框（否则框里框外两重边） */
QListWidget#PointLabels {
    background: transparent;
    border: none;
    outline: none;
}
/* 已打点列表：等宽读数，行紧凑但保留点击热区 */
QListWidget#PointRecords { padding: 3px; }
QListWidget#PointRecords::item {
    padding: 5px 8px;
    border-radius: ${r1}px;
}
QListWidget#PointRecords::item:hover { background-color: ${surface2}; }

/* 主操作：整个界面里唯一使用强调色的地方 */
QPushButton[variant="primary"] {
    background-color: ${accent};
    border: 1px solid ${accent};
    color: ${accent_ink};
    font-weight: 600;
}
QPushButton[variant="primary"]:hover { background-color: ${accent}; border-color: ${text}; }
QPushButton[variant="primary"]:disabled {
    background-color: ${surface2}; border-color: ${line}; color: ${muted2};
}

/* 危险操作：只为"停止/删除"这类动作保留红色 */
QPushButton[variant="danger"] {
    background-color: ${surface2};
    border: 1px solid ${danger};
    color: ${danger};
    font-weight: 600;
}
QPushButton[variant="danger"]:hover { background-color: ${danger_soft}; }
QPushButton[variant="danger"]:disabled {
    background-color: ${surface}; border-color: ${line}; color: ${muted2};
}

/* 进行中/需注意的次级动作（取消上传等） */
QPushButton[variant="warn"] {
    background-color: ${surface2};
    border: 1px solid ${warn};
    color: ${warn};
    font-weight: 600;
}
QPushButton[variant="warn"]:hover { background-color: ${warn_soft}; }

/* 幽灵按钮：次级操作，B 里做成药丸形，弱化到几乎只剩边框 */
QPushButton[variant="ghost"] {
    background-color: transparent;
    border: 1px solid ${line_strong};
    border-radius: ${pill_32}px;
    color: ${muted};
    padding: 6px 14px;
    font-size: ${fs_sm}px;
}
QPushButton[variant="ghost"]:hover { color: ${text}; border-color: ${muted2}; }

/* ---------- 导出目标芯片（v6.4.4）----------
   一颗药丸：左边状态图标，中间目标名，右边可用空间，末尾一个 caret。
   取代原来那个裸QComboBox——下拉只该选目标，而挂载点路径是另一件事，
   塞在一起必然要外挂一行。这里三件事（目标/路径/状态）收在一处。

   `state` 三态与 `theme.set_state` 对齐：none（服务器）/ ok（硬盘就绪）/ warn（未就绪）。
   默认态就把状态画出来是这一版的关键：盘没插好时不用点开面板就能看见。 */
QToolButton#ExportChip {
    background-color: ${surface2};
    border: 1px solid ${line};
    border-radius: ${pill_30}px;
    padding: 5px 10px 5px 11px;
    font-size: ${fs_base}px;
    color: ${muted};
    text-align: left;
}
QToolButton#ExportChip:hover { border-color: ${muted2}; color: ${text}; }
/* 就绪：整颗药丸染上ok_soft。不只染图标——状态要能在一眼扫过时就被认出来，
   只染13px 的小图标，现场很容易漏掉（那正是这一版要解决的问题）。 */
QToolButton#ExportChip[state="ok"] {
    background-color: ${ok_soft};
    border-color: ${ok};
    color: ${text};
}
QToolButton#ExportChip[state="ok"]:hover { background-color: ${ok_soft}; border-color: ${ok}; }
/* 异常：琥珀底。不用 danger 红 —— "盘没插好"是提醒不是故障，
   红底会让人以为数据已经丢了。 */
QToolButton#ExportChip[state="warn"] {
    background-color: ${warn_soft};
    border-color: ${warn};
    color: ${text};
}
QToolButton#ExportChip[state="warn"]:hover { background-color: ${warn_soft}; border-color: ${warn}; }

/* 芯片里那个"可用 128GB"：次要信息，比目标名弱一档。
   不能省掉它 —— 29GB 的盘录满这件事，现场必须不点开就知道。 */
/* 芯片内部的容器与标题。**这两条以前不存在**，于是它们被全局 `QWidget` 背景规则
   涂成页面底色 —— 而按钮自己是 `surface2`，中间一块、两侧各一条，看着像三块
   拼起来的（现场截图反馈）。项目里同一类问题补过好几次（`#InlineRow`、`#DiskConfig`）。 */
QWidget#ChipBody { background: transparent; border: none; }
QLabel#ChipTitle {
    background: transparent;
    color: ${text};
    font-size: ${fs_base}px;
}
/* 芯片右侧那个"可展开"的小箭头。自绘控件，只借 QSS 定尺寸，不画图形 ——
   颜色由控件自己按状态取（常态次要色 / 就绪跟绿 / 异常跟琥珀）。 */
QWidget#ChipCaret { background: transparent; }

QLabel#ChipCaption {
    background: transparent;
    color: ${muted};
    font-size: ${fs_sm}px;
}
QToolButton#ExportChip[state="ok"] QLabel#ChipCaption { color: ${ok}; }
QToolButton#ExportChip[state="warn"] QLabel#ChipCaption { color: ${warn}; }

/* 展开面板：非模态弹层，浮在卡片之上，所以底色用 popup（纯白）+ popup_line 描边，
   与项目既有的菜单/下拉列表同一套（见 POPUP_* 注释）。 */
#ExportPanel {
    background-color: ${popup};
    border: 1px solid ${popup_line};
    border-radius: ${r3}px;
}
#ExportPanel QLabel { background: transparent; }

/* 面板里的三个目标选项（卡片式）。选中态用 accent_soft + accent 描边 ——
   和整体 accent 语言一致，不另造一套高亮色。 */
QWidget#TargetOption {
    background-color: ${surface};
    border: 1px solid ${line};
    /* 圆角用 r2（12px）而不是 r1（8px）：卡片比它内部的小元素（16px 的圆环、
       8px 圆角的输入框）大一号，嵌套的层次才清楚。 */
    border-radius: ${r2}px;
}
QWidget#TargetOption:hover {
    background-color: ${surface2};
    border-color: ${line_strong};
}
QWidget#TargetOption[selected="true"] {
    background-color: ${accent_soft};
    /* 1.5px 而不是 1px：选中与否是这个面板**唯一**要传达的信息，
       1px 的描边在现场屏幕上"看不出这张被选中"（实测反馈）。 */
    border: 1.5px solid ${accent};
}

/* 卡片里的两行文字。**此前这两条规则根本不存在**（v6.4.6 查出来的），
   于是标题和说明同色同重 —— 「服务器」和「通过 SFTP 上传至远程服务器」
   看起来一样重要，现场扫的时候读不出哪个是选项名、哪个是注解。 */
QLabel#TargetOptionTitle {
    background: transparent;
    color: ${text};
    font-size: ${fs_base}px;
    font-weight: 500;
}
QLabel#TargetOptionHint {
    background: transparent;
    color: ${muted};
    font-size: ${fs_sm}px;
}

/* 面板里的挂载点配置块：与整个面板同一张底，不另加灰底 ——
   嵌套灰底（灰底里的灰底）在浅色系里会显得脏。 */
QWidget#DiskConfig { background: transparent; border: none; }
/* 面板里的分组标签（「数据导出目标」「挂载点」）。
   **降一档**：它是分组标签、不是标题 —— 现场反馈"留白太多"，而这一行原本用
   正文级字号 + 正文级颜色，既抢眼又占高度。11px + 更弱的颜色更准。
   ⚠️ **必须用独立 objectName，不能改 `PanelTitle`**：`PanelTitle` 是
   `common.make_panel_head()` / `common.dialog_card()` / `ui/data_view.py`
   共用的**白卡片标题**样式（全项目几十处）。直接改它 = 顺手改了整站所有
   面板标题，而这次的设计只针对导出面板。各管各的。 */
QLabel#GroupLabel {
    background: transparent;
    color: ${muted2};
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    font-weight: 500;
}
/* 落点预览：等宽字体（那是路径）+ 浅底（它是个"引用块"，不是控件）。 */
QLabel#LandingPreview {
    background-color: ${surface2};
    color: ${muted};
    font-family: "IBM Plex Mono", monospace;
    font-size: ${fs_sm}px;
    border-radius: ${r1}px;
}

/* 紧凑幽灵钮：与幽灵钮同一套形状语言，只把横向内距收到 8px。
   用途是"一行里要塞好几个小步进按钮"的场合（姿势控制的一行走轴表有 4 个 ± 钮，
   用常规内距(14px)时每个按钮要 40px 才放得下 3 个字符，4 个就把滑杆挤没了）。 */
QPushButton[variant="ghost-compact"] {
    background-color: transparent;
    border: 1px solid ${line_strong};
    border-radius: ${pill_32}px;
    color: ${muted};
    padding: 6px 8px;
    font-size: ${fs_sm}px;
}
QPushButton[variant="ghost-compact"]:hover { color: ${text}; border-color: ${muted2}; }

/* 幽灵危险钮：删一个点这种"局部、可重来的破坏"。
   实心红是留给急停与"停止录制"的，这里只借红色描边与字色 —— 强度跟着后果走。 */
QPushButton[variant="ghost-danger"] {
    background-color: transparent;
    border: 1px solid ${line_strong};
    border-radius: ${pill_32}px;
    color: ${danger};
    padding: 6px 14px;
    font-size: ${fs_sm}px;
}
QPushButton[variant="ghost-danger"]:hover { background-color: ${danger_soft}; border-color: ${danger}; }
QPushButton[variant="ghost-danger"]:disabled { color: ${muted2}; border-color: ${line}; }

/* 操作区专用几何：**同行必须等高、同区必须同圆角**。
   为什么单独限定在操作区（role="actions"）：主操作/数据/工具三行里同时出现
   默认按钮与幽灵按钮，两者的字号与内距本来不同，总高差 10px 左右，
   并排时肉眼可见地参差（"取消标定比旁边的药丸高一圈"就是这么来的）。
   这里把操作区里所有按钮的**内容高钉到 22px、上下内距统一 10px** →
   总高一律 44px（含 2px 边框），顺带满足触摸目标 ≥44px 的现场要求。
   圆角同样收归 r2：工具行原本是全圆角药丸，跟同一面板里其它按钮的圆角不一样，
   现场一眼就能看出"这几颗不是一套"（实测反馈就是这条）。
   对话框与日志区的小按钮不受影响：那里的密度更值得保留。
   注意 QSS 里 min-height 是"内容盒"高度，padding 与 border 另算。 */
QFrame[role="actions"] QPushButton {
    padding: 10px 16px;
    min-height: ${ch_main}px;   /* 触摸目标：随 configure_scale 放大 */
    border-radius: ${r2}px;
}
/* 幽灵变体的圆角也要在操作区内被覆盖：它自带 pill 全圆角，特异性比上面那条高 */
QFrame[role="actions"] QPushButton[variant="ghost"] {
    border-radius: ${r2}px;
}
/* 主操作仍是操作区里唯一更大的按钮（靠尺寸区分层级，不靠颜色堆叠） */
QFrame[role="actions"] QPushButton[variant="launch"] {
    padding: 14px 28px;
    min-height: ${ch_start}px;  /* 同上，这一档比主按钮更大 */
    font-size: 15px;
    font-weight: 700;
}

/* 带菜单的按钮：关掉 Qt 自绘的右下角小三角。
   它在自定义 QSS 下会缩在圆角外、与药丸形状打架，而且我们**统一在文案里写 ▾**，
   两个箭头同时出现就是重复指示（实测渲染出来就是"相机标定 ▾ ⌄"）。 */
QPushButton::menu-indicator { image: none; width: 0px; }

/* ================= 输入控件 ================= */
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {
    background-color: ${surface2};
    border: 1px solid ${line_strong};
    border-radius: ${r2}px;
    color: ${text};
    padding: 7px 10px;
    min-height: ${ch_input}px;  /* 输入控件高度，随缩放大 */
}
/* 下拉**弹层**里的选中色必须同时给组合框自己一份：弹层的 delegate 用的是
   `QComboBox::viewOptions()` 造出来的调色板，也就是**组合框的**调色板，不是视图的 ——
   只给视图设 `selection-background-color` 时，选中行会被画成组合框底色那种浅灰
   （离屏实测 #F4F6F5），看着像"没选中"（现场反馈过）。 */
QComboBox {
    selection-background-color: ${accent_soft};
    selection-color: ${accent_on_soft};
}
QComboBox:hover, QLineEdit:hover { border-color: ${muted2}; }
QComboBox:focus, QLineEdit:focus { border-color: ${accent}; }
QComboBox::drop-down { border: none; width: 26px; }

/* 可编辑下拉**内部**那个输入框：边框与底色都交给 QComboBox 画，它只承担"输入位"。
   有 Qt 会把上面的 `QLineEdit` 公共规则**也**作用到它身上 —— 于是输入框里又套一圈
   圆角框，提示字被顶到那圈框的边上（现场截图：外框在 x=117、内层框在 x≈129，同一个
   控件里两条边）。这里显式把内层框与底色去掉。
   左内距同样交给 QComboBox 自己的内距（10px），与界面里别的输入框一致。
   **padding 必须显式清零**：内层输入框自己也在上面那条公共规则的命中范围内，
   于是它同样带 7px 内距，而 QComboBox 又把编辑区放在**自己的** 7px 内距里 ——
   两层叠加把文字压到了框的下半部分（实测：框内区中心 17.5px、文字中心 25.5px，
   低了 8px；现场反馈就是"选择框内的文字不居中"）。 */
QComboBox QLineEdit {
    border: none;
    background: transparent;
    padding: 0;
}
$arrow_rule
/* 下拉列表的弹层：与菜单**同一张卡片**（同一套圆角、底色、描边、行内缩）——
   现场反馈"二级菜单和其他的下拉菜单太丑"，两个弹层长得不一样也是丑的一部分。 */
QComboBox QAbstractItemView {
    background-color: ${popup};
    border: 1px solid ${popup_line};
    border-radius: ${r2}px;
    padding: ${popup_pad}px;
    color: ${text};
    font-size: ${fs_base}px;
    selection-background-color: ${accent_soft};
    /* 选中行用深一档的强调色文字：与菜单选中项、Badge[state="accent"] 同一套语言。
       浅青底 + 正文字色看起来接近"浅灰"，读成"没选中"（现场反馈过）；而直接用
       accent 压浅底只有 4.32:1，不到 AA 的 4.5:1（accent_on_soft 是 6.44:1）。 */
    selection-color: ${accent_on_soft};
    outline: 0;
}
/* 下拉行的横向内距：纵向**不在 QSS 里给**（行高由 delegate 定，写垂直 padding 会叠高）。
   这条也只在应用级样式表里生效：同样一条写在视图控件上会被上面那条
   `QComboBox QAbstractItemView` 的特异性压掉。 */
QComboBox QAbstractItemView::item {
    padding: 0px ${popup_item_hpad}px;
}
QCheckBox { color: ${muted}; spacing: 7px; background: transparent; }
QCheckBox:hover { color: ${text}; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid ${line_strong};
    border-radius: ${r0}px;
    background-color: ${surface2};
}
QCheckBox::indicator:checked { background-color: ${accent}; border-color: ${accent}; }
QCheckBox:disabled { color: ${muted2}; }

/* 单选钮：与复选框同尺寸同描边，**只有圆角不同** ——
   复选框是圆角方块（r0=4）、单选是整圆（pill）。
   这条规则此前完全缺失，界面上出现过系统默认样式的单选钮（与整套卡片语言不符）。
   注意：`border-radius` 在浅色主题下只把圆角画成边框线、不裁底色，
   四角仍会被底色填满 —— 这是 Qt 的既有行为，全项目一致，不是这一处的缺陷。 */
QRadioButton { color: ${muted}; spacing: 7px; background: transparent; }
QRadioButton:hover { color: ${text}; }
QRadioButton::indicator {
    width: 15px; height: 15px;
    border: 1px solid ${line_strong};
    /* 用 r1（8px）而**不是 pill**：指示器只有 15px 见方，
       pill=16 超过它的一半又被 Qt 忽略了（实测退回方框）——
       "半径上限"是跟着控件尺寸走的，小控件不能套大控件的值。
       8px 在这个尺寸上正好是整圆。 */
    border-radius: ${r1}px;
    background-color: ${surface2};
}
QRadioButton::indicator:checked {
    /* 选中态用**粗边框成环**，而不是 `background-color: accent` 填满：
       Qt 的 QSS 只把 `border-radius` 画成边框线、**不裁底色矩形**，
       所以实心底色会填满四角、把圆画成方块（实测就是这个样子）。
       靠边框画环则没有这个问题 —— 边框线本身是圆的。
       中间那点底色用 surface（与卡片同色，不外露）。 */
    background-color: ${surface};
    border: 4px solid ${accent};
}
QRadioButton:disabled { color: ${muted2}; }
/* 只读的单选（面板里用它表示"当前项"，真正可点的是整张卡片）：
   禁用态别画成灰 —— 那看着像"不可选"，而它要表达的是"已选中"。 */
QRadioButton::indicator:checked:disabled { background-color: ${surface}; border: 4px solid ${accent}; }
QRadioButton::indicator:disabled { border-color: ${line_strong}; background-color: ${surface2}; }

QGroupBox {
    border: 1px solid ${line};
    border-radius: ${r2}px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    color: ${muted};
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    color: ${muted};
}

/* ================= 日志控制台 ================= */
QTextEdit#Console {
    background-color: ${surface};
    border: none;
    color: ${text};
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    padding: 8px 10px;
    selection-background-color: ${accent_soft};
}

/* ================= 进度 ================= */
QProgressBar {
    background-color: ${surface3};
    border: 1px solid ${line};
    border-radius: ${r2}px;
    color: ${muted};
    font-family: $font_mono;
    font-size: ${fs_micro}px;
    text-align: center;
}
QProgressBar::chunk { background-color: ${accent}; border-radius: ${r1}px; }
QLabel#StatusLine {
    color: ${muted};
    font-family: $font_mono;
    font-size: ${fs_sm}px;
    padding: 2px 4px;
}

/* ================= 表格 / 列表（各对话框） ================= */
QTableWidget, QListWidget, QTreeWidget, QPlainTextEdit {
    background-color: ${surface};
    alternate-background-color: ${surface2};
    border: 1px solid ${line};
    border-radius: ${r2}px;
    gridline-color: ${line};
    color: ${text};
    selection-background-color: ${accent_soft};
    selection-color: ${text};
}
QHeaderView::section {
    background-color: ${surface2};
    color: ${muted};
    border: none;
    border-right: 1px solid ${line};
    border-bottom: 1px solid ${line};
    padding: 6px 8px;
    font-family: $font_mono;
    font-size: ${fs_sm}px;
}
QTableCornerButton::section { background-color: ${surface2}; border: none; }

/* ================= 滚动条 ================= */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 0px;
}
QScrollBar::handle:vertical {
    background: ${surface3}; min-height: 28px; border-radius: 5px;
}
QScrollBar::handle:vertical:hover { background: ${muted2}; }
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 0px;
}
QScrollBar::handle:horizontal {
    background: ${surface3}; min-width: 28px; border-radius: 5px;
}
QScrollBar::handle:horizontal:hover { background: ${muted2}; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ================= 弹出层：菜单 / 下拉列表 / 悬停提示 ================= */
/* 这是全站唯一的"浮在内容之上"的一层（菜单、下拉列表、提示），统一按**一张圆角卡片**做：
   同底色 popup、同描边 popup_line、同圆角 r2、同内距、行内缩的选中态。
   四条具体决定与它们的理由：

   1. **圆角要真圆角**：Qt 的 QSS 圆角只在弹层**窗口自身透明**时成立，否则四个角会被
      弹层底色的矩形填满 —— 看到的是"方角里画了一条圆角边"（离屏实测：不开透明时
      四角 alpha=255，开了之后是 0）。开透明由 `apply_popup_rounding()` 在运行时做
      （QSS 管不到窗口属性），开关与退化办法见那个函数。
   2. **行内缩、行间留缝**（`margin` + `border-radius` 同时给）：选中态不再是一条横贯
      整个弹层的色带，而是一块与弹层边缘留出间距的圆角块 —— 与卡片、药丸是同一种
      形状语言。分隔线也照此内缩，读起来是"分组"，不是"被切开"。
   3. **选中 = accent_soft 底 + accent_on_soft 字**：底色浅、文字深，两侧都在 AA 以上
      （亮色 6.44:1 / 暗色 6.33:1）；`QMenu::item:selected` 同时覆盖悬停与键盘选中。
   4. **不用投影**：QSS 没有 box-shadow，而 QGraphicsDropShadowEffect 需要额外的透明边距，
      弹出层会被窗口矩形裁掉。工业上位机上"一条描边 + 一档底色"足够说明层级，也更省。
   注：弹层的尺寸数字（内距、行距、最小宽、分隔线留白）只在 POPUP 常量里写一次。
   注意**注释里也别写美元号开头的占位符**（Template 连注释一起替换），也别漏掉注释的
   收尾符 —— 漏掉的话后面整段规则都会被当成注释吃掉，表现为"改了样式却完全没生效"。 */
QMenu {
    background-color: ${popup};
    border: 1px solid ${popup_line};
    border-radius: ${r2}px;
    padding: ${popup_pad}px;
    color: ${text};
    font-size: ${fs_base}px;
}
QMenu::item {
    padding: ${popup_item_vpad}px ${popup_item_hpad}px;
    margin: ${popup_item_gap}px 2px;
    border-radius: ${r1}px;
    min-width: ${popup_min_w}px;
}
QMenu::item:selected {
    background-color: ${accent_soft};
    color: ${accent_on_soft};
}
QMenu::item:disabled { color: ${muted2}; }
/* 灰条目被悬停时不要再亮起来：那会读成"其实能点" */
QMenu::item:disabled:selected { background-color: transparent; color: ${muted2}; }
/* 分隔线：两侧内缩到与条目文字对齐的位置，看着是"分组"而不是"切一刀" */
QMenu::separator {
    height: 1px;
    background: ${popup_line};
    margin: ${popup_sep_v}px ${popup_sep_h}px;
}
/* 子菜单箭头与勾选指示器：不显式给尺寸时会被 QSS 缩成一小团（真出现三级菜单时才会看到） */
QMenu::right-arrow { width: 10px; height: 10px; margin-right: 10px; }
QMenu::indicator { width: 14px; height: 14px; margin-left: 10px; }
/* 弹层内滚动条：底是白的，全局那套"surface3 把手"在白底上几乎看不见 */
QMenu QScrollBar:vertical, QComboBox QAbstractItemView QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px 0px 2px 2px;
}
QMenu QScrollBar::handle:vertical, QComboBox QAbstractItemView QScrollBar::handle:vertical {
    background: ${popup_line}; border-radius: 5px; min-height: 24px;
}
QMenu QScrollBar::add-line, QMenu QScrollBar::sub-line,
QComboBox QAbstractItemView QScrollBar::add-line,
QComboBox QAbstractItemView QScrollBar::sub-line { height: 0px; width: 0px; }
QMenu QScrollBar::add-page, QMenu QScrollBar::sub-page,
QComboBox QAbstractItemView QScrollBar::add-page,
QComboBox QAbstractItemView QScrollBar::sub-page { background: transparent; }
""")


def build_stylesheet(mode: str = "dark") -> str:
    """生成整套 QSS。所有令牌在这里填进模板，组件代码不再各自写样式。"""
    values = palette(mode)
    values.update({k: str(v) for k, v in TYPE_SCALE.items()})
    # 控件高度令牌（v6.4.16）：与字号一起随 `configure_scale` 放大。
    # 间距 / 圆角**不注入缩放值** —— 理由见 SCALE 那一段的三条纪律。
    values.update({k: str(v) for k, v in CONTROL_HEIGHT.items()})
    values.update({k: str(v) for k, v in RADIUS.items()})
    values["r1"] = str(RADIUS["r1"])
    values["r2"] = str(RADIUS["r2"])
    values["r3"] = str(RADIUS["r3"])
    values.update({f"popup_{k}": str(v) for k, v in POPUP.items()})
    values["font_sans"] = FONT_SANS
    values["font_mono"] = FONT_MONO

    arrow = arrow_asset(mode)
    if arrow:
        values["arrow_rule"] = (
            "QComboBox::down-arrow { image: url(%s); width: %dpx; height: %dpx; }"
            % (arrow, ARROW_SIZE[0], ARROW_SIZE[1])
        )
    else:
        values["arrow_rule"] = ""
    return _QSS.substitute(values)


# ============ 弹出层的运行时处理 ============
# QSS 能画弹层的底色与圆角**边框**，但改不了弹层的**窗口属性**；而"真圆角"恰恰取决于
# 窗口属性（透明），下拉列表还额外取决于它那个内部容器的底色。这些只能在运行时做。
#: 弹层圆角开关：`DC_POPUP_ROUNDING=0` 启动即退回方角（目标机缺合成器时的退路）。
POPUP_ROUNDING_ENV = "DC_POPUP_ROUNDING"

#: 「弹层圆角」的处理器的唯一实例（见 `apply_popup_rounding`）
_POPUP_ROUNDER = None

#: 下拉弹层容器的 objectName：给它起个名，才能用 ID 选择器只清它自己的底（见 `_round_combo_popup`），
#: 也才能在它每次弹出时认出它（见 `_PopupRounder.eventFilter`）
_COMBO_POPUP_NAME = "DcComboPopup"


def popup_rounding_enabled() -> bool:
    """弹层要不要做"真圆角"（默认要）。

    真圆角的前提是**弹层窗口自身透明**：Qt 只把 QSS 的 border-radius 画成边框线，
    **不裁**底色矩形，四角仍会被弹层底色填满 —— 看到的是"方角里画了一条圆角边"。
    离屏实测：不开透明时四角 alpha=255，开了之后是 0。开透明需要窗口系统有合成器
    （GNOME / Wayland 天然有）。

    万一目标机器上弹层四角发黑（= 没有合成器），启动时带上 `DC_POPUP_ROUNDING=0`
    即可退回方角：底色、描边、内缩行这些都不受影响，只是四个角变直角。
    """
    value = os.environ.get(POPUP_ROUNDING_ENV, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _round_popup_window(widget) -> None:
    """给一个弹层窗口开透明 —— 这是 QSS 圆角成立的前提。"""
    if widget.testAttribute(Qt.WA_TranslucentBackground):
        return
    widget.setWindowFlags(widget.windowFlags() | Qt.FramelessWindowHint)
    widget.setAttribute(Qt.WA_TranslucentBackground, True)


def _style_combo_view(view) -> None:
    """下拉列表的视图：去边框 + 行高 + 行间留缝。"""
    if view is None:
        return
    view.setFrameShape(QFrame.NoFrame)
    view.setSpacing(POPUP["item_gap"])
    # 行高不在这里设（见 POPUP 常量的说明）；这一步只把边框去掉、行间留缝


def _round_combo_popup(combo) -> None:
    """下拉列表的弹层：容器透明（否则它会把视图的圆角盖成方角），视图按弹层规格排。"""
    view = combo.view()
    if view is None:
        return
    _style_combo_view(view)
    container = view.window()
    if container is None or container is combo:
        return
    if container.objectName() != _COMBO_POPUP_NAME:
        # 用 ID 选择器**只命中容器本身**：写在容器上的样式表会向子控件级联，
        # 用类型选择器会把视图的底色也一起清掉（那就成了透明弹层）
        container.setObjectName(_COMBO_POPUP_NAME)
        container.setStyleSheet(
            f"#{_COMBO_POPUP_NAME} {{ background: transparent; border: 0px; }}")
    _round_popup_window(container)


def round_popup(widget) -> None:
    """按弹层规格处理一个弹出层（`QMenu` / `QComboBox` / 提示气泡）。

    过滤器在 Polish 与 Show 两个时机都会调它；测试与离屏渲染脚本也可以直接调，
    不依赖事件时序。异常一律吞掉：弹层只是"方角还是圆角"，不该因此影响任何功能。
    """
    if widget is None or not popup_rounding_enabled():
        return
    try:
        if isinstance(widget, QComboBox):
            _round_combo_popup(widget)
        else:
            _round_popup_window(widget)
    except Exception as exc:                      # noqa: BLE001
        logger.debug("弹层圆角处理失败（不影响功能）：%s", exc)


class _PopupRounder(QObject):
    """把每个弹出的菜单 / 下拉列表都处理成"圆角卡片"。

    为什么用事件过滤器、而不是在每个 `QMenu` / `QComboBox` 的构造处各写一句：
    主题层是这个项目唯一的样式来源（见文件头），"弹层长什么样"属于样式这件事 ——
    写在这里，以后新加的菜单与下拉自动跟着走，谁都不用记得补一句话。

    `Polish` 与 `Show` 都要接：Polish 在控件刚建好时到（此时窗口还没创建，改属性最稳），
    Show 兜住"过滤器装得比控件晚"的情况（例如主题在运行中切换）。

    下拉列表还接**弹层容器自己的 Show**：容器是懒创建的，它的透明属性必须在它第一次
    弹出**之前**设好（弹出后再开透明，这一帧仍是方角）。
    """

    def eventFilter(self, obj, event):            # noqa: N802 (Qt 命名)
        kind = event.type()
        if kind not in (QEvent.Polish, QEvent.Show):
            return False
        # 装的是 app 级过滤器，每一条事件都会经过这里，先做最便宜的判断
        if isinstance(obj, (QMenu, QComboBox)) or obj.__class__.__name__ == "QTipLabel":
            round_popup(obj)
        elif isinstance(obj, QFrame) and obj.objectName() == _COMBO_POPUP_NAME:
            _style_combo_view(obj.findChild(QAbstractItemView))
        return False


def apply_popup_rounding(app) -> None:
    """给 app 装「弹层圆角」过滤器（幂等：同一个 app 只装一次）。

    注意"上一个过滤器"可能已经是**已销毁的包装对象**（测试里每个用例换一个 QApplication，
    程序重启也一样）：那时连 `parent()` 都会抛 `RuntimeError`，所以这里每一步都要挡住。
    """
    global _POPUP_ROUNDER
    if app is None or not popup_rounding_enabled():
        return
    if _POPUP_ROUNDER is not None:
        try:
            previous = _POPUP_ROUNDER.parent()
        except RuntimeError:
            previous = None                 # 包装对象已失效，当作没装过
        if previous is app:
            return
        if previous is not None:
            try:
                previous.removeEventFilter(_POPUP_ROUNDER)
            except (AttributeError, RuntimeError):
                pass                        # 上一个 app 已经销毁（测试里常见）
    _POPUP_ROUNDER = _PopupRounder(app)
    app.installEventFilter(_POPUP_ROUNDER)


def apply_theme(app, mode: str = "dark") -> None:
    """把主题应用到整个应用（对话框一并生效，不需要逐个设置）。"""
    global _ACTIVE_MODE
    if app is None:
        return
    # 记下当前模式给自绘控件用（见 active_mode 的说明）
    _ACTIVE_MODE = "light" if str(mode).lower() == "light" else "dark"
    app.setStyleSheet(build_stylesheet(mode))
    # 弹层的圆角只能靠窗口属性（QSS 画得到边框、裁不掉底色的方角），一并装上
    apply_popup_rounding(app)


#: 最近一次 `apply_theme` 用的明/暗模式。
#:
#: QSS 管不到自绘控件（列表 delegate、电量环），它们得自己去令牌里取色。取色时不要把
#: 宿主窗口的引用存下来——那会形成 Python 引用环（窗口 → 子控件 → delegate → 窗口），
#: 环被回收时 pyqt 里 C++ 对象的销毁顺序不定，**实测会让之后任意一次 setStyleSheet 段错误**
#: （崩在主题层，离病因很远）。这里让"当前主题"成为一个全局事实，自绘控件随时来读，
#: 谁也不需要持有谁。
_ACTIVE_MODE = "dark"


def active_mode() -> str:
    """当前主题模式（`apply_theme` 记下的那一个）。自绘控件用它取色。"""
    return _ACTIVE_MODE


def repolish(widget) -> None:
    """动态属性（state / variant）改完后必须重新抛光，否则 QSS 选择器不重新求值。"""
    if widget is None:
        return
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_state(widget, state: str, deep: bool = True) -> None:
    """设置状态属性并立即刷新样式。state: ok / warn / danger / none / unknown。

    默认 deep=True：QSS 里存在 `QFrame#Vital[state="ok"] QLabel#VitalReadout` 这类
    **依赖父级属性**的后代选择器，只抛光父控件不会让子控件的规则重新求值。这是个
    很容易踩的 Qt 坑——表现是"状态属性改了，但读数颜色不跟着变"。
    """
    if widget is None:
        return
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    repolish(widget)
    if deep:
        for child in widget.findChildren(QWidget):
            repolish(child)


def set_variant(widget, variant: str) -> None:
    """设置按钮层级：primary（主操作）/ danger / ghost。"""
    if widget is None:
        return
    widget.setProperty("variant", variant)
    repolish(widget)


def mono_font(size: int, weight: int = QFont.Normal, letter_spacing: float = 0.0) -> QFont:
    """等宽字体（读数与标签用）。

    间距用 QFont 设置：Qt 的 QSS 不支持 letter-spacing，靠样式表加不出工业标签那种
    "字距拉开"的效果，只能走字体 API。
    """
    font = QFont()
    font.setStyleHint(QFont.Monospace)
    font.setFamily("IBM Plex Mono")
    font.setPointSizeF(max(size * 0.75, 7.0))     # px 近似转 pt
    font.setWeight(weight)
    if letter_spacing:
        font.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
    return font


class HazardStrip(QWidget):
    """急停卡顶部的**安全导轨**：一排圆角短段，未激活是冷灰，激活时整条转红。

    B 方案不用工业警示斜纹（那是 A 的语言），改用一排圆角段——和 B 的圆角卡片、
    药丸徽章是同一种形状语言；红色仍然只属于急停。

    自绘而不是贴图：激活/未激活只是两种绘制状态，不需要两张图片资源。
    """

    def __init__(self, mode: str = "dark", parent=None):
        super().__init__(parent)
        self.setObjectName("HazardStrip")
        self.setFixedHeight(10)
        self._mode = mode
        self._active = False

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    # 段宽 5px + 段距 12px（即 7px 间隙）。**必须是整数** ——
    # painter.drawLine/drawRoundedRect 只接受整型坐标，传 float 会抛 TypeError；
    # 而 paintEvent 中途抛异常会让 QPainter 来不及 end()，之后整个进程会以段错误
    # 收场（实测踩过：崩在离病因很远的地方，很难往回找）。
    STRIPE_WIDTH = 5
    STRIPE_STEP = 12

    def paintEvent(self, event):     # noqa: N802 (Qt 命名)
        colors = palette(self._mode)
        painter = QPainter(self)
        try:
            self._paint(painter, colors)
        except Exception as exc:                     # noqa: BLE001
            # 这个控件是自绘的：万一以后别的绘制逻辑出错，也不能把整个 GUI 拖崩，
            # 降级成一条纯色横条即可。
            logger.warning(f"安全导轨绘制失败（已降级为纯色）: {exc}")
        finally:
            painter.end()

    def _paint(self, painter: QPainter, colors: dict) -> None:
        painter.fillRect(self.rect(), QColor(colors["surface2"]))

        color = QColor(colors["danger"] if self._active else colors["muted2"])
        if not self._active:
            color.setAlpha(90)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)

        height = int(self.height())
        seg = self.STRIPE_WIDTH
        radius = max(1, seg // 2)
        x = 0
        while x + seg <= self.width():
            painter.drawRoundedRect(int(x), (height - seg) // 2, seg, seg, radius, radius)
            x += self.STRIPE_STEP
