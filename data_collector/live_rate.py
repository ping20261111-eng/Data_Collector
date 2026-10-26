"""实时帧率：从**录制中的 bag 数据库**增量计数，按「数据验证」的分组算帧率。

## 分组口径必须与验证报告一致（这一版是返工）

第一版按"7 个话题"做，现场看到验证图后指出：真正要看的是**图里那 8 个分组**。
`/joint_states` 在验证脚本里是**按完整关节名集合拆开**的（`bag_rate_stat.analyze_bag`）：

    key = '/joint_states [' + ','.join(sorted(msg.name)) + ']'

于是一个话题会裂成 5 组，帧率差**一个数量级**（左臂/右臂约 999 Hz、
左手/右手 100 Hz、躯干 30 Hz）。只看话题总数会把这种差异完全掩盖掉 ——
所以这里也按关节名集合分组。

## 为什么手工解析 CDR 而不用 `deserialize_message`

上位机是 Qt 进程，**不装 ROS 环境**（`rclpy` / `sensor_msgs` 都 import 不到），
用不了 `deserialize_message`。而关节名只在前 300 字节里，不必反序列化整条消息：

| 做法 | 每条耗时 | 1851 条/秒的单核占用 |
| --- | --- | --- |
| `deserialize_message`（需要 rclpy） | 52 us | 9.7% |
| 手工解析 name（不需要 ROS） | **2.7 us** | **0.5%** |

手工解析与 `deserialize_message` 的结果**逐条对照过 3000/3000 完全一致**
（见 `tests/test_live_rate.py::test_joint_names_match_rclpy_when_available`）。
CDR 布局（rosbag2 存的 JointState）：

    偏移 0   : 4 字节 CDR 封装头（00 01 00 00）
    偏移 4   : int32  stamp.sec
    偏移 8   : uint32 stamp.nanosec
    偏移 12  : uint32 frame_id 长度（含结尾 \\0）
    偏移 16  : frame_id 字节
    对齐到 4 : uint32 name 个数
    其后     : 每个 name = uint32 长度 + 字节（含结尾 \\0），各自对齐到 4

## 为什么不去订阅话题

图像话题（`/vision/*`、`/camera_*`）订阅要每秒反序列化约 160 MB，且发布端是
`reliable` + `depth: 10`，处理不过来会**背压**到录制与相机驱动。读 bag 的索引
（`timestamp_idx`）实测 0.0~0.1 ms，内存增量 0.4 MB 量级。
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: 采样周期（秒）。2 秒足够稳（相机 30 Hz 时一个窗口 60 帧），又不会让读数来回跳。
SAMPLE_INTERVAL = 2.0

#: sqlite 页缓存上限（KB）。2 MB 就够读索引了，别为这个功能多占内存。
CACHE_KB = 2048

#: 读库等锁的上限（秒）。录制中库是别人在写，等不到就跳过这一轮。
BUSY_TIMEOUT = 2.0

#: 关节状态话题：它要按关节名集合再分组。
JOINT_TOPIC = "/joint_states"

#: 图像话题 → 界面短标签。与 `bag_rate_stat.display_name()` 的中文前缀同口径。
IMAGE_LABELS: Dict[str, str] = {
    "/vision/rgb": "面部·彩",
    "/vision/depth": "面部·深",
    "/camera_left/color/image_rect_raw": "左腕·彩",
    "/camera_left/aligned_depth_to_color/image_raw": "左腕·深",
    "/camera_right/color/image_rect_raw": "右腕·彩",
    "/camera_right/aligned_depth_to_color/image_raw": "右腕·深",
}

#: 关节组 → 中文标签。
#:
#: 左臂 `AR5_Ljoint` 与左手 `LJoint` 只差一个字母的大小写（`Ljoint` / `LJoint`）。
#: 这条判断有**双重保护**：`in` 比较大小写敏感，且 `AR5_*` 排在 `LJoint` 之前 ——
#: 实测单独调换顺序、或单独改成大小写不敏感，结果都不变；**两者同时改**才会把
#: 左臂（999 Hz）贴成左手的标签，现场就会拿错数去看错机构。
#: 守卫：`tests/test_live_rate.py::test_left_arm_is_not_mistaken_for_left_hand`
#: （用组合变异验证过它确实会红）。
JOINT_TOKENS: Tuple[Tuple[str, str], ...] = (
    ("AR5_Ljoint", "左臂"),
    ("AR5_Rjoint", "右臂"),
    ("LJoint", "左手"),
    ("RJoint", "右手"),
    ("trunk", "躯干"),
)

#: 界面上分组的固定顺序（相机在前、关节在后）。不在表里的排到最后。
LABEL_ORDER: Tuple[str, ...] = (
    "面部·彩", "面部·深", "左腕·彩", "左腕·深", "右腕·彩", "右腕·深",
    "左臂", "右臂", "左手", "右手", "躯干",
)

#: `format_rates` 每行最多放几个条目（放太多会被日志区截断，反而看不见后面的）。
ITEMS_PER_LINE = 4


# ---------- 纯函数：关节名解析与分组 ----------


def joint_names(data: bytes) -> Tuple[str, ...]:
    """从一条 JointState 的序列化字节里取出关节名（顺序原样，不做排序）。

    纯函数，不依赖 ROS 环境。**解析失败返回空元组**而不是抛异常 ——
    调用方在采样循环里，一条脏数据不该把整轮采样打断。

    布局见模块说明；只读前几百字节，实测 2.7 us/条。
    """
    try:
        off = 12                                   # 4(封装头) + 8(stamp)
        frame_len = struct.unpack_from("<I", data, off)[0]
        off += 4 + frame_len
        off = (off + 3) & ~3                       # 对齐到 4
        count = struct.unpack_from("<I", data, off)[0]
        off += 4
        names = []
        for _ in range(count):
            off = (off + 3) & ~3
            length = struct.unpack_from("<I", data, off)[0]
            off += 4
            if length <= 0 or off + length > len(data):
                return ()                          # 长度不合理：这条按"读不出"处理
            names.append(data[off:off + length - 1].decode("utf-8", "replace"))
            off += length
        return tuple(names)
    except (struct.error, IndexError, UnicodeDecodeError):
        return ()


def joint_group_label(names) -> str:
    """一组关节名 → 中文短标签（与验证报告/图上的名字一致）。

    `names` 可以是任意顺序（内部会先排序，与 `analyze_bag` 的 `sorted(msg.name)` 同口径）。
    """
    joined = ",".join(sorted(names))
    for token, label in JOINT_TOKENS:
        if token in joined:
            return label
    return "其它关节"


def group_sort_key(label: str) -> Tuple[int, str]:
    """界面排序：相机在前、关节在后，未知标签排最后。"""
    try:
        return (LABEL_ORDER.index(label), "")
    except ValueError:
        return (len(LABEL_ORDER), label)


def rate_from_delta(count: int, seconds: float) -> Optional[float]:
    """由「这段时间里收到的帧数」算帧率（Hz）。

    返回 None 表示**算不出来**，而不是 0：
    - `seconds <= 0`：时钟没走（或同一时刻被采了两次），任何结果都是假的；
    - `count < 0`：bag 换了/被重置，这一轮的数字没有意义。

    `count == 0` 且间隔正常时返回 **0.0** —— 那是真的"一帧都没有"，必须如实显示，
    这正是现场最需要看到的异常（某路相机没出图）。
    """
    if seconds <= 0:
        return None
    if count < 0:
        return None
    return count / seconds


# ---------- 采样器 ----------


class LiveRateSampler:
    """从录制中的 bag 增量计数，按「数据验证」的分组算帧率。

    用法：`attach(目录或 db3)` 绑定当前录制，然后周期性 `sample()`。
    每次 `sample()` 只做索引范围查询 + 解析窗口内的关节消息（实测 1851 Hz 下
    约 5 ms/秒，即 0.5% 单核），可以在主线程直接调。

    ## 为什么按 `timestamp >` 增量而不是 `MAX(id)` 差值

    bag 的 `messages.id` 是**全表共享自增**（多话题交替插入），`MAX(id)` 的差值
    不等于某组的帧数；而且 `GROUP BY topic_id` 取 `MAX(id)` 要全表扫
    （340 MB 的 bag 上 371 ms）。`timestamp` 有索引，范围查询 0.0~0.1 ms。
    """

    def __init__(self, path: Optional[Path] = None):
        self._path: Optional[Path] = None
        #: db3 文件 → (连接, 上次统计的时间上界)
        self._files: Dict[Path, Tuple[sqlite3.Connection, int]] = {}
        #: db3 → {topic_id: 话题名}
        self._topic_ids: Dict[Path, Dict[int, str]] = {}
        self._last_at: Optional[float] = None
        #: 最近一次成功算出的读数：分组标签 → Hz
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
            found = [target]
        elif target.is_dir():
            found = sorted(p for p in target.glob("*.db3") if p.is_file())
        else:
            found = []
        if not found:
            if self._path != target:
                self.close()
            self._path = target
            return False
        if found != sorted(self._files):
            self.close()                       # 换库了：旧的时间基准不能用在新的上
        self._path = target
        return True

    def close(self) -> None:
        """释放所有连接。**不写库**，纯读。"""
        for conn, _ts in self._files.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._files.clear()
        self._topic_ids.clear()
        self._last_at = None
        self._rates = {}

    # ---------- 采样 ----------

    def sample(self, now: Optional[float] = None) -> Dict[str, Optional[float]]:
        """采一次。返回 {分组标签: Hz 或 None}；还没有基准时返回空字典。

        读不到（文件没落盘 / 被锁 / 库坏了）就返回**上一次的读数**，
        这样界面不会因为偶发失败而闪成空白或跳成 0。
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
            for label, count in got.items():
                deltas[label] = deltas.get(label, 0) + count
        if failed:
            # 任一文件读不到，这一轮就是不完整的（分片少算一片会让帧率直接腰斩）。
            # 保留上一次读数，绝不输出偏小的值，更不输出 0 ——
            # 0 的含义是"这一路一帧都没出"，那是要立刻去查相机的事。
            return dict(self._rates)

        if self._last_at is None:
            self._last_at = stamp              # 第一次只取基准，这一轮不产出读数
            return {}

        seconds = stamp - self._last_at
        self._last_at = stamp
        rates: Dict[str, Optional[float]] = {}
        for label, count in deltas.items():
            rates[label] = rate_from_delta(count, seconds)
        # 两种"这一轮没数据"都要补成 0.0，而不是让它从读数里消失：
        #  · 图像分组是**固定的 6 路**，从没出现过就说明这一路压根没录进来
        #    （验证报告里叫"缺失 N 个目标话题"）—— 那正是最该一眼看到的东西；
        #  · 关节分组上一轮出现过、这一轮没有，是"掉帧"，同样要显示 0 而不是整行消失。
        # 只有"还没采够两次"（seconds <= 0）时才写 None（界面显示 `—`）。
        blank = 0.0 if seconds > 0 else None
        for label in IMAGE_LABELS.values():
            rates.setdefault(label, blank)
        for label in self._rates:
            rates.setdefault(label, blank)
        self._rates = rates
        return dict(rates)

    # ---------- 内部 ----------

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
        for db3 in list(self._files):
            if db3 not in found:               # 分片被清掉/换库：把连接收掉
                conn, _ts = self._files.pop(db3)
                self._topic_ids.pop(db3, None)
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        return found

    def _drain_one(self, db3: Path) -> Optional[Dict[str, int]]:
        """对一个 db3 取「自上次上界之后」的增量，按验证分组计数。读不到返回 None。"""
        entry = self._files.get(db3)
        if entry is None:
            opened = self._open(db3)
            if opened is None:
                return None
            entry = opened
            self._files[db3] = entry
        conn, last_ts = entry
        names = self._topic_ids.get(db3, {})
        try:
            upper = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()
            upper_ts = int(upper[0]) if upper and upper[0] is not None else 0
            if upper_ts == 0:
                self._files[db3] = (conn, upper_ts)
                return {}                     # 空库（刚开始录）

            counter: Dict[str, int] = {}
            # 图像等话题：只要帧数，不读 data（省 IO）
            rows = conn.execute(
                "SELECT topic_id, COUNT(*) FROM messages "
                "WHERE timestamp > ? GROUP BY topic_id", (last_ts,)).fetchall()
            joint_ids = []
            for topic_id, count in rows:
                name = names.get(int(topic_id), "")
                if name == JOINT_TOPIC:
                    joint_ids.append(int(topic_id))   # 单独处理，要按关节名分组
                elif name in IMAGE_LABELS:
                    counter[IMAGE_LABELS[name]] = counter.get(IMAGE_LABELS[name], 0) + int(count)
                # 其它话题（tf、joint_cmds…）不在验证口径里，直接忽略
            # 关节状态：窗口内的每条都要读 name 才能归组
            for topic_id in joint_ids:
                for (blob,) in conn.execute(
                        "SELECT data FROM messages WHERE topic_id=? AND timestamp > ?",
                        (topic_id, last_ts)):
                    label = joint_group_label(joint_names(blob))
                    counter[label] = counter.get(label, 0) + 1
            self._files[db3] = (conn, upper_ts)
            return counter
        except sqlite3.Error:
            # 录制中库在被写：偶发 "database is locked" 很正常，这一轮跳过
            # （保留上次读数），不要把它当错误刷到日志区。
            return None

    def _open(self, db3: Path) -> Optional[Tuple[sqlite3.Connection, int]]:
        """打开一个 db3 并读话题表。读不到返回 None。"""
        try:
            conn = sqlite3.connect(
                f"file:{db3}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
            conn.execute(f"PRAGMA cache_size=-{CACHE_KB}")
            conn.execute("PRAGMA temp_store=MEMORY")
            topics = {
                int(tid): str(name)
                for tid, name in conn.execute("SELECT id, name FROM topics").fetchall()
            }
        except sqlite3.Error:
            try:
                conn.close()                  # type: ignore[possibly-undefined]
            except (sqlite3.Error, UnboundLocalError):
                pass
            return None
        self._topic_ids[db3] = topics
        #: 上界从 **0** 起算，不是从 `MAX(timestamp)` 起算 —— 这一条很关键：
        #: 分片录制时 `_1.db3` 是**中途出现的新文件**，里面每一帧都是新写入的，
        #: 若把打开时的上界当基准，整片新数据会被当作"历史"丢掉（实测：两片各 30
        #: 帧被算成 15 Hz）。对"打开一个已经录了很久的库"，多出来的历史帧会在第一次
        #: `sample()` 里算进 deltas 后丢弃（那一轮还没有时间基准、不产出读数）。
        return conn, 0


def format_rates(rates: Dict[str, Optional[float]]) -> str:
    """把读数排成等宽文本（界面直接用），按固定顺序、每行不超过 `ITEMS_PER_LINE` 条。

    一个分组都没有 → 空串（界面据此显示"未在录制"）。
    算不出来的写 `—`，**不是 0** —— 两者在现场含义完全不同：
    0 是"这一路一帧都没出"（要查），`—` 是"还没采够两次"（等一下就好）。
    """
    if not rates:
        return ""
    ordered = sorted(rates.items(), key=lambda kv: group_sort_key(kv[0]))
    items = []
    for label, value in ordered:
        text = "—" if value is None else f"{value:.1f}"
        items.append(f"{label} {text}")
    lines = [" · ".join(items[i:i + ITEMS_PER_LINE])
             for i in range(0, len(items), ITEMS_PER_LINE)]
    return "\n".join(lines)
