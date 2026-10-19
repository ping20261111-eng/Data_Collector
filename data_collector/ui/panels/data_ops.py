"""录制 / 播放 / 频率统计（MainWindow 的一个面板）。

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
from ...core_points import PointRecorder
from ...errors import describe

from ..safety import guard_slot
from ..common import (ensure_remote_password, _warn_if_main_program_running, _play_bag_folder)
from ..dialogs import (RecordDialog)
from ..data_view import (DataViewDialog)
from ..point_dialog import (PointDialog)




class DataOpsMixin:
    """录制 / 播放 / 频率统计。"""

    @guard_slot
    def update_record_time(self):
        self.record_seconds += 1
        minutes = self.record_seconds // 60
        seconds = self.record_seconds % 60
        self.record_status_label.setText(f"录制中 {minutes:02d}:{seconds:02d}")

        # 看门狗：录制期间每秒确认数据确实在落盘。"进程还在"不等于"在录"——
        # 话题无数据、ROS 环境不完整、磁盘满都会让脚本空转或直接退出，那种情况下
        # 计时器还在走、界面显示"录制中"，但磁盘上什么都没有。
        health = self.recorder.health()

        # 「进程已经不在了」必须**每 tick 都判**，放在"只报一次"的闸门之外。
        # v6.4.13 修的现场故障就是这么来的：这一段原来嵌在首次告警分支里，于是
        # "没有落盘"先报过一次 → 闩锁置 True → 脚本随后因磁盘满退出 → 此后每秒
        # 都因为"已告警过"提前 return，**界面永远停在「录制中」**，按钮永远显示
        # 「停止录制」。操作员点它会被分派去新建录制（见 on_record_toggle），现场
        # 看到的正是"点停止却弹出一个新建录制的窗口，怎么都停不下来"。
        # 顺序反过来是因为这两件事性质不同：进程死没死是**状态**（每秒都要准），
        # "要不要为同一件事再说一句"只是**话术**（可以有节制）。
        if not health["alive"]:
            self.record_timer.stop()
            self.record_status_label.setText("录制已中断")
            theme.set_state(self.record_status_label, "none")
            self._finish_points()
            self._set_record_button("idle")
            self.append(f"录制进程已退出，计时已停止{self._record_exit_hint(health)}")
            return

        if health["producing"]:
            self._record_health_warned = False
            return
        if self._record_health_warned:
            return
        self._record_health_warned = True
        self.append(f"录制异常：{health['message']}")

    def _record_exit_hint(self, health: dict) -> str:
        """进程退出后补一句最可能的原因。

        脚本输出不落盘（`launch_record` 走后台模式、不接日志文件），退出原因对界面
        是黑盒；不补这一句，现场只看到"进程已退出"，而真实原因（磁盘满）写在机器人
        自己的日志里、没人会去查。查得到就别浪费 —— 磁盘满是能当场补救的
        （清完空间重录），说清楚比让人猜省事。
        """
        full = self.recorder.disk_full()
        if full:
            return f"（原因：{full} —— 清理后重新录制，本次数据不完整）"
        return "（若数据不完整，请检查 bag 目录）"

    @guard_slot
    def on_record_toggle(self):
        """录制按钮的唯一入口：按当前状态决定是"开始"还是"停止"。

        为什么用状态分派而不是像遥操作那样换信号连接：换连接要 disconnect 几个
        候选槽，漏一个的后果就是"点了没反应"；分派只是一个 if，少一处可错的地方。

        **分派要看"有没有录制会话"，不能只看"进程还活着吗"**（v6.4.13 修）：
        按钮显示的是 `_set_record_button` 记下的状态，而进程可能已经死了、界面
        还没复位（看门狗每秒才跑一次，中间有窗口）。这时操作员点的是「停止录制」，
        按 `is_active()` 分派却会走进 `on_record()` —— 弹出新建录制对话框，现场看到
        的正是"点了停止却弹出一个新建录制的窗口，停不下来"。
        `started_at` 就是这次会话存在的凭据：start() 写入、stop() 清空。
        """
        if self.recorder.is_active() or getattr(self.recorder, "started_at", None) is not None:
            self.on_stop_record()
        else:
            self.on_record()

    def _set_record_button(self, state: str):
        """录制按钮就地切换：idle（数据录制）/ recording（停止录制）。

        与遥操作、上传一致：现场"要停的时候按钮就在原地"，而不是另找一个
        「停止录制」。危险色只在真的录制中出现，空闲态不用灰色禁用表达。
        """
        if state == "recording":
            self.btn_record.setText("停止录制")
            self.btn_record.setToolTip("结束当前录制并保存数据包\n"
                                       "快捷键 F6 = 录制开关；F7 = 只结束录制")
            theme.set_variant(self.btn_record, "danger")
        else:
            self.btn_record.setText("数据录制")
            self.btn_record.setToolTip("开始数据录制（弹窗选类型与任务标签）\n"
                                       "快捷键 F6 = 录制开关；F7 = 只结束录制")
            theme.set_variant(self.btn_record, "default")
        self.btn_record.setEnabled(True)
        # 打点与录制同生共死：放在这里，是因为录制状态的**每一个**出口都会经过它
        # （正常停止、看门狗发现进程已死、启动失败回滚），散在别处一定会漏一处
        self._sync_point_button()

    @guard_slot
    def on_record(self):
        # 冲突先判：否则用户把录制弹窗填完才被告知"已有任务"，白填一遍
        if self.recorder.is_active():
            QMessageBox.information(self, "提示", "已有录制任务正在进行，请先停止")
            return
        # 采集任务清单来自 configs/action_labels.yaml（首次由 record_action_labels 播种）
        # 传 config：让「管理任务」能直接增删并落盘
        dlg = RecordDialog(self, config=self.config)
        if dlg.exec_() != QDialog.Accepted:
            return
        settings = dlg.get_settings()
        if self.recorder.is_active():
            self.show_error("录制冲突", "已有录制任务正在进行")
            return
        # 落盘位置：**起录之前**先确认目标真的能用，再把它记进本次会话。
        # 必须在这里而不是交给脚本 —— 起录后才发现盘写不进去，界面只剩一句从脚本
        # 退出来的"启动后立即退出"，现场完全看不出是盘的问题。
        if not self._apply_record_target(settings):
            return
        if self.recorder.start(settings["type"], settings["name"], settings["desc"],
                               settings["label"], settings["action_id"]):
            # 记住这次的选择（下次打开「录制设置」时作为默认值）。
            # 放在**启动成功之后**：用户点了取消、或起录失败，不该改写"上次用的"。
            self.config.remember_record_settings(settings["type"], settings["label"],
                                                 settings["desc"])
            tag = settings["label"] or "(无)"
            self.append(
                f"录制已后台启动: {settings['type']}, name={settings['name']}, "
                f"desc={settings['desc']}（任务标签 {tag} 已并入 desc）"
            )
            self.record_seconds = 0
            self.record_status_label.setText("录制中 00:00")
            theme.set_state(self.record_status_label, "danger")
            self.record_timer.start(1000)
            self.record_start_time = time.time()
            # 「只报一次」的闩锁必须**每次起录都复位**：它原先只在"又开始落盘了"
            # 的路径上复位，于是上一场录制报过一次异常之后，后面每一场录制都再也不会
            # 提示任何异常 —— 闩锁是按进程活着的，不是按录制会话算的。
            self._record_health_warned = False
            # 打点会话必须**先于**按钮状态建立：_set_record_button → _sync_point_button
            # 会读 point_recorder 来决定按钮显示与否，顺序反了就永远不出现（现场症状）。
            # 起始时刻取自 RecorderManager（它才是真正发指令的时刻，打点的相对秒要以它
            # 为基准去推断 bag 目录）；task 是这次选的采集任务 —— 打点面板按它决定显示
            # 哪一组标签（"选了布草挂架就打找角挂架那一套"）
            self.point_recorder = PointRecorder(
                self.config.get_bag_dir(), settings["name"],
                started_at=self.recorder.started_at,
                task=settings["label"])
            self._set_record_button("recording")
            # 自动弹出打点面板（现场要求）：录制一开始手是忙的，等回过头再去操作区找
            # 「打点」按钮，要标的那一刻早就过去了。原来的模态提示框在这里一并去掉 ——
            # 它会挡在面板前面白要一次点击，而录制已启动这件事按钮、状态行和日志都在说
            self._show_point_panel()
            self.append("录制已后台启动；打点面板已弹出（点标签即以那一刻记一个点）")
        else:
            # 带上具体原因与归属：脚本秒退时"请检查配置"这种话对现场毫无帮助，
            # 而"这是机器人侧的问题"能立刻把排查方向指对
            code = getattr(self.recorder, "last_code", None)
            detail = describe(code, self.recorder.last_error) or "请检查脚本是否存在、依赖是否完整"
            self.show_error("录制启动失败", detail)

    # ---------- 保存位置（本机数据盘 / 移动硬盘） ----------

    def _apply_record_target(self, settings: dict) -> bool:
        """把「保存位置」的选择落到本次会话，并对移动硬盘做起录前预检。

        返回 False = 不该起录（原因已弹给用户）。

        预检为什么放在界面层、而不是交给录制脚本：`ros2 bag record` 写不进去时的
        表现是"进程秒退"，界面只能显示一句"启动后立即退出"，真实原因（盘没挂载 /
        不是数据盘 / 空间不足）在机器人自己的日志里、没人会去查。这里三种都能说清。

        选择**只记在内存里**（`ConfigManager.set_record_target`）：盘拔了下次自动回到
        本机，不会出现"配置还指着那块盘、起录直接失败"。
        """
        from ...disk_export import DISK_TARGET, target_by_key

        key = str(settings.get("record_target") or "local")
        if key == "local" or not key.startswith(f"{DISK_TARGET}:"):
            self.config.set_record_target("local")
            self.append(f"本次录制保存位置：{self.config.get_bag_dir()}（本机数据盘）")
            return True

        # 重新解析一次、并且**按同一个键取**，而不是用对话框里那份快照：
        # ① 从打开对话框到现在，盘可能已经被拔了；
        # ② v6.5.1 之后可能同时列好几块盘，按键取才能确定"就是用户选的那一块"
        #    —— 拿"第一个可用盘"的话，用户选了第二块却写到第一块，且界面看不出来。
        target = target_by_key(self.config, key)
        if target is None:
            self.show_error(
                "移动硬盘不可用",
                f"没有找到这块盘（{key.split(':', 1)[-1]}）。\n"
                f"它可能已经被拔出。\n\n"
                f"可以改用「本机数据盘」录制，或插好硬盘后重新打开「录制设置」。")
            return False
        if not target.available:
            self.show_error(
                "移动硬盘不可用",
                f"{target.reason}。\n\n"
                f"可以改用「本机数据盘」录制，或插好硬盘后重新打开「录制设置」。")
            return False
        try:
            target.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.show_error(
                "移动硬盘不可用",
                f"无法在移动硬盘上创建目录：{target.path}\n{exc}\n\n"
                f"请确认硬盘已插好、可写；必要时改用「本机数据盘」。")
            return False
        self.config.set_record_target(target.key, target.path)
        # 日志里同时给"盘名"和"移动硬盘"这个词：只看路径分不清是哪块盘，
        # 只看盘名又不知道这次是写盘上。排障时两个都要。
        self.append(f"本次录制保存位置：{target.path}"
                    f"（移动硬盘 {target.mount.label}，"
                    f"{target.mount.fs or '未知格式'}，"
                    f"录完可直接拔盘交付）")
        return True

    @guard_slot
    def on_stop_record(self):
        # 进程可能已经自行退出（脚本崩了/被外部杀掉），此时点「停止录制」要能
        # 把界面收干净，而不是弹「停止失败」—— 操作员确实开过录制，也确实结束了，
        # 界面还停在"录制中"才是真bug。这里按"有没有在录/有没有录过"判断，
        # 不按"stop() 有没有真的杀掉进程"判断。
        was_recording = self.recorder.is_active()
        # 用 getattr：这个属性由 RecorderManager.start() 写入，但界面不该假定
        # 它一定存在（替身/旧会话/异常路径下都可能没有）。读不到就当没有会话。
        had_session = getattr(self.recorder, "started_at", None) is not None
        if self.recorder.stop():
            self.append("录制已停止")
            self.record_timer.stop()
            self.record_status_label.setText("已停止")
            theme.set_state(self.record_status_label, "none")
            self._finish_points()
            self._set_record_button("idle")

            if self.record_start_time is not None:
                bag_folders = self.uploader.get_bag_folders()
                latest_folder = None
                for folder in bag_folders:
                    if folder.stat().st_mtime > self.record_start_time:
                        latest_folder = folder
                        break
                if latest_folder:
                    self.append(f"录制文件已保存至: {latest_folder}")
                else:
                    self.append("未找到录制生成的文件，请检查录制是否成功")
                self.record_start_time = None

            QMessageBox.information(self, "提示", "录制已停止")
        elif was_recording or had_session:
            # 进程自己没了，但这次录制确实开过：收尾打点、复位按钮，如实说明
            self.append("录制进程已自行退出，正在收尾")
            self.record_timer.stop()
            self.record_status_label.setText("已停止")
            theme.set_state(self.record_status_label, "none")
            self._finish_points()
            self._set_record_button("idle")
            self.record_start_time = None
            QMessageBox.information(
                self, "提示", "录制进程此前已退出，本次录制已结束。\n"
                             "若数据不完整，请检查 bag 目录并重新录制。")
        else:
            self.show_error("停止失败", "没有活动的录制任务")

    # ---------- 打点 ----------
    def _sync_point_button(self):
        """打点按钮：只在录制进行中显示，并带上本次已打的点数。

        不录制时**隐藏**而不是禁用 —— 打点没有录制起点就没有意义，
        灰按钮会让现场反复点它、然后来问"为什么点不动"。
        """
        recorder = getattr(self, "point_recorder", None)
        # 可见性 = "真的能打点"：既要录制在跑，也要本会话有打点会话。
        # 重开界面时录制可能仍在跑，但本会话没建打点会话（起录时的 name 与起始
        # 时刻都没持久化，重建不准）—— 那种情况按钮就别出现，免得点不动
        recording = self.recorder.is_active() and recorder is not None
        self.btn_point.setVisible(recording)
        count = recorder.count if recording else 0
        self.btn_point.setText(f"打点 ({count})" if count else "打点")

    # ---------- 打点面板 ----------
    @guard_slot
    def on_point(self):
        """操作区的「打点」按钮：打开/前置打点面板。

        面板在录制开始时已自动弹出（`_show_point_panel`），这个按钮是"收起之后
        再打开"的入口 —— 能自己找到入口这件事仍然要保证，不能只靠自动弹。
        """
        recorder = getattr(self, "point_recorder", None)
        if recorder is None or not self.recorder.is_active():
            QMessageBox.information(
                self, "提示", "打点只在录制进行中可用（相对时间需要一个录制起点）")
            return
        panel = getattr(self, "point_panel", None)
        if panel is not None and panel.recorder is not recorder:
            # 会话换了（上一场录制的面板还没被回收）就重开一个，避免往旧会话里写
            self._close_point_panel()
        self._show_point_panel()

    def _show_point_panel(self):
        """建（首次）并前置打点面板。面板非模态，绝不挡住「停止录制」与急停。"""
        recorder = getattr(self, "point_recorder", None)
        if recorder is None:
            return
        panel = getattr(self, "point_panel", None)
        if panel is None:
            panel = PointDialog(self.config, recorder, self)
            panel.points_changed.connect(self._sync_point_button)
            self.point_panel = panel
        panel.show()
        panel.raise_()
        panel.activateWindow()
        self._sync_point_button()

    def _close_point_panel(self):
        """收起并销毁面板。录制结束时必须调用 —— 否则面板还挂在已结束的会话上，
        看着能点，点下去就往一个已经收尾的会话里写。"""
        panel = getattr(self, "point_panel", None)
        if panel is None:
            return
        panel.close()
        panel.deleteLater()
        self.point_panel = None

    def _finish_points(self):
        """录制收尾：把没能关联到录制目录的打点落成一个明确文件，并解散会话。

        宁可多一个"未关联"文件，也不要静默丢 —— 现场看到文件才会去查根因
        （通常是录制时填的 name 与脚本实际用的不一致）。
        """
        # 面板先关：它挂在这次会话上，会话收尾后不该还能点
        self._close_point_panel()
        recorder = getattr(self, "point_recorder", None)
        if recorder is None:
            return
        if recorder.count:
            csv_path = recorder.csv_path()
            self.append(f"本次共打点 {recorder.count} 条"
                        + (f"：{csv_path}" if csv_path is not None else ""))
        fallback = recorder.finalize()
        if fallback is not None:
            self.append(f"注意：有打点没能关联到录制目录，已单独落到 {fallback}"
                        f"（可以核对一下录制名字是否与脚本一致）")
        self.point_recorder = None

    @guard_slot
    def on_play_data(self):
        """播放数据：选择 bag 文件夹，调用 play_bag.sh"""
        if _warn_if_main_program_running(self, self.config):
            return

        folder_path = QFileDialog.getExistingDirectory(
            self,
            "选择要播放的 bag 文件夹",
            str(self.config.get_bag_dir()),
            QFileDialog.ShowDirsOnly
        )
        if not folder_path:
            return

        _play_bag_folder(self, self.launcher, Path(folder_path), self.append)

    @guard_slot
    def on_monitor_performance(self):
        """录制性能监控：新终端启动 monitor_record_performance.sh，等待录制开始后采样。"""
        if self.recorder.is_active():
            self.append("已有录制任务，性能监控将立即开始采样")
        try:
            self.launcher.launch_monitor_performance()
            self.append("已启动录制性能监控（录制开始后自动采样，结束自动汇总）")
        except Exception as e:
            self.show_error("启动失败", f"无法启动性能监控脚本：{e}")

    @guard_slot
    def on_bag_rate_stat(self):
        """采集统计：选一份采集数据，跑完整话题频率统计（bag_rate_stat.py）。

        v6.5.1 起用 `BagPickerDialog`（按时间倒序列出全部 bag、显示体积与话题文件数）
        取代系统目录选择框 —— 后者只有目录名，分不清哪个是 bag、也没法定点判断
        要统计哪一次，而且选到非 bag 目录不会当场拦。
        """
        from ..dialogs import BagPickerDialog
        dlg = BagPickerDialog(self, uploader=self.uploader)
        if dlg.exec_() != QDialog.Accepted:
            return
        folder_path = dlg.selected_path()
        if folder_path is None:
            return
        try:
            self.launcher.launch_bag_rate_stat(str(folder_path))
            self.append(f"已启动采集统计：{folder_path}")
        except Exception as e:
            self.show_error("启动失败", f"无法启动采集统计脚本：{e}")

    @guard_slot
    def on_view_data(self):
        # **不预先问远端密码。** 这个对话框的列表全部来自本地
        # （`get_bag_folders()` / `get_log_files()`），打开它不需要连任何服务器。
        # 原来在这里先问一次密码，离线现场连"看一眼本地录了什么"都做不到 ——
        # 问密码这件事真正需要的地方是 `open_remote_browser`（浏览远端目录），
        # 那里问才问在了刀刃上。
        dlg = DataViewDialog(self.uploader, self.log_fetcher, self)
        dlg.setModal(False)
        dlg.show()
