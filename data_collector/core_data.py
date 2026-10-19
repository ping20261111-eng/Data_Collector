import os
import posixpath
import shlex
import shutil
import tarfile
import tempfile
import time
import zipfile
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Callable, Tuple

import paramiko

from .core import (logger, DEFAULT_BAG_DIR, DEFAULT_LOG_DIR, DEFAULT_REMOTE_HOST,
                   DEFAULT_REMOTE_PORT, DEFAULT_REMOTE_USER, DEFAULT_REMOTE_BASE,
                   is_within)
from .errors import AppError, ErrorCode, describe, is_retryable
from .manifest import (
    MANIFEST_NAME,
    STATUS_COMPLETE,
    STATUS_INTERRUPTED,
    BagManifest,
)
from .verify import sha256_file, sha256_sftp, write_sha256_sidecar

#: 磁盘导出目标。**延迟导入**（不在模块顶层）：
#: `disk_export` 会在 import 期读一次 `get_export_settings`（拿挂载点/盘标识），
#: 顶��导入会把「导入 core_data」与「读配置」两件事绑死 ——
#: 单元测试里只想用 `_pack_folder` 时也会被迫构造一份配置。
#: 真正的使用点在 `DataUploader._export_to_disk`，那里才需要配置。
#: v6.4.17 起，「序列号 / 采集日期 / 盘上分层」这套口径也统一放在那里
#: （录制直接写盘与录完导出必须给出**同一套**落点），所以 `DataUploader` 的
#: 三个小助手改为委托调用。仍然是延迟导入 —— 与上面同一个理由。
import data_collector.disk_export as disk_export


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


class UploadBatchQueue:
    """批量上传的**队列状态机**（纯逻辑，不碰 Qt）。

    抽出来的理由：批量上传最容易错的地方全是**计数与顺序** ——
    "第几个 / 还剩几个 / 哪一条的失败原因" —— 而这部分埋在界面代码里就只能靠
    手工点。这里做成纯类，单测能直接跑完"三条里第二条失败"这种路径。

    约定：
    * `next()` 只在没有在传的那一条时给出下一条（**不会并行**）；
    * `record()` 收尾当前这一条；失败也照常记，**不抛不中断**（批量要传完再汇总）；
    * `cancel()` 清空**未开始**的部分，已经传完的结果保留（汇总要如实报）；
    * `percent()` 把单条进度折算成整批进度（按条数等权）。
    """

    def __init__(self, items):
        self._pending = list(items or [])
        self._current = None
        self.results = []            # [(item, ok, message)]
        self.cancelled = False

    @property
    def current(self):
        return self._current

    @property
    def pending(self):
        """还没开始的（不含正在传的那一条）。"""
        return list(self._pending)

    @property
    def total(self) -> int:
        """这一批的总条数（含已传完、正在传、未开始）。"""
        return len(self.results) + len(self._pending) + (1 if self._current is not None else 0)

    @property
    def done(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> list:
        return [row for row in self.results if not row[1]]

    @property
    def succeeded(self) -> list:
        return [row for row in self.results if row[1]]

    def next(self):
        """取下一个待传项；没有就返回 None（调用方据此收尾）。"""
        if self._current is not None:
            return None              # 上一条还没 record，不并行
        if self.cancelled or not self._pending:
            return None
        self._current = self._pending.pop(0)
        return self._current

    def record(self, ok: bool, message: str = "") -> bool:
        """收尾当前这一条；返回是否真的记下了（没有在传的条目时返回 False）。"""
        if self._current is None:
            return False
        self.results.append((self._current, bool(ok), str(message or "")))
        self._current = None
        return True

    def cancel(self) -> None:
        """取消整批：清空未开始的，保留已完成的结果。"""
        self.cancelled = True
        self._pending.clear()

    def percent(self, current_percent) -> int:
        """整批进度百分比（0~99，100 由收尾时给）。

        按条数等权：按字节要先量遍所有目录（慢、且读不到的目录会让整批卡住）。
        """
        total = max(1, self.total)
        try:
            value = max(0, min(100, int(current_percent)))
        except (TypeError, ValueError):
            value = 0
        return min(99, int((self.done + value / 100.0) * 100 / total))

    def summary(self) -> dict:
        """汇总用的计数：成功 / 失败 / 未开始。"""
        return {
            "total": self.total,
            "done": self.done,
            "ok": len(self.succeeded),
            "failed": len(self.failed),
            "unstarted": len(self._pending),
            "cancelled": self.cancelled,
        }



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
        #: **本次会话**起录时用的落盘根目录（快照）。健康巡检 / 清单 / 可用空间
        #: 都读它而不是实时读配置 —— 免得"起录之后落盘位置被改"导致巡检去看
        #: 另一个目录，把正常录制误报成「没有新文件落盘」。开新一次录制时覆盖。
        self._session_bag_dir: Optional[Path] = None

    def session_bag_dir(self) -> Path:
        """本次会话的落盘根目录：起录时快照优先，否则实时取当前值。

        实时兜底是给"没走过 start（例如只做预检）"的调用方用的。
        """
        if self._session_bag_dir is not None:
            return self._session_bag_dir
        return Path(self.config.get_bag_dir()) if self.config is not None else DEFAULT_BAG_DIR

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
        # 上一次录制若已自行退出（脚本秒退、被外部杀掉、机器重启残留），current_proc
        # 里还留着那个**已死的** Popen。不先收干净就开新的一次，会把上一次的
        # _started_at / _manifest_dir 带过来：录制起点的基准时间被污染成旧值，
        # manifest 也会写到上一个 bag 目录里 —— 现场表现就是"第二次录不停"
        # （stop 时按旧 manifest 收尾，界面状态与实际进程对不上）。
        self._reap_dead_proc("启动新录制前")
        # 快照本次的落盘目录。放在 launch **之前**：录制脚本的 `BAG_DIR` 环境变量
        # 是在 launch 里组装的，而随后所有"该看哪个目录"的判断（产出巡检、清单、
        # 可用空间）都以这个快照为准。开新一次录制时自动覆盖。
        self._session_bag_dir = (
            Path(self.config.get_bag_dir()) if self.config is not None else None)
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

    def _reap_dead_proc(self, why: str = "") -> bool:
        """回收**已经退出**的录制进程句柄，并清掉本次录制的残留状态。

        为什么必须做：`current_proc` 是一个 Popen 引用，脚本自行退出后它并不会
        消失，只是 `poll()` 开始返回退出码。不清的后果有三处一起坏：

        1. `is_active()` 靠 `poll() is None` 判断，看起来没问题；但 `_started_at`
           还停在上一次录制的中点，下一次 `health()` 会拿它当"何时算新数据"的
           基准 —— 旧基线比新录制早，于是**每个文件都算"新数据"**，
           看门狗形同虚设，"进程活着但没落盘"这种事故再也拦不住。
        2. `_manifest_dir` 指向上一次的 bag 目录，第二次录制的 manifest 会写错地方。
        3. 进程注册表里挂着这个死进程的 Popen，`terminate()` 的兜底逻辑会对一个
           已经结束的 pid 发信号，日志里出现看不懂的报错。

        返回是否真的回收了东西（便于测试与日志）。
        """
        proc = self.current_proc
        if proc is None or proc.poll() is None:
            return False
        code = proc.poll()
        logger.info(f"回收已退出的录制进程（PID={getattr(proc, 'pid', '?')} "
                    f"退出码={code}）{('：' + why) if why else ''}")
        # 从注册表里摘掉，别让后续的按名终止/巡检一直看见它
        pm = getattr(self.launcher, "pm", None)
        if pm is not None:
            try:
                pm.unregister(proc)
            except Exception as exc:                    # noqa: BLE001
                logger.debug(f"从进程注册表摘除录制进程失败（可忽略）：{exc}")
        self.current_proc = None
        self._started_at = None
        self._manifest_dir = None
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
            bag_root = self.session_bag_dir()
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
            produced = self._files_since(self.session_bag_dir(),
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
        """停止录制。返回是否**真的**停掉了一个正在跑的录制。

        进程已经自行退出时也要把状态收干净：否则 `_started_at` 与 `_manifest_dir`
        会一直挂着上一场的值，下一次录制把它们当成自己的基线（见 `_reap_dead_proc`）。
        这里返回 False 是如实汇报"没有可停的录制"，界面据此提示即可。
        """
        if self.current_proc and self.current_proc.poll() is None:
            from .core import terminate
            proc = self.current_proc
            terminate(proc)
            pm = getattr(self.launcher, "pm", None)
            if pm is not None:
                try:
                    pm.unregister(proc)
                except Exception as exc:                # noqa: BLE001
                    logger.debug(f"从进程注册表摘除录制进程失败（可忽略）：{exc}")
            self.current_proc = None
            self.finalize_manifest()
            self._started_at = None
            self._manifest_dir = None
            return True
        # 走到这里说明没有活着的录制：若有残留句柄一并回收，避免污染下一次
        self._reap_dead_proc("停止时发现进程已退出")
        return False

    def is_active(self) -> bool:
        return self.current_proc is not None and self.current_proc.poll() is None

    #: bag 目录可用空间低于这个值就算"快满了"（字节）。1 GiB 的判据是"够写一个
    #: 小包、但不够写一场完整采集" —— 真要拦住起录得知道采集时长，那是现场知识，
    #: 这里不去猜；只负责把"已经满到写不进任何东西"这件事说清楚。
    DISK_LOW_BYTES = 1024 ** 3

    def disk_free(self) -> Optional[int]:
        """bag 目录所在分区的可用字节数；读不到返回 None（不猜、不当成 0）。"""
        try:
            bag_dir = self.session_bag_dir()
            # 分区可能还没建出来（第一次录制前），那就近找一层存在的祖先
            probe = bag_dir
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            return shutil.disk_usage(str(probe)).free
        except Exception as exc:                       # noqa: BLE001
            logger.debug(f"读 bag 目录可用空间失败（按未知处理）：{exc}")
            return None

    def disk_full(self) -> str:
        """磁盘已经满到写不进东西时返回一句人话，正常时返回空串。

        现场故障（v6.4.12）：机器人磁盘满 → `ros2 bag record` 写不进 bag 直接退出，
        而脚本输出不落盘、退出码也没人看，界面只显示"录制进程已退出"。机器人那边
        自己的日志里其实有 `[Errno 28] No space left on device`，但没人会去查。
        这里把最常见、也最能当场补救的那一类原因直接说出来。
        """
        free = self.disk_free()
        if free is None:
            return ""
        if free <= 0:
            return "录制盘已满（bag 目录所在分区可用空间为 0）"
        if free < self.DISK_LOW_BYTES:
            return (f"录制盘可用空间只剩 {free / 1024 ** 2:.0f} MB"
                    f"（bag 目录所在分区）")
        return ""

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

        bag_dir = self.session_bag_dir()
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


@dataclass(frozen=True)
class BagCandidate:
    """一个可统计的采集数据（rosbag 目录）。

    字段刻意只留"选它需要知道的"：路径、录制时间、体积、话题文件数、采集类型。
    界面上列表里那一行就是这些字段 —— 操作员靠**时间与大小**判断该统计哪一次，
    靠**类型**知道这次是 vla / bag / cmd 哪种录制。
    """

    path: Path
    captured_at: datetime
    size_bytes: int = 0
    topic_files: int = 0
    kind: str = ""

    #: 目录名里 `_时间戳` 之前的部分就是"这次采集叫什么"。取不到就用目录名。
    name: str = ""

    def size_text(self) -> str:
        # 用局部变量逐级折算，**不要就地改 self.size_bytes** —— 这个类是
        # frozen dataclass，改字段会抛 FrozenInstanceError，而且即使不 frozen
        # 也会让"读一次列表、体积就变一次"这种诡异行为发生。
        if self.size_bytes <= 0:
            return "—"
        number = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if number < 1024 or unit == "TB":
                return f"{number:.0f}B" if unit == "B" else f"{number:.1f}{unit}"
            number /= 1024.0
        return f"{number:.1f}TB"

    def time_text(self) -> str:
        """录制时间：今天的只写时分（列表窄，全写日期会挤掉体积那列）。"""
        today = datetime.now().date()
        if self.captured_at.date() == today:
            return self.captured_at.strftime("今天 %H:%M")
        if self.captured_at.date() == today - timedelta(days=1):
            return self.captured_at.strftime("昨天 %H:%M")
        return self.captured_at.strftime("%m-%d %H:%M")

    def kind_text(self) -> str:
        return self.kind or "bag"


def describe_bag(folder: Path) -> BagCandidate:
    """把一个 bag 目录读成一条候选信息（时间 / 体积 / 话题文件数 / 类型）。

    **体积与文件数读不到就留 0**，不抛异常：这个函数是在"点开一个选择对话框"
    这条交互路径上跑的，一个坏目录不该让整个列表打不开。列表里显示"—"即可。
    """
    path = Path(folder)
    try:
        captured = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        captured = datetime.now()
    size, topic_files = 0, 0
    try:
        for entry in path.iterdir():
            if entry.is_file():
                try:
                    size += entry.stat().st_size
                except OSError:
                    continue
                if entry.suffix == ".mcap":
                    topic_files += 1
    except OSError:
        pass
    name, kind = path.name, ""
    # 录制脚本把类型写在名字里（vla_2026…/ bag_…/ cmd_…），带上前缀才好认；
    # 老目录（`labeltest_…`、`rosbag2_…`）认不出来就不写类型，不硬猜。
    for candidate in ("vla", "bag", "cmd"):
        if path.name.lower().startswith(candidate + "_"):
            name, kind = path.name[len(candidate) + 1:], candidate
            break
    return BagCandidate(path=path, captured_at=captured, size_bytes=size,
                        topic_files=topic_files, kind=kind, name=name)


class DataUploader:
    def __init__(self, config=None):
        """config 为 ConfigManager 时从 app_config.yaml 读取远程主机/目录与密码；

        不传 config 时沿用历史默认值（主机/目录），但密码不会内置默认值：
        必须由调用方通过 config 或 `DATA_COLLECTOR_REMOTE_PASSWORD` 提供。
        """
        settings = config.get_remote_settings() if config is not None else {}
        upload = config.get_upload_settings() if config is not None else {}
        self.config = config
        #: 落盘目录的**实例级覆盖**（None = 跟随 config）。做成 property 而不是在
        #: `__init__` 拷一份快照：本次会话可能把录制切到移动硬盘（config 上的会话级
        #: 覆盖随之变化），而快照会让「数据列表 / 回放列表 / 上传源」停在旧目录上 ——
        #: 现场表现就是"刚录的数据在界面上看不到"。
        self._bag_dir_override: Optional[Path] = None
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

    # ---------- 落盘目录（跟随配置的会话级选择） ----------

    @property
    def bag_dir(self) -> Path:
        """**当前**数据目录：实例覆盖 > `ConfigManager.get_bag_dir()`（含会话级选择）。

        不缓存成属性字段，是因为它会随「录制设置」里选的保存位置而变 ——
        缓存就会让"刚录的数据在界面上看不到"（列表还盯着旧目录）。
        """
        if self._bag_dir_override is not None:
            return self._bag_dir_override
        if self.config is not None:
            return Path(self.config.get_bag_dir())
        return DEFAULT_BAG_DIR

    @bag_dir.setter
    def bag_dir(self, value) -> None:
        """允许直接指定（测试与"手动挑一个目录"的用法都靠它）。"""
        self._bag_dir_override = Path(value)

    def get_bag_folders(self) -> List[Path]:
        if not self.bag_dir.exists():
            return []
        subdirs = [d for d in self.bag_dir.iterdir() if d.is_dir()]
        subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        return subdirs

    # ---------- 采集数据候选（给「采集统计」等"选一个 bag"用） ----------

    def looks_like_bag(self, folder: Path) -> bool:
        """这个目录是不是一个 rosbag 采集目录。

        判据是**目录里有 bag 自己会写的文件**：`metadata.yaml`（rosbag2 的清单）
        或 `*.mcap`（数据本体）。不按目录名判断 —— 名字是操作员随手写的
        （`11111_2026…`、`labeltest3_…`、`rosbag2_…` 都有），按名字判会把一堆
        乱七八糟的目录认成采集数据。
        """
        path = Path(folder)
        try:
            for entry in path.iterdir():
                if entry.name == "metadata.yaml" or entry.suffix == ".mcap":
                    return True
        except OSError:
            return False
        return False

    def scan_bag_candidates(self, root: Optional[Path] = None,
                            limit: int = 200) -> List["BagCandidate"]:
        """列出可统计的采集数据，按**录制时间倒序**（新的在最前）。

        为什么要有这个而不是让操作员在系统文件框里一层层点：
        - 系统框里只有目录名，分不清哪个是 bag、哪个是别的目录；
        - 现场会攒几十上百个 `名字_时间戳`，没法按时间/大小判断要统计哪个；
        - 选到非 bag 目录**当场就该拦**，而不是等脚本跑起来才报错。

        只扫一层（`root` 的直接子目录）：bag 是平铺的，深层扫描在几百个目录的
        机器上要几百毫秒到几秒，而这个对话框是每次点「采集统计」都开的。
        扫不到就返回空列表并让界面说清"这个目录下没有可统计的采集数据"，
        同时保留「浏览其它位置」给盘上的 / 别的机器来的数据。
        """
        base = Path(root) if root is not None else Path(self.bag_dir)
        if not base.is_dir():
            return []
        found = []
        try:
            children = sorted(base.iterdir(), key=lambda d: d.stat().st_mtime,
                              reverse=True)
        except OSError as exc:
            logger.warning(f"扫描采集数据目录失败：{exc}")
            return []
        for child in children:
            if len(found) >= limit:
                break
            if not child.is_dir() or not self.looks_like_bag(child):
                continue
            found.append(describe_bag(child))
        return found

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

    # ---------- 磁盘导出（`export_target: disk` / `both`） ----------
    def _export_target(self) -> str:
        """读导出目标；**没有 config 时按 `server` 处理**。

        缺省必须是 `server` —— 现场已有部署不能因为加了一个配置键就改行为，
        这是 PRD 里最强的约束。而 `DataUploader` 允许不传 config 构造
        （若干处历史调用如此），那条路径上更不能凭空去写硬盘。
        """
        if self.config is None:
            return "server"
        try:
            return str(self.config.get_export_settings().get("target") or "server")
        except Exception as exc:            # 配置层异常不该让"导出"整体失败
            logger.warning(f"读取 export_target 失败，按 server 处理：{exc}")
            return "server"

    def _disk_export_target(self):
        """造一个 `DiskExportTarget`（延迟导入，见文件头的注释）。

        挂载点取值顺序：**配置里写的那条 → 自动枚举的第一块盘**。

        为什么要有第二步：挂载点是系统按卷标自动生成的（`/media/<用户>/<卷标>`），
        换盘 / 换用户名 / 换插口都会变。要求人把系统生成的路径抄进配置里，
        本身就是让现场做一件机器已经知道答案的事 —— 实测就是这么漏的：配置写着
        `/media/nerv/DE8A-9634`，实际盘挂在 `/media/nerv/Seagate`，于是
        "能识别到盘"却"导不出"或"识别不到盘"。留空 = 全靠自动识别。

        取「第一块」而不是让用户选，是因为导出侧**没有选择界面**（只有录制侧有）。
        多块盘同时插时，界面会提示插一块；这一处的兜底只为"没插盘"和"只插一块"
        这两种现场实际会遇到的形态服务。
        """
        from .disk_export import DiskExportTarget, list_removable_mounts
        settings = self.config.get_export_settings() if self.config else {}
        configured = str(settings.get("mount_point", "") or "").strip()
        mount_point = configured
        if not mount_point:
            volumes = list_removable_mounts()
            mount_point = str(volumes[0].path) if volumes else ""
        target = DiskExportTarget(
            mount_point=mount_point,
            volume_id_file=settings.get("volume_id_file", ".nerv_volume"),
            require_volume_id=bool(settings.get("require_volume_id")),
            min_free_margin_mb=int(settings.get("min_free_margin_mb", 2048)),
            warn_file_bytes=int(settings.get("warn_file_bytes", 3_800_000_000)),
        )
        target.enumerated_mount = not configured
        return target

    def _export_to_disk(self, folder_path: Path,
                        progress_cb=None, progress_percent=None) -> bool:
        """把一次采集的数据写到本地挂载的移动硬盘。

        与远端路径的差异是**刻意的**（见 `disk_export` 的模块说明）：不建 SSH、
        不断点续传、不算远端空间。打包器共用 `_pack_folder`——它接受任意文件对象，
        所以"边打包边写盘"不需要第二套实现。

        ## 落点为什么不用远端子路径

        落点是 `挂载点/<序列号>/<采集日期>/<bag 名>/<bag 名>.zip`。早先这里
        接收 `remote_subpath`，那是**远端目录树的相对路径**，语义是
        "在服务器的哪个任务目录下"。把它套到移动硬盘上有两个具体的坏处：

        1. 现场看到的是 `移动硬盘/robot/linen/2026-10-08/xxx.zip` 这种层级，
           要靠猜才知道哪一层是自己要的；拔盘交给别人时更没人认得。
        2. **它让 disk 目标无法离线**。那个子路径原本来自远端目录选择器，
           而选择器要连 SFTP 才能列出目录 —— 于是"只写硬盘"这件事在
           实现上仍然依赖远端可用。参数去掉，这条依赖才真的断掉。

        序列号那一层是 v6.4.3 加的：**同一块盘会被多台机器轮着插**，
        不分层的话两台机器的数据混在一根目录里，盘上看不出来。
        """
        self.upload_cancelled = False
        self.last_error = ""
        self.last_code = None

        folder = Path(folder_path) if folder_path is not None else None
        if folder is None or not folder.exists() or not folder.is_dir():
            return self._fail(ErrorCode.NOT_FOUND, f"文件夹不存在：{folder_path}")

        try:
            entries, total_size = self._iter_files(folder)
        except AppError as exc:
            return self._fail(exc.code, exc.message)
        if total_size == 0:
            return self._fail(ErrorCode.NOT_FOUND, f"bag 文件夹为空：{folder}")

        disk = self._disk_export_target()
        if not disk.mount_point or str(disk.mount_point) == ".":
            # 到这里只有一种成因：**自动枚举没找到任何外接盘**（配置那条也可能没填）。
            # 所以不能说"未配置" —— v6.5.1 起挂载点本来就该留空、靠自动识别，
            # 让现场去 app_config.yaml 填一个系统生成的路径是让问题绕回原点。
            return self._fail(
                ErrorCode.NOT_FOUND,
                "未检测到移动硬盘。\n"
                "请插上移动硬盘后重试；若它已经插着，可在「导出目标」面板里"
                "点「重新检测」看识别结果（挂载点无需手工填写）。\n"
                "也可以把导出目标改回服务器。",
            )

        archive_name = self._archive_name(folder)
        est_size = self._estimate_archive_size(total_size, len(entries), self.transfer_format)
        # 落点：挂载点/<序列号>/<采集日期>/<bag 名>/（理由见 `_disk_subpath`）
        subpath = f"{self._disk_subpath(folder)}/{folder.name}"

        # ⑤ 同盘不再自我复制（v6.4.17）：录制若**直接写在盘上**（落盘位置选了移动硬盘），
        #    落点会与源目录**是同一个目录** —— 于是这次"导出"就是把采集拷进它自己：
        #    白占一倍空间 + 一次全量拷贝，盘上还会多出 `…/<bag>/<bag>.zip` 这种嵌套。
        #
        #    判据取"**目标目录是否落在源目录里面**"而不是"源在不在挂载点之内"：
        #    后者会把"盘挂载点正好是数据目录的祖先"这种正常部署也一起跳过 ——
        #    那种情况下用户确实是想把数据按标准分层拷到盘上，不该拦。
        #    并额外要求挂载点**确实是挂载点**：否则只是路径撞名（盘没插），不该跳过。
        target_dir = disk.mount_point / subpath
        if disk.is_mounted() and is_within(target_dir, folder):
            message = (f"本次数据已在移动硬盘上（录制时直接写入 {folder}）：跳过重复拷贝")
            logger.info(message)
            if progress_cb:
                progress_cb(message)
            return True

        # ---------- 导出前四道检查，按序执行 ----------
        # 顺序不是随手排的：前一道是"这个盘能不能用"，后一道才是"这批数据放不放得下"。
        # 反过来先报"空间不足"，而盘根本没挂载的话，这个提示会指向错误的方向。
        #
        # `prepare` 的返回值直接留用，**不要在写入前再调一次**：它内部会重跑
        # `check_volume`（挂载点 → 盘标识）。调第二次不只是白跑一遍—— 若盘恰好在
        # 两次之间被拔，第二次会报"创建目录时断开"，而目录其实第一次就建好了，
        # 现场会照着这个提示去查一个已经过去的故障。
        try:
            target_dir = disk.prepare(subpath, total_bytes=est_size)   # ① 挂载点 ② 盘标识 ③ 空间
        except AppError as exc:
            return self._fail(exc.code, exc.message)

        # ④ FAT32 单文件上限：**只提醒不拦截**（现场不能被程序卡住交不出数据）
        oversize = disk.warn_oversize([(p.name, s) for p, s in entries])
        if oversize:
            hint = disk.describe_limit_hint()
            names = "、".join(f"{n}（{s / 1e9:.1f}GB）" for n, s in oversize[:3])
            more = f"等 {len(oversize)} 个文件" if len(oversize) > 3 else ""
            message = (f"本次有 {len(oversize)} 个文件超过该盘的单文件上限：{names}{more}。\n"
                       f"{hint}\n可选择「仍继续」（部分文件会写入失败）或更换 exFAT 硬盘。")
            logger.warning(message)
            if progress_cb:
                progress_cb(message)
        if progress_cb:
            progress_cb(
                f"目标：移动硬盘 {disk.mount_point}"
                f"（{disk.filesystem_type() or '文件系统未知'}）\n"
                f"保存位置：{disk.mount_point}/{subpath}/")

        def running() -> bool:
            return not self.upload_cancelled

        # `_pack_folder` 的 `fp` 要的是一个**有 `.write()`/`flush()` 的文件对象**
        # （它直接当 zipfile/tarfile 的 fileobj 用），而 `write_stream` 给的
        # `sink` 是**写入回调**（bytes -> None）。两者语义不同，所以套一层适配。
        #
        # 复用已有的 `ProgressWriter`（它本来就是"把只顺序写的目标包装成
        # zipfile 可用的文件对象"，职责完全一致），不另造一个只写壳。
        class _CallbackSink:
            """把写入回调伪装成文件对象，供 zipfile/tarfile 使用。"""

            def __init__(self, fn):
                self._fn = fn

            def write(self, data) -> int:
                self._fn(data)
                return len(data)

            def flush(self) -> None:
                """zipfile 关闭前会 flush。这里**不能报错**：底层的
                "已写入多少"由 `write_stream` 自己统计，flush 对磁盘侧无意义。"""

        def producer(sink) -> None:
            self._pack_folder(
                folder, ProgressWriter(_CallbackSink(sink)),
                self.transfer_format,
                self.transfer_compression == "deflate",
                entries, running_cb=running, collect_digest=self.verify_content,
            )

        try:
            disk.write_stream(
                target_dir, archive_name, producer,
                progress_cb=progress_cb, progress_percent=progress_percent,
                # **必须传总量**：`write_stream` 要用它把已写字节换算成百分比。
                # 不传的话它只发文字进度、不发百分比（发一个错的数字比不发更糟）。
                total_bytes=est_size,
            )
        except AppError as exc:
            # 拔盘/空间/IO 都已是带码异常，原样保留码，界面才能给对建议
            return self._fail(exc.code, exc.message)
        except Exception as exc:
            return self._fail(ErrorCode.LOCAL_IO, f"导出到移动硬盘失败：{exc}")

        if self.verify_content:
            try:
                written = target_dir / archive_name
                # 校验要**把刚写完的归档整个再读一遍**（几 GB 的盘就是几秒），
                # 而进度条此刻已经是 100%。不说的话，现场看到的就是
                # "满格了但程序还在动"—— 与其让那几秒显得像卡住，不如如实讲。
                if progress_cb:
                    progress_cb("正在校验内容（读取归档文件计算 SHA-256）")
                write_sha256_sidecar(written, sha256_file(written))
            except OSError as exc:
                # 边车文件写不出来不算导出失败：数据已经在盘上且已验过内容
                logger.warning(f"写 .sha256 边车文件失败（数据本身已写入）：{exc}")

        logger.info(f"已导出到移动硬盘：{target_dir / archive_name}")
        if progress_cb:
            # 日志里给**完整落点层级**，而不是只有文件名 ——
            # 盘插满几十台机器的数据时，"在哪个目录下"才是现场要找的东西。
            progress_cb(f"已导出到移动硬盘：{subpath}/{archive_name}")
        return True

    # ---------- 打包 + 上传（统一入口） ----------
    def _archive_name(self, folder: Path) -> str:
        suffix = "tar" if self.transfer_format == "tar" else "zip"
        return f"{folder.name}.{suffix}"

    # ---------- 磁盘落点分层（挂载点 / 序列号 / 日期 / bag 名） ----------

    #: 读不到序列号时用它占位。**刻意不静默扁平**：
    #: 扁平（直接写挂载点根）会让"两台机器的数据混在一根目录"这件事
    #: 在盘上完全看不出来 —— 而这个盘本来就是多台机器轮着插的。
    #: 一个显眼的 `unknown-serial` 让这件事在盘上一眼可见。
    #: 取值与落点分层统一在 `disk_export` 里实现 —— **同一套口径同时服务
    #: 「录制直接写盘」与「录完导出」两条路**，这里只是取用点（别再各写一份）。
    UNKNOWN_SERIAL_DIR = disk_export.UNKNOWN_SERIAL_DIR

    def _serial_number(self) -> str:
        """本机序列号（`Product.json` 的 `serial_number`）；读不到用占位名。

        读的是顶栏「序列号」那同一个字段（`ConfigManager.get_product_info`）——
        顶栏显示什么，落点目录名就是什么，不出现"界面一个样、目录另一个样"。
        """
        return disk_export.read_serial(self.config)

    @staticmethod
    def _safe_dir_name(raw: str) -> str:
        """把序列号变成一个安全的单层目录名。

        序列号要被拼进文件系统路径，所以必须挡住分隔符与相对路径：
        读到 `../../etc` 这种值时，**拼路径这一步就该失败**，
        而不是把文件写到挂载点外面去。
        """
        return disk_export.safe_dir_name(raw)

    @staticmethod
    def _capture_date(folder: Path) -> str:
        """采集日期（`YYYY-MM-DD`），取 bag 目录的 mtime。

        ## 为什么用 mtime 而不是"今天"

        现场有一件很平常的事：**今天导出昨天甚至上周录的数据**。
        按导出日期分层，那批数据会被归进今天 —— 而盘上那天可能有当天的
        正常采集，两批不同的数据混进同一个目录，现场谁也分不出来。

        mtime 是"这份数据什么时候产生的"，用它分层，
        "盘上按日期分目录"才真的等于"按采集日期分目录"。
        补录/重传同一批数据也自然落到同一天，便于核对。
        """
        return disk_export.capture_date(folder)

    def _disk_subpath(self, folder: Optional[Path] = None) -> str:
        """磁盘落点（相对挂载点）：`<序列号>/<YYYY-MM-DD>/`。

        每个采集 bag 一个目录的安排保留在 `prepare` 里（`subpath` 再拼 bag 名），
        所以同一天多次采集不会互相覆盖。

        `folder=None` 表示**预览一次还没发生的采集**（界面上的「保存路径」用）：
        此时日期取**今天**。不能拿某个已有目录的 mtime 顶替 ——
        `bag_dir` 是上次采集建的，它的 mtime 是**上一次**的时间，
        拿它算会让预览显示成上次的日期，而本次采集显然在今天。
        """
        return disk_export.disk_subpath(self._serial_number(), folder)

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

        ## 导出目标分流（`export_target`）

        三态，**缺省 `server` = 行为与加这个功能之前完全一致**：

        | 值 | 行为 |
        | --- | --- |
        | `server`（默认） | 只走原来的 SFTP 路径，磁盘侧**一行都不碰** |
        | `disk` | 只写本地挂载点，**完全不建立 SSH 连接** |
        | `both` | 两边都写；任一边失败都报出来（不吞掉） |

        为什么分流放在这里而不是界面层：界面有**两个**入口
        （`upload_latest_bag` 与 `upload_specific_bag`，数据查看页与上传页各走一个），
        在界面分流要改两处、漏一处就是"某个入口的导出目标不生效"——
        而这类不一致极难在测试里发现。`upload_bag_folder` 是两者的唯一汇合点。
        """
        target = self._export_target()
        if target in ("disk", "both"):
            if target == "disk":
                # 不传 remote_subpath：磁盘侧落点由自己的规则决定（挂载点下的 bag 名目录）
                return self._export_to_disk(folder_path, progress_cb, progress_percent)
            # both：先磁盘后服务器。顺序理由是磁盘侧是"把数据带走"的最后一环，
            # 现场最关心的就是它；先做它可以在网络异常时仍拿到本地副本。
            disk_ok = self._export_to_disk(folder_path, progress_cb, progress_percent)
            if not disk_ok:
                return False
            return self._upload_bag_folder_to_server(
                folder_path, remote_subpath, progress_cb, progress_percent)

        return self._upload_bag_folder_to_server(
            folder_path, remote_subpath, progress_cb, progress_percent)

    def _upload_bag_folder_to_server(self, folder_path: Path, remote_subpath: str,
                                     progress_cb, progress_percent) -> bool:
        """原来的上传实现，一字未改（只是从 `upload_bag_folder` 里挪出来）。

        单独成方法是为了让"分流"这件事在代码上可见：远端路径的行为与
        加磁盘导出之前**逐行相同**，回归风险因此只落在磁盘侧那半边。
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
