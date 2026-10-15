"""数据转换入口面板（MainWindow 的一个面板）。

职责只有一个：把「`nerv_lerobot` 的转换界面在不在」翻译成按钮的可用状态与
一句话理由，然后把点击变成一次独立进程的启动。设计取舍见 ADR-003。

构建在 `actions.py`（工具行），逻辑在这里 —— 与其余面板同构。
"""
from PyQt5.QtCore import QTimer

from ...core import logger
from ..safety import guard_slot


#: 子进程退出后的复位延迟。给 300ms 是为了让"点一下立刻变回未启动"的观感
#: 不至于闪；转换界面通常跑几十分钟，真退出时早已超过这个时间。
_EXIT_SETTLE_MS = 300

#: 存活期的轮询间隔。每秒一次 `Popen.poll()`（已有句柄，不 fork），
#: 换来的是"转换窗口崩了主界面会说一句"而不是"点了没反应"。
_ALIVE_POLL_MS = 1000


class ConvertToolMixin:
    """数据转换工具入口：探测 → 置灰/可用 → 启动 → 跟踪存活。"""

    # ---------- 状态同步 ----------
    def _sync_convert_button(self):
        """按探测结果刷按钮：不可用置灰并写明原因，可用则恢复。

        **幂等**：界面启动、工作空间切换、子进程退出后都会调它。
        """
        button = getattr(self, "btn_convert", None)
        if button is None:
            return None                      # 界面还没构建完（测试里常见）

        target = self.launcher.probe_convert_tool()
        self._convert_target = target

        if not target.available:
            # 置灰 + 理由，一件也不能少：不写理由的灰会被现场读成"坏了"
            # （本项目为此立过规矩，见 actions.py 的菜单约定注释）。
            button.setEnabled(False)
            button.setText("数据转换")
            button.setToolTip(
                f"不可用：{target.reason}\n\n"
                "这一步是把录好的 bag 转成训练格式，不影响录制与上传；\n"
                "不装只是用不了这个功能。")
            return target

        running = self.launcher.convert_tool_running()
        self._apply_convert_running(running)
        return target

    def _apply_convert_running(self, running: bool):
        """运行中 / 空闲两种文案。按钮自己说清"现在什么状态、按下去会怎样"。"""
        button = getattr(self, "btn_convert", None)
        if button is None:
            return
        target = getattr(self, "_convert_target", None)
        if running:
            button.setEnabled(False)          # 已在跑，不给"再起一个"的入口
            button.setText("数据转换 · 运行中")
            button.setToolTip(
                "转换界面正在运行（独立窗口）。\n"
                "关掉那个窗口即可结束；本工具关闭时不会中断它。")
        else:
            button.setEnabled(True)
            button.setText("数据转换")
            button.setToolTip(
                "打开数据转换界面：选 bag 目录 → 预检 → 转成训练数据集 → 看报告。\n"
                "在独立窗口里运行，与本工具互不影响。\n"
                + (f"当前仓库：{target.repo}" if target is not None else ""))

    # ---------- 点击 ----------
    @guard_slot
    def on_convert(self):
        """启动数据转换界面。

        三道拦截按"最可能挡路的那条"排序：
          ① 环境没就绪 —— 按钮本该是灰的，但灰按钮在触屏上仍可能被点到，
             这里补一道并把原因原话说一遍（不弹框，直接写日志区：现场一直在看那里）。
          ② 已在运行 —— 界面重复起会写同一个输出目录。
          ③ 启动本身失败 —— 抛给 `show_error`，让人看见原文而不是猜。
        """
        if self._convert_proc is not None and self._convert_proc.poll() is None:
            self.append("数据转换界面已在运行（独立窗口），不重复启动")
            self._apply_convert_running(True)
            return

        target = self.launcher.probe_convert_tool()
        if not target.available:
            # 与按钮 tooltip 同一句话：灰着不给理由会被读成"坏了"，
            # 而"点了灰按钮只写一行日志"更让人以为界面坏了。
            self.show_error("数据转换工具不可用", target.reason)
            self._sync_convert_button()
            return

        if self.launcher.convert_tool_running():
            self.append("检测到数据转换界面已在运行（可能是手动启动的），不重复启动")
            self._apply_convert_running(True)
            self._adopt_running_convert()
            return

        try:
            proc = self.launcher.launch_convert_tool()
        except Exception as exc:                      # noqa: BLE001
            logger.error(f"启动数据转换界面失败：{exc}")
            self.show_error("启动失败", f"无法启动数据转换界面：\n{exc}")
            self._sync_convert_button()
            return

        self._convert_proc = proc
        self._convert_exit_noted = False
        self.append(f"已启动数据转换界面（独立窗口，PID {proc.pid}）；"
                    "本工具关闭时不会中断它")
        self._apply_convert_running(True)
        self._start_convert_alive_timer()

    # ---------- 存活跟踪 ----------
    def _start_convert_alive_timer(self):
        """子进程活着期间每秒 `poll()` 一次；退出即停表并复位按钮。

        不用常驻定时器：这个探测只在"转换界面开着"期间才有意义，
        而转换界面通常开一次跑几十分钟 —— 为此在界面上常驻一个每秒钟的
        定时器（唤醒 Qt 事件循环、让整台机器多耗一份 CPU）不值得。
        """
        self._stop_convert_alive_timer()
        timer = QTimer(self)
        timer.setInterval(_ALIVE_POLL_MS)
        timer.timeout.connect(self._on_convert_poll)
        timer.start()
        self._convert_alive_timer = timer

    def _stop_convert_alive_timer(self):
        timer = getattr(self, "_convert_alive_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._convert_alive_timer = None

    @guard_slot
    def _on_convert_poll(self):
        """转换界面进程还活着吗？不在了就把按钮复位。"""
        proc = getattr(self, "_convert_proc", None)
        if proc is None or proc.poll() is None:
            return
        self._stop_convert_alive_timer()
        self._convert_proc = None
        if not self._convert_exit_noted:
            self._convert_exit_noted = True
            # 崩了与正常关闭在这里分不开（都是进程退出），所以只说一句中性的话
            # 并给出日志路径 —— 真要排查看那个文件，不在这里猜原因。
            self.append("数据转换界面已退出"
                        f"（日志：{self._convert_log_path()}）")
        self._sync_convert_button()

    def _adopt_running_convert(self):
        """接管"用户自己起的"转换界面：只剩按钮状态，没有句柄可轮询。

        刻意不伪造一个句柄 —— `pgrep` 只告诉我们"有一个"， PID 拿不准，
        拿不准就去 `poll()` 等于把别人的进程当自己的管。没有句柄时按钮
        保持"运行中"，直到用户点它（此时再探一次）或重启界面。
        """
        self._stop_convert_alive_timer()
        self._convert_proc = None
        self._apply_convert_running(True)

    def _convert_log_path(self):
        from ...convert_tool import convert_tool_log_file
        try:
            return convert_tool_log_file(self.config)
        except Exception:                             # noqa: BLE001
            return "(未知)"

    # ---------- 生命周期 ----------
    def _shutdown_convert_on_exit(self) -> str:
        """界面退出前对转换界面的处置。**刻意不杀**，只留一句痕迹。

        转换是几十分钟量级的长任务，关界面就杀掉等于丢数据。与遥操作的处理
        刻意不同：遥操作占着机器人本体，所以退出前问一句；转换只占 CPU 与磁盘，
        跑完就完事，留它继续跑是更符合现场预期的选择。
        """
        proc = getattr(self, "_convert_proc", None)
        if proc is not None and proc.poll() is None:
            return "数据转换界面仍在运行（独立进程，未中断；日志见 logs/convert_gui.log）"
        if self.launcher.convert_tool_running():
            return "检测到数据转换界面仍在运行（独立进程，未中断）"
        return ""
