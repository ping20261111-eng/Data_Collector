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
import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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
                 warn_file_bytes: int = 3_800_000_000,
                 verify_mode: str = "off"):
        self.mount_point = Path(mount_point)
        self.volume_id_file = volume_id_file
        self.require_volume_id = require_volume_id
        self.min_free_margin = min_free_margin_mb * 1024 * 1024
        self.warn_file_bytes = warn_file_bytes
        #: 校验档位 `off` / `digest` / `readback`（见 `ConfigManager.VERIFY_MODES`）。
        #: 放在这里而不是复制函数上：它是**配置的落点**，`copy_file` 只关心
        #: "要不要算摘要"这一个布尔，不该知道档位的名字。
        self.verify_mode = verify_mode
        #: 这个挂载点是**自动枚举**得来的（不是配置里手写的）。只影响报错措辞：
        #: 枚举来的路径"不存在"意思是"盘没插"，说成"路径写错、请改配置"会让人去改
        #: 一个他根本没填过、也填不该填的字段。
        self.enumerated_mount = False

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
            if self.enumerated_mount:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "未检测到移动硬盘（已启用自动识别，无需配置挂载点）。\n"
                    "请插上移动硬盘后重试；若它已经插着，可在「导出目标」面板里"
                    "点「重新检测」看识别结果。",
                )
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


    #: 复制时的读块大小。4MB 是"足够大的顺序 IO + 进度不至于太粗"的折中
    #: （与 `verify.VERIFY_CHUNK` 同量级）。
    COPY_CHUNK = 4 * 1024 * 1024

    def copy_file(self, source: Path, target: Path,
                  on_chunk: Optional[Callable[[int], None]] = None,
                  digest: bool = False) -> Dict[str, object]:
        """把 `source` 复制成 `target` —— **不压缩、不打包**（v6.7.0）。

        磁盘导出为什么不再产出一个 zip：盘本身就是文件系统，打包只是给"搬运"
        加了一道解压步骤；而且打包要"读一遍写一遍"，解压又是"读一遍写一遍"，
        同盘搬运的 IO 直接翻倍。MCAP / 视频本身已是压缩格式，再 DEFLATE 只耗 CPU。

        ## 原子性与可恢复

        **先写 `<target>.partial` 再 `os.replace`**：与 `write_stream` 同一条理由 ——
        移动硬盘随时可能被拔，直接写最终文件名会在盘上留下一个"看起来完整、
        实际半截"的文件，而它会被下游当成正常数据取走。

        ## 返回值

        `{"bytes": 写入字节数, "digest": sha256 十六进制或 ""}`。`digest=True` 时
        一边复制一边算 —— 校验就不必再把源文件读一遍（几 GB × 2 的差别）。
        """
        tmp = target.with_name(target.name + ".partial")
        written = 0
        hasher = hashlib.sha256() if digest else None
        try:
            with open(source, "rb") as src, open(tmp, "wb") as out:
                # 告诉内核这是**纯顺序读**：预读窗口会更积极。拷贝本身就是
                # 从头到尾一遍，没有任何随机访问的意图。拿不到这个接口就跳过
                # （它不是正确性所需，只是给内核的一条提示）。
                try:
                    os.posix_fadvise(src.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
                except (AttributeError, OSError):
                    pass
                while True:
                    chunk = src.read(self.COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    if on_chunk is not None:
                        on_chunk(len(chunk))
        except OSError as exc:
            _quiet_unlink(tmp)
            if is_device_lost(exc):
                raise DeviceDisconnectedError(
                    f"移动硬盘在写入时断开（本文件已写入 {_mb(written)}）：\n"
                    f"{exc}\n已复制的文件保留，本次中断。",
                    partial_path=target,
                ) from exc
            raise AppError(
                ErrorCode.LOCAL_IO,
                f"写入移动硬盘失败：{target}\n{exc}",
            ) from exc
        except BaseException:
            _quiet_unlink(tmp)
            raise
        try:
            os.replace(tmp, target)
        except OSError as exc:
            _quiet_unlink(tmp)
            raise AppError(ErrorCode.LOCAL_IO,
                           f"写入移动硬盘后改名失败：{target}\n{exc}") from exc
        # 保留修改时间：现场按文件时间判断"哪一段先录的"；也是下次导出能
        # "看出这个文件已经拷过"的依据（见 `DataUploader._already_exported`）。
        try:
            os.utime(target, (source.stat().st_atime, source.stat().st_mtime))
        except OSError as exc:
            logger.debug(f"保留修改时间失败（不影响数据本身）：{target}（{exc}）")
        return {"bytes": written, "digest": hasher.hexdigest() if hasher else ""}


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _mb(value: float) -> str:
    return f"{value / 1024 / 1024:.0f}MB"


#: 不是真实文件系统的挂载类型，一律不算"盘"。
#: 实测踩过的坑：本机 `/proc/mounts` 有 30 多条，其中 SNAP 那些全是 squashfs + loop
#: 设备 —— 不排除的话，枚举结果会被一堆 `/snap/core22/2437` 之类的只读系统镜像占满，
#: 界面下拉里全是"移动硬盘"。
PSEUDO_FS_TYPES = frozenset({
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs",
    "devpts", "efivarfs", "fuse", "fusectl", "hugetlbfs", "mqueue", "nsfs",
    "overlay", "proc", "pstore", "ramfs", "securityfs", "squashfs", "sunrpc",
    "sysfs", "tmpfs", "tracefs",
})

#: 本机系统挂载点：即使与 `/` 不是同一设备（独立分区）也不该当成外接盘。
#: `/boot/efi` 就是这种情况 —— 它是 `/dev/nvme0n1p1`，独立分区，但仍在本机盘上。
SYSTEM_MOUNTS = frozenset({
    "/", "/boot", "/boot/efi", "/home", "/opt", "/root", "/run", "/srv", "/var",
})

#: 小于这个容量的外接盘不列为数据盘候选：一次常规采集几 GB 到几十 GB，
#: U 盘虽然容量够但现场极少拿它装采集数据，而误选的代价（抱着 U 盘等交付）
#: 比少列一个候选大。只提示不拦截。
MIN_DISK_BYTES = 8 * 1024 ** 3


@dataclass(frozen=True)
class MountedVolume:
    """一个枚举到的外接设备挂载点（移动硬盘 / U 盘 / 读卡器…）。

    字段只留判定与界面真正用得到的：`path` 决定落点，
    `label` / `fs` / `total_bytes` / `free_bytes` 用来在界面上说清"这是哪块盘"。
    """

    path: Path
    label: str
    fs: str
    total_bytes: int = 0
    free_bytes: int = 0
    #: 是不是配置里 `disk_export.mount_point` 指定的那一块（界面标"配置指定"）
    configured: bool = False

    @property
    def capacity_ok(self) -> bool:
        """容量够不够装一次采集。"""
        return self.total_bytes <= 0 or self.total_bytes >= MIN_DISK_BYTES

    def summary(self) -> str:
        """给界面下拉用的一行摘要（不含路径 —— 路径太长会把控件撑开）。"""
        bits = [self.label]
        if self.fs:
            bits.append(self.fs)
        if self.total_bytes:
            bits.append(f"可用 {_size(self.free_bytes)}")
        return " · ".join(bits)


def _size(value: float) -> str:
    """字节数转成人看得懂的容量（如 `1.1TB`）。"""
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f}B" if unit == "B" else f"{number:.1f}{unit}"
        number /= 1024.0
    return f"{number:.1f}TB"


def _parent_block_device(source: str) -> str:
    """`/dev/sda1` → `sda`；`/dev/mmcblk0p1` → `mmcblk0`（只削末尾分区号）。"""
    name = os.path.basename(source)
    return name.rstrip("0123456789") or name


def _usb_sysfs_path(block: str) -> str:
    """块设备在 sysfs 里的真实路径（USB 设备的路径里含 `/usb`）。

    单独抽出来是为了**可被测试替换**：判据本身（"`/usb` in 路径"）是纯逻辑，
    而"怎么拿到 sysfs 路径"要碰文件系统。测试替换前者、验后者，
    才不会变成"把判据整个打桩 → 改生产代码也抓不到"。
    """
    return os.path.realpath(f"/sys/block/{block}/device")


def _is_usb_block(block: str) -> bool:
    """块设备是否挂在 USB 总线上（读 sysfs 真实路径，不起子进程）。

    为什么不用 `lsblk TRAN`：要 subprocess（每秒轮询一次代价不合适），而且
    **USB 盘经 SCSI 层时 TRAN 给的是 `scsi`**，反而判错。
    为什么不用 `/sys/block/*/removable`：实测移动硬盘盒普遍报 `0`
    （本机 `/dev/sda1` 就是），与内置 NVMe 完全一样，分不出来。
    sysfs 里 USB 设备的真实路径含 `/usb`（如
    `/sys/devices/pci.../0000:00:14.0/usb2/2-3/2-3:1.0/host0/...`）—— 这条是准的。
    """
    try:
        return "/usb" in _usb_sysfs_path(block)
    except OSError:
        return False


def list_removable_mounts(configured: str = "") -> List[MountedVolume]:
    """枚举当前插着的所有外接存储设备。

    ## 为什么不再只认配置里那一条 `mount_point`

    挂载点是**系统按卷标自动生成**的：换一块盘、换一个用户名、换一个插口，路径
    就变（`/media/nerv/Seagate` ↔ `/media/robot/DE8A-9634` ↔
    `/run/media/robot/NERV` ↔ `/mnt/nerv_data`）。要人把系统生成的路径手工抄进
    `app_config.yaml`，等于让现场做一件机器本来就知道答案的事 —— 实测就是这么漏的：
    配置写着 `/media/nerv/DE8A-9634`，实际盘挂在 `/media/nerv/Seagate`，
    程序报"未检测到移动硬盘"，而盘就在眼前。

    所以改成**枚举为主、配置为辅**：`configured` 非空且当前确实挂着时，那一块会被
    标 `configured=True`（界面显示"配置指定"）并排到最前；它挂不上时也只是少一个
    优先项，**不影响其它盘被枚举出来**。

    ## 判定为什么是"两条同时成立"

    1. `st_dev` 与 `/` 不同 —— 是独立块设备，不是本机盘里的一个普通目录
       （后者是"路径撞名"的经典陷阱：机器侧有人把挂载点配成 `/home/nerv/data`，
       于是数据其实写进了本机盘，而界面以为上了移动硬盘）。
    2. sysfs 设备路径含 `/usb` —— 是 USB 设备，不是 NVMe / SATA 内置盘。

    再排除两类：`/proc/mounts` 里的**伪文件系统**（SNAP、fuse、tmpfs…），
    以及 `/`、`/boot/efi` 这类**本机系统挂载点**（独立分区但仍在本机盘上）。

    顺序稳定：配置指定的那块排最前，其余按「容量大的在前、挂载点名字次之」——
    不按挂载顺序排，否则每插拔一次下拉里各项的位置就会跳。
    """
    try:
        root_dev = os.stat("/").st_dev
    except OSError as exc:
        logger.warning(f"读取根目录设备号失败，无法枚举外接盘：{exc}")
        return []

    configured_real = ""
    if configured:
        try:
            configured_real = os.path.realpath(os.path.expanduser(str(configured)))
        except OSError:
            configured_real = str(configured)

    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            mount_lines = handle.readlines()
    except OSError as exc:
        logger.warning(f"读取 /proc/mounts 失败，无法枚举外接盘：{exc}")
        return []

    found: dict = {}
    for line in mount_lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mount, fs = parts[0], parts[1], parts[2]
        if fs in PSEUDO_FS_TYPES or not source.startswith("/dev/"):
            continue                       # 伪文件系统 / 非块设备来源
        if mount in SYSTEM_MOUNTS:
            continue                       # 本机系统挂载点（/boot/efi 等）
        try:
            stat_result = os.stat(mount)
        except OSError:
            continue                       # 拔掉了 / 没权限
        if stat_result.st_dev == root_dev:
            continue                       # 与 / 同设备 → 本机盘上的目录
        if not _is_usb_block(_parent_block_device(source)):
            continue

        real = os.path.realpath(mount)
        if real in found:
            continue                       # 同一块盘被挂到多处，只留第一个
        try:
            usage = shutil.disk_usage(mount)
            total, free = usage.total, usage.free
        except OSError:
            total, free = 0, 0
        found[real] = MountedVolume(
            path=Path(mount),
            label=Path(mount).name or str(mount),
            fs=fs,
            total_bytes=total,
            free_bytes=free,
            configured=(real == configured_real),
        )

    return sorted(found.values(),
                  key=lambda vol: (not vol.configured, -vol.total_bytes, str(vol.path)))


# ---------- 录制落盘目标（本机数据盘 / 移动硬盘） ----------
#
# 「这一次录到哪里」是**一次会话的决定**，不是机器配置 —— 所以这里只负责把两种落点
# **算出来**（含可用性与不可用原因），由「录制设置」界面选、由
# `ConfigManager.set_record_target()` 记住（**不写回 app_config**：
# 把它写死会有个真陷阱 —— 盘拔了配置还指着它，起录直接失败）。
#
# 判定口径**与导出到硬盘完全共用**同一个 `DiskExportTarget`（挂载点 + 盘标识）——
# 否则会出现"能导出却录不进去"这种自相矛盾。

#: 两个候选落点的键。界面、`ConfigManager` 与录制脚本的 `BAG_DIR` 都按它分派。
#: 本机项就是 `local`；外接盘项是 **`disk:<挂载点绝对路径>`**（见 `disk_target_key`）。
LOCAL_TARGET = "local"

#: 外接盘候选的键前缀。**不带挂载点的一段（光 `"disk"`）在 v6.5.1 起不再由界面产生** ——
#: 插两块盘时两个候选的键必须不同，界面才能知道"用户选的是哪一块"。
DISK_TARGET = "disk"


def disk_target_key(mount_point) -> str:
    """外接盘候选的键：`disk:<挂载点绝对路径>`。

    为什么键里要带挂载点：v6.5.1 之前只有一个候选（配置写死那一块），键是 `"disk"`。
    改成自动枚举后可能同时列出好几块，键相同的话 `QComboBox.findData` 永远命中第一个，
    "选了第二块却写到第一块"就成了静默写错位置 —— 这类错现场根本发现不了。
    """
    try:
        real = os.path.realpath(str(mount_point))
    except (OSError, ValueError):
        real = str(mount_point)
    return f"{DISK_TARGET}:{real}"

#: 读不到序列号时的占位目录名。**刻意不静默扁平**：扁平（直接写挂载点根）会让
#: "两台机器的数据混在一根目录"这件事在盘上完全看不出来 —— 而这个盘本来就是
#: 多台机器轮着插的。一个显眼的 `unknown-serial` 让它在盘上一眼可见。
UNKNOWN_SERIAL_DIR = "unknown-serial"


def safe_dir_name(raw: str) -> str:
    """把序列号变成一个安全的单层目录名（挡住分隔符与相对路径）。

    序列号要被拼进文件系统路径，读到 `../../etc` 这种值时**拼路径这一步就该失败**，
    而不是把数据写到挂载点外面去。
    """
    cleaned = "".join(ch for ch in str(raw) if ch.isalnum() or ch in "-_")
    return cleaned.strip("-_")


def read_serial(config) -> str:
    """本机序列号（`Product.json` 的 `serial_number`）；读不到用占位名。

    读的是顶栏「序列号」那同一个字段（`ConfigManager.get_product_info`）——
    顶栏显示什么，落点目录名就是什么，不出现"界面一个样、目录另一个样"。
    """
    if config is None:
        return UNKNOWN_SERIAL_DIR
    try:
        raw = str(config.get_product_info().get("serial_number") or "").strip()
    except Exception as exc:               # noqa: BLE001 —— 配置层异常不该让录制整体失败
        logger.warning(f"读取序列号失败，用占位目录名：{exc}")
        return UNKNOWN_SERIAL_DIR
    return safe_dir_name(raw) or UNKNOWN_SERIAL_DIR


def capture_date(folder: Optional[Path] = None) -> str:
    """采集日期（`YYYY-MM-DD`）；`folder=None` 表示"还没发生的这一次"，取今天。

    ## 已有目录为什么用 mtime 而不是"今天"

    现场有一件很平常的事：**今天导出昨天甚至上周录的数据**。按导出日期分层，
    那批数据会被归进今天 —— 而盘上那天可能有当天的正常采集，两批不同的数据混进
    同一个目录，现场谁也分不出来。mtime 是"这份数据什么时候产生的"，用它分层，
    "盘上按日期分目录"才真的等于"按采集日期分目录"；补录/重传同一批也自然落到同一天。

    ## "还没发生的这一次"为什么必须用今天

    `bag_dir` 是上次采集建的，它的 mtime 是**上一次**的时间；拿它顶替会让预览显示成
    上次的日期，而本次采集显然在今天。
    """
    if folder is None:
        stamp = datetime.now()
    else:
        try:
            stamp = datetime.fromtimestamp(Path(folder).stat().st_mtime)
        except OSError as exc:
            logger.warning(f"读取目录时间失败，按当天分层：{exc}")
            stamp = datetime.now()
    return stamp.strftime("%Y-%m-%d")


def disk_subpath(serial: str, folder: Optional[Path] = None) -> str:
    """盘上相对挂载点的分层：`<序列号>/<YYYY-MM-DD>`。

    与「导出到移动硬盘」的落点**逐层一致** —— 于是"录制直接写盘"与"录完再导出"
    两种来路，交出去的盘长得一模一样；现场不必按来路记两套目录习惯。
    """
    return f"{serial}/{capture_date(folder)}"


@dataclass(frozen=True)
class RecordTarget:
    """一个候选落盘位置。

    `path` 就是**选它时会被当成 `BAG_DIR` 传给录制脚本的那个目录**；
    `available=False` 时 `reason` 说明为什么不能用（界面据此置灰并给出原因）。

    `mount` 指向枚举到的那个外接盘（可能是 `None`，本机候选就是）。
    界面上"这块盘是哪块、什么格式、还剩多少空间"全部从这里取，不要再去查盘。
    """

    key: str
    label: str
    path: Path
    available: bool
    reason: str = ""
    mount: Optional[MountedVolume] = None

    def describe(self) -> str:
        if self.available:
            return f"{self.label}（{self.path}）"
        return f"{self.label}（不可用：{self.reason}）"


def disk_settings(config) -> dict:
    """`app_config.yaml` 的 `disk_export` 段（配置缺失时给一份保守默认）。"""
    settings = config.get_export_settings() if config is not None else {}
    return settings or {}


def _disk_target_object(config, mount_point: str = "") -> DiskExportTarget:
    """按配置造一个 `DiskExportTarget`（与 `DataUploader._disk_export_target` 同源）。

    `mount_point` 非空时用它，而不是配置里那条 —— 这是 v6.5.1 的关键改动：
    判定口径（标识文件、空间余量、体积预判）与导出侧**逐字共用同一个类**，
    所以"能导出却录不进去"这种自相矛盾不会因为多块盘而重新出现。
    """
    settings = disk_settings(config)
    return DiskExportTarget(
        mount_point=mount_point or settings.get("mount_point", ""),
        volume_id_file=settings.get("volume_id_file", ".nerv_volume"),
        require_volume_id=bool(settings.get("require_volume_id")),
        min_free_margin_mb=int(settings.get("min_free_margin_mb", 2048)),
        warn_file_bytes=int(settings.get("warn_file_bytes", 3_800_000_000)),
    )

def _disk_target_for_mount(config, mount: MountedVolume,
                          folder: Optional[Path] = None) -> RecordTarget:
    """把一块枚举到的外接盘变成一个候选落点，逐级给出**可照做**的原因。

    判定顺序刻意是「先说最可能的原因」：容量太小 → 缺标识文件 → 空间余量不足。
    现场看到的是"为什么不能选"以及"下一步做什么"，而不是一句 `not available`。
    """
    disk = _disk_target_object(config, str(mount.path))
    path = Path(mount.path) / disk_subpath(read_serial(config), folder)
    key = disk_target_key(mount.path)
    label = f"移动硬盘（{mount.label}）"

    if not mount.capacity_ok:
        return RecordTarget(
            key, label, path, False,
            f"容量不足（{_size(mount.total_bytes)}，数据盘至少要 {_size(MIN_DISK_BYTES)}）",
            mount)
    # 枚举认得出它是外接设备，但**它现在是不是还挂着**是另一件事：
    # 目录留在那儿而盘已经拔了 / 卸载了，照样能写进去 —— 写进去的是本机磁盘上的
    # 一个普通目录，而界面显示"移动硬盘"。所以这一层不能省。
    if not disk.is_mounted():
        return RecordTarget(key, label, path, False,
                            f"未检测到移动硬盘（{mount.path} 不再是已挂载的设备目录，"
                            f"盘可能已拔出）", mount)
    if disk.require_volume_id and not disk.has_volume_marker():
        # 原因里**不带完整路径**：这一句要放进「录制设置」的提示行，太长会多占一行
        # （实测第 3 行会被下一行的标签压住）。挂载点本身在导出面板里看得见；
        # 需要完整路径的场合是起录失败的那个错误框，那里再补。
        marker = Path(disk.volume_id_file).name if disk.volume_id_file else ""
        return RecordTarget(key, label, path, False,
                            f"该盘不是数据盘（缺标识文件 {marker}；挂载点 {mount.path}）",
                            mount)
    # 空间余量：与导出侧 `check_free_space` 同一口径（同一个类的同一个余量），
    # 免得"导出说够、录制说不够"。
    if mount.free_bytes and mount.free_bytes < disk.min_free_margin:
        return RecordTarget(
            key, label, path, False,
            f"可用空间不足（剩 {_size(mount.free_bytes)}，需要至少 "
            f"{_size(disk.min_free_margin)} 余量）", mount)
    return RecordTarget(key, label, path, True, "", mount)


def disk_record_targets(config, folder: Optional[Path] = None) -> List[RecordTarget]:
    """当前插着的**每一块**外接盘各给一个候选（顺序见 `list_removable_mounts`）。

    没插盘时返回一个"占位候选"，`available=False` 且原因写明"未检测到移动硬盘
    （插上即自动识别）"。**刻意保留这个占位项而不是返回空列表**：界面上
    "移动硬盘"这一项一直在、只是置灰并写着原因，操作员一眼知道有这回事；
    如果整项消失，就得先猜"是不是插了但没认出来"。
    """
    settings = disk_settings(config)
    configured = str(settings.get("mount_point", "") or "")
    volumes = list_removable_mounts(configured)
    if not volumes:
        reason = ("未检测到移动硬盘（插上即自动识别）" if configured
                  else "未检测到移动硬盘（已启用自动识别，无需配置挂载点）")
        return [RecordTarget(DISK_TARGET, "移动硬盘", Path(configured or ""),
                             False, reason)]
    return [_disk_target_for_mount(config, vol, folder) for vol in volumes]


def disk_record_target(config, folder: Optional[Path] = None) -> Optional[RecordTarget]:
    """**可用的**外接盘候选里取第一个（没有则 None）。

    只保留这个"取第一个"的入口，是因为有些地方只需要一个"有没有可用盘"的答案
    （默认选中、顶栏的状态提示），不需要知道有几块。界面要列出全部时用
    `disk_record_targets`。
    """
    for target in disk_record_targets(config, folder):
        if target.available:
            return target
    return None


def local_record_target(config) -> RecordTarget:
    """本机数据盘：配置里的 `bag_dir`（**不含会话覆盖** —— 否则选项会跟着上次的选择漂）。"""
    if config is None:
        path = Path("/var/nerv/bag")
    else:
        path = Path(config.get_configured_bag_dir())
    return RecordTarget(LOCAL_TARGET, "本机数据盘", path, True, "")


def found_disk_choices(config) -> List[Tuple[str, str]]:
    """给「检测到的移动硬盘」那个下拉用的选项：`[(显示文字, 挂载点路径)]`。

    为什么放在逻辑层而不是界面里：**"下拉里显示什么"和"怎么枚举"是同一件事的两半**。
    放在界面里就会出现两份口径 —— 而界面那半一旦改文案，逻辑那半的守卫就抓不到。
    选项**刻意不含完整路径**（路径可能很长，会把那一行撑得很宽，面板宽度是固定的）。

    没插盘时给一项 `("未检测到移动硬盘（插上即自动识别）", "")`：整项消失的话，
    操作员分不清是"没插盘"还是"没认出来"。返回空路径表示"这一项不可选"。
    """
    configured = str(disk_settings(config).get("mount_point", "") or "")
    try:
        volumes = list_removable_mounts(configured)
    except Exception as exc:                      # noqa: BLE001 —— 枚举失败不该弄坏面板
        logger.warning(f"枚举移动硬盘失败：{exc}")
        volumes = []
    if not volumes:
        return [("未检测到移动硬盘（插上即自动识别）", "")]
    return [(vol.summary(), str(vol.path)) for vol in volumes]


def record_targets(config, folder: Optional[Path] = None) -> List[RecordTarget]:
    """候选落点列表，顺序固定：**本机在前、所有外接盘在后**（界面按此顺序列出）。"""
    return [local_record_target(config)] + disk_record_targets(config, folder)


def target_by_key(config, key: str, folder: Optional[Path] = None) -> Optional[RecordTarget]:
    """按键取候选；键不认识返回 None（界面据此回退到默认）。

    **兼容裸 `"disk"`**：v6.5.1 之前界面产生的键就是 `"disk"`（只有一个候选）。会话
    里若还留着这样的旧键（用户先在旧界面选过、再换包），按"第一个外接盘候选"处理，
    而不是让界面回落到本机 —— 那会让用户看到"我明明选了移动硬盘，怎么变本机了"。
    """
    for target in record_targets(config, folder):
        if target.key == key:
            return target
    if str(key) == DISK_TARGET:
        for target in record_targets(config, folder):
            if target.mount is not None:
                return target
    return None


def default_record_target_key(config, folder: Optional[Path] = None) -> str:
    """默认选中哪个：**插了可用的数据盘就用它，否则本机**。

    这是"后续采集可能直接存到移动硬盘"这条需求的自动半边 —— 现场把盘插上就不用
    再点一下；拔了也自动回到本机（**不会因为配置指着盘而起录失败**，因为选择根本
    不写回配置）。

    返回的是 `RecordTarget.key`（外接盘项是 `disk:<挂载点>`，界面用它精确回选）。
    """
    disk = disk_record_target(config, folder)
    return disk.key if disk is not None else LOCAL_TARGET
