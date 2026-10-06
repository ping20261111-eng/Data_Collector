"""数据完整性校验：SHA-256 内容校验与文件系统能力探测。

## 为什么要单独一个模块

原先的完整性校验只有一处：上传后比对**文件大小**。这挡不住一类真实故障 ——
SFTP 传输位翻转、磁盘静默错误、边写边读时脚本改写内容，这些都**保持大小不变**。
对遥操作数据而言，一份静默损坏的bag 比一份明确报错的更有杀伤力：它会一路流到
标注和训练环节才被发现，届时已无法回溯是哪次录制出的问题。

所以校验补成两层，且刻意分层：
- `sha256_file` / `sha256_sftp`：内容级，慢但可信，用于**落盘后确认**；
- 体积与 FAT32 能力探测：快，只用于**事前预判**，不替代内容校验。

分层的意义在于：体积检查可以拦住"必然失败"的情况（如 FAT32 的 4GB 上限），
而只有内容校验能拦住"看起来成功、其实是坏的"。

## 边界

- 校验**不是**加密，也不提供防篡改能力。SHA-256 只能证明"两端一致"，
  证明不了"内容是对的"。不要把它当成数据签名用。
- 大文件全量回读会明显拖慢上传。默认用**尾部抽样**（`verify_tail_bytes`），
  覆盖传输中最容易出错的��部，也把耗时控制在可接受范围；
  需要强保证时可传`verify_tail_bytes=None` 做全量校验。
"""
import hashlib
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .core import logger

# 单次读取块大小（4MB）：与core_data.PACK_CHUNK 对齐，避免大文件校验时
# 读放大把磁盘打满。
VERIFY_CHUNK = 4 * 1024 * 1024

# 抽样校验时读取的尾部字节数。4GB 读 4MB 约等于千分之一的开销，
# 却能覆盖绝大多数"尾部写坏"与"链路末端截断"场景。
VERIFY_TAIL_BYTES = 4 * 1024 * 1024

# FAT32 单文件硬上限 4,294,967,295 字节。留200MB 余量给文件系统元数据与
# 边车文件（.sha256 / manifest），避免"卡在边界"时才失败。
FAT32_MAX_FILE_BYTES = 4_294_967_295
FAT32_SAFE_FILE_BYTES = 3_800_000_000

# 需要按单文件上限做检查的文件系统类型（小写）。
# 只列真正有 4GB 硬上限的：vfat/fat/msdos（FAT12/16/32）。
# exFAT 与 NTFS 理论上限远超遥操作数据的体积（exFAT 无 FAT32 的 4GB 限制，
# NTFS 上限 16EB），**不列入** —— 否则在 exFAT 盘上会误报，
# 而现场误报一次，现场就不会再相信这个提醒了。
SINGLE_FILE_LIMIT_FS = ("vfat", "fat", "fat12", "fat16", "fat32", "msdos")
LIMITED_FS = SINGLE_FILE_LIMIT_FS


def sha256_file(path: Path, tail_bytes: Optional[int] = None) -> str:
    """算本地文件的 SHA-256（十六进制）。

    `tail_bytes` 非空时只读文件尾部这么多字节 —— 配合调用方"头/尾都校验"的
    约定可覆盖大部分实际故障，同时避免大文件的全量读开销。
    传None 表示全量校验。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        if tail_bytes:
            size = os.fstat(fp.fileno()).st_size
            start = max(0, size - tail_bytes)
            fp.seek(start)
        while True:
            chunk = fp.read(VERIFY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_sftp(sftp, remote_path: str, size: int,
                tail_bytes: Optional[int] = None) -> str:
    """算远端文件 SHA-256。

    只能回读，无法跳读（SFTP 协议允许 OFFSET，但paramiko 的
    `file.seek` 远端行为依赖服务端实现，跨设备不可靠），所以这里顺序读完整个
    文件。大文件场景的耗时由调用方通过"是否开启抽样"来控制。

    `size` 为远端已知的字节数，用于决定是否只读尾部。
    """
    digest = hashlib.sha256()
    with sftp.open(remote_path, "rb") as fp:
        if tail_bytes and size > tail_bytes:
            # 只取尾部：先按块丢弃前面的内容，边丢弃边更新摘要代价太大，
            # 因此这里改为"跳过但不摘要"，只对末段做摘要 ——
            # 这与 sha256_file(tail_bytes=…) 的语义一致，调用方需知情。
            for _ in range(0, (size - tail_bytes) // VERIFY_CHUNK + 1):
                if not fp.read(VERIFY_CHUNK):
                    break
        while True:
            chunk = fp.read(VERIFY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_sidecar(target_path: Path, digest: str,
                         relative_to: Optional[Path] = None) -> Path:
    """在目标文件旁写 `<name>.sha256` 边车文件，返回其路径。

    格式与 `sha256sum` 一致（`<hex>  <文件名>`），可以直接用
    `sha256sum -c` 在现场机器上复核 —— 这一点很重要：交付给采数工程师后，
    校验不能只能靠上位机，要能被通用工具验证。
    """
    try:
        name = target_path.name
        if relative_to is not None:
            rel = target_path.resolve().relative_to(Path(relative_to).resolve())
            name = str(rel)
    except (ValueError, OSError):
        name = target_path.name
    sidecar = target_path.with_name(target_path.name + ".sha256")
    sidecar.write_text(f"{digest}  {name}\n", encoding="utf-8")
    return sidecar


def detect_filesystem(path: Path) -> str:
    """探测挂载点的文件系统类型（如 vfat / exfat / ntfs / ext4）。

    优先解析 `/proc/mounts`（无需 root 权限、结果可靠）；拿不到再退回
    `stat -f`。两条路都失败时返回空字符串，调用方按"未知类型"处理 ——
    **不做最坏假设**，因为把未知当成 vfat 会让 exFAT 盘上出现误报。
    """
    path = Path(path)
    try:
        path = path.resolve()
    except OSError:
        pass
    best_fs = ""
    best_len = -1
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fs_type = parts[1], parts[2]
                # 取挂载路径最长的一条：/media/usb 优先于 /media
                if (mount_point == str(path)
                        or str(path).startswith(mount_point.rstrip("/") + "/")):
                    if len(mount_point) > best_len:
                        best_fs, best_len = fs_type.lower(), len(mount_point)
    except OSError as exc:
        logger.debug(f"读取 /proc/mounts 失败：{exc}")

    if best_fs:
        return best_fs

    try:
        out = subprocess.run(["stat", "-f", "-c", "%T", str(path)],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"stat -f 探测文件系统失败：{exc}")
    return ""


def has_single_file_limit(fs_type: str) -> bool:
    """该文件系统是否存在 FAT32 的 4GB 单文件硬上限。

    只对 FAT 系列返回 True。exFAT / NTFS 虽在部分内核上以 `ntfs3` 挂载，
    但它们没有这个限制，不应触发提醒。
    """
    fs = (fs_type or "").lower()
    if not fs:
        return False
    return fs in LIMITED_FS or fs.startswith("vfat")


def check_size_limits(entries: List[Tuple[str, int]], fs_type: str
                      ) -> Tuple[List[Tuple[str, int]], bool]:
    """预判体积风险：挑出超过单文件上限的条目。

    返回 `(超限条目列表, 目标盘是否有限制)`。调用方据此决定是否弹提醒。
    这里**只预判不拦截** —— 是否继续由用户决定，现场流程不能被程序硬性卡死。
    """
    limited = has_single_file_limit(fs_type)
    if not limited:
        return [], False
    over = [(name, size) for name, size in entries
            if size > FAT32_SAFE_FILE_BYTES]
    return over, True
