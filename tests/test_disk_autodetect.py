"""移动硬盘自动识别 + 采集统计的数据选择 —— v6.5.1。

## 这两份测试要钉住什么

### 一、枚举（`list_removable_mounts`）

起因是一个**现场真的发生过**的问题：`app_config.yaml` 里 `mount_point` 写着
`/media/nerv/DE8A-9634`，而实际插着的盘挂在 `/media/nerv/Seagate`
（exFAT，1.8T）—— 程序报"未检测到移动硬盘"，而盘就在眼前。

所以判据必须钉死这几条，且每一条都**有对应的反例**：
1. `st_dev` 与 `/` 不同（独立块设备，不是本机盘里的目录）；
2. sysfs 设备路径含 `/usb`（不是 NVMe/SATA 内置盘）；
3. 排除伪文件系统（本机 SNAP 那 20 多条 squashfs 不排除会被全当成盘）；
4. 排除本机系统挂载点（`/boot/efi` 是独立分区，但仍在本机盘上）。

**为什么桩 `list_removable_mounts` 而不是 `is_mounted`**：判定入口换成了枚举，
只桩后者的话"插着盘"这个前提根本不成立。而且更关键 —— **跑测试的机器可能真插着盘**，
不桩枚举的话"没插盘"的用例会**反向失效**（换台机器结果就不一样）。

### 二、数据选择（`BagPickerDialog`）

`bag_rate_stat.py` 只吃**一个** bag 路径，选错的表现是"脚本跑起来才报错"。
所以钉住：非 bag 目录根本不列、默认选中最新那一个、选中前按钮禁用、空态说清去哪找。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from data_collector import disk_export as de                      # noqa: E402
from data_collector.core_data import (BagCandidate, DataUploader,   # noqa: E402
                                      describe_bag)

MARKER = ".nerv_volume"
ROOT_DEV = os.stat("/").st_dev


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _mount_line(path: str, source: str, fs: str = "ext4") -> str:
    """造一条 `/proc/mounts` 的行（顺序与真实格式一致）。"""
    return f"{source} {path} {fs} rw,relatime 0 0\n"


def _fake_usb(name: str) -> str:
    """一个"看起来在 USB 总线上"的块设备名（测试里不查真 sysfs）。"""
    return name


@pytest.fixture
def mounts(monkeypatch, tmp_path):
    """桩出"当前插着哪些盘"，让枚举结果完全可预测。

    只桩三处，且**只对桩出来的挂载点生效**（别把整台机器的 `os.stat` /
    `open` 换掉 —— 换掉之后 `ConfigManager` 连配置文件都读不出来，
    实测过：报 `TypeError: string indices must be integers`）。

    - `/proc/mounts` 的内容：用一个内存文本代替；
    - `os.stat(挂载点).st_dev`：桩成"独立设备"（`ROOT_DEV + 1`），
      或者由用例给（用来验"与 / 同设备必须排除"）；
    - `_is_usb_block`：桩成"按用例给的设备名集合"。

    **为什么桩 `_is_usb_block` 而不是建真挂载点**：真挂载点要 root 权限，
    而这个判据的价值恰恰是"它能不能在不依赖环境的情况下被验"。

    ## 每次调用 `install()` 都会在 `tmp_path` 下建出对应的真实目录
    （枚举里 `shutil.disk_usage` / `os.stat` 需要路径存在），所以用例只管
    写挂载行、拿不到目录也没关系。
    """
    state = {"lines": [], "st_dev": ROOT_DEV + 1, "usb": set(), "made": [],
             "total_bytes": None, "sizes": {}}

    def install(lines, st_dev=None, usb_blocks=(), total_bytes=None, sizes=None):
        state["lines"] = list(lines)
        state["total_bytes"] = total_bytes
        state["sizes"] = sizes or {}
        if st_dev is not None:
            state["st_dev"] = st_dev
        state["usb"] = set(usb_blocks)
        for line in state["lines"]:
            parts = line.split()
            if len(parts) < 2:
                continue
            # 挂载点必须真存在：`os.stat` 与 `disk_usage` 都要求路径可用。
            # 名字里的 `/` 换成 tmp_path 下的子目录，保持互不相同。
            real = tmp_path / ("mnt_" + str(len(state["made"])))
            real.mkdir(parents=True, exist_ok=True)
            state["made"].append((parts[1], real))
        return state

    class _Shim:
        """只把 `st_dev` 换成桩值的 stat 结果。"""

        def __init__(self, wrapped, dev):
            object.__setattr__(self, "_wrapped", wrapped)
            object.__setattr__(self, "st_dev", dev)

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    real_stat = os.stat

    def _redirect(path):
        """把桩出来的挂载点路径映射到 `tmp_path` 下的真目录。

        **必须先映射再 stat**：生产代码是 `os.stat(mount)` 然后
        `except OSError: continue`，而桩出来的挂载点（`/media/robot/…`）
        在真文件系统上并不存在 —— 直接 stat 会抛 FileNotFoundError，
        那行 `continue` 于是把这一条当成"目录不存在"跳过，枚举结果恒为空。
        第一版夹具就是这个错，症状是"桩了却什么都枚举不到"。
        """
        text = str(path)
        for mount, real in state["made"]:
            if text == mount:
                return real
            if text.startswith(mount.rstrip("/") + "/"):
                return real / text[len(mount):].lstrip("/")
        return None

    def fake_stat(path, *a, **k):
        target = _redirect(path)
        if target is not None:
            return _Shim(real_stat(target, *a, **k), state["st_dev"])
        return real_stat(path, *a, **k)

    real_disk_usage = de.shutil.disk_usage

    def fake_disk_usage(path):
        target = _redirect(path)
        usage = real_disk_usage(target if target is not None else path)
        # 用例要能直接指定"这块盘多大" —— tmp_path 下的目录只有几 MB，
        # 凑不出"4G 的 U 盘"和"8G 的数据盘"这两种量级。
        # **`sizes` 按挂载点给**（而不是全局一个值）：两块盘容量相同时，
        # 排序其实由挂载点名字决定，"容量大的在前"那条守卫就成了假的
        # （实测：去掉 `-total_bytes` 后三处排序守卫全都还绿）。
        forced = None
        sizes = state.get("sizes") or {}
        for mount, _real in state["made"]:
            if str(path) == mount and mount in sizes:
                forced = sizes[mount]
                break
        if forced is None:
            forced = state.get("total_bytes")
        if forced and target is not None:
            import shutil as _sh
            return _sh._ntuple_diskusage(forced, forced // 2, forced)
        return usage
    import io
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/mounts":
            return io.StringIO("".join(state["lines"]))
        return real_open(path, *a, **k)

    # 桩的是 `_usb_sysfs_path`（**取 sysfs 路径**这一步），不是 `_is_usb_block`
    # （`"/usb" in 路径` 这个判据本身）。判据一旦被打桩，改生产代码里那两行就
    # 影响不到任何用例，守卫就成了假的 —— 实测过：三种"改坏"全都还能绿。
    # 判据本身由 `test_usb_detection_uses_sysfs_path` 单独验。
    monkeypatch.setattr(de.os, "stat", fake_stat)
    monkeypatch.setattr(de.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(
        de, "_usb_sysfs_path",
        lambda block: (
            f"/sys/devices/pci0000:00/0000:00:14.0/usb2/2-3/2-3:1.0/{block}"
            if block in state["usb"]
            else f"/sys/devices/pci0000:00/0000:00:1d.0/ata1/{block}"))
    monkeypatch.setattr(
        de, "_parent_block_device",
        lambda source: os.path.basename(source).rstrip("0123456789")
        or os.path.basename(source))
    return install


def _config(tmp_path):
    """造一份最小配置（枚举那几个用例都要用真 `ConfigManager`）。"""
    from data_collector.core import ConfigManager
    path = tmp_path / "app_config.yaml"
    path.write_text(
        "workspace_root: /tmp\nteleop_config_path: a.yaml\n"
        "arm_control_config_path: b.yaml\nbag_dir: /var/nerv/bag\n",
        encoding="utf-8")
    return ConfigManager(path)


# ---------- 枚举：判据 ----------

def test_a_usb_mount_is_found_without_any_configuration(mounts):
    """一条 USB 设备挂载 → 必须被枚举出来，**不需要配置里写任何路径**。

    这条就是现场那个问题的正面回答：配置写错（甚至根本没写）时，
    插着的盘仍然要被认出来。
    """
    mounts([_mount_line("/media/robot/Seagate", "/dev/sda1", "exfat")],
           usb_blocks={"sda"})
    volumes = de.list_removable_mounts()          # 不传 configured
    assert len(volumes) == 1
    assert str(volumes[0].path) == "/media/robot/Seagate"
    assert volumes[0].fs == "exfat"
    assert volumes[0].label == "Seagate"


def test_the_same_device_as_root_is_never_a_removable_disk(mounts):
    """与 `/` **同一设备** → 必须排除。

    这是"路径撞名"的经典陷阱：机器侧有人把挂载点配成 `/home/nerv/data`，
    于是数据其实写进了本机盘，而界面以为上了移动硬盘。
    """
    mounts([_mount_line("/home/nerv/data", "/dev/sda1")],
           st_dev=ROOT_DEV, usb_blocks={"sda"})
    assert de.list_removable_mounts() == [], "本机盘上的目录绝不能被当成移动硬盘"


def test_internal_disks_are_not_removable_even_when_mounted(mounts):
    """sysfs 路径不含 `/usb`（NVMe/SATA 内置盘）→ 排除，哪怕它挂载在非常规位置。

    **前提要造对**：`/boot/efi` 与 `/home` 是 `SYSTEM_MOUNTS`（早一步就被排除），
    所以这条另加一个**不在系统挂载点里**的内置盘位置（`/var/lib/robot`）——
    否则"去掉 USB 判定"这个变异也能通过，守卫就成了假的。
    """
    # 这几条**刻意让 st_dev 与 / 不同**（独立设备），否则它们会被"路径撞名"那条
    # 规则先排除掉，"去掉 USB 判定"这个变异照样能过 —— 守卫就成了假的。
    mounts([
        _mount_line("/", "/dev/nvme0n1p2"),
        _mount_line("/boot/efi", "/dev/nvme0n1p1", "vfat"),
        _mount_line("/home", "/dev/nvme0n1p3"),
        _mount_line("/var/lib/robot", "/dev/nvme0n1p4"),   # 独立分区上的普通目录
        _mount_line("/opt/data", "/dev/nvme0n1p5"),        # 同上，且不在系统挂载点里
    ], st_dev=ROOT_DEV + 1, usb_blocks=set())   # sysfs 里一块 USB 都没有
    assert de.list_removable_mounts() == [], (
        "不是 USB 的独立分区（内置盘）绝不能被当成移动硬盘")


def test_pseudo_filesystems_are_never_disks(mounts):
    """SNAP 那些 squashfs / tmpfs / fuse 一律排除。

    实测本机 `/proc/mounts` 有 30 多条、其中 20 多条是 SNAP 的 squashfs ——
    不排除的话枚举结果会被 `/snap/core22/2437` 之类的只读系统镜像占满。
    """
    mounts([
        _mount_line("/snap/core22/2955", "/dev/loop27", "squashfs"),
        _mount_line("/tmp/fuse", "/dev/fuse", "fuse"),
        _mount_line("/dev/shm", "tmpfs", "tmpfs"),
        _mount_line("/run/user/1000/gvfs", "portal", "fuse.gvfsd-fuse"),
    ], usb_blocks={"loop27", "fuse"})
    assert de.list_removable_mounts() == []


def test_non_device_sources_are_skipped(mounts):
    """来源不是 `/dev/*` 的（NFS、autofs…）→ 跳过。

    NFS 挂载可能真在别的机器上，但把远程目录当"移动硬盘"会让现场以为
    "拔盘了数据已经在本地" —— 而它其实还在别人机器上。
    """
    mounts([_mount_line("/mnt/nfs_data", "10.0.0.9:/data", "nfs4")],
           usb_blocks={"nfs4"})
    assert de.list_removable_mounts() == []

    # 极端情形：NFS 导出的路径里**块设备名恰好撞上**一台真 USB 盘。
    # 不造这个前提的话，去掉 `/dev/` 那道判定也抓不到（名字对得上、USB 也对得上）——
    # 实测踩过，所以这条守卫必须有"名字撞车"这一格。
    mounts([_mount_line("/mnt/sneaky", "10.0.0.9:/export/sda", "nfs4")],
           usb_blocks={"sda"})      # 削掉末尾分区号后正好是 "sda"，与真 USB 盘同名
    assert de.list_removable_mounts() == [], (
        "来源不是 /dev/* 的（哪怕块设备名撞上了真 USB 盘）也不是移动硬盘")


def test_the_configured_mount_is_marked_but_never_exclusive(mounts):
    """配置指定的那块标 `configured=True` 并排最前；**它挂不上也不影响别人**。"""
    # 刻意让「配置指定的那块」容量**更小**（BIG 只有 64G，Seagate 有 1T）：
    # 这样"配置指定排最前"只能来自 configured 这一项 ——
    # 若让两块容量相同，"容量大的在前"会把顺序排成同一个，守卫就抓不到
    # configured 被去掉这个改坏（实测过）。
    mounts([
        _mount_line("/media/robot/BIG", "/dev/sdb1"),
        _mount_line("/media/robot/Seagate", "/dev/sda1", "exfat"),
    ], usb_blocks={"sdb", "sda"},
        sizes={"/media/robot/BIG": 64 * 1024 ** 3,
               "/media/robot/Seagate": 1024 * 1024 ** 3})
    volumes = de.list_removable_mounts("/media/robot/BIG")
    assert volumes[0].total_bytes < volumes[1].total_bytes, (
        "前提：配置指定的那块必须容量更小，否则这条守卫会被容量排序意外满足")
    assert [v.label for v in volumes] == ["BIG", "Seagate"], (
        f"配置指定的那块必须排最前（哪怕它容量更小），实际 {[v.label for v in volumes]}")
    assert volumes[0].configured is True
    assert volumes[1].configured is False


def test_a_configured_mount_that_is_absent_does_not_hide_the_real_ones(mounts):
    """配置里那条路径**现在没挂着** → 其它盘照样要列出来。

    这是"枚举为主、配置为辅"的关键：配置指向的那块插上别的机器、或者那块盘拔了，
    都不能让现场"插了盘却一个都看不到"。
    """
    mounts([
        _mount_line("/media/robot/Seagate", "/dev/sda1", "exfat"),
    ], usb_blocks={"sda"})
    volumes = de.list_removable_mounts("/media/robot/DE8A-9634")   # 配置那块没插
    assert [v.label for v in volumes] == ["Seagate"]
    assert volumes[0].configured is False


def test_ordering_is_stable_by_capacity(mounts):
    """顺序稳定：配置指定的排最前，其余**容量大的在前**。

    不按挂载顺序排 —— 否则每插拔一次下拉里各项的位置就会跳，
    操作员刚要点的项跑到了别处。
    """
    # 刻意让**挂载点名字与容量相反**（叫 big 的那块只有 16G）：
    # 否则按名字排序也会得到同一个顺序，去掉容量那一项这个改坏照样能过
    # —— 实测过，所以这条守卫必须排除"名字巧合"这一种可能。
    mounts([
        _mount_line("/media/robot/big", "/dev/sdb1"),
        _mount_line("/media/robot/small", "/dev/sdc1"),
    ], usb_blocks={"sdb", "sdc"},
        sizes={"/media/robot/big": 16 * 1024 ** 3,
               "/media/robot/small": 512 * 1024 ** 3})
    volumes = de.list_removable_mounts()      # 不传 configured → 都不是"配置指定"
    assert len(volumes) == 2
    assert not any(v.configured for v in volumes), (
        "前提：两块盘都不该被标成 configured，否则排序由那一项决定")
    # **只断言标签顺序**：容量是桩出来的，"降序排列"这种自证式断言在
    # 名字与容量相反时会自己把顺序解释成对的（实测踩过）。标签顺序才是真判据。
    assert [v.label for v in volumes] == ["small", "big"], (
        f"容量大的（名为 small，实为 512G）应当在前，实际 {[v.label for v in volumes]}")
    assert volumes[0].total_bytes > volumes[1].total_bytes, "前提：容量确实一大一小"


def test_summary_does_not_contain_a_path(mounts):
    """摘要里**不许出现路径**（会撑开下拉），但盘名/格式/容量要在。"""
    mounts([_mount_line("/media/averyverylongusername/Seagate_Backup_Disk", "/dev/sda1",
                        "exfat")], usb_blocks={"sda"})
    vol = de.list_removable_mounts()[0]
    assert "/" not in vol.summary()
    assert "Seagate_Backup_Disk" in vol.summary()
    assert "exfat" in vol.summary()


def test_a_tiny_usb_stick_is_flagged_as_too_small(mounts):
    """容量 < 8G 的外接盘标成"容量不足"，不再当数据盘候选。"""
    mounts([_mount_line("/media/robot/STICK", "/dev/sda1")], usb_blocks={"sda"},
           total_bytes=4 * 1024 ** 3)          # 一只 4G 的 U 盘
    vol = de.list_removable_mounts()[0]
    assert vol.capacity_ok is False, "U 盘不该被当成数据盘（只提示不拦截）"


def test_target_key_carries_the_mount_so_two_disks_differ(mounts, tmp_path):
    """两块盘的键必须不同 —— 否则 `findData` 永远命中第一块。

    后果是"选了第二块却写到第一块"，界面完全看不出来（静默写错位置）。
    """
    mounts([
        _mount_line("/media/robot/A", "/dev/sda1"),
        _mount_line("/media/robot/B", "/dev/sdb1"),
    ], usb_blocks={"sda", "sdb"})
    mgr = _config(tmp_path)

    disks = [t for t in de.disk_record_targets(mgr) if t.mount is not None]
    assert len(disks) == 2
    keys = [t.key for t in disks]
    assert len(set(keys)) == 2, f"两块盘的键撞了：{keys}"
    assert all(k.startswith(f"{de.DISK_TARGET}:") for k in keys)
    # 按键取必须取回对的那一块
    for target in disks:
        found = de.target_by_key(mgr, target.key)
        assert found is not None and found.key == target.key
        assert found.mount is not None and found.mount.label == target.mount.label


def test_the_bare_disk_key_still_resolves_for_old_sessions(mounts, tmp_path):
    """裸 `"disk"`（v6.5.1 之前的键）仍能命中第一块盘，不让界面回落到本机。"""
    mounts([_mount_line("/media/robot/Seagate", "/dev/sda1")], usb_blocks={"sda"})
    mgr = _config(tmp_path)
    found = de.target_by_key(mgr, de.DISK_TARGET)
    assert found is not None and found.mount is not None


def test_no_disk_yields_one_disabled_placeholder(mounts, tmp_path):
    """没插盘 → 给**一个置灰的占位项**，不是空列表。

    空列表会让操作员分不清"没插盘"还是"没认出来"。
    """
    mounts([], usb_blocks=set())
    mgr = _config(tmp_path)
    targets = de.disk_record_targets(mgr)
    assert len(targets) == 1
    assert targets[0].available is False
    assert "未检测到移动硬盘" in targets[0].reason
    assert de.disk_record_target(mgr) is None
    assert de.default_record_target_key(mgr) == de.LOCAL_TARGET


def test_a_broken_mounts_file_does_not_raise(mounts):
    """`/proc/mounts` 读不了 → 返回空列表并记日志，不能把界面搞崩。"""
    state = mounts([], usb_blocks=set())

    def boom(*a, **k):
        raise OSError("模拟 /proc/mounts 读不了")

    import builtins
    real_open = builtins.open
    builtins.open = boom
    try:
        assert de.list_removable_mounts() == []
    finally:
        builtins.open = real_open
    assert state["lines"] == []


def test_usb_detection_uses_sysfs_path_not_the_removable_flag(monkeypatch):
    """USB 判据读 sysfs 的**设备路径**，不能读  属性。

    实测（本机  就是那只移动硬盘）： 是 **0**，
    与内置 NVMe 完全一样 —— 分不出��。sysfs 真实路径里含 
    （）才是准的。

    做法：只桩 （取路径那一步），让判据本身跑真的逻辑。
     的值由**真 sysfs** 决定（ 在这台机器上确实是 0），
    所以"它是不是 USB"完全由 sysfs 路径决定 —— 这正是我们要钉住的关系。
    """
    # 真机上 sda 就是 USB 移动硬盘：removable 报 0，但 sysfs 路径含 /usb
    assert Path("/sys/block/sda/removable").exists(), (
        "这条用例要在有 USB 设备的环境里跑才有意义")
    assert de._is_usb_block("sda") is True, (
        "removable=0 的移动硬盘仍必须被认出来（实测本机就是 removable=0）")

    # 内置 NVMe：sysfs 路径不含 /usb → 排除
    monkeypatch.setattr(
        de, "_usb_sysfs_path",
        lambda block: f"/sys/devices/pci0000:00/0000:00:1d.0/ata1/{block}")
    assert de._is_usb_block("sda") is False, (
        "sysfs 路径不含 /usb 的设备绝不能被当成移动硬盘")

    # 反方向也钉住：**路径含 /usb 时必须判真**。
    # 只钉上一条的话，"永远判真"这个改坏（把内置盘也当成移动硬盘）能一路绿灯 ——
    # 实测过，所以两个方向都要有断言。
    monkeypatch.setattr(
        de, "_usb_sysfs_path",
        lambda block: f"/sys/devices/pci0000:00/0000:00:14.0/usb2/2-3/{block}")
    assert de._is_usb_block("sda") is True, (
        "sysfs 路径含 /usb 的设备必须被判成 USB（否则移动硬盘永远认不出来）")

    # sysfs 读不到（拔盘瞬间 / 权限不足）→ 判 False 而不是抛异常
    def boom(_block):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(de, "_usb_sysfs_path", boom)
    assert de._is_usb_block("sda") is False, "读不到 sysfs 时必须安全地判否"


# ---------- 候选扫描：什么是 bag ----------

def _make_bag(root: Path, name: str, *, with_metadata=True, files=1, size=1024) -> Path:
    bag = root / name
    bag.mkdir(parents=True)
    if with_metadata:
        (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n",
                                          encoding="utf-8")
    for index in range(files):
        (bag / f"topic_{index}.mcap").write_bytes(b"x" * size)
    return bag


def test_only_real_bags_are_listed(tmp_path):
    """非 bag 目录**根本不列** —— 这是选界面的核心价值。

    判据看目录内容（有 `metadata.yaml` 或 `*.mcap`），不看目录名：
    现场的名字五花八门（`labeltest_…`、`rosbag2_…`、`11111_…`）。
    """
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "vla_20261015103000", files=3)
    _make_bag(bags, "old_label_name", files=1)
    (bags / "not_a_bag").mkdir()
    (bags / "not_a_bag" / "readme.txt").write_text("不是 bag", encoding="utf-8")
    (bags / "loose.mcap").write_bytes(b"x")        # 文件不是目录 → 也不该列

    up = DataUploader()
    up.bag_dir = bags
    found = [c.path.name for c in up.scan_bag_candidates()]
    assert sorted(found) == ["old_label_name", "vla_20261015103000"]


def test_a_bag_without_metadata_but_with_mcap_still_counts(tmp_path):
    """只有 `.mcap` 没有 `metadata.yaml`（分卷 / 精简录制）也算 bag。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "partial", with_metadata=False, files=2)
    up = DataUploader()
    up.bag_dir = bags
    assert len(up.scan_bag_candidates()) == 1


def test_candidates_are_newest_first(tmp_path):
    """按录制时间**倒序**（新的在最前）—— 操作员多数要统计刚录的那次。"""
    import os as _os
    import time as _time
    bags = tmp_path / "bags"
    bags.mkdir()
    old = _make_bag(bags, "a_old")
    mid = _make_bag(bags, "b_mid")
    new = _make_bag(bags, "c_new")
    now = _time.time()
    _os.utime(old, (now - 9000, now - 9000))
    _os.utime(mid, (now - 3600, now - 3600))
    _os.utime(new, (now - 60, now - 60))

    up = DataUploader()
    up.bag_dir = bags
    assert [c.path.name for c in up.scan_bag_candidates()] == ["c_new", "b_mid", "a_old"]


def test_size_and_topic_count_are_reported(tmp_path):
    """每行要给的"体积"和"数据文件数"必须真的算出来。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "vla_x", files=4, size=2048)
    up = DataUploader()
    up.bag_dir = bags
    cand = up.scan_bag_candidates()[0]
    assert cand.topic_files == 4
    assert cand.size_bytes >= 4 * 2048
    assert "KB" in cand.size_text() or "MB" in cand.size_text()


def test_size_text_is_stable_across_repeated_reads(tmp_path):
    """`size_text()` 读多次结果必须一致。

    曾经的实现就地改 `self.size_bytes` 逐级折算 —— 第一次读 9.0GB，
    第二次读 9.0KB。frozen dataclass 会直接抛异常，不 frozen 就是这种静默错误。
    """
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "vla_x", files=1, size=9 * 1024 ** 3)
    cand = describe_bag(bags / "vla_x")
    assert cand.size_text() == cand.size_text()
    assert "GB" in cand.size_text()


def test_kind_is_read_from_the_name_only_when_it_really_is_one(tmp_path):
    """类型只在名字确实是 `vla_/bag_/cmd_` 前缀时才给，不硬猜。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "vla_20261015103000")
    _make_bag(bags, "labeltest_20260921102345")
    up = DataUploader()
    up.bag_dir = bags
    kinds = {c.path.name: c.kind for c in up.scan_bag_candidates()}
    assert kinds["vla_20261015103000"] == "vla"
    assert kinds["labeltest_20260921102345"] == "", "认不出来的不要瞎标类型"


def test_a_missing_root_is_not_an_error(tmp_path):
    """根目录不存在 → 空列表，不抛异常（对话框刚打开就崩是最糟的失败形态）。"""
    up = DataUploader()
    up.bag_dir = tmp_path / "nope"
    assert up.scan_bag_candidates() == []


def test_bag_candidate_is_frozen():
    """候选是 frozen 的 —— 界面拿它当只读数据用，不许就地改。"""
    from datetime import datetime
    cand = BagCandidate(path=Path("/x"), captured_at=datetime.now())
    with pytest.raises(Exception):
        cand.size_bytes = 1


# ---------- 选择对话框 ----------

@pytest.fixture
def picker(qapp):
    """造一个 `BagPickerDialog` 的工厂（离屏）。"""
    host = QWidget()

    def make(bags_root, tmp_path=None):
        from data_collector.ui import BagPickerDialog
        up = DataUploader()
        up.bag_dir = Path(bags_root)
        return BagPickerDialog(host, uploader=up)

    return make


def test_the_picker_lists_bags_and_selects_the_newest(tmp_path, picker):
    """列表按时间倒序、默认选中**第一条**（今天最新那个）。"""
    import os as _os
    import time as _time
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "old_one")
    _make_bag(bags, "new_one")
    now = _time.time()
    _os.utime(bags / "old_one", (now - 8000, now - 8000))
    _os.utime(bags / "new_one", (now - 10, now - 10))

    dlg = picker(bags)
    try:
        assert dlg.lst.count() == 2
        assert dlg.selected_path() == bags / "new_one"
        assert "共 2 项" in dlg.count_label.text()
    finally:
        dlg.deleteLater()


def test_the_picker_shows_time_size_and_count_in_each_row(tmp_path, picker):
    """每一行都要能判断"是哪一次"：时间 + 体积 + 数据文件数。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "vla_20261015103000", files=3, size=4096)
    dlg = picker(bags)
    try:
        text = dlg.lst.item(0).text()
        # 名字列刻意**剥掉 `vla_` 前缀**（那一列只留"这次采集叫什么"，
        # 类型由最后一列单独给出），否则类型信息会出现两遍。
        assert "20261015103000" in text and "vla_20261015103000" not in text
        assert "数据文件 3" in text
        assert text.rstrip().endswith("vla"), f"最后一列应当是类型：{text!r}"
        assert str(bags / "vla_20261015103000") in dlg.detail.text()
    finally:
        dlg.deleteLater()


def test_empty_state_explains_where_to_look(tmp_path, picker):
    """空目录 → 说清"没有可统计的数据"并指向「浏览其它位置」。

    还要验「开始统计」是禁用的：空列表时点它不该起脚本。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    dlg = picker(empty)
    try:
        assert dlg.lst.count() == 0
        assert dlg.selected_path() is None
        assert dlg.btn_ok.isEnabled() is False
        note = dlg.empty_note.text()
        assert "没有可统计的采集数据" in note
        assert "浏览其它位置" in note, "必须告诉操作员还有一条路"
    finally:
        dlg.deleteLater()


def test_non_bag_directories_never_appear(tmp_path, picker):
    """界面上不该出现非 bag 目录（这是它比系统框好的核心）。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "real_bag")
    (bags / "just_a_folder").mkdir()
    dlg = picker(bags)
    try:
        texts = [dlg.lst.item(i).text() for i in range(dlg.lst.count())]
        assert len(texts) == 1
        assert "real_bag" in texts[0]
        assert not any("just_a_folder" in t for t in texts)
    finally:
        dlg.deleteLater()


def test_reload_is_idempotent(tmp_path, picker):
    """重复点「刷新」不该让条目越攒越多。"""
    bags = tmp_path / "bags"
    bags.mkdir()
    _make_bag(bags, "one")
    _make_bag(bags, "two")
    dlg = picker(bags)
    try:
        first = dlg.lst.count()
        dlg.reload()
        dlg.reload()
        assert dlg.lst.count() == first == 2
    finally:
        dlg.deleteLater()


def test_a_picker_with_no_uploader_still_opens(qapp):
    """`uploader=None`（旧用法 / 替身）也要能开 —— 不能因为多了这一栏就崩。"""
    from data_collector.ui import BagPickerDialog
    host = QWidget()
    dlg = BagPickerDialog(host, uploader=None)
    try:
        assert dlg.lst.count() == 0
        assert dlg.selected_path() is None
        assert dlg.btn_ok.isEnabled() is False
    finally:
        dlg.deleteLater()# ---------- 枚举：判据 ----------




def test_records_directly_to_disk_accepts_the_mount_as_text(tmp_path):
    """`_records_directly_to_disk` 必须接受**字符串**挂载点（别要求调用方先转 Path）。

    这是一条**只出图才能发现**的既存缺陷：界面上那个挂载点来自
    `ed_disk_mount.text()`，是字符串；而 `core.is_within` 内部直接调 `.resolve()`，
    接字符串会抛 `AttributeError` —— 异常被面板里的 `except` 吞成一条警告日志，
    于是「本次录制的数据已直接写在盘上 —— 无需再导出」那行提示**从来没显示过**，
    界面上也不报错。

    这里直接断言"接字符串不抛、且判定正确"，把这条吞掉的异常钉成红的。
    """
    from data_collector.ui.panels.export_target import ExportTargetMixin

    usb = tmp_path / "usb"
    (usb / "seq" / "2026-10-15").mkdir(parents=True)

    class _Win:
        """只借 `_records_directly_to_disk` 需要的那一样（config）。"""

        def __init__(self, config):
            self.config = config

    class _Cfg:
        def __init__(self, bag_dir):
            self._bag_dir = bag_dir

        def get_bag_dir(self):
            return self._bag_dir

    recording_dir = usb / "seq" / "2026-10-15"
    probe = ExportTargetMixin._records_directly_to_disk.__get__(
        _Win(_Cfg(str(recording_dir))))
    # 字符串挂载点（面板给的就是它）
    assert probe(str(usb)) is True, "录制落在盘上时必须认出来（且不能抛异常）"
    assert probe(str(tmp_path / "other")) is False
    # Path 也要能接（单元测试里直接传 Path 的情形）
    assert probe(usb) is True
