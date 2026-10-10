"""「姿势控制」页的**模块使能与躯干模式**（Mixin，v6.2.0 拆出来的）。

从 `posture.py` 拆出来的（那个文件一度到 1068 行，过了 1000 行红线；红线是
`test_ui_structure.py` 守着的，别绕过它）。拆的缝：**"让机构进入可控状态"与"让躯干处于
正确的模式"是同一件事**（都是这一页的前置条件、都走 `MODULE_STEPS` 的同一批话题），
而"怎么运动、怎么确认到位"是另一件事 —— 前者进这里，后者留在 `posture.py` /
`motion_runner.py`。

三条不能弄错的规矩（都是现场踩出来的）：

1. **一个模块一个按钮，文案随状态走**：检测到已使能就只显示「下使能」，否则「上使能」。
   两个按钮都摆出来等于把"该点哪个"推给现场，而界面每 400ms 读一次状态，它明明知道。
2. **成没成不在这里下结论**：上/下使能只保证"发出去了"，生效与否靠轮询状态翻不翻转；
   超时就如实报"未确认"，绝不报"已完成"（v6.1.0 修过的真 bug）。
3. **躯干模式**（v6.2.0）：上使能**结束时必须是非实时**（`motor_mode 0` + 重发 connect，
   见 `core_operations.MODULE_STEPS`）；而遥操作的上电时序会把躯干切成实时
   （`connect → motor_mode 1 → connect`）—— 所以这里记一个"遥操作跑过几次"的计数，
   只在"确实可能不对"时提示，并提供一键切回（那一下会抬头，属动机构，所以不自动做）。
"""

import time

from PyQt5.QtWidgets import QMessageBox

from .. import theme
from ..core_operations import (MODULE_NAMES, MODULE_STATE_OFF, MODULE_STATE_ON,
                              MODULE_STATE_PARTIAL, MODULE_STATE_TEXT,
                              MODULE_STATE_UNKNOWN, MODULE_STEPS,
                              module_state_of)
from .safety import guard_slot
from .threads import LocalTaskThread

#: 模块使能动作等状态翻转的上限（秒）：与机器人侧上电时序同一量级
MODULE_POLL_TIMEOUT_SEC = 15.0


class PostureModulesMixin:
    """`PostureDialog` 的模块使能 / 躯干模式部分。

    依赖宿主上已有的属性（`view` / `launcher` / `module_rows` / `module_frame` /
    `_busy` / `_report` / `_apply_buttons` / `refresh`）—— 它们都不在这里构造，
    这样这一册只讲"使能与模式"，版面与数据留在各自那本册子里。
    """

    def _module_state(self) -> dict:
        state = self.view.get("module_state")
        return state if isinstance(state, dict) else {}

    def _enabled(self, module: str) -> str:
        """`on` / `off` / `partial` / `unknown` —— 判据在 core_operations，别在这里重算。"""
        return module_state_of(self._module_state(), module)

    def _enable_reason(self, group: str) -> str:
        """这一组现在能不能动；不能就返回一句原因（空串 = 可以）。

        **这是运动类操作的唯一门禁**：躯干、手臂、整组、预设都从这里问。
        v5.6.0 的 `_run_arm_preset` 漏了这一步 —— 双臂没使能也照样"发出去"并报"执行完成"，
        现场看到的就是"预设点了没反映"（见交付说明第 7 条）。

        v6.2.0 加了**遥操作互斥**：遥操作运行中，躯干与双臂由手柄/VR 那边连续控制
        （手臂还在 follow 模式下，`ArmMove` 会被忽略；躯干是实时流）。这时候从本页插手
        发一条点位目标，轻则没反应、重则和手柄"抢"机构 —— 而且两边都不报错。
        所以直接挡住并说明"先停止遥操作"，而不是发出去再说。
        """
        if self._busy:
            return "正在执行上一组动作，等它出结果"
        if self._teleop_running():
            return ("遥操作运行中：躯干与双臂由手柄控制（手臂处于跟随模式）"
                    "—— 先用主界面上的「停止遥操作」停下来再动这里")
        if not self.view.get("available"):
            return self.view.get("reason") or "读不到机器人状态（状态监控没在跑？）"
        module = "trunk" if group == "trunk" else "arms"
        name = "躯干" if module == "trunk" else "双臂"
        state = self._enabled(module)
        if state == MODULE_STATE_UNKNOWN:
            return f"{name}使能状态未知"
        if state == MODULE_STATE_OFF:
            return f"{name}未使能（用上面的「{name} 上使能」接通）"
        if state == MODULE_STATE_PARTIAL:
            return "双臂只接通了一侧（先补齐另一侧）"
        return ""

    # ---------- 遥操作 / 躯干模式（v6.2.0） ----------
    def _teleop_running(self) -> bool:
        """遥操作是否在跑（主窗口上的那个标志；本页只读不写）。"""
        return bool(getattr(self.parent(), "teleop_running", False))

    def _teleop_maybe_realtime(self) -> bool:
        """自上次确认非实时以来，遥操作又跑过 —— 躯干可能停在实时模式。

        计数的而不是布尔量：遥操作**停了之后**这个判断依然要有意义（停止遥操作不会把
        躯干切回非实时），所以只能比"跑过几次"。
        """
        runs = getattr(self.parent(), "teleop_runs", 0) or 0
        return runs != getattr(self, "_teleop_runs_seen", 0)

    @guard_slot
    def on_ensure_trunk_mode(self):
        """把躯干切回非实时（`motor_mode 0` + 重新 `connect`）。

        **会动机构**（那一下躯干会抬头回默认姿态），所以先确认；成功后把"已确认"这个
        计数推进一格，提示行随之消失。
        """
        if self._busy:
            self._report("正在执行上一组动作，等它出结果再点")
            return
        if QMessageBox.question(
            self, "确认切回非实时模式",
            "将向躯干下发：切非实时模式（motor_mode 0）→ 重新 connect。\n"
            "注意：这一下躯干会抬头回默认姿态，请先确认现场无人处于机构运动范围内。\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            self._report("已取消「切回非实时」")
            return
        self._busy = True
        self._emit_busy()
        self._apply_buttons()
        self._report("正在切回非实时模式（motor_mode 0 → connect）…")
        thread = LocalTaskThread(lambda: self.launcher.ensure_trunk_non_realtime())
        thread.finished.connect(self._on_ensure_mode_done)
        thread.start()

    @guard_slot
    def _on_ensure_mode_done(self, ok: bool, payload, error: str):
        self._busy = False
        self._emit_busy()
        if ok and isinstance(payload, dict) and payload.get("ok"):
            for step in payload.get("steps") or []:
                self._report(f"    {step.get('name')}：已发 {step.get('sent', 0)} 次"
                             f"（订阅者 {step.get('matched', -1)}）")
            self._teleop_runs_seen = getattr(self.parent(), "teleop_runs", 0) or 0
            self._report("✅ 躯干已切回非实时模式（点位目标由控制节点内部插值）")
        else:
            reason = ""
            if isinstance(payload, dict):
                reason = payload.get("reason") or ""
            self._report(f"⚠ 切回非实时未完成：{reason or error or '通道没有回话'}"
                         f" —— 可到机器人终端确认 motor_mode")
        self.refresh()


    # ================= 模块使能（并入本页） =================
    @guard_slot
    def on_module_action(self, module: str, enabled: bool):
        """上/下使能：上使能先确认，然后后台下发，再轮询状态是否翻转。

        与「模块使能」独立页同一套约定：只挡**同一模块**的重复触发（两个模块不同话题、
        不同控制节点，互不阻塞）；动作中对外抛 `busy_changed` 让主窗口拦「关闭程序」。
        """
        name = MODULE_NAMES[module]
        if module in self._module_actions:
            self._report(f"{name}的动作还在进行中，等它出结果再点")
            return
        if not self.view.get("available"):
            self._report("状态读不出来，未下发任何指令：先启动状态监控")
            return
        if enabled and QMessageBox.question(
            self, f"确认{name}上使能",
            f"将向{name}下发使能指令：机构会立即进入可控状态"
            f"（{'躯干会抬头' if module == 'trunk' else '手臂会开始跟随'}）。\n"
            "请先确认现场无人处于机构运动范围内。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            self._report(f"已取消{name}{'上' if enabled else '下'}使能")
            return
        action = {"enabled": enabled,
                  "deadline": time.monotonic() + MODULE_POLL_TIMEOUT_SEC,
                  "thread": None}
        self._module_actions[module] = action
        self._emit_busy()
        self._report(f"{name}{'上' if enabled else '下'}使能：正在下发指令"
                     f"（{len(MODULE_STEPS[(module, enabled)])} 步）…")
        # 线程先存进 action 再 start：start() 里可能同步就跑完（测试替身是同步的）
        thread = LocalTaskThread(
            lambda m=module, e=enabled: self.launcher.set_module_enabled(m, e))
        action["thread"] = thread
        thread.finished.connect(
            lambda ok, payload, error, m=module: self._on_module_sent(m, ok, payload, error))
        thread.start()

    @guard_slot
    def _on_module_sent(self, module: str, ok: bool, payload, error: str):
        """指令下发回来的回报。**不下结论** —— 结论由轮询状态翻转得出。"""
        name = MODULE_NAMES[module]
        if ok:
            for line in (payload or []):
                self._report(line)
            self._report(f"等待{name}状态翻转…")
        else:
            action = self._module_actions.pop(module, None)
            enabled = (action or {}).get("enabled", False)
            self._emit_busy()
            self._report(f"{name}{'上' if enabled else '下'}使能指令下发失败："
                         f"{error or '未知错误'}")
        self.refresh()

    def _collect_module_actions(self):
        """轮询：状态翻转了就收尾；超时就如实报未确认。"""
        for module in list(self._module_actions):
            action = self._module_actions[module]
            want = MODULE_STATE_ON if action["enabled"] else MODULE_STATE_OFF
            state = self._enabled(module)
            if state == want:
                self._module_actions.pop(module, None)
                self._emit_busy()
                self._report(f"✅ {MODULE_NAMES[module]}"
                             f"{'上' if action['enabled'] else '下'}使能完成"
                             f"（状态已翻转：{MODULE_STATE_TEXT[state]}）")
                self._apply_head()
                self._apply_buttons()
            elif time.monotonic() >= action["deadline"]:
                if state == MODULE_STATE_PARTIAL and action["enabled"]:
                    continue                       # 双臂只通了一侧：再等一会儿（另一侧通常随后）
                self._module_actions.pop(module, None)
                self._emit_busy()
                self._report(f"⚠ {MODULE_NAMES[module]}"
                             f"{'上' if action['enabled'] else '下'}使能未确认："
                             f"{MODULE_POLL_TIMEOUT_SEC:.0f} 秒内状态没有翻转"
                             f"（当前：{MODULE_STATE_TEXT[state]}）—— 可再点一次，"
                             f"或到机器人终端看节点是否在跑")
                self._apply_head()
                self._apply_buttons()

    def _apply_module_buttons(self):
        """每个模块的按钮随检测到的状态走（现场要求）。

        主按钮的判定规则只有一条：**已使能 → 这一次点击是「下使能」；其余（未使能 /
        只通一侧 / 读不到）→ 「上使能」**。只通一侧时按"上使能"处理是对的 —— 补的是
        没通的那一侧；读不到状态时按钮禁用并写明原因（`monitor_ok` 为假），不打哑谜。

        v6.3.7：**只通一侧时额外出现「全部下电」**。那一态下主按钮是「上使能」，
        已通的那一侧原本没有下电出口（现场只能去关程序或断电）。只在 partial 出现，
        正常两态不显示，避免把"该点哪个"推回给现场。
        """
        monitor_ok = bool(self.view.get("available"))
        for module, row in self.module_rows.items():
            state = self._enabled(module)
            name = MODULE_NAMES[module]
            busy = module in self._module_actions
            turn_on = state != MODULE_STATE_ON
            partial = (state == MODULE_STATE_PARTIAL)
            row.set_next_action(turn_on)
            row.set_partial(monitor_ok and partial)
            row.btn.setEnabled(monitor_ok and not busy)
            # 下电按钮与主按钮同生共死：动作进行中一起禁掉，避免连点两个出口
            row.btn_disable.setEnabled(monitor_ok and not busy)
            if not monitor_ok:
                why = "状态读不出来：先启动状态监控（点「启动程序」会自动起）"
            elif busy:
                why = f"{name}的使能动作正在执行，等它出结果"
            elif turn_on and partial:
                why = f"{name}只通了一侧，点这里补齐另一侧（会二次确认）"
            elif turn_on:
                why = "上使能（会二次确认；机构会立即进入可控状态）"
            else:
                why = "下使能（机构失去保持力）"
            row.btn.setToolTip(why)

    def _emit_busy(self):
        """把"还有动作在跑"报给主窗口（姿势动作或任一模块动作在跑即为真）。"""
        self.busy_changed.emit(bool(self._module_actions) or self._busy)

