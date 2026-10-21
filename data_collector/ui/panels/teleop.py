"""遥操作 + 标定（MainWindow 的一个面板）。

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
from ...core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ...core_data import RecorderManager, DataUploader, LogFetcher
from ...core_data import UploadCancelledError

from ..common import (_as_float, _as_int)
from ..threads import (TeleopLogThread, CalibrationThread)
from ..dialogs import (TeleopDialog, CalibrationDialog)
from ..safety import guard_slot




class TeleopMixin:
    """遥操作 + 标定。"""

    def _set_teleop_button(self, text, slot, variant="default"):
        """遥操作按钮在"启动 / 停止 / 取消 VR 标定"三种状态间切换（共用同一个按钮）。

        `variant` 由调用方显式给出，**不要**再用 `slot is self.on_teleop` 之类的判断：
        绑定方法每次属性访问都是新对象，`a.m is a.m` 为 False，那样写会让"启动遥操作"
        也被涂成危险色（实测就是）。
        """
        for candidate in (self.on_teleop, self.on_cancel_calibration, self.on_stop_teleop):
            try:
                self.btn_teleop.clicked.disconnect(candidate)
            except TypeError:
                pass
        self.btn_teleop.setText(text)
        self.btn_teleop.clicked.connect(slot)
        # 运行中/标定中时按钮语义反转成"停止/取消"：用危险色，避免误点
        theme.set_variant(self.btn_teleop, variant)

    def _teleop_log_path(self) -> Path:
        """遥操作日志文件（本地 logs/ 目录，界面实时读取回显）。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.log_fetcher.local_log_dir / f"teleop_{stamp}.log"

    def _launch_teleop(self):
        """后台启动遥操作：脚本日志回显到日志区，显示启动进度，可随时停止。"""
        if self.teleop_running:
            QMessageBox.warning(self, "提示", "遥操作已在运行")
            return
        log_file = self._teleop_log_path()
        try:
            proc = self.teleop.launch_teleop(log_file)
        except Exception as e:
            self.show_error("启动遥操作失败", str(e))
            return

        self.teleop_proc = proc
        self.teleop_seconds = 0
        self.teleop_running = True
        self.teleop_stopped_by_user = False
        self.teleop_runs = getattr(self, "teleop_runs", 0) + 1
        # ↑ 遥操作的**上电时序会把躯干切成实时模式**（connect → motor_mode 1 → connect，
        #   见 scripts/connect_sequence.py）。姿势控制页据此判断"躯干还可能停在实时模式里"，
        #   并给出一次性的「切回非实时」提示 —— 计数而不是布尔量，因为真正要判断的是
        #   "这一页上次确认非实时之后，有没有又跑过遥操作"。
        # 现场反馈「遥操作开启文案简单一点，不需要暴露那么多信息」：
        # 这一行只要说清"起来了、进度在哪看"，日志文件给文件名就够（绝对路径对现场没用，
        # 抓日志走「查看数据」里的入口）。
        self.append(f"遥操作已启动，上电进度见下方（日志文件: {log_file.name}）")
        self.teleop_thread = TeleopLogThread(proc, log_file)
        self.teleop_thread.line_signal.connect(self.append)
        self.teleop_thread.started_signal.connect(self.on_teleop_started)
        self.teleop_thread.exit_signal.connect(self.on_teleop_exit)
        self.teleop_thread.start()
        self.teleop_timer.start(1000)
        self.teleop_startup_timer.start(self.teleop_startup_timeout_ms)
        self._enter_teleop_starting_mode()

    def _enter_teleop_starting_mode(self):
        """启动中：按钮变“停止遥操作”，显示不确定进度条。"""
        self._set_teleop_button("停止遥操作", self.on_stop_teleop, variant="danger")
        self.teleop_status_label.setText("遥操作 启动中…")
        theme.set_state(self.teleop_status_label, "warn")
        self.teleop_progress_bar.show()

    def _enter_teleop_running_mode(self, note=""):
        """已运行：隐藏启动进度条，状态显示已运行时长，按钮切到「停止遥操作」。

        按钮在这里一并切换，而不是只在 _enter_teleop_starting_mode 里切：
        界面重启后接管上一次会话的进程时不会走「启动中」那一步。
        """
        self.teleop_startup_timer.stop()
        self.teleop_progress_bar.hide()
        self._set_teleop_button("停止遥操作", self.on_stop_teleop, variant="danger")
        self.teleop_status_label.setText(f"遥操作 运行中 {self._teleop_elapsed_text()}{note}")
        theme.set_state(self.teleop_status_label, "ok")

    def _exit_teleop_mode(self, status="未启动"):
        """结束运行：按钮回到"遥操作 · <状态>"，与状态文字同步换词。

        空闲态文案带状态词（计划图里的「遥操作 · 未启动」）：现场扫一眼按钮就知道
        "现在没跑、按下去是启动"，不必再去读下面那行状态字。
        """
        self.teleop_startup_timer.stop()
        self.teleop_timer.stop()
        self.teleop_progress_bar.hide()
        # `status` 只传状态词（未启动 / 已停止），名词在这里统一加 ——
        # 否则退出遥操作后这一格只剩「已停止」，读不出是哪个东西停了。
        self.teleop_status_label.setText(f"遥操作 {status}")
        theme.set_state(self.teleop_status_label, "none")
        self._set_teleop_button(f"遥操作 · {status}", self.on_teleop)

    def _teleop_elapsed_text(self) -> str:
        minutes, seconds = divmod(self.teleop_seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"

    @guard_slot
    def update_teleop_time(self):
        """每秒刷新运行时长（与录制状态标签风格一致）。"""
        self.teleop_seconds += 1
        if self.teleop_running:
            self.teleop_status_label.setText(f"遥操作 运行中 {self._teleop_elapsed_text()}")

    @guard_slot
    def on_teleop_started(self):
        """检测到 ros2 launch 已拉起节点：结束“启动中”。"""
        if self.teleop_running:
            self.append("已检测到 ros2 launch 启动节点，遥操作进入运行状态")
            self._enter_teleop_running_mode()

    @guard_slot
    def on_teleop_startup_timeout(self):
        """启动超时兜底：不再显示“启动中”，提示根据日志确认状态。"""
        if self.teleop_running and not self.teleop_progress_bar.isHidden():
            self.append("未在预期时间内检测到 ros2 launch 启动标志，请根据上方日志确认遥操作状态")
            self._enter_teleop_running_mode(note="（未检测到启动标志）")

    @guard_slot
    def on_stop_teleop(self):
        """停止后台遥操作（含 ros2 launch 拉起的全部节点）。"""
        if not self.teleop_running:
            QMessageBox.warning(self, "提示", "遥操作未在运行")
            return
        self.teleop_stopped_by_user = True
        self.append("正在停止遥操作…")
        self.teleop_status_label.setText("遥操作 停止中…")
        theme.set_state(self.teleop_status_label, "warn")
        self.btn_teleop.setEnabled(False)
        try:
            stopped = self.teleop.stop_teleop()
        finally:
            self.btn_teleop.setEnabled(True)

        if not stopped:
            # 状态文件里已经没有任何可停的东西（进程早就没了、或状态文件已被清掉）。
            # 这时**界面必须跟着回到「未启动」**，否则会留下一个按不动、也点不出结果的
            # 按钮：现场看到的是"界面一直说遥操作在运行，点停止只有一句
            # 没有正在运行的遥操作进程"（实测复现，截图反馈就是这个形态）。
            self.append("遥操作进程已经不在了（可能启动它的那次会话之后它自己退出了）："
                        "界面状态回到「未启动」")
            self._finish_teleop_session("未启动")

    def _finish_teleop_session(self, status: str = "未启动"):
        """把这次遥操作会话收尾：停回显线程 + 复位按钮与状态字。

        `on_teleop_exit`（进程真的结束）与 `on_stop_teleop`（发现根本没有进程）共用。
        抽出来的原因：两处的复位动作必须**完全一致**，各写一份必然漏一处 ——
        而漏掉的那一处留下的就是一个与实际状态不符的界面。
        """
        self.teleop_running = False
        self.teleop_stopped_by_user = False
        self.teleop_proc = None
        self.teleop.clear_state()       # 进程已结束，清掉状态文件
        if self.teleop_thread is not None:
            # 等读取线程真正结束再丢引用：run() 可能尚未返回，
            # 直接置空会在运行中销毁 QThread（对象已在退出路径上，wait 立即返回）
            self.teleop_thread.stop()
            self.teleop_thread.wait(1000)
            self.teleop_thread = None
        self._exit_teleop_mode(status)

    @guard_slot
    def on_teleop_exit(self, code):
        """遥操作进程结束（自行退出或被停止）。

        `code` 可能是 `None`：界面接管的是**上一次会话**启动的进程，没有 Popen 句柄，
        退出码本来就不存在。原先那种情况统一编一个 -1，于是"它正常结束了"会被报成
        「异常退出（退出码 -1）」并弹一个模态框 —— 那一刻人可能正在录制，模态框会
        挡在「停止录制」前面。
        """
        stopped_by_user = self.teleop_stopped_by_user
        self._finish_teleop_session("已停止" if stopped_by_user else "未启动")
        if stopped_by_user:
            self.append("遥操作已停止")
        elif code is None:
            self.append("遥操作已结束（进程由上一次会话启动，退出码不可知）")
        elif code == 0:
            self.append("遥操作已退出（退出码 0）")
        else:
            self.append(f"遥操作异常退出（退出码 {code}），请查看日志")
            QMessageBox.warning(self, "遥操作已退出",
                                f"遥操作进程异常退出（退出码 {code}），请查看上方日志。")

    def _enter_calibration_mode(self):
        # 文案必须与相机标定区分开：项目里有两个"标定"（这里是 VR tracker 标定，
        # 走 vr_bridge；另一个是相机标定，走机器人侧 calibration 节点）。
        # 两处都叫"取消标定"时，同屏两个同名按钮停的是完全不同的东西。
        self._set_teleop_button("取消 VR 标定", self.on_cancel_calibration, variant="danger")

    def _exit_calibration_mode(self):
        self._set_teleop_button("遥操作 · 未启动", self.on_teleop)

    def _current_calibration_user(self) -> str:
        """机器人侧当前生效的标定用户（配置文件里的 `teleop.calibration`）。

        读不到就当没有 —— 这个方法只用来在下拉框里给一句对照提示，
        不该因为一份读不了的配置让"启动遥操作"对话框打不开。
        """
        try:
            cfg = self.config.get_teleop_config()
        except Exception as exc:              # noqa: BLE001 (外部文件，类型也不保证)
            logger.warning(f"读取当前标定用户失败：{exc}")
            return ""
        if not isinstance(cfg, dict):
            return ""
        teleop = cfg.get("teleop")
        return str(teleop.get("calibration") or "") if isinstance(teleop, dict) else ""

    @staticmethod
    def _config_owner(path: Path) -> str:
        """配置文件所属的工作空间名（`<工作空间>/install/config/teleop_config.yaml` 取第一个）。"""
        parents = list(path.parents)
        if len(parents) > 2:
            return parents[2].name
        return str(path)

    def _searched_configs_text(self, *extra: Path) -> str:
        """查过哪些配置，写成一行日志。

        说清楚"查了哪儿"很重要：没找到时现场第一反应是"界面根本没找"，
        而这行字能直接回答他 —— 也让"这个人的标定在别的版本目录里"这件事暴露出来。
        """
        paths = [self.config.teleop_config_path,
                 *self.config.teleop_config_candidates(),
                 *extra]
        names: list = []
        for path in paths:
            owner = self._config_owner(path)
            if owner not in names:
                names.append(owner)
        return "、".join(names)

    @guard_slot
    def on_teleop(self):
        """启动遥操作：**先按名字查我们的文件，查到就直接切到这个人的数据再启动**；
        没有这个人才进标定流程。

        "查一遍我们的文件"包括其它工作空间那份：标定是按工作空间存的，版本升级后
        新工作空间里只有出厂配置，只查当前那份会让每个已经标定过的人都变回"没标定过"。
        """
        config_path = self.config.teleop_config_path
        if not config_path.is_file():
            # 换成一句能直接照做的提示：原先会在标定线程里以"标定失败 + 一堆 stderr"
            # 收场，现场根本看不出真正的问题是工作空间里没有这份配置。
            self.show_error(
                "遥操作配置缺失",
                f"当前工作空间里没有 {config_path}\n\n"
                f"工作空间：{self.config.workspace}\n"
                "请确认顶栏「版本」里选的是机器人正在用的那个工作空间，"
                "或该版本包是否完整。")
            return

        prefs = self.config.get_user_prefs()
        dlg = TeleopDialog(self, users=self.teleop.known_users(),
                           current=self._current_calibration_user(),
                           last_user=prefs.get("teleop_user", ""))
        if dlg.exec_() != QDialog.Accepted:
            return
        username = dlg.get_username()
        if not username:
            self.show_error("输入错误", "用户名不能为空")
            return
        # 记住这一次的人（下次打开对话框预填）。放在这里而不是"启动成功后"：
        # 即使随后标定被取消，这次输入的名字也确实是"这次要用的人"，
        # 下次打开时预填它比预填更早的人更有用。
        self.config.remember_teleop_user(username)

        match = self.teleop.find_user(username)
        if match is not None:
            if not self.teleop.activate_user(match.key, match.source):
                self.show_error("激活失败", "无法更新配置文件，请检查权限")
                return
            if match.key != username:
                self.append(f"按配置文件里的写法「{match.key}」匹配到这个人")
            if match.imported:
                # 说清"参数是哪儿来的"：现场要能分辨"用的是刚标定的"还是"翻出来的旧记录"
                self.append(f"「{match.key}」的标定不在当前工作空间（原在 {match.source}），"
                            "已复制进当前工作空间的配置")
            self.append(f"已激活用户 {match.key}（{match.summary()}），正在启动遥操作...")
            self._launch_teleop()
            return

        self.append(f"「{username}」不在已有标定里（查过：{self._searched_configs_text()}），"
                    "进入标定流程")
        calib_dlg = CalibrationDialog(self, username=username)
        if calib_dlg.exec_() != QDialog.Accepted:
            return
        tracker = calib_dlg.get_tracker_number()
        # 对话框里已经是 1~4 的下拉，这里只当兜底（`get_tracker_number` 对外仍可能被换掉）
        if not tracker.isdigit() or int(tracker) < 1 or int(tracker) > 4:
            self.show_error("输入错误", "请输入1-4的数字")
            return
        self.append(f"开始标定，用户 {username}, tracker {tracker}")
        self.btn_teleop.setEnabled(False)
        self.calib_thread = CalibrationThread(self.teleop, username, int(tracker))
        self.calib_thread.output_signal.connect(self.append)
        self.calib_thread.finished.connect(self.on_calib_finished)
        self.calib_thread.start()
        self._enter_calibration_mode()
        self.btn_teleop.setEnabled(True)

    @guard_slot
    def on_cancel_calibration(self):
        if self.calib_thread and self.calib_thread.isRunning():
            self.append("正在取消标定...")
            self.teleop.cancel_calibration()
            self.calib_thread.wait(2000)
            if self.calib_thread.isRunning():
                # terminate() 是异步的：它返回不代表线程已停。必须再 wait 一次，
                # 否则线程对象随时可能被回收 —— 运行中被销毁会让整个进程 abort。
                self.calib_thread.terminate()
                if not self.calib_thread.wait(1000):
                    logger.warning("标定线程 terminate 后仍未退出，可能影响下一次标定")
            self.calib_thread = None
            self.append("标定已取消")
            self._exit_calibration_mode()
            QMessageBox.information(self, "提示", "标定已取消")
        else:
            QMessageBox.warning(self, "提示", "没有正在进行的标定")

    @guard_slot
    def on_calib_finished(self, ok, err, sw, al):
        self._exit_calibration_mode()
        self.btn_teleop.setEnabled(True)
        if ok:
            msg = f"标定完成！\nshoulder_width = {sw:.4f}\narm_length = {al:.4f}\n配置已更新，正在启动遥操作..."
            QMessageBox.information(self, "标定成功", msg)
            self.append(f"标定完成: shoulder={sw}, arm={al}")
            self._launch_teleop()
        else:
            self.show_error("标定失败", err)

    def _adopt_running_teleop(self):
        """界面重启后接管仍在后台运行的遥操作。

        遥操作进程是独立会话（start_new_session）起的，界面关掉它不会退出；
        进程信息记录在状态文件里，这里重新接上日志回显与「停止遥操作」按钮。
        """
        state = self.teleop.running_state()
        if state is None:
            return

        log_file = Path(state.get("log_file") or "")
        # 状态文件同样是"外部输入"（可能被写到一半、或被旧版本写坏）：
        # 这里跑在启动路径上，一个 ValueError 就足以让界面直接 abort。
        started_at = _as_float(state.get("started_at"), time.time())
        self.teleop_running = True
        self.teleop_stopped_by_user = False
        self.teleop_runs = getattr(self, "teleop_runs", 0) + 1     # 接管上一会话的遥操作也算
        self.teleop_seconds = max(int(time.time() - started_at), 0)
        self.teleop_startup_timer.stop()

        self.teleop_thread = TeleopLogThread(None, log_file,
                                            pid=_as_int(state.get("pid")))
        self.teleop_thread.line_signal.connect(self.append)
        self.teleop_thread.exit_signal.connect(self.on_teleop_exit)
        self.teleop_thread.start()
        self.teleop_timer.start(1000)
        self._enter_teleop_running_mode()

        self.append(
            f"检测到上一次会话启动的遥操作仍在运行（PID={state.get('pid')}），"
            f"已恢复日志回显，可点击「停止遥操作」结束"
        )
