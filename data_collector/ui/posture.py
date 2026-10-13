"""「姿势控制」独立页面：躯干 6 轴 + 双臂 14 轴的真实角度与目标角控制、姿势预设、模块使能。

与「夹爪控制」「末端参数」同一族（独立非模态、单例、动作中拦「关闭程序」、统一卡片骨架），
但它是**唯一会连续动机构**的一页：点一下就能让躯干或手臂走到某个姿势。所以每条纪律都围绕
"别让人在没看清的时候把机器动了"：

1. **真实角度只来自机器人侧的 state 话题**（躯干 `trunk_state`、手臂 `arm_state`），
   读不到就显示「未连接」/「—」并把字号降级 —— **绝不显示 0 或推算值**
   （0° 是合法角度，用它冒充"读不到"会让人以为机器正立着）。
2. **点预设 / 点运动一律先二次确认**，确认框里带**示意图**（按 URDF 正运动学画的躯干侧视 /
   双臂正视 + 侧视，见 `posture_draw.py`）与"当前 → 目标（差值）"；差值 > 30° 时追加一句
   "确认周围无遮挡"。
3. **越界只拦下、不静默钳位**；未使能时运动类按钮禁用**并写明原因**
   （`_enable_reason` 是唯一入口 —— 躯干、手臂、整组、预设都走它）。
4. **全部走非实时（点位）模式**（v6.1.1，现场 2026-09-28：「改回非实时模式控制，进入直接就是
   非实时模式，不需要重新连接」）：躯干一条 `Float32MultiArray`（度）由节点内部插值、
   手臂一条 `ArmMove` 由臂内部插值 —— 手动调节与预设**走同一条**。
   于是两条约束回来了：**两条躯干目标之间 ≥1.5s**（`MotionRunner` 等够并说明原因）、
   完成判据是"等插值时长 / 等 `*_arm_moving` 归 false"。
   本页**不主动切模式、也不做任何"重连"动作**（那一下会动机构，且模式无法回读）；
   页面只说"本页按非实时模式下发"。
5. **多步预设可中止**，中止后如实报"停在第 N 步"。
6. **模块使能并入本页**（v6.1.0 起独立页已删）：上使能是这一页的前置条件，所以两个模块
   各一个按钮就在卡头下面；判据与步骤仍走 `MODULE_STEPS`（与「关闭程序」前的下电序列同一批话题）。

轴名不带中文（现场要求）：躯干 `H1`~`H6`（下 → 上，脚踝 → 颈）、双臂 `L1`~`L7` /
`R1`~`R7`（从肩起）、手 `F1`~`F6`。实体含义只放在悬停提示里。
"""
import time
from datetime import datetime

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QMessageBox, QVBoxLayout

from .. import theme
from ..core_motion import (ARM_AXES, ARM_NAMES, MotionRunner, TRUNK_AXES,
                           arm_move_step, arm_preset_steps, check_arm_steps,
                           check_axis_values, check_trunk_values,
                           delete_custom_preset, describe_step, fmt_number,
                           preset_overview, save_custom_preset, trunk_preset_steps,
                           trunk_target_step)
from ..core_operations import (MODULE_STATE_ON, MODULE_STATE_PARTIAL,
                              MODULE_STATE_TEXT, read_robot_status)
from .common import SP, dialog_card, fit_console, fit_dialog_to_screen, sync_layout
from .posture_dialogs import PresetEditDialog, _MoveConfirmDialog
from .posture_modules import PostureModulesMixin
from .posture_cards import PostureCardsMixin
from .posture_widgets import (UNKNOWN, ArmDiagram, PresetCard, TrunkDiagram,
                              group_of, specs_for)
from .safety import guard_slot
from .threads import LocalTaskThread

#: 差值超过这个角度时，确认框里追加一句"确认周围无遮挡"
BIG_DELTA_DEG = 30.0


def _padded_trunk(values) -> list:
    """躯干预设只有 3 个值（脚踝 / 膝 / 大腿）→ 补成 6 轴（腰 / 颈回中）。"""
    raw = [float(item) for item in values or []]
    return (raw + [0.0] * len(TRUNK_AXES))[:len(TRUNK_AXES)]


def preset_summary(kind: str, item: dict) -> str:
    """预设卡第二、三行的摘要：这一条**到底会做什么**。

    躯干那句 v6.3.0 从 `0 / 0 / 0 → 6 轴` 改成 `0 / 0 / 0 · 腰颈回中`：
    旧写法只说了"最后是 6 个轴"，没说 H4~H6 会被放到哪儿 —— 而文件里**只有 3 个值**，
    剩下 3 个轴到底保持不动还是回中，正是看卡片的人要判断的事（答案是回中）。

    **折成两行**（v6.3.2，用显式的 ``\n``）：

    数值与说明各占一行。原因是**卡片的宽度直接决定整页的宽度**（预设那一行是这页最宽的
    一行），而"摘要要一行显示完"要求卡片 ≥157 —— 页宽就被顶到 900 以上，
    于是轴表三列吃满整宽之后在轴名右侧留下 100 多 px 的空白（现场截图里框出来的那块）。
    折成两行之后卡最小宽掉到 ~124，页宽才收得下去。

    为什么用显式换行而不是 ``setWordWrap(True)``：QLabel 一旦开自动换行，它的高度就依赖
    宽度（`heightForWidth`），而卡片是**定高**的 —— 在网格里会和布局反复协商，
    实测直接把进程搞成段错误（与 v6.3.0 `_ElidedLabel` 那次同类的回环）。
    显式 ``\n`` 的行数是确定的，不参与宽度协商。
    """
    if kind == "trunk":
        padded = _padded_trunk(item.get("values"))
        # 数字之间**不留空格**（v6.3.0）：`30 / 54 / 24` 里的空格纯属浪费宽度。
        shown = "/".join(fmt_number(value) for value in padded[:3])
        return f"{shown}\n腰颈回中"
    steps = item.get("steps") if item.get("source") != "custom" else item.get("value")
    steps = steps or []
    sides, hand = [], False
    for step in steps:
        if not isinstance(step, dict):
            continue
        for side in ("left", "right"):
            if step.get(f"{side}_arm") and side not in sides:
                sides.append(side)
        hand = hand or bool(step.get("left_end_effector") or step.get("right_end_effector"))
    parts = [f"{len(steps)} 步" if len(steps) != 1 else "1 步"]
    if len(sides) == 2:
        parts.append("双臂")
    elif sides:
        parts.append(f"{ARM_NAMES[sides[0]][0]}臂")
    if not parts:
        return "（空预设）"
    # 与躯干摘要同样的理由折行（见 `preset_summary` 的说明）：步数/臂 一行，手型 一行。
    head = " · ".join(parts)
    return f"{head}\n含手型" if hand else head


def preset_tooltip(kind: str, item: dict) -> str:
    lines = ["来源：" + ("上位机自定义（右键删除）"
                        if item.get("source") == "custom" else "机器人侧 JSON（只读）")]
    if item.get("problems"):
        lines.append("问题：" + "；".join(item["problems"]))
    if kind == "trunk":
        values = item.get("values") or []
        lines.append("H1~H3（脚踝 / 膝 / 大腿）："
                     + "、".join(fmt_number(value) for value in values) + "（度）")
        lines.append("下发时补成 6 轴：腰 / 颈按 0 回中"
                     "（卡上那一行只写前三个值）")
    else:
        steps = item.get("steps") if item.get("source") != "custom" else item.get("value")
        for index, step in enumerate((steps or [])[:4], start=1):
            if not isinstance(step, dict):
                continue
            moved = [ARM_NAMES[side] for side in ("left", "right") if step.get(f"{side}_arm")]
            hand = "、".join(ARM_NAMES[side] for side in ("left", "right")
                            if step.get(f"{side}_end_effector"))
            bits = []
            if moved:
                bits.append("`" + "、".join(moved) + "`".replace("`", ""))
            if hand:
                bits.append(f"手型（{hand}）")
            lines.append(f"第 {index} 步：" + ("+".join(bits) or "只有 wait_ms")
                         + (f"，wait {step['wait_ms']}ms" if step.get("wait_ms") else ""))
        if len(steps or []) > 4:
            lines.append(f"…还有 {len(steps) - 4} 步")
    lines.append("点卡片 → 二次确认（含示意图）→ 运动；点「改」→ 编辑/另存")
    return "\n".join(lines)


# ============================================================
# 页面
# ============================================================
class PostureDialog(PostureModulesMixin, PostureCardsMixin, QDialog):
    """姿势控制页面：四张卡（姿势控制+模块使能 / 角度与各轴调节 / 姿势预设 / 操作结果）。"""

    #: 页宽是**量出来的**（v6.3.2 第三轮收紧）。页宽的下限由**最宽的那一行**决定，
    #: 不是由轴表 —— 上下排列之后一页的宽 = 「某一行要的宽」，不再是两张卡之和。
    #:
    #: * **预设那一行最宽**：卡最小宽 ≈124（摘要在卡内**折成两行**，最长一行
    #:   `60/108/48` 59px / 名字 `KneeBend_1` 66px 取大 + 间距 8 + 「编辑」34 + 内距 16），
    #:   躯干 3 列 → 整行 ≈ 388；手臂 2 列 → 232。
    #: * 轴表三列各需 216（轴名 26 + 当前 62 + → 12 + 目标 74 + 间距），合计 648 —— 窄得多。
    #:
    #: 取 **860**：卡实得 ≈155（名字 `KneeBend_1` 完整 + 摘要折两行完整），
    #: 轴列实得 ≈273（内容 214，富余 59 **拆成对称两段各 29** —— 见 `AxisRow` 里
    #: 那两个 stretch 的说明）。v6.3.1 是 1040、富余 108 全堆在轴名后一处，
    #: 现场截图里框出来要消掉的正是它。
    #: **不能再小**：800 实测卡宽只有 138，名字被截成 `KneeBend…`；
    #: 再往下（760）预设区会出**横向滚动条**，第 3 张卡被切掉。
    MIN_WIDTH = 860
    #: 结果区**最少**几行 —— 只在屏幕真不够时才会落到底（`_shrink_for_screen` 第一步）。
    #: **2 → 1（v6.4.16）**：这一条是"1080P + 1.33 缩放"下把页面塞进屏幕的最后一块余量。
    #: 平时看到的是 `CONSOLE_MAX_ROWS`（4）行，只有页面被压到极限时才只剩 1 行 ——
    #: 那时用户还能拖大窗口或按 F11 全屏换回更多行。
    CONSOLE_MIN_ROWS = 1
    #: 结果区默认最多几行。**8 → 4（v6.4.16）**：
    #: 这一页是"看一眼上一条结果就走"的用法，8 行从来没被用满，却白占 ~60px 页高。
    #: 而这一页在 1080P 上正卡在"底部按钮被顶出屏幕"的临界点，屏幕不够时它又是
    #: 第一个被压的（`_shrink_for_screen`），下限本来就是 2 —— 默认给 4 是
    #: "平时也不浪费、不够时还有得压"的折中。要翻旧日志仍可拖大或滚。
    CONSOLE_MAX_ROWS = 4

    #: 结果区**被压缩后的上限**（`_shrink_for_screen` 第一步把它降下来；None = 没压过）。
    #:
    #: ⚠️ 为什么需要它：`refresh()` 每次都会调 `_fit_console()`，而那个方法按
    #: `CONSOLE_MAX_ROWS` 恢复行数 —— 于是"压完 500ms 就被撤销"，
    #: 现场表现是"刚打开时好好的，过一会儿底下又出去了"（v6.4.16 实测踩到：
    #: 直接调 `_shrink_for_screen` 量到的下限是 1021，等下一个刷新周期回来又变 1078）。
    #: 压缩必须写进**状态**，不能只改一次控件高度。
    _console_rows_capped = None

    busy_changed = pyqtSignal(bool)
    #: 执行器在后台线程里报进度（跨线程只走信号）
    progress_line = pyqtSignal(str)

    def __init__(self, parent=None, config=None, launcher=None):
        super().__init__(parent)
        self.config = config
        self.launcher = launcher
        self.view = {}
        self.presets = {}
        self.rows = []
        self.row_of = {}
        self._busy = False
        self._loading = False
        self._fitting = False
        self._console_width = 0
        self._runner = None
        self._thread = None
        self._last_sent = {}
        self._hand_last = {"left": None, "right": None}
        self._preset_shown = {"trunk": False, "arm": False}
        self._preset_cards = {"trunk": {}, "arm": {}}
        self._selected = None
        #: 模块动作（与独立页同一约定：**按模块**记，互不阻塞）
        self._module_actions = {}
        #: 上次"确认躯干处于非实时"时，遥操作已经跑过几次（v6.2.0）。
        #: 遥操作会把躯干切实时，所以两者不一致就说明模式可能不对（见 `_teleop_maybe_realtime`）
        self._teleop_runs_seen = getattr(parent, "teleop_runs", 0) or 0

        self.setWindowTitle("姿势控制")
        self.setModal(False)

        layout = QVBoxLayout(self)
        # 页内距 16 → 8、卡片间距 12 → 4（v6.3.0 两轮收紧）：两张卡上下排列之后这一页
        # 同时受宽和高两头挤（现场 2026-09-29 的截图里右侧整条被切掉）。分块感靠卡片自己
        # 的白底与描边维持，不靠间距 —— 所以这两个值可以一路收到"不贴在一起"为止。
        layout.setContentsMargins(SP["sp1"], SP["sp1"], SP["sp1"], SP["sp1"])
        layout.setSpacing(SP["sp1"])
        layout.addWidget(self._build_head_card())
        # 角度卡与预设卡**上下排列**（现场 2026-09-29 要求「改为上下而不是左右」）。
        # 左右并排时两张卡各只有一半宽度：预设卡那一列两张子列表各 ~190px，
        # 卡片摘要只能显示到"30/54/24 · 腰颈…"；角度卡三列也被压到贴住最小宽。
        # 改成上下之后两张卡都拿满 1148px，摘要不再截断、三列轴表也不挤。
        # 代价是页高（见 `MIN_WIDTH` 上面的说明）——所以列表高度单独调过。
        layout.addWidget(self._build_axis_card())
        layout.addWidget(self._build_preset_card())
        layout.addWidget(self._build_result_card())
        layout.addLayout(self._build_button_row())

        self.setMinimumWidth(self.MIN_WIDTH)
        #: 是否已经按屏幕高度收过一次（只在首次显示时收 —— 用户手动拖过大小之后不该再动它）
        self._screen_fitted = False
        self.progress_line.connect(self._report)

        # 首次读取：状态 + 预设 + 把目标框初始化成当前角度
        self.presets = preset_overview(self.config)
        self.view = read_robot_status(self.config)
        self._load_targets_from_current()
        self.refresh()
        self._report(f"读取数据：{self.view.get('path') or '—'}")
        if not self.view.get("available"):
            self._report("读不到机器人状态：" + (self.view.get("reason") or "未知原因"))
        self._report("轴名：躯干 H1~H6（下→上）、双臂 L1~L7 / R1~R7（从肩起）；单位 °")
        self._report("运动方式：非实时模式（点位目标：躯干 / 手臂都由控制节点内部插值）")

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ================= 刷新 =================
    @guard_slot
    def refresh(self):
        self.view = read_robot_status(self.config)
        # 预设也要重读：现场"另存为预设"之后要立刻能在卡片里看到它（踩过：只读了状态，
        # 存完的预设要关掉页面重开才出现）
        self.presets = preset_overview(self.config)
        self._apply_head()
        self._apply_angles()
        self._apply_presets()
        self._apply_buttons()
        self._fit_console()

    @guard_slot
    def _on_tick(self):
        """周期刷新：真实角度会变，模块使能的动作也会在这里收尾。"""
        if self.isVisible():
            self.view = read_robot_status(self.config)
        self._collect_module_actions()
        if self._busy or not self.isVisible():
            return
        self._apply_head()
        self._apply_angles()
        self._apply_buttons()


    # ================= 卡头 =================
    def _apply_head(self):
        fresh = bool(self.view.get("available"))
        age = float(self.view.get("age_sec") or 0.0)
        if fresh:
            # 秒数并进这一个徽章（v6.3.0）：角度卡原来还有个一模一样的「读数 x.xs 前」，
            # 同一件事报两遍；现在只在顶栏报一次，卡的卡头留给"目标差 + 手型"。
            self.monitor_badge.setText(f"监控 运行中 · {age:.1f}s")
            theme.set_state(self.monitor_badge, "ok")
            self.monitor_badge.setToolTip(
                f"状态文件 {age:.1f}s 前更新：{self.view.get('path') or 'robot_status.json'}")
        else:
            self.monitor_badge.setText("监控 未运行")
            theme.set_state(self.monitor_badge, "warn")
            self.monitor_badge.setToolTip(self.view.get("reason") or "读不到状态文件")

        # 非实时模式是**进入页面即如此**的（现场 2026-09-28：不需要重新连接）——
        # 所以这里只报"本页按哪种模式下发"，不报"准备好了没有"
        # （模式无法回读，报状态就是编；本页也不主动切，避免那一下会动机构）。
        # v6.2.0 补一条**例外**：遥操作的上电时序会把躯干切成实时，那时"进入即非实时"
        # 这个前提就不成立了 —— 所以只在"遥操作又跑过"时，把提示行与一键切回露出来。
        if self._teleop_maybe_realtime():
            self.mode_badge.setText("躯干模式待确认（跑过遥操作）")
            theme.set_state(self.mode_badge, "warn")
            self.mode_notice.setText(
                "遥操作的上电时序会把躯干切成实时模式（关节按最大速度追目标、要上位机"
                "自己插值）—— 本页发的是单条点位目标，实时模式下会「又硬又急」，甚至"
                "看着没反应。点右边按钮切回非实时；注意那一下躯干会抬头回默认姿态。")
            self.mode_row.setVisible(True)
        else:
            self.mode_badge.setText("非实时模式")
            theme.set_state(self.mode_badge, "accent")
            self.mode_badge.setToolTip("本页按非实时（点位）模式下发：躯干一条点位目标、"
                                       "手臂一条 ArmMove，都由控制节点内部插值")
            self.mode_row.setVisible(False)

        # 使能状态**只写在模块条上**（v6.3.0）：顶栏原来还并列一整套「躯干 已使能 /
        # 双臂 已使能」，加上提示行那句，同一件事在 46px 高度里说了三遍。模块条紧贴
        # 上/下使能按钮，"是什么 + 能不能点"在这里合在一处，比顶栏更适合承担它。
        for module in ("trunk", "arms"):
            state = self._enabled(module)
            text = MODULE_STATE_TEXT[state]
            row = self.module_rows[module]
            # 徽章只写状态：行首那个标题已经写了模块名，再重复一遍就是"躯干 躯干 已使能"
            row.state_badge.setText(text)
            theme.set_state(row.state_badge,
                            {MODULE_STATE_ON: "ok",
                             MODULE_STATE_PARTIAL: "warn"}.get(state, "none"))

        emergency = (self.view.get("status") or {}).get("emergency")
        if emergency == "急停":
            self.estop_badge.setText("急停 已触发")
            theme.set_state(self.estop_badge, "danger")
        elif emergency == "解锁":
            self.estop_badge.setText("急停 未触发")
            theme.set_state(self.estop_badge, "ok")
        else:
            self.estop_badge.setText("急停 —")
            theme.set_state(self.estop_badge, "none")

        self.preset_card_tip()

    def preset_card_tip(self):
        """三份文件路径进悬停提示（不占版面高度，排障时鼠标本来就在界面上）。"""
        frame = getattr(self, "preset_note", None)
        if frame is None:
            return
        frame.setToolTip(
            f"躯干预设：{self.presets.get('trunk', {}).get('path', '—')}\n"
            f"手臂预设：{self.presets.get('arm', {}).get('path', '—')}\n"
            f"自定义预设（可改）：{self.presets.get('custom', {}).get('path', '—')}")

    # ================= 角度 =================
    def _angle_values(self, group: str):
        """一组的真实角度；读不到返回 None（界面显示「—」，不编数）。"""
        state = self._module_state()
        if group == "trunk":
            entry = state.get("trunk_state") or {}
            values = entry.get("joint_deg") if isinstance(entry, dict) else None
            return list(values) if isinstance(values, list) and len(values) == 6 else None
        entry = (state.get("arm_state") or {}).get(group)
        values = entry.get("joint_deg") if isinstance(entry, dict) else None
        return list(values) if isinstance(values, list) and len(values) == 7 else None

    def _apply_angles(self):
        for group in ("trunk", "left", "right"):
            values = self._angle_values(group)
            for index, spec in enumerate(specs_for(group)):
                row = self.row_of[spec[0]]
                value = values[index] if values else None
                row.set_current(value)
                row.refresh_style()
        for side in ("left", "right"):
            values = self._hand_last.get(side)
            self.hand_labels[side].setText(
                f"{ARM_NAMES[side]} " + ("、".join(str(int(item)) for item in values)
                                         if values else UNKNOWN))

    @guard_slot
    def _load_targets_from_current(self):
        """目标框初始值 = 当前角度（读不到的组填 0，并在日志里说明是占位）。"""
        self._loading = True
        try:
            for group in ("trunk", "left", "right"):
                values = self._angle_values(group)
                for index, spec in enumerate(specs_for(group)):
                    self.row_of[spec[0]].set_target(values[index] if values else 0.0)
        finally:
            self._loading = False

    # ================= 预设卡 =================
    def _apply_presets(self):
        """按**栅格**重铺预设卡：躯干 3 列、手臂 2 列（见 `PRESET_COLUMNS`）。

        v6.3.0 起预设区是栅格而不是一条竖直列表 —— 卡片横着铺才用得上"两张卡上下排列"
        之后预设卡拿到的整幅宽度（`_build_preset_card` 里有量出来的数）。
        """
        for kind, (_area, layout) in self.preset_areas.items():
            columns = self.PRESET_COLUMNS[kind]
            while layout.count():                          # 清空（栅格不带 stretch 项）
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self._preset_cards[kind] = {}
            entries = self.presets.get(kind, {}).get("presets", {})
            names = list(entries)
            if kind == "arm" and not self._preset_shown[kind]:
                # 默认只列 A / LB（现场用的那两个）；**上位机自定义的永远列出来** ——
                # 那是操作员自己刚存的姿势，藏起来等于白存
                names = [name for name in names
                         if name in ("A", "LB")
                         or entries[name].get("source") == "custom"]
            for index, name in enumerate(names):
                item = entries[name]
                info = dict(item, summary=preset_summary(kind, item),
                            tooltip=preset_tooltip(kind, item))
                card = PresetCard(kind, info, self)
                card.run_requested.connect(self.on_preset)
                card.edit_requested.connect(self.on_edit_preset)
                card.delete_requested.connect(self.on_delete_requested)
                layout.addWidget(card, index // columns, index % columns)
                self._preset_cards[kind][item["key"]] = card

        problems = []
        for kind in ("trunk", "arm"):
            info = self.presets.get(kind, {})
            if not info.get("available"):
                problems.append(f"{'躯干' if kind == 'trunk' else '手臂'}预设读不出来："
                                f"{info.get('reason')}")
        if self.presets.get("custom", {}).get("reason"):
            problems.append("自定义预设：" + self.presets["custom"]["reason"])
        self.preset_badge.setText("预设 " + ("有读不出来的" if problems else "可读"))
        theme.set_state(self.preset_badge, "warn" if problems else "ok")
        self.preset_note.setText("；".join(problems) if problems else
                                 "点卡片 = 运动（会二次确认）；点「编辑」= 改这一条或另存到上位机；"
                                 "右键自定义预设 = 删除")

    # ================= 按钮可用性 =================
    def _apply_buttons(self):
        for group in ("trunk", "left", "right"):
            reason = self._enable_reason(group)
            for spec in specs_for(group):
                self.row_of[spec[0]].set_available(not reason, reason)
            self.group_buttons[group].setEnabled(not reason)
            self.group_buttons[group].setToolTip(
                "把这一组的目标值一次性下发（实时流；会二次确认，含示意图）" if not reason
                else f"不可用：{reason}")

        self.btn_reload.setEnabled(not self._busy)
        self.btn_undo.setEnabled(bool(self._last_sent) and not self._busy)
        self.btn_take_all.setEnabled(not self._busy)
        self.btn_save_preset.setEnabled(not self._busy)
        self.btn_abort.setVisible(self._busy)
        self._apply_module_buttons()

        pending = [row.key for row in self.rows if row.delta() is not None
                   and abs(row.delta()) > 0.05]
        self.adjust_badge.setText(f"待运动 {len(pending)} 轴" if pending else "目标 与当前一致")
        theme.set_state(self.adjust_badge, "accent" if pending else "none")
        self.hint_label.setText(self._hint_text())

    def _hint_text(self) -> str:
        """常驻提示行：**只写操作员要用的话**。

        v6.3.0 改掉了原来那句"角度来自机器人侧 state 话题（躯干 trunk_state / 手臂
        arm_state）" —— 话题名是开发者的语言，现场不需要记（v6.1.1 已经定过同一条措辞
        纪律，那次是"实时流 / 100Hz / follow_position"）。话题名挪进了这一行的悬停提示，
        排障时鼠标本来就在界面上。
        """
        if self._busy:
            return "正在执行动作：可以点「中止动作」停在当前位置（已下发的那一段不会回退）。"
        reason = self._enable_reason("trunk") or self._enable_reason("left")
        if reason:
            return reason
        if self._module_actions:
            return "模块使能的动作正在跑，等它出结果（两个模块互不阻塞）。"
        return "角度是机器人当前的真实读数。目标值双击可改：回车生效、Esc 取消。"

    def _fit_console(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            self._console_width = max(1, self.result.viewport().width())
            upper = self.CONSOLE_MAX_ROWS
            if self._console_rows_capped is not None:
                upper = min(upper, self._console_rows_capped)
            fit_console(self.result, self.CONSOLE_MIN_ROWS, upper)
        finally:
            self._fitting = False

    def resizeEvent(self, event):                              # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        if self.result.viewport().width() != self._console_width:
            self._fit_console()

    # ================= 交互：轴行 =================
    @guard_slot
    def on_row_changed(self, _key: str):
        if self._loading:
            return
        self._apply_angles()
        self._apply_buttons()

    @guard_slot
    def on_row_invalid(self, _key: str, message: str):
        self._report("没有改：" + message)

    @guard_slot
    def on_select_row(self, key: str):
        for row in self.rows:
            row.set_selected(row.key == key)
        self._selected = key

    @guard_slot
    def on_take_row(self, key: str):
        """双击「当前」= 把这个轴的当前角度取到目标（读不到就不动，并说明）。"""
        row = self.row_of.get(key)
        if row is None:
            return
        if not row.on_take_current():
            self._report(f"{key} 的当前角度读不到（未使能 / 无回读），目标值没有变")
            return
        self._report(f"{key} 目标值已取成当前角度 {row.target():.1f}°（没有运动）")
        self._apply_angles()
        self._apply_buttons()

    @guard_slot
    def on_take_all(self):
        filled = 0
        for row in self.rows:
            if row.on_take_current():
                filled += 1
        self._report(f"已把 {filled} 个轴的目标值填成当前角度（读不到的轴不动）")
        self._apply_angles()
        self._apply_buttons()

    @guard_slot
    def on_undo(self):
        if not self._last_sent:
            self._report("还没有下发过，没有可撤销的目标值")
            return
        for key, value in self._last_sent.items():
            row = self.row_of.get(key)
            if row is not None:
                row.set_target(value)
        self._report("已把目标值恢复成上一次下发的那组（只回填输入，没有运动）")
        self._apply_angles()
        self._apply_buttons()

    # ================= 交互：运动 =================
    @guard_slot
    def on_move_axis(self, key: str):
        """单轴运动 = 把它所在**整组**的目标数组发下去（协议粒度就是整组）。"""
        self._start_group_move(group_of(key), highlight=key)

    @guard_slot
    def on_group_move(self, group: str):
        self._start_group_move(group)

    def _start_group_move(self, group: str, highlight: str = ""):
        reason = self._enable_reason(group)
        if reason:
            self._report(f"没有下发：{reason}")
            return
        rows = [self.row_of[spec[0]] for spec in specs_for(group)]
        values = [row.target() for row in rows]
        axes = TRUNK_AXES if group == "trunk" else ARM_AXES[group]
        problems = check_axis_values(axes, values)
        if problems:
            # 越界只拦下并说明（不静默钳位）—— 悄悄改值会让现场以为自己填的值生效了
            self._report("没有下发，这些轴的值不合法：" + "；".join(problems))
            return

        name = {"trunk": "躯干 6 轴", "left": "左臂 7 轴", "right": "右臂 7 轴"}[group]
        summary = []
        biggest = 0.0
        for row in rows:
            current = row.current_value()
            delta = row.delta()
            if delta is not None:
                biggest = max(biggest, abs(delta))
            now_text = UNKNOWN if current is None else f"{current:.1f}°"
            delta_text = "—" if delta is None else f"{delta:+.1f}°"
            pointer = "　←" if row.key == highlight else ""
            summary.append(f"{row.key}　{now_text} → {row.target():.1f}°　（{delta_text}）"
                           f"{pointer}")
        summary.insert(0, f"{name}：{'、'.join(row.key for row in rows)}")
        summary.insert(1, "运动方式：非实时模式（点位目标，控制节点内部插值）")
        extra = (f"最大变化 {biggest:.1f}°（超过 {BIG_DELTA_DEG:g}°）—— "
                 f"先确认机器人运动范围内没有遮挡、没有人。" if biggest > BIG_DELTA_DEG else "")
        if not self._confirm_motion(name, group, values, summary, extra):
            self._report(f"已取消：{name} 没有下发")
            return

        if group == "trunk":
            step = trunk_target_step(f"{name} → " + "、".join(f"{v:.1f}" for v in values),
                                     values)
        else:
            step = arm_move_step(f"{name} 运动", group, values)
        self._last_sent = {row.key: value for row, value in zip(rows, values)}
        self._report(f"{name} 下发内容：{describe_step(step)}")
        self._run([step], name)

    def _confirm_motion(self, name, group, values, summary, extra) -> bool:
        """画示意图：躯干给侧视（手臂作陪衬），手臂给正视（虚线当前 / 实线目标）。"""
        trunk_current = self._angle_values("trunk") or [0.0] * len(TRUNK_AXES)
        arms_now = {side: self._angle_values(side) for side in ("left", "right")}
        if group == "trunk":
            diagram = TrunkDiagram()
            diagram.set_values(trunk_current, values, arms=arms_now)
        else:
            targets = {side: arms_now.get(side) for side in ("left", "right")}
            targets[group] = values
            diagram = ArmDiagram()
            diagram.set_values(trunk_current, targets, currents=arms_now)
        dialog = _MoveConfirmDialog(self, f"确认运动 · {name}", summary, diagram, extra)
        return dialog.exec_() == QDialog.Accepted

    # ================= 交互：预设 =================
    @guard_slot
    def on_show_all(self, kind: str, checked: bool):
        self._preset_shown[kind] = bool(checked)
        self._apply_presets()
        self._report(f"{'躯干' if kind == 'trunk' else '手臂'}预设："
                     + ("显示全部" if checked else "只显示 A / LB"))

    @guard_slot
    def on_preset(self, kind: str, key: str):
        item = self.presets.get(kind, {}).get("presets", {}).get(key)
        if item is None:
            self._report(f"没有找到这条预设（{key}）—— 请点「重新读取状态」刷新")
            return
        if item.get("problems"):
            self._report(f"没有下发 {item.get('name')}：预设本身有问题 —— "
                         + "；".join(item["problems"]))
            return
        # **两条腿都要过门禁**（v6.0.0 修的 bug：手臂预设原来绕过了这一步）
        reason = self._enable_reason("trunk" if kind == "trunk" else "arms")
        if reason:
            self._report(f"没有下发 {item.get('name')}：{reason}")
            return
        if kind == "trunk":
            self._run_trunk_preset(item, key)
        else:
            self._run_arm_preset(item, key)

    def _run_trunk_preset(self, item: dict, key: str):
        raw = [float(value) for value in item.get("values") or []]
        padded = _padded_trunk(raw)
        problems = check_trunk_values(padded)
        if problems:
            self._report(f"没有下发 {item.get('name')}：" + "；".join(problems))
            return
        current = self._angle_values("trunk") or [0.0] * len(TRUNK_AXES)
        biggest = max([abs((current[index] or 0.0) - padded[index])
                       for index in range(len(padded))] or [0.0])
        summary = [f"躯干预设 {item.get('name')}",
                   "运动方式：非实时模式（点位目标，节点内部插值）",
                   "6 轴目标：" + "、".join(fmt_number(value) for value in padded),
                   "（文件里只有 H1~H3，其余轴按 0 补 → 腰 / 颈回中）",
                   "当前 → 目标（H1~H3）：" + "；".join(
                       f"{'H' + str(index + 1)} "
                       f"{UNKNOWN if current[index] is None else f'{current[index]:.1f}°'}"
                       f" → {padded[index]:.1f}°" for index in range(3))]
        extra = (f"最大变化 {biggest:.1f}°（超过 {BIG_DELTA_DEG:g}°）—— "
                 f"先确认机器人运动范围内没有遮挡、没有人。"
                 if biggest > BIG_DELTA_DEG else "")
        diagram = TrunkDiagram()
        arms_now = {side: self._angle_values(side) for side in ("left", "right")}
        diagram.set_values(current, padded, arms=arms_now)
        if not self._confirm(f"确认运动 · 躯干预设 {item.get('name')}", summary, diagram, extra):
            self._report(f"已取消：躯干预设 {item.get('name')} 没有下发")
            return
        self._last_sent = {axis[0]: padded[index] for index, axis in enumerate(TRUNK_AXES)}
        self._run(trunk_preset_steps(item.get("name", key), raw),
                  f"躯干预设 {item.get('name')}")

    def _run_arm_preset(self, item: dict, key: str):
        steps_raw = item.get("steps") if item.get("source") != "custom" else item.get("value")
        if not isinstance(steps_raw, (list, tuple)) or not steps_raw:
            self._report(f"没有下发 {item.get('name')}：预设里没有步骤")
            return
        problems = check_arm_steps(steps_raw, item.get("name", key))
        if problems:
            self._report(f"没有下发 {item.get('name')}：预设里有不合法的值 —— "
                         + "；".join(problems[:4]))
            return
        steps = arm_preset_steps(item.get("name", key), steps_raw)
        moved = {"left": [], "right": []}
        for raw in steps_raw:
            if isinstance(raw, dict):
                for side in ("left", "right"):
                    if raw.get(f"{side}_arm"):
                        moved[side] = [float(value) for value in raw[f"{side}_arm"]]
        summary = [f"手臂预设 {item.get('name')}",
                   f"共 {len(steps)} 步" + ("（逐步执行，可中止）" if len(steps) > 1 else ""),
                   "运动方式：非实时（臂内部插值，与手柄 / 示教同一套动作定义）",
                   "要动的手臂：" + ("、".join(ARM_NAMES[side] for side in ("left", "right")
                                              if moved[side]) or "（只动手型）")]
        for step in steps[:5]:
            summary.append("　" + step["label"] + "：" + describe_step(step))
        if len(steps) > 5:
            summary.append(f"　…还有 {len(steps) - 5} 步")
        if any(step["hand"] for step in steps):
            summary.append("含手型下发（灵巧手没有回读，只能按时长等）")

        trunk_current = self._angle_values("trunk") or [0.0] * len(TRUNK_AXES)
        currents = {side: self._angle_values(side) for side in ("left", "right")}
        targets = {side: (moved[side] or None) for side in ("left", "right")}
        diagram = ArmDiagram()
        diagram.set_values(trunk_current, targets, currents=currents)
        if not self._confirm(f"确认运动 · 手臂预设 {item.get('name')}", summary, diagram, ""):
            self._report(f"已取消：手臂预设 {item.get('name')} 没有下发")
            return
        for raw in steps_raw:
            if isinstance(raw, dict):
                for side_name in ("left", "right"):
                    if raw.get(f"{side_name}_end_effector"):
                        self._hand_last[side_name] = [
                            int(value) for value in raw[f"{side_name}_end_effector"]]
        self._last_sent = {}
        self._run(steps, f"手臂预设 {item.get('name')}")

    def _confirm(self, title: str, summary, diagram, extra: str = "") -> bool:
        dialog = _MoveConfirmDialog(self, title, summary, diagram, extra)
        return dialog.exec_() == QDialog.Accepted

    # ---------- 预设：另存 / 改 / 删 ----------
    @guard_slot
    def on_save_preset(self, kind: str):
        """把当前目标框的值存成自定义预设（写上位机自己的那份文件）。"""
        dialog = self._open_editor(kind, name="", trunk_values=None,
                                   arm_values=None, hand_text="")
        if dialog is None:
            return
        name, payload, problems = dialog.payload()
        if problems:
            self._report("没有保存：" + "；".join(problems))
            return
        path = save_custom_preset(self.config, kind, name, payload)
        self._report(f"已保存自定义预设「{name}」→ {path}（机器人侧 JSON 未改动）")
        self.refresh()

    def _open_editor(self, kind: str, name: str, trunk_values, arm_values, hand_text):
        """打开编辑框（新建或修改）；取消返回 None。"""
        if kind == "trunk":
            values = trunk_values or [self.row_of[axis[0]].target() for axis in TRUNK_AXES]
            dialog = PresetEditDialog(self, "trunk", name, values, None)
        else:
            if arm_values is None:
                arm_values = {
                    side: [self.row_of[f"{'L' if side == 'left' else 'R'}{index + 1}"].target()
                           for index in range(len(ARM_AXES[side]))]
                    for side in ("left", "right")}
            if hand_text is None:
                last = self._hand_last
                hand_text = (", ".join(str(int(item)) for item in last["left"])
                             if last.get("left") else "")
            dialog = PresetEditDialog(self, "arm", name, None, arm_values, hand_text)
        return dialog if dialog.exec_() == QDialog.Accepted else None

    @guard_slot
    def on_edit_preset(self, kind: str, key: str):
        """「改」：机器人侧的预设另存成上位机自定义预设（不改机器人侧文件）。"""
        item = self.presets.get(kind, {}).get("presets", {}).get(key)
        if item is None:
            return
        source = item.get("source")
        name = item.get("name", "")
        if kind == "trunk":
            trunk_values = _padded_trunk(item.get("values"))
            dialog = self._open_editor("trunk", name, trunk_values, None, None)
        else:
            steps = item.get("steps") if source != "custom" else item.get("value")
            arm_values, hand_text = self._arm_values_of(steps)
            dialog = self._open_editor("arm", name, None, arm_values, hand_text)
        if dialog is None:
            self._report(f"已取消修改 {name}")
            return
        new_name = (dialog.name_edit.text() or "").strip()
        if source == "robot" and new_name == name:
            # 机器人侧那条不动，重名会让用户以为"改的就是它" —— 强制换个名字
            self._report(f"没有保存：机器人侧的预设不能原地改（交付物），"
                         f"请换一个预设名再保存（例如「{name} · 我的」）")
            return
        new_name, payload, problems = dialog.payload()
        if problems:
            self._report("没有保存：" + "；".join(problems))
            return
        path = save_custom_preset(self.config, kind, new_name, payload)
        self._report(f"已保存自定义预设「{new_name}」→ {path}（机器人侧 JSON 未改动）")
        self.refresh()

    @staticmethod
    def _arm_values_of(steps):
        """从步骤数组里取"末段姿势"（各侧最后一次出现的值）+ 手型文本。"""
        arm_values, hand = {}, ""
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            for side in ("left", "right"):
                values = step.get(f"{side}_arm")
                if isinstance(values, (list, tuple)) and len(values) == 7:
                    arm_values[side] = [float(item) for item in values]
                positions = step.get(f"{side}_end_effector")
                if positions:
                    hand = ", ".join(str(int(item)) for item in positions)
        return arm_values, hand

    @guard_slot
    def on_delete_requested(self, kind: str, key: str):
        """右键卡片：自定义预设删除，机器人侧那份说明为什么不能删。"""
        item = self.presets.get(kind, {}).get("presets", {}).get(key)
        if item is None:
            return
        if item.get("source") != "custom":
            self._report(f"「{item.get('name')}」来自机器人侧的 "
                         f"{'trunk_motion.json' if kind == 'trunk' else 'arm_motion.json'}"
                         f"（交付物），不能在界面上删除 —— 需要调整请用「改」另存一份")
            return
        self.on_delete_preset(kind, str(item.get("name")))

    @guard_slot
    def on_delete_preset(self, kind: str, name: str):
        if delete_custom_preset(self.config, kind, name):
            self._report(f"已删除自定义预设「{name}」")
            self.refresh()
        else:
            self._report(f"「{name}」不是上位机自定义的预设，没有改动任何文件")


    # ================= 中止 / 读状态 =================
    @guard_slot
    def on_abort(self):
        if self._runner is not None:
            self._runner.abort()
            self._report("已请求中止：当前这一段走完就停（已下发的那一段不会回退）")

    @guard_slot
    def on_reload(self):
        self._report("重新读取数据与预设…")
        self.refresh()

    # ================= 执行 =================
    def _run(self, steps, name: str):
        """把步骤交给后台线程执行（含中止、进度、闸门）。"""
        if self._busy:
            self._report("上一组动作还在执行，等它出结果")
            return
        self._busy = True
        self.busy_changed.emit(True)
        self._apply_buttons()
        runner = MotionRunner(self.launcher, steps,
                              status_reader=lambda: read_robot_status(self.config),
                              progress=self.progress_line.emit)
        self._runner = runner
        thread = LocalTaskThread(runner)
        self._thread = thread
        thread.finished.connect(self._on_run_finished)
        self._report(f"开始执行：{name}（{len(steps)} 步）")
        thread.start()

    @guard_slot
    def _on_run_finished(self, ok, payload, error):
        """按**执行器自己的结论**汇报，不看线程的 ok。

        线程的 ok 只表示"那个 callable 没抛异常" —— 中止、半途停住（某步没送达）都是
        **正常返回**，所以拿线程 ok 当成败的话，"停在第 2 步"会被报成"执行完成"。
        那正是这一页最不能有的一种谎：现场会照着一句"执行完成"走开。

        **进度行不在这里重播**：它们已经通过 `progress_line` 实时打过了，
        再打一遍会让"第 1/1 步"在日志里出现两次（看起来像跑了两遍）。
        """
        self._busy = False
        self.busy_changed.emit(bool(self._module_actions))
        payload = payload if isinstance(payload, dict) else {}
        if not payload:
            self._report("执行完成" if ok else f"执行没开始：{error or '未知错误'}")
        elif payload.get("aborted"):
            stopped = payload.get("stopped_at")
            during = payload.get("stop_reason") == "during"
            self._report("已中止 —— 停在"
                         + (f"第 {stopped} 步" if stopped else "中途")
                         + ("（这一步已经下发但没走完，机构停在当前位置）" if during
                            else "之前（这一步没有下发）")
                         + "，后面的步骤没有下发")
        elif payload.get("ok"):
            self._report("执行完成（每一步是否真的到位，见上面每条判据的说明）")
        else:
            stopped = payload.get("stopped_at")
            self._report("执行没有走完："
                         + (f"停在第 {stopped} 步" if stopped else "中途停了")
                         + (f"（{error}）" if error else "，原因见上面"))
        self._runner = None
        self.view = read_robot_status(self.config)
        self._apply_head()
        self._apply_angles()
        self._apply_buttons()

    # ================= 结果区 =================
    def _report(self, line: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.result.append(f"[{stamp}] {line}")
        self._fit_console()
        bar = self.result.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ================= 生命周期 =================
    def showEvent(self, event):                                # noqa: N802 (Qt 命名)
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start()
        self.refresh()
        self._fit_console()
        # 屏幕装不下就收进屏幕（只做一次）。放在 `refresh()` 之后：
        # `_apply_presets` 会重建预设卡，压过的区高要在它之后设才不会被冲掉。
        if not self._screen_fitted:
            self._screen_fitted = True
            fit_dialog_to_screen(self, shrink=self._shrink_for_screen)

    def _shrink_for_screen(self, room: int) -> None:
        """屏幕装不下时按**固定优先级**压缩（`fit_dialog_to_screen` 的回调）。

        顺序与理由都是定死的：

        1. **结果区（日志）先收**：它本来就按内容自适应（`CONSOLE_MIN_ROWS` ~
           `CONSOLE_MAX_ROWS` 行），收到下限只影响"同时能看几条日志"，不动任何操作。
        2. **预设区再收**：从两行卡收到一行。预设区本来就有滚动条，一次少看几条而已，
           6 个躯干预设仍然都够得到。
        3. **轴表最后才压（v6.4.16 新增的一步）**：三列各 214px 是"20 个轴一屏看全"
           （v5.6.0 定的）。这一步原来写的是"一律不碰，真放不下就让它超屏" ——
           **那条规则要改**：超屏的代价是**底部操作按钮在屏幕外、点不到**
           （对话框没有外层滚动条），而压轴表的代价只是"某一列多滚一下"，
           前者严重得多。下限有保底（`AXIS_MIN_ROWS` 行仍完整可见）。

        ⚠️ **每一步之后都重新量一次 `dialog_floor()`，不靠"估算压掉了多少"**。
        v6.4.16 实测踩到：原先按 `before - after` 估算，而"某个控件变矮了"并不等于
        "布局下限变矮了"（结果区本来就在下限、怎么收都是 0），于是 `room` 被算成还剩一点、
        直接 `return`，**后面两步一步都没跑** —— 表现是"可用 1040 的屏上，一个字都没压"。
        改成以 `dialog_floor()` 为准：目标 = 当前下限 − room，哪一步让它达标就停。
        """
        if room <= 0:
            return
        target = self.dialog_floor() - room
        if self.dialog_floor() <= target:
            return

        # 1. 结果区收到下限（**改状态**，不是只改一次高度 —— 见 `_console_rows_capped`）
        self._console_rows_capped = self.CONSOLE_MIN_ROWS
        self._fit_console()
        sync_layout(self)
        if self.dialog_floor() <= target:
            return

        # 2. 预设区收到一行
        self.set_preset_list_height(PresetCard.height_scaled() + SP["sp1"])
        sync_layout(self)
        if self.dialog_floor() <= target:
            return

        # 3. 轴表压"刚好够"的量，不越过保底线
        floor = self.axis_list_floor()
        now = self._axis_height_now
        want = max(floor, now - (self.dialog_floor() - target))
        self.set_axis_list_height(want)
        sync_layout(self)

    def dialog_floor(self) -> int:
        """布局的**最小高** —— 窗口比它还矮，Qt 只会把内容挤扁（不是留白），
        而"底下那几个按钮怎么点不到"这类现场反馈极难从此反推回来。
        所以压缩之后要拿它兜一道底：宁可超屏（贴顶放，标题栏还在屏内），不要挤扁。
        """
        return self.layout().minimumSize().height()

    def hideEvent(self, event):                                # noqa: N802 (Qt 命名)
        """页面一藏起来就停周期刷新。

        不这么做的话，关掉主窗口之后这个定时器还在跑（对话框是主窗口的子对象，
        `win.close()` 不销毁它），它会在别的事件循环里继续读状态文件、重建预设卡 ——
        既白费力气，也让"窗口都关了还有东西在动"这种时序问题更难查。
        """
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):                               # noqa: N802 (Qt 命名)
        self._timer.stop()
        if self._runner is not None:
            self._runner.abort()                               # 关页面 = 停后续步骤
        super().closeEvent(event)
