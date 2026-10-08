"""本地磁盘导出目标（移动硬盘场景）。

## 为什么要独立于 core_data

`core_data` 的上传路径全部建立在 SFTP 语义上：越界校验、断点续传、
`cat` 通道。把"写入本地挂载点"塞进去，等于让本地路径去套网络路径的规则 ——
本机磁盘没有 `_check_remote_path` 那种"只能写在 base 之下"的约束，
硬套会让"哪些路径算合法"变得无法推理。

所以这里只提供**能对齐的最小接口**：目录准备、写入、空间预检、体积预判。
断点续传与流式直传是网络独有的优化，磁盘侧不复制这些复杂度 ——
移动硬盘是本地设备，写入失败可以重来，不需要为了"省一次重传"引入偏移逻辑。

## 现场约束（这些不是过度设计，是实际会遇到的）

- **盘会被拔**：写入中途设备消失会抛 `OSError: 无此设备`，
  必须转成带提示的错误，而不是弹一个原始的 errno 让现场看不懂。
- **盘会插错**：工程师桌上有若干 U 盘/移动盘，写错盘的后果是数据散在
  不知道哪台机器上。所以用盘内标识文件做一道确认，而不是"选个目录就写"。
- **FAT32 有4GB 单文件上限**：超了会**写到一半失败**，
  所以在导出**之前**就把超限文件点出来，让用户决定换盘还是继续。
  只提醒不拦截 —— 现场不能被程序硬性卡住交不出数据。
"""
import os
import shutil
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .core import logger
from .errors import AppError, ErrorCode
from .verify import (
    detect_filesystem,
    has_single_file_limit,
)

# 拔盘/掉盘类错误在 Linux 上的 errno 集合。匹配了就转成"设备不可用"，
# 而不是让现场看到 "Input/output error (errno 5)" 猜不出发生了什么。
DEVICE_LOST_ERRNOS = {
    5,      # EIO
    6,      # ENXIO
    19,     # ENODEV
    107,    # ENOTCONN
    116,    # ESTALE
    19,     # ENODEV (dup, 保留可读性)
}


class DeviceDisconnectedError(AppError):
    """导出过程中存储设备消失（拔盘 / 掉线 / 挂载点被卸载）。"""

    def __init__(self, message: str, partial_path: Optional[Path] = None):
        super().__init__(ErrorCode.LOCAL_IO, message)
        self.partial_path = partial_path


def is_device_lost(exc: BaseException) -> bool:
    """判断异常是否是"设备不可用"（而非普通 IO 错误）。"""
    err = getattr(exc, "errno", None)
    if err in DEVICE_LOST_ERRNOS:
        return True
    text = str(exc).lower()
    return any(k in text for k in ("no such device", "device not ready",
                                   "input/output error", "not connected"))


class DiskExportTarget:
    """把归档写到本地挂载点。

    刻意保持与网络路径**接口不同**：磁盘侧没有 resume，
    因为本地设备写入可以原地重试，引入偏移逻辑只会增加出错面。
    """

    def __init__(self, mount_point: str, volume_id_file: str = "",
                 require_volume_id: bool = True,
                 min_free_margin_mb: int = 2048,
                 warn_file_bytes: int = 3_800_000_000):
        self.mount_point = Path(mount_point)
        self.volume_id_file = volume_id_file
        self.require_volume_id = require_volume_id
        self.min_free_margin = min_free_margin_mb * 1024 * 1024
        self.warn_file_bytes = warn_file_bytes

    # ---------- 挂载点与盘标识 ----------

    def is_mounted(self) -> bool:
        """挂载点是否是一个真实的挂载点（而不是普通目录）。

        用 `os.path.ismount` 而不是"目录存在"：现场把路径写错成
        `/mnt/usb` 而实际盘挂在 `/media/xxx` 时，目录不存在还好，
        但若是存在却没挂载的目录，写进去的东西会落在本机磁盘上 ——
        工程师以为导出成功了，数据其实没上盘。
        """
        return self.mount_point.is_dir() and os.path.ismount(str(self.mount_point))

    def volume_marker_path(self) -> Optional[Path]:
        """盘内标识文件路径；未配置标识时返回 None。"""
        if not self.volume_id_file:
            return None
        return self.mount_point / self.volume_id_file

    def has_volume_marker(self) -> bool:
        marker = self.volume_marker_path()
        return bool(marker and marker.exists())

    def check_volume(self) -> None:
        """校验挂载点可用。失败时抛带码异常，让界面说清"该做什么"。"""
        if not self.mount_point.exists():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"导出路径不存在：{self.mount_point}\n"
                f"请确认移动硬盘已插上并挂载，或修改 app_config.yaml 的 "
                f"disk_export.mount_point",
            )
        if not self.is_mounted():
            raise AppError(
                ErrorCode.CONFIG_MISSING,
                f"{self.mount_point} 不是已挂载的设备目录。\n"
                f"可能是路径写错，或硬盘未挂载。写进去的数据不会真的上盘，"
                f"建议先在文件管理器里确认该盘已挂载。",
            )
        if self.require_volume_id and not self.has_volume_marker():
            marker = self.volume_marker_path()
            raise AppError(
                ErrorCode.CONFIG_MISSING,
                f"该盘不是数据盘（缺少标识文件 {marker}）。\n"
                f"为避免把数据写进错盘，导出已停止。\n"
                f"若确认这就是目标数据盘，可在盘根目录创建该文件后重试。",
            )

    # ---------- 能力探测与预判 ----------

    def filesystem_type(self) -> str:
        return detect_filesystem(self.mount_point)

    def free_space(self) -> int:
        try:
            usage = shutil.disk_usage(str(self.mount_point))
            return usage.free
        except OSError as exc:
            logger.warning(f"读取 {self.mount_point} 可用空间失败：{exc}")
            return 0

    def check_free_space(self, needed: int) -> None:
        """空间预检：不足时提前报错，而不是传到一半失败。"""
        if needed <= 0:
            return
        free = self.free_space()
        # free 为 0 通常是探测失败，此时不拦——真正的写入错误会在写的时候暴露
        if free and free < needed + self.min_free_margin:
            raise AppError(
                ErrorCode.LOCAL_SPACE,
                f"移动硬盘可用空间不足：需要约 {_mb(needed)} + {_mb(self.min_free_margin)} 余量，"
                f"实际可用 {_mb(free)}。请换一块盘。",
            )

    def warn_oversize(self, entries: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
        """返回"写下去会失败"的文件列表（**只提示，不拦截**）。

        判定依据是目标盘的文件系统类型：FAT32 有 4GB 硬上限，
        exFAT/NTFS 没有。所以同一批数据在 exFAT 盘上不该被提醒 ——
        误报一次，现场就不会再相信这个功能了。
        """
        fs = self.filesystem_type()
        if not has_single_file_limit(fs):
            return []
        over = [(n, s) for n, s in entries if s > self.warn_file_bytes]
        if over:
            logger.warning(
                f"目标盘 {self.mount_point}（{fs or '未知文件系统'}）存在 "
                f"{len(over)} 个超过 {self.warn_file_bytes / 1e9:.1f}GB 的文件，"
                f"写入可能失败"
            )
        return over

    def describe_limit_hint(self) -> str:
        """给界面用的限制说明。"""
        fs = self.filesystem_type()
        if not fs:
            return "未能识别目标盘的文件系统类型，将不做体积预判"
        if has_single_file_limit(fs):
            return (f"目标盘文件系统为 {fs}，单文件上限约 4GB。"
                    f"超过该大小的文件会写入失败，建议更换 exFAT 格式的硬盘")
        return f"目标盘文件系统为 {fs}，无 4GB 单文件限制"

    # ---------- 写入 ----------

    def prepare(self, subpath: str, total_bytes: int = 0) -> Path:
        """校验并建目录，返回最终写入目录。"""
        self.check_volume()
        target = self.mount_point / subpath.lstrip("/") if subpath else self.mount_point
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if is_device_lost(exc):
                raise DeviceDisconnectedError(
                    f"移动硬盘在创建目录时断开：{target}", partial_path=target) from exc
            raise AppError(
                ErrorCode.LOCAL_IO,
                f"无法在移动硬盘上创建目录：{target}\n{exc}",
            ) from exc
        if total_bytes:
            self.check_free_space(total_bytes)
        return target

    def write_stream(self, target_dir: Path, filename: str,
                     producer: Callable[[Callable[[bytes], None]], None],
                     progress_cb: Optional[Callable[[str], None]] = None,
                     progress_percent: Optional[Callable[[int], None]] = None,
                     total_bytes: int = 0) -> Path:
        """让`producer` 往目标文件写，由本方法负责原子性与拔盘处理。

        **先写临时文件再 rename**：移动硬盘随时可能被拔，直接写最终文件名的话，
        中途断开会在盘上留下一个"看起来是正常包、实际是半截"的文件 ——
        它会被下游按文件名取走，比直接失败危险得多。
        rename 在同一文件系统内是原子的，所以要么看不到这个文件，
        要么看到的就是完整的。

        ## `progress_percent` 收的是**百分比**，不是字节数

        这里曾经直接发`int(written / 1024 / 1024)`（已写入的 MB 数）。
        而界面 `update_upload_progress` 会 `min(100, value)` —— 于是
        3MB 的包瞬间显示 100%，8000MB 的包传到 100MB 就满格。
        **"传输完成"这两个字与真实进度彻底脱钩**：现场看到满格就去拔盘，
        数据还没写完。百分比必须由总量换算（`total_bytes`），不能由已写量直接充当。
        """
        final_path = target_dir / filename
        tmp_path = target_dir / (filename + ".partial")
        written = 0
        # 总量未知时**不发百分比**（发了也是错的），只发文字进度 ——
        # 这比发一个看似合理实则错误的数字诚实。
        total = max(int(total_bytes or 0), 0)

        try:
            with open(tmp_path, "wb") as out:

                def sink(chunk: bytes) -> None:
                    nonlocal written
                    out.write(chunk)
                    written += len(chunk)
                    if progress_cb and written % (16 * 1024 * 1024) < len(chunk):
                        progress_cb(f"正在写入移动硬盘：{_mb(written)}")
                    if progress_percent and total:
                        # 卡在 99 而不是 100：rename 之前文件还不算写完。
                        # 提前报 100 会让"满格"这个信号失去意义（现场会以为可以拔盘了）。
                        progress_percent(min(99, int(written * 100 / total)))

                producer(sink)
        except OSError as exc:
            # 半成品必须清掉：留着会被当成"完整但没传完"的数据
            _quiet_unlink(tmp_path)
            if is_device_lost(exc):
                raise DeviceDisconnectedError(
                    f"移动硬盘在写入时断开（已写入 {_mb(written)}）：\n"
                    f"{exc}\n已导出的其他文件保留，本次中断。",
                    partial_path=final_path,
                ) from exc
            raise AppError(ErrorCode.LOCAL_IO, f"写入移动硬盘失败：{exc}") from exc
        except BaseException:
            _quiet_unlink(tmp_path)
            raise

        try:
            tmp_path.replace(final_path)
        except OSError as exc:
            _quiet_unlink(tmp_path)
            if is_device_lost(exc):
                raise DeviceDisconnectedError(
                    f"移动硬盘在完成写入时断开：{final_path}", partial_path=final_path,
                ) from exc
            raise AppError(
                ErrorCode.LOCAL_IO,
                f"无法完成移动硬盘上的文件改名：{final_path}\n{exc}",
            ) from exc

        if progress_percent:
            progress_percent(100)
        if progress_cb:
            progress_cb(f"已写入移动硬盘：{final_path.name}（{_mb(written)}）")
        return final_path


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _mb(value: float) -> str:
    return f"{value / 1024 / 1024:.0f}MB"
