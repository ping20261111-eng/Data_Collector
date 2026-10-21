"""「夹爪控制」独立页面：宇树夹爪 Dex11 的张开 / 抓取 / FCC 实时抓取 + 零点标定。

拆出 `dialogs.py` 的理由见 `posture.py` 的文件头（页面与组装件分册）。协议常量与判据的来源都写在
`core_operations.GRIPPER_*` 那一段注释里（机器人侧官方测试程序 + 控制器 yaml）。
"""
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import pyqtSignal, QTimer
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFrame, QDoubleSpinBox, QSpinBox, QMessageBox)

from .. import theme
from ..core import PROJECT_ROOT
from ..core_operations import (GRIPPER_NAMES, GRIPPER_HAND_TYPE, GRIPPER_TAU_MIN_NM,
                              GRIPPER_TAU_MAX_NM, GRIPPER_SPEED_MAX_MMS,
                              GRIPPER_DEFAULT_TAU_RAW, GRIPPER_DEFAULT_SPEED_RAW,
                              GRIPPER_HOLD_SEC_MIN, GRIPPER_HOLD_SEC_MAX,
                              GRIPPER_HOLD_SEC_DEFAULT,
                              gripper_tau_nm, gripper_speed_mms, gripper_tau_raw,
                              gripper_speed_raw, gripper_opening_text,
                              gripper_fcc_seconds)
from .common import (SP, badge, dialog_card, field_label, fit_console,
                     fit_dialog_to_screen)
from .safety import guard_slot
from .threads import LocalTaskThread
from .widgets import LogView


class GripperDialog(QDialog):
    """宇树夹爪 Dex11 控制页面：左右两侧各三个动作 + 一套工具（检测 / 零点标定）。

    现场要求原话：「起程序和不起程序都要，不起程序只用 grasp 就行」（v5.2.0）、
    「夹爪控制把标定也做进去；开启主控的 fcc 抓取模式可以设置抓取多少时间；
    不起程序用 grasp 抓，用 open 开」（v5.3.0）。于是：

    | 主程序 | 走哪条路 | 张开 | 闭合抓取 | FCC 抓取 |
    | --- | --- | --- | --- | --- |
    | 在跑 | `/arm_control/hand_posture`（单次非实时，JTC） | ✅ | ✅ | ✅（实时，可设夹持时长）|
    | 没跑 | `dex11_direct_test id=N open|grasp=X` | ✅ | ✅ | ❌ |

    选路**自动**，界面上把当前走哪条写清楚（`probe["reason"]` 原话显示），不让人猜。

    六条与硬件打交道的约定：

    1. **能不能走话题，看"话题有没有订阅者"**（`arm_control_node` 在不在），不是扫
       `/proc` 的命令行特征串 —— 后者不校验进程归属；
    2. **能不能走直测/标定工具，看"有没有主程序进程"**。官方文档与 `dex11_calib --help`
       都明确"串口需要独占，先确保 ROS 程序已停止"，所以这两类工具与主程序互斥。
       两个判据**故意分开**：主程序刚起、话题还没就绪的那几秒两者都不满足 ——
       这时**两条路都不走**，界面提示"等几秒再检测"，而不是闷头选一条；
    3. **末端连接只在主程序在跑时才谈**（手掌要在手臂连接之后才连上）。没跑主程序时
       不显示"未连接"这种会误导的结论，直接说"未检测"；"读不出"（None）与
       "确认没连上"（False）分开，给的操作指引不一样；
    4. **按侧独立**（`_actions[side]`）：左右夹爪是两条独立链路（不同话题、不同控制器），
       一边在动不该把另一边的按钮灰掉。但**直测 / 标定 / 扫描这几类工具同一时刻只跑一个** ——
       它们都直接操作串口，官方两个工具的帮助文本都强调串口独占；
    5. **不做张合的二次确认，但标定要确认**：张合是抓取作业里的常规高频动作（每次取放
       都要按），力矩已被限制在 0.5~2.0 N·m，把"会夹持、限力多少"写在按钮悬停里常驻提示
       比每次都弹框更合适；而**零点标定会把当前物理位置永久写成零位**，前提是两指已手动
       推到完全夹紧 —— 这一步错了以后每次抓取都会偏，所以必须弹框（默认「取消」）。
    6. **动作进行中拦「关闭程序」**（`busy_changed` = 任一侧动作或工具动作在跑）：
       节点在动作中途消失会让硬件停在半途状态。

    结果判定：**话题路径报"指令已下发"**（单次指令发出去就结束了，轨迹由 JTC 负责），
    随后延迟回读一次关节读数；**FCC 路径**在动作进行中 tail 日志文件显示进度
    （一次夹持最长 60 秒，没有进度看起来就像卡住了），结束时取脚本自己的结论；
    **直测路径**解析工具输出（接触 / 未夹住 / 刚度档 / 电机报错）。
    """

    #: 内容宽度，按"一行放得下状态 + 三个按钮"定
    MIN_WIDTH = 860
    #: 主程序在跑时，动作完成后等轨迹走完再回读一次关节（看实际张合到哪）
    READBACK_DELAY_MS = 1800
    #: FCC 抓取进行中 tail 日志的周期（毫秒）
    TAIL_MS = 400

    #: 动作进行中置真，供主窗口拦「关闭程序」
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None, config=None, launcher=None):
        super().__init__(parent)
        self.config = config
        self.launcher = launcher
        self.probe = {}                 # 最近一次 probe_gripper() 的结果
        self._probing = False
        self._probe_thread = None
        #: side -> {"name": 动作名, "thread": QThread, "tail": bool}；按侧记，两侧互不阻塞
        self._actions = {}
        #: 直测 / 标定 / 扫描这类**独占串口**的工具动作（同一时刻只允许一个）
        self._tool = None
        self._tail_path = None
        self._tail_pos = 0
        self._tail_seen = 0

        self.setWindowTitle("夹爪控制")
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP["sp4"], SP["sp4"], SP["sp4"], SP["sp4"])
        layout.setSpacing(SP["sp3"])          # 卡片之间拉开；同卡片内用 sp2

        # 卡一：夹爪状态与动作（手型 / 走哪条路 / 两侧动作 / 参数）
        card, head_row, body = dialog_card("宇树夹爪 Dex11")
        self.hand_label = badge("手型 —")
        head_row.addWidget(self.hand_label)
        self.route_label = badge("路径 未检测")
        head_row.addWidget(self.route_label)

        self.rows = {
            "left": self._add_side_row(body, "left"),
            "right": self._add_side_row(body, "right"),
        }
        self.left_label, self.left_open, self.left_grasp, self.left_fcc = self.rows["left"]
        self.right_label, self.right_open, self.right_grasp, self.right_fcc = self.rows["right"]

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("Hint")
        self.hint_label.setWordWrap(True)
        body.addWidget(self.hint_label)

        body.addWidget(self._divider())

        params = QHBoxLayout()
        params.setSpacing(SP["sp2"])
        params.addWidget(field_label("目标力矩"))
        self.tau_spin = QDoubleSpinBox()
        self.tau_spin.setRange(GRIPPER_TAU_MIN_NM, GRIPPER_TAU_MAX_NM)
        self.tau_spin.setSingleStep(0.05)
        self.tau_spin.setDecimals(2)
        self.tau_spin.setSuffix(" N·m")
        self.tau_spin.setValue(gripper_tau_nm(GRIPPER_DEFAULT_TAU_RAW))
        self.tau_spin.setToolTip(
            "闭合时的限力。下限就是 0.5 N·m —— 控制器把低于它的力矩当作"
            "\"没给力\"，会给到完全松掉")
        self.tau_spin.valueChanged.connect(self._update_param_hint)
        params.addWidget(self.tau_spin)

        params.addSpacing(SP["sp3"])
        params.addWidget(field_label("合拢速度"))
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(1, int(GRIPPER_SPEED_MAX_MMS))
        self.speed_spin.setSingleStep(5)
        self.speed_spin.setSuffix(" mm/s")
        self.speed_spin.setValue(round(gripper_speed_mms(GRIPPER_DEFAULT_SPEED_RAW)))
        self.speed_spin.setToolTip(
            "整行程约 0.45s。上限 100 mm/s 是机器人侧实测跟得住的最快值，不再往上放")
        self.speed_spin.valueChanged.connect(self._update_param_hint)
        params.addWidget(self.speed_spin)

        params.addSpacing(SP["sp3"])
        params.addWidget(field_label("FCC 夹持"))
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(GRIPPER_HOLD_SEC_MIN, GRIPPER_HOLD_SEC_MAX)
        self.hold_spin.setSingleStep(0.5)
        self.hold_spin.setDecimals(1)
        self.hold_spin.setSuffix(" s")
        self.hold_spin.setValue(GRIPPER_HOLD_SEC_DEFAULT)
        self.hold_spin.setToolTip(
            "「FCC 抓取」在合拢到位后「保持夹持」多久（最长 60s）。\n"
            "非实时的「闭合抓取」不需要它 —— 那条指令发出去就结束了。")
        self.hold_spin.valueChanged.connect(self._update_param_hint)
        params.addWidget(self.hold_spin)

        params.addStretch()
        self.param_hint = QLabel("")
        self.param_hint.setObjectName("Hint")
        self.param_hint.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"]))
        self.param_hint.setToolTip("协议里的千分比（与机器人侧测试程序 --tau-left 300 是同一个量）")
        params.addWidget(self.param_hint)
        body.addLayout(params)
        layout.addWidget(card)

        # 卡二：操作结果（与另两个控制页面同一套骨架；标题也统一）
        result_card, _result_head, result_body = dialog_card("操作结果")
        self.result = LogView()
        self.result.setObjectName("Console")
        self.result.setReadOnly(True)
        self.result.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        self._console_width = 0
        self._fitting = False
        self._fit_console()
        result_body.addWidget(self.result)
        layout.addWidget(result_card)

        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setProperty("variant", "ghost")
        self.btn_recheck.setToolTip("重新探测主程序、末端连接与夹爪关节读数")
        self.btn_recheck.clicked.connect(self.on_recheck)
        row.addWidget(self.btn_recheck)

        self.btn_scan = QPushButton("检测夹爪")
        self.btn_scan.setProperty("variant", "ghost")
        self.btn_scan.setToolTip("只扫描串口上的夹爪（dex11_calib --scan，不做任何改动）："
                                 "看插好没有、在哪个口、当前角度与错误码。\n"
                                 "要独占串口，所以需要主程序已停止。")
        self.btn_scan.clicked.connect(self.on_scan_grippers)
        row.addWidget(self.btn_scan)

        self.btn_calibrate = QPushButton("零点标定")
        self.btn_calibrate.setProperty("variant", "danger")
        self.btn_calibrate.setToolTip("把夹爪当前物理位置写成零位（会永久改标定，需二次确认）。\n"
                                      "前提：主程序已停止、两指已手动推到完全夹紧。")
        self.btn_calibrate.clicked.connect(self.on_calibrate)
        row.addWidget(self.btn_calibrate)

        row.addStretch()

        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("variant", "ghost")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        self.setMinimumWidth(theme.px(self.MIN_WIDTH))
        #: 是否已经按屏幕高度收过一次（只做一次；用户手动拖过大小之后不该再动它）
        self._screen_fitted = False
        self._tail_timer = QTimer(self)
        self._tail_timer.timeout.connect(self._poll_tail)
        self._update_param_hint()
        self.refresh()

    # ---------- 版面 ----------
    def _fit_console(self):
        """结果区高度跟着内容走（3~9 行）—— 三个控制页面共用 `common.fit_console`。"""
        if self._fitting:
            return
        self._fitting = True
        try:
            self._console_width = max(1, self.result.viewport().width())
            fit_console(self.result, min_rows=4)      # 夹爪页一条动作经常有 4+ 行输出
        finally:
            self._fitting = False

    def _shrink_for_screen(self, room: int) -> None:
        """屏幕装不下时收结果区（`fit_dialog_to_screen` 的回调）。

        本页除了结果区之外都是**内容定高**的卡片（每一格多高由字段本身决定），
        没有可压的余地 —— 结果区本来就在 `CONSOLE_MIN_ROWS` ~ `CONSOLE_MAX_ROWS`
        之间按内容自适应，收到下限只是"同时少看几条日志"，不动任何操作。
        """
        if room <= 0:
            return
        fit_console(self.result, self.CONSOLE_MIN_ROWS, self.CONSOLE_MIN_ROWS)

    def resizeEvent(self, event):
        """窗口宽度变了，折行数就变了 → 重算一次高度。"""
        super().resizeEvent(event)
        if self.result.viewport().width() != self._console_width:
            self._fit_console()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        return line

    def _add_side_row(self, layout, side: str):
        """一行 = 一侧夹爪状态 + 张开 / 闭合抓取 / FCC 抓取。

        返回 ``(状态标签, 张开按钮, 闭合按钮, FCC 按钮)``。
        """
        row = QHBoxLayout()
        row.setSpacing(SP["sp2"])

        label = QLabel(f"○ {GRIPPER_NAMES[side]} 未检测")
        label.setObjectName("FactValue")
        label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))
        row.addWidget(label, 1)

        btn_open = QPushButton("张开")
        btn_open.clicked.connect(
            lambda _checked=False, s=side: self.on_gripper_action(s, "open"))
        row.addWidget(btn_open)

        btn_grasp = QPushButton("闭合抓取")
        btn_grasp.clicked.connect(
            lambda _checked=False, s=side: self.on_gripper_action(s, "grasp"))
        row.addWidget(btn_grasp)

        btn_fcc = QPushButton("FCC 抓取")
        btn_fcc.clicked.connect(
            lambda _checked=False, s=side: self.on_gripper_action(s, "fcc"))
        row.addWidget(btn_fcc)

        layout.addLayout(row)
        return label, btn_open, btn_grasp, btn_fcc

    # ---------- 状态刷新 ----------
    @guard_slot
    def refresh(self):
        """按最近一次探测结果刷新（**不**重新探测 —— 探测要跑 ROS 命令，几秒）。"""
        self._apply_head()
        self._apply_sides()
        self._apply_buttons()

    def _apply_head(self):
        probe = self.probe
        hand = probe.get("hand") or ""
        if not probe:
            self.hand_label.setText("手型 未检测")
            theme.set_state(self.hand_label, "none")
        else:
            self.hand_label.setText(f"手型 {hand or '未知'}")
            if not probe.get("hand_type_ok"):
                theme.set_state(self.hand_label, "danger")
            else:
                # 读不到 Product.json 时保持中性：路径可能随版本变，不当成错误
                theme.set_state(self.hand_label, "ok" if hand else "none")

        if self._probing:
            self.route_label.setText("路径 检测中…")
            theme.set_state(self.route_label, "none")
        elif not probe.get("available"):
            self.route_label.setText("路径 未检测")
            theme.set_state(self.route_label, "warn")
        elif probe.get("running"):
            self.route_label.setText("路径 ROS 话题")
            theme.set_state(self.route_label, "ok")
        elif probe.get("blocked"):
            self.route_label.setText("路径 待就绪")
            theme.set_state(self.route_label, "warn")
        else:
            self.route_label.setText("路径 直测工具")
            theme.set_state(self.route_label, "ok")

        self.hint_label.setText(probe.get("reason") or "点「重新检测」开始探测")

    def _apply_sides(self):
        sides = self.probe.get("sides") or {}
        for side, (label, _btn_open, _btn_grasp, _btn_fcc) in self.rows.items():
            entry = sides.get(side) or {}
            mark = "●" if entry.get("connected") else "○"
            label.setText(f"{mark} {GRIPPER_NAMES[side]} {self._reading_text(entry)}")
            theme.set_state(label, "ok" if entry.get("connected") else "none")

    @staticmethod
    def _reading_text(entry: dict) -> str:
        """一侧的读数描述。`connected` 为 None = 还没探测过，与"未连接"要分开说。"""
        if entry.get("connected") is None:
            return "未检测"
        if not entry.get("connected"):
            return "未连接（在遥操作里连接手臂后自动连上）"
        parts = []
        rad = entry.get("rad")
        if isinstance(rad, (int, float)):
            parts.append(f"{rad:+.4f} rad")
            # 开口宽度分档说（已张开 / 合到底 / 具体毫米数）—— 直接报毫米数会把"全开"
            # 读成"夹着东西"，见 core_operations.gripper_opening_text
            parts.append(gripper_opening_text(rad))
        effort = entry.get("effort")
        if isinstance(effort, (int, float)):
            parts.append(f"实际力矩 {effort:.2f} N·m")
        return " · ".join(parts) or "已连接"

    def _tool_busy(self) -> bool:
        """有独占串口的工具动作在跑（扫描 / 标定）。"""
        return self._tool is not None

    def _direct_busy(self, exclude: str = "") -> bool:
        """有没有**直测路径**的动作在跑（左/右都算，可排除自己那一侧）。

        只在"主程序没在跑"时有意义：那条路走 `dex11_direct_test`，**直接开串口**；
        话题路径走控制器，左右本就分开、不占串口。官方两个工具的帮助文本都强调
        串口独占，所以这里把直测动作串行化 —— 代价是几秒的等待，换来的是不会出现
        "两条指令抢同一个口"这种极难查的失败。
        """
        return any(entry.get("direct") for side, entry in self._actions.items()
                   if side != exclude)

    def _blocked_reason(self) -> str:
        """全局性的"为什么不能点"（与具体哪一侧无关）。空串 = 没有全局阻塞。"""
        probe = self.probe
        if not probe.get("available"):
            return "检测未完成（先点「重新检测」）"
        if not probe.get("hand_type_ok", True):
            return "当前手型不是宇树夹爪，不可操作"
        if probe.get("blocked"):
            return "主程序正在启动中（ROS 话题未就绪），等几秒再检测"
        return ""

    def _apply_buttons(self):
        """按"探测结果 + 在跑的动作"决定所有按钮。

        四个决定：**未知不给点**（先查明）、**禁用必给理由**（灰着不说话会被读成
        "坏了"）、**按侧判断忙**（另一边在动不影响这边）、**工具动作与侧动作互斥**
        （直测/标定都独占串口）。
        """
        probe = self.probe
        running = bool(probe.get("running"))
        sides = probe.get("sides") or {}
        blocked_reason = self._blocked_reason()

        for side, (_label, btn_open, btn_grasp, btn_fcc) in self.rows.items():
            name = GRIPPER_NAMES[side]
            entry = sides.get(side) or {}
            hand_ready = entry.get("connected") is True
            busy = side in self._actions

            if busy:
                reason = f"{name}的动作正在执行，等它出结果"
            elif blocked_reason:
                reason = blocked_reason
            elif running and not hand_ready:
                reason = f"{name}的末端手掌未连接（在遥操作里连接手臂后自动连上）"
            elif not running and (self._tool_busy() or self._direct_busy(side)):
                # 直测/标定正在用串口：这时再发一条直测指令会和它抢同一个口
                reason = "串口正被占用（另一个夹爪的动作 / 检测夹爪 / 零点标定），等它出结果"
            else:
                reason = ""

            # 两侧都支持张开与闭合抓取（不起程序时走 open / grasp）；FCC 只在话题路径下存在
            open_ok = grasp_ok = not reason
            fcc_ok = not reason and running
            if not reason and not running:
                reason_fcc = "FCC 抓取需要主程序在跑（它走实时话题）"
            else:
                reason_fcc = reason

            btn_open.setEnabled(open_ok)
            btn_grasp.setEnabled(grasp_ok)
            btn_fcc.setEnabled(fcc_ok)
            route = "ROS 话题" if running else "直测工具"
            btn_open.setToolTip(
                f"张开到完全打开（走{route}）" if open_ok else f"不可用：{reason}")
            btn_grasp.setToolTip(
                (f"闭合抓取：以 {self.tau_spin.value():.2f} N·m 限力夹持（走{route}）"
                 f"—— 请先确认夹取范围内没有手指")
                if grasp_ok else f"不可用：{reason}")
            btn_fcc.setToolTip(
                (f"FCC 实时抓取：S 曲线合拢 → 保持夹持 {self.hold_spin.value():.1f}s "
                 f"→ 自动松开并退出实时模式（预计约 "
                 f"{gripper_fcc_seconds(self._params()[1], self.hold_spin.value()):.0f}s）"
                 f"—— 请先确认夹取范围内没有手指")
                if fcc_ok else f"不可用：{reason_fcc}")

        # 检测 / 标定：都要独占串口 → 主程序没在跑时才能用
        if blocked_reason:
            tool_reason = blocked_reason
        elif running:
            tool_reason = "主程序在跑时串口被占（标定程序自己的提示也是先停 ROS 程序）"
        elif self._tool_busy():
            tool_reason = "已有工具动作在跑"
        elif any(side in self._actions for side in self.rows):
            tool_reason = "有夹爪动作正在跑，等它出结果"
        else:
            tool_reason = ""

        self.btn_scan.setEnabled(not tool_reason)
        self.btn_calibrate.setEnabled(not tool_reason)
        self.btn_scan.setToolTip(
            "只扫描串口上的夹爪（不做任何改动）" if not tool_reason
            else f"不可用：{tool_reason}")
        self.btn_calibrate.setToolTip(
            "把夹爪当前物理位置写成零位（需二次确认；先把两指推到完全夹紧）"
            if not tool_reason else f"不可用：{tool_reason}")

    def _update_param_hint(self):
        """把界面上的物理量换算成千分比显示出来（与机器人侧测试程序对得上）。"""
        tau_raw, speed_raw = self._params()
        self.param_hint.setText(f"raw {tau_raw} / {speed_raw}")

    def _params(self):
        """取当前参数，返回协议用的千分比 ``(力矩, 速度)``。"""
        return (gripper_tau_raw(self.tau_spin.value()),
                gripper_speed_raw(self.speed_spin.value()))

    # ---------- 探测 ----------
    @guard_slot
    def on_recheck(self):
        """后台重新探测（要跑 ROS 命令，几秒，不能占界面线程）。"""
        if self._probing:
            return
        self._probing = True
        self._report("正在检测主程序、末端连接与夹爪关节…")
        self.refresh()
        thread = LocalTaskThread(lambda: self.launcher.probe_gripper())
        self._probe_thread = thread
        thread.finished.connect(self._on_probed)
        thread.start()

    @guard_slot
    def _on_probed(self, ok: bool, payload, error: str):
        self._probing = False
        if ok and isinstance(payload, dict) and payload.get("available"):
            self.probe = payload
            self._report(f"检测结果：{payload.get('reason') or '（无结论）'}")
        elif ok and isinstance(payload, dict):
            self.probe = payload
            self._report(f"检测未完成：{payload.get('reason') or '没有取到结果'}")
        else:
            self.probe = {}
            self._report(f"检测失败：{error or '未知错误'}")
        self.refresh()

    # ---------- 单个夹爪的动作 ----------
    @guard_slot
    def on_gripper_action(self, side: str, action: str):
        """张开 / 闭合抓取 / FCC 抓取：按探测结果**自动选路**，后台执行。

        `action` 取 ``"open"`` / ``"grasp"`` / ``"fcc"``。只挡**同一侧**的重复触发；
        另一侧照常可点（见类文档第 4 条）。
        """
        name = GRIPPER_NAMES[side]
        verb = {"open": "张开", "grasp": "闭合抓取", "fcc": "FCC 抓取"}[action]
        if side in self._actions:
            self._report(f"{name}的动作还在进行中，等它出结果再点")
            return

        probe = self.probe
        reason = self._blocked_reason()
        if reason:
            self._report(f"{reason}，未下发任何指令")
            return

        running = bool(probe.get("running"))
        if running:
            entry = (probe.get("sides") or {}).get(side) or {}
            if entry.get("connected") is not True:
                self._report(f"{name}的末端手掌未连接，未下发任何指令 —— "
                             "先在遥操作里连接手臂，手掌会跟着连上")
                return
        elif action == "fcc":
            self._report(f"{name} FCC 抓取需要主程序在跑（它走实时话题），未下发任何指令")
            return
        elif self._tool_busy() or self._direct_busy(side):
            self._report("串口正被占用（另一个夹爪的动作 / 检测夹爪 / 零点标定），"
                         "未下发任何指令 —— 直测工具要独占串口")
            return

        tau_raw, speed_raw = self._params()
        hold_sec = float(self.hold_spin.value())
        log_file = None
        if action == "fcc":
            log_file = self._fcc_log_path(side)

        # `direct` = 这个动作走的是直测工具（直接开串口），用于把直测动作串行化
        entry = {"name": verb, "thread": None, "tail": log_file is not None,
                 "direct": action in ("open", "grasp") and not running}
        self._actions[side] = entry
        self._emit_busy()
        # 立刻按"本侧在忙"把按钮灰掉：等下一次 refresh 才变的话，现场会以为没点上
        self._apply_buttons()
        self._report(self._action_intro(name, action, running, tau_raw, speed_raw, hold_sec))

        if action == "fcc":
            def task():
                return self.launcher.run_gripper_fcc(side, tau_raw, speed_raw, hold_sec,
                                                     log_file=log_file)
        elif running:
            def task():
                return self.launcher.set_gripper_posture([side], action == "grasp",
                                                        tau_raw, speed_raw)
        else:
            def task():
                return self.launcher.run_gripper_direct(side, action, tau_raw)

        if log_file is not None:
            self._start_tail(log_file)
        # 线程先存进 action 再 start：测试替身是同步的，start() 里可能已经跑完并
        # 把 action 取走了，事后再赋值会 KeyError（与模块使能页面同一个坑）
        thread = LocalTaskThread(task)
        entry["thread"] = thread
        thread.finished.connect(
            lambda ok, payload, error, s=side: self._on_action_done(s, ok, payload, error))
        thread.start()

    @staticmethod
    def _action_intro(name, action, running, tau_raw, speed_raw, hold_sec) -> str:
        """动手前那行预告：现场最关心"走哪条路、多大力、要等多久"。"""
        speed = f"{gripper_speed_mms(speed_raw):.0f} mm/s"
        if action == "fcc":
            return (f"{name} FCC 抓取：走 ROS 实时话题，限力 "
                    f"{gripper_tau_nm(tau_raw):.2f} N·m、合拢 {speed}、"
                    f"夹持 {hold_sec:.1f}s（预计约 "
                    f"{gripper_fcc_seconds(speed_raw, hold_sec):.0f}s）…")
        route = "ROS 话题" if running else "直测工具"
        if action == "grasp":
            return (f"{name} 闭合抓取：走{route}，限力 {gripper_tau_nm(tau_raw):.2f} N·m、"
                    f"合拢 {speed} …")
        return f"{name} 张开：走{route}，{speed} …"

    @guard_slot
    def _on_action_done(self, side: str, ok: bool, payload, error: str):
        action = self._actions.pop(side, None)
        name = GRIPPER_NAMES[side]
        verb = (action or {}).get("name", "动作")
        if (action or {}).get("tail"):
            # FCC 的进度是边跑边 tail 出来的；只补报还没显示过的那几行，避免同一句出现两次
            self._stop_tail()
            payload = list(payload or [])[self._tail_seen:]
        if ok:
            for line in (payload or []):
                self._report(line)
        else:
            self._report(f"{name} {verb}未能执行：{error or '未知错误'}")
        self._emit_busy()
        self.refresh()
        # 主程序在跑时才有得回读：等轨迹走完再探一次，把"实际张合到哪儿"显示出来
        if self.probe.get("running"):
            QTimer.singleShot(self.READBACK_DELAY_MS, self._readback)

    @guard_slot
    def _readback(self):
        if not self.isVisible():
            return                      # 页面已经关了就别再跑 ROS 命令
        self.on_recheck()

    # ---------- 工具：检测夹爪 / 零点标定 ----------
    @guard_slot
    def on_scan_grippers(self):
        """只扫描夹爪（不做任何改动）：看插好没有、在哪个口、当前角度。"""
        self._run_tool("检测夹爪", lambda: self.launcher.scan_gripper_ports())

    @guard_slot
    def on_calibrate(self):
        """零点标定：**二次确认**后跑扫描 → 标定 → 再扫描。"""
        reason = self._blocked_reason()
        if reason:
            self._report(f"{reason}，未执行标定")
            return
        if self.probe.get("running"):
            self._report("主程序在跑时串口被占，未执行标定 —— "
                         "请先在「关闭程序」里停掉主程序（标定程序自己的提示也是先停 ROS）")
            return
        if self._tool_busy():
            self._report("已有工具动作在跑，未执行标定")
            return
        if QMessageBox.question(
            self, "确认零点标定",
            "这一步会把夹爪「当前物理位置」写成零位（永久写进电机），做法是：\n"
            "  1. 夹爪断电、拔线；\n"
            "  2. 手动把两指推到「完全夹紧」；\n"
            "  3. 重新插线供电，然后点「是」。\n\n"
            "如果现在两指没有完全夹紧，标定出来的零位就是错的 —— "
            "以后每次抓取都会偏。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            self._report("已取消零点标定")
            return
        self._run_tool("零点标定", lambda: self.launcher.calibrate_gripper())

    def _run_tool(self, label: str, task):
        """跑一个独占串口的工具动作（扫描 / 标定）。"""
        if self._tool_busy():
            self._report(f"{label}：已有工具动作在跑（{self._tool['name']}），等它出结果")
            return
        if any(side in self._actions for side in self.rows):
            self._report(f"{label}：有夹爪动作正在跑，等它出结果 —— 串口要独占")
            return
        self._tool = {"name": label, "thread": None}
        self._emit_busy()
        self._apply_buttons()
        self._report(f"{label}：开始（要独占串口，请勿同时操作夹爪）…")
        thread = LocalTaskThread(task)
        self._tool["thread"] = thread
        thread.finished.connect(
            lambda ok, payload, error: self._on_tool_done(label, ok, payload, error))
        thread.start()

    @guard_slot
    def _on_tool_done(self, label: str, ok: bool, payload, error: str):
        self._tool = None
        if ok:
            for line in (payload or []):
                self._report(line)
        else:
            self._report(f"{label}未能执行：{error or '未知错误'}")
        self._emit_busy()
        self.refresh()

    # ---------- FCC 进度：tail 脚本的输出 ----------
    def _fcc_log_path(self, side: str) -> Path:
        """FCC 抓取的输出落盘位置（上位机自己的 logs/ 下，不进交付包）。"""
        directory = PROJECT_ROOT / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return directory / f"gripper_fcc_{side}_{stamp}.log"

    def _start_tail(self, path: Path):
        """开始 tail 一个文件。

        FCC 一次可能几十秒（夹持时长最长 60s），那条子进程的输出只有落到文件里才看得到 ——
        不 tail 的话界面会安静几十秒，现场会以为卡住了。
        """
        self._tail_path = path
        self._tail_pos = 0
        self._tail_seen = 0
        self._tail_timer.start(self.TAIL_MS)

    @guard_slot
    def _poll_tail(self):
        for line in self._read_tail():
            self._report(line)

    def _read_tail(self) -> list:
        """读出文件里新增的非空行（并推进读取位置）。"""
        if self._tail_path is None:
            return []
        try:
            with open(self._tail_path, encoding="utf-8") as handle:
                handle.seek(self._tail_pos)
                fresh = handle.read()
                self._tail_pos = handle.tell()
        except (OSError, ValueError):
            return []
        lines = [line.rstrip() for line in fresh.splitlines() if line.strip()]
        self._tail_seen += len(lines)
        return lines

    def _stop_tail(self):
        """停 tail，并**把最后一批行读进来**。

        不补这一次的话，进程刚结束那一刻还没被 tail 到的收尾几行会永远不显示
        （而"完成/用时"那几行恰好就是结论）。
        """
        if self._tail_path is None:
            return
        for line in self._read_tail():
            self._report(line)
        self._tail_timer.stop()
        self._tail_path = None

    def _emit_busy(self):
        """把"还有动作在跑"报给主窗口（任一侧动作或工具动作在跑即真）。"""
        self.busy_changed.emit(bool(self._actions) or self._tool_busy())

    def _report(self, line: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.result.append(f"[{stamp}] {line}")
        self._fit_console()
        bar = self.result.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---------- 生命周期 ----------
    def showEvent(self, event):
        super().showEvent(event)
        # 首次打开自动探一次；之后靠「重新检测」与动作后的回读刷新
        if not self.probe and not self._probing:
            self.on_recheck()
        else:
            self.refresh()
        if not self._screen_fitted:             # 屏幕装不下就收进屏幕（只做一次）
            self._screen_fitted = True
            fit_dialog_to_screen(self, shrink=self._shrink_for_screen)

    def closeEvent(self, event):
        # 只停 FCC 的 tail 定时器；动作线程结束时会自己把 busy 放开（`_emit_busy`），
        # 所以中途关掉页面不会让主窗口的「关闭程序」闸门卡死。
        # **不清 `_actions`**：在跑的动作仍要被判定完（收尾时才放开闸门）。
        self._tail_timer.stop()
        self._tail_path = None
        super().closeEvent(event)
