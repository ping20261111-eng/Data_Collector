"""急停下发 + 机器人状态监控（MainWindow 的一个面板）。

方法自原 MainWindow 原样搬迁，逻辑未改 —— 拆分只为把 3455 行的单文件
拆回可读规模。改动这个面板时只需要打开这一个文件。
"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
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
from ... import theme
from ...core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ...core_motion import TRUNK_LOCK_AXES_TEXT
from ...core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ...core_data import RecorderManager, DataUploader, LogFetcher
from ...core_data import UploadCancelledError

from ..safety import guard_slot
from ..common import (LOW_BATTERY_PERCENT, CHARGING_MARKERS, _as_int, module_state_texts,
                       lost_source_fields, FIELD_NAMES)
from ..gripper import GripperDialog
from ..end_effector import EndEffectorDialog
from ..posture import PostureDialog
from ..threads import (ProcessExitThread, LocalTaskThread)




class StatusMixin:
    """急停下发 + 机器人状态监控。"""

    @guard_slot
    def on_emergency_stop(self):
        """下发急停：底盘 + 躯干。

        刻意不做二次确认：急停按钮在任何情况下都该"按下去就生效"，
        被一个确认框拖慢反而危险。
        """
        try:
            proc = self.launcher.launch_emergency_stop(True)
        except Exception as e:
            self.show_error("急停失败", f"无法下发急停指令：{e}")
            return
        self.append(f"已下发急停指令（{EMERGENCY_STOP_PART_NAMES}），等待退出码与状态回显")
        self.emergency_label.setText("急停 指令已发送…")
        theme.set_state(self.emergency_label, "warn")
        self._watch_estop_process(proc, engage=True)

    @guard_slot
    def on_release_emergency_stop(self):
        """解除急停：会让机器人恢复可动，必须二次确认。"""
        if QMessageBox.question(
            self, "解除急停",
            "解除急停后机器人将恢复可动，请先确认周围安全。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            proc = self.launcher.launch_emergency_stop(False)
        except Exception as e:
            self.show_error("解除急停失败", f"无法下发指令：{e}")
            return
        self.append(f"已下发解除急停指令（{EMERGENCY_STOP_PART_NAMES}），等待退出码")
        self._watch_estop_process(proc, engage=False)

    def _watch_estop_process(self, proc, engage: bool):
        """等急停命令结束，据退出码如实报告结果。

        躯干侧没有状态回读，退出码是判断"发出去了没有"的唯一依据，
        所以这里不做"指令已发送"的乐观汇报。
        """
        if proc is None:                 # 便于测试替换 launcher 时不炸
            return
        thread = ProcessExitThread(proc, parent=self)
        thread.exited.connect(lambda code: self._on_estop_finished(code, engage))
        thread.start()
        self.estop_thread = thread       # 持引用，避免线程对象被回收

    @guard_slot
    def _on_estop_finished(self, code: int, engage: bool):
        action = "急停" if engage else "解除急停"
        if code == 0:
            self.append(f"{action}指令已下发到：{EMERGENCY_STOP_PART_NAMES}")
            if engage:
                # 每次急停都提一次：这条覆盖缺口不能靠"记得"
                self.append("提醒：手臂没有软件急停接口，请人工确认机械臂状态")
            return
        # 不静默失败：这是安全链路，退出码非 0 说明至少一个部件没发出去
        self.append(
            f"警告：{action}指令下发失败（退出码 {code}）—— "
            f"请检查底盘/躯干节点是否在线，必要时改用硬件急停"
        )
        self.show_error(
            f"{action}指令下发失败",
            f"退出码 {code}。\n\n"
            f"请确认底盘与躯干节点已启动；若无法确认，"
            f"请改用硬件急停按钮。",
        )

    @guard_slot
    def on_toggle_robot_monitor(self):
        """启动/停止机器人状态监控（订阅电量/急停/定位/地图/充电并落盘）。"""
        if self._robot_monitor_running():
            if self.launcher.stop_robot_monitor():
                self.append("机器人状态监控已停止")
            self.robot_monitor_proc = None
        else:
            self.start_robot_monitor()
        self.refresh_robot_status()

    def start_robot_monitor(self, *, auto: bool = False) -> bool:
        """启动状态监控（**幂等**：已经有一份在跑就复用，不重复拉起）。

        抽出来是因为它现在有两个调用方：手动按钮，以及「启动程序」成功后的自动拉起
        —— 电量不是底盘一连上就有的，它由本监控订阅 /power_report 落盘，界面只读那份
        JSON；监控没起，顶栏电量会一直显示"未知"。

        为什么要防重复：两个监控进程会同时原子写同一份 robot_monitor.json，
        界面读到的就是互相覆盖的碎片（_robot_monitor_running 只认本会话记下的那个
        句柄，"停止"若来自别处就会漏判，所以再问一次进程管理器）。

        Args:
            auto: True 表示"随主程序自动拉起"，日志措辞会点明来源，
                便于现场区分"我按的"和"它自己起的"。

        Returns:
            True = 本次真的拉起了新进程；False = 已有一份在跑（复用）或启动失败。
        """
        if self._robot_monitor_running():
            return False
        existing = self.launcher.pm.find_by_name("robot_monitor")
        if existing is not None:
            # 接管进本会话，按钮文字/状态才跟得上（否则会显示"启动状态监控"却已有一份在跑）
            self.robot_monitor_proc = existing
            return False

        log_file = self.log_fetcher.local_log_dir / "robot_monitor.log"
        try:
            self.robot_monitor_proc = self.launcher.launch_robot_monitor(log_file)
        except Exception as e:
            if auto:
                # 自动拉起失败不弹模态框：现场刚点完「启动程序」，弹框会盖在启动结果上；
                # 这条信息写在日志区里一样看得见，也更靠近它发生的位置。
                self.append(f"状态监控未能自动启动：{e}（可稍后手工点「启动状态监控」）")
            else:
                self.show_error("启动监控失败", str(e))
            return False
        prefix = "随主程序自动" if auto else ""
        self.append(f"机器人状态监控已{prefix}启动（订阅电量/急停/充电，日志: {log_file}）")
        return True

    @guard_slot
    def on_gripper_control(self):
        """打开「夹爪控制」独立页面（宇树 Dex11 的左/右张开与闭合抓取）。

        与「夹爪控制」同构，也是**单例**：两个页面各自持有探测结果与"在飞"的动作，
        会让同一侧夹爪被下发两次，而现场看着两个窗口无从判断哪次有效。
        """
        dialog = getattr(self, "_gripper_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            dialog.refresh()
            return
        dialog = GripperDialog(self, self.config, self.launcher)
        dialog.busy_changed.connect(self._on_gripper_busy_changed)
        self._gripper_dialog = dialog      # 必须留引用：局部变量出栈后窗口会被回收
        dialog.show()
        self.append("已打开「夹爪控制」页面（宇树夹爪张开 / 闭合抓取）")

    def _on_gripper_busy_changed(self, busy: bool):
        """夹爪动作进行中：置标志，给「关闭程序」当闸门用（见 launch.py）。"""
        self.gripper_action_in_flight = bool(busy)

    @guard_slot
    def on_end_effector(self):
        """打开「末端参数」独立页面（改 arm_control_config.yaml 里的张开 / 握紧指令）。

        同样是**单例**：两个页面各持一份"未保存的改动"，谁最后点保存、写进去的是哪一份
        就说不清了 —— 而写的是机器人侧配置文件，这种含糊代价太大。
        """
        dialog = getattr(self, "_end_effector_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            dialog.refresh()
            return
        dialog = EndEffectorDialog(self, self.config, self.launcher)
        dialog.busy_changed.connect(self._on_end_effector_busy_changed)
        self._end_effector_dialog = dialog     # 必须留引用：局部变量出栈后窗口会被回收
        dialog.show()
        self.append("已打开「末端参数」页面（改张开/握紧指令，保存后需重启主程序生效）")

    def _on_end_effector_busy_changed(self, busy: bool):
        """写配置进行中：置标志，给「关闭程序」当闸门用（见 launch.py）。"""
        self.end_effector_action_in_flight = bool(busy)

    @guard_slot
    def on_posture(self):
        """打开「姿势控制」独立页面（躯干 / 双臂各轴角度、姿势预设）。

        同样是**单例**：这一页会连续下发多步动作（LB 是 8 步），两个页面各跑一套的话，
        两条轨迹会在机器人上叠加 —— 现场看着两个窗口，根本判断不出机器在跟哪一条走。
        """
        dialog = getattr(self, "_posture_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            dialog.refresh()
            return
        dialog = PostureDialog(self, self.config, self.launcher)
        dialog.busy_changed.connect(self._on_posture_busy_changed)
        self._posture_dialog = dialog      # 必须留引用：局部变量出栈后窗口会被回收
        dialog.show()
        self.append("已打开「姿势控制」页面（躯干 / 双臂各轴角度 + 姿势预设）")

    def _on_posture_busy_changed(self, busy: bool):
        """姿势动作进行中：置标志，给「关闭程序」当闸门用（见 launch.py）。"""
        self.posture_action_in_flight = bool(busy)

    # ---------- 躯干自锁（两个入口共用这一处，v6.4.15） ----------
    def _sync_robot_menu(self):
        """「机器人控制 ▾」弹出前刷新那几项的名字。

        自锁那一项**文案恒为「躯干自锁」**（v6.5.7）：它只有这一个方向。
        自锁状态读不回来（`getMotorBrakeState()` 只在 C++ SDK 里），而且它的生命周期
        属于**遥操作进程** —— 现场重启遥操作即重置。上位机按自己的记录把文案翻成
        「解除自锁」，就会在真实未锁时发出解除指令（`connect true`，躯干抬头）。
        全屏那一项表示的是**窗口当前状态**（可回读，用 `isFullScreen()`）。
        本菜单的约定是**一律可点、不置灰**，所以"点下去会做什么"只能靠文案说清。
        """
        self.btn_trunk_lock.setText("躯干自锁")
        self.btn_fullscreen.setText("退出全屏" if self.isFullScreen() else "全屏显示")

    @guard_slot
    def on_trunk_lock(self, *, report=None):
        # `report` 是 **keyword-only**（v6.5.7）。不是洁癖：`QAction.triggered` 会带一个
        # `checked=False` 位置参数，若这里收位置参数，那个 False 会被当成 `report`
        # 静默吃掉（`report or self.append` 恰好把它兜回 `self.append`，看起来无害）——
        # 但语义已经错了，而且以后谁把 `report` 改成必传就会突然崩。
        # 收成 keyword-only 之后，直接 `triggered.connect(self.on_trunk_lock)` 会
        # TypeError，问题当场暴露。

        """躯干**自锁**（仅上电）—— 两个入口共用这一处，**只有一个方向**。

        入口：
          · 工具行「机器人控制 ▾」→「躯干自锁」；
          · 姿势控制页里躯干那一行的「自锁」按钮（它把 `report` 换成自己那页的结果区）。

        ## 这个按键只有"下发自锁指令"这一件事（v6.6.1）

        现场 2026-10-20 明确：「取消解除自锁功能，该按键只有下发自锁指令的功能」。
        所以**解除那个方向从底层就删了**（`Launcher.lock_trunk()` 没有方向参数，
        通道也不再接受 `what="trunk_unlock"`）—— 不是只从界面藏起来。

        自锁状态**读不回来**（`getMotorBrakeState()` 只存在于 C++ SDK），而且它的生命周期
        属于**遥操作进程**（现场：重启遥操作即重置）。按"我下发过什么"推断"现在是不是自锁"
        必然出错：遥操作重启后自锁已重置，上位机却还记得。而自锁指令本身**幂等** ——
        重复发只是让那几个电机再退出一次使能。所以正确做法就是"永远只发自锁"，
        恢复交给重启遥操作。

        ## 为什么按下第二次一定会再发一条

        现场反馈「发一两次可能没触发」，而**再按一次是唯一的补救手段**（没有解除可点）。
        所以这里的判据是：**每一次按下都要对应一次真实的下发** ——

        · 上一条已经回来 → 直接再发（`_start_trunk_lock`）；
        · 上一条还在路上 → 记下 `_trunk_lock_pending`，它一回来**立刻补发一次**
          （不会丢、也不会并发抢通道：通道本身是串行的，并发只会让两条互相等超时）。

        线程引用收在列表里，**不在原位覆盖**：`LocalTaskThread` 刻意不挂 parent，
        如果在它还跑着的时候把 `self._trunk_lock_thread` 换成新对象，旧对象会被 Python
        回收 —— 那是「QThread: Destroyed while thread is still running」，C 层直接 abort。
        现场表现就是"点快了两下，程序没了"。这也是 v6.6.1 之前"发一两次没反应"的
        一个真实成因（不是机器人侧）。

        自锁会改变机构状态（关节失去保持力矩），所以仍然二次确认 —— 确认逻辑只写在这一处。
        """
        report = report or self.append
        title = "确认躯干自锁"
        ask = (f"将把躯干 {TRUNK_LOCK_AXES_TEXT} 转为「仅上电」（电机退出使能）：\n"
               "· 之后仍然能读角度；\n"
               "· 但不接受运动指令（自锁生效期间，下发的目标不会被执行）；\n"
               "· 自锁等于「关节失去保持力矩」—— 若躯干当前不在支撑位，可能自行下沉；\n"
               "· 重复点没有副作用（每次都会真的再发一条）；\n"
               "· 恢复方式：「重启遥操作」（自锁状态跟着遥操作进程走）。\n\n"
               "是否继续？")
        if QMessageBox.question(self, title, ask,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            report("已取消躯干自锁")
            return
        self._start_trunk_lock(report)

    # ---------- 躯干自锁的下发（v6.6.1） ----------
    def _trunk_lock_busy(self) -> bool:
        """有没有一条自锁指令还在路上。"""
        return any(thread.isRunning() for thread in self._trunk_lock_threads)

    def _prune_trunk_lock_threads(self) -> None:
        """摘掉**已经结束**的线程引用（在"下一次下发"时做）。

        ⚠️ **不能在 `finished` 槽里摘**：`finished` 是在 `run()` 里发出去的，
        那一刻 `run()` 可能还没返回、`isRunning()` 仍是 True —— 丢掉最后一个引用
        （`LocalTaskThread` 不挂 parent）就等于销毁一个"仍在运行"的 QThread，
        Qt 会直接 abort。放在下一次下发前摘，那时它早已退出，安全。
        """
        self._trunk_lock_threads = [thread for thread in self._trunk_lock_threads
                                    if thread.isRunning()]

    def _start_trunk_lock(self, report, *, resend: bool = False):
        """真正下发那一条自锁指令（`resend=True` = 补发，不再二次确认）。"""
        self._prune_trunk_lock_threads()
        if self._trunk_lock_busy():
            # 已经在路上：**记账，别丢掉这一次按下**。通道是串行的，并发只会让
            # 两条互相等超时（20s 窗口），现场看到的就是"两条都没反应"。
            self._trunk_lock_pending = True
            report("上一次自锁指令还在下发中 —— 已记下，它一回来立刻再发一次")
            return
        # 线程引用必须**自己留着**：`LocalTaskThread` 刻意不接收 parent（挂到窗口上当
        # 子对象，会让 Qt 在销毁窗口时连带销毁还在收尾的线程 —— 那就是 C 层段错误）。
        # 收在列表里而不是单个属性：连点两次时**不能覆盖**还活着的那个线程对象。
        # 结束的引用由 `_prune_trunk_lock_threads()` 在**下一次下发前**摘（不在
        # finished 槽里摘 —— 那时 run() 可能还没返回）。
        thread = LocalTaskThread(lambda: self.launcher.lock_trunk())
        self._trunk_lock_threads.append(thread)
        thread.finished.connect(
            lambda ok, payload, error, sink=report:
                self._on_trunk_lock_done(ok, payload, error, sink))
        report("躯干自锁：正在重发…" if resend else "躯干自锁：正在下发…")
        thread.start()

    @guard_slot
    def _on_trunk_lock_done(self, ok: bool, payload, error: str, report):
        """下发回报。**只报"发出去了"，不报"生效了"** —— 自锁状态读不回来。"""
        if ok and isinstance(payload, dict) and payload.get("ok"):
            steps = payload.get("steps") or []
            for step in steps:
                report(f"    {step.get('name')}：已发 {step.get('sent', 0)} 次"
                       f"（订阅者 {step.get('matched', -1)}）")
            # 走了哪条通道**要说出来**：常驻通道起不来时会自动改用一次性 CLI
            # （自锁那条指令本来就是一次性的，两条路都合法）。现场看到"订阅者 -1"
            # 会以为没发出去，其实那只是"一次性 CLI 无法统计订阅者"。
            channels = {step.get("channel") for step in steps if step.get("channel")}
            if "一次性 CLI" in channels:
                report("    （本次走的是一次性 CLI：常驻通道未起，用的是与现场"
                       "手敲等价的那条 `ros2 topic pub --once`）")
            # 不记任何"自锁状态"：它跟着遥操作进程走，上位机记不住真相。
            report("✅ 躯干自锁指令已下发\n"
                   "    注意：自锁状态无法回读（ROS 侧没有这个话题），以机构实际表现为准；\n"
                   "    若机构没有反应，可再点一次（每次都真的再发一条）；\n"
                   "    恢复方式：重启遥操作（状态跟着遥操作进程走）。")
        else:
            reason = payload.get("reason") if isinstance(payload, dict) else ""
            # 失败时也要把**恢复方式**说出来：现场可能已经下发过一次自锁，
            # 这一条没成功时最想知道的是「现在怎么恢复」。
            report(f"⚠ 躯干自锁未完成：{reason or error or '通道没有回话'}\n"
                   "    可以再点一次重发（每次都真的再发一条）；\n"
                   "    恢复方式：重启遥操作（自锁状态跟着遥操作进程走）")
        # 姿势控制页开着的话，它那一行按钮的文案要跟着刷新（同一个真源）
        dialog = getattr(self, "_posture_dialog", None)
        if dialog is not None:
            try:
                dialog._apply_module_buttons()
            except Exception as exc:                       # noqa: BLE001
                logger.debug(f"刷新姿势页的自锁按钮失败（不影响下发结果）：{exc}")
        # 上一次按下的那一条**没有丢**：这里补发一次（只补一次，不攒队列 ——
        # 自锁幂等，攒 N 条也只是重复 N 次同样的动作）。
        if self._trunk_lock_pending:
            self._trunk_lock_pending = False
            self._start_trunk_lock(report, resend=True)

    def _robot_monitor_running(self) -> bool:
        return (self.robot_monitor_proc is not None
                and self.robot_monitor_proc.poll() is None)

    @guard_slot
    def refresh_robot_status(self):
        """周期刷新：主程序运行状态 + 机器人状态 + 监控自身状态。"""
        # 顺手回收已退出的子进程：ProcessManager 是单例且只增不删，
        # 不主动清理的话僵尸进程会随会话累积（长班次采集尤其明显）。
        self.launcher.pm.prune()
        pid = self.sdk_service.launch_pid()
        self.sdk_service_running = pid is not None
        if pid is not None:
            # 只读显示：不再提供"启动服务"。启动是机器人侧的事，从上位机去
            # systemctl start 在多数部署上既没权限也没有同名 unit，点了只会失败。
            self.sdk_service_label.setText(f"SDK 服务 运行中（PID {pid}）")
            theme.set_state(self.sdk_service_label, "ok")
        else:
            self.sdk_service_label.setText("SDK 服务 未运行")
            theme.set_state(self.sdk_service_label, "none")

        monitor_running = self._robot_monitor_running()
        # "监控"这个词在界面里有四处（状态监控 / 性能监控 / 数据验证），
        # 按钮文案带上限定词，免得与日志区的"性能监控"混为一谈
        self.btn_robot_monitor.setText("停止状态监控" if monitor_running else "启动状态监控")
        self._update_robot_status(read_robot_status(self.config), monitor_running)

    def _update_robot_status(self, view: dict, monitor_running: bool):
        """把状态视图映射到界面。

        显示面只有三处：
        - 急停 -> 左侧急停柱（安全信息，最醒目）
        - 电量 / 充电 -> 顶栏电池图标（充电时叠闪电）
        - 其余（数据新鲜度、为什么没数据）-> 顶栏第二行的状态文字

        定位与地图刻意不显示：调试平台已经覆盖，上位机再放一份只会互相打架。

        两条必须守住的原则：
        1. **过期状态显式标注**：拿一份 30 秒前的"未急停"当现在的状态是危险的；
        2. **过期数据不报警**：用旧读数触发低电量提醒等于制造一条假警报。
        """
        status = view.get("status")
        if not isinstance(status, dict):
            # 状态文件是外部脚本写的，字段类型不能假定 —— 这里的异常会让整个界面
            # abort（PyQt5 槽内未捕获异常 = 进程退出），所以先把类型摆平。
            status = {}
        stale = bool(view.get("stale"))
        available = bool(view.get("available"))
        # 哪些字段的数据源已断开（旧版监控不写这个键 → 空表，界面不做任何标注）
        lost_fields = lost_source_fields(status)

        def item_of(field):
            item = status.get(field)
            return item if isinstance(item, dict) else {}

        def text_of(field):
            text = item_of(field).get("text")
            return text if isinstance(text, str) else None

        # ---------------- 急停（安全信息，最醒目） ----------------
        emergency = text_of("emergency")
        # 数据源断开（`/androidmsg_emergencystatus` 没有发布者）与"文件停更"是两种不同的
        # 麻烦：文件新鲜只说明监控进程活着，读数可能早就冻住了 —— 拿一个冻住的"未急停"
        # 当现在的状态，正是这条信息最危险的形态，所以单独一档、措辞上区分开。
        emergency_lost = bool(item_of("emergency").get("source_lost")) and not stale
        engaged = None
        if emergency and not stale and not emergency_lost:
            engaged = emergency.startswith("急停")
            self.emergency_label.setText("已急停" if engaged else f"未急停（{emergency}）")
            theme.set_state(self.pillar, "engaged" if engaged else "normal")
            theme.set_state(self.emergency_label, "engaged" if engaged else "released")
            if self.last_estop_state is not None and self.last_estop_state != engaged:
                self.append(f"机器人急停状态变更：{emergency}")
            self.last_estop_state = engaged
        elif emergency_lost:
            # 不参与急停柱的着色，也不更新 last_estop_state：这一条只说明"读不到"，
            # 不能推出"当前未急停"（也就不该让急停柱回到"安全"的样子）
            self.emergency_label.setText(f"急停 未知（数据源已断开，停更值：{emergency}）")
            theme.set_state(self.pillar, "normal")
            theme.set_state(self.emergency_label, "warn")
            if not self._source_lost_warned:
                self._source_lost_warned = True
                self.append("警告：急停状态的数据源已断开"
                            "（/androidmsg_emergencystatus 没有发布者，底盘可能未连接或已掉线），"
                            "读数不再更新，请以硬件急停按钮为准")
        elif emergency:
            self.emergency_label.setText(f"急停 状态已过期（{emergency}）")
            theme.set_state(self.pillar, "normal")
            theme.set_state(self.emergency_label, "warn")
        elif available:
            self.emergency_label.setText("急停 未知（未收到状态话题）")
            theme.set_state(self.pillar, "normal")
            theme.set_state(self.emergency_label, "warn")
        else:
            self.emergency_label.setText("急停 未知")
            theme.set_state(self.pillar, "normal")
            theme.set_state(self.emergency_label, "none")
        if self.hazard_strip is not None:
            self.hazard_strip.set_active(bool(engaged))

        # ---------------- 电量 + 充电 -> 电池图标 ----------------
        power_item = item_of("power_percent")
        power_raw = power_item.get("raw")
        power_lost = bool(power_item.get("source_lost")) and not stale
        charge_text = text_of("charge")
        charging = bool(charge_text) and any(m in charge_text for m in CHARGING_MARKERS)

        fresh_power = None
        if power_raw is None:
            self.battery.set_state(None, charging=charging, stale=stale)
        else:
            value = _as_int(power_raw)
            if stale or power_lost:
                # 两种"不可信"走同一个显示：底盘掉线时这份电量是停更前的旧值，
                # 而它偏偏是个正常数字，不标出来就没人会怀疑它
                self.battery.set_state(value, charging=charging, stale=True)
            else:
                fresh_power = value
                self.battery.set_state(value, charging=charging, stale=False)

        battery_tip = []
        if charge_text:
            battery_tip.append(f"充电状态：{charge_text}")
        if power_lost:
            battery_tip.append("无数据源：/power_report 已没有发布者（底盘可能未连接或已掉线），"
                               "这里显示的是停更前的读数，不可信")
        elif stale:
            battery_tip.append("读数已过期（状态文件停止刷新），不可信")
        elif power_raw is None:
            battery_tip.append("尚未收到电量话题")
        elif fresh_power is not None and fresh_power < LOW_BATTERY_PERCENT:
            battery_tip.append(f"低于 {LOW_BATTERY_PERCENT}%，长时录制/建图可能被自动回充中断")
        self.battery.setToolTip("\n".join(battery_tip) or "机器人电量")

        # ---------------- 说明行：只在"没数据 / 数据过期"时解释 ----------------
        if stale and monitor_running:
            self.robot_status_label.setText(f"⚠ {view.get('reason') or '机器人状态已停止刷新'}")
            theme.set_state(self.robot_status_label, "warn")
            if not self._status_stale_warned:
                self._status_stale_warned = True
                self.append(
                    f"警告：机器人状态已停止刷新（{view.get('reason') or '监控可能已退出'}）"
                )
        elif stale:
            # 监控压根没启动时，说"监控可能已停止"是误导：直接讲清楚这是上次的旧数据
            age = view.get("age_sec")
            age_text = f"{age:.0f}s 未刷新" if isinstance(age, (int, float)) else "已过期"
            self.robot_status_label.setText(
                f"上次状态数据已过期（{age_text}），点右上「启动状态监控」开始刷新"
            )
            theme.set_state(self.robot_status_label, "none")
            self._status_stale_warned = False
        elif not available:
            self.robot_status_label.setText(view.get("reason") or "机器人状态监控未启动")
            theme.set_state(self.robot_status_label, "none")
            self._status_stale_warned = False
        elif lost_fields:
            names = "、".join(FIELD_NAMES[field] for field in lost_fields)
            self.robot_status_label.setText(f"⚠ {names} 读数已停更（数据源断开）")
            theme.set_state(self.robot_status_label, "warn")
            self._status_stale_warned = False
        else:
            age = view.get("age_sec")
            age_text = f"{age:.1f}s 前更新" if isinstance(age, (int, float)) else "刚刚更新"
            self.robot_status_label.setText(f"状态数据 {age_text}")
            theme.set_state(self.robot_status_label, "none")
            self._status_stale_warned = False

        # ---------------- 模块使能状态（二期） ----------------
        # `file_stale` 要单独传：状态文件停更时，里面冻结的 age_sec 会一直显示"已使能"，
        # 只看模块自己的年龄会得到假绿灯。监控压根没起时 module_state 是空的，
        # 那两个标签自然落到"未知"。
        for label, (text, state) in zip(
                (self.trunk_state_label, self.arm_state_label),
                module_state_texts(view.get("module_state"),
                                   file_stale=bool(stale) or not available)):
            label.setText(text)
            theme.set_state(label, state)

        # ---------------- 低电量提醒（建图/长时录制的头号中断原因） ----------------
        # 只用新鲜数据提醒：过期状态不报警，否则等于拿几十秒前的读数制造假警报
        if fresh_power is not None and fresh_power < LOW_BATTERY_PERCENT:
            if not self._low_battery_warned:
                self._low_battery_warned = True
                self.append(
                    f"警告：机器人电量仅 {fresh_power}%（低于 {LOW_BATTERY_PERCENT}%）。"
                    f"长时间录制或建图可能中途触发自动回充而中断"
                )
        elif fresh_power is not None:
            self._low_battery_warned = False

        # ---------------- 监控自身状态 ----------------
        if monitor_running:
            if available:
                self.robot_monitor_label.setText("状态监控 运行中")
                theme.set_state(self.robot_monitor_label, "ok")
            else:
                self.robot_monitor_label.setText("状态监控 启动中…")
                theme.set_state(self.robot_monitor_label, "warn")
        else:
            self.robot_monitor_label.setText("状态监控 未启动")
            theme.set_state(self.robot_monitor_label, "none")
