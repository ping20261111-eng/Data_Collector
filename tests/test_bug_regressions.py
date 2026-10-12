"""回归测试：现场报上来的两个缺陷。

bug1（P0）：启动录制后进程自行退出，界面自动收尾；第二次点录制能录，但**停不下来**。
bug2（P1）：打点面板里修改标签后，界面上标签没变。

两个都不是"功能没写"，而是**返回值语义**与**残留状态**的问题：
bug1 是 Popen 句柄与录制基准时间在进程死后没回收，
bug2 是"CSV 还没落盘"被当成了"整次修改失败"。
"""
import time
from pathlib import Path

import pytest


# ---------- 公共脚手架 ----------

class FakeProc:
    """模拟录制子进程。alive 翻转即代表脚本自行退出。"""

    def __init__(self, alive: bool = True, pid: int = 999999):
        self.alive = alive
        self.pid = pid
        self.signals: list = []

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        import subprocess
        if timeout == 0:
            return 0
        raise subprocess.TimeoutExpired("fake", timeout or 0)

    def send_signal(self, sig):
        self.signals.append(sig)
        raise ProcessLookupError()      # 进程早已不在，走退路

    def kill(self):
        pass


class FakePM:
    """ProcessManager 的最小替身：只关心注册表是否干净。"""

    def __init__(self):
        self.items: list = []

    def register(self, proc, name=""):
        self.items.append((proc, name))

    def unregister(self, proc):
        self.items = [(p, n) for p, n in self.items if p is not proc]


class FakeLauncher:
    def __init__(self):
        self.pm = FakePM()
        self.procs: list = []
        self.launch_count = 0

    def launch_record(self, *args, **kwargs):
        self.launch_count += 1
        proc = FakeProc()
        self.procs.append(proc)
        self.pm.register(proc, "record_bag")
        return proc


class FakeCfg:
    def __init__(self, bag: Path):
        self._bag = bag

    def get_bag_dir(self):
        return self._bag


@pytest.fixture
def rec_env(tmp_path):
    from data_collector.core_data import RecorderManager
    bag = tmp_path / "bag"
    bag.mkdir()
    launcher = FakeLauncher()
    rm = RecorderManager(FakeCfg(bag), launcher)
    return rm, bag, launcher


# ---------- bug1：录制进程状态机 ----------

def test_bug1_dead_proc_leaves_stale_state(rec_env):
    """现场症状的根因：进程死了，_started_at 还挂着上一场的值。

    旧基准比新录制早，于是"新文件"判定形同虚设 —— 录制脚本起来但没落盘这种
    事故再也拦不住（health() 会一直报 producing=True）。
    """
    rm, bag, launcher = rec_env
    bag_dir = bag / "test2_20261008_120000"
    bag_dir.mkdir()

    assert rm.start("bag", name="test2", settle_seconds=0) is True
    rm._manifest_dir = bag_dir
    stale_base = rm.started_at

    launcher.procs[0].alive = False        # 脚本自行退出

    # 状态仍被持有 —— 这就是"第二次录不停"的前提
    assert rm.current_proc is not None
    assert rm.started_at == stale_base
    assert rm._manifest_dir == bag_dir


def test_bug1_start_reaps_dead_proc_before_new_session(rec_env):
    """第二次启动必须先把上一次的死句柄收干净，否则基准时间与 manifest 会串场。"""
    rm, bag, launcher = rec_env
    # 两个目录：第一次录制的旧目录 + 第二次的新目录。缺了后者的话，
    # _begin_manifest 找不到"更新的"目录，会又落回旧目录，断言就失去意义。
    old_dir = bag / "test2_20260101_000000"
    old_dir.mkdir()
    new_dir = bag / "test2_20260101_010000"
    new_dir.mkdir()

    rm.start("bag", name="test2", settle_seconds=0)
    rm._manifest_dir = old_dir
    stale_base = rm.started_at
    launcher.procs[0].alive = False

    assert rm.start("bag", name="test2", settle_seconds=0) is True

    # 基准时间必须是**新**这次录制的，不能是上一次的中点
    assert rm.started_at is not None
    assert rm.started_at != stale_base
    assert rm.started_at > stale_base
    # manifest 目录也不能还指着上一次那个
    assert rm._manifest_dir != old_dir
    # 注册表里只应剩下当前这一个活进程
    assert len(launcher.pm.items) == 1
    assert launcher.pm.items[0][0] is rm.current_proc


def test_bug1_reap_clears_all_residues(rec_env):
    """_reap_dead_proc 要同时清句柄、基准时间、manifest 目录，并把死进程摘出注册表。"""
    rm, bag, launcher = rec_env
    bag_dir = bag / "test2_20260101_000000"
    bag_dir.mkdir()

    rm.start("bag", settle_seconds=0)
    rm._manifest_dir = bag_dir
    dead = launcher.procs[0]
    dead.alive = False

    assert rm._reap_dead_proc("测试") is True

    assert rm.current_proc is None
    assert rm.started_at is None
    assert rm._manifest_dir is None
    assert dead not in [p for p, _ in launcher.pm.items]
    # 再调一次应无事可做（幂等）
    assert rm._reap_dead_proc("重复调用") is False


def test_bug1_reap_noop_when_alive(rec_env):
    """还在录的时候不能误回收 —— 那会把活着的录制状态清空。"""
    rm, _bag, launcher = rec_env
    rm.start("bag", settle_seconds=0)
    live = launcher.procs[0]

    assert rm._reap_dead_proc() is False
    assert rm.current_proc is live
    assert rm.started_at is not None
    assert len(launcher.pm.items) == 1


def test_bug1_second_session_can_be_stopped(rec_env):
    """现场的核心诉求：第二次录制的 stop() 必须真的停得掉。"""
    rm, bag, launcher = rec_env
    bag_dir = bag / "test2_20260101_000000"
    bag_dir.mkdir()
    rm.start("bag", name="test2", settle_seconds=0)
    rm._manifest_dir = bag_dir
    launcher.procs[0].alive = False       # 第一次崩了

    assert rm.start("bag", name="test2", settle_seconds=0) is True
    assert rm.is_active() is True

    assert rm.stop() is True               # 关键：不再返回 False
    assert rm.is_active() is False
    assert rm.current_proc is None
    assert rm.started_at is None
    assert rm._manifest_dir is None        # 也不能留下上一场的目录
    assert launcher.pm.items == []


def test_bug1_stop_unregisters_from_process_manager(rec_env):
    """正常停止也要把 Popen 摘出注册表，否则退出时会对已结束的 pid 发信号。"""
    rm, _bag, launcher = rec_env
    rm.start("bag", settle_seconds=0)

    assert rm.stop() is True
    assert launcher.pm.items == []


def test_bug1_stop_after_death_returns_false(rec_env):
    """没有活动录制时 stop() 如实返回 False（UI 据此给"没有活动的录制任务"）。"""
    rm, _bag, _launcher = rec_env
    assert rm.stop() is False


def test_bug1_watchdog_baseline_not_polluted(rec_env):
    """回归验证：第二次录制的看门狗不能把上一场的旧文件当成新产出。

    注意两次录制要落在**不同**的 bag 目录（现场脚本按时间戳命名）。
    """
    rm, bag, launcher = rec_env

    # 第一次录制：目录里落一个文件
    first_dir = bag / "test2_20260101_000000"
    first_dir.mkdir()
    rm.start("bag", name="test2", settle_seconds=0)
    artifact = first_dir / "bag_0.mcap"
    artifact.write_bytes(b"x" * 64)
    stale_mtime = time.time() - 3600      # 明确早于第二次录制
    import os
    os.utime(artifact, (stale_mtime, stale_mtime))
    time.sleep(0.01)

    # 进程崩掉，目录里不再有新写入
    launcher.procs[0].alive = False

    # 第二次录制：新目录已建好但还没有任何文件产出
    second_dir = bag / "test2_20260101_010000"
    second_dir.mkdir()
    rm.start("bag", name="test2", settle_seconds=0)

    health = rm.health(stale_seconds=15)
    # 基准是新的，旧文件不该被算成"正在产出"
    assert health["alive"] is True
    assert health["producing"] is False


def test_bug1_files_since_skips_dirs_and_manifest(rec_env):
    """产出判定只认数据文件：目录本身与 manifest.json 都不算。

    这条是"脚本起来了但没落盘"能被拦住的关键 —— 录制脚本先建 bag 目录、
    上位机自己写 manifest.json，两者都会让"数出来的文件数 > 0"，若把它们
    算作产出，看门狗就永远显示正常，事故要到事后才发现。
    """
    rm, bag, launcher = rec_env
    rm.start("bag", settle_seconds=0)

    empty_dir = bag / "test2_20260101_000000"
    empty_dir.mkdir()                       # 只有目录，没有任何数据文件
    rm._manifest_dir = empty_dir
    manifest = empty_dir / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    count, _newest = rm._files_since(bag, rm.started_at - 1.0)
    assert count == 0, "目录与 manifest.json 都不该被算成数据产出"

    # 真落一个数据文件，才算产出
    (empty_dir / "bag_0.mcap").write_bytes(b"x" * 64)
    count_after, _ = rm._files_since(bag, rm.started_at - 1.0)
    assert count_after == 1
    assert rm.health(stale_seconds=15)["producing"] is True


# ---------- bug2：打点改名 ----------

def test_bug2_update_returns_true_even_when_dir_missing(tmp_path):
    """现场症状：改了标签，界面像没反应。

    根因是 update() 把「CSV 还没落盘」当成「整次修改失败」返回 False，
    PointEditDialog据此不 accept，列表自然不刷新。
    """
    from data_collector.core_points import PointRecorder
    bag = tmp_path / "bag"
    bag.mkdir()                       #故意不建 bag_xxx 目录，模拟录制刚开始几秒

    rec = PointRecorder(bag, "test2", started_at=time.time(), task="布草挂架")
    record = rec.add("原始标签", 1, "原始备注")

    assert rec.update(record.index, "改名后的标签", 2, "新备注") is True
    assert rec.records[0].label == "改名后的标签"
    assert rec.records[0].note == "新备注"


def test_bug2_update_persists_to_csv_when_dir_ready(tmp_path):
    """目录就绪时，改动必须真正落盘（不只是内存里变一下）。"""
    from data_collector.core_points import PointRecorder
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "test2_20261008_120000").mkdir()

    rec = PointRecorder(bag, "test2", started_at=time.time() - 10, task="布草挂架")
    record = rec.add("原始标签", 1, "原始备注")

    assert rec.update(record.index, "改名后的标签", 2, "新备注") is True

    csv_text = rec.csv_path().read_text(encoding="utf-8")
    assert "改名后的标签" in csv_text
    assert "原始标签" not in csv_text


def test_bug2_update_unknown_index_still_false(tmp_path):
    """改一个不存在的序号必须仍然失败 —— 不能为了修 bug 而放宽判定。"""
    from data_collector.core_points import PointRecorder
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "test2_20261008_120000").mkdir()

    rec = PointRecorder(bag, "test2", started_at=time.time() - 10)
    rec.add("原始标签", 1)

    assert rec.update(999, "改名", 2, "备注") is False
    assert rec.remove(999) is False


def test_bug2_remove_returns_true_when_dir_missing(tmp_path):
    """删除同理：删掉了就算成功，不因没落盘而报失败。"""
    from data_collector.core_points import PointRecorder
    bag = tmp_path / "bag"
    bag.mkdir()

    rec = PointRecorder(bag, "test2", started_at=time.time())
    rec.add("点1")
    rec.add("点2")

    assert rec.remove(1) is True
    assert rec.count == 1
    assert rec.records[0].index == 1     # 序号重排


def test_bug2_pending_records_are_deduped(tmp_path):
    """反复改同一条时，pending 里不能堆同一序号的多份（否则补写会重复行）。"""
    from data_collector.core_points import PointRecorder
    bag = tmp_path / "bag"
    bag.mkdir()

    rec = PointRecorder(bag, "test2", started_at=time.time())
    record = rec.add("原始", 1)

    rec.update(record.index, "改一次", 2)
    rec.update(record.index, "改两次", 3)

    indexes = [p.index for p in rec._pending]
    assert indexes.count(record.index) == 1
    assert rec.records[0].label == "改两次"

# ---------- 磁盘空间：录制退出的头号现场原因（v6.4.13） ----------

class _Usage:
    """`shutil.disk_usage` 的最小替身（真调用只用到 `.free`）。"""

    def __init__(self, free: int):
        self.free = free


def _patch_disk_usage(monkeypatch, free=None, boom=False):
    """把 `core_data.shutil.disk_usage` 换成固定值。

    不用真的把磁盘写满 —— 那既慢又危险；这里只桩住"读到多少可用空间"这一步。
    """
    if boom:
        def _raise(_path):
            raise OSError("读不到")
        monkeypatch.setattr("data_collector.core_data.shutil.disk_usage", _raise)
        return
    monkeypatch.setattr("data_collector.core_data.shutil.disk_usage",
                        lambda _path: _Usage(free))


def test_disk_full_reports_zero_free(rec_env, monkeypatch):
    """可用空间为 0 时要说"已满" —— 现场机器人日志里的 `Errno 28` 就是这个。"""
    rm, _bag, _launcher = rec_env
    _patch_disk_usage(monkeypatch, free=0)
    assert "已满" in rm.disk_full()


def test_disk_full_reports_low_free(rec_env, monkeypatch):
    """剩得不多也要说，且带上具体数字 —— 现场据此判断还能不能再录一场。"""
    rm, _bag, _launcher = rec_env
    _patch_disk_usage(monkeypatch, free=500 * 1024 * 1024)
    hint = rm.disk_full()
    assert "500 MB" in hint
    assert "已满" not in hint          # 快满不等于满，别用最重的那个词


def test_disk_full_silent_when_healthy(rec_env, monkeypatch):
    """空间正常时一个字都不多说 —— 这条路径每秒都会走一次。"""
    rm, _bag, _launcher = rec_env
    _patch_disk_usage(monkeypatch, free=50 * 1024 ** 3)
    assert rm.disk_full() == ""


def test_disk_free_unknown_is_not_treated_as_full(rec_env, monkeypatch):
    """读不到就当**未知**，不能当成 0。

    当成 0 会在正常机器上误报"磁盘已满"，而那是在每秒一次的看门狗路径上 ——
    误报一次就把现场引到错误的方向上（他们会去清磁盘，而问题在别处）。
    """
    rm, _bag, _launcher = rec_env
    _patch_disk_usage(monkeypatch, boom=True)
    assert rm.disk_free() is None
    assert rm.disk_full() == ""


def test_disk_free_probes_nearest_existing_ancestor(rec_env):
    """bag 目录还没建出来时，往上找一层存在的祖先照样能读到分区空间。

    第一次录制前 bag 目录可能不存在；直接 `disk_usage` 会抛 FileNotFoundError，
    于是"最需要空间检查的时候反而查不到"。
    """
    rm, bag, _launcher = rec_env
    import shutil as real_shutil
    missing = bag / "not_created_yet" / "deeper"
    rm.config._bag = missing
    free = rm.disk_free()
    assert free is not None
    assert free == real_shutil.disk_usage(str(bag)).free
