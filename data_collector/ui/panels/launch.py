"""启动自检 / 产品配置 / 主程序启停（MainWindow 的一个面板）。

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
                              EMERGENCY_STOP_PART_NAMES, DISCONNECT_PART_NAMES)
from ...core_data import RecorderManager, DataUploader, LogFetcher
from ...core_data import UploadCancelledError

from ..safety import guard_slot
from ..common import schedule_once
from ..threads import LocalTaskThread





class LaunchMixin:
    """启动自检 / 产品配置 / 主程序启停。"""

    @guard_slot
    def _run_startup_doctor(self):
        """启动自检：把配置/脚本问题在打开界面时就报出来，而不是等到点下去才失败。

        只读探测，不阻塞启动；结果**只进日志区**。

        ## 为什么不弹模态框（2026-10-23，现场「第一次启动 UI 不显示」）

        原来这里失败时会 `show_error(...)`，也就是一个 `QMessageBox.critical`。
        问题在于**它会被叠在另一个模态框的嵌套事件循环里**：

        1. 启动时 `_maybe_setup_product`（0ms）先弹「产品未配置」的询问框；
        2. 1.5s 后本自检触发 —— 此时上面那个框还开着，`exec_()` 的嵌套循环
           正好在跑定时器；
        3. 于是又弹一个 critical 框，**两个模态框叠加**。

        实测（离屏）：`QMessageBox.critical` 一旦弹出就进入阻塞的嵌套循环，
        没有代码能让它自己返回。如果那个框因为窗口管理器/显示环境的原因
        不可见（现场是 GNOME Wayland 会话，Qt 走的是 XWayland），
        用户看到的就是「界面没出来」，随后强杀 → shell 报段错误。

        所以启动阶段**一个模态框都不弹**：配置问题在日志区逐项写清楚了
        （`[错误] 工作空间：<路径>`），界面照常可用，改完配置点「启动程序」即可。
        """
        try:
            results = self.config.check_environment()
        except Exception as exc:
            logger.warning(f"启动自检未能完成：{exc}")
            return

        problems = [r for r in results if not r["ok"]]
        if not problems:
            self.append(f"启动自检通过（{len(results)} 项检查全部就绪）")
            return

        self.append(f"启动自检发现 {len(problems)} 项待处理：")
        for item in problems:
            mark = "错误" if item["level"] == "fail" else "提醒"
            self.append(f"  [{mark}] {item['name']}：{item['detail']}")

        failures = [r for r in problems if r["level"] == "fail"]
        if failures:
            # 汇总成**一行醒目日志**，不弹框（理由见 docstring）。
            # 现场第一眼要能看出"是哪一项、怎么改"，所以把第一项的 detail 也带上。
            names = "、".join(r["name"] for r in failures)
            first = failures[0]
            self.append(f"⚠ 必需配置有问题（{len(failures)} 项）：{names}"
                        f" —— 界面可以用，但相关功能会失败")
            self.append(f"    先看这一项：{first['name']} → {first['detail']}")
            self.append("    改好 configs/app_config.yaml 后点「启动程序」重试，"
                        "或重启本工具让自检重新跑一遍")

    @guard_slot
    def _maybe_setup_product(self):
        """启动时检测产品未配置，提示并运行产品配置向导 setup_product.sh。

        Product.json 缺失或 hand 字段为空都算未配置（手型是启动/数采的前提）。
        整个会话只提示一次；用户点「是」才在新终端跑向导。
        """
        if getattr(self, "_product_setup_prompted", False):
            return
        self._product_setup_prompted = True

        if self.config.is_product_configured():
            return

        # 工作空间都不存在时**不弹向导询问**（2026-10-23）：向导要读的就是工作空间里的
        # 产品与脚本，路径都不对时问「要不要跑向导」只会多一个挡住首启的模态框。
        # 这时真正要处理的是 workspace_root（启动自检会在日志区指出它）。
        workspace = self.config.workspace
        if not workspace.is_dir():
            self.append(f"⚠ 产品未配置，但工作空间不存在：{workspace}")
            self.append("    先把 configs/app_config.yaml 的 workspace_root 改成这台机器上的"
                        "真实路径，再运行产品配置向导（setup_product.sh）")
            return

        ret = QMessageBox.question(
            self, "产品未配置",
            "未检测到产品参数（Product.json 缺失或手型为空）。\n\n"
            f"配置文件：{self.config.get_product_file()}\n"
            "是否现在运行产品配置向导 setup_product.sh？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if ret != QMessageBox.Yes:
            self.append("产品未配置：已跳过 setup_product.sh，可稍后手动配置")
            return

        try:
            self.launcher.launch_setup_product()
            self.append("已启动产品配置向导 setup_product.sh（在独立终端完成配置）")
            self._product_setup_polls = 0
            schedule_once(self, 3000, self._watch_product_setup)
        except Exception as e:
            self.show_error("启动产品配置向导失败", str(e))

    @guard_slot
    def _watch_product_setup(self):
        """轮询等待 setup_product.sh 完成（最多约 2 分钟），配置好就刷新展示。"""
        if self.config.is_product_configured():
            self.refresh_topbar_facts()
            self.refresh_hand_display()
            self.append("产品配置已生效")
            return
        self._product_setup_polls = getattr(self, "_product_setup_polls", 0) + 1
        if self._product_setup_polls < 40:
            schedule_once(self, 3000, self._watch_product_setup)

    @guard_slot
    def _block_launch_when_running(self) -> bool:
        """主程序已在运行时：**只提示、不代为停止**（v4.7.3 起）。

        为什么不代停：`run_nodes.sh` 一旦发现 `wheeled_robot_launch.py` 在跑就直接
        `exit 0`，所以必须先把它停掉。但"停"的正确执行者不是上位机 —— 它要 systemd
        权限、unit 名随部署变化，而且上位机本身可能就活在那个服务的 cgroup 里，
        代为停止会把界面一起带走（现场现象：停止服务后界面消失、一个字日志都没有，
        SIGKILL 无法捕获，事后极难定位）。

        所以这里只把**背景 + 命令**讲清楚，由人在终端执行；命令同时写进日志区
        （可以复制）。判定与 run_nodes.sh 同源（都看进程），避免"界面说没运行、
        脚本却退出"。

        Returns:
            True = 可以继续启动；False = 已提示并拦下本次启动。
        """
        pid = self.sdk_service.launch_pid()
        if pid is None:
            return True

        service = self.sdk_service.name
        lines = [
            f"检测到 wheeled_robot_launch.py 正在运行（PID {pid}）。",
            "",
            "run_nodes.sh 检测到它会直接退出，界面上勾选的 joy / rviz / use_sim /",
            "rgb_only 参数都不会生效，因此本次启动已拦下。",
            "",
            "请先在终端停止它，再回来点「启动程序」：",
            f"    {self.sdk_service.stop_command()}",
        ]
        if self.sdk_service.self_hosted():
            lines += [
                "",
                f"⚠ 本界面当前运行在 {service} 的 cgroup 内：停止该服务会同时结束本界面，",
                "   建议换一台机器 / 另一个终端来执行停止操作。",
            ]
        lines += ["", "（上位机不再代为下发停止指令：权限与部署差异太大，出问题也难查。）"]

        QMessageBox.information(self, "机器人主程序正在运行", "\n".join(lines))
        self.append(f"已拦下启动：主程序仍在运行（PID {pid}）。"
                    f"请先执行：{self.sdk_service.stop_command()}")
        return False

    @guard_slot
    def on_launch(self):
        """启动主程序。

        手型不传 hand= 参数：机器人侧 run_nodes.sh 不解析 hand=，手型一律由
        product.sh（Product.json 的 hand 字段）决定，界面只做只读展示。
        """
        opts = []
        if self.cb_joy.isChecked():
            opts.append("joy")
        if self.cb_rviz.isChecked():
            opts.append("rviz")
        if self.cb_sim.isChecked():
            opts.append("use_sim")
        if self.cb_rgb_only.isChecked():
            opts.append("rgb_only")

        if not self._block_launch_when_running():
            return
        self.btn_launch.setEnabled(False)
        self.append("正在启动主程序...")
        try:
            self.launcher.launch_run_nodes(opts)
            self.append(f"主程序已在新终端启动（参数: {' '.join(opts) or '默认'}）")
            self.btn_launch.setEnabled(True)
        except Exception as e:
            self.btn_launch.setEnabled(True)
            self.show_error("启动失败", str(e))
            return
        # 启动成功后分两步，**顺序有实际差别**：
        # ① 先把状态监控拉起来（电量/急停/充电靠它订阅 /power_report 并落盘），
        # ② 再自动连底盘。反过来做的话，底盘连上后第一帧电量没人在听，得等下一轮。
        self._auto_start_robot_monitor()
        self._auto_connect_chassis()

    def _auto_start_robot_monitor(self):
        """启动程序后自动拉起状态监控（幂等：已经在跑就复用，见 start_robot_monitor）。

        为什么要绑到「启动程序」：电量由 `robot_monitor.py` 订阅 /power_report 落盘，
        界面只是读那份 JSON —— 监控没起，顶栏电量会一直显示"未知"，现场很容易
        误判成"自动连底盘没成功"。
        """
        self.start_robot_monitor(auto=True)
        self.refresh_robot_status()

    def _auto_connect_chassis(self):
        """启动程序后自动下发底盘连接（后台线程，结果写日志区）。

        为什么放后台：`ros2 topic pub -w 1` 会**等底盘节点出现**（主程序是
        `ros2 launch`，节点要几秒才起来），放界面线程就是几秒钟界面不动。
        为什么启动成功才发：启动失败时根本没节点会订阅，发了只能等超时。
        """
        thread = getattr(self, "_chassis_connect_thread", None)
        if thread is not None and thread.isRunning():
            return
        self.append("正在等待底盘节点并自动下发连接指令（connect=true）…")
        self._chassis_connect_thread = LocalTaskThread(self.launcher.connect_chassis)
        self._chassis_connect_thread.finished.connect(self._on_chassis_connect_finished)
        self._chassis_connect_thread.start()

    def _on_chassis_connect_finished(self, ok, payload, error):
        """自动连底盘的回报：成/不成都写进日志区，并刷新一次状态（电量该出来了）。"""
        if ok:
            for line in (payload or []):
                self.append(line)
        else:
            self.append(f"自动连接底盘失败：{error or '未知错误'}"
                        "（可稍后手工补一条 connect）")
        self.refresh_robot_status()

    @guard_slot
    def on_stop_main(self):
        """关闭主程序：二次确认 → 后台停进程 + 清残留节点 → 回报核验结果。

        为什么要确认：这个按钮的代价是整个机器人停机，而它就在操作区里
        （虽然已经与「启动程序」拉开距离）。确认框同时说明"会一并清理残留节点"，
        免得现场以为只是关了个窗口。
        为什么要后台：信号阶梯的等待 + `ros2 topic list` 核验加起来十几秒，
        放主线程就是卡死界面。

        **为什么要两次确认**（v4.14.0）：第一层回答"这个按钮做什么"，第二层回答
        "现在做会毁掉什么"。只有第一层时，它是个**无条件**的确认框 —— 现场点久了
        就变成肌肉记忆；而真正危险的是"带着还在跑的东西去关"：录制中的 bag 会断在
        半截、遥操作会因为主节点消失而失去响应。代价不在"关程序"本身，而在
        **当时还有什么在跑**，所以这一层只在那时才出现，并且**默认按钮是「取消」**。
        """
        thread = getattr(self, "_stop_main_thread", None)
        if thread is not None and thread.isRunning():
            self.append("上一次关闭流程还在进行中，请等它结束")
            return

        # 硬件动作进行中不许关：控制节点在动作中途消失，硬件会停在半途状态
        # （上使能只发到一半 / 正在切实时模式 / 夹爪夹到一半），是最难判断的一种残留。
        # 每个来源单独判，因为"去哪个页面等"不一样。上/下使能现在只在「姿势控制」一页，
        # 所以它并入下面那条 posture 分支（v6.0.0 起独立页已删）。
        if getattr(self, "gripper_action_in_flight", False):
            QMessageBox.information(
                self, "夹爪动作进行中",
                "正在给夹爪下发张/合指令（几秒钟）。\n"
                "这时候关闭主程序会让动作在节点消失时中断，夹爪可能停在半合的位置，\n"
                "请在「夹爪控制」页面等它出结果后再关。")
            self.append("已拦下关闭：夹爪动作还在进行中（等它出结果，通常几秒）")
            return

        if getattr(self, "end_effector_action_in_flight", False):
            # 写配置文件本身是毫秒级，但"写一半被打断"留下的是坏配置，
            # 而它要到下次启动主程序才暴露 —— 比多等两秒的代价大得多
            QMessageBox.information(
                self, "正在写入末端参数",
                "正在把末端参数写入 arm_control_config.yaml（先备份、写后回读校验）。\n"
                "这时候关闭程序会把写入打断，配置可能停在半截状态，\n"
                "请在「末端参数」页面等它出结果后再关。")
            self.append("已拦下关闭：末端参数正在写入（等它出结果，通常一两秒）")
            return

        if getattr(self, "posture_action_in_flight", False):
            # 姿势控制可能正在跑一个多步预设（LB 是 8 步、十几秒），中途关主程序会让
            # 手臂停在两步之间 —— 那正是"手握东西走到一半"的位置
            QMessageBox.information(
                self, "姿势动作进行中",
                "正在执行「姿势控制」里的动作：可能是多步预设，也可能是正在上/下使能。\n"
                "这时候关闭主程序会让手臂/躯干停在两步之间、或让使能动作半途中断，\n"
                "请在「姿势控制」页面点「中止」或等它出结果后再关。")
            self.append("已拦下关闭：姿势控制的动作还在进行中（可点「中止」）")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("关闭主程序")
        box.setText("将停止机器人主程序，并清理残留的 ROS 节点。")
        box.setInformativeText(
            # 部件列表取常量，别在这里再抄一份（「姿势控制」页的单模块下使能走 MODULE_STEPS，
            # 是同一批话题的另一条入口，两处由 test_module_steps_match_connect_sequence 钉住）
            f"关闭前会先向 {DISCONNECT_PART_NAMES} 下发断开连接（下使能），再结束节点进程 ——\n"
            "顺序反了硬件会留在使能状态、电机随后不受控。\n"
            "清理按工作空间归属结束节点进程，遥操作 / 相机标定等辅助进程不受影响。\n"
            "完成后会自动核验剩余话题数并写进日志区。"
        )
        confirm = box.addButton("关闭并清理", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec_()
        if box.clickedButton() is not confirm:
            return

        # 第二层：只有"还有东西在跑"时才出现 —— 逐条列出后果，默认动作是安全的那个
        risks = self._stop_main_risks()
        if risks and not self._confirm_stop_main_risks(risks):
            self.append("已取消关闭主程序（仍有任务在运行，先处理它们更稳妥）")
            return

        self.btn_stop_main.setEnabled(False)
        self.append("正在关闭主程序并清理残留节点…")
        self._stop_main_thread = LocalTaskThread(self.launcher.stop_main_program)
        self._stop_main_thread.finished.connect(self._on_stop_main_finished)
        self._stop_main_thread.start()

    def _stop_main_risks(self) -> list:
        """"现在关掉主程序会毁掉什么"的清单；每条都写清后果，不写"注意风险"。

        只列**真的有影响**的三类：上传走 SFTP、与机器人主程序无关；相机标定 / 戳点
        是独立进程，主程序停不停它们都在。
        """
        risks = []
        try:
            pid = self.sdk_service.launch_pid()
        except Exception as exc:                     # noqa: BLE001 —— 探测失败不该挡住关闭
            logger.warning(f"关闭主程序前探测主程序状态失败（按未运行处理）：{exc}")
            pid = None
        if pid is not None:
            risks.append(f"机器人主程序正在运行（PID {pid}）——"
                         "关掉它机器人会停机，正在进行的作业会被中断。")
        if getattr(self, "teleop_running", False):
            risks.append("遥操作正在运行 —— 遥操作进程不会被这次清理关闭"
                         "（它在放过名单里），但它依赖的机器人主节点会消失："
                         "结果是遥操作还在、机器人却不动，现场容易误判成卡死。")
        if self.recorder.is_active():
            risks.append("数据正在录制 —— 主节点一停，这一段 bag 就断在这里，"
                         "拿到的数据可能不完整（不能用于训练）。")
        return risks

    def _confirm_stop_main_risks(self, risks) -> bool:
        """第二层确认：默认按钮放在「取消」上。

        这一屏的代价是**整台机器人停机 + 可能毁掉一段正在录的数据**，而那个按钮
        就在操作区里、和旁边按钮长得很像。"默认动作 = 安全的那一个"在这里是硬要求。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("请再确认一次：仍有任务在运行")
        box.setText(f"检测到 {len(risks)} 项还在运行：")
        box.setInformativeText(
            "\n\n".join(f"· {line}" for line in risks)
            + "\n\n建议先停止录制 / 遥操作，再关闭主程序。\n确认要现在关闭吗？")
        still = box.addButton("仍然关闭", QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec_()
        return box.clickedButton() is still

    @guard_slot
    def _on_stop_main_finished(self, ok, payload, error):
        self.btn_stop_main.setEnabled(True)
        if ok:
            for line in (payload or []):
                self.append(line)
        else:
            self.show_error("关闭主程序失败", error or "未知错误")
        self.refresh_robot_status()
