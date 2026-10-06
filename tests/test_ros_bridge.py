"""常驻命令通道**客户端**（`data_collector/ros_bridge.py`）的行为测试。

这一层考的不是 ROS，而是"进程管不管得住"：

* 握手通了才能用（起不来要**说清原因**，而不是把整页功能变成"点了没反应"）；
* 超时/进程死掉 → 返回"未确认"并**把进程收掉**（卡住的通道比没有通道更危险：
  下一条请求会挂在同一个锁上）；下一条命令能自动重来；
* 协议以外的行（rclpy 的日志混进 stdout）**不能**被当成回复；
* 进度行要能透到界面（8 步预设跑十几秒，中途一声不吭会被读成卡住）。

做法：在 tmp 里写一个**假的通道脚本**（纯 python，不 import rclpy），按剧本说话，
然后让客户端去起它 —— 走的是真的 `subprocess`，比 mock 更有底气。
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_collector.ros_bridge import BRIDGE_SCRIPT, PROTOCOL_PREFIX, RosBridge  # noqa: E402

#: 假通道：按 `mode` 演几种现场会遇到的脾气
FAKE_DAEMON = '''#!/usr/bin/env python3
import json, os, sys

PREFIX = "@DC@"
mode = os.environ.get("FAKE_BRIDGE_MODE", "ok")


def emit(obj):
    sys.stdout.write(PREFIX + " " + json.dumps(obj) + "\\n")
    sys.stdout.flush()


if mode == "fatal":
    emit({"ok": False, "fatal": True, "reason": "无法导入 rclpy（假装）"})
    sys.exit(2)

def crash_once(tag):
    """第一次进来就"崩溃"（现场：订阅回调抛异常把进程带走），第二次正常干活。

    用文件当标记 —— 进程重启后还得记得"已经崩过一次了"。
    用 print(..., file=sys.stderr) 逐行写：**不要在假脚本里写换行转义** ——
    这段文本是外层三引号字符串，转义会在用例文件里就被解释掉（第一版就踩了这个）。
    """
    marker = os.environ.get("FAKE_CRASH_MARKER", "")
    if not marker or os.path.exists(marker):
        return False
    with open(marker, "w") as handle:
        handle.write(tag)
    print("Traceback (most recent call last):", file=sys.stderr)
    print("  File ros_command_bridge.py, line 254, in _on_trunk", file=sys.stderr)
    print("ValueError: The truth value of an array with more than one element is "
          "ambiguous.", file=sys.stderr)
    sys.stderr.flush()
    return True


if mode == "junk":
    print("这不是协议行，只是混进 stdout 的杂音")
    sys.stderr.write("这是 stderr 的一行\\n")

if mode == "crash_on_start" and crash_once("start"):
    sys.exit(1)

emit({"ok": True, "event": "ready", "version": 2})

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        request = json.loads(raw)
    except ValueError:
        continue
    if mode == "crash_on_request" and crash_once("request"):
        sys.exit(1)
    op = request.get("op")
    if op == "shutdown":
        emit({"id": request.get("id"), "ok": True})
        break
    if mode == "silent":
        continue                                  # 收下了，就是不做声 —— 客户端该超时
    if op == "publish":
        emit({"id": request["id"], "ok": True, "channel": "常驻通道",
              "items": [{"name": item["name"], "ok": True, "reason": "",
                         "matched": 2, "sent": 3} for item in request.get("items", [])]})
    elif op == "move":
        emit({"id": request["id"], "event": "progress", "line": "  正在等躯干到位"})
        emit({"id": request["id"], "ok": True, "residual": [0.1] * 6, "attempts": 1,
              "matched": 2})
    elif op == "state":
        emit({"id": request["id"], "ok": True, "state": {"trunk": [0] * 6}})
    else:
        emit({"id": request["id"], "ok": False, "reason": "不认识的 op"})
'''


@pytest.fixture
def fake_daemon(tmp_path):
    """写一个假通道脚本并返回 `(bridge, 脚本路径)` 的工厂。"""
    script = tmp_path / "fake_daemon.py"
    script.write_text(FAKE_DAEMON, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)

    def make(mode: str = "ok", workspace_ok: bool = True, script_name=None):
        config = SimpleNamespace(
            workspace=workspace,
            check_workspace=lambda: workspace_ok,
            resolve_script_path=lambda value: Path(value))
        # 「演哪种脾气」用环境变量传 —— 脚本路径必须是**干净的一个路径**，
        # `bash -c` 里拼参数会让 `is_file()` 判不过（这正是第一版踩的坑）
        env = f"export FAKE_BRIDGE_MODE={mode}; "
        env += f"export FAKE_CRASH_MARKER={tmp_path / 'crashed'}; "
        return RosBridge(config, env_prefix=env, script_name=script_name or str(script))

    return make

    return make


def test_client_handshakes_and_publishes(fake_daemon):
    """握手 → 下发 → 逐条目回结果（`ok / matched / sent` 一个不少）。"""
    bridge = fake_daemon()
    try:
        assert bridge.start() is True
        assert bridge.alive is True

        results = bridge.publish([("躯干目标", "/trunk_control/joint_target_position",
                                   "std_msgs/msg/Float32MultiArray", {"data": [0] * 6})])

        assert results["躯干目标"]["ok"] is True
        assert results["躯干目标"]["matched"] == 2
        assert results["躯干目标"]["sent"] == 3
    finally:
        bridge.stop()
    assert bridge.alive is False


def test_client_reports_why_it_could_not_start(fake_daemon):
    """起不来时**原因要具体** —— 界面就靠这句话解释"为什么不能用"。"""
    bridge = fake_daemon(mode="fatal")

    assert bridge.start() is False
    assert "无法导入 rclpy" in bridge.unavailable_reason()
    assert bridge.alive is False


def test_client_refuses_to_start_without_workspace(fake_daemon):
    """工作空间不在就**别起进程**（也不该假装通道可用）。"""
    bridge = fake_daemon(workspace_ok=False)

    assert bridge.start() is False
    assert "工作空间不存在" in bridge.unavailable_reason()


def test_client_reports_missing_script(fake_daemon):
    """通道脚本不在包里 → 说清它该在哪（随交付包发布在 scripts/ 下）。"""
    bridge = fake_daemon(script_name="/nonexistent/" + BRIDGE_SCRIPT)

    assert bridge.start() is False
    assert "找不到通道脚本" in bridge.unavailable_reason()


def test_client_times_out_then_kills_the_process_and_retries(fake_daemon):
    """超时 → 返回"未确认"、**把进程收掉**、下一次调用能重新起来。

    卡住的通道比没有通道更危险：请求会一直挂在同一把锁上，界面表现为"点了没反应"。
    所以超时必须 kill，宁可付一次冷启动。
    """
    bridge = fake_daemon(mode="silent")

    results = bridge.publish([("躯干目标", "/t", "std_msgs/msg/Float32MultiArray",
                               {"data": [0] * 6})], timeout=1.0)

    assert results["躯干目标"]["ok"] is None            # 未确认，既不算成功也不算失败
    assert "没有回话" in results["躯干目标"]["reason"] or \
        "超时" in bridge.unavailable_reason()
    assert bridge.alive is False
    assert bridge._proc is None                          # 进程已经被收掉了
    assert bridge.failures == 1


def test_client_ignores_non_protocol_output(fake_daemon):
    """stdout 上混进来的杂音**不参与判据**（只记下来排障用）。"""
    bridge = fake_daemon(mode="junk")
    try:
        assert bridge.start() is True
        results = bridge.publish([("躯干目标", "/t", "std_msgs/msg/Float32MultiArray",
                                   {"data": [0] * 6})])
        assert results["躯干目标"]["ok"] is True
        assert "杂音" in bridge._stderr_tail()
    finally:
        bridge.stop()


def test_client_forwards_progress_lines(fake_daemon):
    """进度行要能到界面（长动作中途一声不吭会被读成卡住）。"""
    bridge = fake_daemon()
    lines = []
    try:
        result = bridge.move("trunk", [0] * 6, timeout=5.0, on_line=lines.append)
        assert result["ok"] is True and result["residual"] == pytest.approx([0.1] * 6)
        assert any("正在等躯干到位" in line for line in lines)
    finally:
        bridge.stop()


def test_client_counts_requests_and_failures(fake_daemon):
    """留两个计数：这条件通道值不值得长期留着，事后看它（成功/失败比）。"""
    bridge = fake_daemon()
    try:
        bridge.state()
        bridge.state()
        assert bridge.requests == 2 and bridge.failures == 0
    finally:
        bridge.stop()


def test_client_state_is_none_when_the_channel_is_down(fake_daemon):
    """通道不可用时 `state()` 返回 None（调用方据此降级，不要编一个空字典）。"""
    bridge = fake_daemon(mode="fatal")

    assert bridge.state() is None


def test_client_shutdown_is_graceful(fake_daemon):
    """`stop()` 先发 shutdown，进程自己退出（而不是先挨一刀）。"""
    bridge = fake_daemon()
    assert bridge.start() is True
    proc = bridge._proc

    bridge.stop()

    assert proc.poll() is not None
    assert bridge._proc is None
    time.sleep(0.05)                    # 让读线程把 EOF 收完，避免影响后续用例


def test_client_restarts_and_resends_when_the_channel_crashes_on_a_request(fake_daemon):
    """通道**在等回复时崩溃**：重连 + 把同一条命令重发一次。

    为什么要重发：崩溃是"通道自己死了"，命令大概率根本没被执行；重发的代价只是几十毫秒。
    这一条把"通道崩了"从"这一步失败"变成"用户根本看不出来"—— 现场 2026-09-29 就是因为
    通道在**启动**时崩溃，所有运动掉回单次发布的兜底路径，右臂那条 ArmMove 丢了。

    **超时不重发**（见 `request` 的说明）：那是"命令已经发出去了但不知道结果"，
    重发可能让动作做两遍。
    """
    bridge = fake_daemon(mode="crash_on_request")

    results = bridge.publish([("躯干目标", "/t", "std_msgs/msg/Float32MultiArray",
                               {"data": [0] * 6})], timeout=10.0)

    assert results["躯干目标"]["ok"] is True          # 重连之后照样送达
    assert bridge.failures == 1                       # 但如实记了一次崩溃
    assert "ValueError" in bridge._stderr_tail()      # 死因留在排障信息里


def test_client_retries_the_startup_once_when_the_channel_crashes(fake_daemon):
    """通道**在启动阶段崩溃**（还没握手完）：再起一次。

    现场那条崩溃就发生在等状态首帧 —— 比"等回复时崩"更早。只重试后者的话，
    用户看到的仍然是"常驻通道不可用"，然后所有命令走单次发布。

    `fatal`（缺 rclpy / 缺消息包）这种**明确答复不重试**：再起一百次也是同一个答案。
    """
    bridge = fake_daemon(mode="crash_on_start")

    assert bridge.start() is True                     # 第二次起来了
    assert bridge.alive is True
    results = bridge.publish([("躯干目标", "/t", "std_msgs/msg/Float32MultiArray",
                               {"data": [0] * 6})])
    assert results["躯干目标"]["ok"] is True
    bridge.stop()


def test_client_does_not_retry_a_fatal_startup_answer(fake_daemon):
    """脚本明确回 `fatal`（如没装 rclpy）→ **不重试**，如实把原因交给界面。"""
    bridge = fake_daemon(mode="fatal")

    assert bridge.start() is False
    assert "无法导入 rclpy" in bridge.unavailable_reason()
