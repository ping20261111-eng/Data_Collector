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
        # 以 argv 数组传参，避免命令行里的单引号与外层引用冲突；
        # -hold 让脚本退出后窗口保留，方便查看报错。
        cmd = ["xterm", "-hold", "-e", "bash", "-c", f"{command}; exec bash"]
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
        """按约定顺序写回 teleop 配置。

        注意：只对已知字段做重排序，其它顶层字段（如 limits）必须原样保留，
        否则标定/切换用户会把这些机器人控制参数整段抹掉。
        """
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

        # 保留 top_order 之外的其它顶层字段（如 limits），避免丢配置。
        for key, value in data.items():
            if key not in ordered_data:
                ordered_data[key] = value

        save_yaml(self.teleop_config_path, ordered_data)

    def get_user_calibration(self, username: str) -> Optional[dict]:
        cfg = self.get_teleop_config()
        return cfg.get("calibrations", {}).get(username)

    def set_teleop_calibration_user(self, username: str) -> bool:
        cfg = self.get_teleop_config()
        if username not in cfg.get("calibrations", {}):
            return False
        cfg.setdefault("teleop", {})["calibration"] = username
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
        cfg.setdefault("teleop", {})["calibration"] = username
        self.save_teleop_config(cfg)

    def get_script_path(self, key: str) -> Path:
        return self.workspace / self.app_config[key]

    def get_teleop_script_path(self) -> Path:
        # 旧配置文件可能缺少该键，给出默认值避免 KeyError。
        return self.workspace / self.app_config.get("run_teleop_script", "run_teleop.sh")

    def get_terminal(self) -> str:
        return self.app_config.get("terminal_command", "gnome-terminal")

    # ---------- 路径 / 远程主机配置（带默认值，兼容旧配置文件） ----------
    def get_bag_dir(self) -> Path:
        return Path(self.app_config.get("bag_dir", "/var/nerv/bag"))

    def get_log_dir(self) -> Path:
        return Path(self.app_config.get("log_dir", "/var/nerv/log"))

    def get_sudo_password(self) -> str:
        """sudo 密码：优先环境变量，其次配置文件，最后回退到历史默认值。

        空字符串视为未配置，避免配置文件里留了空值导致认证失败。
        """
        return (
            os.environ.get("DATA_COLLECTOR_SUDO_PASSWORD")
            or self.app_config.get("sudo_password")
            or "nervai"
        )

    def get_remote_settings(self) -> dict:
        """SFTP 目标配置：密码支持用环境变量覆盖，避免明文写在配置里。"""
        return {
            "host": self.app_config.get("remote_host") or "172.16.10.2",
            "port": int(self.app_config.get("remote_port") or 22),
            "user": self.app_config.get("remote_user") or "nerv",
            "password": (
                os.environ.get("DATA_COLLECTOR_REMOTE_PASSWORD")
                or self.app_config.get("remote_password")
                or "nervai"
            ),
            "base": self.app_config.get("remote_base")
            or "/NervData/share/logs/遥操作数据",
        }

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