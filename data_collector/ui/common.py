"""ui 包共享的辅助：设计令牌、面板排版辅助、跨面板复用的纯函数。

从原来的 ui.py 顶部抽出。放在单独模块是为了让各面板能复用，
而不用把 ui.py 再撑大一圈。
"""
import os
import re
import subprocess
from typing import Optional
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QPlainTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygon
from PyQt5.Qt import QDesktopServices
import time
import traceback
import shutil
import zipfile
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from .. import theme
from ..core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ..core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError



__all__ = [
    "ANSI_RE",
    "ARM_MOTION_LABELS",
    "CHARGING_MARKERS",
    "LOW_BATTERY_CRITICAL_PERCENT",
    "LOW_BATTERY_PERCENT",
    "REMOTE_TARGETS",
    "SP",
    "TELEOP_STARTUP_TIMEOUT_MS",
    "_as_float",
    "_as_int",
    "_main_program_running",
    "_play_bag_folder",
    "_warn_if_main_program_running",
    "describe_export_target",
    "ensure_remote_password",
    "FIELD_NAMES",
    "file_link",
    "lost_source_fields",
    "make_panel_head",
    "module_state_texts",
    "remote_subpath_arg",
    "resolve_upload_subpath",
]

#: `resolve_upload_subpath` 的"没有远端"返回值。见该函数 docstring 的说明：
#: 不能用 `""`，因为它在 server 侧是合法值（远端根目录）。
_NO_REMOTE = "\x00no-remote"


SP = theme.SPACING


def _main_program_running(config) -> bool:
    """主程序（run_nodes.sh）是否在运行。

    用 pgrep 而不是进程表：主程序可能是上一次界面会话拉起的，本会话没有它的句柄。
    加超时是为了不让一次异常的 pgrep 卡住界面线程。
    """
    try:
        pattern = config.get_script_path("run_nodes_script").name
    except Exception:
        pattern = "run_nodes.sh"
    try:
        result = subprocess.run(["pgrep", "-f", pattern],
                                capture_output=True, timeout=2)
    except Exception:
        return False
    return result.returncode == 0


def _warn_if_main_program_running(widget, config) -> bool:
    """主程序在跑就拦下播放。返回 True 表示已拦截。"""
    if not _main_program_running(config):
        return False
    QMessageBox.warning(
        widget, "主程序正在运行",
        "主程序正在运行，请先关闭主程序（点击「关闭程序」按钮）再播放数据。")
    return True


def _play_bag_folder(widget, launcher, folder_path, log) -> None:
    """播放一个 bag 文件夹：先修权限，再交给 Launcher 启脚本。"""
    try:
        subprocess.run(["chmod", "-R", "u+w", str(folder_path)],
                       check=False, timeout=5)
    except Exception as exc:
        log(f"警告：无法修改权限 {folder_path}，播放可能失败：{exc}")

    try:
        launcher.launch_play_bag(str(folder_path), with_rviz=True)
        log(f"已启动数据播放：{folder_path}")
    except Exception as exc:
        QMessageBox.critical(widget, "启动失败", f"无法启动播放脚本：{exc}")


ARM_MOTION_LABELS = dict(DEFAULT_ACTION_LABELS)


def file_link(url_text: str, path) -> str:
    """把本地路径渲染成日志区里可点击的链接（text 与 href 都做转义）。"""
    target = QUrl.fromLocalFile(str(path))
    return f'<a href="{target.toString()}">{html_escape(url_text)}</a>'


def ensure_remote_password(uploader, parent=None) -> bool:
    """确保 uploader 拿到了 SFTP 密码；没有就弹一次输入框。

    密码只存在内存里，本次会话内复用；不写入任何文件。返回 False 表示用户取消，
    调用方应直接中止当前远程操作。
    """
    if getattr(uploader, "password", ""):
        return True

    hint = (f"{uploader.remote_user}@{uploader.remote_host}:{uploader.remote_port}\n"
            "仅在本次运行期间记住，不会写入任何文件。")
    password, accepted = QInputDialog.getText(
        parent or uploader, "输入 SFTP 密码", hint, QLineEdit.Password
    )
    if not accepted or not password:
        return False

    uploader.password = password
    log = getattr(parent, "append_log", None) or getattr(parent, "append", None)
    if log:
        log("已记录本次会话的远端密码（仅内存，未写入磁盘）")
    return True


#: 这些目标需要问远端密码。`disk` 不在其中——它一条SSH 都不建，
#: 问密码是纯粹的骚扰（离线现场根本没有人知道服务器密码，却每次都要被问一次）。
REMOTE_TARGETS = ("server", "both")


def resolve_upload_subpath(uploader, parent=None) -> Optional[str]:
    """上传前问清楚"传到哪儿"，返回远端子路径；返回 None 表示用户取消。

    ## 为什么这道分流必须在界面层，而不能只靠逻辑层

    `core_data.upload_bag_folder` 已经在按 `export_target` 分流了，但界面在
    进入它**之前**还有两道远端专属的闸门：问密码、弹远端目录选择器。
    `RemoteDirectoryDialog` 构造完就会去列远端目录 —— 离线现场连不上，
    用户看到的是一个空对话框，选不了也退不出干净。**于是无论逻辑层分流
    写得多对，disk 目标都走不到它。** 分流放在这里，是因为"要不要问远端"
    这个问题本身就属于界面职责：它问的是"你在跟谁说话"。

    ## 为什么用 sentinel 而不是简单的 subpath

    `disk` 目标下**没有远端子路径**这个概念，而 `""` 在 server 侧是合法取值
    （表示远端根目录）。用一个独一无二的 `_NO_REMOTE` 把它和"用户选了根目录"
    分开，否则两个入口里都会写出`subpath or "(根目录)"` 这种在 disk 下
    明显不对的话。`None` 保留给"用户取消"，与既有调用方语义一致。

    ## 为什么要区分"取消"与"磁盘不可用"

    现场真正会遇到的困难不是"路径写错"（那个由 `_probe_disk_mount` 在
    输入时就报红），而是"路径对、盘也对，但少一个标识文件"。那属于
    配置问题，应该在这里给一次**可直接照做的说明**，而不是弹密码框 ——
    弹了密码框用户只会以为"要联网"，然后更困惑。
    """
    target = getattr(uploader, "_export_target", lambda: "server")()
    log = getattr(parent, "append_log", None) or getattr(parent, "append", None)

    if target not in REMOTE_TARGETS:
        # `disk`：不联网、不问密码、不弹远端浏览器。子路径交回None，
        # 磁盘侧会用自己的规则（挂载点下的 bag 名目录）决定落点。
        if log:
            log("导出目标为移动硬盘：本次不连接远端服务器，也不需要远端密码")
        return _NO_REMOTE

    if not ensure_remote_password(uploader, parent):
        return None
    # 惰性导入：`remote_browser` 在模块顶层 `from .common import schedule_once`，
    # 顶层反向导入会成环。
    from .remote_browser import RemoteDirectoryDialog

    remote_dlg = RemoteDirectoryDialog(uploader, parent, select_mode=True)
    if remote_dlg.exec_() != QDialog.Accepted:
        return None
    subpath = remote_dlg.get_selected_subpath()
    if subpath is None:
        return None
    return subpath


def remote_subpath_arg(subpath: Optional[str]) -> str:
    """把 `resolve_upload_subpath` 的返回值转成能传给 uploader 的远端子路径。

    sentinel（disk 目标）转成 `""`。对磁盘侧它会被完全忽略，磁盘侧按
    bag 名建目录；对 `both` 目标它本来就该是远端选出来的那个子路径。
    """
    return "" if subpath == _NO_REMOTE else (subpath or "")


def describe_export_target(uploader) -> str:
    """给日志区用的一句话目标说明（导出目标不同，落点完全不同）。"""
    target = getattr(uploader, "_export_target", lambda: "server")()
    if target == "disk":
        settings = uploader.config.get_export_settings() if uploader.config else {}
        return f"移动硬盘 {settings.get('mount_point') or '(未填挂载点)'}"
    if target == "both":
        return "移动硬盘 + 远端服务器"
    return f"远端服务器 {uploader.remote_user}@{uploader.remote_host}"


TELEOP_STARTUP_TIMEOUT_MS = 90_000


LOW_BATTERY_PERCENT = 40


LOW_BATTERY_CRITICAL_PERCENT = 20


CHARGING_MARKERS = ("充电中",)


def _as_int(value, default=0):
    """把状态读数安全转 int（状态文件由外部脚本产生，不能假定类型）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    """把外部读数安全转 float（时间戳这类字段可能是字符串或空值）。

    外部输入（状态文件 / 版本文件 / 配置）类型不符时**不能让异常冒出去**：
    这些函数跑在 Qt 槽里，未捕获异常会让 PyQt5 直接 abort 整个界面。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


#: 状态字段 -> 界面上叫的名字（顶栏说明行里"点名"用，别直接甩英文键名）
FIELD_NAMES = {
    "power_percent": "电量",
    "emergency": "急停",
    "charge": "充电",
    "init": "初始化",
    "location": "定位",
    "navigation": "导航",
    "map": "地图",
}


def lost_source_fields(status) -> list:
    """哪些状态字段的**数据源已确认断开**（`source_lost: true`）。

    为什么需要它：状态文件写得勤（每 0.5s）只说明**监控进程活着**，不代表读数是活的 ——
    底盘掉线 / 控制节点退出之后没人再发这个话题，文件里的旧读数会一直冻着，
    而"文件新鲜"这条判据看不出任何异常（电量、急停最危险）。

    只认监控脚本显式写下的标记：**缺这个键一律当"不知道"**（旧版脚本、或还没到
    下结论的时候），界面据此不标任何东西 —— 宁可少标一个，也不要在现场闪假警告。

    Args:
        status: 状态文件里 `status` 这一段（顺序、类型都不假定，按外部输入处理）。

    Returns:
        按 `FIELD_NAMES` 顺序排列的字段名列表（可能为空）。
    """
    if not isinstance(status, dict):
        return []
    lost = []
    for field in FIELD_NAMES:
        item = status.get(field)
        if isinstance(item, dict) and item.get("source_lost"):
            lost.append(field)
    return lost


def module_state_texts(module_state, file_stale: bool = False) -> list:
    """把状态文件里的 `module_state` 摊成顶栏文案：[（躯干文案, 状态）,（手臂文案, 状态）]。

    状态取 `theme.set_state` 的语义色名：`ok` 绿 / `warn` 黄 / `none` 灰。

    规则（现场定的）：
    * **双臂状态相同就合并**成「手臂 已使能」/「手臂 未使能」；
    * **不一致就分开写**，用 L / R 代表左右：「手臂 L 已使能 · R 未使能」——
      只有一侧没准备好时，那一侧的 follow 会被 `arm_control_node` 静默丢弃，
      所以这一种情况必须能一眼看出来（也因此给它 warn 黄，而不是安静地混过去）；
    * 已使能 = ok 绿；都没使能 = 灰（**中性，不是故障**，关机时本来就该是灰）；
    * 数据够不够新由 `robot_monitor.py` 判定：它已经把 `enabled`（有数据且够新）
      与 `age_sec` 写好了，这里只负责文案；
    * 整个状态文件停止刷新时（`file_stale`）**显式写"过期"并降级成 warn** ——
      文件不刷新时里面的 `age_sec` 是冻结的，只看它会一直显示"已使能"。

    只读：这里不做任何下发，也不改状态文件。
    """
    if not isinstance(module_state, dict) or not module_state:
        # 监控没启动 / 老版本脚本没写这一段：说"未知"，不编
        return [("躯干 未知", "none"), ("手臂 未知", "none")]
    if not module_state.get("available", True):
        return [("躯干 未知", "none"), ("手臂 未知", "none")]

    def entry(key):
        item = module_state.get(key)
        return item if isinstance(item, dict) else {}

    def age_suffix(item):
        age = item.get("age_sec")
        if isinstance(age, (int, float)):
            return f"（{age:.0f}s 前）"
        return "（已过期）"

    def finalize(text, state, *, live_warning: bool = False):
        """文件级过期统一收口。

        为什么以它为准：文件停更时，里面每个模块的 `age_sec` 都冻在最后一次写入的
        那一刻（一直是 0.xs），只看模块自己的年龄会得到**假绿灯** —— 这正是最危险
        的那种显示。所以文件一过期，模块文案一律加"（过期）"并降级成 warn。

        `live_warning=True` 表示这条文案本身已经在说"读数不是活的"（数据源已断开），
        此时不再叠一个"（过期）"：那是同一件事的第二次标注，只会把顶栏挤长。
        """
        if file_stale and not live_warning:
            return text + "（过期）", "warn"
        return text, state

    # 「有读数、但没人再发」要排在最前面：这一条原本会显示成"已使能"一直不变，
    # 而实际上没有任何人在更新它（模块被下使能 / 节点退出 / 掉线都长这样）。
    # 注意判据是**有数据 + 无发布者**：从未收到数据是正常状态（未使能），不能标故障。
    trunk = entry("trunk_state")
    if trunk.get("source_lost"):
        trunk_text, trunk_state = "⚠ 躯干 读数已停更（无数据源）", "warn"
    elif not trunk.get("has_data"):
        trunk_text, trunk_state = "○ 躯干 未使能", "none"
    elif trunk.get("enabled"):
        trunk_text, trunk_state = "● 躯干 已使能", "ok"
    else:
        trunk_text = "● 躯干 已使能"
        if not file_stale:              # 过期时不再报那个冻住的年龄，交给 finalize
            trunk_text += age_suffix(trunk)
        trunk_state = "warn"

    arms = entry("arm_state")
    if arms.get("source_lost"):
        arm_text, arm_state = "⚠ 手臂 读数已停更（无数据源）", "warn"
    elif not arms.get("has_data"):
        arm_text, arm_state = "○ 手臂 未使能", "none"
    else:
        left, right = bool(arms.get("enabled_left")), bool(arms.get("enabled_right"))
        if left and right:
            arm_text, arm_state = "● 手臂 已使能", "ok"
        elif not left and not right:
            arm_text, arm_state = "○ 手臂 未使能", "none"
        else:
            # 一多一少：分开写清是哪一侧（L/R），并按"需要留意"上色
            arm_text = (f"⚠ 手臂 L {'已使能' if left else '未使能'}"
                        f" · R {'已使能' if right else '未使能'}")
            arm_state = "warn"
        if not arms.get("enabled") and not file_stale:
            arm_text += age_suffix(arms)
            arm_state = "warn"

    return [finalize(trunk_text, trunk_state, live_warning=bool(trunk.get("source_lost"))),
            finalize(arm_text, arm_state, live_warning=bool(arms.get("source_lost")))]


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def make_panel_head(title, right_widgets=()):
    """面板标题行：等宽小标题 + 右侧控件。返回 (head_frame, head_layout)。"""
    head = QFrame()
    head.setObjectName("PanelHead")
    layout = QHBoxLayout(head)
    layout.setContentsMargins(SP["sp4"], SP["sp3"], SP["sp4"], SP["sp3"])
    layout.setSpacing(SP["sp3"])

    label = QLabel(title)
    label.setObjectName("PanelTitle")
    label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"], letter_spacing=1.0))
    layout.addWidget(label)
    layout.addStretch()
    for widget in right_widgets:
        layout.addWidget(widget)
    return head, layout


def dialog_card(title: str, *, head_pad=None, body_pad=None):
    """对话框里的一张白卡片：`QFrame#Panel` + `PanelHead`（标题 + 右侧徽章位）+ 内容区。

    三个控制页面（模块使能 / 夹爪控制 / 末端参数）原本各写一套版面：模块页与夹爪页是
    "裸行 + 分隔线"，末端参数是白卡片 —— 同一个家族长两副样子，现场学一次不够。
    抽成这一个构造器后三页同构（现场："控制页面统一吧"）。

    Args:
        head_pad / body_pad: 卡头与内容区的**上下**内边距，不给就用默认（`sp3`）。
            姿势控制页会传 `sp2` —— 它一页摞了四张卡，每张各让 8px 就是 32px，
            而这一页在 1080P 上正好卡在"底部按钮被顶出屏幕"的临界点（v6.4.16）。
            其余页面保持默认：那点留白是"卡片语言"的一部分，没必要跟着一起挤。

    Returns:
        ``(卡片, 卡头行布局, 内容区布局)``：卡头行由调用方往里加徽章/说明（标题已加好并靠左）。
    """
    card = QFrame()
    card.setObjectName("Panel")
    outer = QVBoxLayout(card)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    head_v = SP["sp3"] if head_pad is None else head_pad
    head = QFrame()
    head.setObjectName("PanelHead")
    head_row = QHBoxLayout(head)
    head_row.setContentsMargins(SP["sp4"], head_v, SP["sp4"], head_v)
    head_row.setSpacing(SP["sp2"])
    label = QLabel(title)
    label.setObjectName("PanelTitle")
    label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"], letter_spacing=1.0))
    head_row.addWidget(label)
    head_row.addStretch()
    outer.addWidget(head)

    body = QVBoxLayout()
    body.setContentsMargins(SP["sp4"], head_v, SP["sp4"],
                            SP["sp4"] if body_pad is None else body_pad)
    body.setSpacing(SP["sp2"])
    outer.addLayout(body)
    return card, head_row, body


def badge(text: str = "") -> QLabel:
    """药丸徽章（`QLabel#Badge`）—— 与顶栏、打点面板同一套形状语言。

    状态用 `theme.set_state(badge, "ok"/"warn"/"danger")` 上色；不给状态就是中性灰。
    """
    widget = QLabel(text)
    widget.setObjectName("Badge")
    widget.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
    # 纵向固定：徽章与更高的控件（勾选框、按钮）同一行时，QLabel 默认会被拉高，
    # 描边跟着拉成一个大方块（在姿势预设卡头实测到）
    widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)
    return widget


def fit_console(console, min_rows: int = 3, max_rows: int = 9) -> int:
    """把结果区高度设成"内容多高就多高"（`min_rows`~`max_rows` 行），返回设完的高度。

    为什么不能定死高度（原先各页写死 110 / 140 / 150px）：结果区没有边框、底色又与卡片
    同色（`QTextEdit#Console` 的 `background: surface`），只有两三行时那片空白看起来就是
    "卡片空了一块"，现场会读成"内容没显示出来"。

    按**文档实际高度**算而不是"块数 × 行高"：日志里有很长的行（工作空间路径、数组值），
    它们会自动折行，按块数估会算少、把内容裁掉。

    ## 两种控件取高度的方法不一样（v6.7.4）

    主日志区从 `QTextEdit` 换成了 `QPlainTextEdit`（见 `widgets.LogView`：前者加行数上限
    也封不住内存）。而结果区（末端执行器 / 姿势页 / 夹爪）复用的是同一个 `LogView`，
    它们的"高度跟着内容走"就落到了这里 —— 两种控件的文档布局**单位不同**：

    | 控件 | `document.size().height()` 的含义 |
    | --- | --- |
    | `QTextEdit` | 像素（实测 3 行 = 68.0） |
    | `QPlainTextEdit` | **块数**（实测 3 行 = 3.0）—— 照用会把内容算成 3px |

    `QPlainTextEdit` 用的是 `QPlainTextDocumentLayout`，要逐块累加
    `blockBoundingRect().height()` 才是像素，而且它**把长行折行也算进去了**
    （实测 3 个长行 = 244px，30 个短行 = 604px）。结果区只有几十行，遍历成本可忽略。
    """
    width = max(1, console.viewport().width())
    row = console.fontMetrics().lineSpacing()
    chrome = 2 * console.frameWidth() + 20          # 边框 + QSS 的 8px 上下内距 + 余量
    low, high = min_rows * row + chrome, max_rows * row + chrome
    height = int(max(low, min(_document_height(console, width) + chrome, high)))
    console.setFixedHeight(height)
    return height


def _document_height(console, width: int) -> float:
    """文档内容高度（像素）。两种控件取值方式不同，理由见 `fit_console` 的说明。"""
    document = console.document()
    if isinstance(console, QPlainTextEdit):
        layout = document.documentLayout()
        total = 0.0
        block = document.begin()
        while block.isValid():
            total += layout.blockBoundingRect(block).height()
            block = block.next()
        return total
    document.setTextWidth(width)      # QTextEdit 必须显式给宽度才能算折行
    return document.size().height()


#: 屏幕可用高度低于这个值就不再压缩：压到这个份上，内容已经挤得没法读，
#: 再压只会让"还看得见什么"变得不可预测 —— 保持原样让人自己拖动窗口，行为至少可预期。
MIN_SCREEN_HEIGHT = 640

#: 窗口管理器给对话框加的标题栏 + 边框高度（估算）。
#:
#: `availableGeometry().height()` 是**桌面可用区**，不含窗口自己的装饰；把内容直接
#: 铺到那个高度，底部的按钮行就会被标题栏顶出屏幕 —— 现场 1080P 上姿势控制页
#: "下面有一截看不到"正是这么来的（v6.4.16）。留在屏内这 40px 是给窗口装饰的。
#:
#: 只影响"不给 limit 时"的自动值；用例显式传 limit 时不受影响（它们要能喂固定值）。
WINDOW_FRAME_ALLOWANCE = 40


def sync_layout(widget) -> None:
    """把布局**整棵子树**重算一遍，让刚改过的定高区反映到 `sizeHint()` 上。

    ⚠️ 只调顶层 `layout().activate()` 是**不够的**（实测）：一张卡片的
    `minimumSizeHint` 由**卡内**的 layout 决定，卡内那层没重算就还是旧值 ——
    于是"还差多少"是猜出来的数，而兜底那次 `resize` 也会被**旧的布局最小高顶回去**
    （姿势控制页实测：压完仍报 1021，等于一像素都没压）。
    """
    layout = widget.layout()
    if layout is None:
        return
    for child in widget.findChildren(QWidget):
        child_layout = child.layout()
        if child_layout is not None:
            child_layout.invalidate()
            child_layout.activate()          # 同步重算 —— `updateGeometry` 是异步的，不够
        child.updateGeometry()
    layout.invalidate()
    widget.updateGeometry()
    layout.activate()


def fit_dialog_to_screen(dialog, *, limit=None, shrink=None) -> int:
    """按屏幕可用高度给对话框封顶，返回最终高度。

    **为什么需要它**：三个控制页的高度是**内容的自然高度** —— 姿势控制 1021、
    末端参数 1003，而屏幕的可用高度是有上限的。没有这一步时，窗口比屏幕高就会
    **把底部的操作按钮推到屏幕外**；对话框没有滚动条，用户没有任何办法够到它们，
    拖窗口也拖不出屏幕。

    `shrink(room)` 由各页自己给，收到"还要压掉多少高度才能放下"。各页按自己的
    优先级压缩（姿势控制页：先收结果区、再收预设区，**轴表一律不碰** ——
    三列各 214px 是"20 个轴一屏看全"的承诺）。压完仍超的话兜底
    `setMaximumHeight(limit)`：至少让标题栏与窗口锚点留在屏内，
    比"标题栏跑到屏幕上方之外、连拖都拖不动"强。

    `limit` 不给就用当前屏幕的可用高度。显式传值是为了让用例能在离屏环境里验它 ——
    离屏平台的"屏幕"只有 600px 高，会落进 `MIN_SCREEN_HEIGHT` 的"不压"分支。
    """
    screen = dialog.screen() or QApplication.primaryScreen()
    if limit is None:
        if screen is None:
            return dialog.height()
        # 减掉窗口装饰的高度：不减的话"刚好塞满可用区"的窗口，底部会被标题栏顶出屏。
        limit = screen.availableGeometry().height() - WINDOW_FRAME_ALLOWANCE
    if not limit or limit < MIN_SCREEN_HEIGHT:
        return dialog.height()
    need = max(dialog.height(), dialog.sizeHint().height())
    if need <= limit:
        return dialog.height()
    if shrink is not None:
        shrink(need - limit)
        sync_layout(dialog)
        # 压完重新量一次 —— `shrink` 可能已经压够了，也可能一点没压动。
        need = max(dialog.height(), dialog.sizeHint().height())
    if need > limit:
        # `setMaximumHeight` 只是**约束**，窗口不会因此变小（要等一次 resize 才生效）；
        # 压不动时这一下 `resize` 才是真正把窗口收进屏幕的动作。
        dialog.setMaximumHeight(limit)
        dialog.resize(dialog.width(), limit)
        sync_layout(dialog)
    if dialog.height() > limit and screen is not None:
        # **压不动、只能超屏**时，把窗口贴着屏幕顶部放（留 8px 边距）。
        # 三个控制页都是非模态 `show()`，位置由窗口管理器定 —— 默认居中，
        # 于是超出屏幕的那一截**上下各切一半**；贴顶之后被切掉的只有底部那一截，
        # 窗口标题栏一定在屏内（用户还能拖它），操作按钮那一排也尽量保住。
        dialog.move(dialog.x(), screen.availableGeometry().top() + 8)
    return dialog.height()


def field_label(text: str) -> QLabel:
    """表单里"这个字段是什么"的小标签（等宽小字 + 次级色）。

    与顶栏、操作区的 `FactLabel` 同一套字号与字体 —— 界面里所有"说明性小字"
    长得一样，读者不用重新判断哪一行是标签、哪一行是内容。

    原先 RecordDialog 自己写了一份，TeleopDialog 直接用普通 QLabel（14px 正文色），
    结果对话框里的字段名与用户输入的内容**同样重**，扫视时拉不出层级。
    """
    label = QLabel(text)
    label.setObjectName("FactLabel")
    label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))
    return label


def schedule_once(owner, msec: int, slot):
    """延迟执行一次，并把定时器**挂在 owner 上**（owner 销毁即自动取消）。

    为什么不直接用 `QTimer.singleShot(ms, callable)`：那种定时器不绑定任何对象，
    owner 销毁后它照样会触发，回调就落到已析构的 C++ 对象上 —— 表现为
    `processEvents()` 里的段错误，而且崩的位置随时序漂移，极难定位
    （实测：单跑不崩，跑过几十个界面测试后必崩）。

    PyQt5 也没有 `singleShot(ms, receiver, slot)` 这个重载（那是 Qt5 C++ 的），
    所以这里自己持有一个单次定时器，父对象设为 owner。每个调用点排的定时器都很
    有限（启动自检、产品配置轮询、远端空间刷新），随窗口一起销毁即可，不必回收。
    """
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(slot)
    timer.start(msec)
    return timer
