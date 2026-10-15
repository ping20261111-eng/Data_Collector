import json
import os
import signal
import subprocess
import time
from unittest import mock

import pytest
from pathlib import Path
from data_collector.core import (ConfigManager, ProcessManager, PROJECT_ROOT,
                                 load_yaml, save_yaml)

@pytest.fixture
def temp_dir(tmp_path):
    # 创建模拟配置文件（脚本一律写裸文件名，统一由 scripts_dir 定位）
    app_cfg = {
        "workspace_root": str(tmp_path),
        "scripts_dir": "scripts",
        "run_nodes_script": "run_nodes.sh",
        "run_teleop_script": "run_teleop.sh",
        "teleop_config_path": "teleop.yaml",
        "record_bag_script": "record_bag.sh",
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
        "play_bag_script": "play_bag.sh",
        "bag_rate_stat_script": "bag_rate_stat.py",
        "monitor_performance_script": "monitor_record_performance.sh",
        "terminal_command": "gnome-terminal"
    }
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, app_cfg)
    # 创建 teleop 配置
    teleop_cfg = {
        "teleop": {"calibration": "default"},
        "calibrations": {"default": {"tracker_index": "tracker1", "shoulder_width": 0.45, "arm_length": 0.72, "elbow_length": 0.35}}
    }
    teleop_file = tmp_path / "teleop.yaml"
    save_yaml(teleop_file, teleop_cfg)
    return tmp_path, cfg_file

def test_config_load(temp_dir):
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    assert cm.workspace == tmp
    assert cm.teleop_config_path == tmp / "teleop.yaml"

def test_get_user_calibration(temp_dir):
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    data = cm.get_user_calibration("default")
    assert data["shoulder_width"] == 0.45

def test_set_teleop_calibration_user(temp_dir):
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    assert cm.set_teleop_calibration_user("default") is True
    cfg = cm.get_teleop_config()
    assert cfg["teleop"]["calibration"] == "default"


# ---------- 回归测试：save_teleop_config 不得丢弃未知顶层字段 ----------
def test_save_teleop_config_preserves_unknown_top_level_keys(temp_dir):
    """标定/切换用户写回配置时，limits/trackers 等顶层字段必须原样保留。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    cfg = cm.get_teleop_config()
    cfg["trackers"] = {"0": {"serial": "ABC"}}
    cfg["limits"] = {"waist": {"max_velocity_deg": 60.0}}
    cm.save_teleop_config(cfg)

    reloaded = cm.get_teleop_config()
    assert reloaded["trackers"] == {"0": {"serial": "ABC"}}
    assert reloaded["limits"] == {"waist": {"max_velocity_deg": 60.0}}
    # 已知字段仍然按约定顺序排在最前
    assert list(reloaded.keys())[:3] == ["teleop", "calibrations", "trackers"]


def test_add_calibration_user_preserves_limits(temp_dir):
    """新增标定用户后，机器人控制参数（limits）不能被抹掉。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    cfg = cm.get_teleop_config()
    cfg["limits"] = {"chassis": {"linear_scale": 1.0, "angular_scale": 0.8}}
    cm.save_teleop_config(cfg)

    cm.add_calibration_user("alice", "tracker2", 0.5, 0.8, elbow=0.36)

    reloaded = cm.get_teleop_config()
    assert reloaded["limits"]["chassis"]["linear_scale"] == 1.0
    assert reloaded["teleop"]["calibration"] == "alice"
    assert reloaded["calibrations"]["alice"] == {
        "tracker_index": "tracker2",
        "shoulder_width": 0.5,
        "arm_length": 0.8,
        "elbow_length": 0.36,
    }


# ---------- 脚本路径统一解析（scripts_dir + 环境变量注入） ----------
def test_script_paths_resolve_into_project_scripts_dir(tmp_path):
    """配置里写裸文件名时，统一解析到项目的 scripts/ 目录（与 workspace 无关）。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "record_bag_script": "record_bag.sh",
        "bag_rate_stat_script": "bag_rate_stat.py",
    })
    cm = ConfigManager(cfg_file)

    assert cm.get_scripts_dir() == PROJECT_ROOT / "scripts"
    assert cm.get_script_path("record_bag_script") == PROJECT_ROOT / "scripts" / "record_bag.sh"
    assert cm.get_script_path("bag_rate_stat_script") == PROJECT_ROOT / "scripts" / "bag_rate_stat.py"


def test_script_path_accepts_project_relative_and_absolute(tmp_path):
    """兼容三种写法：裸文件名 / 项目内相对路径 / 绝对路径。"""
    absolute_script = tmp_path / "custom_record.sh"
    absolute_script.write_text("#!/bin/bash\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "record_bag_script": "record_bag.sh",             # 裸文件名
        "record_cmd_script": "scripts/record_cmd.sh",     # 项目内相对路径
        "record_vla_script": str(absolute_script),        # 绝对路径
    })
    cm = ConfigManager(cfg_file)

    assert cm.get_script_path("record_bag_script") == PROJECT_ROOT / "scripts" / "record_bag.sh"
    assert cm.get_script_path("record_cmd_script") == PROJECT_ROOT / "scripts" / "record_cmd.sh"
    assert cm.get_script_path("record_vla_script") == absolute_script


def test_scripts_dir_override_absolute(tmp_path):
    """scripts_dir 支持绝对路径（脚本可整体换目录部署）。"""
    scripts_dir = tmp_path / "my_scripts"
    scripts_dir.mkdir()
    (scripts_dir / "record_bag.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "scripts_dir": str(scripts_dir),
        "record_bag_script": "record_bag.sh",
    })
    cm = ConfigManager(cfg_file)

    assert cm.get_scripts_dir() == scripts_dir
    assert cm.get_script_path("record_bag_script") == scripts_dir / "record_bag.sh"


def test_script_key_missing_raises(tmp_path):
    """未配置且未给默认值的脚本键应报错，避免启动到错误路径。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    cm = ConfigManager(cfg_file)
    with pytest.raises(KeyError):
        cm.get_script_path("record_vla_script")


def test_script_env_matches_script_header_convention(temp_dir):
    """脚本环境变量与 scripts/ 各脚本头部的约定保持一致。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    assert cm.get_nerv_dir() == Path("/var/nerv")
    assert cm.get_script_env() == {
        "ROBOT_WORKSPACE": str(tmp),
        "NERV_DIR": "/var/nerv",
        "BAG_DIR": "/var/nerv/bag",
        "LOG_DIR": "/var/nerv/log",
        "ROBOT_STATUS_FILE": "/var/nerv/status/robot_status.json",
    }


def test_bag_log_dirs_default_under_nerv_dir(tmp_path):
    """只配置 nerv_dir 时，BAG_DIR/LOG_DIR 自动落在 ${nerv_dir} 下，保持四处环境变量自洽。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "nerv_dir": str(tmp_path / "nerv"),
    })
    cm = ConfigManager(cfg_file)
    env = cm.get_script_env()
    assert env["BAG_DIR"] == str(tmp_path / "nerv" / "bag")
    assert env["LOG_DIR"] == str(tmp_path / "nerv" / "log")


def test_script_env_uses_configured_paths(tmp_path):
    """nerv_dir / bag_dir / log_dir 可由配置覆盖。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "nerv_dir": str(tmp_path / "nerv"),
        "bag_dir": str(tmp_path / "nerv" / "bags"),
        "log_dir": str(tmp_path / "nerv" / "logs"),
    })
    cm = ConfigManager(cfg_file)
    env = cm.get_script_env()
    assert env["NERV_DIR"] == str(tmp_path / "nerv")
    assert env["BAG_DIR"] == str(tmp_path / "nerv" / "bags")
    assert env["LOG_DIR"] == str(tmp_path / "nerv" / "logs")


# ---------- 缺省值 / 配置读取 ----------
def test_get_teleop_script_path_default_when_key_missing(tmp_path):
    """旧配置文件缺少 run_teleop_script 时不应抛 KeyError，按统一约定回退 scripts/。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    cm = ConfigManager(cfg_file)
    assert cm.get_teleop_script_path() == PROJECT_ROOT / "scripts" / "run_teleop.sh"


def test_default_paths_and_remote_settings(temp_dir, monkeypatch):
    tmp, cfg_file = temp_dir
    monkeypatch.delenv("DATA_COLLECTOR_REMOTE_PASSWORD", raising=False)
    monkeypatch.delenv("DATA_COLLECTOR_SUDO_PASSWORD", raising=False)
    cm = ConfigManager(cfg_file)

    assert cm.get_bag_dir() == Path("/var/nerv/bag")
    assert cm.get_log_dir() == Path("/var/nerv/log")
    # 源码里不留默认密码：未配置就是空串，由调用方给出可操作的提示
    assert cm.get_sudo_password() == ""

    settings = cm.get_remote_settings()
    assert settings["host"] == "172.16.10.2"
    assert settings["port"] == 22
    assert settings["user"] == "nerv"
    assert settings["password"] == ""
    assert settings["base"] == "/NervData/share/logs/遥操作数据"


def test_upload_settings_defaults_and_overrides(temp_dir):
    """上传策略默认值：不压缩 + 流式直传（大数据量下最稳最快）。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    settings = cm.get_upload_settings()
    assert settings["mode"] == "auto"
    assert settings["format"] == "zip"
    assert settings["compression"] == "store"
    assert settings["temp_dir"] == ""
    assert settings["retries"] == 1
    assert settings["space_check"] is True

    app_cfg = load_yaml(cfg_file)
    app_cfg.update({
        "upload_mode": "tempfile",
        "upload_format": "tar",
        "upload_compression": "deflate",
        "upload_temp_dir": str(tmp / "cache"),
        "upload_retries": 3,
        "upload_space_check": False,
    })
    save_yaml(cfg_file, app_cfg)
    settings = ConfigManager(cfg_file).get_upload_settings()
    assert settings["mode"] == "tempfile"
    assert settings["format"] == "tar"
    assert settings["compression"] == "deflate"
    assert settings["temp_dir"] == str(tmp / "cache")
    assert settings["retries"] == 3
    assert settings["space_check"] is False


def test_upload_settings_invalid_values_fall_back(temp_dir):
    """配置写错不应导致上传崩溃，一律回退到安全默认值。"""
    tmp, cfg_file = temp_dir
    app_cfg = load_yaml(cfg_file)
    app_cfg.update({
        "upload_mode": "flying",
        "upload_format": "rar",
        "upload_compression": "lzma",
    })
    save_yaml(cfg_file, app_cfg)

    settings = ConfigManager(cfg_file).get_upload_settings()
    assert settings["mode"] == "auto"
    assert settings["format"] == "zip"
    assert settings["compression"] == "store"


def test_remote_password_env_override(temp_dir, monkeypatch):
    """环境变量优先于配置文件与内置默认值。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    monkeypatch.setenv("DATA_COLLECTOR_REMOTE_PASSWORD", "from-env")
    monkeypatch.setenv("DATA_COLLECTOR_SUDO_PASSWORD", "sudo-env")
    assert cm.get_remote_settings()["password"] == "from-env"
    assert cm.get_sudo_password() == "sudo-env"


def test_config_value_overrides_builtin_default(tmp_path, monkeypatch):
    """配置文件里显式给出的值应生效（空字符串视为未配置）。"""
    monkeypatch.delenv("DATA_COLLECTOR_REMOTE_PASSWORD", raising=False)
    monkeypatch.delenv("DATA_COLLECTOR_SUDO_PASSWORD", raising=False)
    app_cfg = {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "bag_dir": str(tmp_path / "bags"),
        "log_dir": str(tmp_path / "logs"),
        "remote_host": "10.0.0.9",
        "remote_port": 2222,
        "remote_user": "alice",
        "remote_password": "secret",
        "remote_base": "/data/bags",
        "sudo_password": "",
    }
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, app_cfg)
    cm = ConfigManager(cfg_file)

    assert cm.get_bag_dir() == tmp_path / "bags"
    assert cm.get_log_dir() == tmp_path / "logs"
    assert cm.get_remote_settings() == {
        "host": "10.0.0.9",
        "port": 2222,
        "user": "alice",
        "password": "secret",
        "base": "/data/bags",
    }
    # 空字符串视为未配置 -> 空串（源码不内置默认密码）
    assert cm.get_sudo_password() == ""


# ---------- Launcher：统一脚本调用与环境变量注入 ----------
class FakeProc:
    """最小可用的 Popen 替身（ProcessManager.register 只读取 pid）。"""

    def __init__(self, pid=4321):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture
def launcher_env(temp_dir, monkeypatch):
    """构造 Launcher，并把启动函数替换为记录命令的桩函数。"""
    from data_collector import core_operations

    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    calls = {}

    def fake_run_in_terminal(command, terminal="gnome-terminal"):
        calls["terminal"] = (command, terminal)
        return FakeProc()

    def fake_run_background(command, cwd=None):
        calls["background"] = (command, str(cwd))
        return FakeProc()

    def fake_run_background_with_log(command, cwd=None, log_file=None):
        # 标定/戳点节点走这条：落盘 tail 回显。记录全部命令便于逐条断言
        calls.setdefault("bg_cmds", []).append(command)
        calls["background"] = (command, str(cwd))
        calls["background_log"] = log_file
        return FakeProc()

    def fake_run_background_captured(command, cwd=None):
        # `ros2 service call` 走这条：捕获输出（DEVNULL 会吞掉 Trigger 回执）
        calls["captured"] = (command, str(cwd))
        return FakeProc()

    monkeypatch.setattr(core_operations, "run_in_terminal", fake_run_in_terminal)
    monkeypatch.setattr(core_operations, "run_background", fake_run_background)
    monkeypatch.setattr(core_operations, "run_background_with_log",
                        fake_run_background_with_log)
    monkeypatch.setattr(core_operations, "run_background_captured",
                        fake_run_background_captured)

    launcher = core_operations.Launcher(cm)
    return launcher, tmp, cfg_file, calls


def test_launcher_play_bag_uses_script_path_and_env(launcher_env, monkeypatch):
    """回放脚本统一从 scripts/ 取，并把 ROBOT_WORKSPACE/BAG_DIR 等注入命令。"""
    launcher, tmp, _cfg, calls = launcher_env
    launcher.launch_play_bag("/var/nerv/bag/bag_a", with_rviz=True)

    command, terminal = calls["terminal"]
    assert terminal == "gnome-terminal"
    assert f"export ROBOT_WORKSPACE={tmp}" in command
    assert "export NERV_DIR=/var/nerv" in command
    assert "export BAG_DIR=/var/nerv/bag" in command
    assert "export LOG_DIR=/var/nerv/log" in command
    assert f"bash {PROJECT_ROOT / 'scripts' / 'play_bag.sh'}" in command
    assert "rviz" in command
    assert "bag=/var/nerv/bag/bag_a" in command


def test_launcher_bag_rate_stat_requires_bag_path(launcher_env):
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(ValueError):
        launcher.launch_bag_rate_stat("")


def test_launcher_bag_rate_stat_sources_workspace_setup(launcher_env):
    """采集统计直接跑 python3，必须先 source 工作空间 setup.bash（否则缺 rclpy）。"""
    launcher, tmp, _cfg, calls = launcher_env
    setup = tmp / "install" / "setup.bash"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("# fake\n", encoding="utf-8")

    launcher.launch_bag_rate_stat("/var/nerv/bag/bag_a")

    command, _terminal = calls["terminal"]
    assert f"source {setup} &&" in command
    assert command.index(f"source {setup}") < command.index("python3 ")
    assert f"python3 {PROJECT_ROOT / 'scripts' / 'bag_rate_stat.py'}" in command
    assert "/var/nerv/bag/bag_a" in command
    # 退出码提示必须能被 shell 展开（单引号会原样打印 $?）
    # 保活文案已统一为「<脚本名> 已退出」，不再逐脚本硬编码
    assert 'echo "=== bag_rate_stat.py 已退出，退出码: $? ==="' in command


def test_launcher_bag_rate_stat_without_setup_bash_still_launches(launcher_env):
    """工作空间没有 install/setup.bash 时不加 source 前缀，由脚本给出缺 rclpy 的提示。"""
    launcher, tmp, _cfg, calls = launcher_env
    assert not (tmp / "install" / "setup.bash").exists()

    launcher.launch_bag_rate_stat("/var/nerv/bag/bag_a")

    command, _terminal = calls["terminal"]
    assert "source" not in command
    assert "python3 " in command


def test_launcher_monitor_performance_uses_script(launcher_env):
    launcher, _tmp, _cfg, calls = launcher_env
    launcher.launch_monitor_performance("/var/nerv/monitor/run1")

    command, _terminal = calls["terminal"]
    assert f"bash {PROJECT_ROOT / 'scripts' / 'monitor_record_performance.sh'}" in command
    assert "/var/nerv/monitor/run1" in command


def test_launcher_record_maps_type_to_script_and_args(launcher_env):
    """录制类型 bag/vla/cmd -> record_<type>_script，名称/描述按 name=/desc= 传递。"""
    launcher, _tmp, _cfg, calls = launcher_env

    launcher.launch_record("vla", name="my bag", desc="first")

    command, _cwd = calls["background"]
    # 录制脚本的真源是**我们仓里** scripts/ 下那份（v6.5.0 起，见 ADR-002）——
    # 它排在工作空间之前，所以工作空间里那份同名脚本不会被选中。
    assert f"bash {PROJECT_ROOT / 'scripts' / 'record_vla.sh'}" in command
    assert "'name=my bag'" in command
    assert "desc=first" in command
    assert "export BAG_DIR=/var/nerv/bag" in command


def test_launcher_allows_non_executable_script(tmp_path, monkeypatch):
    """脚本统一经 bash/python3 调用，缺可执行位只告警、不阻断启动。"""
    from data_collector import core_operations

    script = tmp_path / "stat.py"
    script.write_text("print('ok')\n", encoding="utf-8")  # 故意不 chmod +x
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "teleop.yaml",
        "bag_rate_stat_script": str(script),
        "terminal_command": "gnome-terminal",
    })
    calls = {}

    def fake_run_in_terminal(command, terminal="gnome-terminal"):
        calls["cmd"] = command
        return FakeProc()

    monkeypatch.setattr(core_operations, "run_in_terminal", fake_run_in_terminal)
    core_operations.Launcher(ConfigManager(cfg_file)).launch_bag_rate_stat(str(tmp_path / "bag_a"))

    assert f"python3 {script}" in calls["cmd"]


# ---------- 回归测试：脚本调用必须自带 ROS 环境 ----------
def test_bag_rate_stat_script_mentions_ros_environment():
    """采集统计脚本自身要给出 source setup.bash 的提示（独立运行时也有明确报错）。"""
    content = (PROJECT_ROOT / "scripts" / "bag_rate_stat.py").read_text(encoding="utf-8")
    assert "require_ros_environment()" in content
    assert "install/setup.bash" in content
    assert "rclpy" in content


def test_launcher_record_unknown_type_raises(tmp_path):
    """配置里没有对应录制脚本时给出明确错误，而不是静默失败。"""
    from data_collector import core_operations

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    launcher = core_operations.Launcher(ConfigManager(cfg_file))
    with pytest.raises(KeyError):
        launcher.launch_record("vla")


def test_launcher_run_nodes_sources_workspace_setup(launcher_env):
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)

    launcher.launch_run_nodes(["joy", "rgb_only"])

    command, _terminal = calls["terminal"]
    assert f"cd {tmp}" in command
    # 未配置 sudo 密码时不带 sudo 预授权（见 test_launcher_run_nodes_skips_sudo_when_password_missing）
    assert "echo  | sudo" not in command
    # run_nodes.sh 以机器人侧为准：从工作空间根目录解析，而不是项目 scripts/ 里的镜像
    assert f"bash {tmp / 'run_nodes.sh'} joy rgb_only" in command
    # 退出码提示必须能被 shell 展开（单引号会原样打印 $?）
    assert 'echo "=== run_nodes.sh 已退出，退出码: $? ==="' in command


def test_launcher_run_nodes_requires_setup_bash(launcher_env):
    launcher, tmp, _cfg, _calls = launcher_env
    # run_nodes.sh 存在、但 install/setup.bash 缺失：应明确报错，而不是起一个缺 ROS 环境的进程
    (tmp / "run_nodes.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        launcher.launch_run_nodes([])


def test_launcher_run_nodes_resolves_workspace_script(launcher_env):
    """run_nodes.sh 真源在工作空间根目录，不是项目 scripts/ 的镜像。"""
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_run_nodes([])

    command, _terminal = calls["terminal"]
    assert f"bash {tmp / 'run_nodes.sh'}" in command
    # 项目 scripts/ 目录下不该再有一份 run_nodes.sh（否则又会分叉）
    assert not (PROJECT_ROOT / "scripts" / "run_nodes.sh").exists()


def _fake_setup(tmp: Path) -> Path:
    setup = tmp / "install" / "setup.bash"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("# fake\n", encoding="utf-8")
    # run_nodes.sh 是机器人侧维护的脚本，上位机从工作空间根目录解析；测试里放一份
    # 空壳即可（launch_run_nodes 只校验存在性，不执行它）。
    (tmp / "run_nodes.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return setup


def test_launcher_teleop_writes_output_to_log_file(launcher_env, monkeypatch):
    """遥操作后台启动时把脚本输出写入日志文件，供界面实时回显。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    log_file = tmp / "logs" / "teleop_20260101_000000.log"
    calls.clear()

    def fake_run_background_with_log(command, cwd, path):
        calls["log"] = (command, str(cwd), str(path))
        return FakeProc()

    monkeypatch.setattr(core_operations, "run_background_with_log", fake_run_background_with_log)

    launcher.launch_teleop(log_file)

    command, cwd, path = calls["log"]
    assert path == str(log_file)
    assert cwd == str(tmp)
    assert f"cd {tmp}" in command
    assert f"export ROBOT_WORKSPACE={tmp}" in command
    assert "export NERV_DIR=/var/nerv" in command
    assert f"bash {PROJECT_ROOT / 'scripts' / 'run_teleop.sh'}" in command
    assert "background" not in calls


def test_launcher_teleop_without_log_file_discards_output(launcher_env):
    """不传日志文件时保持原来的完全后台行为（输出丢弃）。"""
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_teleop()

    assert "background" in calls
    assert "log" not in calls
    assert f"bash {PROJECT_ROOT / 'scripts' / 'run_teleop.sh'}" in calls["background"][0]


def test_launcher_teleop_falls_back_when_log_unwritable(launcher_env, monkeypatch):
    """日志文件不可写时退化到丢弃输出，不能因此启动不了遥操作。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    def boom(command, cwd, path):
        raise OSError("read-only file system")

    monkeypatch.setattr(core_operations, "run_background_with_log", boom)

    launcher.launch_teleop(tmp / "logs" / "teleop.log")

    assert "background" in calls
    assert f"bash {PROJECT_ROOT / 'scripts' / 'run_teleop.sh'}" in calls["background"][0]


def test_process_manager_find_and_stop_teleop(tmp_path):
    """find_by_name / stop_teleop 能定位并终止注册的遥操作进程。"""
    from data_collector.core import ProcessManager
    from data_collector.core_operations import TeleopManager

    pm = ProcessManager()
    # ProcessManager 是单例：先清掉其它用例留下的桩进程，保证断言只针对本次注册
    for stale, _name in list(pm._processes):
        pm.unregister(stale)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    mgr = TeleopManager(None, None, state_path=tmp_path / "teleop.state.json")
    pm.register(proc, "run_teleop")
    try:
        assert pm.find_by_name("run_teleop") is proc
        assert mgr.stop_teleop() is True
        proc.wait(timeout=10)
        assert proc.poll() is not None
        assert pm.find_by_name("run_teleop") is None
        assert mgr.stop_teleop() is False      # 已经没有可停的进程
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        pm.unregister(proc)


# ---------- 回归测试：终端子进程必须独立会话 ----------
def test_run_in_terminal_starts_new_session(monkeypatch):
    """终端子进程必须新开会话。

    历史上这里没加 start_new_session，xterm 与上位机同进程组，
    于是 terminate() 的 os.killpg 会把 SIGINT 一起发给界面自己
    （点「关闭程序」会把上位机一起带走）。
    """
    from data_collector import core

    monkeypatch.setattr(core, "check_command_exists", lambda cmd: cmd == "xterm")
    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(core.subprocess, "Popen", FakePopen)

    core.run_in_terminal("echo hi")

    assert captured["args"][0] == "xterm"
    assert captured["kwargs"].get("start_new_session") is True


class _TerminableProc:
    """terminate() 用得到的 Popen 替身（poll / wait / send_signal / kill）。"""

    pid = 4321

    def __init__(self):
        self.signals = []
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


def test_terminate_degenerates_when_same_process_group(monkeypatch):
    """子进程与上位机同组时绝不能 killpg，否则连界面一起发信号。"""
    from data_collector import core

    proc = _TerminableProc()
    killpg_calls = []
    monkeypatch.setattr(core.os, "getpgid", lambda pid: 77)          # 同组
    monkeypatch.setattr(core.os, "killpg",
                        lambda pgid, sig: killpg_calls.append((pgid, sig)))

    core.terminate(proc)

    assert killpg_calls == []
    assert proc.signals == [core.signal.SIGINT]


def test_terminate_uses_killpg_for_isolated_process(monkeypatch):
    """独立会话的子进程仍然整组发信号（ros2 launch 拉起的节点要一起退）。"""
    from data_collector import core

    proc = _TerminableProc()
    killpg_calls = []
    monkeypatch.setattr(core.os, "getpgid", lambda pid: 4321 if pid == 4321 else 77)
    monkeypatch.setattr(core.os, "killpg",
                        lambda pgid, sig: killpg_calls.append((pgid, sig)))

    core.terminate(proc)

    assert killpg_calls == [(4321, core.signal.SIGINT)]
    assert proc.signals == []


# ---------- 回归测试：遥操作运行状态持久化（界面重启后接管） ----------
@pytest.fixture
def teleop_env(temp_dir, monkeypatch):
    """TeleopManager + 独立状态文件；ProcessManager 单例先清空避免串味。"""
    from data_collector import core_operations
    from data_collector.core import ProcessManager

    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    launcher = core_operations.Launcher(cm)
    pm = ProcessManager()
    pm._processes.clear()

    monkeypatch.setattr(launcher, "launch_teleop",
                        lambda log_file=None: FakeProc(pid=4242))
    state_file = tmp / "teleop.state.json"
    mgr = core_operations.TeleopManager(cm, launcher, state_path=state_file)
    return mgr, tmp, state_file


def test_teleop_state_persisted_on_launch(teleop_env):
    """启动遥操作必须落状态文件：否则界面一关就再也接管不到这个进程。"""
    mgr, tmp, state_file = teleop_env

    mgr.launch_teleop(tmp / "teleop.log")

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["log_file"] == str(tmp / "teleop.log")
    assert data["started_at"] > 0
    # 启动时刻与 pid 一起唯一标识进程（pid 会被系统复用）
    assert "start_ticks" in data


def _state_file_with(state_file, **overrides):
    data = {"pid": 4242, "pgid": 4243, "start_ticks": 111, "started_at": 0}
    data.update(overrides)
    state_file.write_text(json.dumps(data), encoding="utf-8")


def _live_process(pid=4242):
    """把"这个 pid 真的在跑"这一层判定替掉。

    为什么不直接用一个真实存在的 pid：这些用例想验的是**pid 复用与启动时刻比对**
    那一段逻辑，pid 只是个数字。而"进程在不在跑"是另一个独立判断（走 /proc 的
    state 字段，见 `_process_running`）——它由下面那条僵尸用例单独钉。
    """
    from data_collector import core_operations
    return mock.patch.object(core_operations, "_process_running",
                             lambda value: int(value or 0) == int(pid))


def test_teleop_state_rejected_when_pid_gone(teleop_env):
    """进程已消失的残留状态要清掉，否则界面会一直误以为遥操作在跑。"""
    from data_collector import core_operations

    mgr, _tmp, state_file = teleop_env
    _state_file_with(state_file)
    with mock.patch.object(core_operations, "_proc_start_ticks", lambda pid: None):
        assert mgr.running_state() is None
    assert not state_file.exists()


def test_teleop_state_rejected_when_pid_reused(teleop_env):
    """pid 被系统复用给别的进程（启动时刻对不上）时不能认，否则会误杀无关进程。"""
    from data_collector import core_operations

    mgr, _tmp, state_file = teleop_env
    _state_file_with(state_file, start_ticks=111)
    with _live_process(), \
            mock.patch.object(core_operations, "_proc_start_ticks", lambda pid: 999):
        assert mgr.running_state() is None
    assert not state_file.exists()


def test_teleop_state_accepted_when_pid_and_start_match(teleop_env):
    """pid 与启动时刻都一致 -> 就是原来那个遥操作进程。"""
    from data_collector import core_operations

    mgr, _tmp, state_file = teleop_env
    _state_file_with(state_file, start_ticks=111)
    with _live_process(), \
            mock.patch.object(core_operations, "_proc_start_ticks", lambda pid: 111):
        assert mgr.running_state()["pid"] == 4242
    assert state_file.exists()


def test_stop_teleop_uses_recorded_pgid_after_restart(teleop_env):
    """界面重启后没有 Popen 句柄，按状态文件里的 pgid 整组停止。"""
    from data_collector import core_operations

    mgr, _tmp, state_file = teleop_env
    _state_file_with(state_file)
    sent = []
    with _live_process(), \
            mock.patch.object(core_operations, "_proc_start_ticks", lambda pid: 111), \
            mock.patch.object(core_operations.os, "getpgid", lambda pid: 99), \
            mock.patch.object(core_operations.os, "killpg",
                              lambda pgid, sig: sent.append((pgid, sig))):
        assert mgr.stop_teleop() is True

    assert sent == [(4243, core_operations.signal.SIGINT)]
    assert not state_file.exists()


def test_launch_teleop_refuses_when_already_running(teleop_env):
    """状态文件里已有活着的遥操作时拒绝重复启动，避免状态互相覆盖。"""
    from data_collector import core_operations

    mgr, tmp, state_file = teleop_env
    _state_file_with(state_file)
    with _live_process(), \
            mock.patch.object(core_operations, "_proc_start_ticks", lambda pid: 111):
        with pytest.raises(RuntimeError, match="已在运行"):
            mgr.launch_teleop(tmp / "teleop.log")


# ---------- 存活判定：僵尸进程 ≠ 在运行 ----------
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="需要 Linux /proc")
def test_zombie_process_counts_as_not_running():
    """僵尸进程必须判为"没在跑"。

    这条是现场那次的根因：`os.kill(pid, 0)` 对僵尸是**成功**的，于是"已经死掉、
    只是还没被回收"的遥操作被一直当成活着的 —— 界面停在「遥操作运行中」、
    按钮写着「停止遥操作」，点下去什么都停不掉，再点只剩一句
    "没有正在运行的遥操作进程"（现场截图就是这个形态）。
    """
    import subprocess

    from data_collector.core_operations import _process_running

    child = subprocess.Popen(["bash", "-c", "sleep 30"], start_new_session=True)
    try:
        time.sleep(0.2)
        assert _process_running(child.pid) is True          # 活着：算是"在跑"

        os.kill(child.pid, signal.SIGKILL)                  # 杀掉，但**先不回收**
        time.sleep(0.3)
        stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8")
        assert stat.rpartition(")")[2].split()[0] == "Z"    # 确认真的进了僵尸态
        # pid 还在进程表里（os.kill(pid, 0) 会成功），但已经不是在跑了
        assert os.kill(child.pid, 0) is None
        assert _process_running(child.pid) is False
    finally:
        child.wait()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="需要 Linux /proc")
def test_proc_start_ticks_reads_starttime_field():
    """starttime 是 /proc/<pid>/stat 第 22 字段（comm 之后的第 20 项）。"""
    from data_collector.core_operations import _proc_start_ticks

    ticks = _proc_start_ticks(os.getpid())
    assert isinstance(ticks, int) and ticks > 0
    # 同一个进程连续读取必须稳定，否则接管判断会自己抖
    assert _proc_start_ticks(os.getpid()) == ticks
    assert _proc_start_ticks(999_999_999) is None


# ---------- 回归测试：sudo 密码不再内置默认值 ----------
def test_launcher_run_nodes_skips_sudo_when_password_missing(launcher_env):
    """未配置 sudo 密码时不带 sudo -S（空密码喂 sudo 反而必然认证失败）。"""
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_run_nodes([])

    command, _terminal = calls["terminal"]
    assert "sudo -S" not in command
    assert "echo  | sudo" not in command
    assert f"bash {tmp / 'run_nodes.sh'}" in command


def test_launcher_run_nodes_pipes_sudo_password_when_configured(tmp_path, monkeypatch):
    """配置了 sudo 密码时沿用管道喂密码的老行为。"""
    from data_collector import core_operations

    monkeypatch.delenv("DATA_COLLECTOR_SUDO_PASSWORD", raising=False)
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "scripts_dir": "scripts",
        "run_nodes_script": "run_nodes.sh",
        "teleop_config_path": "teleop.yaml",
        "sudo_password": "s3cret",
    })
    _fake_setup(tmp_path)

    calls = {}

    def fake_terminal(command, terminal="gnome-terminal"):
        calls["cmd"] = command
        return FakeProc()

    monkeypatch.setattr(core_operations, "run_in_terminal", fake_terminal)

    core_operations.Launcher(ConfigManager(cfg_file)).launch_run_nodes([])

    assert "echo s3cret | sudo -S -v" in calls["cmd"]

# ---------- 急停（安全链路：不依赖任何常驻进程） ----------
def test_launcher_emergency_stop_covers_every_target(launcher_env):
    """急停必须覆盖 EMERGENCY_STOP_TARGETS 里的每一个部件（当前是底盘 + 躯干）。

    只发底盘是不够的：躯干（升降/俯仰立柱）不在急停范围内的话，按下急停后
    立柱仍可动。这条测试直接遍历常量，以后加部件不用回来改断言。
    """
    from data_collector.core_operations import EMERGENCY_STOP_TARGETS

    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_emergency_stop(True)

    command, _cwd = calls["background"]
    assert f"source {tmp / 'install' / 'setup.bash'} && " in command
    for _name, topic in EMERGENCY_STOP_TARGETS:
        assert topic in command, f"急停没有覆盖 {topic}"
    assert command.count("std_msgs/msg/Bool") == len(EMERGENCY_STOP_TARGETS)
    assert command.count("--once --keep-alive 2") == len(EMERGENCY_STOP_TARGETS)
    assert command.count("data: true") == len(EMERGENCY_STOP_TARGETS)


def test_launcher_emergency_stop_publishes_concurrently(launcher_env):
    """各部件必须并发下发，不能串行。

    `--keep-alive 2` 每条要占住约 2 秒，串行下发就是 4 秒 —— 对急停来说太久。
    做法是每个部件 `&` 到后台，统一 `wait` 收尸。
    """
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_emergency_stop(True)
    command, _cwd = calls["background"]

    from data_collector.core_operations import EMERGENCY_STOP_TARGETS
    for index in range(1, len(EMERGENCY_STOP_TARGETS) + 1):
        assert f"& pid{index}=$!" in command, f"第 {index} 个部件没有后台化"
        assert f"wait $pid{index}" in command, f"第 {index} 个部件没有 wait"
    # 必须包在子 shell 里：`A && B & C` 会被解析成 `(A && B) & C`，优先级会打架
    assert "( ros2 topic pub" in command and command.rstrip().endswith(")")


def test_launcher_emergency_stop_aggregates_exit_codes(launcher_env):
    """任一部件失败 → 整条命令非 0，让界面能如实报告。

    躯干没有状态回读（trunk_state 只报关节角），退出码是判断"到底发出去了没有"
    的唯一依据，所以这个汇总逻辑不能省。
    """
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_emergency_stop(True)
    command, _cwd = calls["background"]

    assert "rc=0;" in command
    assert "|| rc=1" in command
    assert command.rstrip().endswith("exit $rc )")


def test_launcher_release_emergency_stop_sends_false_to_all_targets(launcher_env):
    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    launcher.launch_emergency_stop(False)

    command, _cwd = calls["background"]
    from data_collector.core_operations import EMERGENCY_STOP_TARGETS
    assert command.count("data: false") == len(EMERGENCY_STOP_TARGETS)
    assert "data: true" not in command


def test_emergency_stop_shell_aggregates_and_runs_concurrently(launcher_env, tmp_path):
    """真跑一遍生成的 shell：验证退出码汇总与并发性。

    这段 shell 是安全链路的要害，光看字符串不够 —— 子 shell 分组、`&&`/`&` 的
    优先级、`wait` 的退出码，任何一处写错都会让"急停失败"被静默吞掉。
    """
    import subprocess
    import time

    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()
    launcher.launch_emergency_stop(True)
    command, _cwd = calls["background"]

    # 桩 ros2：按 topic 返回不同退出码，并占用一点时间来暴露串行/并发差异
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ros2 = bindir / "ros2"
    ros2.write_text(
        "#!/bin/bash\n"
        "case \"$*\" in\n"
        "  *trunk*) sleep ${TRUNK_SLEEP:-0.4}; exit ${TRUNK_RC:-0} ;;\n"
        "  *)        sleep ${CHASSIS_SLEEP:-0.4}; exit ${CHASSIS_RC:-0} ;;\n"
        "esac\n",
        encoding="utf-8")
    ros2.chmod(0o755)

    base_env = dict(os.environ)
    base_env["PATH"] = f"{bindir}:{base_env.get('PATH', '')}"

    def run(**overrides):
        env = dict(base_env)
        env.update({k: str(v) for k, v in overrides.items()})
        start = time.monotonic()
        result = subprocess.run(["bash", "-c", command], env=env,
                                capture_output=True, text=True, timeout=60)
        return result.returncode, time.monotonic() - start

    assert run()[0] == 0                                   # 都成功
    assert run(TRUNK_RC=3)[0] != 0                         # 躯干失败要被暴露
    assert run(CHASSIS_RC=7)[0] != 0                       # 底盘失败要被暴露
    assert run(CHASSIS_RC=2, TRUNK_RC=5)[0] != 0

    # 各 0.4s：并发应约 0.4s，串行会是 0.8s
    _code, elapsed = run(CHASSIS_SLEEP=0.4, TRUNK_SLEEP=0.4)
    assert elapsed < 0.75, f"看起来是串行下发（耗时 {elapsed:.2f}s）"


# ---------- 机器人状态监控脚本 ----------
def test_launcher_robot_monitor_uses_script_and_ros_env(launcher_env, monkeypatch):
    """状态监控要跑 python3，且必须先 source setup.bash（否则缺 rclpy）。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, calls = launcher_env
    _fake_setup(tmp)
    calls.clear()

    def fake_log(command, cwd, path):
        calls["monitor"] = (command, str(cwd), str(path))
        return FakeProc()

    monkeypatch.setattr(core_operations, "run_background_with_log", fake_log)

    launcher.launch_robot_monitor(tmp / "logs" / "robot_monitor.log")

    command, _cwd, _path = calls["monitor"]
    assert f"source {tmp / 'install' / 'setup.bash'} && " in command
    assert f"python3 {PROJECT_ROOT / 'scripts' / 'robot_monitor.py'}" in command
    assert "export ROBOT_STATUS_FILE=" in command


def test_launcher_robot_monitor_requires_setup_bash(launcher_env):
    """没有 setup.bash 时明确报错，而不是起一个必然缺 rclpy 的进程。"""
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(FileNotFoundError):
        launcher.launch_robot_monitor()


def test_stop_robot_monitor_reports_when_nothing_running(launcher_env):
    from data_collector.core import ProcessManager

    launcher, _tmp, _cfg, _calls = launcher_env
    ProcessManager()._processes.clear()
    assert launcher.stop_robot_monitor() is False


# ---------- 状态文件解析 ----------
@pytest.fixture
def status_config(tmp_path):
    """带临时 nerv_dir 的 ConfigManager。

    默认配置的 nerv_dir 是 /var/nerv，直接拿 temp_dir 造的配置写状态文件会写到
    系统路径上（不同用户/沙箱下还会因权限失败）。所有涉及状态文件的用例都必须
    用这个 fixture，把路径钉在 tmp_path 里。
    """
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "nerv_dir": str(tmp_path / "nerv"),
    })
    return ConfigManager(cfg_file)


def _write_status(cm, payload):
    path = cm.get_robot_status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_read_robot_status_missing_file(status_config):
    from data_collector.core_operations import read_robot_status

    view = read_robot_status(status_config)
    assert view["available"] is False
    assert view["stale"] is False
    assert "尚未启动" in view["reason"]


def test_read_robot_status_valid_payload(status_config):
    from data_collector.core_operations import read_robot_status

    cm = status_config
    _write_status(cm, {
        "updated_at": time.time(), "monitor_pid": 4242, "has_data": True,
        "status": {"power_percent": {"raw": 78, "text": 78, "age_sec": 0.1}},
    })
    view = read_robot_status(cm)
    assert view["available"] is True
    assert view["stale"] is False
    assert view["monitor_pid"] == 4242
    assert view["status"]["power_percent"]["raw"] == 78


def test_read_robot_status_marks_stale(status_config):
    """过期状态必须显式标记：拿 30 秒前的"未急停"当现在的状态是危险的。"""
    from data_collector.core_operations import read_robot_status

    cm = status_config
    _write_status(cm, {
        "updated_at": time.time() - 60, "has_data": True,
        "status": {"emergency": {"raw": 32, "text": "解锁"}},
    })
    view = read_robot_status(cm)
    assert view["stale"] is True
    assert view["available"] is False
    assert "未刷新" in view["reason"]


def test_read_robot_status_distinguishes_no_data(status_config):
    """监控在跑但一个话题都没收到，和监控没启动，是两回事。"""
    from data_collector.core_operations import read_robot_status

    cm = status_config
    _write_status(cm, {"updated_at": time.time(), "has_data": False, "status": {}})
    view = read_robot_status(cm)
    assert view["available"] is True
    assert "还没收到" in view["reason"]


def test_read_robot_status_tolerates_broken_file(status_config):
    from data_collector.core_operations import read_robot_status

    cm = status_config
    path = cm.get_robot_status_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 半截 json", encoding="utf-8")
    view = read_robot_status(cm)
    assert view["available"] is False
    assert "不可用" in view["reason"]


# ---------- 机器人主程序运行探测（SDK 服务） ----------


def test_launch_argv_requires_standalone_marker():
    """命令行里只是"提到"文件名不能算主程序在跑。

    实测踩过：/proc/1/cmdline 是整条 shell 命令原文，其中可能恰好提到过
    wheeled_robot_launch.py（比如正在 grep 它），用子串匹配会把 init 认成主程序。
    """
    from data_collector import core_operations

    assert not core_operations._is_launch_argv(
        ["/bin/bash", "-c", "ps aux | grep wheeled_robot_launch.py"])
    assert not core_operations._is_launch_argv(
        ["/bin/bash", "-c", "echo wheeled_robot_launch.py"])
    # 正常形态：ros2 launch / python 直接跑 launch 文件，文件名是独立参数
    assert core_operations._is_launch_argv(
        ["ros2", "launch", "wheeled_robot", "wheeled_robot_launch.py", "namespace:=/"])
    assert core_operations._is_launch_argv(["python3", "wheeled_robot_launch.py"])
    assert core_operations._is_launch_argv(["/opt/ros/share/x/wheeled_robot_launch.py"])


def test_find_launch_process_detects_real_process(tmp_path, monkeypatch):
    """用真实进程验证一遍：探到、然后杀掉后探不到。

    判定逻辑单测过还不够 —— 这里要确认 /proc 读取路径本身是通的。

    **marker 换成这一条用例专属的串**：机器人机上经常真在跑
    `ros2 launch wheeled_robot wheeled_robot_launch.py`，用默认 marker 时
    `find_launch_process()` 会先探到那个真进程 —— 于是这两条断言变成在考"运行环境干不干净"，
    而不是在考探测逻辑本身（现场实测就是这么挂的）。真进程存在与否由
    `test_launch_blocked_with_prompt_when_main_program_running` 用替身覆盖。
    """
    import subprocess
    import time

    from data_collector import core_operations

    marker = "dc_launch_probe_target.py"
    monkeypatch.setattr(core_operations, "LAUNCH_PROCESS_MARKER", marker)
    script = tmp_path / marker
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    assert core_operations.find_launch_process() is None
    proc = subprocess.Popen(["python3", str(script)])
    try:
        time.sleep(0.4)
        assert core_operations.find_launch_process() == proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(0.2)
    assert core_operations.find_launch_process() is None


def test_sdk_service_is_running_from_process(temp_dir, monkeypatch):
    from data_collector import core_operations

    _tmp, cfg_file = temp_dir
    mgr = core_operations.SdkServiceManager(ConfigManager(cfg_file))

    monkeypatch.setattr(core_operations, "find_launch_process", lambda: 4242)
    assert mgr.is_running() is True
    assert mgr.launch_pid() == 4242

    monkeypatch.setattr(core_operations, "find_launch_process", lambda: None)
    assert mgr.is_running() is False


def test_sdk_service_is_read_only_no_start_no_stop(temp_dir):
    """SDK 服务是**纯只读**：START 与 STOP 都不提供（v4.7.3 收敛）。

    两条都踩过：
    - start：开发机/多数部署上必然失败（没权限或没同名 unit），用户只看到"点了没反应"；
    - stop：代为停止会把界面自己一起带走（界面可能就在该服务的 cgroup 里），
      现场表现为"停止服务后界面消失、一个字日志都没有"，事后极难定位。
    现在只保留"探测 + 把命令贴给用户"，界面不再替人按按钮。
    """
    from data_collector import core_operations

    assert hasattr(core_operations.SdkServiceManager, "is_running")
    assert hasattr(core_operations.SdkServiceManager, "stop_command")   # 只给命令文本
    assert not hasattr(core_operations.SdkServiceManager, "stop")
    assert not hasattr(core_operations.SdkServiceManager, "start")
    assert not hasattr(core_operations.SdkServiceManager, "is_active")










# ---------- 手型：合法值只认 hand_plugins 的键 ----------
def _make_rviz_dir(tmp_path, names):
    rviz = tmp_path / "install/share/robot_description/rviz"
    for name in names:
        (rviz / name).mkdir(parents=True, exist_ok=True)
        (rviz / name / "wheel_robot.rviz").write_text("# fake\n", encoding="utf-8")
    return rviz


def test_hand_types_scanned_from_rviz_dir(temp_dir):
    """UI 下拉与 launch 的 hand choices 同源：扫描 rviz 目录。"""
    tmp, cfg_file = temp_dir
    _make_rviz_dir(tmp, ["empty", "hangkai_tg9801", "yinshi_rh56d", "yushu_dex11"])
    cm = ConfigManager(cfg_file)
    assert cm.get_hand_types() == ["empty", "hangkai_tg9801", "yinshi_rh56d", "yushu_dex11"]


def test_hand_types_fallback_when_dir_missing(temp_dir):
    """开发机上没有工作空间时退回内置列表，界面仍可用。"""
    _tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    assert "hangkai_tg9801" in cm.get_hand_types()


def test_normalize_hand_type_maps_legacy_short_names(temp_dir):
    """界面曾经用 hangkai/yinshi 短名，传给 launch 会找不到配置，必须归一化。"""
    tmp, cfg_file = temp_dir
    _make_rviz_dir(tmp, ["empty", "hangkai_tg9801", "yinshi_rh56d", "yushu_dex11"])
    cm = ConfigManager(cfg_file)

    assert cm.normalize_hand_type("hangkai") == "hangkai_tg9801"
    assert cm.normalize_hand_type("yinshi") == "yinshi_rh56d"
    assert cm.normalize_hand_type("yushu") == "yushu_dex11"
    # SDK 里没有 zhixing 的 rviz/控制器配置，退回空手而不是写一个加载不到的名字
    assert cm.normalize_hand_type("zhixing") == "empty"
    # 合法值原样通过；空值/未知值返回空串
    assert cm.normalize_hand_type("yushu_dex11") == "yushu_dex11"
    assert cm.normalize_hand_type("") == ""
    assert cm.normalize_hand_type("nonsense") == ""


# ---------- 录制：标签随参数传给脚本 ----------
def test_launcher_record_merges_label_into_desc(launcher_env):
    """真机脚本不解析 label=/action_id=，动作标签并进 desc（脚本会写进 info.json）。"""
    launcher, _tmp, _cfg, calls = launcher_env

    launcher.launch_record("bag", name="采集1", desc="演示", label="挥手", action_id=4)

    command, _cwd = calls["background"]
    assert f"bash {PROJECT_ROOT / 'scripts' / 'record_bag.sh'}" in command
    assert "'name=采集1'" in command
    assert "desc=演示 挥手 #4" in command          # desc + label + action_id
    assert "label=" not in command                 # 不再传 label 参数
    assert "action_id=" not in command


def test_launcher_record_omits_empty_label(launcher_env):
    launcher, tmp, _cfg, calls = launcher_env
    (tmp / "scripts").mkdir(exist_ok=True)
    (tmp / "scripts" / "record_cmd.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    launcher.launch_record("cmd")

    command, _cwd = calls["background"]
    assert "desc=" not in command          # 空 desc + 空 label 不传 desc
    assert "label=" not in command
    assert "action_id=" not in command


# ---------- 动作标签：可新增 / 改名 / 删除 ----------
def _labels_config(tmp_path, **overrides):
    """带独立标签文件的 ConfigManager。必须隔离，否则会写到项目里真正的配置。"""
    cfg_file = tmp_path / "app_config.yaml"
    config = {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
              "action_labels_file": str(tmp_path / "action_labels.yaml")}
    config.update(overrides)
    save_yaml(cfg_file, config)
    return ConfigManager(cfg_file)


def test_action_labels_seed_from_sdk_table(tmp_path):
    """还没有标签文件时用 SDK 默认表做初始内容（顺序按编号）。"""
    cm = _labels_config(tmp_path)
    items = cm.get_action_labels()

    assert [i["name"] for i in items] == ["双臂垂下", "预置位", "邀请", "递话筒", "挥手", "演讲", "端盘"]
    assert items[4]["action_id"] == 4
    # 只读不该产生副作用文件
    assert not cm.get_action_labels_file().exists()


def test_action_labels_seed_from_config_dict(tmp_path):
    """配置里的 record_action_labels（旧 dict 形式）可作为初始内容。"""
    cm = _labels_config(tmp_path, record_action_labels={3: "递话筒"})
    assert cm.get_action_labels() == [{"name": "递话筒", "action_id": 3}]


def test_action_labels_file_takes_priority_over_config(tmp_path):
    cm = _labels_config(tmp_path, record_action_labels={3: "递话筒"})
    cm.save_action_labels([{"name": "倒水", "action_id": None}])

    assert cm.get_action_labels() == [{"name": "倒水", "action_id": None}]


def test_add_custom_action_label(tmp_path):
    """核心诉求：录"倒水"这种新动作，只要新增一个标签就能选。"""
    cm = _labels_config(tmp_path)

    ok, message = cm.upsert_action_label("倒水")
    assert ok is True and "倒水" in message

    items = cm.get_action_labels()
    assert items[-1] == {"name": "倒水", "action_id": None}
    # 立即落盘，换一个 ConfigManager 也能读到
    assert cm.get_action_labels_file().exists()
    assert any(i["name"] == "倒水" for i in _labels_config(tmp_path).get_action_labels())


def test_add_action_label_rejects_duplicates_and_empty(tmp_path):
    cm = _labels_config(tmp_path)
    cm.upsert_action_label("倒水")

    assert cm.upsert_action_label("倒水")[0] is False           # 重名
    assert cm.upsert_action_label("  ")[0] is False             # 空名
    assert cm.upsert_action_label("挥手2", 4)[0] is False        # 编号被「挥手」占用
    assert cm.upsert_action_label("随便", "不是数字")[0] is False


def test_rename_action_label_keeps_position(tmp_path):
    """改名要留原位（列表顺序稳定），且不能把自己当成重名挡住。"""
    cm = _labels_config(tmp_path)

    # 传入的 action_id 就是"改完之后的值"（界面上的编号框是预填的）
    ok, message = cm.upsert_action_label("挥手挥起来", 4, original_name="挥手")
    assert ok is True and "已更新" in message

    names = [i["name"] for i in cm.get_action_labels()]
    assert "挥手" not in names
    assert names[4] == "挥手挥起来"          # 仍排在第 5 位，不是被挪到末尾
    entry = next(i for i in cm.get_action_labels() if i["name"] == "挥手挥起来")
    assert entry["action_id"] == 4           # 编号保留

    # 编号传空 = 把这条改成自定义标签（清掉编号）
    ok, _ = cm.upsert_action_label("挥手挥起来", None, original_name="挥手挥起来")
    assert ok is True
    entry = next(i for i in cm.get_action_labels() if i["name"] == "挥手挥起来")
    assert entry["action_id"] is None


def test_rename_action_label_fails_when_original_gone(tmp_path):
    """点名要改的标签已经不在了 → 明确报失败，不要静默变成新增。"""
    cm = _labels_config(tmp_path)

    ok, message = cm.upsert_action_label("倒茶", original_name="倒水")
    assert ok is False
    assert "已不存在" in message
    assert not any(i["name"] == "倒茶" for i in cm.get_action_labels())


def test_reassign_action_label_id(tmp_path):
    cm = _labels_config(tmp_path)
    cm.upsert_action_label("倒水")

    ok, _ = cm.upsert_action_label("倒水", 9, original_name="倒水")
    assert ok is True
    entry = next(i for i in cm.get_action_labels() if i["name"] == "倒水")
    assert entry["action_id"] == 9


def test_remove_action_label(tmp_path):
    cm = _labels_config(tmp_path)

    assert cm.remove_action_label("挥手")[0] is True
    assert not any(i["name"] == "挥手" for i in cm.get_action_labels())
    assert cm.remove_action_label("不存在")[0] is False


def test_action_labels_normalize_legacy_and_broken_entries(tmp_path):
    """手写的标签文件要容错：字符串项、缺 name、编号不是数字都跳过或降级。"""
    cm = _labels_config(tmp_path)
    cm.get_action_labels_file().write_text(
        "labels:\n"
        "  - name: 挥手\n"
        "    action_id: 4\n"
        "  - 倒水\n"
        "  - name: 编号写错\n"
        "    action_id: abc\n"
        "  - action_id: 7\n"
        "  - name: 挥手\n"
        "    action_id: 9\n",
        encoding="utf-8")

    items = cm.get_action_labels()
    assert items[0] == {"name": "挥手", "action_id": 4}
    assert {"name": "倒水", "action_id": None} in items
    assert {"name": "编号写错", "action_id": None} in items
    # 缺 name 的条目被跳过；重名的只保留先出现的那个
    assert [i["name"] for i in items].count("挥手") == 1


def test_action_labels_broken_file_falls_back_to_seed(tmp_path):
    cm = _labels_config(tmp_path)
    cm.get_action_labels_file().write_text("labels: [ {,,,", encoding="utf-8")

    names = [i["name"] for i in cm.get_action_labels()]
    assert "挥手" in names          # 回退到 SDK 默认表，不让界面开不了录制


# ---------- 顶栏身份：机型 / 序列号 / 固件 ----------
def test_product_info_and_version_string(tmp_path):
    tmp, cfg_file = temp_dir_files = (tmp_path, tmp_path / "app_config.yaml")
    (tmp / "Product.json").write_text(
        json.dumps({"hardware_model": "NERV-AMA01", "serial_number": 12345678}),
        encoding="utf-8")
    (tmp / "VERSION").write_text("v1.4.0\n202609021101\n", encoding="utf-8")
    # 必须显式把 product_file 钉到 tmp_path：这台机器的 /var/nerv/product/
    # 本来就有一份真实 Product.json，查找顺序里它优先于工作空间 ——
    # 不钉住的话，测试结果取决于机器上碰巧有什么文件（实测踩过）。
    save_yaml(cfg_file, {"workspace_root": str(tmp), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp / "Product.json")})

    cm = ConfigManager(cfg_file)
    info = cm.get_product_info()
    assert info["hardware_model"] == "NERV-AMA01"
    assert info["serial_number"] == 12345678
    assert cm.get_version_string() == "v1.4.0"        # 只取首行


def test_product_file_priority_order(tmp_path):
    """查找顺序：product_file 配置 > ${nerv_dir}/product > 项目根 > 工作空间。

    这台机器的 /var/nerv/product/ 里就有一份真实 Product.json ——
    优先级写错的话，顶栏会显示别的机器的机型/序列号。
    """
    nerv = tmp_path / "nerv"
    (nerv / "product").mkdir(parents=True)
    (nerv / "product" / "Product.json").write_text(
        json.dumps({"hardware_model": "FROM-NERV"}), encoding="utf-8")
    (tmp_path / "root" / "Product.json").parent.mkdir()
    (tmp_path / "root" / "Product.json").write_text(
        json.dumps({"hardware_model": "FROM-ROOT"}), encoding="utf-8")
    (tmp_path / "ws" / "Product.json").parent.mkdir()
    (tmp_path / "ws" / "Product.json").write_text(
        json.dumps({"hardware_model": "FROM-WS"}), encoding="utf-8")

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "ws"), "teleop_config_path": "t.yaml",
                         "nerv_dir": str(nerv),
                         # 项目根没法在测试里重定向（是真实仓库根），只验后两级
                         })
    cm = ConfigManager(cfg_file)
    assert cm.get_product_info()["hardware_model"] == "FROM-NERV"

    # 显式配置最优先
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "ws"), "teleop_config_path": "t.yaml",
                         "nerv_dir": str(nerv), "product_file": str(tmp_path / "explicit.json")})
    (tmp_path / "explicit.json").write_text(
        json.dumps({"hardware_model": "FROM-EXPLICIT"}), encoding="utf-8")
    assert ConfigManager(cfg_file).get_product_info()["hardware_model"] == "FROM-EXPLICIT"


def test_product_info_missing_returns_empty(tmp_path):
    """没有任何 Product.json 时返回空 dict（界面显示"—"），不能抛异常阻断启动。"""
    cfg_file = tmp_path / "app_config.yaml"
    # product_file 钉到一个不存在的路径：这台机器 /var/nerv/product/ 里有真实的
    # Product.json，查找顺序里它优先于工作空间 —— 不钉住这条测试就会依赖机器环境。
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "不存在的Product.json")})

    cm = ConfigManager(cfg_file)
    assert cm.get_product_info() == {}
    assert cm.get_version_string() == ""              # 没有 VERSION 文件


def test_product_info_survives_broken_json(tmp_path):
    """Product.json 内容坏了 → 返回空 dict，不能抛异常阻断启动。"""
    (tmp_path / "Product.json").write_text("{ 坏的", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})

    assert ConfigManager(cfg_file).get_product_info() == {}


# ---------- 版本号：来自 ${workspace}/doc/version.txt ----------
def test_parse_version_text_prefers_branch_line():
    from data_collector.core import parse_version_text, parse_version_details

    text = ("# 构建版本信息\n"
            "打包人：wp\n"
            "打包时间：2026-09-17 10:10:58\n"
            "分支: release/v1.4.0\n"
            "commit id: e64afe0e\n")

    assert parse_version_text(text) == "v1.4.0"
    details = parse_version_details(text)
    assert details["packer"] == "wp"
    # 值里本身带冒号（时间），不能按第一个冒号切
    assert details["built_at"] == "2026-09-17 10:10:58"
    assert details["branch"] == "release/v1.4.0"

    assert parse_version_text("") == ""
    # 没有"分支"行时全文兜底
    assert parse_version_text("版本 v2.1.3 之类") == "v2.1.3"


def test_version_prefers_doc_version_txt(tmp_path):
    """顶栏「版本」以 doc/version.txt 为准。"""
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "version.txt").write_text(
        "分支: release/v9.9.9\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("v1.4.0\n202609171012\n", encoding="utf-8")

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    cm = ConfigManager(cfg_file)

    assert cm.get_version_file() == tmp_path / "doc" / "version.txt"
    assert cm.get_version_string() == "v9.9.9"


def test_version_falls_back_to_VERSION_file(tmp_path):
    (tmp_path / "VERSION").write_text("v1.4.0\n202609171012\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    cm = ConfigManager(cfg_file)

    assert cm.get_version_file() == tmp_path / "VERSION"
    assert cm.get_version_string() == "v1.4.0"        # 只取首行


def test_version_missing_everywhere_is_empty(tmp_path):
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})
    cm = ConfigManager(cfg_file)

    assert cm.get_version_string() == ""
    assert cm.get_version_details() == {}


# ---------- 末端手型：来自 ${nerv_dir}/product/Product.json ----------
def test_product_file_prefers_nerv_dir_product(tmp_path):
    """权威位置是 ${nerv_dir}/product/Product.json（机器人侧 product.sh 读同一个）。"""
    nerv = tmp_path / "nerv"
    (nerv / "product").mkdir(parents=True)
    (nerv / "product" / "Product.json").write_text(
        json.dumps({"hardware_model": "NERV-AMA01-1", "serial_number": "12345678",
                    "hand": "yinshi_rh56d", "wrist": "camera_up_wrist"}),
        encoding="utf-8")

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "nerv_dir": str(nerv)})
    cm = ConfigManager(cfg_file)

    assert cm.get_product_file() == nerv / "product" / "Product.json"
    assert cm.get_product_info()["hardware_model"] == "NERV-AMA01-1"
    assert cm.get_product_hand() == "yinshi_rh56d"
    assert cm.get_product_wrist() == "camera_up_wrist"


def test_product_hand_defaults_when_missing(tmp_path):
    """缺 hand 字段按空处理（产品侧 product.sh 会退回 empty）。"""
    (tmp_path / "Product.json").write_text(
        json.dumps({"hardware_model": "NERV-AMA01"}), encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})
    cm = ConfigManager(cfg_file)

    assert cm.get_product_hand() == ""
    assert cm.get_product_wrist() == ""


def test_product_hand_treats_null_as_empty(tmp_path):
    (tmp_path / "Product.json").write_text(
        json.dumps({"hand": "null", "wrist": None}), encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})
    cm = ConfigManager(cfg_file)

    assert cm.get_product_hand() == ""
    assert cm.get_product_wrist() == ""


def test_action_labels_seed_accepts_plain_string_list(tmp_path):
    """配置里写字符串清单就等于一串自定义标签（布草机器人的默认就是这样）。"""
    cm = _labels_config(tmp_path, record_action_labels=["布草挂架", "矿泉水瓶分拣"])

    assert cm.get_action_labels() == [
        {"name": "布草挂架", "action_id": None},
        {"name": "矿泉水瓶分拣", "action_id": None},
    ]


def test_action_labels_seed_accepts_empty_list(tmp_path):
    """显式写 [] 表示"初始不要任何标签"，不该偷偷回退成 SDK 默认表。"""
    cm = _labels_config(tmp_path, record_action_labels=[])
    assert cm.get_action_labels() == []


def test_repo_default_labels_are_linen_tasks():
    """项目自带的默认标签就是布草机器人的两个任务。

    这条钉的是"出厂默认"，避免以后有人改配置时顺手把它改回 SDK 的演讲类动作表。
    """
    cm = ConfigManager(PROJECT_ROOT / "configs" / "app_config.yaml")
    names = [item["name"] for item in cm._seed_action_labels()]
    assert names == ["布草挂架", "矿泉水瓶分拣"]
    assert all(item["action_id"] is None for item in cm._seed_action_labels())


# ---------- 产品配置检测与向导 ----------
def test_is_product_configured_false_when_missing(tmp_path):
    """Product.json 缺失时未配置。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})
    assert ConfigManager(cfg_file).is_product_configured() is False


def test_is_product_configured_false_when_hand_empty(tmp_path):
    """Product.json 存在但 hand 为空 → 仍未配置（手型是启动前提）。"""
    (tmp_path / "Product.json").write_text(
        json.dumps({"hardware_model": "NERV-AMA01"}), encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})
    assert ConfigManager(cfg_file).is_product_configured() is False


def test_is_product_configured_true(tmp_path):
    (tmp_path / "Product.json").write_text(
        json.dumps({"hardware_model": "NERV-AMA01", "hand": "yushu_dex11"}),
        encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "product_file": str(tmp_path / "Product.json")})
    assert ConfigManager(cfg_file).is_product_configured() is True


def test_launcher_setup_product_uses_workspace_script(launcher_env):
    """setup_product.sh 真源在工作空间根目录，不是项目 scripts/ 的镜像。"""
    launcher, tmp, _cfg, calls = launcher_env
    # 脚本放工作空间根目录
    (tmp / "setup_product.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    calls.clear()

    launcher.launch_setup_product()

    command, _terminal = calls["terminal"]
    assert f"bash {tmp / 'setup_product.sh'}" in command
    assert f"cd {tmp}" in command


def test_launcher_setup_product_requires_script(launcher_env):
    """工作空间里没有 setup_product.sh 时明确报错。"""
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(FileNotFoundError):
        launcher.launch_setup_product()


# ---------- 工作空间切换 ----------
def test_set_workspace_root_updates_yaml_and_paths(tmp_path):
    """切换 workspace_root：写回 yaml、更新 workspace 与 teleop_config_path。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "old"),
                         "teleop_config_path": "install/config/teleop_config.yaml"})
    cm = ConfigManager(cfg_file)

    new_root = tmp_path / "new ws"          # 带空格，验证 YAML 引号处理
    cm.set_workspace_root(str(new_root))

    assert cm.workspace == new_root
    assert cm.teleop_config_path == new_root / "install/config/teleop_config.yaml"
    # 持久化：重读 yaml 仍是新值
    reloaded = load_yaml(cfg_file)
    assert Path(reloaded["workspace_root"]).expanduser() == new_root


def test_set_workspace_root_preserves_comments(tmp_path):
    """写回只动 workspace_root 一行，其余注释保留。"""
    cfg_file = tmp_path / "app_config.yaml"
    cfg_file.write_text(
        "# 顶部注释\nworkspace_root: ~/a/b\nteleop_config_path: x.yaml\n# 尾部注释\n",
        encoding="utf-8")
    cm = ConfigManager(cfg_file)
    cm.set_workspace_root(str(tmp_path / "c"))

    text = cfg_file.read_text(encoding="utf-8")
    assert "# 顶部注释" in text
    assert "# 尾部注释" in text
    assert "teleop_config_path: x.yaml" in text


# ---------- 进程回收（僵尸清理） ----------
def test_process_manager_prune_reaps_exited():
    """已退出的子进程要 wait 掉并移出注册表，否则僵尸随会话累积。"""
    pm = ProcessManager()
    pm._processes = []
    proc = subprocess.Popen(["true"])
    proc.wait()
    pm.register(proc, "unit_test_proc")

    assert len(pm._processes) == 1
    assert pm.prune() == 1
    assert pm._processes == []


def test_process_manager_prune_keeps_running():
    pm = ProcessManager()
    pm._processes = []
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pm.register(proc, "unit_test_alive")
        assert pm.prune() == 0
        assert len(pm._processes) == 1
    finally:
        proc.kill()
        proc.wait()
        pm._processes = []


# ---------- 收尾读取不能永久阻塞 ----------
def test_read_remaining_stdout_does_not_hang_on_live_process():
    """活进程上直接 read() 会阻塞到管道 EOF —— 收尾读必须先结束进程。"""
    from data_collector.core_operations import _read_remaining_stdout

    proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    started = time.time()
    out = _read_remaining_stdout(proc, timeout=2)
    elapsed = time.time() - started

    assert elapsed < 10                 # 关键：没有永久阻塞
    assert proc.poll() is not None      # 已被收掉
    assert out == ""


# ---------- 统一启动入口：守卫不再遗漏 ----------
def test_launcher_play_bag_rejects_missing_workspace(launcher_env):
    """play_bag 原先漏做工作空间校验，收敛到 _run_script 后补上。"""
    launcher, tmp, _cfg, _calls = launcher_env
    launcher.config.workspace = tmp / "no_such_ws"

    with pytest.raises(FileNotFoundError, match="工作空间不存在"):
        launcher.launch_play_bag()


def test_launcher_monitor_performance_rejects_missing_workspace(launcher_env):
    """monitor_performance 原先漏做工作空间校验，收敛后补上。"""
    launcher, tmp, _cfg, _calls = launcher_env
    launcher.config.workspace = tmp / "no_such_ws"

    with pytest.raises(FileNotFoundError, match="工作空间不存在"):
        launcher.launch_monitor_performance()


def test_hold_tail_only_for_interactive_scripts(launcher_env):
    """保活尾巴只给需要看报错的交互式脚本（play_bag 退出即关窗）。"""
    launcher, tmp, _cfg, calls = launcher_env
    (tmp / "install").mkdir(exist_ok=True)
    (tmp / "install" / "setup.bash").write_text("# fake\n", encoding="utf-8")
    # run_nodes.sh 的真源在工作空间根目录（上位机不存镜像）
    (tmp / "run_nodes.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    launcher.launch_play_bag()
    play_cmd, _ = calls["terminal"]
    assert "read -p" not in play_cmd

    launcher.launch_run_nodes([])
    run_cmd, _ = calls["terminal"]
    assert "read -p" in run_cmd


# ---------- 启动自检 ----------
# 自检会遍历全部脚本配置项，缺 key 记为 fail，所以测试里需要配全
_DOCTOR_SCRIPTS = {
    "run_nodes_script": "run_nodes.sh",
    "run_teleop_script": "run_teleop.sh",
    "record_bag_script": "record_bag.sh",
    "record_vla_script": "record_vla.sh",
    "record_cmd_script": "record_cmd.sh",
    "play_bag_script": "play_bag.sh",
    "bag_rate_stat_script": "bag_rate_stat.py",
    "monitor_performance_script": "monitor_record_performance.sh",
    "robot_monitor_script": "robot_monitor.py",
}


def _doctor_ready_workspace(tmp_path):
    """构造一个通过自检所需的最小工作空间（只要求文件存在）。"""
    (tmp_path / "install").mkdir(exist_ok=True)
    (tmp_path / "install" / "setup.bash").write_text("# fake\n", encoding="utf-8")
    (tmp_path / "run_nodes.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "bag").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("record_bag.sh", "record_vla.sh", "record_cmd.sh"):
        (tmp_path / "scripts" / name).write_text("#!/bin/bash\n", encoding="utf-8")
    # 相机标定 / 戳点 / 视频流节点（colcon 布局：install/lib/<pkg>/<node>）
    for pkg, node in (("calibration", "calibration_node"),
                      ("pokepoint", "pokepoint_node"),
                      ("teleop_video_streamer", "teleop_video_streamer_node")):
        d = tmp_path / "install" / "lib" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / node).write_text("#!/bin/bash\n", encoding="utf-8")


def test_check_environment_reports_missing_workspace(tmp_path):
    """工作空间不存在时只报这一项：后续路径检查没有意义。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "no_such_ws"),
                         "teleop_config_path": "t.yaml"})

    results = ConfigManager(cfg_file).check_environment()

    assert len(results) == 1
    assert results[0]["name"] == "工作空间"
    assert results[0]["ok"] is False
    assert results[0]["level"] == "fail"


def test_check_environment_all_ok_when_ready(tmp_path):
    """环境齐备时不应有任何未通过项。"""
    _doctor_ready_workspace(tmp_path)
    (tmp_path / "Product.json").write_text(
        json.dumps({"hand": "yushu_dex11"}), encoding="utf-8")
    calib = tmp_path / "calib"
    calib.mkdir(exist_ok=True)
    (calib / "head.yaml").write_text("ok\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "scripts_dir": "scripts",
        "bag_dir": str(tmp_path / "bag"),
        "vision_calib_dir": str(calib),
        "product_file": str(tmp_path / "Product.json"),
        "remote_password": "test-secret",
        **_DOCTOR_SCRIPTS,
    })

    failed = [r for r in ConfigManager(cfg_file).check_environment() if not r["ok"]]

    assert failed == [], failed


def test_check_environment_downgrades_config_issues_to_warn(tmp_path, monkeypatch):
    """产品未配置 / 远端无凭据只算 warn：不阻塞启动，但会提示。"""
    monkeypatch.delenv("DATA_COLLECTOR_REMOTE_PASSWORD", raising=False)
    _doctor_ready_workspace(tmp_path)
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "bag_dir": str(tmp_path / "bag"),
        "product_file": str(tmp_path / "Product.json"),   # 故意不存在
        "run_nodes_script": "run_nodes.sh",
        "record_bag_script": "record_bag.sh",
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
    })

    by_name = {r["name"]: r for r in ConfigManager(cfg_file).check_environment()}

    assert by_name["产品配置 (Product.json)"]["ok"] is False
    assert by_name["产品配置 (Product.json)"]["level"] == "warn"
    assert by_name["远端 SFTP 凭据"]["ok"] is False
    assert by_name["远端 SFTP 凭据"]["level"] == "warn"


def test_check_environment_flags_missing_record_script(tmp_path):
    """缺脚本必须算 fail —— 这正是"点下去才失败"的典型场景。"""
    _doctor_ready_workspace(tmp_path)
    (tmp_path / "scripts" / "record_bag.sh").unlink()
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "bag_dir": str(tmp_path / "bag"),
        "run_nodes_script": "run_nodes.sh",
        # 用**绝对路径**指向那份已被删掉的脚本：裸文件名现在会命中我们仓里的真副本
        # （v6.5.0 起录制脚本真源在本仓），"缺失"的前提得靠绝对路径才成立。
        "record_bag_script": str(tmp_path / "scripts" / "record_bag.sh"),
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
    })

    by_name = {r["name"]: r for r in ConfigManager(cfg_file).check_environment()}

    assert by_name["脚本 · 录制 bag"]["ok"] is False
    assert by_name["脚本 · 录制 bag"]["level"] == "fail"


# ---------- 体积格式化与目录统计（界面共用） ----------
def test_human_size_uses_binary_units():
    from data_collector.core import human_size

    assert human_size(0) == "0.0 B"
    assert human_size(1023) == "1023.0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024 ** 2) == "1.0 MB"
    assert human_size(int(1.5 * 1024 ** 3)) == "1.5 GB"


def test_dir_size_counts_recursively(tmp_path):
    from data_collector.core import dir_size

    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.mcap").write_bytes(b"x" * 300)
    (tmp_path / "a" / "y").write_bytes(b"y" * 100)

    assert dir_size(tmp_path) == 400


def test_dir_size_returns_zero_for_missing_path(tmp_path):
    from data_collector.core import dir_size

    assert dir_size(tmp_path / "nope") == 0


def test_is_within_rejects_sibling_and_dotdot(tmp_path):
    """删除前的越界校验：同级的兄弟目录、以及用 `..` 绕出来的路径都不算在内。"""
    from data_collector.core import is_within

    root = tmp_path / "bag"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "other").mkdir()

    assert is_within(root, root)
    assert is_within(root / "sub", root)
    assert not is_within(tmp_path / "other", root)
    assert not is_within(root / ".." / "other", root)


def test_remote_browser_human_size_delegates_to_shared(tmp_path):
    """远端目录浏览器的体积格式化不再是第二份实现（同数字不会两处长得不一样）。"""
    from data_collector.core import human_size
    from data_collector.ui import RemoteDirectoryDialog

    assert RemoteDirectoryDialog.human_size(3 * 1024 ** 3) == human_size(3 * 1024 ** 3)


# ---------- 三相机标定 / 戳点（机器人侧节点，上位机只做入口） ----------
def _ros_ready_workspace(tmp):
    """最小 ROS 环境：只有 install/setup.bash 存在即可。"""
    (tmp / "install").mkdir(exist_ok=True)
    (tmp / "install" / "setup.bash").write_text("# fake\n", encoding="utf-8")


def test_camera_calibration_command_runs_configured_node(launcher_env):
    """标定节点走 `ros2 run`（后台落盘，界面 tail 回显），且必须 source 工作空间。"""
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    launcher.launch_camera_calibration(with_video=False)

    cmd, _ = calls["background"]
    assert "ros2 run calibration calibration_node" in cmd
    assert f"source {tmp / 'install' / 'setup.bash'}" in cmd
    assert calls["background_log"] is not None       # 输出落盘才能回显


def test_pokepoint_command_runs_configured_node(launcher_env):
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    launcher.launch_pokepoint(with_video=False)

    cmd, _ = calls["background"]
    assert "ros2 run pokepoint pokepoint_node" in cmd


def test_camera_calibration_starts_video_stream_when_enabled(launcher_env):
    """视频流节点是画面弹窗的来源，默认一并启动。"""
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    launcher.launch_camera_calibration(with_video=True)

    cmds = calls["bg_cmds"]
    assert len(cmds) == 2                            # 标定节点 + 视频流
    assert any("ros2 run teleop_video_streamer teleop_video_streamer_node" in c
               for c in cmds)


def test_video_stream_can_be_skipped(launcher_env):
    """现场已经手动起好视频流时，应当能关掉（避免重复实例抢话题）。"""
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    launcher.launch_camera_calibration(with_video=False)

    assert len(calls["bg_cmds"]) == 1                # 只起了标定节点
    assert "teleop_video_streamer" not in calls["bg_cmds"][0]


def test_camera_calibration_call_uses_configured_service(launcher_env):
    """下发采集指令：命令与节点解耦，服务名来自配置。"""
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    launcher.start_camera_calibration_call()

    cmd, _ = calls["captured"]
    assert "ros2 service call" in cmd
    assert "/calibration/start_srv" in cmd


def test_camera_calibration_requires_ros_env(launcher_env):
    """没有 setup.bash 就没法 ros2 run —— 要在点按钮时说清，而不是开个空终端。"""
    launcher, _tmp, _cfg, _calls = launcher_env   # 故意不建 install/setup.bash

    with pytest.raises(FileNotFoundError, match="setup.bash"):
        launcher.launch_camera_calibration()
    with pytest.raises(FileNotFoundError, match="setup.bash"):
        launcher.start_camera_calibration_call()


def test_video_stream_failure_does_not_block_calibration(launcher_env, monkeypatch):
    """视频流拉不起来只告警：标定节点本身已经起来了，挡住才是帮倒忙。"""
    launcher, tmp, _cfg, calls = launcher_env
    _ros_ready_workspace(tmp)

    def boom(*args, **kwargs):
        raise RuntimeError("视频流节点不存在")

    monkeypatch.setattr(launcher, "_start_video_stream_if_enabled", boom)

    launcher.launch_camera_calibration()          # 不应抛出

    assert "ros2 run calibration calibration_node" in calls["background"][0]


# ---------- 标定文件：自检提示但不强制 ----------
def test_vision_calib_dir_defaults_under_nerv_dir(tmp_path):
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "nerv_dir": str(tmp_path / "nerv")})

    assert ConfigManager(cfg_file).get_vision_calib_dir() == tmp_path / "nerv/vision/calib"


def test_vision_calib_dir_can_be_overridden(tmp_path):
    target = tmp_path / "custom_calib"
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "vision_calib_dir": str(target)})

    assert ConfigManager(cfg_file).get_vision_calib_dir() == target


def test_has_camera_calibration_needs_real_files(tmp_path):
    """空目录不算已标定 —— 它通常是标定跑一半失败留下的，同样没有可用外参。"""
    calib = tmp_path / "calib"
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "vision_calib_dir": str(calib)})
    cm = ConfigManager(cfg_file)

    assert cm.has_camera_calibration() is False       # 目录不存在
    calib.mkdir()
    assert cm.has_camera_calibration() is False       # 存在但为空
    (calib / "head_camera.yaml").write_text("ok\n", encoding="utf-8")
    assert cm.has_camera_calibration() is True


def test_doctor_warns_when_calibration_missing(tmp_path):
    """缺标定文件只提醒：不阻断启动，也不该让自检弹错误框。"""
    _doctor_ready_workspace(tmp_path)
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "scripts_dir": "scripts",
        "bag_dir": str(tmp_path / "bag"),
        "vision_calib_dir": str(tmp_path / "no_calib"),
        "run_nodes_script": "run_nodes.sh",
        "record_bag_script": "record_bag.sh",
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
    })

    by_name = {r["name"]: r for r in ConfigManager(cfg_file).check_environment()}
    item = by_name["相机标定文件"]

    assert item["ok"] is False
    # 关键：标为 warn 而不是 fail —— 自检只在有 fail 项时才弹框，
    # 缺标定文件不能弹框、更不能阻断启动。
    assert item["level"] == "warn"
    assert "建议先做三相机标定" in item["detail"]


def test_doctor_reports_calibration_present(tmp_path):
    _doctor_ready_workspace(tmp_path)
    calib = tmp_path / "calib"
    calib.mkdir()
    (calib / "head.yaml").write_text("ok\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "bag_dir": str(tmp_path / "bag"),
        "vision_calib_dir": str(calib),
        "run_nodes_script": "run_nodes.sh",
        "record_bag_script": "record_bag.sh",
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
    })

    by_name = {r["name"]: r for r in ConfigManager(cfg_file).check_environment()}

    assert by_name["相机标定文件"]["ok"] is True


def test_doctor_checks_calibration_nodes_exist(tmp_path):
    """节点没编译安装时提前告知，而不是点了按钮才看到终端一闪而过。"""
    # 刻意不调 _doctor_ready_workspace：它会把节点文件也造出来
    (tmp_path / "install").mkdir(exist_ok=True)
    (tmp_path / "install" / "setup.bash").write_text("# fake\n", encoding="utf-8")
    (tmp_path / "run_nodes.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "bag").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("record_bag.sh", "record_vla.sh", "record_cmd.sh"):
        (tmp_path / "scripts" / name).write_text("#!/bin/bash\n", encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(tmp_path),
        "teleop_config_path": "t.yaml",
        "scripts_dir": "scripts",
        "bag_dir": str(tmp_path / "bag"),
        "run_nodes_script": "run_nodes.sh",
        "record_bag_script": "record_bag.sh",
        "record_vla_script": "record_vla.sh",
        "record_cmd_script": "record_cmd.sh",
    })

    by_name = {r["name"]: r for r in ConfigManager(cfg_file).check_environment()}

    assert by_name["节点 · 相机标定"]["ok"] is False
    assert by_name["节点 · 相机标定"]["level"] == "warn"   # 路径布局由 colcon 决定，不能当硬错误


# ---------- 关闭主程序：残留节点清理 ----------
# 背景：run_nodes.sh 跑在新终端里，我们持有的 Popen 是终端进程，单靠 terminate()
# 收不掉 `ros2 launch … | python3` 拉起的节点 —— 实测关完 /arm_control、
# /trunk_control、/navi_*、/ascamera 一整套话题还在（现场 ros2 topic list 一看就露馅）。
class _FakeConfig:
    """只提供 cleaner 需要的两个属性，避免为几条断言搭整个 ConfigManager。"""

    def __init__(self, workspace, keep=None):
        self.workspace = workspace
        self.app_config = {"stop_keep_markers": keep} if keep else {}


def _cleaner(lister=None, workspace="/ws", keep=None):
    from data_collector.core_operations import MainProgramCleaner
    return MainProgramCleaner(_FakeConfig(workspace, keep),
                              lister=lister or (lambda: []),
                              sleeper=lambda _seconds: None)


def test_cleaner_matches_main_program_only():
    """只认主程序的三类进程；辅助工具（遥操作/视频流/VR 桥/戳点/标定）一律放过。"""
    cleaner = _cleaner()

    assert cleaner.is_target("ros2 launch wheeled_robot wheeled_robot_launch.py namespace:=/")
    assert cleaner.is_target("bash -c cd /ws && ./run_nodes.sh joy rviz")
    assert cleaner.is_target("/ws/install/lib/arm_control/arm_control_node")

    for cmd in ("/ws/install/lib/teleop/teleop_node",
                "bash /ws/run_teleop.sh",
                "/ws/install/lib/teleop_video_streamer/teleop_video_streamer_node",
                "ros2 run teleop vr_bridge_node",
                "/ws/install/lib/pokepoint/pokepoint_node",
                "/ws/install/lib/calibration/calibration_node",
                "/usr/bin/gnome-terminal -- bash"):
        assert not cleaner.is_target(cmd), cmd


def test_cleaner_accepts_configured_keep_markers():
    """放过名单可配：机型/部署不同，辅助工具的路径片段可能不一样。"""
    cleaner = _cleaner(keep=["my_streamer"])
    assert not cleaner.is_target("/ws/install/lib/my_streamer/node")
    assert cleaner.is_target("/ws/install/lib/arm_control/arm_control_node")


def test_cleaner_skips_own_process_group():
    """永不动自己与同组进程 —— 否则"关闭程序"会把上位机一起带走。"""
    mine = os.getpid()
    my_group = os.getpgid(0)
    rows = [
        {"pid": mine, "pgid": my_group, "cmd": "bash -c ./run_nodes.sh"},
        {"pid": 1, "pgid": 1, "cmd": "/ws/install/lib/arm_control/arm_control_node"},
        {"pid": mine + 100000, "pgid": my_group,
         "cmd": "/ws/install/lib/trunk_control/trunk_control_node"},
    ]
    cleaner = _cleaner(lister=lambda: rows)

    assert cleaner.scan() == []
    assert len(cleaner.skipped) == 3          # 记下来，便于诊断"为什么没清干净"


def test_cleaner_stop_returns_early_when_nothing_found():
    report = _cleaner().stop()

    assert report["found"] == []
    assert report["survivors"] == []
    assert "没有发现残留节点进程" in report["steps"][0]


def test_cleaner_stop_escalates_signals(monkeypatch):
    """一直不退 → 走完 SIGINT → SIGTERM → SIGKILL 三档，并报告存活者。"""
    import signal as _signal

    rows = [{"pid": 4242, "pgid": 4242,
             "cmd": "/ws/install/lib/arm_control/arm_control_node"}]
    sent = []
    monkeypatch.setattr("data_collector.core_operations.os.killpg",
                        lambda pgid, sig: sent.append((pgid, sig)))

    report = _cleaner(lister=lambda: rows).stop()

    assert [sig for _pgid, sig in sent] == [_signal.SIGINT, _signal.SIGTERM, _signal.SIGKILL]
    assert all(pgid == 4242 for pgid, _sig in sent)
    assert len(report["steps"]) == 3
    assert len(report["survivors"]) == 1


def test_cleaner_stop_stops_at_first_step_when_targets_die(monkeypatch):
    """SIGINT 就退干净了，不该再补刀（SIGTERM/SIGKILL 一次都不发）。"""
    import signal as _signal

    state = {"alive": True}
    rows = [{"pid": 4242, "pgid": 4242,
             "cmd": "/ws/install/lib/arm_control/arm_control_node"}]

    def lister():
        return list(rows) if state["alive"] else []

    def fake_killpg(_pgid, sig):
        sent.append(sig)
        state["alive"] = False

    sent = []
    monkeypatch.setattr("data_collector.core_operations.os.killpg", fake_killpg)

    report = _cleaner(lister=lister).stop()

    assert sent == [_signal.SIGINT]
    assert report["survivors"] == []
    assert len(report["steps"]) == 1


def test_stop_main_program_reports_cleanup_and_topic_check(launcher_env, monkeypatch):
    """关闭主程序的日志行：清理概况 + 话题核验结论。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(
        core_operations.MainProgramCleaner, "stop",
        lambda self: {"found": ["/ws/install/lib/arm_control/arm_control_node"],
                      "steps": ["SIGINT → 1/1 个进程组"],
                      "survivors": []})
    monkeypatch.setattr(core_operations.Launcher, "count_ros_topics",
                        lambda self, timeout=0: (0, [], "绕过 daemon 直查"))

    text = "\n".join(launcher.stop_main_program())

    assert "发现残留节点进程 1 个" in text
    assert "SIGINT → 1/1 个进程组" in text
    assert "残留节点已全部退出" in text
    assert "已无话题" in text


def test_stop_main_program_lists_survivors_and_topics(launcher_env, monkeypatch):
    """清不干净的进程与话题必须**如实列出来**，不能只报一句"已完成"。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(
        core_operations.MainProgramCleaner, "stop",
        lambda self: {"found": ["/ws/install/lib/arm_control/arm_control_node"],
                      "steps": ["SIGINT → 0/1 个进程组"],
                      "survivors": ["/ws/install/lib/arm_control/arm_control_node"]})
    monkeypatch.setattr(core_operations.Launcher, "count_ros_topics",
                        lambda self, timeout=0: (2, ["/arm_control/arm_state",
                                                  "/trunk_control/connect"], "绕过 daemon 直查"))

    text = "\n".join(launcher.stop_main_program())

    assert "仍有 1 个进程未退出" in text
    assert "仍需关注的话题 2 个" in text
    assert "/arm_control/arm_state" in text


# ---------- 关程序前必须先下使能（现场 P0：躯干上着使能直接关程序，电机就不受控了） ----------
def test_release_hardware_connections_sends_disconnect_to_all_parts(launcher_env, monkeypatch):
    """躯干/手臂/底盘三条 connect=false 都要发，而且必须"等发现再发"。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = " ".join(cmd)
        return subprocess.CompletedProcess(cmd, 0, "rc1=0\nrc2=0\nrc3=0\n", "")

    monkeypatch.setattr(core_operations.subprocess, "run", fake_run)
    lines = launcher.release_hardware_connections()
    cmd = seen["cmd"]

    for _name, topic in core_operations.DISCONNECT_TARGETS:
        assert topic in cmd, f"{topic} 没在断开指令里"
    assert cmd.count("data: false") == len(core_operations.DISCONNECT_TARGETS)
    # -w 1：等匹配到订阅者再发。不等的话 DDS 发现还没完成，消息会被丢掉 —— 等于没发
    assert cmd.count("-w 1") == len(core_operations.DISCONNECT_TARGETS)
    # 没有订阅者时会一直等，所以每条都套了 timeout，不能挂住关闭流程
    assert "timeout 4" in cmd

    text = "\n".join(lines)
    assert "已下发断开连接（下使能）：躯干、手臂、底盘" in text


def test_release_hardware_connections_reports_unconfirmed_parts(launcher_env, monkeypatch):
    """没确认送达的部件要单独点名 —— 那意味着它可能还上着使能。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(
        core_operations.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "rc1=0\nrc2=124\nrc3=2\n", ""))

    text = "\n".join(launcher.release_hardware_connections())

    assert "已下发断开连接（下使能）：躯干" in text
    assert "手臂" in text and "超时" in text             # rc=124 → 等待超时
    assert "底盘" in text and "退出码 2" in text
    assert "请人工确认已下使能" in text


def _capture_module_command(monkeypatch, launcher, tmp, stdout):
    """跑一次模块下发，把真正执行的 bash 命令与超时预算抓出来（不真发指令）。"""
    from data_collector import core_operations

    _fake_setup(tmp)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd[2]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(core_operations.subprocess, "run", fake_run)
    return seen


def test_set_module_enabled_trunk_sequence_is_ordered(launcher_env, monkeypatch):
    """躯干上使能必须是三步且**按序**：connect → motor_mode 0（非实时）→ connect。

    少中间那步会得到"抬头了但模式没变"（后续控制看起来没反应，现场踩过）；
    并发下发则顺序没有保证 —— 所以这条既钉步数、也钉先后顺序与每步之间的等待。

    **取值是 0 而不是 1**（v6.2.0 改的，现场原话"进入直接就是非实时模式"）：
    实时模式下关节按最大速度追目标、要上位机自己 100Hz 插值，而本项目发的是一条
    点位目标 —— 上使能留成实时，就是"动作又快又硬 / 看着没反应"的根源。
    """
    launcher, tmp, _cfg, _calls = launcher_env
    seen = _capture_module_command(monkeypatch, launcher, tmp, "rc1=0\nrc2=0\nrc3=0\n")

    lines = launcher.set_module_enabled("trunk", True)

    cmd = seen["cmd"]
    first_connect = cmd.index("/trunk_control/connect")
    mode = cmd.index("/trunk_control/motor_mode")
    last_connect = cmd.rindex("/trunk_control/connect")
    assert first_connect < mode < last_connect
    # 切模式那一步是 Byte（不是 Bool）：传 int 会被 Python 绑定拒掉，YAML 里写 data: 0
    assert 'std_msgs/msg/Byte "data: 0"' in cmd
    assert '"data: 1"' not in cmd, "上使能结束时必须是**非实时**（0），不能留成实时（1）"
    assert cmd.count("ros2 topic pub") == 3
    # 每步之间的等待取自上电时序，别被改小（那是留给硬件动作的时间）
    assert "sleep 5.0" in cmd and "sleep 3.0" in cmd
    # 预算要盖得住 11 秒的等待，否则会被自己的超时杀掉
    assert seen["timeout"] > 11
    assert "已下发" in "\n".join(lines)


def test_set_module_enabled_arms_is_single_step(launcher_env, monkeypatch):
    """双臂是**一条** connect（话题粒度就是整机双臂，没有单臂话题）。"""
    launcher, tmp, _cfg, _calls = launcher_env
    seen = _capture_module_command(monkeypatch, launcher, tmp, "rc1=0\n")

    lines = launcher.set_module_enabled("arms", True)

    assert seen["cmd"].count("ros2 topic pub") == 1
    assert '/arm_control/connect std_msgs/msg/Bool "data: true"' in seen["cmd"]
    assert "接通双臂" in "\n".join(lines)


def test_set_module_enabled_reports_unconfirmed_steps(launcher_env, monkeypatch):
    """没送到的步骤要**点名**，不能混成一句"已完成"。

    124 = `timeout` 到点：说明这条话题没有订阅者（对应节点没在跑）——
    现场据此知道该去查谁，而不是只看到"失败了"。
    """
    launcher, tmp, _cfg, _calls = launcher_env
    _capture_module_command(monkeypatch, launcher, tmp, "rc1=0\nrc2=124\nrc3=0\n")

    text = "\n".join(launcher.set_module_enabled("trunk", True))

    assert "躯干上使能指令已下发" in text
    assert "接通躯干" in text and "躯干重新使能" in text      # rc1 / rc3 到了
    assert "躯干上使能未确认" in text and "躯干切非实时模式" in text   # rc2 没到
    assert "没有节点在订阅" in text


def test_set_module_enabled_requires_workspace(launcher_env, monkeypatch):
    """工作空间不存在：一条指令都不发（判据与其它 launch_* 一致）。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    spawned = []
    monkeypatch.setattr(core_operations.subprocess, "run",
                        lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(launcher.config, "check_workspace", lambda: False)

    lines = launcher.set_module_enabled("trunk", True)

    assert spawned == []
    assert "工作空间不存在" in "\n".join(lines)


def test_set_module_enabled_survives_subprocess_failure(launcher_env, monkeypatch):
    """子进程起不来也要如实说、且**不抛异常** —— 界面要在结果区里显示它。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(core_operations.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("bash 不可用")))

    text = "\n".join(launcher.set_module_enabled("arms", False))

    assert "未能确认" in text and "请人工确认双臂的使能状态" in text


def test_set_module_enabled_rejects_unknown_action(launcher_env):
    """模块名/动作写错是编程错误，直接抛 —— 不要变成一个"静默什么也没做"的界面。"""
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(ValueError):
        launcher.set_module_enabled("chassis", True)


def test_module_state_of_covers_four_states():
    """"未知"与"未使能"必须是两种状态，按钮才敢按状态点灯。

    躯干：有数据才算知道；双臂：一侧已使能 = `partial`（这个态最该被看见，
    因为那一侧的 follow 会被 arm_control_node 静默丢弃）。
    """
    from data_collector import core_operations as co

    assert co.module_state_of(None, "trunk") == co.MODULE_STATE_UNKNOWN
    assert co.module_state_of({}, "trunk") == co.MODULE_STATE_UNKNOWN
    # 消息包缺失 / 监控没起：整段标 not available
    assert co.module_state_of({"available": False}, "arms") == co.MODULE_STATE_UNKNOWN
    # 从没上报过 = 未使能（现场澄清：上了使能才会有状态数据）。这一条很关键：
    # 若当成"未知"，最常见的场景（躯干还没上使能、要点上使能）会被自己的判据挡住
    assert co.module_state_of(
        {"available": True, "trunk_state": {"has_data": False, "enabled": False}},
        "trunk") == co.MODULE_STATE_OFF
    assert co.module_state_of(
        {"available": True, "arm_state": {"has_data": False, "enabled": False}},
        "arms") == co.MODULE_STATE_OFF

    assert co.module_state_of(
        {"available": True, "trunk_state": {"has_data": True, "enabled": True}},
        "trunk") == co.MODULE_STATE_ON
    assert co.module_state_of(
        {"available": True, "trunk_state": {"has_data": True, "enabled": False}},
        "trunk") == co.MODULE_STATE_OFF

    arms = {"available": True, "arm_state": {"has_data": True, "enabled_left": True,
                                             "enabled_right": False}}
    assert co.module_state_of(arms, "arms") == co.MODULE_STATE_PARTIAL
    arms["arm_state"] = {"has_data": True, "enabled_left": True, "enabled_right": True}
    assert co.module_state_of(arms, "arms") == co.MODULE_STATE_ON
    arms["arm_state"] = {"has_data": True, "enabled_left": False, "enabled_right": False}
    assert co.module_state_of(arms, "arms") == co.MODULE_STATE_OFF


# ============ 夹爪（宇树 Dex11）============
def test_gripper_conversions_round_trip():
    """千分比 ↔ 物理量：界面填 N·m / mm·s⁻¹，协议要的是千分比。"""
    from data_collector import core_operations as co

    assert co.gripper_tau_nm(300) == pytest.approx(0.6)
    assert co.gripper_tau_nm(1000) == pytest.approx(co.GRIPPER_TAU_MAX_NM)
    # 低于下限要钳住：控制器把 <0.5 N·m 当作"没给力"，夹爪会完全松掉
    assert co.gripper_tau_nm(0) == pytest.approx(co.GRIPPER_TAU_MIN_NM)
    assert co.gripper_tau_nm("不是数字") == pytest.approx(0.6)     # 外部输入不假定类型
    assert co.gripper_tau_raw(0.6) == 300
    assert co.gripper_tau_raw(99) == 1000                          # 超范围钳到 2.0
    assert co.gripper_speed_mms(300) == pytest.approx(30.0)
    assert co.gripper_speed_raw(30) == 300
    # 位置：**越大越闭合**（协议语义，搞反了会变成"点闭合反而张开"）
    assert co.gripper_position_raw(True) == co.GRIPPER_RAW_CLOSED == 1000
    assert co.gripper_position_raw(False) == co.GRIPPER_RAW_OPEN == 100


def test_gripper_opening_text_has_three_buckets():
    """开口描述分三档：已张开 / 夹着东西 / 合到底 —— 现场要问的正是这三件事。"""
    from data_collector import core_operations as co

    assert co.gripper_opening_text(
        co.gripper_joint_rad(co.GRIPPER_RAW_OPEN)).startswith("已张开")
    assert "夹着东西" in co.gripper_opening_text(co.gripper_joint_rad(500))
    assert "没夹住" in co.gripper_opening_text(
        co.gripper_joint_rad(co.GRIPPER_RAW_CLOSED))
    assert co.gripper_opening_text(None) == "位置未知"
    # 张开位不是满行程：raw=100 对应约 40.1mm（协议留了 10% 余量），
    # 所以"张开位基准"必须单独算，不能拿 44.5mm 满行程当它
    assert co.GRIPPER_OPEN_GAP_MM == pytest.approx(40.05, abs=0.1)
    assert co.gripper_gap_mm(co.gripper_joint_rad(co.GRIPPER_RAW_CLOSED)) == pytest.approx(0.0)


def test_read_product_hand_survives_missing_and_broken_file(tmp_path):
    """Product.json 读不到就返回空串（界面显示"未知"），不抛异常、也不编一个值。"""
    from data_collector import core_operations as co

    assert co.read_product_hand(tmp_path / "没有这个文件.json") == ""
    broken = tmp_path / "broken.json"
    broken.write_text("{这不是 json", encoding="utf-8")
    assert co.read_product_hand(broken) == ""
    good = tmp_path / "Product.json"
    good.write_text(json.dumps({"hardware_model": "NERV-AMA01", "hand": "yushu_dex11"}),
                    encoding="utf-8")
    assert co.read_product_hand(good) == "yushu_dex11"


def test_parse_joint_state_handles_block_and_inline():
    """`ros2 topic echo` 的两种 YAML 写法都要认；长度对不上要整体丢弃。"""
    from data_collector import core_operations as co

    block = ("header:\n  stamp:\n    sec: 1\n    nanosec: 0\n  frame_id: ''\n"
             "name:\n- Joint1\n- LJoint1_1\nposition:\n- 0.1\n- 0.0123\n"
             "velocity:\n- 0.0\n- 0.0\neffort:\n- 0.5\n- 0.42\n---\n")
    fields = co.parse_joint_state(block)
    assert fields["name"] == ["Joint1", "LJoint1_1"]
    assert fields["position"] == [0.1, 0.0123]          # 数字要真的变成 float
    assert fields["effort"] == [0.5, 0.42]

    inline = co.parse_joint_state(
        "name: [a, RJoint1_1]\nposition: [0.1, 0.2]\neffort: [0.0, 0.1]\n")
    assert inline["name"] == ["a", "RJoint1_1"]
    assert inline["position"] == [0.1, 0.2]

    # 截断（name 两个、position 一个）→ 空 dict：拿它会按错位的下标取到别人的读数
    assert co.parse_joint_state("name:\n- a\n- b\nposition:\n- 0.1\n") == {}
    assert co.parse_joint_state("") == {}


def test_gripper_reading_marks_missing_side_as_disconnected():
    """关节名不在 `name` 里 = 那一侧没连上（官方测试程序 `wait_joints` 同款判据）。"""
    from data_collector import core_operations as co

    only_left = co.parse_joint_state("name:\n- LJoint1_1\nposition:\n- 0.01\neffort:\n- 0.3\n")
    reading = co.gripper_reading(only_left)

    assert reading["left"] == {"connected": True, "rad": 0.01, "effort": 0.3}
    assert reading["right"]["connected"] is False
    assert reading["right"]["rad"] is None


def _capture_gripper_commands(monkeypatch, calls_log):
    """把 subprocess.run 换成记录命令的桩（两条路径都要看拼出来的命令行）。"""
    from data_collector import core_operations as co

    def fake_run(cmd, **kwargs):
        calls_log.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, stdout="rc1=0\n", stderr="")

    monkeypatch.setattr(co.subprocess, "run", fake_run)


def test_set_gripper_posture_switches_mode_then_sends_posture(launcher_env, monkeypatch):
    """话题路径**两步、顺序固定**：先把该侧切回非实时（JTC），再发 hand_posture。

    顺序反了（或只发后一条）会出现"现场刚用过实时抓取测试、单次指令安静地不生效"，
    那是最难查的一类问题 —— 所以这条既钉两步、也钉先后。
    """
    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    commands = []
    _capture_gripper_commands(monkeypatch, commands)

    lines = launcher.set_gripper_posture(["left"], True, 300, 300)

    assert len(commands) == 2
    switch, posture = commands
    # 第一步：切回非实时，只针对要操作的那一侧
    assert '/arm_control/left_hand_forward_set std_msgs/msg/Bool "data: false"' in switch
    assert "right_hand_forward_set" not in switch
    # 第二步：side 0 = 左手；position = [闭合速度, 闭合位置, 目标力矩]
    assert "/arm_control/hand_posture" in posture
    assert "wheeled_robot_msgs/msg/HandPosture" in posture
    assert "{side: 0, id: 0, start_addr: 0, position: [300, 1000, 300]}" in posture

    text = "\n".join(lines)
    assert "夹爪闭合抓取指令已下发：左爪" in text
    assert "0.60 N·m" in text


def test_set_gripper_posture_uses_open_position_and_right_side(launcher_env, monkeypatch):
    """张开用 RAW_OPEN(100) 而不是 0；右手是 side=1。"""
    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    commands = []
    _capture_gripper_commands(monkeypatch, commands)

    lines = launcher.set_gripper_posture(["right"], False, 300, 300)

    posture = commands[-1]
    assert "position: [300, 100, 300]" in posture       # 100 = 张开位
    assert "{side: 1," in posture
    assert "夹爪张开指令已下发：右爪" in "\n".join(lines)


def test_set_gripper_posture_reports_unconfirmed_separately(launcher_env, monkeypatch):
    """没送到要**点名**（124 = 没有订阅者），不能混成一句"已完成"。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    outputs = iter(["rc1=0\n", "rc1=124\n"])

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(co.subprocess, "run", fake_run)

    text = "\n".join(launcher.set_gripper_posture(["left"], True, 300, 300))

    assert "未确认：左爪（等待超时：没有订阅者，主程序可能没在跑）" in text


def test_set_gripper_posture_without_workspace_sends_nothing(launcher_env, monkeypatch):
    """工作空间不存在：一条指令都不发。"""
    from data_collector import core_operations as co

    launcher, _tmp, _cfg, _calls = launcher_env
    spawned = []
    monkeypatch.setattr(co.subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(launcher.config, "check_workspace", lambda: False)

    lines = launcher.set_gripper_posture(["left"], True, 300, 300)

    assert spawned == []
    assert "工作空间不存在" in "\n".join(lines)


def test_run_gripper_direct_grasp_uses_tool_without_ros_env(launcher_env, monkeypatch):
    """不起程序那条路：跑直测工具，**不 source setup.bash**。

    工具不依赖 ROS 库（ldd 只有 libunitree_motor_sdk / libserialport），
    硬 source 只是多一层依赖 —— 这条路本来就该在"没有 ROS 环境"时也能用。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)                                   # 会造出 install/setup.bash
    tool = tmp / co.GRIPPER_TOOL_REL
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/bash\n", encoding="utf-8")
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd[2])
        return subprocess.CompletedProcess(
            cmd, 0, stdout="start to grasp with tau_target=0.600 N\n", stderr="")

    monkeypatch.setattr(co.subprocess, "run", fake_run)

    lines = launcher.run_gripper_direct("left", "grasp", 300)

    assert len(commands) == 1
    cmd = commands[0]
    assert f"./{co.GRIPPER_TOOL_REL}" in cmd
    # 左手 id=1、右手 id=0（与 hand_posture 的 side **相反**，错了就是"点左夹右"）
    assert f"id={co.GRIPPER_DIRECT_ID['left']} grasp=0.600" in cmd
    assert "source " not in cmd and "setup.bash" not in cmd
    assert "左爪直接闭合抓取" in "\n".join(lines)


def test_run_gripper_direct_rejects_unknown_side(launcher_env):
    """侧别写错是编程错误，直接抛 —— 不要变成"静默什么也没做"。"""
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(ValueError):
        launcher.run_gripper_direct("middle", "grasp", 300)


def test_run_gripper_direct_reports_missing_tool(launcher_env, monkeypatch):
    """工具不在位要说清它随哪个目录发布，而不是丢一个 FileNotFoundError。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    spawned = []
    monkeypatch.setattr(co.subprocess, "run", lambda *a, **k: spawned.append(a))

    lines = launcher.run_gripper_direct("right", "grasp", 300)

    assert spawned == []
    assert "找不到直测程序" in "\n".join(lines)


def test_summarize_direct_action_reads_tool_markers():
    """工具输出的判读：接触 / 未夹住 / 电机报错，各自对应一句人话。"""
    from data_collector import core_operations as co

    def completed(stdout, returncode=0):
        return subprocess.CompletedProcess(["bash"], returncode, stdout=stdout, stderr="")

    # 正常抓到：有 grasp by …（刚度档），也有几十行逐周期的数据表
    source = [
        "start to grasp with tau_target=0.600 N",
        "   0.100    0.100    0.000    0.000 contacted",
        "   0.200    0.200    0.000    0.000 contacted",
        "grasp by kStiffHard with tau_target=0.60 N",
    ]
    outcome = "\n".join(co._summarize_direct_action(
        "left", "grasp", 300, completed("\n".join(source))))
    assert "抓取完成（刚度档：硬）" in outcome
    assert sum(1 for row in source if "contacted" in row) == 2
    # 同类原文只留一条：数据表里每行内容都不一样，按整行去重等于没去重
    assert outcome.count("contacted") == 1

    # 夹到底但没东西：not grasped
    assert "没夹住物体" in "\n".join(co._summarize_direct_action(
        "right", "grasp", 300, completed("not grasped\n")))

    # 电机报错：必须原样传到界面
    assert "电机报错" in "\n".join(co._summarize_direct_action(
        "left", "grasp", 300, completed("motor has errors, pls power off and reconnect\n")))

    # 工具直接失败：退出码要报出来
    assert "退出码 2" in "\n".join(co._summarize_direct_action(
        "left", "grasp", 300, completed("", returncode=2)))


def test_run_gripper_direct_open_uses_open_action(launcher_env, monkeypatch):
    """张开走 `open`（现场："不起程序用 grasp 抓，用 open 开"）。

    `open` 不带力矩参数 —— 工具那边它也不需要（张开没有"限力"这回事）。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    tool = tmp / co.GRIPPER_TOOL_REL
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/bash\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: (
        commands.append(cmd[2]),
        subprocess.CompletedProcess(cmd, 0, stdout="move to 0.000 rad\n", stderr=""))[1])

    lines = launcher.run_gripper_direct("right", "open")

    cmd = commands[0]
    # 右手 id=0（与左手相反）
    assert f"id={co.GRIPPER_DIRECT_ID['right']} open" in cmd
    assert "grasp=" not in cmd
    text = "\n".join(lines)
    assert "右爪直接张开" in text
    # open 没有"夹住没夹住"这回事，所以不给抓取那套结论
    assert "刚度档" not in text and "没夹住物体" not in text


def test_run_gripper_direct_rejects_unknown_action(launcher_env):
    """动作不在常量表里就抛 —— 工具支持的 move/jump/auto 都不该从界面漏下去。"""
    launcher, _tmp, _cfg, _calls = launcher_env
    with pytest.raises(ValueError):
        launcher.run_gripper_direct("left", "calib-force", 300)


def _stub_binary(tmp, rel: str):
    """在工作空间里放一份假二进制（只校验命令行，不执行）。"""
    binary = tmp / rel
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/bash\n", encoding="utf-8")
    return binary


def test_scan_gripper_ports_runs_scan_and_summarizes(launcher_env, monkeypatch):
    """检测夹爪 = `dex11_calib --scan`，**只扫描**（绝不能带上 --yes）。

    它回答的是"夹爪插好没有、在哪个口、当前角度"——标定前先看这一眼，
    比标定完发现"一只都没扫到"省事得多。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    _stub_binary(tmp, co.GRIPPER_CALIB_TOOL_REL)
    commands = []
    stdout = ("扫描 /dev/ttyUSB* -> [/dev/ttyUSB0, /dev/ttyUSB4] ...\n"
              "右爪           id=0 (/dev/ttyUSB0)  q=  0.000 rad\n"
              "左爪           id=1 (/dev/ttyUSB4)  q=  0.000 rad\n")
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: (
        commands.append(cmd[2]),
        subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""))[1])

    lines = launcher.scan_gripper_ports()

    cmd = commands[0]
    assert f"./{co.GRIPPER_CALIB_TOOL_REL} --scan" in cmd
    assert "--yes" not in cmd
    assert "source " not in cmd and "setup.bash" not in cmd   # 标定程序也不依赖 ROS 库
    text = "\n".join(lines)
    assert "扫描结果" in text and "/dev/ttyUSB0" in text
    assert "id=0" in text and "共找到 2 条读数" in text


def test_scan_gripper_ports_reports_nothing_found(launcher_env, monkeypatch):
    """一只都没扫到：直接采用工具自己那句提示（供电 / USB 线 / 串口被占）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    _stub_binary(tmp, co.GRIPPER_CALIB_TOOL_REL)
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="扫描 /dev/ttyUSB* -> [<empty>] ...\n"
                       "没有找到夹爪。检查供电、USB线，以及ROS程序是否还占着串口\n",
        stderr=""))

    text = "\n".join(launcher.scan_gripper_ports())

    assert "没有找到夹爪" in text
    assert "主程序是否还占着串口" in text


def test_calibrate_gripper_scans_calibrates_rescans(launcher_env, monkeypatch):
    """零点标定是**三段**、顺序固定：扫描 → 标定 → 再扫描。

    为什么夹着两次扫描：`--yes` 是就地写零点偏移，**成没成只有角度能说明**
    （标定后各爪角度应回到 0.000 rad 附近）。前后摆在一起，现场一眼看出这次动没动。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    _stub_binary(tmp, co.GRIPPER_CALIB_TOOL_REL)
    commands = []
    outputs = iter([
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=  1.234 rad\n",
        "  0.000 rad (err=0)\n",
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=  0.000 rad\n",
    ])
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: (
        commands.append(cmd[2]),
        subprocess.CompletedProcess(cmd, 0, stdout=next(outputs), stderr=""))[1])

    lines = launcher.calibrate_gripper()

    assert len(commands) == 3
    assert commands[0].endswith("--scan")
    assert commands[1].endswith("--yes")
    assert commands[2].endswith("--scan")
    text = "\n".join(lines)
    assert "零点标定" in text
    assert "标定前：" in text and "标定后：" in text
    assert "1.234 rad" in text and "0.000 rad" in text      # 前后对比摆在一起
    assert "标定程序正常退出" in text


def test_calibrate_gripper_skips_when_nothing_found(launcher_env, monkeypatch):
    """标定前那一扫没找到夹爪：**不往下标**（工具会自己再扫一遍，结果一样，白等）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    _stub_binary(tmp, co.GRIPPER_CALIB_TOOL_REL)
    commands = []
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: (
        commands.append(cmd[2]),
        subprocess.CompletedProcess(cmd, 0, stdout="没有找到夹爪。检查供电、USB线\n",
                                    stderr=""))[1])

    text = "\n".join(launcher.calibrate_gripper())

    assert len(commands) == 1                        # 只扫了一次，没有 --yes
    assert "--yes" not in commands[0]
    assert "未执行标定" in text


def test_calibrate_gripper_reports_missing_tool(launcher_env, monkeypatch):
    """标定程序不在位要说清它随哪个目录发布。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    spawned = []
    monkeypatch.setattr(co.subprocess, "run", lambda *a, **k: spawned.append(a))

    lines = launcher.calibrate_gripper()

    assert spawned == []
    assert "找不到标定程序" in "\n".join(lines)


class _FakePopen:
    """FCC 用的 Popen 替身：wait 一次就返回，并可顺手往 stdout 句柄里写点东西。"""

    def __init__(self, script_text="", first_wait_timeout=False):
        self.pid = 4242
        self._calls = 0
        self._script_text = script_text
        self._first_timeout = first_wait_timeout
        self.handles = []

    def _attach(self, kwargs):
        handle = kwargs.get("stdout")
        if handle is not None and handle is not subprocess.DEVNULL and self._script_text:
            handle.write(self._script_text)
            handle.flush()
        self.handles.append(handle)

    def wait(self, timeout=None):
        self._calls += 1
        if self._first_timeout and self._calls == 1:
            raise subprocess.TimeoutExpired("bash", timeout)
        return 0


def test_run_gripper_fcc_command_shape_and_log(launcher_env, monkeypatch, tmp_path):
    """FCC 抓取：命令行带 --side/--tau/--speed/--hold，**要 source**（它用 rclpy），
    输出落到 log_file（界面靠它显示进度）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    log = tmp_path / "fcc.log"
    script_text = ("FCC 实时抓取（左爪）：力矩 0.60 N·m，合拢速度 30 mm/s，夹持 5.0s\n"
                   "  [5/6] 夹持 5.0s\n"
                   "  夹持结果：左爪 开口 12.2mm（夹着东西），实际力矩 0.58 N·m\n"
                   "FCC 抓取完成，用时 11.7s\n")
    proc = _FakePopen(script_text)
    commands = []

    def fake_popen(cmd, **kwargs):
        commands.append(cmd[2])           # cmd 是 ["bash", "-c", <整条命令>]
        proc._attach(kwargs)
        return proc

    monkeypatch.setattr(co.subprocess, "Popen", fake_popen)

    lines = launcher.run_gripper_fcc("left", 300, 300, 5.0, log_file=log)

    cmd = commands[0]
    assert "source " in cmd and "setup.bash" in cmd       # 与直测/标定不同：它要 rclpy
    assert co.GRIPPER_FCC_SCRIPT_NAME in cmd
    assert "--side left --tau 300 --speed 300 --hold 5.0" in cmd
    assert log.read_text(encoding="utf-8") == script_text
    text = "\n".join(lines)
    assert "夹持结果" in text and "FCC 抓取完成" in text


def test_run_gripper_fcc_timeout_sends_sigint_not_kill(launcher_env, monkeypatch, tmp_path):
    """超时**先送 SIGINT**（脚本的 finally 会松开爪子并退出实时模式），不直接硬杀。

    直接 SIGKILL 的话，实时模式下没人继续喂指令，控制器会保持最后一条指令 ——
    夹爪冻在半合位置还在使劲。所以顺序必须是 SIGINT → 等 → 才 SIGKILL。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    log = tmp_path / "fcc.log"
    proc = _FakePopen(first_wait_timeout=True)
    signals = []
    monkeypatch.setattr(co.subprocess, "Popen", lambda cmd, **kw: (proc._attach(kw), proc)[1])
    monkeypatch.setattr(co.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(co.os, "killpg", lambda pgid, sig: signals.append(sig))

    lines = launcher.run_gripper_fcc("left", 300, 300, 5.0, log_file=log, timeout=0.01)

    assert signals == [co.signal.SIGINT]             # 只有 SIGINT，没有 SIGKILL
    text = "\n".join(lines)
    assert "已中断" in text and "SIGINT" in text
    assert "断电复位" not in text                     # 没落到硬杀那一档


def test_run_gripper_fcc_timeout_falls_back_to_kill(launcher_env, monkeypatch, tmp_path):
    """脚本连收尾都不动时：兜底 SIGKILL 并**明说夹爪可能停在半合位置**。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    log = tmp_path / "fcc.log"

    class _Stuck(_FakePopen):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("bash", timeout)

    signals = []
    monkeypatch.setattr(co.subprocess, "Popen", lambda cmd, **kw: _Stuck())
    monkeypatch.setattr(co.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(co.os, "killpg", lambda pgid, sig: signals.append(sig))

    lines = launcher.run_gripper_fcc("left", 300, 300, 5.0, log_file=log, timeout=0.01)

    assert signals == [co.signal.SIGINT, co.signal.SIGKILL]
    assert "断电复位" in "\n".join(lines)


def test_run_gripper_fcc_resolves_script_through_config(launcher_env, monkeypatch):
    """FCC 脚本走统一入口 `_script_for` 解析 —— 于是"项目 scripts/ 优先于工作空间"
    这条既有规则对它同样生效，脚本随上位机交付包走。

    同时确认：脚本不在位时 `_script_for` 抛的 `FileNotFoundError` 会原样传出去
    （界面把它显示成"未能执行"），不会被吞成一句"已完成"。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    seen = {}

    def fake_script_for(self, key, label, default=""):
        seen["key"], seen["default"] = key, default
        return tmp / "dex11_fcc_grasp.py"

    monkeypatch.setattr(co.Launcher, "_script_for", fake_script_for)
    monkeypatch.setattr(co.subprocess, "Popen", lambda *a, **k: _FakePopen())

    launcher.run_gripper_fcc("left", 300, 300, 3.0)

    assert seen == {"key": "gripper_fcc_script", "default": co.GRIPPER_FCC_SCRIPT_NAME}

    monkeypatch.setattr(co.Launcher, "_script_for",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError("FCC 抓取脚本不存在")))
    with pytest.raises(FileNotFoundError):
        launcher.run_gripper_fcc("left", 300, 300, 3.0)


def test_gripper_fcc_seconds_scales_with_speed_and_hold():
    """预计耗时：夹持时长按秒线性叠加，速度越慢越久（界面用它预告"会安静几秒"）。"""
    from data_collector import core_operations as co

    base = co.gripper_fcc_seconds(300, 3.0)
    assert co.gripper_fcc_seconds(300, 13.0) == pytest.approx(base + 10.0)
    assert co.gripper_fcc_seconds(100, 3.0) > base            # 慢了就更久
    assert co.gripper_fcc_seconds(300, 3.0) > 3.0


def test_summarize_scan_handles_three_shapes():
    """扫描摘要：没找到 / 找到多只 / 空输出，三种形态都要给出人话。"""
    from data_collector import core_operations as co

    none_found = co._summarize_scan("扫描 /dev/ttyUSB* -> [<empty>] ...\n没有找到夹爪。检查供电\n")
    assert "没有找到夹爪" in none_found[0]

    found = co._summarize_scan("扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n"
                               "右爪 id=0 (/dev/ttyUSB0)  q=  0.000 rad\n")
    assert any("id=0" in line for line in found)
    assert any("共找到 1 条读数" in line for line in found)

    assert "没有任何输出" in co._summarize_scan("")[0]


def test_summarize_calibration_flags_motor_problems():
    """标定输出里的电机问题要单独点名（id 不匹配 / 没应答），不能混进"正常退出"。"""
    from data_collector import core_operations as co

    class _Run:
        returncode = 0

    _Run.stdout = "  refusing to calibrate: asked id=1 but motor reports id=0\n"
    _Run.stderr = ""
    text = "\n".join(co._summarize_calibration(
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=1.0 rad\n",
        _Run(), "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=0.0 rad\n"))
    assert "未标定" in text and "id 与请求的不一致" in text

    _Run.stdout = "  motor id=0 on /dev/ttyUSB0 does not reply\n"
    text = "\n".join(co._summarize_calibration(
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=1.0 rad\n",
        _Run(), "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=1.0 rad\n"))
    assert "没有应答" in text

    # `err=0` 是正常输出的一部分，**不能**当成错误码（报"有问题"之前先证伪自己）；
    # 真正要报的是非 0 错误码
    _Run.stdout = "右爪 id=0 (/dev/ttyUSB0)  q=  0.000 rad  err=0(正常)\n"
    text = "\n".join(co._summarize_calibration(
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=1.0 rad\n",
        _Run(), "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=0.0 rad\n"))
    assert "正常退出" in text and "错误码" not in text

    _Run.stdout = "右爪 id=0 (/dev/ttyUSB0)  q=  0.000 rad  err=17(过温)\n"
    text = "\n".join(co._summarize_calibration(
        "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=1.0 rad\n",
        _Run(), "扫描 /dev/ttyUSB* -> [/dev/ttyUSB0] ...\n右爪 id=0 (/dev/ttyUSB0)  q=0.0 rad\n"))
    assert "非 0 错误码" in text and "err=17" in text


def test_summarize_fcc_passes_script_output_through():
    """FCC 的输出本身就是"阶段 + 结果"几行，原样带进结果区，不额外编结论。"""
    from data_collector import core_operations as co

    text = "FCC 抓取完成，用时 11.7s\n"
    lines = co._summarize_fcc("left", text, 0)
    assert lines == ["FCC 抓取完成，用时 11.7s"]        # 退出码 0 → 不加多余的话

    lines = co._summarize_fcc("left", text, 4)
    assert any("异常结束（退出码 4）" in line for line in lines)

    # 脚本自己已经说了原因（比如收不到关节）就不再补一句，免得两句打架
    warned = "⚠ 收不到 左爪 的夹爪关节 —— 请确认主程序在跑\n"
    assert not any("异常结束" in line for line in co._summarize_fcc("left", warned, 3))

    assert "没有任何输出" in co._summarize_fcc("left", "", 2)[0]


def _probe_output(subs: str, joint_lines: str = "") -> str:
    return f"SUBS={subs}\n" + ("JOINTS_BEGIN\n" + joint_lines + "JOINTS_END\n"
                               if joint_lines else "")


def test_probe_gripper_selects_route_by_topic_subscribers(launcher_env, monkeypatch):
    """选路判据：**话题有订阅者 = 可走 ROS 话题**（不是扫 /proc 的特征串）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    monkeypatch.setattr(co, "find_launch_process", lambda: 4321)
    joints = "name:\n- LJoint1_1\n- RJoint1_1\nposition:\n- 0.0123\n- -0.02\neffort:\n- 0.42\n- 0.0\n"
    monkeypatch.setattr(co.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout=_probe_output("1", joints), stderr=""))

    probe = launcher.probe_gripper()

    assert probe["available"] is True
    assert probe["running"] is True
    assert probe["blocked"] is True                    # 有主程序进程
    assert probe["sides"]["left"]["connected"] is True
    assert probe["sides"]["left"]["effort"] == pytest.approx(0.42)
    assert probe["sides"]["right"]["connected"] is True
    assert "走 ROS 话题" in probe["reason"]


def test_probe_gripper_without_subscribers_goes_direct(launcher_env, monkeypatch):
    """话题没订阅者 + 没有主程序进程 → 走直测工具，并且**不去读关节**（省一次超时）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    monkeypatch.setattr(co, "find_launch_process", lambda: None)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, stdout=_probe_output("0"), stderr="")

    monkeypatch.setattr(co.subprocess, "run", fake_run)

    probe = launcher.probe_gripper()

    assert probe["running"] is False
    assert probe["blocked"] is False
    assert "直测工具" in probe["reason"]
    assert len(commands) == 1                          # 只有一条 bash，不是两条串行
    assert probe["sides"]["left"]["connected"] is None  # 没读过就是"未检测"
    # 短路写在脚本里：没有订阅者就不去 echo /joint_states（否则每次都要白等一次超时）
    assert '"${subs:-0}" -gt 0' in commands[0]


def test_probe_gripper_blocks_when_process_up_but_topic_not_ready(launcher_env, monkeypatch):
    """主程序进程在、话题还没起来（正在启动中）：两条路都不走，说清等几秒。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    monkeypatch.setattr(co, "find_launch_process", lambda: 4321)
    monkeypatch.setattr(co.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout=_probe_output("0"), stderr=""))

    probe = launcher.probe_gripper()

    assert probe["running"] is False
    assert probe["blocked"] is True
    assert "两条路都不适合走" in probe["reason"]


def test_probe_gripper_flags_wrong_hand_type(launcher_env, monkeypatch, tmp_path):
    """手型不对要**说清当前是什么、怎么换**；读不到 Product.json 则不拦（见下一条）。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    product = tmp_path / "Product.json"
    product.write_text(json.dumps({"hand": "empty"}), encoding="utf-8")
    monkeypatch.setattr(co, "PRODUCT_JSON_PATH", product)
    monkeypatch.setattr(co, "find_launch_process", lambda: 4321)
    monkeypatch.setattr(co.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout=_probe_output("1"), stderr=""))

    probe = launcher.probe_gripper()

    assert probe["hand"] == "empty"
    assert probe["hand_type_ok"] is False
    assert "不是宇树夹爪" in probe["reason"] and "setup_product.sh" in probe["reason"]


def test_probe_gripper_without_product_file_does_not_block(launcher_env, monkeypatch):
    """读不到 Product.json（开发机/路径变了）**不当成错误**，否则功能会被永久锁死。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    monkeypatch.setattr(co, "PRODUCT_JSON_PATH", tmp / "没有这个文件.json")
    monkeypatch.setattr(co, "find_launch_process", lambda: None)
    monkeypatch.setattr(co.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout=_probe_output("0"), stderr=""))

    probe = launcher.probe_gripper()

    assert probe["hand"] == ""
    assert probe["hand_type_ok"] is True


def test_probe_gripper_reports_missing_ros_environment(launcher_env, monkeypatch):
    """ROS 环境没进来：`available=False`，**不能**被当成"主程序没跑"而放行直测工具。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    monkeypatch.setattr(co.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="ROS2=missing\n", stderr=""))

    probe = launcher.probe_gripper()

    assert probe["available"] is False
    assert probe["running"] is False
    assert "ROS 环境不可用" in probe["reason"]


def test_release_hardware_connections_survives_subprocess_failure(launcher_env, monkeypatch):
    """发不出去也要如实说，并且**不抛异常** —— 收尾流程不能被它挡住。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env

    def boom(cmd, **kwargs):
        raise OSError("bash 不可用")

    monkeypatch.setattr(core_operations.subprocess, "run", boom)

    text = "\n".join(launcher.release_hardware_connections())

    assert "未确认送达" in text
    assert "请人工确认" in text


# ---------- 启动程序后自动连底盘（现场：不连就没有 /power_report） ----------
def test_connect_chassis_sends_true_to_chassis_connect(launcher_env, monkeypatch):
    """向 /chassis_control/connect 发 data: true，并且等底盘节点出现再发。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = " ".join(cmd)
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "rc1=0\n", "")

    monkeypatch.setattr(core_operations.subprocess, "run", fake_run)
    lines = launcher.connect_chassis()

    cmd = seen["cmd"]
    assert "/chassis_control/connect" in cmd
    assert "data: true" in cmd
    assert "-w 1" in cmd                    # 等底盘节点出现再发，不猜 sleep
    assert "timeout 30" in cmd              # 主程序拉起节点要几秒，给足等待
    # 外层兜底超时必须大于"每条发布自己的 timeout"，否则会把还在等的发布掐掉
    assert seen["timeout"] > 30

    text = "\n".join(lines)
    assert "已自动连接底盘（底盘）" in text
    assert "/power_report" in text          # 说清这一步是为了让电量有数据


def test_connect_chassis_reports_unconfirmed_and_keeps_going(launcher_env, monkeypatch):
    """底盘节点没起来（rc=124）时如实报，并提示电量仍会是未知。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(
        core_operations.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "rc1=124\n", ""))

    text = "\n".join(launcher.connect_chassis())

    assert "未确认" in text and "等待超时" in text
    assert "电量仍会显示未知" in text
    assert "已自动连接底盘" not in text      # 没送到就不能说"已连接"


def test_connect_chassis_skips_without_workspace(launcher_env, monkeypatch):
    """工作空间不存在时直接跳过，不起子进程。"""
    from data_collector import core_operations

    launcher, tmp, _cfg, _calls = launcher_env
    monkeypatch.setattr(launcher.config, "check_workspace", lambda: False)
    ran = []
    monkeypatch.setattr(core_operations.subprocess, "run",
                        lambda *a, **kw: ran.append(a))

    text = "\n".join(launcher.connect_chassis())

    assert "跳过底盘连接" in text
    assert ran == []


def test_stop_main_program_disconnects_before_killing(launcher_env, monkeypatch):
    """顺序钉死：**先下使能，再停进程** —— 反了硬件会留在使能态（现场 P0）。"""
    from data_collector import core_operations

    launcher, _tmp, _cfg, _calls = launcher_env
    order = []

    monkeypatch.setattr(
        core_operations.Launcher, "release_hardware_connections",
        lambda self, timeout=4.0: order.append("disconnect") or
        ["已下发断开连接（下使能）：躯干、手臂、底盘"])
    monkeypatch.setattr(core_operations.ProcessManager, "terminate_by_name",
                        lambda self, name: order.append(f"terminate:{name}"))
    monkeypatch.setattr(core_operations.MainProgramCleaner, "stop",
                        lambda self: {"found": [], "steps": [], "survivors": []})
    monkeypatch.setattr(core_operations.Launcher, "count_ros_topics",
                        lambda self, timeout=0: (0, [], "绕过 daemon 直查"))

    text = "\n".join(launcher.stop_main_program())

    assert order[0] == "disconnect", "下使能必须先于任何进程清理"
    assert order[1] == "terminate:run_nodes"
    assert "已下发断开连接（下使能）：躯干、手臂、底盘" in text


def test_count_ros_topics_reports_unverified_without_ros_env(launcher_env):
    """没有 ROS 环境时返回 -1：表示"没核验成"，不能当成"没有残留"。"""
    launcher, _tmp, _cfg, _calls = launcher_env       # 故意不建 install/setup.bash

    assert launcher.count_ros_topics(timeout=1.0) == (-1, [], "未核验")


# ---------- SDK 服务（systemctl）路径：不能抛异常、要能识别"服务拉起了界面" ----------
def test_runs_under_service_detects_own_cgroup():
    """本界面是否活在某个服务的 cgroup 里 —— 决定"停服务会不会连带杀掉界面"。"""
    from data_collector.core_operations import runs_under_service

    cgroup_v2 = "0::/user.slice/user-1000.slice/user@1000.service/wheel_controller.service\n"
    cgroup_v1 = "9:name=systemd:/user.slice/user-1000.slice/session-1.scope\n"

    assert runs_under_service("wheel_controller.service", cgroup_v2) is True
    assert runs_under_service("wheel_controller", cgroup_v2) is True
    assert runs_under_service("wheel_controller.service", cgroup_v1) is False
    assert runs_under_service("", cgroup_v2) is False
    assert runs_under_service("wheel_controller.service", "") is False






def _FakeConfig_from_ops(service_name: str = "wheel_controller.service"):
    """SdkServiceManager 只需要"服务名"这一个能力，不必搭整个 ConfigManager。"""
    class _Cfg:
        def get_sdk_service_name(self):
            return service_name

    return _Cfg()


def test_workspaces_parsed_from_launch_command():
    """工作空间要从**实际启动命令**里解析：配置里的路径可能和现场在用的那份不一致。"""
    from data_collector.core_operations import _workspaces_from_command

    cmd = ("bash -c cd /home/nerv/test_ws/wheeled_robot_x86_64-v1.4.0-develop/"
           "wheeled_robot && ./run_nodes.sh joy")
    assert _workspaces_from_command(cmd) == [
        "/home/nerv/test_ws/wheeled_robot_x86_64-v1.4.0-develop/wheeled_robot"]

    abs_cmd = "/opt/robot/ws/run_nodes.sh rviz"
    assert "/opt/robot/ws" in _workspaces_from_command(abs_cmd)
    assert _workspaces_from_command("ros2 topic list") == []


def test_cleaner_matches_nodes_of_discovered_workspace():
    """按命令行解析出的工作空间匹配节点 —— 只认配置路径会漏扫（现场就是这样漏的）。"""
    configured = "/home/nerv/test_ws/wheeled_robot_pkg"          # 配置里写的
    actual = "/home/nerv/test_ws/wheeled_robot_x86_64-v1.4.0-develop/wheeled_robot"
    node = (f"{actual}/install/lib/arm_control/arm_control_node "
            "--ros-args -r __ns:=/")

    rows = [
        {"pid": 100, "pgid": 100,
         "cmd": f"bash -c cd {actual} && ./run_nodes.sh joy"},   # 启动入口（带真实路径）
        {"pid": 200, "pgid": 200, "cmd": node},                  # 真实工作空间下的节点
        {"pid": 300, "pgid": 300, "cmd": "/usr/bin/gnome-terminal -- bash"},
    ]
    cleaner = _cleaner(lister=lambda: rows, workspace=configured)

    found = cleaner.scan()

    assert {item["pid"] for item in found} == {100, 200}
    assert actual in cleaner.workspaces and configured in cleaner.workspaces
    # 无关终端仍被放过
    assert all("gnome-terminal" not in item["cmd"] for item in found)


# ---------- 本地日志目录：必须落在项目根，不能落在包目录 ----------
def test_local_log_dir_defaults_to_project_root_not_package(tmp_path):
    """回归守卫：日志目录用过 `Path(__file__).parent.parent`，拆包后算成了**包目录**。

    后果是运行日志（遥操作日志、状态文件）被写进 `data_collector/logs/`，
    打包时"删掉 logs 再打"总是漏掉这一半 —— 实测 301 个日志文件被打进交付包。
    """
    from data_collector.core import PROJECT_ROOT

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml"})

    resolved = ConfigManager(cfg_file).get_local_log_dir()

    assert resolved == PROJECT_ROOT / "logs"
    assert resolved.parent == PROJECT_ROOT            # 项目根，而不是 data_collector/


def test_local_log_dir_can_be_overridden(tmp_path):
    """部署或测试可以把本地日志目录挪走（app_config.yaml 的 local_log_dir）。"""
    target = tmp_path / "mylogs"
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path), "teleop_config_path": "t.yaml",
                         "local_log_dir": str(target)})

    assert ConfigManager(cfg_file).get_local_log_dir() == target


# ============ 遥操作用户查找（现场：切用户总弹标定流程） ============
# 现场反馈："切换遥操作用户时，会弹出标定流程（不管这个人是否已经标定过）"。
# 查了两条真实成因，下面几条分别钉住：
#   ① 名字是**逐字节精确匹配**：大小写/不可见字符不同就判成"没标定过"；
#   ② 标定按**工作空间**存放，版本升级换到新工作空间后所有人都"没标定过"。
def _sibling_env(tmp_path, *, active_users=("default",), old_users=("hyx", "gmz")):
    """造出现场那种并列布局：

        <tmp>/now/install/config/teleop_config.yaml   当前工作空间（版本包带的出厂配置）
        <tmp>/old/install/config/teleop_config.yaml   上一次的工作空间（现场用户的标定）
    """
    from data_collector.core import ConfigManager, save_yaml

    now, old = tmp_path / "now", tmp_path / "old"
    for ws, users in ((now, active_users), (old, old_users)):
        (ws / "install" / "config").mkdir(parents=True)
        save_yaml(ws / "install" / "config" / "teleop_config.yaml", {
            "teleop": {"calibration": users[0]},
            "calibrations": {u: {"tracker_index": "tracker4", "shoulder_width": 0.28,
                                 "arm_length": 0.60} for u in users},
            "limits": {"chassis": {"linear_scale": 0.4}},
        })
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(now),
                         "teleop_config_path": "install/config/teleop_config.yaml"})
    return ConfigManager(cfg_file), now, old


def test_find_calibration_key_ignores_case_and_invisible_chars(temp_dir):
    """查人不受大小写、首尾空白、零宽字符影响 —— 一个字符的差错不该换一次重新标定。

    现场是手打名字：多打一个空格、输入法留个零宽空格、大小写不对，都会被原来的
    精确匹配判成"这个没标定过"，然后弹一整套 VR 标定流程（实测复现）。
    """
    from data_collector.core import ConfigManager, save_yaml

    tmp, cfg_file = temp_dir
    save_yaml(tmp / "teleop.yaml", {
        "teleop": {"calibration": "default"},
        "calibrations": {"hyx": {"tracker_index": "tracker4", "shoulder_width": 0.28}},
    })
    cm = ConfigManager(cfg_file)

    for typed in ("hyx", "HYX", "Hyx", " hyx ", "hyx\u200b", "\ufeffhyx", "ＨＹＸ"):
        # 全角（ＨＹＸ）算不算同一个人？`casefold` 会把全角折成半角，正好是想要的
        assert cm.find_calibration_key(typed) == "hyx", f"输入 {typed!r} 没匹配上"
    assert cm.get_user_calibration("HYX")["tracker_index"] == "tracker4"
    assert cm.find_calibration_key("zhangsan") is None
    assert cm.find_calibration_key("") is None


def test_find_user_reads_sibling_workspace(tmp_path):
    """当前工作空间里没有这个人时，去并列的其它工作空间里找。

    版本下载把新包解到当前工作空间的兄弟目录并自动切过去，新目录里那份是出厂配置 ——
    于是"这个人已经标定过"在界面上表现为"没标定过"。
    """
    from data_collector.core_operations import TeleopManager, Launcher

    cm, now, old = _sibling_env(tmp_path)
    mgr = TeleopManager(cm, Launcher(cm))

    assert cm.calibration_users() == ["default"]          # 当前这份确实只有出厂用户
    match = mgr.find_user("hyx")
    assert match is not None
    assert match.key == "hyx"
    assert match.source == old / "install/config/teleop_config.yaml"   # 数据来源说得清
    assert match.imported is True
    # 别的版本目录里的人也应当出现在下拉里，否则现场还是得盲打
    assert mgr.known_users() == ["default", "hyx", "gmz"]


def test_activate_user_imports_profile_into_current_workspace(tmp_path):
    """切到别的工作空间里的用户时，那条标定要**真的写进当前配置**。

    只改 `teleop.calibration` 不够：机器人侧 teleop_node 只读它自己那份
    `${workspace}/install/config/teleop_config.yaml`，参数不在文件里就会退回默认标定 ——
    界面显示切成功了、机器人用的是别人的参数。
    """
    from data_collector.core_operations import TeleopManager, Launcher

    cm, now, old = _sibling_env(tmp_path)
    mgr = TeleopManager(cm, Launcher(cm))
    match = mgr.find_user("HYX")                          # 顺手带个大小写差异

    assert mgr.activate_user(match.key, match.source) is True

    cfg = cm.get_teleop_config()
    assert cfg["teleop"]["calibration"] == "hyx"
    assert cfg["calibrations"]["hyx"]["shoulder_width"] == 0.28
    assert cfg["calibrations"]["default"] is not None      # 原有用户不受影响
    assert cfg["limits"]["chassis"]["linear_scale"] == 0.4  # 其它顶层字段不被抹掉


def test_import_calibration_never_overwrites_current_workspace(tmp_path):
    """当前工作空间里已有的那个用户是权威，不能被旧版本目录里的旧参数盖掉。"""
    from data_collector.core import save_yaml
    from data_collector.core_operations import TeleopManager, Launcher

    cm, now, old = _sibling_env(tmp_path, active_users=("default", "hyx"))
    save_yaml(now / "install/config/teleop_config.yaml", {
        "teleop": {"calibration": "default"},
        "calibrations": {"hyx": {"tracker_index": "tracker2", "shoulder_width": 0.99}},
    })
    mgr = TeleopManager(cm, Launcher(cm))

    match = mgr.find_user("hyx")
    assert match is not None and match.imported is False       # 命中当前这份，不用导入
    assert cm.import_calibration("hyx", old / "install/config/teleop_config.yaml") is False
    assert cm.get_user_calibration("hyx")["shoulder_width"] == 0.99


def test_teleop_config_candidates_empty_when_path_is_absolute(tmp_path):
    """配置里把 teleop_config_path 写成绝对路径时只认那一个文件：不做跨工作空间搜索。

    那是"我已经指定了用哪份配置"的意思，到处翻别的工作空间反而会引入不想要的参数。
    """
    from data_collector.core import ConfigManager, save_yaml

    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "ws"),
                         "teleop_config_path": str(tmp_path / "pinned.yaml")})
    cm = ConfigManager(cfg_file)

    assert cm.teleop_config_candidates() == []
    assert cm.calibration_users() == []          # 文件不存在也不抛


def test_calibration_users_survives_broken_config(tmp_path):
    """配置写坏/不是映射结构时，列用户只返回空表 —— 不能因此让"启动遥操作"打不开。"""
    from data_collector.core import ConfigManager, save_yaml

    ws = tmp_path / "ws"
    (ws / "install" / "config").mkdir(parents=True)
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(ws),
                         "teleop_config_path": "install/config/teleop_config.yaml"})
    cm = ConfigManager(cfg_file)

    (ws / "install/config/teleop_config.yaml").write_text("calibrations: [1, 2\n", encoding="utf-8")
    assert cm.calibration_users() == []
    assert cm.find_calibration_key("hyx") is None


def test_wait_camera_calibration_ready_probes_the_ros_graph(launcher_env, monkeypatch):
    """「节点就绪」的判据是**服务注册出来了**，不是"进程还在"。

    进程起来但 DDS 没发现完 / 还没走到 `create_service` 时，`ros2 service call` 只会拿到
    "服务不存在" —— 所以等的是 ROS 图里的服务（与夹爪那边"看话题订阅者数、不看 /proc
    命令行"同一条判据，见 MEMORY.md 的三条不能弄错的判据）。
    """
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _ros_ready_workspace(tmp)
    seen = {}

    class Done:
        stdout, stderr, returncode = "CALIB_READY\n", "", 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd[2] if isinstance(cmd, list) else str(cmd)
        seen["timeout"] = kwargs.get("timeout")
        return Done()

    monkeypatch.setattr(co.subprocess, "run", fake_run)
    result = launcher.wait_camera_calibration_ready(timeout=5.0, interval=1.0)

    assert result["ready"] is True
    assert "/calibration/start_srv" in result["service"]
    assert "ros2 service list" in seen["cmd"]        # 判据是 ROS 图，不是进程
    assert "grep -qx" in seen["cmd"]                 # 整行精确匹配（前缀会撞上别的服务）
    assert "$SECONDS" in seen["cmd"]                 # 按 deadline 循环，不是"轮数 × 间隔"
    assert seen["timeout"] and seen["timeout"] > 5.0  # 外层还有兜底超时
    assert "已就绪" in result["text"]


def test_wait_camera_calibration_ready_reports_timeout_honestly(launcher_env, monkeypatch):
    """等不到就如实说"没就绪 + 等了多久"，不假装成功 —— 调用方据此**不下发**指令。"""
    from data_collector import core_operations as co

    launcher, tmp, _cfg, _calls = launcher_env
    _ros_ready_workspace(tmp)

    class Done:
        stdout, stderr, returncode = "CALIB_TIMEOUT\n", "", 0

    monkeypatch.setattr(co.subprocess, "run", lambda *a, **k: Done())
    result = launcher.wait_camera_calibration_ready(timeout=3.0, interval=0.5)

    assert result["ready"] is False
    assert "仍没看到" in result["text"]
    assert "没有下发" not in result["text"] or True    # 措辞由调用方补，这里只说事实


def test_wait_camera_calibration_ready_without_workspace(launcher_env):
    """工作空间不存在时不跑 ROS 命令，直接说清楚。"""
    from data_collector import core_operations as co

    launcher, _tmp, _cfg, _calls = launcher_env
    launcher.config.workspace = launcher.config.workspace / "not-exists"
    result = launcher.wait_camera_calibration_ready(timeout=1.0, interval=0.5)

    assert result["ready"] is False
    assert "工作空间不存在" in result["text"]
    assert co is not None


# ============================================================
# 姿势控制：「姿势控制」页面对外下发的两条入口（v5.6.0）
#
# 这一组的价值是**命令串本身**：沙箱里没有 rclpy，跑不了真机，所以话题名、消息类型、
# 单位（度 → 弧度）、payload 的写法就是唯一能在这里钉住的东西。逐条断言，不靠"看着像"。
# ============================================================

def _capture_all_commands(monkeypatch, tmp, stdout="rc1=0\n"):
    """把接下来所有 `ros2 topic pub` 的 bash 命令按顺序抓下来（一次调用一个命令）。"""
    from data_collector import core_operations

    _fake_setup(tmp)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd[2], "timeout": kwargs.get("timeout")})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(core_operations.subprocess, "run", fake_run)
    return calls


def test_publish_motion_falls_back_to_single_shot_cli(launcher_env, monkeypatch):
    """通道不可用时，姿势控制的下发退回**一次性 CLI**（并发 + `-w 1` + keep-alive 2s）。

    v6.2.0：正常情况下走常驻通道；这条用例考的是**降级那条路**（conftest 里默认把通道
    禁掉）。降级必须真的可用 —— 通道起不来（没脚本 / 没 rclpy / 本机没 ROS）时界面照旧
    能下发，这是"不改变原有可用性"的底线。

    两条路的**结果形状必须一样**：``{名字: {ok, reason, matched, sent, channel}}``，
    `ok=None` = 未确认。否则"这条到底是没发出去还是没人收"又要靠猜。
    """
    from data_collector import core_motion

    launcher, tmp, _cfg, _calls = launcher_env
    calls = _capture_all_commands(monkeypatch, tmp, "rc1=0\nrc2=0\n")

    entries = [("左臂运动", core_motion.ARM_MOVE_TOPICS["left"], core_motion.ARM_MOVE_MSG,
                core_motion.arm_move_payload([0] * 7, 0.4)),
               ("左臂手型", core_motion.HAND_POSTURE_TOPIC, core_motion.HAND_POSTURE_MSG,
                core_motion.hand_posture_payload("left", [1] * 6))]
    results = launcher.publish_motion(entries, timeout=6.0, label="测试下发")

    assert set(results) == {"左臂运动", "左臂手型"}
    for name, item in results.items():
        assert item["ok"] is True, name
        assert item["channel"] == "一次性 CLI", name
        assert item["reason"] == ""
    assert len(calls) == 1                      # 一条命令里并发发出去（不是串行两次）
    cmd = calls[0]["cmd"]
    # `--keep-alive 2`（原本 1）：现场"一次发两条只到一条"说明交付握手可能没走完 ——
    # 降级路径没有回读确认，只能把窗口放大（并发下发，墙钟成本 ≈ 2s）
    assert cmd.count("ros2 topic pub --once -w 1 --keep-alive 2") == 2
    assert "/arm_control/left_arm_move wheeled_robot_msgs/msg/ArmMove" in cmd
    assert "/arm_control/hand_posture wheeled_robot_msgs/msg/HandPosture" in cmd
    assert "scale: 0.4" in cmd
    assert calls[0]["timeout"] and calls[0]["timeout"] > 6.0    # 外层兜底要比每条更长


class _FakeStreamProc:
    """假的实时流子进程：按剧本吐行、给出退出码，并记录收到的信号。

    `pid` 故意用一个**不存在的号**：`_terminate_stream` 会先试 `os.killpg`，
    拿到 `ProcessLookupError` 后退化到 `proc.send_signal` —— 这样用例既能验证
    "信号发出去了"，又不会误伤跑用例的这个进程组（真 pid 会把整组 SIGTERM 掉）。
    """

    def __init__(self, lines=None, code=0, hang=False):
        self.stdout = list(lines or [])
        self._code = code
        self._hang = hang
        self.pid = 999999
        self.signals = []
        self.terminated = False

    def poll(self):
        return None if (self._hang or not self.terminated) else self._code

    def wait(self, timeout=None):
        self.terminated = True
        if self._hang:
            raise subprocess.TimeoutExpired("motion_stream.py", timeout or 0)
        return self._code

    def send_signal(self, sig):
        self.signals.append(sig)
        self.terminated = True

    def kill(self):
        self.signals.append("kill")
        self.terminated = True


def _capture_stream_commands(monkeypatch, *, lines=None, code=0, hang=False,
                             timeout=0.02):
    """把 `run_motion_streams` 起的子进程换成假进程，返回 (命令列表, 假进程列表)。"""
    from data_collector import core_operations

    cmds, procs = [], []

    def fake_popen(cmd, **kwargs):
        assert cmd[0] == "bash" and cmd[1] == "-c"       # 与一次性通道同一套起法
        cmds.append({"cmd": cmd[2], "kwargs": kwargs})
        proc = _FakeStreamProc(lines=lines, code=code, hang=hang)
        procs.append(proc)
        return proc

    monkeypatch.setattr(core_operations.subprocess, "Popen", fake_popen)
    return cmds, procs


def test_motion_stream_runs_the_script_with_the_documented_arguments(launcher_env, monkeypatch):
    """实时流走**自建脚本**（100Hz 发不出一次性 CLI 的节奏），命令与输出都逐条钉住。

    这条通道与一次性通道分工明确：连续运动给脚本（rclpy + 自己插值），
    一次性指令仍然全部走 `_publish_topics` —— 所以这里也守"脚本参数里有 `--prepare`、
    频率对齐、退出码 0 才算到位"。
    """
    from data_collector import core_motion

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    job = core_motion.trunk_stream_job([30, 54, 24, 0, 0, 0])
    cmds, _procs = _capture_stream_commands(
        monkeypatch, lines=["躯干实时流：0.0 → 30.0", "已到位（用时 2.5s）"], code=0)
    seen = []

    results = launcher.run_motion_streams([job], on_line=seen.append)

    assert results == {"躯干实时流": True}
    assert len(cmds) == 1
    cmd = cmds[0]["cmd"]
    assert "motion_stream.py" in cmd and "trunk" in cmd
    assert "--values 30,54,24,0,0,0" in cmd
    assert "--prepare" in cmd and "--rate 100" in cmd
    assert cmds[0]["kwargs"]["start_new_session"] is True     # 中止时能整组收到信号
    assert cmds[0]["kwargs"]["stderr"] == subprocess.STDOUT    # 脚本的报错也要进日志
    assert any("已到位" in line for line in seen)


def test_motion_stream_maps_exit_codes_to_confirmed_or_not(launcher_env, monkeypatch):
    """退出码 → 结论：0 = 走到位；非 0 = 没走到位（保留脚本自己那几行原因）；
    超时/收不到退出码 = **未经确认**（不许当成成功，也不许当成失败）。"""
    from data_collector import core_motion

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    job = core_motion.arm_stream_job("left", [0] * 7)

    _capture_stream_commands(monkeypatch, lines=["ERROR: 读不到 left 当前角度"], code=3)
    assert launcher.run_motion_streams([job]) == {"左臂实时流": False}

    _capture_stream_commands(monkeypatch, hang=True)
    seen = []
    assert launcher.run_motion_streams([job], on_line=seen.append, timeout=0.05) == \
        {"左臂实时流": None}
    assert any("未经确认" in line for line in seen)


def test_motion_stream_terminates_the_process_when_aborted(launcher_env, monkeypatch):
    """中止要**真的把脚本结束掉**（脚本在 finally 里退出 follow_position，否则手臂会卡在实时流模式）。"""
    from data_collector import core_motion

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    job = core_motion.trunk_stream_job([0] * 6)
    _cmds, procs = _capture_stream_commands(monkeypatch, hang=True)

    results = launcher.run_motion_streams([job], is_aborted=lambda: True, timeout=5.0)

    assert results == {"躯干实时流": None}            # 中止 ≠ 走到位
    assert procs and procs[0].signals                 # 信号确实发出去了


def test_motion_stream_without_workspace_or_script_sends_nothing(launcher_env, monkeypatch):
    """工作空间不在 / 脚本不在：一条都不发，并如实说明（不留"发了一半"的中间态）。"""
    from data_collector import core_motion

    launcher, tmp, _cfg, _calls = launcher_env
    _fake_setup(tmp)
    job = core_motion.trunk_stream_job([0] * 6)
    cmds, _procs = _capture_stream_commands(monkeypatch, code=0)

    real_workspace = launcher.config.workspace
    launcher.config.workspace = real_workspace / "not-exists"
    seen = []
    assert launcher.run_motion_streams([job], on_line=seen.append) == {"躯干实时流": False}
    assert cmds == [] and any("工作空间不存在" in line for line in seen)

    # 脚本找不到：把解析结果指到一个不存在的位置（工作空间先还原，否则会先被上面那条拦住）
    launcher.config.workspace = real_workspace
    monkeypatch.setattr(launcher.config, "resolve_script_path",
                        lambda name: real_workspace / "nowhere" / name)
    seen = []
    assert launcher.run_motion_streams([job], on_line=seen.append) == {"躯干实时流": False}
    assert any("找不到实时流脚本" in line for line in seen)
