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
    def __init__(self, config=None):
        """config 为 ConfigManager 时从 app_config.yaml 读取远程主机/目录与密码；

        不传 config 时沿用历史默认值，保证向后兼容（测试与旧调用方可用）。
        """
        settings = config.get_remote_settings() if config is not None else {}
        self.config = config
        self.bag_dir = config.get_bag_dir() if config is not None else Path("/var/nerv/bag")
        self.remote_host = settings.get("host", "172.16.10.2")
        self.remote_port = int(settings.get("port", 22))
        self.remote_user = settings.get("user", "nerv")
        self.remote_base = settings.get("base", "/NervData/share/logs/遥操作数据")
        self.password = settings.get("password", "nervai")
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
                        progress_cb: Callable[[str], None] = None,
                        progress_percent: Callable[[int], None] = None) -> bool:
        """压缩文件夹，支持取消、文字进度和百分比进度。压缩阶段占总进度 0~50%。"""
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
            start_time = time.time()
            last_progress_time = 0.0

            # 显式启用 ZIP64，并在每个大文件 entry 上强制使用 ZIP64。
            # 机器人 Bag/MCAP/DB3 很容易超过传统 ZIP 2 GiB/4 GiB 边界。
            with zipfile.ZipFile(
                output_zip,
                mode='w',
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as z:
                for file in files:
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消压缩")

                    arcname = str(file.relative_to(folder.parent))
                    # 分块写入 + force_zip64=True，支持超大单文件并保持实时进度。
                    with file.open('rb') as src, z.open(
                        arcname, 'w', force_zip64=True
                    ) as dst:
                        while True:
                            if running_cb and not running_cb():
                                raise UploadCancelledError("用户取消压缩")
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                            current += len(chunk)

                            now = time.time()
                            elapsed = max(now - start_time, 0.1)
                            speed = current / 1024 / 1024 / elapsed
                            remain_sec = ((total_size - current) / (speed * 1024 * 1024)) if speed > 0 else 0
                            percent = int(current / total_size * 50)

                            if progress_percent:
                                progress_percent(min(percent, 50))
                            if progress_cb and (now - last_progress_time >= 0.5 or current >= total_size):
                                progress_cb(
                                    f"正在压缩：{current / 1024 / 1024:.1f} / "
                                    f"{total_size / 1024 / 1024:.1f} MB  "
                                    f"速度：{speed:.1f} MB/s  剩余：{remain_sec:.0f} 秒"
                                )
                                last_progress_time = now

            if progress_percent:
                progress_percent(50)
            if progress_cb:
                progress_cb(f"压缩完成：{total_size / 1024 / 1024:.1f} MB")
            logger.info(f"压缩完成: {output_zip}")
            return True
        except UploadCancelledError:
            logger.info("压缩被取消")
            try:
                if output_zip.exists():
                    output_zip.unlink()
                    logger.info(f"已删除未完成压缩文件: {output_zip}")
            except Exception as cleanup_error:
                logger.warning(f"删除未完成压缩文件失败: {cleanup_error}")
            return False
        except Exception as e:
            logger.error(f"压缩失败: {e}")
            try:
                if output_zip.exists():
                    output_zip.unlink()
                    logger.info(f"已删除压缩失败残留文件: {output_zip}")
            except Exception as cleanup_error:
                logger.warning(f"删除压缩失败残留文件失败: {cleanup_error}")
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
        transport = paramiko.Transport((self.remote_host, self.remote_port))
        sftp = None
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
                    progress_cb(
                        f"正在上传：{done / 1024 / 1024:.1f} / "
                        f"{file_size / 1024 / 1024:.1f} MB  "
                        f"速度：{speed:.1f} MB/s  剩余：{remain_sec:.0f} 秒"
                    )
                    last_progress_time = now
                if progress_percent:
                    progress_percent(50 + int(done / file_size * 50))

            sftp.put(str(local_path), remote_path, callback=callback)
            if progress_percent:
                progress_percent(100)
            if progress_cb:
                progress_cb(f"上传完成：{file_size / 1024 / 1024:.1f} MB")
            logger.info(f"上传成功: {remote_path}")
            return True

        except UploadCancelledError:
            # 取消时删除远程文件（如果部分上传）
            try:
                if sftp is None:
                    return False
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
        if not self.compress_folder(latest, zip_path, running, progress_cb, progress_percent):
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

        if not self.compress_folder(folder_path, zip_path, running, progress_cb, progress_percent):
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


    # ==================== 远程目录浏览 ====================
    def _connect_sftp(self):
        """创建独立的 SFTP 连接，调用方负责关闭。"""
        transport = paramiko.Transport((self.remote_host, self.remote_port))
        try:
            transport.connect(username=self.remote_user, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            return transport, sftp
        except Exception:
            transport.close()
            raise

    @staticmethod
    def _normalize_remote_path(path: str) -> str:
        """统一远程路径格式，避免 // 和尾部 /。"""
        path = (path or "/").replace("\\", "/")
        while "//" in path:
            path = path.replace("//", "/")
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def _check_remote_path(self, path: str) -> str:
        """限制远程目录浏览/删除等操作在 remote_base 下。"""
        path = self._normalize_remote_path(path)
        base = self._normalize_remote_path(self.remote_base)
        if path != base and not path.startswith(base + "/"):
            raise ValueError(f"远程路径超出允许范围: {path}")
        return path

    def list_remote_dir(self, remote_path: str = None) -> List[dict]:
        """列出远程目录，返回 name/path/is_dir/size/mtime。"""
        remote_path = self._check_remote_path(remote_path or self.remote_base)
        transport, sftp = self._connect_sftp()
        try:
            result = []
            for attr in sftp.listdir_attr(remote_path):
                name = attr.filename
                full_path = self._normalize_remote_path(remote_path + "/" + name)
                is_dir = bool(attr.st_mode & 0o040000)
                result.append({
                    "name": name,
                    "path": full_path,
                    "is_dir": is_dir,
                    "size": int(getattr(attr, "st_size", 0) or 0),
                    "mtime": int(getattr(attr, "st_mtime", 0) or 0),
                })
            result.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return result
        finally:
            sftp.close()
            transport.close()

    def get_remote_file_info(self, remote_path: str) -> dict:
        """获取远程文件/目录的详细信息（RemoteDirectoryDialog 用）。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            attr = sftp.stat(remote_path)
            return {
                "name": os.path.basename(remote_path.rstrip("/")) or remote_path,
                "path": remote_path,
                "is_dir": bool(attr.st_mode & 0o040000),
                "size": int(getattr(attr, "st_size", 0) or 0),
                "mtime": int(getattr(attr, "st_mtime", 0) or 0),
            }
        finally:
            sftp.close()
            transport.close()

    def search_remote(self, remote_path: str, keyword: str,
                      running_cb: Callable[[], bool] = None,
                      progress_cb: Callable[[int, int], None] = None) -> List[dict]:
        """在 remote_path 下递归搜索名称包含 keyword 的项（忽略大小写）。

        running_cb: 返回 False 时中止搜索（抛 UploadCancelledError，与取消上传统一处理）。
        progress_cb: 回调 (已扫描数量, 已命中数量)。
        """
        remote_path = self._check_remote_path(remote_path or self.remote_base)
        keyword = (keyword or "").lower()
        results: List[dict] = []
        scanned = 0

        transport, sftp = self._connect_sftp()
        try:
            stack = [remote_path]
            while stack:
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消搜索")
                current = stack.pop()
                try:
                    entries = sftp.listdir_attr(current)
                except IOError as e:
                    logger.warning(f"跳过无法访问的目录 {current}: {e}")
                    continue
                for attr in entries:
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消搜索")
                    scanned += 1
                    name = attr.filename
                    full_path = self._normalize_remote_path(current + "/" + name)
                    is_dir = bool(attr.st_mode & 0o040000)
                    if keyword in name.lower():
                        results.append({
                            "name": name,
                            "path": full_path,
                            "is_dir": is_dir,
                            "size": int(getattr(attr, "st_size", 0) or 0),
                            "mtime": int(getattr(attr, "st_mtime", 0) or 0),
                        })
                    if is_dir:
                        stack.append(full_path)
                    if progress_cb:
                        progress_cb(scanned, len(results))
            results.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            logger.info(f"搜索完成: 关键词={keyword!r} 扫描 {scanned} 项, 命中 {len(results)} 项")
            return results
        finally:
            sftp.close()
            transport.close()

    def upload_local_file(self, local_path: Path, remote_path: str,
                          running_cb: Callable[[], bool] = None,
                          progress_cb: Callable[[int, int], None] = None) -> bool:
        """上传单个本地文件到远程（RemoteDirectoryDialog 的“上传文件”用）。

        progress_cb: 回调 (已完成字节, 总字节)；running_cb 返回 False 时中止并删除残缺文件。
        """
        local_path = Path(local_path)
        if not local_path.is_file():
            logger.error(f"本地文件不存在: {local_path}")
            return False
        try:
            remote_path = self._check_remote_path(remote_path)
        except ValueError as e:
            logger.error(f"远程路径非法: {e}")
            return False

        transport = paramiko.Transport((self.remote_host, self.remote_port))
        sftp = None
        try:
            transport.connect(username=self.remote_user, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            self._ensure_remote_dir(sftp, os.path.dirname(remote_path))

            def callback(done, total):
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消上传")
                if progress_cb:
                    progress_cb(int(done), int(total))

            sftp.put(str(local_path), remote_path, callback=callback)
            logger.info(f"上传成功: {remote_path}")
            return True
        except UploadCancelledError:
            logger.info(f"上传已取消: {remote_path}")
            try:
                if sftp is not None:
                    sftp.remove(remote_path)
                    logger.info(f"已删除未完成上传文件: {remote_path}")
            except Exception as e:
                logger.warning(f"清理远程文件失败: {e}")
            return False
        except Exception as e:
            logger.error(f"上传失败: {e}")
            return False
        finally:
            transport.close()

    def remote_path_exists(self, remote_path: str) -> bool:
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            sftp.stat(remote_path)
            return True
        except IOError:
            return False
        finally:
            sftp.close()
            transport.close()

    def create_remote_dir(self, remote_path: str) -> bool:
        """递归创建远程目录。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            self._ensure_remote_dir(sftp, remote_path)
            return True
        finally:
            sftp.close()
            transport.close()

    def _remove_remote_recursive(self, sftp, remote_path: str):
        for attr in sftp.listdir_attr(remote_path):
            child = self._normalize_remote_path(remote_path + "/" + attr.filename)
            if attr.st_mode & 0o040000:
                self._remove_remote_recursive(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(remote_path)

    def get_remote_disk_usage(self, remote_path: str = None) -> dict:
        """通过远程 df 获取文件系统空间，兼容不支持 SFTP statvfs 的服务端。"""
        import shlex

        remote_path = self._check_remote_path(remote_path or self.remote_base)
        transport = None
        channel = None
        try:
            transport, _sftp = self._connect_sftp()
            command = "df -Pk -- " + shlex.quote(remote_path)
            channel = transport.open_session()
            channel.exec_command(command)
            stdout = channel.makefile("r", -1).read()
            stderr = channel.makefile_stderr("r", -1).read()
            status = channel.recv_exit_status()
            if status != 0:
                raise RuntimeError(stderr.strip() or f"远程 df 执行失败，退出码：{status}")

            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            if len(lines) < 2:
                raise RuntimeError(f"无法解析远程 df 输出：{stdout!r}")
            parts = lines[-1].split()
            capacity_index = next(
                (i for i, p in enumerate(parts)
                 if p.endswith('%') and p[:-1].isdigit()),
                None
            )
            if capacity_index is None or capacity_index < 3:
                raise RuntimeError(f"无法解析远程 df 输出：{lines[-1]!r}")

            total = int(parts[capacity_index - 3]) * 1024
            used = int(parts[capacity_index - 2]) * 1024
            available = int(parts[capacity_index - 1]) * 1024
            return {
                'total': total,
                'used': used,
                'free': available,
                'available': available,
                'block_size': 1024,
            }
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass

    def get_remote_tree_size(self, remote_path: str,
                             running_cb=None, progress_cb=None) -> int:
        """递归统计远程目录下所有文件的总大小。"""
        remote_path = self._check_remote_path(remote_path)
        transport, sftp = self._connect_sftp()
        try:
            total_size = 0
            file_count = 0
            stack = [remote_path]

            while stack:
                if running_cb and not running_cb():
                    raise UploadCancelledError("用户取消目录统计")

                current = stack.pop()
                for attr in sftp.listdir_attr(current):
                    if running_cb and not running_cb():
                        raise UploadCancelledError("用户取消目录统计")

                    child = self._normalize_remote_path(
                        current + "/" + attr.filename
                    )
                    is_dir = bool(attr.st_mode & 0o040000)

                    if is_dir:
                        stack.append(child)
                    else:
                        total_size += int(getattr(attr, "st_size", 0) or 0)
                        file_count += 1

                    if progress_cb:
                        progress_cb(total_size, file_count)

            return total_size
        finally:
            sftp.close()
            transport.close()

    def delete_remote_path(self, remote_path: str) -> bool:
        """删除远程文件或目录。禁止删除 remote_base 本身。"""
        remote_path = self._check_remote_path(remote_path)
        base = self._normalize_remote_path(self.remote_base)
        if remote_path == base:
            raise ValueError("不能删除远程根目录")
        transport, sftp = self._connect_sftp()
        try:
            attr = sftp.stat(remote_path)
            if attr.st_mode & 0o040000:
                self._remove_remote_recursive(sftp, remote_path)
            else:
                sftp.remove(remote_path)
            return True
        finally:
            sftp.close()
            transport.close()

    def download_remote_file(self, remote_path: str, local_path: Path,
                             progress_cb: Callable[[int, int], None] = None) -> bool:
        """下载远程文件到本地。"""
        remote_path = self._check_remote_path(remote_path)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        transport, sftp = self._connect_sftp()
        try:
            sftp.get(str(remote_path), str(local_path), callback=progress_cb)
            return True
        finally:
            sftp.close()
            transport.close()


class LogFetcher:
    def __init__(self, local_log_dir: Path, config=None):
        # 源日志目录可由 app_config.yaml 的 log_dir 指定，默认 /var/nerv/log
        self.log_dir = config.get_log_dir() if config is not None else Path("/var/nerv/log")
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