"""遥操作打点：把录制过程中"某一刻发生了什么"记下来。

## 为什么需要它

一段 bag 只说明"发生了什么"，不说明"哪一刻是任务的关键点"。后续训练与分析要的是
时刻标注（`开始任务` / `双爪夹住第一个角` / `第一个角上夹具` / …），
所以要在录制过程中按一下就记一个点。

## 落点：写进本次录制的 bag 目录

`<bag_dir>/<NAME>_<时间戳>/points.csv` —— 跟着数据一起打包上传，远端做分析时直接拿到。
这条没法用"写到上位机本地"替代：远端没有上位机那份文件。

## 一个绕不开的麻烦：目录名是脚本内部生成的

`record_bag.sh` 自己拼 `BAG_NAME="${NAME}_$(date +%Y%m%d%H%M%S)"`，上位机拿不到这个名字。
只能推断：在 bag 目录下找 `{name}_` 开头、创建时间不早于本次录制开始的目录，取最早的那个
（脚本在建 bag 之前就会 `cd`+创建目录，所以录制中打点时它必然已存在）。

推断可能失败（权限、目录被改名、传下去的 name 与脚本实际用的不一致）。失败时的处置是
明确的：**不静默丢** —— 记录先攒在内存里，下次打点时重试补写；整场录完仍没写进去，
就落到 `<bag_dir>/points_unlinked_<起始时间戳>.csv`，并在界面明说这批点没能关联到录制目录。

## 相对时间的基准

以 bag 目录的**创建时间**为基准，而不是上位机发指令的时间 —— 脚本 source 环境、建目录、
起 `ros2 bag record` 之间有几秒延迟，用发指令时间会让所有点整体偏移。
CSV 每行都带 epoch，所以 `基准 = epoch - 相对录制秒` 可以反推，不必额外加一列。
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .core import logger

#: 打点记录文件名。刻意用 ASCII：远端脚本 / 其他工具的 glob 与 unzip 处理中文名容易出问题，
#: 而表头与内容都是中文，现场用 Excel 打开一样看得懂。
CSV_NAME = "points.csv"
#: 未能关联到录制目录时的兜底文件名（带本次录制起始时间戳，便于对账）。
UNLINKED_TMPL = "points_unlinked_{stamp}.csv"
#: record_bag.sh 在 name 为空时使用的默认名字（见脚本里的 NAME="rosbag2"）。
DEFAULT_RECORD_NAME = "rosbag2"
#: 空标签直接打点时的默认名：现场"先记下来、回头再命名"的那一刻等不起。
PLACEHOLDER_LABEL = "点{n}（待命名）"
_PLACEHOLDER_RE = re.compile(r"^点(\d+)（待命名）$")


def is_placeholder_label(name: str) -> bool:
    """是不是"待命名"的占位标签（`点3（待命名）`）。"""
    return bool(_PLACEHOLDER_RE.match(str(name or "").strip()))


def next_placeholder_label(records) -> str:
    """下一个占位标签名：`点1（待命名）`、`点2（待命名）`…

    取本场录制里已有占位名的**最大编号 + 1**，而不是"现有条数 + 1"：删掉中间那个再记时
    不会撞名，也不会把新的点又叫回一个已经用过的名字。
    """
    biggest = 0
    for record in records:
        matched = _PLACEHOLDER_RE.match(record.label or "")
        if matched:
            biggest = max(biggest, int(matched.group(1)))
    return PLACEHOLDER_LABEL.format(n=biggest + 1)


#: 表头：中文，便于现场直接用 Excel 打开；epoch 列给分析脚本对齐话题时间戳用。
#: 「任务」是本次录制的采集任务名 —— 落在 bag 目录里的那份能从 info.json 看出来，
#: 但未关联时落到 bag 根目录的兜底文件脱离了 bag 目录，必须自带这个信息。
CSV_HEADER = ("序号", "墙钟时间", "epoch", "相对录制秒", "任务", "标签", "标签编号", "备注")


@dataclass(frozen=True)
class PointRecord:
    """一个打点。"""

    index: int                 # 本次录制内的序号，从 1 开始
    epoch: float               # 墙钟时间（Unix 秒）
    rel_seconds: float         # 相对录制起点（秒）
    label: str
    label_id: Optional[int] = None
    note: str = ""
    task: str = ""             # 本次录制的采集任务（打点标签就是按它分组的）

    @property
    def wall_time(self) -> str:
        """本地墙钟时间，带毫秒（打点是秒级操作，毫秒能区分连点）。"""
        millis = int((self.epoch % 1) * 1000)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.epoch)) + f".{millis:03d}"

    @property
    def clock(self) -> str:
        """只到秒的时钟（列表里显示用）。"""
        return time.strftime("%H:%M:%S", time.localtime(self.epoch))

    @property
    def clock_ms(self) -> str:
        """`14:23:07.123` —— 打点面板里显示的时刻。

        面板要能区分"同一个标签连点了两次"，只到秒会让两条记录看起来一模一样。
        """
        millis = int((self.epoch % 1) * 1000)
        return self.clock + f".{millis:03d}"

    def as_row(self) -> List[str]:
        return [
            str(self.index),
            self.wall_time,
            f"{self.epoch:.3f}",
            f"{self.rel_seconds:.3f}",
            self.task,
            self.label,
            "" if self.label_id is None else str(self.label_id),
            self.note,
        ]


class PointRecorder:
    """一次录制会话的打点记录器。

    生命周期与一次录制绑定：录制开始时建、录制结束时 `finalize()`。
    记录**边打边落盘**（不攒到最后一次性写），这样遥操作中途崩溃或断电，
    已经打好的点仍在盘上。
    """

    def __init__(self, bag_dir, name: str = "", started_at: Optional[float] = None,
                 locator: Optional[Callable[[], Optional[Path]]] = None,
                 task: str = ""):
        """
        Args:
            bag_dir: 机器人侧 bag 根目录（`ConfigManager.get_bag_dir()`）。
            name: 本次录制传给脚本的 name（为空时按脚本默认 `rosbag2` 处理）。
            started_at: 本次录制开始时刻（`RecorderManager._started_at`）；
                用于判断哪个目录属于本次录制。
            locator: 自定义目录定位函数（测试注入用），默认按前缀 + 时间推断。
            task: 本次录制的采集任务名（录制弹窗里选的 label）。打点标签按它分组，
                所以定下来之后不再变。
        """
        self.bag_dir = Path(bag_dir)
        self.name = (name or DEFAULT_RECORD_NAME).strip() or DEFAULT_RECORD_NAME
        self.task = str(task or "").strip()
        self.started_at = time.time() if started_at is None else float(started_at)
        self._locator = locator or self._locate_bag_path

        self._bag_path: Optional[Path] = None
        self._base: Optional[float] = None          # 相对时间基准（首次计算后固定）
        self.records: List[PointRecord] = []
        self._pending: List[PointRecord] = []      # 还没写进 CSV 的点（目录尚未出现）
        self.last_error = ""

    # ---------- 目录定位 ----------
    def _locate_bag_path(self) -> Optional[Path]:
        """按 `{name}_` 前缀 + 创建时间推断本次录制的 bag 目录。

        取**最早创建**的那个：同一次录制期间只会新建一个目录，而更早的历史目录
        创建时间小于本次起始时间，已经被过滤掉了。
        """
        prefix = f"{self.name}_"
        candidates = []
        try:
            entries = list(self.bag_dir.iterdir())
        except OSError as exc:
            self.last_error = f"无法读取 bag 目录 {self.bag_dir}：{exc}"
            return None

        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith(prefix):
                continue
            try:
                created = entry.stat().st_ctime
            except OSError:
                continue          # 录制中被移走的条目，忽略
            # 2 秒容差：脚本从"上位机发指令"到"mkdir 出目录"之间有 source 环境等耗时，
            # 加上文件系统的 ctime 精度，卡死在 started_at 会漏掉刚建好的目录
            if created >= self.started_at - 2.0:
                candidates.append((created, entry))

        if not candidates:
            self.last_error = (f"还没找到本次录制的目录（{self.bag_dir} 下 "
                               f"{prefix}* 且创建于录制开始之后）")
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def bag_path(self) -> Optional[Path]:
        """本次录制的 bag 目录；找不到返回 None（并更新 last_error）。"""
        if self._bag_path is not None and self._bag_path.is_dir():
            return self._bag_path
        self._bag_path = self._locator()
        return self._bag_path

    def csv_path(self) -> Optional[Path]:
        path = self.bag_path()
        return (path / CSV_NAME) if path is not None else None

    def base_epoch(self) -> float:
        """相对时间的基准：bag 目录创建时间 > 录制起始时间。

        **首次计算后即固定。** 宁可让全场共用一个（可能偏早几秒的）基准，也不要
        出现"前两个点按发指令时间算、后面的点按目录创建时间算" —— 那样同一份 CSV
        里的相对秒互相不可比，而这种错比"整体偏移几秒"难查得多。

        实际影响很小：打点总是人看着画面手动点的，那时脚本早把目录建好了，
        首次计算就会拿到目录创建时间。
        """
        if self._base is None:
            path = self.bag_path()
            self._base = self.started_at
            if path is not None:
                try:
                    self._base = path.stat().st_ctime
                except OSError:
                    pass
        return self._base

    # ---------- 打点 ----------
    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, label: str, label_id=None, note: str = "",
            epoch: Optional[float] = None) -> PointRecord:
        """记一个点。

        `epoch` 由界面在**点击那一刻**取好传进来 —— 先锁时间、再从容选标签，
        否则选完标签记录的就是"选完的时间"，不是"动作发生的时间"。

        Returns:
            落库的记录（同时尝试写盘；写不进去会进入 pending，不会丢）。
        """
        moment = time.time() if epoch is None else float(epoch)
        record = PointRecord(
            index=len(self.records) + 1,
            epoch=moment,
            rel_seconds=max(moment - self.base_epoch(), 0.0),   # 时钟回拨时不给负数
            label=str(label or "").strip(),
            label_id=label_id,
            note=str(note or "").strip(),
            task=self.task,
        )
        self.records.append(record)
        self._flush(record)
        return record

    def update(self, index: int, label: str, label_id=None, note: str = "") -> bool:
        """改一个点的标签/备注（时间不可改 —— 时间才是打点的本体）。"""
        for position, record in enumerate(self.records):
            if record.index != index:
                continue
            self.records[position] = replace(
                record, label=str(label or "").strip(), label_id=label_id,
                note=str(note or "").strip())
            return self._rewrite()
        return False

    def remove(self, index: int) -> bool:
        """删一个点，并把后面点的序号重排。

        打错点是常态（遥操作时手上正忙着），所以删除要**真删**而不是标记作废 ——
        下游拿到一份带作废行的 CSV，还得自己判断哪行算数。
        """
        kept = [record for record in self.records if record.index != index]
        if len(kept) == len(self.records):
            return False
        self.records = [replace(record, index=position + 1)
                        for position, record in enumerate(kept)]
        return self._rewrite()

    def set_records(self, records: Sequence[PointRecord]) -> None:
        """整体替换（测试与"恢复会话"用）。会重排序号并重写 CSV。"""
        self.records = [replace(record, index=position + 1)
                        for position, record in enumerate(records)]
        self._rewrite()

    # ---------- 落盘 ----------
    def _flush(self, record: PointRecord) -> bool:
        """把新点写进 CSV；目录还没出现就先攒着，下次打点一并补写。

        攒着而不是丢弃：脚本建目录可能晚几秒，那时补写即可 ——
        丢一条打点，现场是**没有任何提示**的，这比晚写几秒糟糕得多。
        """
        path = self.csv_path()
        if path is None:
            self._pending.append(record)
            return False
        rows = [*self._pending, record]
        if not self._append(path, rows):
            self._pending.extend(rows)
            return False
        self._pending.clear()
        self.last_error = ""
        return True

    def _append(self, path: Path, records: Sequence[PointRecord]) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists()
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if fresh:
                    writer.writerow(CSV_HEADER)
                for record in records:
                    writer.writerow(record.as_row())
            return True
        except OSError as exc:
            self.last_error = f"写打点记录失败：{exc}"
            logger.error(f"写打点记录失败（{path}）：{exc}")
            return False

    def _rewrite(self) -> bool:
        """整表重写（增删改之后序号必须连续）。"""
        path = self.csv_path()
        if path is None:
            self.last_error = "还没找到本次录制的目录，改动只保留在界面上"
            return False
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CSV_HEADER)
                for record in self.records:
                    writer.writerow(record.as_row())
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = f"重写打点记录失败：{exc}"
            logger.error(f"重写打点记录失败（{path}）：{exc}")
            return False

    def finalize(self) -> Optional[Path]:
        """录制结束收尾：仍没关联上的点落成一个明确命名的文件。

        Returns:
            兜底文件路径（没有未关联的点时返回 None）。

        宁可多一个"未关联"文件，也不要静默丢。现场看到这个文件就会去查根因
        （通常是传给脚本的 name 与脚本实际用的不一致）。
        """
        if not self._pending:
            return None
        stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(self.started_at))
        path = self.bag_dir / UNLINKED_TMPL.format(stamp=stamp)
        if self._append(path, self._pending):
            logger.warning(f"{len(self._pending)} 条打点记录没能关联到录制目录，"
                           f"已落到 {path}")
            self._pending.clear()
            return path
        return None
