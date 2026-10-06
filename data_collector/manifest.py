"""录制清单（manifest）：让"这次录制到底完整吗"在事后可回答。

## 要解决的问题

`RecorderManager.health()` 能在**运行时**发现"进程活着但没在落盘"，
这个能力已经很好。但它有个前提：界面还开着。现场的真实故障是 ——
工程师下午开始录，机器人断电，第二天才开机。这时候没有任何进程能告诉他
"昨天下午那次在 2 小时 17 分处中断，前面的数据能不能用"。

这在内部测试时是隐性成本（做测试的人自己记得哪里断了），
交付给采数工程师后就是两种结果：把不确定的数据交上去，或者白录一天。

## 设计取向

- **清单写在 bag 目录里**，跟着数据一起走。不写进上位机的本地状态文件：
  数据换台机器、清单丢了，状态就说不清了。跟着 bag 走才自洽。
- **只追加，不改写既有字段**。`info.json` 是机器人侧 `record_bag.sh` 生成的，
  上位机往里加字段而不动原有内容，老数据照样能被读。
- **状态判定保守**：宁可标"可疑"也不标"完整"。把坏数据说成好的，
  代价是下游训练环节才发现；把好数据标成可疑，代价是工程师多看一眼。
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .core import logger

MANIFEST_NAME = "manifest.json"

# 录制状态取值。刻意不叫 pass/fail —— 它描述的是"发生了什么"，
# 不是"数据好不好"，后者是频率门禁（quality_gate）的事。两件事分开，
# 免得一个字段承担两种含义后谁也说不清。
STATUS_RECORDING = "recording"     # 正在录
STATUS_COMPLETE = "complete"# 正常结束
STATUS_INTERRUPTED = "interrupted" # 异常中断（断电/ 崩溃 / 拔盘）
STATUS_UNKNOWN = "unknown"         # 有清单但判不出来

# 界面展示用的中文。放在这里而不是 UI 层，是为了让文案和状态定义在一起 ——
# 状态只有一份定义，就不该有两处映射。
STATUS_LABELS = {
    STATUS_RECORDING: "录制中",
    STATUS_COMPLETE: "完整",
    STATUS_INTERRUPTED: "中断",
    STATUS_UNKNOWN: "状态未知",
}

# 判定"录制已停止"的时间阈值：最后写入的文件距今超过这么久，
# 且当前没有录制进程，就认为这次录制已经结束。
IDLE_END_SECONDS = 30


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _mtime_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class BagManifest:
    """一次录制对应的清单。

    生命周期：start() → update()（每卷完成）→ finish()（正常结束）。
    若进程异常退出，下次启动时由 `scan_interrupted()` 依据 mtime 与
    进程状态把残留的 `recording` 标记改判为 `interrupted`。
    """

    def __init__(self, bag_dir: Path):
        self.bag_dir = Path(bag_dir)
        self.path = self.bag_dir / MANIFEST_NAME

    # ---------- 读取 ----------

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> Optional[dict]:
        """读清单。损坏时返回 None 而不是抛异常 ——
        清单本身是为了辅助判断的，它坏了不该把整个"查看数据"界面打不开。"""
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"读取 manifest 失败（{self.path}）：{exc}")
            return None

    def status(self) -> str:
        data = self.read()
        return (data or {}).get("status", STATUS_UNKNOWN)

    # ---------- 写入 ----------

    def _write(self, payload: dict) -> None:
        # 先写临时文件再 replace：断电时不会留下"写了一半的 JSON"，
        # 那会让下一次启动既读不出状态、又以为这个包是坏的。
        tmp = self.path.with_name(MANIFEST_NAME + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2, ensure_ascii=False)
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning(f"写入 manifest 失败（{self.path}）：{exc}")

    def start(self, task_name: str = "", operator: str = "",
              script_type: str = "", extra: Optional[dict] = None) -> None:
        """录制开始时写清单。"""
        payload = {
            "schema_version": 1,
            "status": STATUS_RECORDING,
            "task_name": task_name,
            "operator": operator,
            "script_type": script_type,
            "started_at": _now_iso(),
            "started_ts": time.time(),
            "finished_at": "",
            "files": [],
            "note": "",
        }
        if extra:
            payload.update(extra)
        self._write(payload)

    def add_file(self, name: str, size: int = 0) -> None:
        """记录一个产出文件（每完成一卷调一次）。"""
        data = self.read() or {}
        files = data.setdefault("files", [])
        if not isinstance(files, list):
            files = []
            data["files"] = files
        files.append({"name": name, "size": size, "at": _now_iso()})
        self._write(data)

    def finish(self, note: str = "") -> None:
        """录制正常结束。"""
        data = self.read() or {}
        data["status"] = STATUS_COMPLETE
        data["finished_at"] = _now_iso()
        data["finished_ts"] = time.time()
        if note:
            data["note"] = note
        self._write(data)

    # ---------- 事后判定 ----------

    def mark_interrupted(self, reason: str = "") -> dict:
        """把残留的 recording 状态改判为 interrupted。"""
        data = self.read() or {}
        data["status"] = STATUS_INTERRUPTED
        data["finished_at"] = data.get("finished_at") or _now_iso()
        data["note"] = reason or "上次录制未正常结束（可能是断电、崩溃或拔盘）"
        self._write(data)
        return data

    def infer_status(self, recording_active: bool = False) -> str:
        """推断当前状态。

        规则（保守优先）：
        - 清单写着 recording 且**此刻确实在录** → 录制中
        - 清单写着 recording 但**此刻没在录** → 中断
        - 清单写着 complete → 完整
        - 读不出清单 → 状态未知（不是"完整"）
        """
        data = self.read()
        if not data:
            return STATUS_UNKNOWN
        recorded = data.get("status")
        if recorded == STATUS_RECORDING:
            return STATUS_RECORDING if recording_active else STATUS_INTERRUPTED
        if recorded in (STATUS_COMPLETE, STATUS_INTERRUPTED):
            return recorded
        return STATUS_UNKNOWN

    def unresolved(self, recording_active: bool = False) -> bool:
        """是否存在"上次没录完"的痕迹（供界面在启动时提示）。

        `recording_active` 必须传当前是否真在录：否则**正在录制中**的这次
        也会被判成"未完成"，启动提示就会对着正在录的数据喊"上次没录完"。
        """
        return self.infer_status(recording_active=recording_active) == STATUS_INTERRUPTED


def list_manifests(bag_root: Path) -> List[Path]:
    """列出 bag 根目录下所有带清单的录制。"""
    root = Path(bag_root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p / MANIFEST_NAME).exists())


def scan_interrupted(bag_root: Path, recording_active: bool = False) -> List[dict]:
    """扫描未正常结束的录制，返回它们的摘要（供界面提示）。

    启动时调一次即可。**不改写任何文件** —— 判定和落盘分开，
    这样即使判定逻辑有 bug，也不会把好数据标成坏数据。
    """
    found = []
    for bag_dir in list_manifests(bag_root):
        manifest = BagManifest(bag_dir)
        status = manifest.infer_status(recording_active=recording_active)
        if status != STATUS_INTERRUPTED:
            continue
        data = manifest.read() or {}
        started = data.get("started_at", "")
        last_file = ""
        files = data.get("files") or []
        if files and isinstance(files, list):
            last_file = files[-1].get("at", "")
        found.append({
            "bag_dir": bag_dir,
            "name": bag_dir.name,
            "status": STATUS_INTERRUPTED,
            "status_label": STATUS_LABELS[STATUS_INTERRUPTED],
            "task_name": data.get("task_name", ""),
            "operator": data.get("operator", ""),
            "started_at": started,
            "last_file_at": last_file,
            "file_count": len(files) if isinstance(files, list) else 0,
        })
    return found


def write_info_field(bag_dir: Path, key: str, value) -> bool:
    """往 bag 的 `info.json` **追加**一个字段，返回是否成功。

    为什么要追加而不是覆盖：`info.json` 是机器人侧 `record_bag.sh` 生成的
    （机型、序列号、手型、固件版本、名称、描述都在里面）。上位机覆盖它会
    丢掉这些采集条件 —— 而它们恰恰是事后筛选数据的关键依据。

    不做"字段合并"以外的合并：已经是顶层键直接替换（同键更新），
    新键直接加，不动其他任何键。
    """
    info_path = Path(bag_dir) / "info.json"
    data = {}
    if info_path.exists():
        try:
            with open(info_path, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"读取 {info_path} 失败，将新建：{exc}")
    data[key] = value
    tmp = info_path.with_name("info.json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=4, ensure_ascii=False)
        tmp.replace(info_path)
        return True
    except OSError as exc:
        logger.warning(f"写入 {info_path} 失败：{exc}")
        _quiet_unlink(tmp)
        return False


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
