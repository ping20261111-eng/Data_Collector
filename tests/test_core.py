import pytest
from pathlib import Path
from data_collector.core import ConfigManager, load_yaml, save_yaml

@pytest.fixture
def temp_dir(tmp_path):
    # 创建模拟配置文件
    app_cfg = {
        "workspace_root": str(tmp_path),
        "run_nodes_script": "run.sh",
        "teleop_config_path": "teleop.yaml",
        "record_bag_script": "bag.sh",
        "record_cmd_script": "cmd.sh",
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


# ---------- 缺省值 / 配置读取 ----------
def test_get_teleop_script_path_default_when_key_missing(temp_dir):
    """旧配置文件缺少 run_teleop_script 时不应抛 KeyError。"""
    tmp, cfg_file = temp_dir
    cm = ConfigManager(cfg_file)
    assert cm.get_teleop_script_path() == tmp / "run_teleop.sh"


def test_default_paths_and_remote_settings(temp_dir, monkeypatch):
    tmp, cfg_file = temp_dir
    monkeypatch.delenv("DATA_COLLECTOR_REMOTE_PASSWORD", raising=False)
    monkeypatch.delenv("DATA_COLLECTOR_SUDO_PASSWORD", raising=False)
    cm = ConfigManager(cfg_file)

    assert cm.get_bag_dir() == Path("/var/nerv/bag")
    assert cm.get_log_dir() == Path("/var/nerv/log")
    assert cm.get_sudo_password() == "nervai"

    settings = cm.get_remote_settings()
    assert settings["host"] == "172.16.10.2"
    assert settings["port"] == 22
    assert settings["user"] == "nerv"
    assert settings["base"] == "/NervData/share/logs/遥操作数据"


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
    # 空字符串视为未配置 -> 回退默认值
    assert cm.get_sudo_password() == "nervai"