import os
import subprocess
import time
import re
from pathlib import Path
from typing import List, Tuple, Optional
from .core import (
    logger, run_in_terminal, run_background, run_with_env, terminate,
    ConfigManager, ProcessManager
)

class Launcher:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.pm = ProcessManager()

    def launch_run_nodes(self, options: List[str]) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        script = self.config.get_script_path("run_nodes_script")
        if not script.exists():
            raise FileNotFoundError(f"脚本不存在: {script}")
        if not os.access(script, os.X_OK):
            raise PermissionError(f"脚本没有执行权限: {script}")

        setup_script = self.config.workspace / "install/setup.bash"
        if not setup_script.exists():
            raise FileNotFoundError(f"setup.bash 不存在: {setup_script}")

        cmd = (
            f"cd {self.config.workspace} && "
            f"echo \"nervai\" | sudo -S -v 2>/dev/null && "
            f"source install/setup.bash && "
            f"bash {script} " + " ".join(options) + " ; "
            f"echo '=== run_nodes.sh 已退出，退出码: $? ===' ; "
            f"read -p '按 Enter 键关闭窗口...'"
        )
        terminal = self.config.get_terminal()
        logger.info(f"在新终端启动主程序: {cmd}")
        proc = run_in_terminal(cmd, terminal)
        self.pm.register(proc, "run_nodes")
        return proc

    def launch_teleop(self) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        script = self.config.get_teleop_script_path()
        if not script.exists():
            raise FileNotFoundError(f"遥操作脚本不存在: {script}")
        if not os.access(script, os.X_OK):
            raise PermissionError(f"脚本没有执行权限: {script}")

        setup_script = self.config.workspace / "install/setup.bash"
        if not setup_script.exists():
            raise FileNotFoundError(f"setup.bash 不存在: {setup_script}")

        cmd = (
            f"cd {self.config.workspace} && "
            f"source install/setup.bash && "
            f"bash {script}"
        )
        logger.info(f"后台启动遥操作: {cmd}")
        proc = run_background(cmd, self.config.workspace)
        self.pm.register(proc, "run_teleop")
        return proc

    def launch_record(self, script_type: str, name: str = "", desc: str = "") -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        key = f"record_{script_type}_script"
        script = self.config.get_script_path(key)
        if not script.exists():
            raise FileNotFoundError(f"录制脚本不存在: {script}")
        if not os.access(script, os.X_OK):
            raise PermissionError(f"脚本没有执行权限: {script}")

        args = []
        if name:
            args.append(f'name="{name}"')
        if desc:
            args.append(f'desc="{desc}"')

        bag_dir = "/var/nerv/bag"
        cmd = (
            f"mkdir -p {bag_dir} 2>/dev/null || true && "
            f"cd {bag_dir} && "
            f"source {self.config.workspace}/install/setup.bash && "
            f"bash {script} " + " ".join(args)
        )
        logger.info(f"后台启动录制 ({script_type}): {cmd}")
        proc = run_background(cmd, self.config.workspace)
        self.pm.register(proc, f"record_{script_type}")
        return proc

    def launch_vr_bridge(self) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        cmd = f"source {self.config.workspace}/install/setup.bash && ros2 run teleop vr_bridge_node"
        proc = run_with_env(["bash", "-c", cmd], cwd=self.config.workspace)
        self.pm.register(proc, "vr_bridge")
        return proc

    def launch_calibration(self, username: str, tracker_number_str: str) -> subprocess.Popen:
        if not self.config.check_workspace():
            raise FileNotFoundError(f"工作空间不存在: {self.config.workspace}")
        cmd = f"source {self.config.workspace}/install/setup.bash && ros2 run teleop calibration_node"
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(self.config.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        proc.stdin.write(f"{username}\n{tracker_number_str}\n")
        proc.stdin.flush()
        self.pm.register(proc, "calibration")
        return proc


class TeleopManager:
    def __init__(self, config: ConfigManager, launcher: Launcher):
        self.config = config
        self.launcher = launcher
        self.pm = ProcessManager()

    def check_user(self, username: str) -> bool:
        return self.config.get_user_calibration(username) is not None

    def activate_user(self, username: str) -> bool:
        return self.config.set_teleop_calibration_user(username)

    def cancel_calibration(self):
        """取消标定：终止 vr_bridge 和 calibration 进程"""
        self.pm.terminate_by_name("vr_bridge")
        self.pm.terminate_by_name("calibration")

    def run_calibration(self, username: str, tracker_number: int, output_callback=None) -> Tuple[bool, Optional[str], Optional[float], Optional[float]]:
        tracker = f"tracker{tracker_number}"
        vr_proc = None
        cal_proc = None
        try:
            vr_proc = self.launcher.launch_vr_bridge()
            self.pm.register(vr_proc, "vr_bridge")   # 冗余，但注册已由 launch_vr_bridge 完成，此处可不重复
            time.sleep(1)
            cal_proc = self.launcher.launch_calibration(username, str(tracker_number))
            self.pm.register(cal_proc, "calibration")
            sw, al = None, None
            calibration_done = False
            timeout = 120
            start = time.time()
            while time.time() - start < timeout:
                import select
                if select.select([cal_proc.stdout], [], [], 0.1)[0]:
                    line = cal_proc.stdout.readline()
                    if not line:
                        if cal_proc.poll() is not None:
                            if calibration_done:
                                break
                            else:
                                err = cal_proc.stderr.read()
                                return False, f"标定节点意外退出: {err}", None, None
                        continue
                    if output_callback:
                        output_callback(line.strip())
                    logger.debug(f"calibration: {line.strip()}")
                    if "标定完成" in line:
                        calibration_done = True
                        for _ in range(5):
                            nxt = cal_proc.stdout.readline()
                            if not nxt:
                                break
                            if output_callback:
                                output_callback(nxt.strip())
                            m = re.search(r"shoulder_width\s*=\s*([\d.]+)", nxt)
                            if m: sw = float(m.group(1))
                            m = re.search(r"arm_length\s*=\s*([\d.]+)", nxt)
                            if m: al = float(m.group(1))
                        break
                else:
                    if cal_proc.poll() is not None:
                        if calibration_done:
                            break
                        else:
                            err = cal_proc.stderr.read()
                            return False, f"标定节点意外退出: {err}", None, None

            if sw is None or al is None:
                rest = cal_proc.stdout.read()
                if output_callback and rest:
                    output_callback(rest.strip())
                m = re.search(r"shoulder_width\s*=\s*([\d.]+)", rest)
                if m: sw = float(m.group(1))
                m = re.search(r"arm_length\s*=\s*([\d.]+)", rest)
                if m: al = float(m.group(1))
                if sw is None or al is None:
                    err = cal_proc.stderr.read()
                    return False, f"解析标定结果失败: {err}", None, None

            self.config.add_calibration_user(username, tracker, sw, al, 0.35)
            # 标定完成，终止辅助进程
            self.pm.terminate_by_name("vr_bridge")
            self.pm.terminate_by_name("calibration")
            return True, None, sw, al
        except Exception as e:
            logger.error(f"标定异常: {e}", exc_info=True)
            self.pm.terminate_by_name("vr_bridge")
            self.pm.terminate_by_name("calibration")
            return False, str(e), None, None

    def launch_teleop(self) -> subprocess.Popen:
        return self.launcher.launch_teleop()