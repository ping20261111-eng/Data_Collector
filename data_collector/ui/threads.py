"""后台工作线程：远程任务、进程退出回执、遥操作日志、标定、上传、日志抓取。"""
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
from typing import Optional
from .. import theme
from ..core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ..core_operations import (_process_running,
                              Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ..core_data import RecorderManager, DataUploader, LogFetcher
from ..core_data import UploadCancelledError
from ..core_release import ReleaseIndex, ReleaseDownloader, ReleaseExtractor
from ..errors import AppError, describe


from .common import (ANSI_RE)



class RemoteTaskThread(QThread):
    """远程 SFTP 长任务线程，避免搜索/下载/上传/统计阻塞 GUI。"""
    finished = pyqtSignal(bool, object, str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, task, args, uploader, parent=None):
        super().__init__(parent)
        self.task = task
        self.args = args
        self.uploader = uploader
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.uploader.cancel_upload()

    def running(self):
        return not self._cancelled

    def run(self):
        try:
            if self.task == "download":
                remote, local = self.args

                def cb(done, total):
                    if not self.running():
                        raise UploadCancelledError("用户取消下载")
                    self.progress.emit(int(done), int(total), "下载")

                ok = self.uploader.download_remote_file(
                    remote, Path(local), cb
                )
                self.finished.emit(
                    ok, None, "下载完成" if ok else "下载已取消"
                )

            elif self.task == "upload_file":
                local, remote = self.args

                def cb(done, total):
                    if not self.running():
                        raise UploadCancelledError("用户取消上传")
                    self.progress.emit(int(done), int(total), "上传")

                ok = self.uploader.upload_local_file(
                    Path(local), remote, self.running, cb
                )
                self.finished.emit(
                    ok, None, "上传完成" if ok else "上传已取消"
                )

            elif self.task == "dir_size":
                remote = self.args[0]

                def cb(done, count):
                    self.progress.emit(
                        int(done), 0, f"已扫描 {count} 个文件"
                    )

                size = self.uploader.get_remote_tree_size(
                    remote, self.running, cb
                )
                self.finished.emit(True, size, "统计完成")

            elif self.task == "search":
                remote, keyword = self.args

                def cb(scanned, matches):
                    self.progress.emit(
                        int(scanned), 0,
                        f"正在搜索：已扫描 {scanned} 项，命中 {matches} 项"
                    )

                result = self.uploader.search_remote(
                    remote, keyword, self.running, cb
                )
                self.finished.emit(
                    True, result, f"搜索完成，共找到 {len(result)} 项"
                )

            elif self.task == "zip_info":
                remote = self.args[0]
                # 远程 ZIP 的详细信息：下载到临时文件后只读取 ZIP 中央目录。
                import tempfile
                transport, sftp = self.uploader._connect_sftp()
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=".zip", delete=False
                    ) as tmp:
                        tmp_path = Path(tmp.name)
                    sftp.get(remote, str(tmp_path))
                    with zipfile.ZipFile(tmp_path, "r") as zf:
                        members = zf.infolist()
                        file_count = sum(1 for x in members if not x.is_dir())
                        dir_count = sum(1 for x in members if x.is_dir())
                        unpacked = sum(x.file_size for x in members)
                    self.finished.emit(
                        True,
                        {
                            "members": len(members),
                            "files": file_count,
                            "dirs": dir_count,
                            "unpacked": unpacked,
                        },
                        "ZIP 信息读取完成"
                    )
                finally:
                    if tmp_path:
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                    sftp.close()
                    transport.close()

            else:
                raise ValueError(f"未知远程任务: {self.task}")

        except UploadCancelledError as e:
            self.finished.emit(False, None, str(e))
        except Exception as e:
            self.finished.emit(False, None, str(e))


class ProcessExitThread(QThread):
    """等一个后台进程结束并回报退出码。

    为什么急停需要它：**躯干没有状态回读**（`trunk_state` 只报关节角），
    所以"指令到底发出去了没有"只能看退出码。只报"指令已发送"而不看结果，
    等于把失败藏起来 —— 安全链路上不能这么做。
    """

    exited = pyqtSignal(int)

    def __init__(self, proc, timeout: float = 15.0, parent=None):
        super().__init__(parent)
        self.proc = proc
        self.timeout = timeout

    def run(self):
        try:
            code = self.proc.wait(timeout=self.timeout)
        except Exception:                # 超时 / 进程异常：统一按失败上报
            code = -1
        self.exited.emit(int(code))


class TeleopLogThread(QThread):
    """尾随遥操作日志文件，把脚本原本输出到终端的内容回显到界面日志区。

    输出写文件而不是管道：界面关闭后进程继续写文件，不会因管道写满而阻塞。
    两种用法：
    - proc 不为空：本次会话启动的进程，用 Popen.poll() 判断存活；
    - proc 为空、pid 不为空：界面重启后接管上一次会话的进程，用 PID 判断存活。
    """

    line_signal = pyqtSignal(str)
    started_signal = pyqtSignal()      # 检测到 ros2 launch 已拉起节点
    # 进程退出码；**None 表示不可知**（接管的会话没有 Popen 句柄，拿不到）。
    # 所以这里用 object 而不是 int —— 声明成 int 时 PyQt 会把 None 转成 0，
    # 于是「拿不到」会被当成「正常退出（0）」，两者在现场的含义完全不同。
    exit_signal = pyqtSignal(object)

    STARTED_MARKERS = ("process started", "process has started")
    POLL_INTERVAL_MS = 200

    def __init__(self, proc, log_file, pid=None, parent=None):
        super().__init__(parent)
        self.proc = proc
        self.pid = int(pid or getattr(proc, "pid", 0) or 0)
        self.log_file = Path(log_file)
        self._stop_requested = False

    def stop(self):
        """界面关闭时调用：只停止读取，不影响后台遥操作进程。"""
        self._stop_requested = True

    def _running(self) -> bool:
        """进程是否还在跑。

        界面重启后没有 Popen 句柄，退化为查 PID —— 但**必须用 `_process_running`
        而不是 `os.kill(pid, 0)`**：后者对僵尸进程也返回成功，于是"已经死掉、只是
        还没被回收"的遥操作会被永远当成活着的，回显线程一直不结束、界面也一直
        停在「运行中」（实测复现过，见 core_operations._process_running 的说明）。
        """
        if self.proc is not None:
            return self.proc.poll() is None
        return _process_running(self.pid)

    def _exit_code(self) -> Optional[int]:
        """退出码：拿不到就返回 None，**不要**编一个 -1 出来。

        接管的会话（`proc is None`）根本没有 Popen 句柄，退出码不可知。
        原先统一返回 -1，界面于是把"遥操作正常结束"报成
        「异常退出（退出码 -1）」还弹一个模态框 —— 而那时人可能正在录制，
        模态框会挡在「停止录制」前面。
        """
        if self.proc is None:
            return None
        code = getattr(self.proc, "returncode", None)
        return None if code is None else int(code)

    def _emit_lines(self, text):
        """按行回显（去掉 ANSI 颜色码），返回是否出现启动标志。"""
        started = False
        for line in text.splitlines():
            clean = ANSI_RE.sub("", line).rstrip()
            if not clean:
                continue
            self.line_signal.emit(clean)
            if any(marker in clean for marker in self.STARTED_MARKERS):
                started = True
        return started

    def _drain(self, stream, pending, notified):
        """读取新增内容并按完整行回显，返回 (未完成的行, 是否已提示启动)。"""
        chunk = stream.read()
        if not chunk:
            return pending, notified
        lines = (pending + chunk).split("\n")
        pending = lines.pop()
        if self._emit_lines("\n".join(lines)) and not notified:
            notified = True
            self.started_signal.emit()
        return pending, notified

    def run(self):
        stream = None
        pending = ""
        notified = False
        while True:
            if stream is None:
                try:
                    stream = self.log_file.open("r", encoding="utf-8", errors="replace")
                except OSError:
                    stream = None      # 文件还没建好/被删，下一轮再试
            if stream is not None:
                pending, notified = self._drain(stream, pending, notified)
            if not self._running() or self._stop_requested:
                break
            self.msleep(self.POLL_INTERVAL_MS)

        if stream is not None:
            # 进程退出后再读一次，避免漏掉最后几行
            pending, notified = self._drain(stream, pending, notified)
            stream.close()
        if pending.strip():
            self._emit_lines(pending)
        if self._stop_requested:
            return      # 界面正在关闭，不再回调
        self.exit_signal.emit(self._exit_code())


class CalibrationThread(QThread):
    finished = pyqtSignal(bool, str, float, float)
    output_signal = pyqtSignal(str)

    def __init__(self, teleop_mgr, username, tracker_num):
        super().__init__()
        self.teleop_mgr = teleop_mgr
        self.username = username
        self.tracker_num = tracker_num

    def run(self):
        try:
            ok, err, sw, al = self.teleop_mgr.run_calibration(
                self.username, self.tracker_num,
                output_callback=self.output_signal.emit
            )
            self.finished.emit(ok, err, sw if sw else 0.0, al if al else 0.0)
        except Exception as e:
            err_msg = traceback.format_exc()
            logger.error(err_msg)
            self.finished.emit(False, f"标定过程异常: {str(e)}\n{err_msg}", 0.0, 0.0)


class UploadThread(QThread):
    """后台上传线程：把一次上传任务交给 uploader 的指定方法执行。

    `upload_latest_bag` / `upload_specific_bag` 只是调用名不同，原先为此写了两份
    几乎逐字重复的线程类 —— 重复的代价是改一处漏一处，所以合并为接收 callable
    的单一实现。
    """

    finished = pyqtSignal(bool, str)
    progress_signal = pyqtSignal(int)

    def __init__(self, uploader, start_callable, progress_cb=None):
        super().__init__()
        self.uploader = uploader
        self._start = start_callable
        self.progress_cb = progress_cb

    def cancel(self):
        self.uploader.cancel_upload()      # 终止 paramiko 传输

    def run(self):
        try:
            def progress_percent_wrapper(value):
                self.progress_signal.emit(value)

            success = self._start(progress_cb=self.progress_cb,
                                  progress_percent=progress_percent_wrapper)
            if success:
                self.finished.emit(True, "上传成功")
            else:
                # describe_last_error() = 原始信息 + 归属 + 可执行建议。
                # 只抛 last_error 的话，界面拿到的是"上传失败：xxx"，用户无从判断
                # 该改配置、找网络，还是找机器人侧。
                self.finished.emit(False, self.uploader.describe_last_error()
                                   or "上传失败或已取消")
        except Exception as e:
            self.finished.emit(False, str(e))


class FetchLogThread(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, fetcher):
        super().__init__()
        self.fetcher = fetcher

    def run(self):
        success = self.fetcher.fetch_latest_log()
        if success:
            self.finished.emit(True, "日志抓取成功")
        else:
            self.finished.emit(False, "日志抓取失败，请查看日志")


class LocalTaskThread(QThread):
    """本地（非网络）长任务的通用线程：把任意 callable 放到后台跑。

    为什么需要它：批量删除数据包要先**递归统计体积**（几十 GB 的目录走一遍就是
    几百毫秒到几秒），再 `shutil.rmtree`（同样秒级）。两者都跑在主线程上会明显卡界面。

    与 RemoteTaskThread 的区别：那个按 `task` 字符串分派且绑着 uploader（网络语义），
    这里只收一个 callable，也不涉及取消 —— 本地删除一旦开始就该做完，
    半途取消只会留下删到一半的目录。

    结果通过 `finished(ok, payload, message)` 回主线程；payload 是 callable 的返回值。

    **刻意不接收 parent**（与 CalibrationThread / UploadThread / FetchLogThread 一致）：
    把 QThread 挂到窗口对象上当子对象，会让 Qt 在销毁窗口时连带销毁线程对象 ——
    若那一刻线程还在收尾，就是 C 层段错误。生命周期由调用方持有的引用管理更安全。
    """
    finished = pyqtSignal(bool, object, str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        try:
            payload = self.task()
        except Exception as exc:
            logger.error(f"本地后台任务失败: {exc}", exc_info=True)
            self.finished.emit(False, None, str(exc))
        else:
            self.finished.emit(True, payload, "")


class VisionCalibThread(QThread):
    """下发相机标定采集指令（`ros2 service call`）的后台线程。

    为什么单独一个类而不是复用 LocalTaskThread：这次调用**需要能被取消** ——
    采集要走完左臂、右臂、头部相机几个位姿，可能持续几分钟，界面必须能停。
    取消的语义是终止"发起调用的客户端进程"，机器人侧的标定节点不受影响，
    所以不会把机器人留在半配置状态。

    结果通过 `finished(ok, output)` 回主线程，output 是**命令原文输出**：
    上位机不解析 Trigger 响应的 success/message —— 那属于机器人侧的语义，
    翻译过来只会丢细节。
    """
    finished = pyqtSignal(bool, str)
    #: 前置步骤的进度一行行回主线程（写进日志区）——"等节点就绪"最长 90 秒，
    #: 界面上一声不吭会被读成卡住
    progress = pyqtSignal(str)

    def __init__(self, launcher, timeout: float = 600.0, waiter=None):
        """
        Args:
            waiter: 可选的前置检查 ``() -> (ok, text)``。给定时**先跑它**：
                不通过就直接结束、不发起调用（用来表达"节点没就绪就不下发采集指令"）。
        """
        super().__init__()
        self.launcher = launcher
        self.timeout = timeout
        self.waiter = waiter
        self._proc = None
        self._cancelled = False

    def cancel(self):
        """终止发起调用的那个客户端进程（不触碰机器人侧节点）。"""
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                from ..core import terminate
                terminate(proc)
            except Exception as exc:
                logger.warning(f"终止标定调用进程失败：{exc}")

    def run(self):
        if self.waiter is not None:
            try:
                ok, text = self.waiter()
            except Exception as exc:                     # noqa: BLE001
                logger.error(f"等待标定节点就绪失败: {exc}", exc_info=True)
                self.finished.emit(False, f"等待标定节点就绪失败：{exc}")
                return
            for line in (text or "").splitlines():
                if line.strip():
                    self.progress.emit(line.rstrip())
            if not ok:
                # 注意：这段会原样进日志区（QTextEdit 不渲染 markdown），别写 ** 这类记号
                self.finished.emit(False, "标定节点未就绪，没有下发采集指令（原因见上一行）")
                return
        try:
            self._proc = self.launcher.start_camera_calibration_call()
        except Exception as exc:
            logger.error(f"下发标定指令失败: {exc}", exc_info=True)
            self.finished.emit(False, str(exc))
            return

        try:
            out, err = self._proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            try:
                out, err = self._proc.communicate(timeout=10)
            except Exception:
                out, err = b"", b""
            self.finished.emit(False, f"标定指令超时（>{self.timeout:.0f} 秒）\n{_decode(err)}")
            return
        except Exception as exc:
            logger.error(f"等待标定指令返回失败: {exc}", exc_info=True)
            self.finished.emit(False, str(exc))
            return

        text = (_decode(out) + _decode(err)).strip()
        if self._cancelled:
            self.finished.emit(False, text or "已取消标定指令")
            return
        ok = self._proc.returncode == 0
        self.finished.emit(ok, text or ("标定指令已下发" if ok else "标定指令失败（无输出）"))


def _decode(data) -> str:
    """子进程输出可能是 bytes，统一解码（坏字节直接跳过，别让解码炸掉回显）。"""
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


class LogTailThread(QThread):
    """通用日志 tail：把后台进程的日志文件逐行回显到界面日志区。

    为什么需要它：相机标定 / 戳点这类节点跑在**后台**（没有终端），
    它们的输出（采集进度、重投影误差、报错）是判断有没有标定成功的唯一依据。
    不回显的话现场只能凭记忆转述，排障信息就被丢在半路了。

    与 TeleopLogThread 的区别：那个带遥操作专用语义（启动标志检测、
    退出码与会话接管），这里只做"把新增内容按行发出去"。

    `offset` 用于只看本次会话：日志文件是 append 打开的，会累积历史内容，
    从调用方记录的起始偏移开始读，就不会把上一次的输出又刷一遍。
    """

    line_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int)     # 进程退出码（-1 表示未知）

    POLL_INTERVAL_MS = 300

    def __init__(self, log_file, proc=None, pid=None, offset=None, parent=None):
        super().__init__(parent)
        self.log_file = Path(log_file)
        self.proc = proc
        self.pid = int(pid or getattr(proc, "pid", 0) or 0)
        self._offset = offset
        self._stop_requested = False

    def stop(self):
        """只停止读取，不影响被 tail 的进程。"""
        self._stop_requested = True

    def _running(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _emit(self, text):
        for line in text.splitlines():
            clean = ANSI_RE.sub("", line).rstrip()
            if clean:
                self.line_signal.emit(clean)

    def run(self):
        stream = None
        pending = ""
        while True:
            if stream is None:
                try:
                    stream = self.log_file.open("r", encoding="utf-8", errors="replace")
                    if self._offset is None:
                        # 调用方没给起点：从**当前文件大小**开始。
                        # 日志文件是 append 打开的、会累积历史内容，若从 0 开始读
                        # 就会把上一次运行的输出整段重放一遍 —— 现场会以为"又卡
                        # 在同一处"，实际那是几分钟前的日志。
                        self._offset = self.log_file.stat().st_size
                    stream.seek(self._offset)
                except OSError:
                    stream = None      # 文件还没建好，下一轮再试
            if stream is not None:
                chunk = stream.read()
                if chunk:
                    lines = (pending + chunk).split("\n")
                    pending = lines.pop()
                    self._emit("\n".join(lines))
                    self._offset = stream.tell()
            if not self._running() or self._stop_requested:
                break
            self.msleep(self.POLL_INTERVAL_MS)

        if stream is not None:
            # 进程退出后再读一次，避免漏掉最后几行
            chunk = stream.read()
            if chunk:
                lines = (pending + chunk).split("\n")
                pending = lines.pop()
                self._emit("\n".join(lines))
            stream.close()
        if pending.strip():
            self._emit(pending)

        code = getattr(self.proc, "returncode", None) if self.proc is not None else None
        self.finished_signal.emit(-1 if code is None else int(code))


# ============ 版本下载（远端文件服务器 → 本地工作空间） ============
class ReleaseFetchThread(QThread):
    """拉远端版本列表。网络请求可能卡十几秒，必须离开主线程。"""

    finished = pyqtSignal(bool, object, str)

    def __init__(self, repo_url: str, timeout: float = 20.0, parent=None):
        super().__init__(parent)
        self.repo_url = repo_url
        self.timeout = timeout

    def run(self):
        try:
            releases = ReleaseIndex(self.repo_url, timeout=self.timeout).fetch()
        except AppError as exc:
            self.finished.emit(False, None, describe(exc.code, exc.message))
        except Exception as exc:                       # noqa: BLE001
            logger.error(f"拉取版本列表异常：{exc}", exc_info=True)
            self.finished.emit(False, None, f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(True, releases, "")


class ReleaseTaskThread(QThread):
    """下载 + 解压一个版本包（界面上是「下载并解压」一个动作）。

    进度分两段：下载能算百分比（Content-Length 给了总长）；解压算不出 ——
    tar.bz2 的总解压量要读完整条压缩流才知道，所以解压段的 total 传 0，
    界面按"不确定进度 + 已解压多少"显示，而不是编一个假的百分比出来。
    """

    finished = pyqtSignal(bool, object, str)
    progress = pyqtSignal(str, int, int, str)      # stage, done, total, note
    stage_changed = pyqtSignal(str)                # download / extract

    def __init__(self, release, download_dir, keep_archive: bool = True, parent=None):
        super().__init__(parent)
        self.release = release
        self.download_dir = download_dir
        self.keep_archive = keep_archive
        self._cancelled = False

    def cancel(self):
        """只置标志位：真正的停止点在 core 的分块循环里，中途打断写文件会留坏档。"""
        self._cancelled = True

    def _stop_now(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            archive = ReleaseDownloader().download(
                self.release, self.download_dir,
                on_progress=lambda done, total, note: self.progress.emit(
                    "download", done, total, note),
                should_cancel=self._stop_now)
        except AppError as exc:
            self.finished.emit(False, None, describe(exc.code, exc.message))
            return
        except Exception as exc:                       # noqa: BLE001
            logger.error(f"下载版本包异常：{exc}", exc_info=True)
            self.finished.emit(False, None, f"{type(exc).__name__}: {exc}")
            return

        self.stage_changed.emit("extract")
        try:
            workspace = ReleaseExtractor().extract(
                archive, self.download_dir,
                on_progress=lambda done, total, note: self.progress.emit(
                    "extract", done, total, note),
                should_cancel=self._stop_now)
        except AppError as exc:
            self.finished.emit(False, None, describe(exc.code, exc.message))
            return
        except Exception as exc:                       # noqa: BLE001
            logger.error(f"解压版本包异常：{exc}", exc_info=True)
            self.finished.emit(False, None, f"{type(exc).__name__}: {exc}")
            return

        if not self.keep_archive:
            try:
                archive.unlink()
            except OSError as exc:
                # 删不掉不是失败：包已经解压好了，只是磁盘上多留一份
                logger.warning(f"删除压缩包 {archive} 失败：{exc}")
        self.finished.emit(True, {"archive": archive, "workspace": workspace}, "")
