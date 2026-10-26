"""实时帧率：从**录制中的 bag 数据库**增量计数，算各目标话题的帧率。

## 为什么不去订阅话题（这是本模块存在的全部理由）

数据验证的 7 个目标话题里有 **6 个是相机图像**（640×480）。订阅它们意味着每帧都要
经过 DDS 传输 + rclpy 反序列化（color 约 900 KB、depth 约 600 KB），6 路 × 30 Hz
约等于**每秒 160 MB 字节流过 Python**。更要紧的是发布端用的是
`reliable` + `depth: 10`（见机器人侧 `scripts/qos_override.yaml`），订阅端处理不过来
会**背压**到发布端 —— 录制和相机驱动都会被拖慢。为了看一个数字去冒这个险不值得。

而"数据验证"要的本来就是**录进 bag 的帧率**，不是话题上的发布帧率：两者并不相同，
磁盘慢的时候 bag 会掉帧，但话题上照发。所以直接读 bag 的 sqlite：

| 操作 | 实测耗时（340 MB / 95 万行的真实 bag） |
| --- | --- |
| `SELECT MAX(timestamp)` 取时间上界 | **0.0 ms**（走 `timestamp_idx`） |
| 一次增量计数（全部话题分组） | **0.0~0.1 ms** |

内存增量约 **2~3 MB**（一个 sqlite 连接 + 2 MB 页缓存上限 + 几个字典），
CPU 每 2 秒不到 1 ms。**不影响 DDS、不影响录制、不影响相机驱动。**

## 边界

- 只有**录制进行中**才有数据可读（未录制时返回空，界面如实显示"未在录制"）；
- 录制刚开始时 `*.db3` 可能还没落盘，返回空；
- 读取只用 `mode=ro` + `busy_timeout`，绝不写库；偶发锁冲突时保留上一次的读数。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: 与数据验证同口径的 7 个目标话题 → 界面上的短标签。
#:
#: **必须与 `scripts/bag_rate_stat.py` 的 `TOPICS` 一致**（那边是独立脚本、不 import 本包，
#: 所以只能各写一份）。守卫：`tests/test_live_rate.py::test_target_topics_match_the_verify_script`。
TARGET_TOPICS: Dict[str, str] = {
    "/vision/rgb": "rgb",
    "/vision/depth": "dep",
    "/camera_left/color/image_rect_raw": "L",
    "/camera_left/aligned_depth_to_color/image_raw": "L-d",
    "/camera_right/color/image_rect_raw": "R",
    "/camera_right/aligned_depth_to_color/image_raw": "R-d",
    "/joint_states": "关节",
}

#: 采样周期（秒）。2 秒足够稳（相机 30 Hz 时一个窗口 60 帧），又不会让读数来回跳。
SAMPLE_INTERVAL = 2.0

#: sqlite 页缓存上限（KB）。2 MB 就够读索引了，别为这个功能多占内存。
CACHE_KB = 2048

#: 读库等锁的上限（秒）。录制中库是别人在写，等不到就跳过这一轮。
BUSY_TIMEOUT = 2.0


def label_of(topic: str) -> str:
    """话题名 → 界面短标签；不在目标清单里就原样返回（便于排查新增话题）。"""
    return TARGET_TOPICS.get(topic, topic)


def rate_from_delta(count: int, seconds: float) -> Optional[float]:
    """由「这段时间里收到的帧数」算帧率（Hz）。

    纯函数，所以"帧率怎么算"这件事可以单测 —— 它决定现场看到的数字准不准。

    Args:
        count: 两次采样之间新增的帧数。
        seconds: 两次采样之间的实际间隔（秒）。

    返回 None 表示**算不出来**，而不是 0：
    - `seconds <= 0`：时钟没走（或同一时刻被采了两次），任何结果都是假的；
    - `count < 0`：bag 换了/被重置，这一轮的数字没有意义。

    `count == 0` 且间隔正常时返回 **0.0** —— 那是真的"一帧都没有"，必须如实显示，
    这正是现场最需要看到的异常（相机没出图）。
    """
    if seconds <= 0:
        return None
    if count < 0:
        return None
    return count / seconds


class LiveRateSampler:
    """从录制中的 bag 增量计数算帧率。

    用法：`attach(目录或 db3)` 绑定当前录制，然后周期性 `sample()`。
    每次 `sample()` 只做一次索引范围查询（实测 0.0~0.1 ms），可以在主线程直接调。

    ## 为什么要维护 `_last_ts` 而不是累计计数

    bag 的 `messages.id` 是**全表共享自增**（多话题交替插入），所以 `MAX(id)` 的差值
    不等于某个话题的帧数 —— 拿它算帧率是错的（实测：340 MB 的 bag 上
    `MAX(id)` 分组查询还要 371 ms，而且结果对不上）。按 `timestamp > 上次上界`
    做增量才是既准又快：`timestamp` 有索引（`timestamp_idx`）。
    """

    def __init__(self, path: Optional[Path] = None):
        self._path: Optional[Path] = None
        #: db3 文件 → (连接, 上次统计的时间上界)
        self._files: Dict[Path, Tuple[sqlite3.Connection, int]] = {}
        #: sqlite 的连接 id → 话题名（每次 attach 重读，bag 换了话题表也会变）
        self._topic_names: Dict[Path, Dict[int, str]] = {}
        self._last_at: Optional[float] = None
        #: 最近一次成功算出的读数：话题 → Hz（算不出来的话题不在里面）
        self._rates: Dict[str, Optional[float]] = {}
        if path is not None:
            self.attach(path)

    # ---------- 绑定 / 释放 ----------

    def attach(self, path) -> bool:
        """绑定一个 bag 目录或 db3 文件。**换了目标就重置所有基准**。

        返回 False 表示当前没有可读的文件（录制刚开始时很常见）。
        """
        target = Path(path)
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = sorted(p for p in target.glob("*.db3") if p.is_file())
        else:
            candidates = []
        if not candidates:
            self.close()
            self._path = target
            return False
        if candidates != self._path_files():
            self.close()                      # 换库了：旧的时间基准不能用在新库上
            self._path = target
        return True

    def _path_files(self) -> List[Path]:
        return sorted(self._files)

    def close(self) -> None:
        """释放所有连接。**不写库**，纯读。"""
        for conn, _ts in self._files.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._files.clear()
        self._topic_names.clear()
        self._last_at = None
        self._rates = {}

    # ---------- 采样 ----------

    def sample(self, now: Optional[float] = None) -> Dict[str, Optional[float]]:
        """采一次。返回 {话题: Hz 或 None}；还没有基准时返回空字典。

        读不到（文件没落盘 / 被锁 / 库坏了）就返回**上一次的读数**，
        这样界面不会因为偶发失败而闪成空白。
        """
        import time

        stamp = time.monotonic() if now is None else float(now)
        files = self._collect_files()
        if not files:
            return dict(self._rates)

        deltas: Dict[str, int] = {}
        failed = False
        for db3 in files:
            got = self._drain_one(db3)
            if got is None:
                failed = True
                continue
            for topic, count in got.items():
                deltas[topic] = deltas.get(topic, 0) + count
        if failed:
            # 任一文件读不到，这一轮的数字就是**不完整的**（分片少算一片会让帧率
            # 直接腰斩）。此时保留上一次读数，绝不输出一个偏小的值 ——
            # 现场看到"30"变"15"会去查相机，其实是读库被锁了一下。
            # 也绝不能输出 0：0 的含义是"相机一帧都没出"，那是要立刻处理的事。
            return dict(self._rates)

        if self._last_at is None:
            self._last_at = stamp              # 第一次只取基准，这一轮不产出读数
            return {}

        seconds = stamp - self._last_at
        self._last_at = stamp
        rates: Dict[str, Optional[float]] = {}
        for topic in TARGET_TOPICS:
            rates[topic] = rate_from_delta(deltas.get(topic, 0), seconds)
        self._rates = rates
        return dict(rates)

    def _collect_files(self) -> List[Path]:
        """当前该读哪些 db3。目录会被重新扫（录制中可能新增分片）。"""
        if self._path is None:
            return []
        if self._path.is_file():
            return [self._path]
        if not self._path.is_dir():
            return []
        try:
            found = sorted(p for p in self._path.glob("*.db3") if p.is_file())
        except OSError:
            return []
        # 旧库里已经不存在的连接要收掉（换库 / 被清理）
        for db3 in list(self._files):
            if db3 not in found:
                conn, _ts = self._files.pop(db3)
                self._topic_names.pop(db3, None)
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        return found

    def _drain_one(self, db3: Path) -> Optional[Dict[str, int]]:
        """对一个 db3 取「自上次上界之后」的增量计数。读不到返回 None。"""
        entry = self._files.get(db3)
        if entry is None:
            opened = self._open(db3)
            if opened is None:
                return None
            entry = opened
            self._files[db3] = entry
        conn, last_ts = entry
        try:
            upper = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()
            upper_ts = int(upper[0]) if upper and upper[0] is not None else 0
            if upper_ts == 0:
                return {}                     # 空库（刚开始录）
            rows = conn.execute(
                "SELECT topic_id, COUNT(*) FROM messages WHERE timestamp > ? GROUP BY topic_id",
                (last_ts,),
            ).fetchall()
            counter: Dict[str, int] = {}
            names = self._topic_names.get(db3, {})
            for topic_id, count in rows:
                name = names.get(int(topic_id))
                if name in TARGET_TOPICS:
                    counter[name] = counter.get(name, 0) + int(count)
            self._files[db3] = (conn, upper_ts)
            return counter
        except sqlite3.Error:
            # 录制中库在被写：偶发 "database is locked" 很正常，
            # 这一轮跳过（保留上次读数），不要把它当成错误刷到日志区。
            return None

    def _open(self, db3: Path) -> Optional[Tuple[sqlite3.Connection, int]]:
        """打开一个 db3 并读话题表。读不到返回 None。"""
        try:
            conn = sqlite3.connect(
                f"file:{db3}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
            conn.execute(f"PRAGMA cache_size=-{CACHE_KB}")
            conn.execute("PRAGMA temp_store=MEMORY")
            names = {
                int(tid): str(name)
                for tid, name in conn.execute("SELECT id, name FROM topics").fetchall()
            }
        except sqlite3.Error:
            try:
                conn.close()                  # type: ignore[possibly-undefined]
            except (sqlite3.Error, UnboundLocalError):
                pass
            return None
        self._topic_names[db3] = names
        #: 上界从 **0** 起算，不是从 `MAX(timestamp)` 起算 —— 这一条很关键：
        #: 分片录制时 `_1.db3` 是**中途出现的新文件**，里面每一帧都是新写入的，
        #: 若把打开时的上界当成基准，整片新数据会被当作"历史"丢掉（实测：两片各 30
        #: 帧被算成 15 Hz）。
        #: 对"打开一个已经录了很久的库"这种情况，多出来的历史帧会在第一次
        #: `sample()` 里被算进 deltas 后丢弃（那一轮 `_last_at` 还没基准、不产出读数），
        #: 所以不会出现"历史帧算进这 2 秒"的爆表读数。
        return conn, 0


def format_rates(rates: Dict[str, Optional[float]]) -> str:
    """把读数排成一行等宽文本（界面直接用）。

    一个话题都没有 → 空串（界面据此显示"未在录制"）。
    算不出来的话题写 `—`，而不是 0 —— 两者在现场的含义完全不同：
    0 是"相机没出图"（要查），`—` 是"还没采够两次"（等一下就好）。
    """
    if not rates:
        return ""
    parts = []
    for topic, label in TARGET_TOPICS.items():
        if topic not in rates:
            continue
        value = rates[topic]
        text = "—" if value is None else f"{value:.1f}"
        parts.append(f"{label} {text}")
    return " · ".join(parts)
