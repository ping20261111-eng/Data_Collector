"""常驻命令通道的客户端：起进程、说话、看门、超时、降级。

对应脚本 `scripts/ros_command_bridge.py`（协议与"为什么要有它"写在那份文件的文件头）。
这一侧只做四件事：

1. **懒启动**：第一次用到才起进程（不在界面启动时就占一个 ROS 进程）；
2. **一次发现、长期复用**：进程活着就一直用 —— 这正是成功率的那笔账：
   `ros2 topic pub --once` 每条都要重新发现（0.3~3s），等不到订阅者就退出码 124，
   被如实报成"未确认送达"。通道里 publisher 建一次，之后每条命令只剩一次 publish；
3. **看见它真的死了**：协议写失败 / 进程退出 / 请求超时 → 标记不可用，**kill 掉重起**，
   并把原因（含 stderr 最后几行）留给界面显示。**超时必须 kill**：一个卡住的通道
   比没有通道更危险（下一次请求会一直挂在同一个锁上）；
4. **说清"为什么用不了"**：`unavailable_reason()` 会给出具体原因（缺脚本 / 缺 rclpy /
   工作空间没有 / 启动报错），而不是把整页功能变成"点了没反应"。
   调用方据此**降级到一次性 CLI 通道**（那条路一直在，没删）。

线程安全：`request()` 全程持锁 —— 界面线程与工作线程都可能下发，串行是刻意的
（通道一次只处理一条命令，协议本身也没有并发语义）。
"""

import json
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .core import logger

#: 协议行前缀（与脚本里的常量一致；改一处要改两处，由契约用例钉住）
PROTOCOL_PREFIX = "@DC@"
#: 通道脚本文件名（随交付包发布在 scripts/ 下，与 motion_stream.py 同一个位置）
BRIDGE_SCRIPT = "ros_command_bridge.py"
#: 启动握手（等 `ready` / `fatal`）的上限（秒）：里面要 rclpy.init + 建 12 个 publisher
STARTUP_TIMEOUT = 25.0
#: 单次请求的默认上限（秒）。`move` 那条会带上自己的 timeout，取两者大的
REQUEST_TIMEOUT = 15.0
#: 保留多少行通道 stderr 用于排障
STDERR_KEEP = 20
#: 通道进程**崩溃**时重连重发的次数（1 次）。超时不重发（见 `request` 的说明）
RESTART_RETRIES = 1
#: **启动阶段**崩溃时再起几次（1 次）。不覆盖 `fatal` / 缺脚本 / 缺工作空间
START_RETRIES = 1


class BridgeUnavailable(Exception):
    """通道不可用（起不来 / 已死）。调用方应当降级到一次性 CLI 通道。"""


class RosBridge:
    """常驻命令通道客户端。**不碰 Qt**，全部接口都是普通方法，可单测。"""

    def __init__(self, config, *, env_prefix: str = "", source_prefix: str = "",
                 script_name: str = BRIDGE_SCRIPT,
                 on_line: Optional[Callable[[str], None]] = None):
        self.config = config
        self.env_prefix = env_prefix
        self.source_prefix = source_prefix
        self.script_name = script_name
        self.on_line = on_line
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._replies: "queue.Queue" = queue.Queue()
        self._stderr: List[str] = []
        self._next_id = 1
        self._ready = False
        self._reason = "还没启动"
        #: 上一次启动是不是"进程自己退了"（那种情况可以重试；`fatal` 不行）
        self._crashed = False
        #: 统计（界面/日志用：这件事值不值得长期留着，看这两个数）
        self.requests = 0
        self.failures = 0

    # ---------- 生命周期 ----------
    @property
    def alive(self) -> bool:
        """通道是否可用（进程在 + 握手过 + 没被判死）。"""
        proc = self._proc
        return bool(proc is not None and proc.poll() is None and self._ready)

    def unavailable_reason(self) -> str:
        """为什么用不了（可直接显示给现场）。"""
        return self._reason

    def script_path(self) -> Path:
        return Path(self.config.resolve_script_path(self.script_name))

    def start(self) -> bool:
        """起进程并完成握手。已就绪时直接返回 True。

        **启动阶段崩了就再试一次**（`START_RETRIES = 1`）：现场 2026-09-29 那次是
        "订阅回调抛异常 → 进程退出码 1"，而它发生在**等状态首帧**（还在握手阶段）——
        如果只重试"等回复时崩"，命令就会直接掉回单次发布的兜底路径。
        重试只针对"进程自己退了"；`fatal`（缺 rclpy / 缺消息包）这种**明确答复**不重试，
        脚本不存在、工作空间不在也不重试。
        """
        with self._lock:
            for attempt in range(START_RETRIES + 1):
                if self._start_once():
                    return True
                if attempt >= START_RETRIES or not self._crashed:
                    break
                logger.warning(f"常驻命令通道启动时崩了，**再试一次**：{self._reason}")
                self._crashed = False
            return False

    def _start_once(self) -> bool:
        """起一次（不重试）。失败时把原因写进 `_reason`，并在**进程自己退了**时置 `_crashed`。"""
        with self._lock:
            self._crashed = False
            if not self.config.check_workspace():
                self._reason = f"工作空间不存在（{self.config.workspace}）"
                return False
            script = self.script_path()
            if not script.is_file():
                self._reason = (f"找不到通道脚本 {script} —— 它随上位机交付包发布在 "
                                f"scripts/ 下（与 motion_stream.py 同目录）")
                logger.warning(self._reason)
                return False

            cmd = (f"cd {shlex.quote(str(self.config.workspace))} && {self.env_prefix}"
                   f"{self.source_prefix}python3 {shlex.quote(str(script))}")
            logger.info(f"启动常驻命令通道：{cmd}")
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
            except OSError as exc:
                self._reason = f"起不了通道进程：{exc}"
                logger.warning(self._reason)
                return False
            self._proc = proc
            self._ready = False
            self._reason = "正在启动"
            threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
            threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()

            deadline = time.monotonic() + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    self._reason = (f"通道进程已退出（退出码 {proc.returncode}）："
                                    + self._stderr_tail())
                    self._crashed = True
                    logger.warning(self._reason)
                    self._stop_locked()
                    return False
                try:
                    reply = self._replies.get(timeout=0.2)
                except queue.Empty:
                    continue
                if reply.get("event") == "ready":
                    self._ready = True
                    self._reason = ""
                    logger.info("常驻命令通道就绪")
                    return True
                if reply.get("fatal"):
                    self._reason = str(reply.get("reason") or "启动失败")
                    logger.warning(f"常驻命令通道启动失败：{self._reason}")
                    self._stop_locked()
                    return False
                self._handle_unsolicited(reply)
            self._reason = f"通道启动超时（{STARTUP_TIMEOUT:g}s，没等到就绪握手）"
            logger.warning(self._reason)
            self._stop_locked()
            return False

    def stop(self) -> None:
        """优雅关掉（发 shutdown，等 1 秒，再收尾）。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._write({"id": 0, "op": "shutdown"})
                    self._proc.wait(timeout=1.0)
                except Exception:               # noqa: BLE001 —— 关不掉就直接杀，别拖
                    pass
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:                       # noqa: BLE001
            pass

    # ---------- 请求 ----------
    def request(self, payload: dict, timeout: float = REQUEST_TIMEOUT,
                on_line: Optional[Callable[[str], None]] = None) -> Optional[dict]:
        """发一条请求并等回复；``None`` = **未确认**（超时/通道不可用）。

        两条现场定下来的规矩：

        * **超时一律 kill 通道**：一个卡死的通道留着只会让后续每条命令都超时；
        * **进程崩溃则重连重发一次**（`RESTART_RETRIES = 1`）：崩溃是"通道自己死了"，
          命令大概率根本没被执行，重连后重发是对的 —— 这一条把"通道崩了"从
          "这一步失败"变成"用户根本看不出来"（现场 2026-09-29 就是因为回调抛异常
          让通道退出码 1，于是所有运动都掉回单次发布的兜底路径）。
          **超时不重发**：那是"命令已经发出去了但不知道结果"，重发可能动作做两遍。
        """
        sink = on_line or self.on_line
        with self._lock:
            if not self.alive and not self.start():
                return None
            request_id = self._next_id
            self._next_id += 1
            message = dict(payload)
            message["id"] = request_id
            self.requests += 1
            if not self._write(message):
                return None
            restarts_left = RESTART_RETRIES
            deadline = time.monotonic() + float(timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failures += 1
                    self._reason = f"命令超时（{timeout:g}s）：通道没回话，已重启通道"
                    logger.warning(f"常驻命令通道{self._reason}")
                    self._stop_locked()
                    return None
                try:
                    reply = self._replies.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    if self._proc is not None and self._proc.poll() is not None:
                        self.failures += 1
                        self._reason = "通道进程在等回复时退出了：" + self._stderr_tail()
                        logger.warning(self._reason)
                        self._stop_locked()
                        if restarts_left > 0 and self.start():
                            restarts_left -= 1
                            logger.warning("已验证通道已重新起来，**重发这条命令**"
                                           f"（id={request_id}）")
                            if not self._write(message):
                                return None
                            deadline = time.monotonic() + float(timeout)
                            continue
                        return None
                    continue
                if reply.get("id") != request_id:
                    self._handle_unsolicited(reply, sink)
                    continue
                if reply.get("event") == "progress":
                    if sink is not None:
                        try:
                            sink(str(reply.get("line", "")))
                        except Exception as exc:    # noqa: BLE001 —— 界面的问题不该打断命令
                            logger.warning(f"通道进度回调失败: {exc}")
                    continue
                return reply

    def _write(self, message: dict) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError) as exc:
            self._reason = f"写通道失败：{exc}"
            logger.warning(self._reason)
            self._stop_locked()
            return False

    # ---------- 读取端 ----------
    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        try:
            for raw in proc.stdout or []:
                line = raw.rstrip("\n")
                if not line:
                    continue
                if line.startswith(PROTOCOL_PREFIX):
                    try:
                        self._replies.put(json.loads(line[len(PROTOCOL_PREFIX):]))
                        continue
                    except ValueError:
                        pass
                # 不是协议行：记下来（排障用），**不**当成回复 —— 协议以外的东西永远不该
                # 影响判据，这一条是本项目"失败语义只有一套"的延续
                self._keep(f"stdout: {line}")
        finally:
            self._replies.put({"event": "eof"})

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr or []:
            line = raw.rstrip("\n")
            if line:
                self._keep(line)

    def _keep(self, line: str) -> None:
        self._stderr.append(line)
        del self._stderr[:-STDERR_KEEP]

    def _stderr_tail(self) -> str:
        """通道 stderr 的最后几行（**排障用**）。

        留 4 行而不是 3 行：现场那条崩溃的完整信息是
        `File ... line 254, in _on_trunk` + `values = list(... or [])` + 异常行 + 消息，
        少一行就只剩"通道进程已退出"，看不出死因。
        """
        return " / ".join(self._stderr[-4:]) if self._stderr else "（通道没有输出）"

    def _handle_unsolicited(self, reply: dict, sink=None) -> None:
        """收到对不上号的回复：记为通道输出（宁可多记一行，也不要静默丢掉原因）。"""
        text = reply.get("reason") or reply.get("line")
        if text:
            self._keep(f"未匹配的消息：{text}")
            if sink is not None and reply.get("event") == "progress":
                try:
                    sink(str(text))
                except Exception:               # noqa: BLE001
                    pass

    # ---------- 语义层（调用方用的就是这四个） ----------
    def publish(self, entries, timeout: float = REQUEST_TIMEOUT,
                on_line: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, dict]]:
        """批量下发一次性条目，返回 ``{条目名: {ok, reason, matched, sent}}``。"""
        items = []
        for name, topic, msg_type, payload in entries:
            item = {"name": name, "topic": topic, "type": msg_type, "payload": payload}
            items.append(item)
        reply = self.request({"op": "publish", "items": items},
                             timeout=timeout, on_line=on_line)
        if not reply or "items" not in reply:
            reason = (reply or {}).get("reason") or self._reason or "通道没有回话"
            return {item["name"]: {"ok": None, "reason": reason, "matched": -1, "sent": 0}
                    for item in items}
        out = {}
        for item in reply["items"]:
            out[item["name"]] = {"ok": bool(item.get("ok")),
                                 "reason": str(item.get("reason") or ""),
                                 "matched": int(item.get("matched", -1)),
                                 "sent": int(item.get("sent", 0))}
        return out

    def move(self, kind: str, target, *, side: Optional[str] = None,
             tol: Optional[float] = None, timeout: float = 15.0, retries: int = 2,
             on_line: Optional[Callable[[str], None]] = None) -> dict:
        """下发一条运动目标并确认到位；``ok`` 取 ``None`` = 未确认。"""
        payload = {"op": "move", "kind": kind, "target": [float(item) for item in target],
                   "timeout": float(timeout), "retries": int(retries)}
        if side:
            payload["side"] = side
        if tol is not None:
            payload["tol"] = float(tol)
        reply = self.request(payload, timeout=float(timeout) + 5.0, on_line=on_line)
        if not reply:
            return {"ok": None, "reason": self._reason or "通道没有回话",
                    "residual": None, "attempts": 0, "matched": -1}
        reply.pop("id", None)
        return reply

    def move_batch(self, items, *, timeout: float = 15.0, retries: int = 2,
                   on_line=None) -> dict:
        """一次下发**多条**目标并**一起**等到位（左右臂的镜像动作要同时开始）。

        `items` 形如 ``[{"kind": "arm", "side": "left", "target": [度×7]}, ...]``。
        返回 ``{ok, items: {键: {ok, residual, attempts}}, attempts, reason}`` ——
        `attempts == 0` 表示**通道没跑起来**（调用方据此降级到一次性 CLI）。
        """
        payload = {"op": "move_batch",
                   "items": [{"kind": item["kind"], "side": item.get("side"),
                              "target": [float(v) for v in item["target"]],
                              **({"tol": float(item["tol"])} if item.get("tol") is not None else {})}
                             for item in items],
                   "timeout": float(timeout), "retries": int(retries)}
        reply = self.request(payload, timeout=float(timeout) + 5.0, on_line=on_line)
        if not reply:
            return {"ok": None, "items": {}, "attempts": 0, "reason":
                    self._reason or "通道没有回话"}
        reply.pop("id", None)
        reply["attempts"] = max((item.get("attempts", 0)
                                 for item in (reply.get("items") or {}).values()),
                                default=0)
        return reply

    def ensure(self, what: str, side: Optional[str] = None, timeout: float = 20.0,
               on_line: Optional[Callable[[str], None]] = None) -> dict:
        """模式前置条件（切非实时 / 退出跟随）。"""
        payload = {"op": "ensure", "what": what}
        if side:
            payload["side"] = side
        reply = self.request(payload, timeout=timeout, on_line=on_line)
        if not reply:
            return {"ok": None, "reason": self._reason or "通道没有回话"}
        reply.pop("id", None)
        return reply

    def state(self, timeout: float = 5.0) -> Optional[dict]:
        reply = self.request({"op": "state"}, timeout=timeout)
        return (reply or {}).get("state")
