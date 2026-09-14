import os
import zipfile
import paramiko
import time
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List, Callable
from .core import logger

# 自定义异常
class UploadCancelledError(Exception):
    pass

class RecorderManager:
    def __init__(self, config, launcher):
        self.config = config
        self.launcher = launcher
        self.current_proc = None

    def start(self, script_type: str, name: str = "", desc: str = "") -> bool:
        if self.current_proc and self.current_proc.poll() is None:
            logger.warning("录制已在进行中")
            return False
        try:
            self.current_proc = self.launcher.launch_record(script_type, name, desc)
            return True
        except Exception as e:
            logger.error(f"启动录制失败: {e}")
            return False

    def stop(self) -> bool:
        if self.current_proc and self.current_proc.poll() is None:
            from .core import terminate
            terminate(self.current_proc)
            self.current_proc = None
            return True
        return False

    def is_active(self) -> bool:
        return self.current_proc is not None and self.current_proc.poll() is None


class DataUploader:
    def __init__(self):
        self.bag_dir = Path("/var/nerv/bag")
        self.remote_user = "nerv"
        self.remote_host = "172.16.10.2"
        self.remote_base = "/NervData/share/logs/遥操作数据"
        self.password = "nervai"
        self.temp_dir = Path(tempfile.gettempdir()) / "data_collector_uploads"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.upload_cancelled = False

    def get_bag_folders(self) -> List[Path]:
        if not self.bag_dir.exists():
            return []
        subdirs = [d for d in self.bag_dir.iterdir() if d.is_dir()]
        subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        return subdirs

    def get_latest_bag_folder(self) -> Optional[Path]:
        folders = self.get_bag_folders()
        return folders[0] if folders else None

    def compress_folder(self, folder: Path, output_zip: Path,
                    running_cb: Callable[[], bool] = None,
                    progress_cb: Callable[[str], None] = None) -> bool:
        """
        压缩文件夹，支持取消和进度回调
        :param folder: 要压缩的文件夹
        :param output_zip: 输出的 zip 文件路径
        :param running_cb: 返回 bool 的函数，用于检查是否取消（True 继续，False 取消）
        :param progress_cb: 进度信息回调，接收字符串（速度/剩余时间）
        :return: 成功返回 True，失败或取消返回 False
        """
        try:
            files = []
            total_size = 0
            for root, _, fs in os.walk(folder):
                for f in fs:
                    path = Path(root) / f
                    files.append(path)
                    total_size += path.stat().st_size

            if total_size == 0:
                raise Exception("bag 文件夹为空")

            current = 0
            start = time.time()
            last_progress_time = 0.0

            with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for file in files:
                    # 每处理一个文件检查取消标志
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消压缩")
                    arcname = str(file.relative_to(folder.parent))
                    z.write(str(file), arcname)
                    current += file.stat().st_size
                    if total_size > 0:
                        percent = int((current / total_size) * 50)
                        if progress_percent:
                            progress_percent(percent)
                    elapsed = time.time() - start
                    speed = current / 1024 / 1024 / max(elapsed, 0.1)
                    if speed > 0:
                        remain_sec = (total_size - current) / (speed * 1024 * 1024)
                    else:
                        remain_sec = 0
                    now = time.time()
                    if progress_cb and (now - last_progress_time >= 0.5 or current >= total_size):
                        progress_cb(f"压缩速度: {speed:.1f} MB/s  剩余: {remain_sec:.0f} 秒")
                        last_progress_time = now
            logger.info(f"压缩完成: {output_zip}")
            return True
        except UploadCancelledError:
            logger.info("压缩被取消")
            return False
        except Exception as e:
            logger.error(f"压缩失败: {e}")
            return False

    def _ensure_remote_dir(self, sftp, remote_dir: str):
        """递归创建远程目录"""
        dirs = []
        path = remote_dir
        while path not in ("", "/"):
            dirs.append(path)
            path = os.path.dirname(path)
        for d in reversed(dirs):
            try:
                sftp.mkdir(d)
            except:
                pass  # 目录已存在

    def _upload_file(self, local_path: Path, remote_path: str,
                 running_cb: Callable[[], bool] = None,
                 progress_cb: Callable[[str], None] = None,
                 progress_percent: Callable[[int], None] = None) -> bool:
        """
        使用 paramiko 上传文件，支持取消和进度回调
        :param local_path: 本地文件路径
        :param remote_path: 远程完整路径（含文件名）
        :param running_cb: 返回 bool 的函数，用于检查是否取消
        :param progress_cb: 进度信息回调（速度/剩余时间）
        :param progress_percent: 进度百分比回调（0-100）
        :return: 成功返回 True，失败或取消返回 False
        """
        transport = paramiko.Transport((self.remote_host, 22))
        try:
            transport.connect(username=self.remote_user, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            remote_dir = os.path.dirname(remote_path)
            self._ensure_remote_dir(sftp, remote_dir)

            file_size = local_path.stat().st_size
            start = time.time()
            uploaded = 0
            last_progress_time = 0.0

            def callback(done, total):
                nonlocal uploaded, last_progress_time
                uploaded = done
                if file_size > 0:
                    percent = 50 + int((done / file_size) * 50)
                    if progress_percent:
                        progress_percent(percent)
                # 每次回调检查取消标志
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消上传")
                elapsed = time.time() - start
                speed = done / 1024 / 1024 / max(elapsed, 0.1)
                if speed > 0:
                    remain_sec = (file_size - done) / (speed * 1024 * 1024)
                else:
                    remain_sec = 0
                now = time.time()
                if progress_cb and (now - last_progress_time >= 0.5 or done >= file_size):
                    progress_cb(f"上传速度: {speed:.1f} MB/s  剩余: {remain_sec:.0f} 秒")
                    last_progress_time = now
                if progress_percent:
                    progress_percent(50 + int(done / file_size * 50))

            sftp.put(str(local_path), remote_path, callback=callback)
            logger.info(f"上传成功: {remote_path}")
            return True

        except UploadCancelledError:
            # 取消时删除远程文件（如果部分上传）
            try:
                sftp.remove(remote_path)
                logger.info(f"已删除远程文件: {remote_path}")
                # 尝试删除空目录
                try:
                    sftp.rmdir(remote_dir)
                    logger.info(f"已删除远程空目录: {remote_dir}")
                except:
                    pass
            except Exception as e:
                logger.warning(f"清理远程文件失败: {e}")
            return False

        except Exception as e:
            logger.error(f"上传失败: {e}")
            return False
        finally:
            transport.close()
    def upload_latest_bag(self, remote_subpath: str = "",
                          progress_cb: Callable[[str], None] = None,
                          progress_percent: Callable[[int], None] = None) -> bool:
        """上传最新 bag 文件夹（支持取消）"""
        self.upload_cancelled = False
        

        def running():
            return not self.upload_cancelled

        latest = self.get_latest_bag_folder()
        if not latest:
            logger.error("没有找到 bag 文件夹")
            return False

        zip_name = f"{latest.name}.zip"
        zip_path = self.temp_dir / zip_name

        # 压缩
        if not self.compress_folder(latest, zip_path, running, progress_cb):
            return False

        # 远程路径
        remote_dir = self.remote_base
        if remote_subpath:
            remote_dir += f"/{remote_subpath}"
        remote_path = f"{remote_dir}/{zip_name}"

        try:
            success = self._upload_file(zip_path, remote_path, running, progress_cb, progress_percent)
            return success
        except UploadCancelledError:
            logger.info("上传已取消")
            return False
        finally:
            # 删除本地临时zip
            if zip_path.exists():
                zip_path.unlink()
                logger.info("已删除本地临时zip")
        

    def upload_specific_bag(self, folder_path: Path, remote_subpath: str = "",
                            progress_cb: Callable[[str], None] = None,
                            progress_percent: Callable[[int], None] = None) -> bool:
        """上传指定的 bag 文件夹"""
        self.upload_cancelled = False

        def running():
            return not self.upload_cancelled

        if not folder_path.exists() or not folder_path.is_dir():
            logger.error(f"文件夹不存在: {folder_path}")
            return False

        zip_name = f"{folder_path.name}.zip"
        zip_path = self.temp_dir / zip_name

        if not self.compress_folder(folder_path, zip_path, running, progress_cb):
            return False

        remote_dir = self.remote_base
        if remote_subpath:
            remote_dir += f"/{remote_subpath}"
        remote_path = f"{remote_dir}/{zip_name}"

        try:
            success = self._upload_file(zip_path, remote_path, running, progress_cb, progress_percent)
            return success
        except UploadCancelledError:
            logger.info("上传已取消")
            return False
        finally:
            if zip_path.exists():
                zip_path.unlink()
                logger.info("已删除本地临时zip")

    def cancel_upload(self):
        """取消当前上传"""
        self.upload_cancelled = True


class LogFetcher:
    def __init__(self, local_log_dir: Path):
        self.log_dir = Path("/var/nerv/log")
        self.local_log_dir = local_log_dir
        self.local_log_dir.mkdir(parents=True, exist_ok=True)

    def get_log_files(self) -> List[Path]:
        if not self.log_dir.exists():
            return []
        files = [f for f in self.log_dir.iterdir() if f.is_file()]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return files

    def get_latest_log_file(self) -> Optional[Path]:
        files = self.get_log_files()
        return files[0] if files else None

    def fetch_specific_log(self, file_path: Path) -> bool:
        if not file_path.exists() or not file_path.is_file():
            logger.error(f"日志文件不存在: {file_path}")
            return False
        dest = self.local_log_dir / file_path.name
        try:
            shutil.copy2(file_path, dest)
            logger.info(f"日志已复制到: {dest}")
            return True
        except Exception as e:
            logger.error(f"复制日志失败: {e}")
            return False

    def fetch_latest_log(self) -> bool:
        latest = self.get_latest_log_file()
        if not latest:
            return False
        dest = self.local_log_dir / latest.name
        try:
            shutil.copy2(latest, dest)
            logger.info(f"日志已复制到: {dest}")
            return True
        except Exception as e:
            logger.error(f"复制日志失败: {e}")
            return False


class DataPlayer:
    def __init__(self):
        pass

    def play_bag(self, bag_path: str):
        logger.info("数据播放功能预留")