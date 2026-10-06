"""三相机标定与戳点（MainWindow 的一个面板）。

职责边界：**上位机只做入口**。启动哪个节点、下发哪条服务指令由配置决定，
采集流程、按键交互、标定算法全在机器人侧的节点里 —— 上位机不编排流程，
免得两边各维护一套"标定进行到哪一步"的状态。

另一个必须区分的点：遥操作里那个"标定"是 **VR tracker 标定**（走 vr_bridge），
跟这里的**相机标定**毫无关系，界面文案与日志都不要混用"标定"二字。
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
from ...core import (ConfigManager, logger, ProcessManager,
                     DEFAULT_ACTION_LABELS, terminate)
from ...core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)

from ..safety import guard_slot
from ..common import SP
from ..threads import VisionCalibThread, LogTailThread


class VisionMixin:
    """三相机标定与戳点。"""

    # ---------- 节点日志回显 ----------
    def _tail_node_log(self, starter, node_key: str, label: str):
        """启动节点并把它的终端输出 tail 回日志区。

        `starter` 是 Launcher 上的启动方法（launch_camera_calibration /
        launch_pokepoint）。节点跑在后台（没有终端），输出落盘后由
        LogTailThread 逐行回显 —— 采集进度、重投影误差、报错都在里面，
        不回显的话现场只能凭记忆转述。
        """
        node = self.config.ros_node(node_key)
        log_file = self.launcher.vision_log_path(node)
        # 先记下起始偏移：日志文件是 append 打开的，否则会把历史内容又刷一遍
        offset = log_file.stat().st_size if log_file.exists() else 0

        proc = starter()
        self._start_log_tail(proc, log_file, offset, label)
        return proc

    def _start_log_tail(self, proc, log_file, offset, label):
        """起一个 tail 线程；同一时刻只跟一个节点（后启动的接管日志区）。"""
        old = getattr(self, "vision_tail_thread", None)
        if old is not None and old.isRunning():
            old.stop()
            old.wait(1000)

        thread = LogTailThread(log_file, proc=proc, offset=offset)
        thread.line_signal.connect(self.append)
        thread.finished_signal.connect(
            lambda code: self._on_vision_node_exit(label, code))
        thread.start()
        self.vision_tail_thread = thread

    @guard_slot
    def _on_vision_node_exit(self, label: str, code: int):
        """节点退出：收掉「停止相机标定」按钮，并清掉"标定已开始"标记。

        按钮只在节点运行期间出现（空闲时既不占位、也不用灰按钮表达"没什么可停"）。
        """
        self.append(f"{label}节点已退出（退出码 {code}）")
        self.vision_calib_started = False
        self.btn_cancel_calibration.setVisible(False)

    # ---------- 启动节点 ----------
    @guard_slot
    def _start_calibration_node(self):
        """起标定节点（含视频流）并把它的输出接入日志区。返回进程句柄。

        从 `on_camera_calibration` 里抽出来：现在「开始标定」也要用它（现场要求
        "点开始标定后先启动节点，确定节点启动后开始标定"）。
        """
        proc = self._tail_node_log(self.launcher.launch_camera_calibration,
                                  "camera_calib_node", "相机标定")
        # 记下节点进程：「停止相机标定」要能停掉它（否则标定在节点里继续跑，
        # 界面上看就是"取消没反应"）
        self.vision_node_proc = proc
        self.vision_calib_started = False
        self.btn_cancel_calibration.setVisible(True)
        self.append("相机标定节点已启动，输出已接入下方日志")
        if not self.config.has_camera_calibration():
            self.append(f"提示：{self.config.get_vision_calib_dir()} 下还没有标定文件")
        return proc

    @guard_slot
    def on_camera_calibration(self):
        """**只**启动相机标定节点，输出实时回显到日志区；并按配置一并拉起视频流。

        界面上不再单独给这个入口了（已并进「开始标定」），方法留着是因为：
        ① 它是"只想先起节点、看看画面与报错"的那条路（排障时有用）；
        ② `test_ui_structure.py` 的方法清单里钉着它，删掉等于悄悄收窄了对外能力。
        """
        try:
            self._start_calibration_node()
        except Exception as exc:
            self.show_error("启动相机标定失败", str(exc))
            return
        self.append("提示：从「机器人控制 ▾」点「开始标定」会自己起节点并等它就绪，"
                    "一般不必单独走这一步")

    @guard_slot
    def on_pokepoint(self):
        """启动戳点节点，输出实时回显到日志区。"""
        try:
            self._tail_node_log(self.launcher.launch_pokepoint,
                                "pokepoint_node", "戳点")
        except Exception as exc:
            self.show_error("启动戳点失败", str(exc))
            return
        self.append("戳点节点已启动，输出已接入下方日志"
                    "（切相机 1/2/3、选臂 L/R 都在弹出的图像窗口里按）")

    # ---------- 下发采集指令 ----------
    @guard_slot
    def on_start_calibration(self):
        """**开始标定**：先把节点起起来 → 确认它就绪 → 再下发采集指令（现场要求合并成一步）。

        三步在一个动作里，但**每一步单独说结果**，这样现场知道现在停在哪：

        1. 节点没在跑 → 先起（含视频流）并说明；已在跑 → 跳过启动；
        2. 等 `/calibration/start_srv` 出现在 `ros2 service list`（上限 90 秒）——
           这是"节点真的能接指令"的判据，不是"进程还在"（进程起来但 DDS 没发现完时，
           调用只会拿到"服务不存在"）；
        3. 就绪后才 `ros2 service call` 下发。

        任一步没过：**说清停在哪一步、不下发采集指令**，并把节点留在跑 ——
        报错在节点日志里，随手停掉就看不到了。

        两道旧拦截保留：指令已经下发过（`vision_calib_started`）时不再重复下发，
        改为提示用标定窗口的 Enter / ESC；指令在飞时不重复启动线程。
        """
        if getattr(self, "vision_calib_started", False):
            QMessageBox.information(
                self, "标定已在进行",
                "采集指令已经下发过了，不用重复点击。\n\n"
                "标定开始/结束由弹出的标定窗口接管：\n"
                "    · 按 Enter 开始标定\n"
                "    · 按 ESC 取消标定\n\n"
                "若窗口已经关掉，请先点「停止相机标定」再重新点「开始标定」。")
            return

        if getattr(self, "vision_thread", None) is not None \
                and self.vision_thread.isRunning():
            QMessageBox.information(self, "提示", "标定指令正在执行中")
            return

        proc = getattr(self, "vision_node_proc", None)
        if proc is None or proc.poll() is not None:
            self.append("开始标定：标定节点没在跑，先启动它（含视频流）…")
            try:
                self._start_calibration_node()
            except Exception as exc:
                self.show_error("启动相机标定失败", str(exc))
                return
        else:
            self.append("开始标定：标定节点已在运行，跳过启动")

        try:
            timeout = float(self.config.app_config.get("camera_calib_timeout", 600))
        except (TypeError, ValueError):
            timeout = 600.0

        self.append("正在等标定节点就绪（看 /calibration/start_srv 有没有注册出来）…")
        self.vision_thread = VisionCalibThread(
            self.launcher, timeout=timeout, waiter=self._wait_calibration_ready)
        self.vision_thread.progress.connect(self.append)
        self.vision_thread.finished.connect(self._on_vision_calib_finished)
        self.vision_thread.start()

        self.btn_start_calibration.setEnabled(False)
        self.btn_start_calibration.setText("标定中...")

    def _wait_calibration_ready(self):
        """给 `VisionCalibThread` 的前置检查：``() -> (ok, text)``。在后台线程里跑。"""
        result = self.launcher.wait_camera_calibration_ready()
        return bool(result.get("ready")), result.get("text") or ""

    @guard_slot
    def _on_vision_calib_finished(self, ok: bool, output: str):
        self.btn_start_calibration.setEnabled(True)
        self.btn_start_calibration.setText("开始标定")

        if ok:
            # 指令成功 = 节点已进入等待交互状态：从现在起重复点击没有意义
            self.vision_calib_started = True
            self.append("相机标定指令执行完成，节点输出：")
        else:
            self.append("相机标定指令未成功完成，节点输出（原文保留，便于排障）：")
        for line in (output or "").splitlines():
            if line.strip():
                self.append(f"    {line.rstrip()}")
        if ok:
            self.append("请在弹出的标定窗口中按 Enter 开始标定，按 ESC 取消标定")
        if ok and self.config.has_camera_calibration():
            self.append(f"标定文件已更新：{self.config.get_vision_calib_dir()}")
        elif ok:
            self.append(f"注意：{self.config.get_vision_calib_dir()} 下仍未看到标定文件")

    @guard_slot
    def on_cancel_calibration_call(self):
        """停止相机标定：终止采集指令 + 停止标定节点进程。

        旧实现只终止 `ros2 service call` 客户端（秒级就返回了），机器人侧的
        标定节点继续采集 —— 界面上看就是"点了没反应"。标定节点是我们自己
        拉起的后台进程，停掉它才是真的停；重做时从「相机标定 ▾」菜单重新启动。
        """
        stopped = False

        thread = getattr(self, "vision_thread", None)
        if thread is not None and thread.isRunning():
            thread.cancel()
            thread.wait(3000)
            self.append("已取消标定指令")
            stopped = True

        proc = getattr(self, "vision_node_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                terminate(proc)
            except Exception as exc:
                logger.warning(f"停止相机标定节点失败：{exc}")
                self.append(f"停止相机标定节点失败：{exc}")
            else:
                self.append("已停止相机标定节点（要重新标定，从「相机标定 ▾」菜单再启动）")
            self.vision_node_proc = None
            stopped = True

        self.vision_calib_started = False
        self.btn_cancel_calibration.setVisible(False)

        if not stopped:
            QMessageBox.information(self, "提示", "当前没有进行中的相机标定")
