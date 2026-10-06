"""逐步执行器：把一串姿势步骤发出去，并按**每步自己的判据**等它完成。

从 `core_motion.py` 拆出来的（v6.2.0：那个文件到 1082 行，过了本项目"单文件 1000 行"
的红线 —— 红线是 `test_ui_structure.py` 守着的，别绕过它）。拆的缝选在**执行**这一层：
`core_motion` 管"要发什么"（轴表、量程、payload、预设 → 步骤），这里管"怎么发、怎么确认"。

只依赖 `core_motion` 的常量与格式化函数（单向），**不反向 import** —— 循环导入会在
"先 import 本模块"时炸掉。`core_motion.MotionRunner` 仍可用（那边用模块级 `__getattr__`
惰性再导出），所以界面与用例的写法一个字没变。

v6.2.0 起这一步的判据换了：**回读关节角确认到位**（常驻通道），确认到位就不再固定睡眠 ——
多步预设的节奏因此变成"到位就走下一步"（现场说的"效果不满意"里，有一半是节奏问题）。
通道起不来时退回原样（固定等待 + 如实标注"未经确认"）。
"""

import time
from typing import Callable, Dict, List, Optional, Sequence

from .core import logger
from .core_motion import (
    ARM_FIXED_WAIT_SEC, ARM_FOLLOW_SET_TOPICS, ARM_MOVE_SETTLE_SEC, ARM_MOVE_TIMEOUT_SEC,
    ARM_NAMES, ARM_SERVOJ_SET_TOPICS, BOOL_MSG, HAND_WAIT_MS, PUBLISH_TIMEOUT,
    STREAM_TIMEOUT_PAD, TRUNK_SETTLE_SEC, TRUNK_TARGET_GAP_SEC, fmt_number,
    stream_job_summary,
)


# ============ 逐步执行器 ============
class MotionRunner:
    """把一串步骤发出去，并按**每步自己的判据**等它完成。

    为什么要有它（而不是循环里直接 `sleep` 固定秒数）：多步预设（LB 8 步）如果全靠固定
    时长，动作会前松后紧 —— 手臂实际几秒到位、躯干插值几秒，都是变量。判据分别是：

    | 步骤类型 | 完成判据 | 兜底 |
    | --- | --- | --- |
    | 躯干（非实时点位）| 节点内部插值 → 等 `TRUNK_SETTLE_SEC` | 步骤的 `wait_ms` 更大时用 `wait_ms` |
    | 手臂（`ArmMove`）| `arm_state.*_arm_moving` 由 true→false | 未见 `moving=True` 或状态读不到 → 固定等待并**标注未确认** |
    | 手 | 等 `wait_ms`（协议无回读） | —— |
    | 两条躯干目标之间 | 至少 `TRUNK_TARGET_GAP_SEC`（文档要求）| —— |

    **v6.1.1 起全部走非实时**（现场 2026-09-28 要求改回）：所以这里是"发一条点位目标 +
    等插值"。实时流那条通道（`streams`）仍被支持（`_stream`），但当前没有任何步骤会用到它 ——
    保留是为了"现场再切回实时"时不用重写，别顺手删。

    **可中止**：`abort()` 从界面线程调用，执行器在每个检查点读 `self._abort`。
    中止后如实报"停在第 N 步"，不假装跑完。

    这个类刻意不碰 Qt：线程包装由界面负责（`LocalTaskThread`），逻辑本身可以单测。
    """

    def __init__(self, launcher, steps: Sequence[dict], *,
                 status_reader: Optional[Callable[[], dict]] = None,
                 progress: Optional[Callable[[str], None]] = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 poll: float = 0.2,
                 arm_timeout: float = ARM_MOVE_TIMEOUT_SEC,
                 trunk_settle: float = TRUNK_SETTLE_SEC,
                 trunk_gap: float = TRUNK_TARGET_GAP_SEC,
                 publish_timeout: float = PUBLISH_TIMEOUT,
                 stream_runner: Optional[Callable] = None,
                 stream_timeout: float = STREAM_TIMEOUT_PAD):
        self.launcher = launcher
        self.steps = list(steps)
        self.status_reader = status_reader
        self.progress = progress
        self.sleeper = sleeper
        self.clock = clock
        self.poll = poll
        self.arm_timeout = arm_timeout
        self.trunk_settle = trunk_settle
        self.trunk_gap = trunk_gap
        self.publish_timeout = publish_timeout
        #: 实时流的执行体：``(jobs, on_line, is_aborted) -> {任务键: True/False/None}``。
        #: 默认用 launcher 的实现；用例注入替身即可完全不碰 ROS。
        #: **v6.1.1 起没有任何步骤会走它**（全部非实时），保留供现场再切回实时时使用。
        self.stream_runner = stream_runner
        self.stream_timeout = stream_timeout
        self._abort = False
        self._last_trunk_at: Optional[float] = None

    # ---------- 中止 ----------
    def abort(self) -> None:
        """请求中止（界面线程调用；执行器在下一个检查点退出）。"""
        self._abort = True

    # ---------- 主流程 ----------
    def __call__(self) -> dict:
        """跑完（或被中止）后返回 ``{"ok", "lines", "aborted", "stopped_at", "stop_reason"}``。

        `stop_reason` 是给界面用的**措辞依据**，三选一：

        * ``"before"`` —— 中止发生在某一步**下发之前**（那一步一条都没发）；
        * ``"during"`` —— 中止发生在某一步**执行当中**（那一步已经发了，没走完）；
        * ``"failed"`` —— 不是中止，是某一步没确认送达 / 实时流没正常结束。

        为什么非要分开：这一页最不能有的谎是让现场以为"动过了"。把"已经发了但没走完"
        说成"没发"（或反过来）都会让人对着机器的实际位置做出错误判断。
        """
        lines: List[str] = []
        self._lines = lines
        if not self.steps:
            return {"ok": False, "lines": ["没有要执行的动作"], "aborted": False,
                    "stopped_at": None, "stop_reason": "empty"}
        total = len(self.steps)
        for index, step in enumerate(self.steps, start=1):
            if self._abort:
                self._note(f"已在第 {index}/{total} 步之前中止（这一步没有下发）")
                return {"ok": False, "lines": lines, "aborted": True,
                        "stopped_at": index, "stop_reason": "before"}
            self._note(f"[{index}/{total}] {step['label']}")
            if not step.get("entries") and not step.get("streams"):
                self._note("    这一步没有任何可下发的条目，跳过")
                continue
            if step.get("trunk"):
                # 非实时模式：两条目标之间必须隔够，否则会在半途接上新目标（文档要求）
                self._wait_trunk_gap()
            if step.get("entries") and not self._send(step):
                return {"ok": False, "lines": lines, "aborted": False,
                        "stopped_at": index, "stop_reason": "failed"}
            if step.get("streams") and not self._stream(step):
                aborted = bool(self._abort)
                return {"ok": False, "lines": lines, "aborted": aborted,
                        "stopped_at": index,
                        "stop_reason": "during" if aborted else "failed"}
            if step.get("streams"):
                self._await_step(step)          # 实时流自带到位判据，这里只留收敛时间
        self._note(f"全部 {total} 步已执行完毕")
        return {"ok": True, "lines": lines, "aborted": False, "stopped_at": None,
                "stop_reason": ""}

    def _note(self, line: str) -> None:
        """记一行结果，并**顺手推给界面**。

        8 步的预设（LB）跑起来要十几秒，中途一声不吭会被读成"卡住了"；
        而进度跨线程只能走信号 —— 这里只调用注入的回调，Qt 那一层由界面负责。
        """
        self._lines.append(line)
        if self.progress is not None:
            try:
                self.progress(line)
            except Exception as exc:                # noqa: BLE001 —— 界面的问题不该打断动作
                logger.warning(f"进度回调失败: {exc}")

    # ---------- 内部：下发一步 ----------
    def _send(self, step: dict) -> bool:
        """发这一步；返回是否**确认成功**。

        两条路（v6.2.0）：

        * **闭环**（常驻通道可用）：运动目标交给 `launcher.move_trunk / move_arm` ——
          它等订阅者、重发、**回读关节角确认到位**（带残差与重试）。确认到位就**不再固定
          睡眠**，所以多步预设的节奏变成"到位就走下一步"（这才是\"效果\"那件事）；
        * **兜底**（通道起不来）：一次性和以前一样发出去，然后按固定时长等，
          并如实标注\"这一步未经确认\"。**没通道不等于不能用**，这是刻意保留的降级。

        区分两者的判据是 `move_*` 返回的 `attempts == 0` —— 通道没跑起来时它一条都没发。
        """
        targets = step.get("target") or {}
        motion = []                                     # (kind, side, values)
        covered = set()                                 # 已被闭环路径覆盖的条目名
        if step.get("trunk") and targets.get("trunk"):
            motion.append(("trunk", None, targets["trunk"]))
            covered.add("躯干目标")
            # **「躯干限速」也必须进这里**（v6.3.5）。不写这一行的话它会掉进 `rest`
            # → 走一次性 CLI 兜底（新进程 + 重做一遍 DDS 发现，几秒起步）、
            # 而且是**在目标之后**才发 —— 速率本该在目标之前生效，顺序就反了。
            # 闭环那条路里它由 `Bridge._send_target` 先发；降级那条路用的是
            # `step["entries"]` 全量，本来就带着它。
            covered.add("躯干限速")
        for side in step.get("arms") or ():
            values = targets.get(f"{side}_arm")
            if values:
                motion.append(("arm", side, values))
                covered.add(f"{ARM_NAMES[side]}运动")
        rest = [entry for entry in step["entries"] if entry[0] not in covered]

        if not motion:
            return self._publish_entries(rest, step)

        moved = self._move(motion)
        if moved is None:                               # 通道没跑起来 → 整步走兜底
            # **兜底路径也要退 follow / servoJ**：`*_arm_move` 只在"非 follow position
            # 模式"生效，而 follow / servoJ 开着时控制节点是**静默忽略**（没有报错）。
            # 通道那条路由脚本的 `ensure_arm_point_mode` 做；一次性 CLI 这条路原来没做 ——
            # 现场 2026-09-29 恰恰是用它跑的（通道崩了），右臂的 ArmMove 就这样被吃掉。
            self._release_follow([side for kind, side, _values in motion if kind == "arm"])
            if not self._publish_entries(step["entries"], step):
                return False
            self._await_step(step)
            return True
        if not moved:
            return False
        if not self._publish_entries(rest, step):
            return False
        if any("手型" in entry[0] for entry in rest):
            self._await_hand(step)
        return True

    def _move(self, motion) -> Optional[bool]:
        """闭环走这几个目标；``None`` = 通道不可用（调用方据此降级）。

        两处刻意的安排：

        * **手臂走批量**（`move_arms`）：左右臂本来是同一时刻的目标，一次都发出去、
          再一起等到位 —— 逐条发会让镜像动作变成"先左后右"（官方手柄示例也是先发两条
          再一起等 `arm_move_over_`）；
        * **躯干仍是一次一条**：协议要求两条躯干目标之间 ≥1.5s，批量发会违反它。
        """
        for kind, _side, values in [item for item in motion if item[0] == "trunk"]:
            moved = self._move_one("trunk", None, values)
            if moved is not True:
                return moved                            # False = 失败；None = 通道不可用
        arms = {side: values for kind, side, values in motion if kind == "arm"}
        if not arms:
            return True
        for side, values in arms.items():
            self._note(f"    {ARM_NAMES[side]} → "
                       + "、".join(fmt_number(item) for item in values) + "（度）")
        move_arms = getattr(self.launcher, "move_arms", None)
        if move_arms is None:                           # 老实现/老替身 → 逐侧退回单条
            for side, values in arms.items():
                moved = self._move_one("arm", side, values)
                if moved is not True:
                    return moved
            return True
        result = move_arms(dict(arms))
        if result is None:                              # 老替身：当作没有闭环能力
            return None
        if result.get("attempts", 0) == 0:
            self._note(f"    常驻通道不可用（{result.get('reason') or '未知原因'}）"
                       f"—— 这一步改用一次性下发 + 固定等待，是否到位未经确认")
            return None
        return self._report_batch(result, list(arms))

    def _move_one(self, kind: str, side, values) -> Optional[bool]:
        """单条闭环（躯干用它；手臂在"没有批量接口"时也走它）。"""
        move = getattr(self.launcher, "move_trunk" if kind == "trunk" else "move_arm", None)
        if move is None:
            return None
        label = "躯干" if kind == "trunk" else ARM_NAMES[side]
        self._note(f"    {label} → "
                   + "、".join(fmt_number(item) for item in values) + "（度）")
        result = move(values) if kind == "trunk" else move(side, values)
        if result is None:
            return None
        if result.get("attempts", 0) == 0:
            self._note(f"    常驻通道不可用（{result.get('reason') or '未知原因'}）"
                       f"—— 这一步改用一次性下发 + 固定等待，是否到位未经确认")
            return None
        return self._report_single(label, result, kind)

    def _report_single(self, label: str, result: dict, kind: str) -> bool:
        """把一条闭环结果翻译成人话（成功/未确认/没到位）。"""
        if result.get("ok") is True:
            residual = result.get("residual") or []
            peak = max((abs(item) for item in residual), default=0.0)
            self._note(f"    {label}已到位（第 {result.get('attempts', 1)} 次下发，"
                       f"残差 {peak:.2f}°）")
            self._after_trunk(kind)
            return True
        if result.get("ok") is None:
            self._note(f"    未经确认：{result.get('reason') or '读不到当前角度'}")
            return False
        self._note(f"    没走到位：{result.get('reason') or '超时'}")
        return False

    def _report_batch(self, result: dict, sides) -> bool:
        """把批量结果**逐侧**报出来（哪一侧到了、哪一侧没到，一眼能看到）。"""
        items = result.get("items") or {}
        ok = True
        for side in sides:
            entry = items.get(f"arm:{side}") or {}
            label = ARM_NAMES[side]
            peak = max((abs(item) for item in (entry.get("residual") or [])), default=0.0)
            if entry.get("ok") is True:
                self._note(f"    {label}已到位（第 {entry.get('attempts', 1)} 次下发，"
                           f"残差 {peak:.2f}°）")
            elif entry.get("ok") is None:
                self._note(f"    {label}未经确认：读不到当前角度")
                ok = False
            else:
                self._note(f"    {label}没走到位：{entry.get('reason') or '超时'}")
                ok = False
        return ok

    def _release_follow(self, sides) -> None:
        """走点位目标之前，把这几侧的 follow / servoJ 关掉（**幂等**，关着的再发一次 false 无害）。

        依据（SDK 原文）：`*_arm_move` 只在"非 follow position 模式"生效。而遥操作的上电时序
        会把两侧置为 `follow_position_set=true`，**停止遥操作不会复位它** ——
        此后 `ArmMove` 被控制节点静默丢弃：没有报错、没有反馈，现场只看到"点了没反应"。

        结果**不阻塞动作**：退出失败只是"没退出"，紧接着的运动判据（`_await_arms` 的
        `moving`、或通道的回读）会把这个事实暴露出来 —— 在这里拦住反而会掩盖真因。
        """
        sides = [side for side in dict.fromkeys(sides) if side in ARM_NAMES]
        if not sides:
            return
        entries = []
        for side in sides:
            entries.append((f"{ARM_NAMES[side]}退出跟随", ARM_FOLLOW_SET_TOPICS[side],
                            BOOL_MSG, "data: false"))
            entries.append((f"{ARM_NAMES[side]}退出servoJ", ARM_SERVOJ_SET_TOPICS[side],
                            BOOL_MSG, "data: false"))
        self._note("    先退出跟随 / servoJ（否则 `ArmMove` 会被控制节点忽略）")
        try:
            self.launcher.publish_motion(entries, self.publish_timeout, "退出跟随模式")
        except Exception as exc:                    # noqa: BLE001 —— 外部进程什么都可能
            self._note(f"    退出跟随的指令没发出去（{exc}）—— 先让手臂停在原状态")

    def _after_trunk(self, kind: str) -> None:
        """闭环确认过的躯干目标也要记账：下一条躯干目标仍须隔够 1.5s（文档要求）。"""
        if kind == "trunk":
            self._last_trunk_at = self.clock()

    def _publish_entries(self, entries, step: dict) -> bool:
        """发一批一次性条目；返回是否**全部确认送达**。

        结果形状由 `launcher.publish_motion` 统一给出：``{名字: {ok, reason, channel}}``，
        `ok` 取 `None` = 未确认（既不能当成功也不能当失败）。**兼容旧的退出码写法**
        （`int` / `None`）—— 测试替身与外部实现还在用，而且它是"同一套语义"的另一种表述。
        """
        entries = list(entries)
        if not entries:
            return True
        codes = self.launcher.publish_motion(entries, self.publish_timeout, step["label"])
        if codes is None:
            self._note("    未取到结果（下发进程没回话）—— 这一步是否到达无法确认")
            return False
        if isinstance(codes, dict) and not codes and entries and \
                not self.launcher.config.check_workspace():
            self._note("    工作空间不存在，未下发任何指令")
            return False
        missed = []
        for name, _topic, _msg, _payload in entries:
            ok, reason, channel = self._read_result(codes, name)
            if ok is True:
                continue
            if ok is None:
                missed.append(f"{name}（未确认：{reason or '没取到结果'}）")
            else:
                missed.append(f"{name}（{reason or '下发失败'}）")
        if missed:
            self._note("    未确认送达：" + "、".join(missed))
            return False
        if step.get("trunk"):
            # 记下"上一条躯干目标的时刻"，下一条要走 `_wait_trunk_gap`
            self._last_trunk_at = self.clock()
        return True

    @staticmethod
    def _read_result(codes, name: str):
        """把一条结果读成 ``(ok, reason, channel)``；兼容退出码写法。"""
        value = codes.get(name) if isinstance(codes, dict) else None
        if isinstance(value, dict):
            return value.get("ok"), str(value.get("reason") or ""), value.get("channel", "")
        if value is None:
            return None, "没有回取到结果（下发进程没回话）", ""
        if value == 0:
            return True, "", ""
        if value == 124:
            return False, "等待超时：没等到订阅者（主程序没在跑？）", ""
        return False, f"退出码 {value}", ""

    # ---------- 内部：下发一步（旧入口，仍被测试与实时流路径使用） ----------
    def _publish(self, step: dict) -> bool:
        """发一步的全部条目（回头再等）—— `_send` 的内部实现之一。"""
        return self._publish_entries(step["entries"], step)

    def _wait_trunk_gap(self) -> None:
        """两条躯干目标之间至少 `TRUNK_TARGET_GAP_SEC`（文档要求）。

        不够就等，并把**原因**写进日志 —— 现场看到"卡了 1.2 秒"必须能一眼看出
        这是协议要求，不是界面卡死。
        """
        if self._last_trunk_at is None:
            return
        elapsed = self.clock() - self._last_trunk_at
        if elapsed >= self.trunk_gap:
            return
        wait = self.trunk_gap - elapsed
        self._note(f"    等待 {wait:.1f}s（文档要求躯干两条目标间隔 ≥"
                   f"{self.trunk_gap:g}s，否则会在半途接上新目标）")
        self._sleep(wait)

    # ---------- 内部：跑一步的实时流 ----------
    def _stream(self, step: dict) -> bool:
        """跑这一步的实时流任务；返回是否**每个任务都正常结束**。"""
        jobs = list(step["streams"])
        self._note("    实时流：" + "；".join(stream_job_summary(job) for job in jobs))
        runner = self.stream_runner
        if runner is None:
            runner = getattr(self.launcher, "run_motion_streams", None)
        if runner is None:
            self._note("    没有可用的实时流执行体（launcher 未实现 run_motion_streams）"
                       "—— 这一步没有执行")
            return False
        try:
            results = runner(jobs, self._note, lambda: self._abort)
        except Exception as exc:                    # noqa: BLE001 —— 外部进程什么都可能
            self._note(f"    实时流执行失败：{exc}")
            return False
        if self._abort:
            self._note("    已在实时流执行中收到中止 —— 后面的步骤不会下发")
            return False
        if not isinstance(results, dict):
            self._note("    未取到结果（实时流没回话）—— 这一步是否到位无法确认")
            return False
        bad = [key for key, value in results.items() if value is None]
        if bad:
            self._note("    未确认：" + "、".join(bad)
                       + "（脚本没回退出码，无法判断是否走到位）")
            return False
        failed = [key for key, value in results.items() if not value]
        if failed:
            self._note("    这些实时流没有正常结束：" + "、".join(failed))
            return False
        return True

    # ---------- 内部：等这一步完成 ----------
    def _await_step(self, step: dict) -> None:
        if step.get("streams"):
            # 实时流本身就是"等它走完"（脚本走到位才退），这里只留一点收敛时间
            if self.trunk_settle > 0:
                self._sleep(self.trunk_settle)
            return
        if not step.get("entries"):
            return
        if step["trunk"]:
            wait = max(self.trunk_settle, (step["wait_ms"] or 0) / 1000.0)
            self._note(f"    等躯干插值完成（{wait:.1f}s，非实时模式由节点内部插值）")
            self._sleep(wait)
            return
        if step["arms"]:
            self._await_arms(step)
            if step["hand"]:
                # 手型与手臂在同一个步骤里：手型单独等一段（协议没有回读）
                extra = max(0.0, (step["wait_ms"] or HAND_WAIT_MS) / 1000.0 - ARM_FIXED_WAIT_SEC)
                if extra > 0:
                    self._sleep(extra)
            return
        self._await_hand(step)

    def _await_hand(self, step: dict) -> None:
        """等手型生效：**协议没有回读话题**，只能按时长等（`wait_ms`，缺省 500ms）。

        闭环路径（`_send`）也走它 —— 手臂能回读确认，手型不能，两件事的判据不同，
        但"等多久"只能有一个说法。
        """
        wait_ms = step["wait_ms"] if step["wait_ms"] is not None else HAND_WAIT_MS
        self._note(f"    等 {wait_ms} ms（手型没有回读话题，只能按时长等）")
        self._sleep(wait_ms / 1000.0)

    def _await_arms(self, step: dict) -> None:
        """等手臂 `*_arm_moving` 归 false；取不到标志就固定等待并**标注未确认**。"""
        sides = step["arms"]
        names = "、".join(ARM_NAMES[side] for side in sides)
        start = self.clock()
        started = {side: False for side in sides}
        while self.clock() - start < self.arm_timeout:
            if self._abort:
                return
            flags = self._arm_moving_flags(sides)
            if flags is None:
                self._note(f"    读不到 arm_state（监控没跑或状态文件不可用）→ "
                           f"按固定 {ARM_FIXED_WAIT_SEC:g}s 等，该步未经确认")
                self._sleep(ARM_FIXED_WAIT_SEC)
                return
            for side in sides:
                if flags.get(side):
                    started[side] = True
            if any(started.values()) and not any(flags.get(side) for side in sides):
                self._note(f"    等 {names} 运动到位：已确认 moving=false"
                           f"（用时 {self.clock() - start:.1f}s）")
                return
            if not any(started.values()) and self.clock() - start > ARM_MOVE_SETTLE_SEC:
                self._note(f"    {ARM_MOVE_SETTLE_SEC:g}s 内没看到 {names} 的 moving=true"
                           f"（可能已经到位，也可能没收到指令）→ 按固定 "
                           f"{ARM_FIXED_WAIT_SEC:g}s 继续，该步未经确认")
                self._sleep(ARM_FIXED_WAIT_SEC)
                return
            self._sleep(self.poll)
        self._note(f"    等 {names} 运动到位超时（{self.arm_timeout:g}s）→ 继续下一步，"
                   f"该步未经确认")

    def _arm_moving_flags(self, sides: Sequence[str]) -> Optional[Dict[str, bool]]:
        """从状态视图取指挥过的这几侧的 `moving`；取不到返回 None（= 不能用它当判据）。"""
        if self.status_reader is None:
            return None
        try:
            view = self.status_reader() or {}
        except Exception as exc:                    # noqa: BLE001 —— 判据取不到不该炸执行器
            logger.warning(f"读状态失败（姿势控制等手臂完成）: {exc}")
            return None
        entry = ((view.get("module_state") or {}).get("arm_state") or {})
        if not isinstance(entry, dict) or not entry.get("has_data"):
            return None
        flags: Dict[str, bool] = {}
        for side in sides:
            detail = entry.get(side)
            if not isinstance(detail, dict) or "moving" not in detail:
                return None
            flags[side] = bool(detail.get("moving"))
        return flags

    def _sleep(self, seconds: float) -> None:
        """分片睡：这样 `abort()` 最多等一个分片就被看到（一整段 sleep 里喊停是无效的）。"""
        remaining = float(seconds)
        while remaining > 0:
            if self._abort:
                return
            slice_ = min(0.2, remaining)
            self.sleeper(slice_)
            remaining -= slice_
