"""数据转换工具（nerv_lerobot）入口的探测与启动契约。

对应 ADR-003。钉住三件事：
  ① 探测的四种判定分支 —— 每一种都必须给出**可照做**的理由，不能只说"不可用"；
  ② 启动命令的形状 —— 直接执行 `run.sh`（末尾 exec，PID 必须是真进程）
     而不是绕一层 bash；
  ③ 界面层「不可用就置灰并写明原因、可用就恢复」这条约定。
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from data_collector import convert_tool


# ---------- 测试夹具 ----------
def _make_repo(root: Path, *, launcher=True, entry=True, python=True,
               python_exec=True) -> Path:
    """按需搭出一个"装好了"的仓库。缺哪一项就对应一条判定分支。"""
    gui = root / "tools" / "gui"
    gui.mkdir(parents=True, exist_ok=True)
    if launcher:
        script = gui / "run.sh"
        script.write_text("#!/usr/bin/env bash\nexec .venv/bin/python x\n")
        # 要真被执行，所以必须有执行位 —— 否则用例会挂在
        # PermissionError 上而不是它想验的那件事上。
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    if entry:
        (gui / "run_gui.py").write_text("print('gui')\n")
    if python:
        venv_bin = root / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        py = venv_bin / "python"
        py.write_text("#!/bin/sh\n")
        if python_exec:
            py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return root


def _write_launcher(repo: Path, body: str) -> Path:
    """改写 run.sh 的内容（保持可执行）。

    单独一个入口是因为改了内容就得重新 chmod —— 漏掉的话用例挂在
    PermissionError 上，报错指向完全无关的地方。
    """
    script = repo / "tools" / "gui" / "run.sh"
    script.write_text(f"#!/usr/bin/env bash\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


# ---------- ① 探测的四种分支 ----------
def test_probe_reports_available_when_everything_is_in_place(tmp_path):
    repo = _make_repo(tmp_path / "nerv_lerobot")

    # allow_broken=True：这条测的是"四项齐全"这个维度，
    # 与"对方界面已知崩溃"闸门无关（那个另有一条用例钉住）。
    target = convert_tool.probe_convert_tool(repo, allow_broken=True)

    assert target.available
    assert target.reason == ""
    assert target.repo == repo
    assert target.launcher == repo / "tools" / "gui" / "run.sh"


def test_probe_flags_a_missing_repo(tmp_path):
    """没装：理由里要点名查了哪个路径，并给出下一步。"""
    target = convert_tool.probe_convert_tool(tmp_path / "nerv_lerobot")

    assert not target.available
    assert "未找到" in target.reason
    # 现场要能照做：说清查了哪儿 + 去哪儿改配置
    assert "nerv_lerobot" in target.reason
    assert "convert_tool_root" in target.reason


def test_probe_flags_a_version_without_the_gui(tmp_path):
    """装了但版本还没有 GUI：不能说成"没装"，否则现场会去重装一遍。"""
    repo = tmp_path / "nerv_lerobot"
    repo.mkdir()
    (repo / "src").mkdir()

    target = convert_tool.probe_convert_tool(repo)

    assert not target.available
    assert "run.sh" in target.reason


def test_probe_flags_an_incomplete_gui_directory(tmp_path):
    repo = _make_repo(tmp_path / "nerv_lerobot", entry=False)

    target = convert_tool.probe_convert_tool(repo)

    assert not target.available
    assert "run_gui.py" in target.reason


def test_probe_flags_a_repo_without_synced_dependencies(tmp_path):
    """装了但没 uv sync：这一条最容易被漏掉 —— run.sh 自己会 exit 1，
    但那时按钮点下去只闪一下，现场不知道是哪一步坏的。"""
    repo = _make_repo(tmp_path / "nerv_lerobot", python=False)

    target = convert_tool.probe_convert_tool(repo)

    assert not target.available
    assert "uv sync" in target.reason


def test_known_broken_gate_greys_out_a_complete_install(tmp_path):
    """对方界面已知崩溃时，即便四项齐全也要置灰。

    为什么要有这条：崩在**另一个窗口**里，现场多半会当成上位机的 bug 报上来。
    与其让人点一下看段错误，不如主动说清"对方界面当前会崩"。
    """
    repo = _make_repo(tmp_path / "nerv_lerobot")

    target = convert_tool.probe_convert_tool(repo)

    assert not target.available
    assert "段错误" in target.reason
    assert "convert_tool_allow_broken" in target.reason   # 给出放行办法


def test_known_broken_gate_can_be_lifted_by_config(tmp_path):
    """现场急用时可显式放行（配置 convert_tool_allow_broken: true）。"""
    repo = _make_repo(tmp_path / "nerv_lerobot")

    target = convert_tool.probe_convert_tool(repo, allow_broken=True)

    assert target.available
    assert target.reason == ""


def test_known_broken_gate_flag_is_togglable():
    """闸门本身是一个显式常量，且默认是保守的那一侧。

    钉住"默认关着闸门"（= 默认置灰）：这个决定是现场定的，
    不该在某次改动里被无声地翻掉。
    """
    assert convert_tool.KNOWN_BROKEN is True
    assert convert_tool.KNOWN_BROKEN_REASON.strip()


def test_allow_broken_comes_from_config(tmp_path):
    cfg = _cfg_with(tmp_path / "x")
    assert convert_tool.convert_tool_allow_broken(cfg) is False

    cfg.app_config["convert_tool_allow_broken"] = True
    assert convert_tool.convert_tool_allow_broken(cfg) is True


def test_probe_flags_a_non_executable_python(tmp_path):
    """文件在但没有执行位：判不可用，并说清怎么修。
    （`os.access(X_OK)` 这一层不能省 —— 目录在不代表能跑。）"""
    repo = _make_repo(tmp_path / "nerv_lerobot", python_exec=False)

    target = convert_tool.probe_convert_tool(repo)

    assert not target.available
    assert "uv sync" in target.reason


def test_every_unavailable_reason_is_actionable(tmp_path):
    """每一条不可用的理由都要同时含"缺什么"与"怎么办"。

    这条是本项目的通用纪律（"灰着不给理由会被读成坏了"），
    在这里具体化为：理由不能只有前半句。
    """
    missing_repo = convert_tool.probe_convert_tool(tmp_path / "nope")
    no_gui = convert_tool.probe_convert_tool(_bare_repo(tmp_path / "a"))
    no_sync = convert_tool.probe_convert_tool(_make_repo(tmp_path / "b", python=False))
    known_broken = convert_tool.probe_convert_tool(
        _make_repo(tmp_path / "c"))       # 四项齐全，但对方界面已知崩溃

    for target in (missing_repo, no_gui, no_sync, known_broken):
        assert not target.available
        assert len(target.reason) > 10, f"理由太短，现场看不懂：{target.reason}"
        # 至少给一个"怎么办"的抓手
        assert any(hint in target.reason
                   for hint in ("convert_tool_root", "uv sync", "版本", "目录",
                                "convert_tool_allow_broken")), \
            f"理由没有下一步动作：{target.reason}"


def _bare_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_describe_is_readable_on_its_own(tmp_path):
    """describe() 的输出会直接进日志区/错误框，必须是完整人话。"""
    repo = _make_repo(tmp_path / "nerv_lerobot")

    ok = convert_tool.probe_convert_tool(repo, allow_broken=True).describe()
    bad = convert_tool.probe_convert_tool(tmp_path / "gone").describe()

    assert str(repo) in ok
    assert "不可用" in bad and "未找到" in bad


# ---------- 只读：探测不许有副作用 ----------
def test_probe_creates_nothing_on_disk(tmp_path):
    """探测必须是纯只读的 —— 它在界面启动时跑，不能成为新的故障源
    （`check_environment` 的既有纪律）。"""
    repo = tmp_path / "nerv_lerobot"
    _make_repo(repo)
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))

    convert_tool.probe_convert_tool(repo)

    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before


def test_probe_does_not_execute_anything(tmp_path, monkeypatch):
    """刻意不做依赖探活（`python -c "import lerobot"`）：那会把探测变成
    一个会失败、会超时、可能留下僵尸的新故障源。这条钉住这个取舍。"""
    def _boom(*args, **kwargs):                       # pragma: no cover
        raise AssertionError("探测里不该执行任何子进程")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    repo = _make_repo(tmp_path / "nerv_lerobot")
    assert convert_tool.probe_convert_tool(repo, allow_broken=True).available


# ---------- ② 启动命令的形状 ----------
def test_launch_cmd_executes_run_sh_directly(tmp_path):
    """必须是 `[run.sh]` 而不是 `["bash", "run.sh"]`。

    `run.sh` 末尾是 `exec`：直接执行时内核把 shell 换成 python，
    Popen 拿到的 PID 就是真进程；绕一层 bash 的话 PID 是那个等待中的
    shell，"还在运行"的判断与后续 kill 都会指向错对象。
    """
    repo = _make_repo(tmp_path / "nerv_lerobot")
    target = convert_tool.probe_convert_tool(repo)

    cmd = convert_tool.build_launch_cmd(target)

    assert cmd == [str(repo / "tools" / "gui" / "run.sh")]
    assert "bash" not in cmd


def test_launch_cmd_is_an_argv_list_not_a_shell_string(tmp_path):
    """命令是 argv 数组而不是拼出来的 shell 字符串。

    这一条顺带解决了"路径含空格"：argv 不经过 shell 解析，
    `Path("/home/x/my repo/run.sh")` 原样传进去就是对的 ——
    用 `"bash -c \"...\""` 拼的话就必须逐段 quote，少一处就静默跑错目录。
    """
    repo = _make_repo(tmp_path / "my repo" / "nerv_lerobot")
    assert " " in str(repo)                       # 确认这个用例覆盖了含空格的路径

    cmd = convert_tool.build_launch_cmd(convert_tool.probe_convert_tool(repo))

    assert cmd == [str(repo / "tools" / "gui" / "run.sh")]
    assert isinstance(cmd, list) and all(isinstance(part, str) for part in cmd)


# ---------- ③ 启动行为 ----------
def test_launch_starts_an_independent_session(tmp_path):
    """必须 `start_new_session=True`。

    本工具 `terminate()` 用 `os.killpg` 杀整个进程组，不独立会话的话
    **关界面会把转换窗口一起带走** —— 而转换是几十分钟的长任务。
    """
    repo = _make_repo(tmp_path / "nerv_lerobot")
    _write_launcher(repo, "sleep 30")
    log = tmp_path / "convert.log"

    proc = convert_tool.launch_convert_tool(
        convert_tool.probe_convert_tool(repo), log_file=log)

    try:
        assert proc.poll() is None, "进程应该还活着"
        # 独立会话：pgid 等于自己的 pid，且不等于父进程的进程组
        assert os.getpgid(proc.pid) == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_launch_runs_in_the_repo_root(tmp_path):
    """cwd 必须是仓库根：即使对方改了 run.sh 里的 cd 逻辑，
    解析相对路径的行为也不变。"""
    repo = _make_repo(tmp_path / "nerv_lerobot")
    _write_launcher(repo, 'pwd > "$PWD/cwd_marker.txt"')

    proc = convert_tool.launch_convert_tool(convert_tool.probe_convert_tool(repo))
    proc.wait(timeout=10)

    assert (repo / "cwd_marker.txt").read_text().strip() == str(repo)


def test_launch_writes_output_to_the_log_file(tmp_path):
    """输出落文件而不是留管道：界面退出后进程仍会继续写，
    管道没人读会写满 64KB 把进程卡死。"""
    repo = _make_repo(tmp_path / "nerv_lerobot")
    _write_launcher(repo, "echo hello-from-convert")
    log = tmp_path / "logs" / "convert_gui.log"

    proc = convert_tool.launch_convert_tool(
        convert_tool.probe_convert_tool(repo), log_file=log)
    proc.wait(timeout=10)

    assert log.exists()
    assert "hello-from-convert" in log.read_text()


def test_launch_refuses_when_the_script_vanished(tmp_path):
    """起之前再 stat 一次实际要执行的那份：探测是界面启动时的快照，
    用户可能在这中间把仓库挪了/删了。"""
    repo = _make_repo(tmp_path / "nerv_lerobot")
    target = convert_tool.probe_convert_tool(repo)
    (repo / "tools" / "gui" / "run.sh").unlink()

    with pytest.raises(FileNotFoundError):
        convert_tool.launch_convert_tool(target)


def test_running_probe_is_false_when_nothing_matches(monkeypatch):
    """`pgrep -f` 判"已经在跑"，但探不到时不许抛 —— 界面线程不能被它卡住。"""
    monkeypatch.setattr(convert_tool.shutil, "which", lambda cmd: None)

    assert convert_tool.is_convert_tool_running() is False


def test_running_probe_survives_a_broken_pgrep(monkeypatch):
    """pgrep 异常/超时一律当"没在跑"：宁可让用户起第二个（顶多重复窗口），
    也不要让界面卡住或崩。"""

    class _Boom:
        @staticmethod
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired("pgrep", 2)

    monkeypatch.setattr(convert_tool.shutil, "which", lambda cmd: "/usr/bin/pgrep")
    monkeypatch.setattr(convert_tool.subprocess, "run", _Boom.run)

    assert convert_tool.is_convert_tool_running() is False


# ---------- 与配置、启动自检的接线 ----------
def _cfg_with(repo_value):
    class _Cfg:
        def __init__(self):
            self.app_config = {"convert_tool_root": str(repo_value)}

        def get_local_log_dir(self):
            return Path("/tmp/dc-test-logs")

    return _Cfg()


def test_repo_root_comes_from_config(tmp_path):
    cfg = _cfg_with(tmp_path / "custom" / "nerv_lerobot")

    resolved = convert_tool.convert_tool_repo(cfg)

    assert resolved == tmp_path / "custom" / "nerv_lerobot"


def test_repo_root_falls_back_to_the_default_location():
    """留空时用默认位置 —— 与 ADR-002 里"另一个仓不进本仓"的边界一致。"""
    cfg = _cfg_with("")

    resolved = convert_tool.convert_tool_repo(cfg)

    assert resolved == Path.home() / "test_ws" / "nerv_lerobot"


def test_log_file_follows_the_local_log_dir(tmp_path):
    cfg = _cfg_with(tmp_path)
    cfg.get_local_log_dir = lambda: tmp_path / "logs"

    assert convert_tool.convert_tool_log_file(cfg) == tmp_path / "logs" / "convert_gui.log"


def test_startup_doctor_reports_the_convert_tool_as_a_warning(tmp_path):
    """必须标 warn 而不是 fail：它只影响"录完之后转格式"这一步，
    缺了完全不影响采集与回传。写成 fail 会让没装它的机器启动就弹红框。"""
    from data_collector.core import ConfigManager

    workspace = tmp_path / "ws"
    (workspace / "install").mkdir(parents=True)
    (workspace / "install" / "setup.bash").write_text("")
    (workspace / "scripts").mkdir(parents=True)
    for name in ("run_nodes.sh", "run_teleop.sh", "record_bag.sh", "record_vla.sh",
                 "record_cmd.sh", "play_bag.sh", "bag_rate_stat.py",
                 "monitor_record_performance.sh", "robot_monitor.py"):
        (workspace / "scripts" / name).write_text("")

    config_path = tmp_path / "app_config.yaml"
    config_path.write_text(
        f"workspace_root: {workspace}\n"
        "teleop_config_path: teleop.yaml\n"
        "scripts_dir: scripts\n"
        "remote_password: x\n"
        f"convert_tool_root: {tmp_path / 'not-installed'}\n",
        encoding="utf-8")

    results = ConfigManager(config_path).check_environment()
    entry = next(r for r in results if r["name"] == "数据转换工具")

    assert entry["ok"] is False
    assert entry["level"] == "warn"          # 不是 fail
    assert "未找到" in entry["detail"]


def test_this_module_does_not_import_qt():
    """探测层必须能脱离 Qt 单测 —— 与对方 `tools/gui/core.py` 同一性质，
    也是它能进启动自检（那里没有界面上下文）的前提。

    查的是 **AST 的真实 import**，不是源码文本 —— 模块 docstring 里
    提到 "PyQt5" 是解释背景，不是依赖；用 `"PyQt5" in source` 判会假红。
    """
    import ast

    tree = ast.parse(Path(convert_tool.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "PyQt5" not in imported
    assert "PySide6" not in imported
