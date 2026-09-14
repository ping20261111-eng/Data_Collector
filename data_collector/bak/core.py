import os
import sys
import re
import time
import signal
import subprocess
import logging
import yaml
import shutil
from pathlib import Path
from typing import Optional, List, Tuple, Any

# ============ 日志 ============
def setup_logger(name: str = "data_collector", log_file: Path = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger

logger = setup_logger()

# ============ YAML 工具 ============
def load_yaml(path: Path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def save_yaml(path: Path, data: dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

# ============ 子进程工具 ============
def check_command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def run_in_terminal(command: str, terminal: str = "gnome-terminal") -> subprocess.Popen:
    """在新终端中执行命令，自动选择可用终端（优先 xterm）"""
    if "DISPLAY" not in os.environ:
        logger.warning("DISPLAY 未设置，可能无法启动图形终端")

    if check_command_exists("xterm"):
        cmd = ["xterm", "-e", f"bash -c '{command}; exec bash'"]
        try:
            return subprocess.Popen(cmd)
        except Exception as e:
            logger.warning(f"xterm 启动失败: {e}")

    if check_command_exists("gnome-terminal"):
        cmd = ["gnome-terminal", "--disable-factory", "--", "bash", "-c", f"{command}; exec bash"]
        try:
            return subprocess.Popen(cmd)
        except Exception as e:
            logger.warning(f"gnome-terminal (disable-factory) 启动失败: {e}")

    if check_command_exists(terminal):
        cmd = [terminal, "--", "bash", "-c", f"{command}; exec bash"]
        try:
            return subprocess.Popen(cmd)
        except Exception as e:
            raise RuntimeError(f"终端启动失败: {e}")

    raise RuntimeError("没有可用的终端模拟器 (xterm, gnome-terminal)")

def run_background(command: str, cwd: Path) -> subprocess.Popen:
    """后台运行命令（无终端），创建独立进程组"""
    return subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

def run_with_env(cmd_list: List[str], env_add: dict = None, cwd: Path = None) -> subprocess.Popen:
    env = os.environ.copy()
    if env_add:
        env.update(env_add)
    return subprocess.Popen(
        cmd_list,
        env=env,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

def terminate(proc: subprocess.Popen) -> None:
    """终止进程及其子进程，发送 SIGINT (Ctrl+C) 模拟优雅退出"""
    if proc and proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGINT)
            proc.wait(timeout=3)
        except (ProcessLookupError, OSError) as e:
            logger.debug(f"无法发送进程组信号，回退到单个进程: {e}")
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except subprocess.TimeoutExpired:
            proc.kill()

# ============ 配置管理器 ============
class ConfigManager:
    def __init__(self, config_path: Path):
        self.app_config = load_yaml(config_path)
        self.workspace = Path(self.app_config["workspace_root"]).expanduser()
        self.teleop_config_path = self.workspace / self.app_config["teleop_config_path"]

    def get_teleop_config(self) -> dict:
        return load_yaml(self.teleop_config_path)

    def save_teleop_config(self, data: dict) -> None:
        top_order = ['teleop', 'calibrations', 'trackers']
        ordered_data = {}
        for key in top_order:
            if key in data:
                ordered_data[key] = data[key]

        if 'calibrations' in ordered_data:
            calib_ordered = {}
            inner_order = ['tracker_index', 'shoulder_width', 'arm_length', 'elbow_length']
            for user, params in ordered_data['calibrations'].items():
                user_ordered = {}
                for k in inner_order:
                    if k in params:
                        user_ordered[k] = params[k]
                for k, v in params.items():
                    if k not in user_ordered:
                        user_ordered[k] = v
                calib_ordered[user] = user_ordered
            ordered_data['calibrations'] = calib_ordered

        save_yaml(self.teleop_config_path, ordered_data)

    def get_user_calibration(self, username: str) -> Optional[dict]:
        cfg = self.get_teleop_config()
        return cfg.get("calibrations", {}).get(username)

    def set_teleop_calibration_user(self, username: str) -> bool:
        cfg = self.get_teleop_config()
        if username not in cfg.get("calibrations", {}):
            return False
        cfg["teleop"]["calibration"] = username
        self.save_teleop_config(cfg)
        return True

    def add_calibration_user(self, username: str, tracker: str, sw: float, al: float, elbow: float = 0.35):
        cfg = self.get_teleop_config()
        cfg.setdefault("calibrations", {})[username] = {
            "tracker_index": tracker,
            "shoulder_width": sw,
            "arm_length": al,
            "elbow_length": elbow
        }
        cfg["teleop"]["calibration"] = username
        self.save_teleop_config(cfg)

    def get_script_path(self, key: str) -> Path:
        return self.workspace / self.app_config[key]

    def get_teleop_script_path(self) -> Path:
        return self.workspace / self.app_config["run_teleop_script"]

    def get_terminal(self) -> str:
        return self.app_config.get("terminal_command", "gnome-terminal")

    def check_workspace(self) -> bool:
        return self.workspace.exists()
    
# ============ 进程管理器 ============
class ProcessManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._processes = []
        return cls._instance

    def register(self, proc: subprocess.Popen, name: str = ""):
        self._processes.append((proc, name))
        logger.debug(f"进程注册: {name} PID={proc.pid}")

    def unregister(self, proc: subprocess.Popen):
        self._processes = [(p, n) for p, n in self._processes if p != proc]

    def terminate_by_name(self, name_pattern: str):
        for proc, name in self._processes[:]:
            if name_pattern in name:
                logger.info(f"终止进程: {name} PID={proc.pid}")
                terminate(proc)
                self.unregister(proc)

    def terminate_all(self):
        for proc, name in self._processes[:]:
            terminate(proc)
        self._processes.clear()