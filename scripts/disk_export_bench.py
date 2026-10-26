#!/usr/bin/env python3
"""移动硬盘导出测速 / 瓶颈诊断（现场自查用，独立可跑，不需要 ROS、不需要界面）。

## 为什么要有这个脚本

现场反馈"传到硬盘太慢"时，"慢"可能来自**四个完全不同的地方**，而它们的处理
方式互不相干 —— 猜错方向就会去优化一个本来就不慢的环节：

| 可能原因 | 典型速度 | 处理方式 |
| --- | --- | --- |
| 插在 USB 2.0 口 / USB 2.0 hub 上 | 30~40 MB/s | 换口（最容易被忽略，也最便宜） |
| 盘是 2.5" 机械（如 Seagate Expansion） | 80~120 MB/s | 这是物理上限，只能换盘或直写 |
| 盘是 exFAT / NTFS（FUSE 实现） | 常只有 ext4 的 1/3 | 换文件系统 |
| `upload.verify_content: true`（写完回读校验） | 写速凭空**减半** | 改成 `digest` 模式 |

本脚本把这四件事一次量出来，并且**按实测数据**给出该动哪一项。

## 它会写盘吗

会 —— 只有测速那一步，在**目标盘根目录**写一个
`.nerv_speedtest_<pid>.tmp`（默认 256 MB，`--size-mb` 可调），
测完立即删除。用 `--no-write` 可以只做只读诊断（识别盘、链路、文件系统、配置）。
绝不碰盘上任何既有文件。

## 用法

    python3 scripts/disk_export_bench.py                 # 自动找盘
    python3 scripts/disk_export_bench.py --size-mb 512   # 测更久更准
    python3 scripts/disk_export_bench.py --no-write      # 不写盘
    python3 scripts/disk_export_bench.py --mount /media/nerv/Seagate
"""
from __future__ import annotations

import argparse
import mmap
import os
import shutil
import sys
import time
from pathlib import Path

#: 让它能 `python3 scripts/disk_export_bench.py` 直接跑（此时 sys.path[0] 是
#: `scripts/`，不含项目根）。必须放在**模块导入期**：`--mount` 显式指定时
#: `find_mount()` 会提前返回，若把这段塞在那里面，后面的生产代码导入就会失败。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: 测速块大小。必须是 4096 的倍数才能满足 O_DIRECT 的对齐要求
BLOCK = 4 * 1024 * 1024

#: 判断"快 / 一般 / 慢"的分界（MB/s）。取自常见硬件的实测区间：
#: USB2 ≈ 30~40、2.5" 机械 ≈ 80~120、USB3 SSD ≈ 300~450
THRESHOLDS = (
    (250, "很快", "USB3 SSD 或更好的盘"),
    (150, "正常", "USB3 机械盘的上限区间"),
    (70, "偏慢", "低于 USB3 机械盘的应有水平"),
    (0, "很慢", "典型 USB 2.0 速率"),
)


def human_mb(value: float) -> str:
    return f"{value:,.1f} MB/s"


def verdict_for(mbps: float) -> tuple:
    for floor, label, note in THRESHOLDS:
        if mbps >= floor:
            return label, note
    return "很慢", ""


# ---------- 只读诊断 ----------

def find_mount(explicit: str) -> str:
    """优先用命令行给的；否则用程序自己的自动枚举（与导出走同一套判据）。"""
    if explicit:
        return explicit
    try:
        from data_collector.disk_export import list_removable_mounts
        volumes = list_removable_mounts()
        if volumes:
            return str(volumes[0].path)
    except Exception as exc:                      # noqa: BLE001
        print(f"  自动识别不可用（{exc}），改用命令行 --mount 指定")
    return ""


def mount_details(mount: str) -> dict:
    """从 /proc/mounts 取这个挂载点的真实设备、文件系统与挂载选项。

    比 `df -T` 可靠：`df` 对 FUSE 类文件系统（ntfs-3g）显示的名字容易误导，
    而 `/proc/mounts` 里的 `fuseblk` 正是"跑在用户态"的判据 —— 那才是慢的原因。
    """
    info = {"device": "", "fstype": "", "options": ""}
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    best = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # 挂载点里的空格会被写成 \040
        point = parts[1].replace("\\040", " ")
        if mount == point or (mount.startswith(point.rstrip("/") + "/") and point != "/"):
            if best is None or len(point) > len(best[1]):
                best = (parts, point)
    if best:
        parts, _point = best
        info["device"] = parts[0]
        info["fstype"] = parts[2]
        info["options"] = parts[3]
    return info


def usb_link_speed(device: str) -> str:
    """USB 链路协商速率（Mbps）。走不通返回空串。

    为什么不用 `lsusb -t`：它输出到 stderr 且格式随版本变化，解析脆；
    而 sysfs 的 `speed` 是内核给的确定值。路径是
    `/sys/block/sdb -> …/usb1/1-2/1-2:1.0/host6/…/block/sdb`，向上 8 层内
    一定会经过设备节点（`speed` 在该层）或 root hub。
    """
    name = Path(device).name
    if not name:
        return ""
    try:
        node = Path(f"/sys/block/{name}").resolve()
    except OSError:
        return ""
    current = node
    for _ in range(9):
        current = current.parent
        marker = current / "speed"
        try:
            if marker.is_file():
                return marker.read_text().strip()
        except OSError:
            return ""
        if current == current.parent:
            break
    return ""


def describe_link(speed: str) -> str:
    if not speed:
        return ("读不到（不是 USB 设备，或这台机器的内核没暴露 speed）"
                " —— 可手动执行 `lsusb -t` 看，速率 5000M 才是 USB3")
    try:
        value = float(speed)
    except ValueError:
        return speed
    if value >= 10000:
        return f"{value:g} Mbps（USB 3.1/3.2，理论上限约 {value / 8 / 1.15:.0f} MB/s）"
    if value >= 5000:
        return f"{value:g} Mbps（USB 3.0/3.1 Gen1，理论上限约 {value / 8 / 1.15:.0f} MB/s）"
    if value >= 480:
        return f"{value:g} Mbps（**USB 2.0** —— 实际约 35~40 MB/s，这是硬上限）"
    return f"{value:g} Mbps"


def report_config(mount: str) -> dict:
    """把与速度有关的配置项取出来（只看，不改）。"""
    result = {"export_target": "", "verify_content": False, "verify_mode": "", "config_path": ""}
    root = Path(__file__).resolve().parents[1]
    candidates = [root / "configs" / "app_config.yaml",
                  Path.home() / ".config" / "Data_Collector" / "app_config.yaml"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:                         # noqa: BLE001
            continue
        upload = data.get("upload") or {}
        disk = data.get("disk_export") or {}
        result["config_path"] = str(path)
        result["export_target"] = str(upload.get("export_target", "server"))
        result["verify_content"] = bool(upload.get("verify_content", False))
        result["verify_mode"] = str(disk.get("verify_mode", "") or "")
        break
    return result


def effective_verify_mode(cfg: dict) -> str:
    """实际生效的档位 —— **直接问生产代码**，不在这里另写一套规则。

    这里曾经自己实现过一遍推导（"没配就按旧字段映射"），看起来只有三行、
    没必要复用。但脚本和程序对同一份配置给出不同结论时，现场会照着脚本的
    结论去改配置，改完发现耗时没变 —— 这种"诊断工具本身说错"最难排查。
    规则只留一份，`resolve_disk_verify_mode` 就是那份。
    """
    try:
        from data_collector.core import resolve_disk_verify_mode
    except Exception as exc:                      # noqa: BLE001
        print(f"    ⚠ 读不到程序的档位规则（{exc}），按「不校验」保守显示")
        return "off"
    return resolve_disk_verify_mode(
        {"verify_mode": cfg.get("verify_mode", "")},
        bool(cfg.get("verify_content")),
    )


# ---------- 测速 ----------

def measure(mount: Path, size_mb: int) -> dict:
    """在目标盘上做一次顺序写 + 顺序读。

    写用 O_DIRECT（绕开页缓存，测的是盘的真实写入能力）；不支持时回落到
    "写完 fsync"，并在结果里标注 —— 后者会被盘的 RAM 缓存美化，读数偏乐观。
    """
    path = mount / f".nerv_speedtest_{os.getpid()}.tmp"
    total = size_mb * 1024 * 1024
    buffer = mmap.mmap(-1, BLOCK)             # 匿名映射，页对齐 → 满足 O_DIRECT
    result = {"write_mbps": 0.0, "read_mbps": 0.0, "direct": False, "error": ""}
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        direct = hasattr(os, "O_DIRECT")
        fd = -1
        if direct:
            try:
                fd = os.open(path, flags | os.O_DIRECT, 0o644)
            except OSError:
                direct = False
        if not direct:
            fd = os.open(path, flags, 0o644)
        result["direct"] = direct
        try:
            started = time.monotonic()
            written = 0
            while written < total:
                chunk = min(BLOCK, total - written)
                os.write(fd, buffer[:chunk])
                written += chunk
            if not direct:
                os.fsync(fd)                  # 不走 O_DIRECT 时，只有 fsync 之后才算落盘
            result["write_mbps"] = written / 1e6 / max(time.monotonic() - started, 1e-9)
        finally:
            os.close(fd)

        # 读回：同样优先 O_DIRECT，否则清掉页缓存再读
        rflags = os.O_RDONLY
        rfd = -1
        if direct:
            try:
                rfd = os.open(path, rflags | os.O_DIRECT)
            except OSError:
                rfd = os.open(path, rflags)
        else:
            rfd = os.open(path, rflags)
            try:
                os.posix_fadvise(rfd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        try:
            started = time.monotonic()
            read = 0
            while True:
                n = len(os.read(rfd, BLOCK))
                if n == 0:
                    break
                read += n
            result["read_mbps"] = read / 1e6 / max(time.monotonic() - started, 1e-9)
        finally:
            os.close(rfd)
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        buffer.close()
        try:
            path.unlink()
        except OSError:
            pass
    return result


def estimate(mbps: float, gigabytes: float, mode: str) -> str:
    """按实测速度推算一次典型采集的导出耗时（含校验回读那一遍）。"""
    if mbps <= 0:
        return "（无法推算）"
    write_sec = gigabytes * 1000 / mbps
    if mode == "readback":
        # 写完再整份读回来算摘要：读速通常略高于写速，这里按同速保守估
        write_sec *= 2
        note = "写 + 回读校验"
    elif mode == "digest":
        note = "写（校验在复制时顺带算完，不额外读盘）"
    else:
        note = "写（不校验）"
    return f"{gigabytes:g} GB 约 {write_sec / 60:.1f} 分钟 · {note}"


def main() -> int:
    parser = argparse.ArgumentParser(description="移动硬盘导出测速 / 瓶颈诊断")
    parser.add_argument("--mount", default="", help="挂载点；留空自动识别")
    parser.add_argument("--size-mb", type=int, default=256, help="测速写入量（默认 256MB）")
    parser.add_argument("--no-write", action="store_true", help="只做只读诊断，不写盘")
    args = parser.parse_args()

    print("=" * 68)
    print("移动硬盘导出 · 速度诊断")
    print("=" * 68)

    mount = find_mount(args.mount)
    if not mount:
        print("\n没有找到移动硬盘。请插上盘，或用 --mount 指定挂载点。")
        return 2
    mount_path = Path(mount)
    print("\n[1] 目标盘")
    print(f"    挂载点 : {mount}")
    info = mount_details(mount)
    if info["device"]:
        print(f"    设备   : {info['device']}")
        print(f"    文件系统: {info['fstype']}    挂载选项: {info['options']}")
        if info["fstype"] in ("fuseblk", "fuse", "fuse.ntfs", "fuse.ntfs-3g"):
            print("    ⚠ 跑在 FUSE（用户态）上 —— NTFS 经 ntfs-3g 写入常只有内核"
                  "驱动的 1/3 速度。换 exFAT 或 ext4 能明显提速。")
        elif info["fstype"] in ("vfat", "exfat"):
            print("    提示: FAT 系列不记 POSIX 权限、写入偏慢，但比 ntfs-3g 好。")
        speed = usb_link_speed(info["device"])
        print(f"    USB 链路:  {describe_link(speed)}")
        try:
            usage = shutil.disk_usage(mount)
            print(f"    剩余空间: {usage.free / 1e9:.1f} GB / 共 {usage.total / 1e9:.1f} GB")
        except OSError as exc:
            print(f"    剩余空间: 读不到（{exc}）")
    else:
        print("    ⚠ /proc/mounts 里找不到这个挂载点（路径写错了？）")

    print("\n[2] 相关配置")
    cfg = report_config(mount)
    if not cfg["config_path"]:
        print("    没读到 app_config.yaml，按默认值判断")
    else:
        print(f"    配置文件: {cfg['config_path']}")
    mode = effective_verify_mode(cfg)
    print(f"    export_target : {cfg['export_target'] or '（默认 server）'}")
    print(f"    verify_content: {cfg['verify_content']}")
    print(f"    verify_mode   : {mode}"
          + ("   ← 复制时算摘要，不回读（快）" if mode == "digest" else "")
          + ("   ← 写完回读整份校验（慢一倍）" if mode == "readback" else "")
          + ("   ← 不校验（最快，但没有数据指纹）" if mode == "off" else ""))

    if args.no_write:
        print("\n（--no-write：跳过测速）")
        return 0

    print(f"\n[3] 顺序读写测速（写 {args.size_mb} MB 到盘上，完成后立即删除）")
    measured = measure(mount_path, args.size_mb)
    if measured["error"]:
        print(f"    测速失败：{measured['error']}")
        return 1
    print(f"    方式   : {'O_DIRECT（绕开页缓存，读数可信）' if measured['direct'] else 'fsync 回落（读数可能被盘的缓存美化）'}")
    label, note = verdict_for(measured["write_mbps"])
    print(f"    顺序写 : {human_mb(measured['write_mbps'])}   {label} —— {note}")
    print(f"    顺序读 : {human_mb(measured['read_mbps'])}")

    print("\n[4] 按这个速度推算")
    for gb in (10, 30, 60):
        print(f"    {estimate(measured['write_mbps'], gb, mode)}")

    print("\n[5] 结论")
    write_mbps = measured["write_mbps"]
    links = usb_link_speed(info["device"]) if info["device"] else ""
    advice = []
    try:
        is_usb2 = links and float(links) <= 480
    except ValueError:
        is_usb2 = False
    if is_usb2:
        advice.append("① **插在 USB 2.0 上了** —— 换到蓝色的 USB3 口（或直插机身、"
                      "别经 hub/延长线）。这一项通常能把速度提到 4~8 倍，且不花钱。")
    if write_mbps < 70 and not is_usb2:
        advice.append("① 盘的写入能力明显偏低 —— 先看上面 USB 链路是否 5000M；"
                      "若已是 USB3，换一根短线/换个口再测一次（供电不足会降速）。")
    if info["fstype"] in ("fuseblk", "fuse", "fuse.ntfs", "fuse.ntfs-3g"):
        advice.append("② 文件系统是 NTFS 经 FUSE —— 换 exFAT（内核驱动）能提速，"
                      "但要先备份盘上数据再格式化。")
    if mode == "readback":
        advice.append("③ 现在开着回读校验（verify_content: true）—— 导出耗时接近翻倍。"
                      "若只是想留指纹供以后核对，改成 `disk_export.verify_mode: digest`："
                      "复制时顺带算 SHA-256 并写 `SHA256SUMS`，不回读，速度回来一倍。")
    elif mode == "off":
        advice.append("③ 现在不做校验。若想留数据指纹（以后能 `sha256sum -c` 核对）"
                      "又不牺牲速度，设 `disk_export.verify_mode: digest` —— 只多约 9% CPU，"
                      "不额外读盘。")
    if write_mbps >= 150:
        advice.append("④ 盘本身不慢，软件侧也已经到位（拷贝循环实测 2.2 GB/s，远高于盘速）。"
                      "若仍觉得久，唯一的大头是**数据量**本身 —— 可以考虑把录制直接落在移动硬盘上"
                      "（省掉整次拷贝，但要先确认这块盘的持续写速 ≥ 录制码率的 2 倍）。")
    if not advice:
        advice.append("未发现明显异常。")
    for line in advice:
        print(f"    {line}")
    print()

    print("    参考：程序里的拷贝循环本身在内存里能跑 2.2 GB/s，"
          "加 SHA-256 也有 1.1 GB/s ——")
    print("    所以只要盘速低于 1 GB/s（移动硬盘都是），瓶颈就一定在盘或链路上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
