import os
import posixpath
import shlex
import shutil
import tarfile
import tempfile
import time
import zipfile
import hashlib
from pathlib import Path
from typing import Optional, List, Callable, Tuple

import paramiko

from .core import (logger, DEFAULT_BAG_DIR, DEFAULT_LOG_DIR, DEFAULT_REMOTE_HOST,
                   DEFAULT_REMOTE_PORT, DEFAULT_REMOTE_USER, DEFAULT_REMOTE_BASE)
from .errors import AppError, ErrorCode, describe, is_retryable
from .manifest import (
    MANIFEST_NAME,
    STATUS_COMPLETE,
    STATUS_INTERRUPTED,
    BagManifest,
)
from .verify import sha256_file, sha256_sftp, write_sha256_sidecar


def _current_operator() -> str:
    """当前操作员标识，用于数据溯源。

    取值顺序：`DATA_COLLECTOR_OPERATOR` 环境变量 → `USER`。
    上位机**不做登录体系**（现场是共用机器，单人操作为主），
    所以只求"能区分是谁录的"，不强求身份认证。
    """
    return os.environ.get("DATA_COLLECTOR_OPERATOR") or os.environ.get("USER") or ""

# 打包/上传调参
PACK_CHUNK = 4 * 1024 * 1024        # 单次读写块大小（进度粒度 vs 系统调用开销）
PROGRESS_INTERVAL = 1024 * 1024     # 每写出/读入 1MB 通知一次进度
ARCHIVE_FORMATS = ("zip", "tar")
TRANSFER_MODES = ("auto", "stream", "tempfile")
COMPRESSIONS = ("store", "deflate")


# 自定义异常。每个都带 code —— 界面据此决定提示与「重试有没有意义」，
# 而 message 里的原始细节一律保留（排障要靠它，不为了好读而把它抹掉）。
class UploadCancelledError(AppError):
    def __init__(self, message: str = "用户取消上传"):
        super().__init__(ErrorCode.CANCELLED, message)


class StreamUnsupportedError(AppError):
    """远端不支持流式直传（例如禁用 exec / 没有 cat），可回退本地打包后上传。"""

    def __init__(self, message: str):
        super().__init__(ErrorCode.REMOTE_COMMAND, message)


class RemoteAuthError(AppError):
    """远端密码未配置。给出可操作的提示，而不是让 paramiko 抛底层认证错误。"""

    def __init__(self, message: str):
        super().__init__(ErrorCode.AUTH, message)


REMOTE_PASSWORD_HINT = (
    "未配置远端 SFTP 密码。请设置环境变量 DATA_COLLECTOR_REMOTE_PASSWORD，"
    "或在 configs/app_config.yaml 中填写 remote_password 后重试"
)


def _mb(size: float) -> str:
    """人类可读体积（GB 以上用 GB，便于大数据量排查）。"""
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 / 1024:.1f} MB"


class ProgressWriter:
    """把只能顺序写的目标（SFTP 通道）包装成 zipfile/tarfile 可用的文件对象。

    zipfile/tarfile 只要求 write/flush：没有 seek/tell 时会自动按流式（顺序）写法处理。
    这里顺带统计已写出字节数，用于上传进度回调。
    """

    def __init__(self, raw, on_bytes: Callable[[int], None] = None,
                 interval: int = PROGRESS_INTERVAL):
        self._raw = raw
        self._on_bytes = on_bytes
        self._interval = max(int(interval), 1)
        self._pending = 0
        self.written = 0
        self.closed = False

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("向已关闭的打包流写入")
        written = self._raw.write(data)
        if written is None:
            written = len(data)
        self.written += written
        self._pending += written
        if self._on_bytes is not None and self._pending >= self._interval:
            self._pending = 0
            self._on_bytes(self.written)
        return written

    def flush(self) -> None:
        flush = getattr(self._raw, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        """只结束包装层，底层通道由调用方关闭（避免打包库提前关掉远端通道）。"""
        self.flush()
        self.closed = True


class ProgressReader:
    """包装源文件：边读边报进度，并支持随时取消（大文件打包时也能立即响应）。

    注意：tarfile 的 copyfileobj 要求“有数据时必须读满”，所以这里只在 size<0 时才
    用 PACK_CHUNK 兜底，正数 size 一律按请求读取；进度通知按 interval 批量上报。
    """

    def __init__(self, raw, on_bytes: Callable[[int], None] = None,
                 running_cb: Callable[[], bool] = None, name: str = "",
                 interval: int = PROGRESS_INTERVAL):
        self._raw = raw
        self._on_bytes = on_bytes
        self._running_cb = running_cb
        self._name = name
        self._interval = max(int(interval), 1)
        self._pending = 0

    def read(self, size: int = -1):
        if self._running_cb is not None and not self._running_cb():
            raise UploadCancelledError(f"用户取消打包：{self._name}")
        if size is None or size < 0:
            size = PACK_CHUNK
        data = self._raw.read(size)
        if data and self._on_bytes is not None:
            self._pending += len(data)
            if self._pending >= self._interval:
                self.flush_pending()
        return data

    def flush_pending(self) -> None:
        """立即上报未满一个 interval 的累计字节（小文件多时保证进度能刷新）。"""
        if self._pending and self._on_bytes is not None:
            delta, self._pending = self._pending, 0
            self._on_bytes(delta)

    def __getattr__(self, item):
        # tarfile 会顺手访问 name 等属性，透传给底层文件对象。
        return getattr(self._raw, item)


class ProgressReporter:
    """统一的打包/上传进度汇报。

    - 文字进度按 0.5s 节流，避免刷屏；
    - 百分比进度支持映射到任意区间（打包 0~50% + 上传 50~100% 的旧行为可复用）。
    """

    def __init__(self, total_bytes: int, label: str = "正在传输",
                 percent_from: int = 0, percent_to: int = 100,
                 progress_cb: Callable[[str], None] = None,
                 progress_percent: Callable[[int], None] = None):
        self.total = max(int(total_bytes), 1)
        self.label = label
        self.percent_from = percent_from
        self.percent_to = percent_to
        self.progress_cb = progress_cb
        self.progress_percent = progress_percent
        self.done = 0
        self._start = time.time()
        self._last_emit = 0.0

    def text(self) -> str:
        elapsed = max(time.time() - self._start, 0.1)
        speed = self.done / 1024 / 1024 / elapsed
        remain = (self.total - self.done) / (speed * 1024 * 1024) if speed > 0 else 0
        return (
            f"{self.label}：{self.done / 1024 / 1024:.1f} / {self.total / 1024 / 1024:.1f} MB  "
            f"速度：{speed:.1f} MB/s  剩余：{remain:.0f} 秒"
        )

    def percent_for(self, done: int) -> int:
        ratio = min(max(done, 0) / self.total, 0.99)
        span = self.percent_to - self.percent_from
        return min(self.percent_from + int(ratio * span), self.percent_to)

    def update(self, done: int, force: bool = False) -> None:
        self.done = done
        now = time.time()
        if not force and self.done < self.total and now - self._last_emit < 0.5:
            return
        self._last_emit = now
        if self.progress_percent:
            self.progress_percent(self.percent_for(done))
        if self.progress_cb:
            self.progress_cb(self.text())

    def finish(self, text: str = "") -> None:
        """收尾：补齐总字节、百分比打到区间上限并输出总结文字。"""
        self.done = self.total
        if self.progress_percent:
            self.progress_percent(self.percent_to)
        if self.progress_cb and text:
            self.progress_cb(text)

class RecorderManager:
    def __init__(self, config, launcher):
        self.config = config
        self.launcher = launcher
        self.current_proc = None
        self.last_error = ""
        self.last_code: Optional[ErrorCode] = None
        # 本次录制的起始时间：看门狗用它划基线，只统计之后写入的文件 ——
        # 否则 bag 目录里的历史数据会让"是否在产出"永远判为真。
        self._started_at: Optional[float] = None
        # 本次录制对应的 bag 目录（写manifest 用）；启动瞬间可能还找不到，
        # 收尾时会再兜一次
        self._manifest_dir: Optional[Path] = None

    def start(self, script_type: str, name: str = "", desc: str = "",
              label: str = "", action_id=None, settle_seconds: float = 0.5) -> bool:
        """启动录制，并确认进程真的活着。

        只把进程拉起来不等于录制成立：脚本可能因缺少依赖、权限不足、bag 目录
        不可写或脚本文件不存在而立刻退出。不做这层校验时，界面会显示「录制中」
        而实际零数据落盘 —— 对采集设备这是最严重的事故形态（用户以为在采，
        等发现时整批数据已报废）。settle_seconds 是留给脚本起步的观察窗口。
        """
        if self.current_proc and self.current_proc.poll() is None:
            logger.warning("录制已在进行中")
            self.last_code = ErrorCode.NOT_FOUND
            self.last_error = "已有录制任务正在进行"
            return False
        try:
            self.current_proc = self.launcher.launch_record(
                script_type, name, desc, label, action_id
            )
        except Exception as e:
            logger.error(f"启动录制失败: {e}")
            self.last_code = ErrorCode.SCRIPT_FAILED
            self.last_error = str(e)
            self.current_proc = None
            return False

        time.sleep(settle_seconds)
        code = self.current_proc.poll()
        if code is not None:
            # 脚本秒退是**机器人侧**的问题（依赖/环境/权限），归属必须标出来 ——
            # 否则操作员会一直在上位机里找原因。
            self.last_code = ErrorCode.SCRIPT_FAILED
            self.last_error = (
                f"录制脚本启动后立即退出（退出码 {code}）。"
                f"请检查脚本是否存在、依赖是否安装、bag 目录是否可写"
            )
            logger.error(self.last_error)
            self.current_proc = None
            return False

        self.last_error = ""
        self._started_at = time.time()
        self._begin_manifest(name, desc, script_type)
        return True

    # ---------- manifest（录制连续性） ----------
    # 目的：让"这次录制完整吗"在**事后**也能回答。health() 只能在运行时判断，
    # 而现场最常见的故障是断电 —— 工程师第二天开机时，没有任何进程能告诉他
    # 昨天那次录到哪、能不能用。manifest跟着 bag 走，换台机器也说得清。

    def _begin_manifest(self, name: str, desc: str, script_type: str) -> None:
        """录制开始时写清单。失败只记日志，不影响录制本身 ——
        采数是主任务，清单是辅助，不该因为写不了清单就不让录。"""
        try:
            bag_dir = self._latest_bag_dir()
            if bag_dir is None:
                # 脚本刚起，bag 目录可能还没建出来：这不是错误，
                # 下一轮 health 巡检会补上
                logger.info("录制刚开始，bag 目录尚未生成，manifest 稍后补写")
                return
            manifest = BagManifest(bag_dir)
            manifest.start(task_name=name or bag_dir.name,
                            operator=_current_operator(),
                            script_type=script_type,
                            extra={"desc": desc} if desc else None)
            self._manifest_dir = bag_dir
            logger.info(f"已写录制清单: {manifest.path}")
        except Exception as exc:
            logger.warning(f"写录制清单失败（不影响录制）：{exc}")

    def _latest_bag_dir(self) -> Optional[Path]:
        try:
            bag_root = Path(self.config.get_bag_dir())
        except Exception as exc:
            logger.warning(f"读取 bag 目录失败：{exc}")
            return None
        if not bag_root.is_dir():
            return None
        try:
            dirs = [p for p in bag_root.iterdir() if p.is_dir()]
        except OSError as exc:
            logger.warning(f"列举 bag 目录失败：{exc}")
            return None
        if not dirs:
            return None
        # 按 mtime 取最新：录制脚本按时间戳命名目录，最新的即本次
        return max(dirs, key=lambda p: p.stat().st_mtime)

    def finalize_manifest(self) -> Optional[str]:
        """录制正常结束后调用：把清单标为 complete 并返回状态。

        若期间真的没落盘任何文件，标成"中断"而不是"完整" ——
        与其让工程师以为录到了，不如明确告诉他这一段是空的。
        """
        bag_dir = getattr(self, "_manifest_dir", None) or self._latest_bag_dir()
        if bag_dir is None:
            return None
        try:
            manifest = BagManifest(bag_dir)
            if not manifest.exists():
                self._begin_manifest("", "", "")
                manifest = BagManifest(bag_dir)
            produced = self._files_since(Path(self.config.get_bag_dir()),
                                         (self._started_at or time.time()) - 1.0)[0]
            if produced <= 0:
                manifest.mark_interrupted("录制已停止，但未检测到新数据落盘")
                return STATUS_INTERRUPTED
            manifest.finish()
            return STATUS_COMPLETE
        except Exception as exc:
            logger.warning(f"收尾录制清单失败：{exc}")
            return None

    def stop(self) -> bool:
        if self.current_proc and self.current_proc.poll() is None:
            from .core import terminate
            terminate(self.current_proc)
            self.current_proc = None
            self.finalize_manifest()
            self._started_at = None
            return True
        return False

    def is_active(self) -> bool:
        return self.current_proc is not None and self.current_proc.poll() is None

    @property
    def started_at(self) -> Optional[float]:
        """本次录制的起始时刻。

        看门狗与打点记录都用它划基准 —— 之前只有私有字段，外部要用只能碰
        `_started_at`，于是"要不要用"这件事就没人愿意做（打点记录当时只能自己取
        `time.time()`，与录制实际起点差着脚本 source 环境的几秒）。
        """
        return self._started_at

    # ---------- 录制看门狗 ----------
    @staticmethod
    def _files_since(root: Path, since: float) -> Tuple[int, float]:
        """统计 root 下在 since 之后被写过的**文件**数，以及最近一次写入的时间。

        只数文件、**跳过目录**：录制脚本会先建出 bag 目录再往里写数据，
        若把新建目录本身算作产出，"脚本起来了但没落盘"会被判成正在产出 ——
        那样 health() 就失去了它唯一要拦的那种事故。
        同理跳过 `manifest.json`：它由上位机自己写，不是机器人侧的数据产出，
        把它算进去会让"零数据录制"被误判为正常。
        """
        count, newest = 0, 0.0
        if not root.is_dir():
            return count, newest
        for path in root.rglob("*"):
            if path.name in (MANIFEST_NAME, "info.json"):
                continue
            try:
                if path.is_dir():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue          # 录制中被移走/重命名的条目，忽略即可
            if mtime >= since:
                count += 1
                newest = max(newest, mtime)
        return count, newest

    def health(self, stale_seconds: float = 15.0) -> dict:
        """录制健康检查：**进程活着 ≠ 数据在落盘**。

        脚本可能"起来了但没在录"：话题上没有数据、ROS 环境不完整、bag 目录
        实际不可写 —— 进程还在，界面显示「录制中」，但磁盘上一个字节都没写。
        只看进程存活挡不住这一类，所以这里额外确认目录里确实有新产出。

        返回 {"alive": bool, "producing": bool, "message": str}。
        """
        if not self.is_active():
            return {"alive": False, "producing": False,
                    "message": "录制进程已退出"}

        # 没有基线就不做产出判断（例如 start 之外的路径起来的录制）
        if self._started_at is None or self.config is None:
            return {"alive": True, "producing": True, "message": ""}

        bag_dir = Path(self.config.get_bag_dir())
        # 文件系统的 mtime 精度可能低于 time.time()（tmpfs / 网络盘尤其明显），
        # 留 1 秒容差，避免录制刚开始那一瞬间被误判成"没有产出"而误报。
        count, newest = self._files_since(bag_dir, self._started_at - 1.0)
        if count == 0:
            return {"alive": True, "producing": False,
                    "message": f"{bag_dir} 下还没有新文件落盘"}
        age = time.time() - newest
        if age > stale_seconds:
            return {"alive": True, "producing": False,
                    "message": f"{bag_dir} 已有 {age:.0f} 秒没有新数据写入"}
        return {"alive": True, "producing": True, "message": ""}


class DataUploader:
    def __init__(self, config=None):
        """config 为 ConfigManager 时从 app_config.yaml 读取远程主机/目录与密码；

        不传 config 时沿用历史默认值（主机/目录），但密码不会内置默认值：
        必须由调用方通过 config 或 `DATA_COLLECTOR_REMOTE_PASSWORD` 提供。
        """
        settings = config.get_remote_settings() if config is not None else {}
        upload = config.get_upload_settings() if config is not None else {}
        self.config = config
        self.bag_dir = config.get_bag_dir() if config is not None else DEFAULT_BAG_DIR
        self.remote_host = settings.get("host", DEFAULT_REMOTE_HOST)
        self.remote_port = int(settings.get("port", DEFAULT_REMOTE_PORT))
        self.remote_user = settings.get("user", DEFAULT_REMOTE_USER)
        self.remote_base = settings.get("base", DEFAULT_REMOTE_BASE)
        self.password = settings.get("password", "")

        # 上传策略（默认：不压缩 + 边打包边直传，大数据量下最稳最快）
        self.transfer_mode = upload.get("mode", "auto")
        self.transfer_format = upload.get("format", "zip")
        self.transfer_compression = upload.get("compression", "store")
        self.upload_retries = max(int(upload.get("retries", 1) or 0), 0)
        self.space_check = bool(upload.get("space_check", True))
        # 内容校验：只比大小挡不住"传输位翻转/ 磁盘静默错误"这类**大小不变**的损坏，
        # 所以加一层 SHA-256。默认关闭是为了不给现有流程增加可见耗时，
        # 现场交付时可在 app_config.yaml 里打开（upload.verify_content: true）。
        self.verify_content = bool(upload.get("verify_content", False))
        # 本地打包的额外空间余量：临时目录很小（tmpfs）时可调小
        self.temp_margin_mb = max(int(upload.get("temp_margin_mb", 200) or 0), 0)
        self.retry_delay = 2.0  # 重试前等待秒数（测试里置 0，避免 sleep）
        # 断点续传开关。只对「本地有完整归档」的路径生效（见 _tempfile_pack_upload）：
        # 流式直传没有本地副本，谈续传只能靠"两次打包的字节流完全一致"这种脆弱假设。
        self.upload_resume = bool(upload.get("resume", True))

        temp_dir_cfg = (upload.get("temp_dir") or "").strip()
        self.temp_dir = (
            Path(temp_dir_cfg).expanduser() if temp_dir_cfg
            else Path(tempfile.gettempdir()) / "data_collector_uploads"
        )
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.upload_cancelled = False
        self.last_error = ""  # 最近一次失败原因，供界面直接显示
        self.last_code: Optional[ErrorCode] = None   # 结构化错误码（界面据此提示/决定重试）
        # 本次上传会话内已打好的本地归档。保留了它，重试才能从断点续传 ——
        # 但也正因为如此，它的清理必须挪到"会话结束"，见 upload_bag_folder。
        self._session_archive: Optional[Path] = None
        # 流式直传在「已写出数据后」失败过 → 后续尝试优先改走本地打包路径。
        # 理由：流式不可续传（它的半成品接不上），继续用它只会每次都从零开始。
        # 但"改走本地打包"必须先确认本地放得下归档，见 _tempfile_switch_ok()。
        self._prefer_tempfile = False
        self._session_est_size = 0

    def _fail(self, code: ErrorCode, message: str) -> bool:
        """记录一次结构化失败。

        统一入口的意义：`last_error` 与 `last_code` 不会各写一半（历史上
        `last_error` 分散在十几处赋值，任何人漏掉一处就会得到"有文案没码"的状态）。
        """
        self.last_code = code
        self.last_error = message
        logger.error(f"[{code.value}] {message}")
        return False

    def describe_last_error(self) -> str:
        """给界面用的完整文案：原始信息 + 归属 + 可执行建议。"""
        return describe(self.last_code, self.last_error)

    def get_bag_folders(self) -> List[Path]:
        if not self.bag_dir.exists():
            return []
        subdirs = [d for d in self.bag_dir.iterdir() if d.is_dir()]
        subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        return subdirs

    def get_latest_bag_folder(self) -> Optional[Path]:
        folders = self.get_bag_folders()
        return folders[0] if folders else None

    # ---------- 打包（zip / tar，统一入口） ----------
    def _iter_files(self, folder: Path) -> tuple:
        """列出待打包文件 [(path, size)] 与总字节数。"""
        entries = []
        total_size = 0
        for root, _, names in os.walk(folder):
            for name in sorted(names):
                path = Path(root) / name
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    raise AppError(ErrorCode.LOCAL_IO, f"读取文件失败：{path}（{exc}）") from exc
                entries.append((path, size))
                total_size += size
        return entries, total_size

    @staticmethod
    def _check_cancel(running_cb, what: str = "打包"):
        if running_cb is not None and not running_cb():
            raise UploadCancelledError(f"用户取消{what}")

    @staticmethod
    def _estimate_archive_size(total_size: int, file_count: int, fmt: str) -> int:
        """估算归档体积，用于流式直传的进度百分比（tar 头 + 512 对齐，zip 本地头 + ZIP64）。"""
        if fmt == "tar":
            overhead = file_count * (512 + 512 + 1536) + 2048
        else:
            overhead = file_count * 400 + 2048
        return int(total_size + overhead)

    def _pack_folder(self, folder: Path, fp, fmt: str, compress: bool,
                     entries: List[tuple], running_cb=None,
                     on_source_bytes: Callable[[int], None] = None,
                     collect_digest: bool = False) -> Optional[str]:
        """把 folder 打包写入 fp（可为不可 seek 的流），返回归档 SHA-256（可选）。

        - arcname 保留 bag 文件夹名（与旧 zip 行为一致：压缩包内为 <bag名>/xxx.mcap）
        - 支持随时取消；on_source_bytes 收到的是“已读源数据累计字节”，用于实时进度
        - `collect_digest=True` 时返回归档字节的 SHA-256。

        **摘要必须算"归档字节"而不是源文件字节** —— 远端收到的就是归档字节。

        ## 为什么不边写边算

        zipfile 关闭时会**回退 seek** 重写 local header 与中央目录
        （实测一个普通 zip 就要回退 5 次），所以"顺序累加"的摘要与落盘内容
        必然不一致。而一个**看起来通过的错误校验比没有校验更危险** ——
        它会让一份坏数据带着"已校验"的标记流进下游。

        因此这里改成：把写入**暂存**到一个可 seek 的对象（SpooledTemporaryFile，
        小归档只在内存，超过阈值自动落盘），关闭后对最终内容一次性求摘要，
        再把字节交给 fp。这样摘要与落盘内容**按构造一致**，不依赖对
        zipfile 内部行为的假设。
        """
        fmt = (fmt or "zip").lower()
        if fmt not in ARCHIVE_FORMATS:
            raise ValueError(f"不支持的打包格式：{fmt}")

        read_total = 0

        spool = None
        sink = fp
        if collect_digest:
            # 内存优先，超出阈值自动落盘到临时文件 —— 单个超大 bag 不会把内存吃满
            spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
            sink = spool

        def on_read(delta: int) -> None:
            nonlocal read_total
            read_total += delta
            if on_source_bytes is not None:
                on_source_bytes(read_total)

        def finish() -> Optional[str]:
            """把 spool 内容写给真实目标，并返回归档摘要。

            摘要与写入都基于**同一份最终字节**，所以两者按构造一致 ——
            这是这里不用"边写边算"的原因，见docstring。
            """
            if spool is None:
                return None
            digest = hashlib.sha256()
            spool.seek(0)
            while True:
                chunk = spool.read(PACK_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                fp.write(chunk)
            spool.close()
            return digest.hexdigest()

        if fmt == "tar":
            # 流式（w|）写法：只顺序写，不 seek，可直接对接网络通道
            with tarfile.open(fileobj=sink, mode="w|", format=tarfile.GNU_FORMAT) as tar:
                top = tarfile.TarInfo(folder.name)
                top.type = tarfile.DIRTYPE
                top.mode = 0o755
                top.mtime = int(folder.stat().st_mtime)
                tar.addfile(top)
                for path, size in entries:
                    self._check_cancel(running_cb)
                    arcname = str(path.relative_to(folder.parent))
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    with path.open("rb") as src:
                        reader = ProgressReader(src, on_read, running_cb, arcname)
                        tar.addfile(info, reader)
                        reader.flush_pending()
            return finish()

        # zip：显式 ZIP64（Bag/MCAP/DB3 容易超过 2GiB/4GiB 边界），
        # 流式写出时 zipfile 会自动改用 data descriptor，解包工具（unzip/7-Zip）均支持。
        zip_compress = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        with zipfile.ZipFile(sink, mode="w", compression=zip_compress, allowZip64=True) as z:
            for path, size in entries:
                self._check_cancel(running_cb)
                arcname = str(path.relative_to(folder.parent))
                info = zipfile.ZipInfo(arcname, date_time=time.localtime(path.stat().st_mtime)[:6])
                info.compress_type = zip_compress
                info.external_attr = (0o100644 << 16)  # -rw-r--r--
                with path.open("rb") as src, z.open(info, "w", force_zip64=True) as dst:
                    while True:
                        self._check_cancel(running_cb)
                        chunk = src.read(PACK_CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                        on_read(len(chunk))
        return finish()

    def compress_folder(self, folder: Path, output_zip: Path,
                        running_cb: Callable[[], bool] = None,
                        progress_cb: Callable[[str], None] = None,
                        progress_percent: Callable[[int], None] = None) -> bool:
        """压缩文件夹（兼容旧接口）：zip + DEFLATE 落盘，进度占 0~50%。

        新流程请用 upload_bag_folder()：默认“不压缩 + 边打包边直传”，
        大数据量下不再需要等量临时空间，也省掉整包 DEFLATE 的 CPU 开销。

        注意：生产路径已全部走 _pack_folder / 流式直传，本方法目前只被测试引用
        （test_core_data.py::test_compress_folder_compat_still_writes_zip）。
        保留是为了不改动既有测试与外部调用习惯，不要在新代码里使用。
        """
        try:
            entries, total_size = self._iter_files(folder)
            if total_size == 0:
                raise AppError(ErrorCode.NOT_FOUND, "bag 文件夹为空")
            reporter = ProgressReporter(total_size, "正在压缩", 0, 50, progress_cb, progress_percent)
            with output_zip.open("wb") as fp:
                self._pack_folder(folder, fp, "zip", True, entries, running_cb, reporter.update)
            reporter.finish(f"压缩完成：{_mb(total_size)}")
            logger.info(f"压缩完成: {output_zip}")
            return True
        except UploadCancelledError:
            logger.info("压缩被取消")
            try:
                if output_zip.exists():
                    output_zip.unlink()
                    logger.info(f"已删除未完成压缩文件: {output_zip}")
            except Exception as cleanup_error:
                logger.warning(f"删除未完成压缩文件失败: {cleanup_error}")
            return False
        except Exception as e:
            logger.error(f"压缩失败: {e}")
            try:
                if output_zip.exists():
                    output_zip.unlink()
                    logger.info(f"已删除压缩失败残留文件: {output_zip}")
            except Exception as cleanup_error:
                logger.warning(f"删除压缩失败残留文件失败: {cleanup_error}")
            return False

    def _ensure_remote_dir(self, sftp, remote_dir: str):
        """递归创建远程目录"""
        dirs = []
        path = remote_dir
        while path not in ("", "/"):
            dirs.append(path)
            path = os.path.dirname(path)
        for d in reversed(dirs):
            try:
                sftp.mkdir(d)
            except IOError as exc:
                # 目录已存在是最常见的正常情况，所以不必上抛；但认证失败、权限不足、
                # 连接断开也会落到这里 —— 一律静默会让错误延后以无关形式爆发
                # （例如几行之后报"文件大小校验不一致"），排障时彻底失去上下文。
                logger.debug(f"创建远端目录 {d} 未成功（可能已存在）：{exc}")

    def _upload_file(self, local_path: Path, remote_path: str,
                 running_cb: Callable[[], bool] = None,
                 progress_cb: Callable[[str], None] = None,
                 progress_percent: Callable[[int], None] = None,
                 resume: bool = False) -> bool:
        """上传单个文件，并**核对远端字节数与本地一致**。

        :param resume: 允许断点续传。前提是「远端那份就是本文件的字节前缀」——
            只有本会话内对**同一个本地归档**续传满足这个前提。流式直传留下的
            残留不在此列：那是另一条字节流的产物，接上去会得到「大小对、内容错」
            的包，而大小校验恰好发现不了它。

        校验不能省：上传完直接 return True，等于把「连接断了但 paramiko 没抛错」
        当成成功 —— 与「录制脚本秒退却显示录制中」是同一类事故形态。
        """
        transport = paramiko.Transport((self.remote_host, self.remote_port))
        sftp = None
        remote_dir = os.path.dirname(remote_path)
        try:
            transport.connect(**self._transport_auth())
            sftp = paramiko.SFTPClient.from_transport(transport)

            self._ensure_remote_dir(sftp, remote_dir)

            file_size = local_path.stat().st_size
            offset = self._resume_offset(sftp, remote_path, file_size) if resume else 0
            if offset:
                logger.info(f"断点续传: {remote_path} 远端已有 {_mb(offset)}/{_mb(file_size)}")
                if progress_cb:
                    progress_cb(f"断点续传：从 {_mb(offset)} 继续（共 {_mb(file_size)}）")

            start = time.time()
            last_progress_time = 0.0

            def callback(done, total):
                nonlocal last_progress_time
                # 每次回调检查取消标志
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消上传")
                elapsed = time.time() - start
                # 速度按「本次真正传了多少」算：续传时不应把跳过的前缀算进去
                speed = (done - offset) / 1024 / 1024 / max(elapsed, 0.1)
                if speed > 0:
                    remain_sec = (file_size - done) / (speed * 1024 * 1024)
                else:
                    remain_sec = 0
                now = time.time()
                if progress_cb and (now - last_progress_time >= 0.5 or done >= file_size):
                    progress_cb(
                        f"正在上传：{done / 1024 / 1024:.1f} / "
                        f"{file_size / 1024 / 1024:.1f} MB  "
                        f"速度：{speed:.1f} MB/s  剩余：{remain_sec:.0f} 秒"
                    )
                    last_progress_time = now
                if progress_percent and file_size:
                    progress_percent(50 + int(done / file_size * 50))

            if offset:
                self._put_from_offset(sftp, local_path, remote_path, offset, callback)
            else:
                sftp.put(str(local_path), remote_path, callback=callback)

            remote_size = sftp.stat(remote_path).st_size
            if remote_size != file_size:
                # 不一致说明远端是坏的（截断或错乱），必须清掉再重来 ——
                # 留着会被下游按文件名当成正常数据取走
                self._remove_remote_quietly(sftp, remote_path)
                return self._fail(
                    ErrorCode.VERIFY_FAILED,
                    f"远端文件大小校验不一致：本地 {_mb(file_size)}，"
                    f"远端 {_mb(remote_size)}（已清理远端半成品，可直接重试）",
                )

            # 大小对不代表内容对：位翻转与磁盘静默错误都保持大小不变。
            digest = self._verify_remote_digest(sftp, local_path, remote_path,
                                                remote_size)
            if self.verify_content and digest is None:
                return False
            if digest:
                # 边车文件让现场能用 `sha256sum -c` 独立复核，不依赖本工具
                write_sha256_sidecar(local_path, digest)
                logger.info(f"内容校验通过，边车已写入: {local_path.name}.sha256")

            if progress_percent:
                progress_percent(100)
            if progress_cb:
                progress_cb(f"上传完成：{file_size / 1024 / 1024:.1f} MB")
            logger.info(f"上传成功: {remote_path}")
            return True

        except UploadCancelledError:
            # 取消是「用户主动中止」：不留半成品
            try:
                if sftp is None:
                    return False
                sftp.remove(remote_path)
                logger.info(f"已删除远程文件: {remote_path}")
                # 尝试删除空目录
                try:
                    sftp.rmdir(remote_dir)
                    logger.info(f"已删除远程空目录: {remote_dir}")
                except IOError as exc:
                    # 目录非空或不存在都属正常，记 debug 即可
                    logger.debug(f"远端目录未删除（非空或不存在）：{exc}")
            except Exception as e:
                logger.warning(f"清理远程文件失败: {e}")
            return False

        except RemoteAuthError as e:
            # 凭据缺失是配置问题，原样抛出提示，别包成“上传失败”让人以为网络坏了
            self._fail(ErrorCode.AUTH, str(e))
            return False
        except paramiko.AuthenticationException as e:
            # 注意顺序：它是 SSHException 的子类，必须排在前面，否则会被归成「连接失败」
            self._fail(ErrorCode.AUTH, f"远端认证失败：{e}")
            return False
        except paramiko.SSHException as e:
            self._fail(ErrorCode.CONNECT, f"连接远端失败：{e}")
            return False
        except Exception as e:
            # 传输中断：**不删远端**，留给下一次尝试续传；由会话结束统一清理。
            # 删了就永远没得续 —— 这正是「重试总是从头开始」的根因。
            self._fail(ErrorCode.INTERRUPTED, f"上传中断：{e}")
            return False
        finally:
            transport.close()

    def _verify_remote_digest(self, sftp, local_path: Path, remote_path: str,
                              remote_size: int) -> Optional[str]:
        """内容级校验：比对本地与远端的 SHA-256，不一致则清理远端并报带码失败。

        为什么在大小校验之后做：大小是廉价的快速闸门，先用它挡掉截断；
        剩下"大小对但内容错"的那一类才交给 SHA-256。

        返回本地摘要（供调用方写边车文件）；未开启校验时返回 None。
        任何读取异常都**按校验失败处理** —— 读不到摘要就等于没验过，
        把它当成功会留下一个虚假的安全感。
        """
        if not self.verify_content:
            return None
        try:
            local_digest = sha256_file(local_path)
            remote_digest = sha256_sftp(sftp, remote_path, remote_size)
        except Exception as exc:
            self._remove_remote_quietly(sftp, remote_path)
            self._fail(
                ErrorCode.VERIFY_FAILED,
                f"内容校验无法完成：{type(exc).__name__}: {exc}"
                f"（已清理远端半成品，可直接重试）",
            )
            return None
        if local_digest != remote_digest:
            self._remove_remote_quietly(sftp, remote_path)
            self._fail(
                ErrorCode.VERIFY_FAILED,
                f"远端内容校验不一致：本地 {local_digest[:16]}…，"
                f"远端 {remote_digest[:16]}…（已清理远端半成品，可直接重试）",
            )
            return None
        return local_digest

    @staticmethod
    def _resume_offset(sftp, remote_path: str, file_size: int) -> int:
        """可安全续传的偏移；不适合续传时返回 0（由 SFTP 截断重写）。

        只有 `0 < 远端 < 本地` 才续：
        - 远端更大 → 不是同一份内容，绝不能往上接；
        - 两者相等 → 数据可能已完整，但没经过校验，重写一遍更便宜也更安全。
        """
        try:
            remote_size = sftp.stat(remote_path).st_size
        except IOError:
            return 0
        return int(remote_size) if 0 < remote_size < file_size else 0

    @staticmethod
    def _put_from_offset(sftp, local_path: Path, remote_path: str, offset: int,
                         callback: Callable[[int, int], None]) -> None:
        """从 offset 处续写远端文件。

        用 `r+b` 而不是 `ab`：SFTP 的 APPEND 标志会让 seek 失效、所有写强制落到
        文件末尾，既定位不了也多不出好处；`r+b` 的语义（读+写、不截断）才是对的。
        """
        total = local_path.stat().st_size
        with local_path.open("rb") as fp, sftp.open(remote_path, "r+b") as remote:
            remote.set_pipelined(True)
            remote.seek(offset)
            fp.seek(offset)
            done = offset
            while True:
                buf = fp.read(PACK_CHUNK)
                if not buf:
                    break
                remote.write(buf)
                done += len(buf)
                callback(done, total)

    # ---------- 打包 + 上传（统一入口） ----------
    def _archive_name(self, folder: Path) -> str:
        suffix = "tar" if self.transfer_format == "tar" else "zip"
        return f"{folder.name}.{suffix}"

    def _remote_target(self, archive_name: str, remote_subpath: str = "") -> tuple:
        remote_dir = self.remote_base + (f"/{remote_subpath}" if remote_subpath else "")
        return remote_dir, f"{remote_dir}/{archive_name}"

    def _ensure_local_space(self, needed: int) -> None:
        """本地临时目录余量检查：打包到一半写满盘是大数据上传最常见的失败原因。

        余量默认 200MB，可用 app_config.yaml 的 `upload_temp_margin_mb` 调整
        （临时目录是容量很小的 tmpfs 时需要调小）。
        """
        try:
            free = shutil.disk_usage(str(self.temp_dir)).free
        except OSError as exc:
            logger.warning(f"无法获取临时目录空间信息：{exc}")
            return
        required = int(needed * 1.05) + self.temp_margin_mb * 1024 * 1024
        if free < required:
            raise AppError(ErrorCode.LOCAL_SPACE,
                f"本地临时目录空间不足：打包约需 {_mb(required)}，可用 {_mb(free)}（{self.temp_dir}）。"
                f"可把 app_config.yaml 的 upload_temp_dir 指向数据盘、调小 upload_temp_margin_mb"
                f"（当前 {self.temp_margin_mb}MB），或直接用流式直传（upload_mode: stream）"
            )

    def _ensure_remote_space(self, remote_dir: str, needed: int) -> None:
        if not self.space_check:
            return
        usage = None
        # 目标目录可能还没创建（首次上传），df 会报 No such file，此时退一步查 remote_base
        for probe in (remote_dir, self.remote_base):
            try:
                usage = self.get_remote_disk_usage(probe)
                break
            except Exception as exc:
                logger.warning(f"远端空间检查（{probe}）失败：{exc}")
        if usage is None:
            logger.warning("远端空间检查跳过：无法获取远端可用空间")
            return
        free = int(usage.get("free") or 0)
        if free and free < int(needed * 1.02):
            raise AppError(
                ErrorCode.REMOTE_SPACE,
                f"远端剩余空间不足：需要约 {_mb(needed)}，可用 {_mb(free)}（{remote_dir}）"
            )

    @staticmethod
    def _close_quietly(obj) -> None:
        try:
            if obj is not None:
                obj.close()
        except Exception:
            pass

    def _remove_remote_quietly(self, sftp, remote_path: str) -> None:
        if sftp is None:
            return
        try:
            sftp.remove(remote_path)
            logger.info(f"已删除远端半成品: {remote_path}")
        except IOError as exc:
            # 文件本来就不存在（例如取消时已清过、或续传前发现不一致）属正常情况，
            # 不该报成警告 —— 否则每次正常收尾都会刷一条无意义的告警
            logger.debug(f"远端文件无需删除：{exc}")
        except Exception as exc:
            logger.warning(f"清理远端文件失败：{exc}")

    def _remove_remote_by_path_quietly(self, remote_path: str) -> None:
        """按路径静默删除远端文件（打包上传失败后清理半成品用）。

        需要独立建连：失败发生在 `_upload_file` 内部，那条连接已经关闭。
        清理失败只记警告 —— 收尾动作不该再抛异常掩盖原始错误。
        """
        try:
            transport, sftp = self._connect_sftp()
        except Exception as exc:
            logger.warning(f"清理远端半成品失败（无法连接）：{exc}")
            return
        try:
            self._remove_remote_quietly(sftp, remote_path)
        finally:
            sftp.close()
            transport.close()

    def upload_latest_bag(self, remote_subpath: str = "",
                          progress_cb: Callable[[str], None] = None,
                          progress_percent: Callable[[int], None] = None) -> bool:
        """上传最新 bag 文件夹（支持取消）"""
        latest = self.get_latest_bag_folder()
        if not latest:
            self._fail(ErrorCode.NOT_FOUND, "没有找到 bag 文件夹")
            if progress_cb:
                progress_cb(self.describe_last_error())
            return False
        return self.upload_bag_folder(latest, remote_subpath, progress_cb, progress_percent)

    def upload_specific_bag(self, folder_path: Path, remote_subpath: str = "",
                            progress_cb: Callable[[str], None] = None,
                            progress_percent: Callable[[int], None] = None) -> bool:
        """上传指定的 bag 文件夹（内部走打包 + 上传统一流程）"""
        return self.upload_bag_folder(folder_path, remote_subpath, progress_cb, progress_percent)

    def upload_bag_folder(self, folder_path: Path, remote_subpath: str = "",
                          progress_cb: Callable[[str], None] = None,
                          progress_percent: Callable[[int], None] = None) -> bool:
        """上传指定数据文件夹。

        默认策略（可在 app_config.yaml 调整）：
        - 不压缩（MCAP/视频本身已压缩，再 DEFLATE 只耗 CPU）
        - 边打包边直传（不占本地临时空间，pack 与上传重叠进行）
        - 失败自动重试；远端不支持流式时自动回退“本地打包 + SFTP 上传”
        """
        self.upload_cancelled = False
        self.last_error = ""
        self.last_code = None
        # 会话级状态：本地归档与远端半成品都活到本次上传结束（成功或最终失败），
        # 中途不清理 —— 否则「重试从断点续传」永远无料可续。
        self._session_archive = None
        self._prefer_tempfile = False

        folder = Path(folder_path) if folder_path is not None else None
        if folder is None or not folder.exists() or not folder.is_dir():
            return self._fail(ErrorCode.NOT_FOUND, f"文件夹不存在：{folder_path}")

        try:
            entries, total_size = self._iter_files(folder)
        except AppError as exc:
            return self._fail(exc.code, exc.message)
        except RuntimeError as exc:
            return self._fail(ErrorCode.NOT_FOUND, str(exc))
        if total_size == 0:
            return self._fail(ErrorCode.NOT_FOUND, f"bag 文件夹为空：{folder}")

        archive_name = self._archive_name(folder)
        remote_dir, remote_path = self._remote_target(archive_name, remote_subpath)
        est_size = self._estimate_archive_size(total_size, len(entries), self.transfer_format)
        self._session_est_size = est_size   # 供 _tempfile_switch_ok 判断本地放不放得下
        mode_label = {
            "stream": "流式直传（不落本地临时盘）",
            "tempfile": "本地打包后上传",
            "auto": "流式直传（不可用则回退本地打包）",
        }.get(self.transfer_mode, "流式直传（不可用则回退本地打包）")
        compress_label = "不压缩" if self.transfer_compression == "store" else "DEFLATE 压缩"
        logger.info(
            f"开始上传 {folder.name}：方式={mode_label} 格式={self.transfer_format}({compress_label}) "
            f"数据量={_mb(total_size)} 远端={remote_path}"
        )
        if progress_cb:
            progress_cb(
                f"上传方式：{mode_label} / {self.transfer_format}({compress_label})，"
                f"数据量 {_mb(total_size)}"
            )

        try:
            self._ensure_remote_space(remote_dir, est_size)
        except AppError as exc:
            self._fail(exc.code, exc.message)
            if progress_cb:
                progress_cb(self.describe_last_error())
            return False
        except RuntimeError as exc:
            self._fail(ErrorCode.UNKNOWN, str(exc))
            if progress_cb:
                progress_cb(self.describe_last_error())
            return False

        def running():
            return not self.upload_cancelled

        attempts = self.upload_retries + 1
        succeeded = False
        try:
            for attempt in range(1, attempts + 1):
                try:
                    ok = self._upload_bag_attempt(
                        folder, entries, total_size, remote_dir, remote_path,
                        running, progress_cb, progress_percent
                    )
                except UploadCancelledError:
                    logger.info("上传已取消")
                    self.last_code = self.last_code or ErrorCode.CANCELLED
                    self.last_error = self.last_error or "上传已取消"
                    if progress_cb:
                        progress_cb("上传已取消")
                    return False
                except AppError as exc:
                    # 带码异常原样保留码（本地空间/远端空间/本地 IO…），
                    # 否则会被统一归成"传输中断"，重试策略就失去依据了
                    ok = False
                    self._fail(exc.code, exc.message)
                except Exception as exc:
                    ok = False
                    self._fail(ErrorCode.INTERRUPTED, f"上传失败：{exc}")
                if ok:
                    succeeded = True
                    return True
                # 无意义的重试要当场停：凭据错、远端没有 cat、空间不足 ——
                # 重试一万次也是同一个结果，白等 retry_delay 还给出误导性提示
                if not is_retryable(self.last_code):
                    logger.error(f"错误不可重试（{self.last_code}），停止重试")
                    if progress_cb:
                        progress_cb(self.describe_last_error())
                    return False
                if attempt < attempts:
                    logger.warning(f"上传失败，准备第 {attempt + 1}/{attempts} 次重试")
                    if progress_cb:
                        progress_cb(f"上传失败，正在重试（{attempt + 1}/{attempts}）...")
                    if self.retry_delay:
                        time.sleep(self.retry_delay)
            if progress_cb and self.last_error:
                progress_cb(self.describe_last_error())
            return False
        finally:
            self._end_upload_session(remote_path, succeeded)

    def _end_upload_session(self, remote_path: str, succeeded: bool) -> None:
        """上传会话收尾：本地归档一定删；远端半成品只在**最终没成功**时删。

        为什么不在每次尝试后删远端：删了就没得续传 —— 这正是「重试总是从头开始」
        的根因。为什么最终失败还是必须删：远端留一个同名截断包，下游按文件名取用
        就会把残缺数据当正常数据用，那比传失败本身严重得多（P0-3 的原始意图）。

        取消也算「没成功」：用户主动中止，不留半成品。
        """
        archive = self._session_archive
        self._session_archive = None
        if archive is not None and archive.exists():
            try:
                archive.unlink()
                logger.info(f"已删除本地临时归档: {archive}")
            except OSError as exc:
                logger.warning(f"删除本地临时归档失败：{exc}")
        if not succeeded:
            self._remove_remote_by_path_quietly(remote_path)

    def _upload_bag_attempt(self, folder: Path, entries, total_size: int,
                            remote_dir: str, remote_path: str, running_cb,
                            progress_cb, progress_percent) -> bool:
        """单次上传尝试：auto 模式下流式直传失败会回退本地打包后上传。

        `_prefer_tempfile` 一旦被置位（流式在已写出数据后失败），auto 模式就不再
        走流式 —— 流式的半成品接不上，反复用它等于每次都从零开始。改走本地打包后，
        半成品与本地归档形成明确的偏移关系，重试就能续传。

        但这个切换**要先确认本地放得下归档**：auto 之所以优先流式，正是因为有些
        设备的临时目录很小（tmpfs）。为了"换来续传"把一次可重试的失败变成
        "本地空间不足"的硬失败，是拿确定的损失换不确定的收益 —— 不划算。
        """
        go_tempfile = False
        if self.transfer_mode == "auto" and self._prefer_tempfile:
            if self._tempfile_switch_ok():
                go_tempfile = True
                if progress_cb:
                    progress_cb("上次流式直传中断且无法续传，改用本地打包（可续传）...")
            elif progress_cb:
                progress_cb("本地放不下归档，继续用流式直传（本次不可续传）")

        use_stream = self.transfer_mode == "stream" or (
            self.transfer_mode == "auto" and not go_tempfile)
        if use_stream:
            try:
                self._stream_pack_upload(
                    folder, entries, total_size, remote_path,
                    running_cb, progress_cb, progress_percent
                )
                return True
            except UploadCancelledError:
                raise
            except StreamUnsupportedError as exc:
                if self.transfer_mode == "stream":
                    self._fail(ErrorCode.REMOTE_COMMAND, f"流式直传不可用：{exc}")
                    return False
                logger.warning(f"流式直传不可用（{exc}），回退为本地打包后上传")
                if progress_cb:
                    progress_cb("远端不支持流式直传，改用本地打包后上传...")
        return self._tempfile_pack_upload(
            folder, entries, total_size, remote_path,
            running_cb, progress_cb, progress_percent
        )

    def _tempfile_switch_ok(self) -> bool:
        """auto 模式下从流式切到本地打包是否可行（本地放不放得下归档）。

        这个判断是必要的，不是保守：auto 优先流式的**根本原因**就是有些设备的
        临时目录很小（tmpfs 只有几十 MB）。为了换来续传而切路径，若本地放不下，
        就会把一次"可重试的中断"变成"本地空间不足"的硬失败 —— 亏了。
        放不下就继续用流式：慢，但能成。
        """
        try:
            self._ensure_local_space(self._session_est_size)
            return True
        except AppError as exc:
            logger.warning(f"本地放不下归档（{exc.message}），继续用流式直传（不可续传）")
            return False

    # ---------- 两种传输方式 ----------
    def _stream_pack_upload(self, folder: Path, entries, total_size: int, remote_path: str,
                            running_cb, progress_cb, progress_percent) -> int:
        """边打包边写入远端（不落本地临时盘）。

        通过 SFTP 通道执行 `cat > 远端文件`，把打包器直接接到通道 stdin：
        数据是流水线式的，本地零临时空间，也不需要等整包打完再传。
        远端若不支持 exec（SFTP-only / 无 cat），会抛 StreamUnsupportedError，
        由 auto 模式回退到本地打包后上传。
        返回写出的归档字节数。

        注：ChannelFile.close() 只会 flush，不会给远端发 EOF，
        因此必须先 shutdown_write() 再 recv_exit_status()，否则会永久阻塞。
        """
        fmt = self.transfer_format
        compress = self.transfer_compression == "deflate"
        estimate = self._estimate_archive_size(total_size, len(entries), fmt)
        reporter = ProgressReporter(estimate, "正在打包直传", 0, 100, progress_cb, progress_percent)

        transport = None
        sftp = None
        channel = None
        sent = 0
        local_digest = None
        try:
            transport, sftp = self._connect_sftp()
            self._ensure_remote_dir(sftp, os.path.dirname(remote_path))
            channel = transport.open_session()
            channel.exec_command("cat > " + shlex.quote(remote_path))
            raw = channel.makefile("wb", 0)
            writer = ProgressWriter(raw)
            try:
                local_digest = self._pack_folder(
                    folder, writer, fmt, compress, entries, running_cb,
                    reporter.update, collect_digest=self.verify_content)
                writer.flush()
            except BaseException:
                # 无论成功失败都记录已写出的字节数：为 0 才可能是不支持 exec
                sent = writer.written
                raise
            else:
                sent = writer.written
            finally:
                # ChannelFile.close() 只 flush、不发 EOF，远端 EOF 由下面的 shutdown_write 发送
                self._close_quietly(raw)
            channel.shutdown_write()
            stderr = channel.makefile_stderr("r", -1).read()
            code = channel.recv_exit_status()
            stderr_text = (stderr or b"").decode(errors="replace").strip()
            if code != 0:
                if code == 127 and sent == 0:
                    raise StreamUnsupportedError(f"远端没有可用 shell 命令（退出码 127）：{stderr_text}")
                raise RuntimeError(f"远端写入失败（退出码 {code}）：{stderr_text or '无错误输出'}")
            remote_size = sftp.stat(remote_path).st_size
            if remote_size != sent:
                # 用带码异常：这是"校验不一致"，不是"传输中断"。分类准确了，
                # 重试策略与界面提示才能对上（否则会被归成网络问题）。
                raise AppError(
                    ErrorCode.VERIFY_FAILED,
                    f"远端文件大小校验不一致：已发送 {_mb(sent)}，远端 {_mb(remote_size)}"
                )
            if local_digest is not None:
                # 大小相同之后仍要确认内容：大小不变的中断/位翻转是这个闸门挡不住的
                try:
                    remote_digest = sha256_sftp(sftp, remote_path, remote_size)
                except Exception as exc:
                    raise AppError(
                        ErrorCode.VERIFY_FAILED,
                        f"内容校验无法完成：{type(exc).__name__}: {exc}"
                    ) from exc
                if local_digest != remote_digest:
                    raise AppError(
                        ErrorCode.VERIFY_FAILED,
                        f"远端内容校验不一致：本地 {local_digest[:16]}…，"
                        f"远端 {remote_digest[:16]}…"
                    )
                logger.info(f"流式内容校验通过: {remote_path}")
            reporter.finish(f"上传完成：{_mb(sent)}")
            logger.info(f"流式直传成功: {remote_path}（{_mb(sent)}）")
            return sent
        except UploadCancelledError:
            logger.info("流式直传被取消")
            self._close_quietly(channel)
            self._remove_remote_quietly(sftp, remote_path)
            raise
        except StreamUnsupportedError:
            self._close_quietly(channel)
            if sent:
                self._remove_remote_quietly(sftp, remote_path)
            raise
        except AppError:
            # 已经写出数据后的带码失败（如校验不一致）：收尾照做，但**码要原样保留**，
            # 不能被下面那个兜底分支包成"流式直传中断"
            self._close_quietly(channel)
            if sent:
                self._remove_remote_quietly(sftp, remote_path)
                self._prefer_tempfile = True
            raise
        except Exception as exc:
            self._close_quietly(channel)
            if sent:
                # 已经写了数据说明通道本身可用，属于真实错误（网络/远端空间等），不回退。
                # 半成品必须删：流式那条字节流无法续传，留着只会在下次被当成"可续的起点"。
                self._remove_remote_quietly(sftp, remote_path)
                # 记住"流式在已写出数据后失败过"：后续尝试改走本地打包，才有续传可言
                self._prefer_tempfile = True
                raise RuntimeError(f"流式直传中断：{exc}") from exc
            if isinstance(exc, (paramiko.AuthenticationException, paramiko.BadHostKeyException,
                                paramiko.ssh_exception.NoValidConnectionsError)):
                # 连不上/认证失败属于配置问题，回退本地打包也救不了，直接报错
                raise RuntimeError(f"连接远端失败：{exc}") from exc
            if isinstance(exc, (paramiko.SSHException, EOFError, OSError)):
                # 一个字都没写出去：多半是远端不允许 exec（无 cat / SFTP-only 服务）
                raise StreamUnsupportedError(f"{type(exc).__name__}: {exc}") from exc
            raise
        finally:
            self._close_quietly(channel)
            if transport is not None:
                transport.close()

    def _tempfile_pack_upload(self, folder: Path, entries, total_size: int, remote_path: str,
                              running_cb, progress_cb, progress_percent) -> bool:
        """先打包到本地临时目录，再用 SFTP 上传（打包 0~50%，上传 50~100%）。

        与流式直传的区别不只是"占不占本地盘"：本地有一份完整归档，才谈得上
        **断点续传** —— 偏移语义明确（远端那份就是这个文件的字节前缀）。
        所以归档在整个上传会话内保留（`_session_archive`），重试时直接复用、
        跳过打包；删归档的责任交给了会话结束（见 `upload_bag_folder`）。
        """
        fmt = self.transfer_format
        compress = self.transfer_compression == "deflate"
        archive = self._session_archive or (self.temp_dir / self._archive_name(folder))

        if self._session_archive is not None and archive.exists():
            packed = archive.stat().st_size
            logger.info(f"复用本次会话已打好的归档: {archive}（{_mb(packed)}），跳过重新打包")
            if progress_cb:
                progress_cb(f"复用已打好的本地归档（{_mb(packed)}），跳过重新打包")
            if progress_percent:
                progress_percent(50)
        else:
            self._ensure_local_space(self._estimate_archive_size(total_size, len(entries), fmt))
            reporter = ProgressReporter(total_size, "正在打包", 0, 50, progress_cb, progress_percent)
            with archive.open("wb") as fp:
                self._pack_folder(folder, fp, fmt, compress, entries, running_cb, reporter.update)
            packed = archive.stat().st_size
            reporter.finish(f"打包完成：{_mb(packed)}")
            logger.info(f"打包完成: {archive}（{_mb(packed)}）")
            self._session_archive = archive

        ok = self._upload_file(archive, remote_path, running_cb, progress_cb, progress_percent,
                               resume=self.upload_resume)
        if not ok:
            if running_cb is not None and not running_cb():
                # 用户取消：必须抛出，否则外层会把它当成普通失败而自动重试
                raise UploadCancelledError("用户取消上传")
            # 非取消的失败**不在这里清理远端**：远端半成品正是下次续传的起点。
            # 「不留坏包」由会话结束统一保证（失败收尾会删干净），语义没丢。
        return ok

    def cancel_upload(self):
        """取消当前上传"""
        self.upload_cancelled = True


    # ==================== 远程目录浏览 ====================
    def _transport_auth(self) -> dict:
        """SFTP 连接凭据：密码缺失时立刻给出可操作的报错。

        凭据只从环境变量 `DATA_COLLECTOR_REMOTE_PASSWORD` 或
        `configs/app_config.yaml` 的 `remote_password` 读取，源码里不留默认密码。
        """
        if not self.password:
            raise RemoteAuthError(REMOTE_PASSWORD_HINT)
        return {"username": self.remote_user, "password": self.password}

    def _connect_sftp(self):
        """创建独立的 SFTP 连接，调用方负责关闭。"""
        auth = self._transport_auth()
        transport = paramiko.Transport((self.remote_host, self.remote_port))
        try:
            transport.connect(**auth)
            sftp = paramiko.SFTPClient.from_transport(transport)
            return transport, sftp
        except Exception:
            transport.close()
            raise

    @staticmethod
    def _normalize_remote_path(path: str) -> str:
        """统一远程路径格式，并消解 . 与 ..（越界校验成立的前提）。

        必须真的消解 `..`：`_check_remote_path` 用 startswith 判断是否越界，
        而 `<base>/../../etc` 这种写法在做前缀匹配时是「合法」的 —— 不归一化
        那道校验就形同虚设。
        """
        path = (path or "/").replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        path = posixpath.normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def _check_remote_path(self, path: str) -> str:
        """限制远程目录浏览/删除等操作在 remote_base 下。"""
        path = self._normalize_remote_path(path)
        base = self._normalize_remote_path(self.remote_base)
        if path != base and not path.startswith(base + "/"):
            raise ValueError(f"远程路径超出允许范围: {path}")
        return path

    def list_remote_dir(self, remote_path: str = None) -> List[dict]:
        """列出远程目录，返回 name/path/is_dir/size/mtime。"""
        remote_path = self._check_remote_path(remote_path or self.remote_base)
        transport, sftp = self._connect_sftp()
        try:
            result = []
            for attr in sftp.listdir_attr(remote_path):
                name = attr.filename
                full_path = self._normalize_remote_path(remote_path + "/" + name)
                is_dir = bool(attr.st_mode & 0o040000)
                result.append({
                    "name": name,
                    "path": full_path,
                    "is_dir": is_dir,
                    "size": int(getattr(attr, "st_size", 0) or 0),
                    "mtime": int(getattr(attr, "st_mtime", 0) or 0),
                })
            result.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return result
        finally:
            sftp.close()
            transport.close()

    def get_remote_file_info(self, remote_path: str) -> dict:
        """获取远程文件/目录的详细信息（RemoteDirectoryDialog 用）。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            attr = sftp.stat(remote_path)
            return {
                "name": os.path.basename(remote_path.rstrip("/")) or remote_path,
                "path": remote_path,
                "is_dir": bool(attr.st_mode & 0o040000),
                "size": int(getattr(attr, "st_size", 0) or 0),
                "mtime": int(getattr(attr, "st_mtime", 0) or 0),
            }
        finally:
            sftp.close()
            transport.close()

    def search_remote(self, remote_path: str, keyword: str,
                      running_cb: Callable[[], bool] = None,
                      progress_cb: Callable[[int, int], None] = None) -> List[dict]:
        """在 remote_path 下递归搜索名称包含 keyword 的项（忽略大小写）。

        running_cb: 返回 False 时中止搜索（抛 UploadCancelledError，与取消上传统一处理）。
        progress_cb: 回调 (已扫描数量, 已命中数量)。
        """
        remote_path = self._check_remote_path(remote_path or self.remote_base)
        keyword = (keyword or "").lower()
        results: List[dict] = []
        scanned = 0

        transport, sftp = self._connect_sftp()
        try:
            stack = [remote_path]
            while stack:
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消搜索")
                current = stack.pop()
                try:
                    entries = sftp.listdir_attr(current)
                except IOError as e:
                    logger.warning(f"跳过无法访问的目录 {current}: {e}")
                    continue
                for attr in entries:
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消搜索")
                    scanned += 1
                    name = attr.filename
                    full_path = self._normalize_remote_path(current + "/" + name)
                    is_dir = bool(attr.st_mode & 0o040000)
                    if keyword in name.lower():
                        results.append({
                            "name": name,
                            "path": full_path,
                            "is_dir": is_dir,
                            "size": int(getattr(attr, "st_size", 0) or 0),
                            "mtime": int(getattr(attr, "st_mtime", 0) or 0),
                        })
                    if is_dir:
                        stack.append(full_path)
                    if progress_cb:
                        progress_cb(scanned, len(results))
            results.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            logger.info(f"搜索完成: 关键词={keyword!r} 扫描 {scanned} 项, 命中 {len(results)} 项")
            return results
        finally:
            sftp.close()
            transport.close()

    def upload_local_file(self, local_path: Path, remote_path: str,
                          running_cb: Callable[[], bool] = None,
                          progress_cb: Callable[[int, int], None] = None) -> bool:
        """上传单个本地文件到远程（RemoteDirectoryDialog 的“上传文件”用）。

        progress_cb: 回调 (已完成字节, 总字节)；running_cb 返回 False 时中止并删除残缺文件。
        """
        local_path = Path(local_path)
        if not local_path.is_file():
            logger.error(f"本地文件不存在: {local_path}")
            return False
        try:
            remote_path = self._check_remote_path(remote_path)
        except ValueError as e:
            logger.error(f"远程路径非法: {e}")
            return False

        transport = paramiko.Transport((self.remote_host, self.remote_port))
        sftp = None
        try:
            transport.connect(**self._transport_auth())
            sftp = paramiko.SFTPClient.from_transport(transport)
            self._ensure_remote_dir(sftp, os.path.dirname(remote_path))

            def callback(done, total):
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消上传")
                if progress_cb:
                    progress_cb(int(done), int(total))

            sftp.put(str(local_path), remote_path, callback=callback)
            logger.info(f"上传成功: {remote_path}")
            return True
        except UploadCancelledError:
            logger.info(f"上传已取消: {remote_path}")
            try:
                if sftp is not None:
                    sftp.remove(remote_path)
                    logger.info(f"已删除未完成上传文件: {remote_path}")
            except Exception as e:
                logger.warning(f"清理远程文件失败: {e}")
            return False
        except Exception as e:
            logger.error(f"上传失败: {e}")
            return False
        finally:
            transport.close()


    def create_remote_dir(self, remote_path: str) -> bool:
        """递归创建远程目录。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            self._ensure_remote_dir(sftp, remote_path)
            return True
        finally:
            sftp.close()
            transport.close()

    def _remove_remote_recursive(self, sftp, remote_path: str):
        for attr in sftp.listdir_attr(remote_path):
            child = self._normalize_remote_path(remote_path + "/" + attr.filename)
            if attr.st_mode & 0o040000:
                self._remove_remote_recursive(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(remote_path)

    @staticmethod
    def _channel_text(raw) -> str:
        """exec 通道的输出在不同 paramiko 版本下可能是 bytes，统一解码成 str。

        历史上这里直接按 str 处理，导致含中文的远端路径（remote_base 就带中文）
        解析 df 输出时抛 TypeError，空间查询静默失效。
        """
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="replace")
        return raw or ""

    def get_remote_disk_usage(self, remote_path: str = None) -> dict:
        """通过远程 df 获取文件系统空间，兼容不支持 SFTP statvfs 的服务端。"""
        remote_path = self._check_remote_path(remote_path or self.remote_base)
        transport = None
        channel = None
        try:
            transport, _sftp = self._connect_sftp()
            command = "df -Pk -- " + shlex.quote(remote_path)
            channel = transport.open_session()
            channel.exec_command(command)
            stdout = self._channel_text(channel.makefile("r", -1).read())
            stderr = self._channel_text(channel.makefile_stderr("r", -1).read())
            status = channel.recv_exit_status()
            if status != 0:
                raise RuntimeError(stderr.strip() or f"远程 df 执行失败，退出码：{status}")

            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            if len(lines) < 2:
                raise RuntimeError(f"无法解析远程 df 输出：{stdout!r}")
            parts = lines[-1].split()
            capacity_index = next(
                (i for i, p in enumerate(parts)
                 if p.endswith('%') and p[:-1].isdigit()),
                None
            )
            if capacity_index is None or capacity_index < 3:
                raise RuntimeError(f"无法解析远程 df 输出：{lines[-1]!r}")

            total = int(parts[capacity_index - 3]) * 1024
            used = int(parts[capacity_index - 2]) * 1024
            available = int(parts[capacity_index - 1]) * 1024
            return {
                'total': total,
                'used': used,
                'free': available,
                'available': available,
                'block_size': 1024,
            }
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass

    def get_remote_tree_size(self, remote_path: str,
                             running_cb=None, progress_cb=None) -> int:
        """递归统计远程目录下所有文件的总大小。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            total_size = 0
            file_count = 0
            stack = [remote_path]

            while stack:
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消目录统计")

                current = stack.pop()
                for attr in sftp.listdir_attr(current):
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消目录统计")

                    child = self._normalize_remote_path(
                        current + "/" + attr.filename
                    )
                    is_dir = bool(attr.st_mode & 0o040000)

                    if is_dir:
                        stack.append(child)
                    else:
                        total_size += int(getattr(attr, "st_size", 0) or 0)
                        file_count += 1

                    if progress_cb:
                        progress_cb(total_size, file_count)

            return total_size
        finally:
            sftp.close()
            transport.close()

    def delete_remote_path(self, remote_path: str) -> bool:
        """删除远程文件或目录。禁止删除 remote_base 本身。"""
        remote_path = self._check_remote_path(remote_path)
        base = self._normalize_remote_path(self.remote_base)
        if remote_path == base:
            raise ValueError("不能删除远程根目录")
        transport, sftp = self._connect_sftp()
        try:
            attr = sftp.stat(remote_path)
            if attr.st_mode & 0o040000:
                self._remove_remote_recursive(sftp, remote_path)
            else:
                sftp.remove(remote_path)
            return True
        finally:
            sftp.close()
            transport.close()

    def download_remote_file(self, remote_path: str, local_path: Path,
                             progress_cb: Callable[[int, int], None] = None) -> bool:
        """下载远程文件到本地。"""
        remote_path = self._check_remote_path(remote_path)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        transport, sftp = self._connect_sftp()
        try:
            sftp.get(str(remote_path), str(local_path), callback=progress_cb)
            return True
        finally:
            sftp.close()
            transport.close()


class LogFetcher:
    #: 抓回来的机器人日志落在**上位机日志目录下的这个子目录**里。
    #:
    #: 为什么要分开：抓取是把机器人侧 `log_dir` 里的文件原样复制过来，文件名也叫
    #: `wheeled_robot_*.log` / `startup.log` 之类，与上位机自己的日志
    #: （`ui_error.log` / `teleop_*.log` / `robot_monitor.log`）混在同一个目录里时，
    #: 两种日志互相淹没 —— 抓两次就分不清"哪个是本机现场、哪个是机器人拉回来的"，
    #: 清理时也容易连带删掉本机排查用的那份。
    FETCH_SUBDIR = "fetched"

    def __init__(self, local_log_dir: Path, config=None):
        # 源日志目录可由 app_config.yaml 的 log_dir 指定
        self.log_dir = config.get_log_dir() if config is not None else DEFAULT_LOG_DIR
        #: 上位机**自己**的日志目录（ui_error / teleop_* / robot_monitor / 主题缓存都在这里）
        self.local_log_dir = local_log_dir
        self.local_log_dir.mkdir(parents=True, exist_ok=True)
        #: 抓取回来的机器人日志（与上面那份分开，见 FETCH_SUBDIR 的说明）
        self.fetch_dir = local_log_dir / self.FETCH_SUBDIR
        self.fetch_dir.mkdir(parents=True, exist_ok=True)

    def get_log_files(self) -> List[Path]:
        if not self.log_dir.exists():
            return []
        files = [f for f in self.log_dir.iterdir() if f.is_file()]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return files

    def get_latest_log_file(self) -> Optional[Path]:
        files = self.get_log_files()
        return files[0] if files else None

    def fetch_specific_log(self, file_path: Path) -> bool:
        if not file_path.exists() or not file_path.is_file():
            logger.error(f"日志文件不存在: {file_path}")
            return False
        dest = self.fetch_dir / file_path.name
        try:
            shutil.copy2(file_path, dest)
            logger.info(f"日志已复制到: {dest}")
            return True
        except Exception as e:
            logger.error(f"复制日志失败: {e}")
            return False

    def fetch_latest_log(self) -> bool:
        latest = self.get_latest_log_file()
        if not latest:
            return False
        dest = self.fetch_dir / latest.name
        try:
            shutil.copy2(latest, dest)
            logger.info(f"日志已复制到: {dest}")
            return True
        except Exception as e:
            logger.error(f"复制日志失败: {e}")
            return False
