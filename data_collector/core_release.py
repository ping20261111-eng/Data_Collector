"""版本包获取：列远端版本 → 下载（可续传） → 安全解压成工作空间。

## 为什么单独成一个模块

数据采集（core_data）管"录什么"，进程编排（core_operations）管"起什么"，
都不负责回答"工作空间从哪来"。版本下载是第三条独立的线：

    内网只读文件服务（nginx autoindex） → 本地归档 → 解压 → 可用工作空间

## 关键约束

- **零第三方依赖**：下载用标准库 `urllib.request`。机器人镜像上多装一个
  requests/httpx 是长期负担，而这里只需要 GET + Range 两件事。
- **断点续传**：远端 nginx 带 `Accept-Ranges: bytes`，291MB 的包断一次全重下代价太大。
  下到 `<name>.tar.bz2.part`，下次按已落盘偏移发 Range 续传。
  两条纪律：① 服务器不认 Range（返回 200）时**必须从零写** —— 拿 200 的响应体
  追加到已下载内容后面，会得到一个"字节数正确、内容错乱"的包，而大小校验发现不了；
  ② 只有**同一个归档**才接偏移（文件名带版本与时间戳，天然可寻址）。
- **解压不肯覆盖**：目标目录已存在就拒绝。那极可能是现场正在使用的工作空间，
  覆盖等于把运行中的环境挖掉一半。
- **路径穿越必须自己校验**：tar 成员名可以写成 `../../etc/xxx`。Python 3.12 才有
  `filter="data"`，本项目要兼容 3.8+，所以逐成员校验（绝对路径、`..`、软链指向外部、
  设备文件一律拒绝）。
- **不假设包内顶层名**：现有包解开是 `<包名>/wheeled_robot/...`，但换机型/换打包脚本
  就可能变成 `<包名>/...`，所以解压后用 `detect_workspace_root()` 现场认。
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .errors import AppError, ErrorCode

ARCHIVE_SUFFIX = ".tar.bz2"
PART_SUFFIX = ".part"
STAGING_TMPL = ".{name}.extracting"
CHUNK = 256 * 1024
#: 解压后占用按压缩包的倍数估。实测（端到端跑过）：291MB 的包解出 **0.96 GB = 3.3 倍**。
#: 取 4.0 倍留一点余量 —— 宁可保守拦下，也不要解到一半磁盘满
#: （那会留下一个清理不掉的半成品）。
#: 注意别把归档自身算进"解压后大小"：第一次统计时我多算了 0.29GB，
#: 得出 4.4 倍这个偏高的数字，照着它改系数会误报空间不足。
EXTRACT_SPACE_FACTOR = 4.0
EXTRACT_SPACE_MARGIN = 200 * 1024 * 1024
USER_AGENT = "Data-Collector/version-fetch"

#: 进度回调：(已完成, 总量, 附注)。总量为 0 表示"算不出总量"，界面应显示不确定进度。
#: 这个形状与 ui/threads.py 的 `progress(int, int, str)` 信号一致，接线时不必转换。
Progress = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

_ROW_RE = re.compile(
    r'<a\s+href="(?P<href>[^"]*)"[^>]*>(?P<label>.*?)</a>'
    r'(?:\s*(?P<date>\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}))?'
    r'(?:\s*(?P<size>-|[\d.]+[KMGTP]?))?',
    re.IGNORECASE | re.DOTALL)
_STAMP_RE = re.compile(r"-(?P<stamp>\d{12})(?:-(?P<kind>[A-Za-z0-9]+))?$")
_SIZE_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}


def human_size(num: float) -> str:
    """字节数转可读文本。"""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def parse_size(text: str) -> int:
    """解析 nginx autoindex 的大小列（`282M` / `-` / `1234`）。解析不出返回 0。"""
    raw = (text or "").strip()
    if not raw or raw == "-":
        return 0
    unit = raw[-1].upper()
    if unit in _SIZE_UNITS:
        try:
            return int(float(raw[:-1]) * _SIZE_UNITS[unit])
        except ValueError:
            return 0
    try:
        return int(raw)
    except ValueError:
        return 0


@dataclass(frozen=True)
class RemoteRelease:
    """远端一个版本包。"""

    name: str
    url: str
    size: int
    mtime: str
    stamp: str
    kind: str

    @property
    def dir_name(self) -> str:
        """解压后与之对应的目录名（归档名去后缀）。"""
        if self.name.endswith(ARCHIVE_SUFFIX):
            return self.name[: -len(ARCHIVE_SUFFIX)]
        return self.name

    @property
    def size_text(self) -> str:
        return human_size(self.size) if self.size else "—"

    @property
    def stamp_text(self) -> str:
        """`202609171012` → `09-17 10:12`；解析不出就退回列表里的原始日期。"""
        if len(self.stamp) == 12:
            return (f"{self.stamp[4:6]}-{self.stamp[6:8]} "
                    f"{self.stamp[8:10]}:{self.stamp[10:12]}")
        return self.mtime or "—"


@dataclass(frozen=True)
class LocalState:
    """某个远端版本在本地的状态。"""

    extracted: bool
    archive_bytes: int
    part_bytes: int
    is_current: bool
    workspace: Optional[Path] = None

    def label(self, release: Optional[RemoteRelease] = None) -> str:
        """给列表用的状态文案。"""
        if self.is_current:
            return "使用中"
        if self.extracted:
            return "已下载"
        if self.part_bytes:
            total = release.size if release else 0
            if total:
                return f"断点 {int(self.part_bytes * 100 / total)}% 可续传"
            return f"断点 {human_size(self.part_bytes)} 可续传"
        if self.archive_bytes:
            # 包下完了但没解开：上次可能卡在解压那一步（磁盘满/被取消），
            # 这时不该让用户以为要重下 290MB
            return "已下载包"
        return "未下载"


def parse_autoindex(html: str, base_url: str,
                    suffix: str = ARCHIVE_SUFFIX) -> List[RemoteRelease]:
    """解析 nginx autoindex 页面，列出匹配 `suffix` 的版本包（新的在前）。

    只认 `href` 本身，不解析页面上的标题或表头 —— autoindex 的 HTML 结构很土，
    但 `href` 是稳定契约；具体某天它换了主题，最多是少列几个包，不会认错包。
    """
    base = base_url if base_url.endswith("/") else base_url + "/"
    seen = set()
    releases: List[RemoteRelease] = []
    for match in _ROW_RE.finditer(html or ""):
        href = (match.group("href") or "").strip()
        if not href or href.startswith(("?", "#", "http")) or href.endswith("/"):
            continue
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        if not name.endswith(suffix) or name in seen:
            continue
        seen.add(name)

        stem = name[: -len(suffix)]
        stamp_match = _STAMP_RE.search(stem)
        stamp = stamp_match.group("stamp") if stamp_match else ""
        kind = (stamp_match.group("kind") or "") if stamp_match else ""
        releases.append(RemoteRelease(
            name=name,
            url=urllib.parse.urljoin(base, href),
            size=parse_size(match.group("size") or ""),
            mtime=" ".join((match.group("date") or "").split()),
            stamp=stamp,
            kind=kind or "正式",
        ))

    # 时间戳降序：列表第一行就是最新包，现场"默认选中最新"不用再筛
    releases.sort(key=lambda item: (item.stamp, item.name), reverse=True)
    return releases


def _looks_like_workspace(path: Path) -> bool:
    """判据与 ConfigManager.check_workspace 保持一致，避免"界面说能切、切过去不能用"。"""
    return (path / "run_nodes.sh").is_file() or (path / "install").is_dir()


def detect_workspace_root(path) -> Optional[Path]:
    """从解压出来的目录里认出工作空间根。

    两种包布局都要认：`<包名>/wheeled_robot/{install,...}`（现有包）与
    `<包名>/{install,...}`（顶层就是工作空间）。认不出返回 None —— 调用方
    应当据此拒绝自动切换，而不是把 workspace_root 指到一个不能用的目录上。
    """
    root = Path(path)
    if not root.is_dir():
        return None
    if _looks_like_workspace(root):
        return root
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return None
    candidates = [child for child in children if _looks_like_workspace(child)]
    return candidates[0] if len(candidates) == 1 else None


def _same_path(left: Path, right: Path) -> bool:
    """两个路径是不是同一个目录。

    现场存在软链（`/home/nerv/test_ws` 可能指向别处）与 `..` 混写，
    直接比字符串会漏判"这就是当前正在使用的工作空间"，于是把一个已经在用的
    目录当成"未下载"，用户再点一次下载就会撞上 TARGET_EXISTS。
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def inspect_local(release: RemoteRelease, download_dir, current_workspace=None) -> LocalState:
    """看某个远端版本在本地处于什么状态（列表里那一列"本地状态"）。"""
    root = Path(download_dir).expanduser()
    archive = root / release.name
    part = root / (release.name + PART_SUFFIX)
    extracted_dir = root / release.dir_name
    workspace = detect_workspace_root(extracted_dir) if extracted_dir.is_dir() else None

    current = Path(current_workspace).expanduser() if current_workspace else None
    is_current = bool(workspace and current and _same_path(workspace, current))
    return LocalState(
        extracted=workspace is not None,
        archive_bytes=archive.stat().st_size if archive.is_file() else 0,
        part_bytes=part.stat().st_size if part.is_file() else 0,
        is_current=is_current,
        workspace=workspace,
    )


def _free_bytes(path: Path) -> int:
    """查某个路径所在文件系统的可用空间。

    路径**可能还不存在**（解压前目标目录尚未创建），直接 `disk_usage` 会抛
    `No such file or directory` —— 于是"磁盘几乎满了"这个本该提前拦下的情况
    变成了一个莫名其妙的 IO 错误。这里向上找到最近的已存在祖先再问：
    同一文件系统内可用空间一致，结论不变。
    """
    probe = path
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
    except OSError:
        pass
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError as exc:
        raise AppError(ErrorCode.LOCAL_IO, f"无法读取 {probe} 的磁盘空间：{exc}") from exc


def _ensure_space(path: Path, needed: int, what: str) -> None:
    """空间不够就直接拦下。等到写一半才 ENOSPC，现场看到的是"下载到 90% 失败"。"""
    if needed <= 0:
        return
    free = _free_bytes(path)
    if free < needed:
        raise AppError(
            ErrorCode.LOCAL_SPACE,
            f"{path} 可用空间 {human_size(free)}，不足{what}所需的 {human_size(needed)}")


class ReleaseIndex:
    """远端版本目录（nginx autoindex）的只读客户端。"""

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout

    def fetch(self) -> List[RemoteRelease]:
        """拉取版本列表。失败抛 AppError，界面按错误码给归属与建议。"""
        request = urllib.request.Request(self.base_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            raise AppError(ErrorCode.REPO_UNREACHABLE,
                           f"版本服务器返回 HTTP {exc.code}：{self.base_url}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AppError(ErrorCode.REPO_UNREACHABLE,
                           f"无法连接版本服务器 {self.base_url}：{exc}") from exc

        releases = parse_autoindex(raw.decode(charset, errors="replace"), self.base_url)
        if not releases:
            raise AppError(
                ErrorCode.REPO_BAD_RESPONSE,
                f"在 {self.base_url} 没解析到任何版本包（确认 version_repo_url 指向的是"
                f"包目录而不是上层目录）")
        return releases


class ReleaseDownloader:
    """下载版本包到本地目录，支持断点续传与取消。"""

    #: autoindex 给出的大小精度只有 1 MiB（页面上显示成 `282M` / `291M`），拿它做
    #: 精确比对必然误判，所以只要求"本地归档不比远端小、且差距在 2 MiB 以内"。
    COMPLETE_TOLERANCE = 2 * 1024 * 1024

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def _archive_looks_complete(self, archive: Path, remote_size: int) -> bool:
        """本地已有归档且大小对得上（粗筛，不代替解压时的 bz2 CRC 校验）。"""
        if not remote_size or not archive.is_file():
            return False
        try:
            local = archive.stat().st_size
        except OSError:
            return False
        return 0 <= remote_size - local <= self.COMPLETE_TOLERANCE

    def download(self, release: RemoteRelease, download_dir,
                 on_progress: Optional[Progress] = None,
                 should_cancel: Optional[CancelCheck] = None) -> Path:
        """下到 `<download_dir>/<包名>`；中途失败保留 `.part` 供续传。

        Returns:
            落地的归档路径。

        Raises:
            AppError: `REPO_UNREACHABLE` 网络/HTTP 失败；`LOCAL_SPACE` 空间不足；
                `LOCAL_IO` 写盘失败；`CANCELLED` 用户取消；`INTERRUPTED` 字节数不符。
        """
        root = Path(download_dir).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(ErrorCode.LOCAL_IO,
                           f"无法创建下载目录 {root}：{exc}") from exc

        archive = root / release.name
        part = root / (release.name + PART_SUFFIX)
        _ensure_space(root, int(release.size * 1.05), f"下载 {release.name}")

        if self._archive_looks_complete(archive, release.size):
            # 上次已经下完、只是没解开（解压失败或用户中途取消）——再下一遍 290MB
            # 纯属浪费。完整性最终由解压时的 bz2 CRC 把关，这里只做粗筛。
            if on_progress is not None:
                size = archive.stat().st_size
                on_progress(size, size, "本地已有完整压缩包，跳过下载")
            return archive

        offset = part.stat().st_size if part.is_file() else 0
        request = urllib.request.Request(release.url, headers={"User-Agent": USER_AGENT})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise AppError(ErrorCode.REPO_UNREACHABLE,
                           f"下载 {release.name} 失败，服务器返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AppError(ErrorCode.REPO_UNREACHABLE,
                           f"下载 {release.name} 时无法连接版本服务器：{exc}") from exc

        with response:
            length = int(response.headers.get("Content-Length") or 0)
            if response.status == 206 and offset:
                written, total, mode = offset, offset + length, "ab"
            else:
                # 服务器不认 Range（200）：从零写。把 200 的响应体追加到已有内容后面，
                # 会得到大小正确、内容错乱的包 —— 而大小校验对此毫无察觉。
                written, total, mode = 0, length, "wb"
            try:
                with open(part, mode) as handle:
                    while True:
                        if should_cancel is not None and should_cancel():
                            raise AppError(
                                ErrorCode.CANCELLED,
                                f"已取消下载（已保留 {human_size(written)}，下次可续传）")
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        if on_progress is not None:
                            on_progress(written, total or written, "")
            except OSError as exc:
                code = ErrorCode.LOCAL_SPACE if exc.errno == errno.ENOSPC else ErrorCode.LOCAL_IO
                raise AppError(code, f"写入 {part} 失败：{exc}") from exc

        size = part.stat().st_size
        if total and size != total:
            raise AppError(
                ErrorCode.INTERRUPTED,
                f"{release.name} 下载不完整（{human_size(size)} / {human_size(total)}），"
                f"断点已保留，重试会从断点继续")
        # 原子落位：改名前文件不叫正式名，别的逻辑不会把它当"下载好了"
        os.replace(part, archive)
        return archive


def _validate_member(member: tarfile.TarInfo, dest_root: Path) -> None:
    """校验单个成员，越界或危险类型直接抛。

    tar 的成员名是**外部数据**，可以写成 `../../../etc/passwd` 或指向外部的软链。
    Python 3.12 的 `filter="data"` 能兜住这类问题，本项目要兼容 3.8+，只能自己来。
    """
    base = dest_root.resolve()
    name = member.name or ""
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise AppError(ErrorCode.PATH_REJECTED,
                       f"归档内路径越界，已中止解压：{name}")
    if member.isdev():
        raise AppError(ErrorCode.PATH_REJECTED,
                       f"归档内含设备文件，已中止解压：{name}")
    if member.issym() or member.islnk():
        link = Path(member.linkname or "")
        if link.is_absolute() or ".." in link.parts:
            raise AppError(ErrorCode.PATH_REJECTED,
                           f"归档内含指向外部的链接，已中止解压：{name} -> {member.linkname}")


class ReleaseExtractor:
    """把版本包解压成工作空间目录。"""

    def __init__(self, progress_every: int = 200):
        self.progress_every = progress_every

    def extract(self, archive, download_dir,
                on_progress: Optional[Progress] = None,
                should_cancel: Optional[CancelCheck] = None) -> Path:
        """解压到 `<download_dir>/<包名去后缀>/`，返回工作空间根。

        Raises:
            AppError: `TARGET_EXISTS` 目标目录已存在（不做任何改动）；
                `LOCAL_SPACE` 空间不足；`PATH_REJECTED` 归档内容危险；
                `LOCAL_IO` 解压失败；`CANCELLED` 用户取消。
        """
        archive = Path(archive)
        root = Path(download_dir).expanduser()
        dir_name = (archive.name[: -len(ARCHIVE_SUFFIX)]
                    if archive.name.endswith(ARCHIVE_SUFFIX) else archive.stem)
        target = root / dir_name
        if target.exists():
            raise AppError(
                ErrorCode.TARGET_EXISTS,
                f"目标目录已存在，未做任何改动：{target}\n"
                f"（若是正在使用的工作空间，请先切到别的版本，或把它改名/移走）")

        try:
            archive_size = archive.stat().st_size
        except OSError as exc:
            raise AppError(ErrorCode.NOT_FOUND, f"读不到归档 {archive}：{exc}") from exc
        _ensure_space(root, int(archive_size * EXTRACT_SPACE_FACTOR) + EXTRACT_SPACE_MARGIN,
                      f"解压 {dir_name}")

        # 解压到临时目录、成功后再改名：中途失败/取消不会留下一个"看着像能用"的半成品
        staging = root / STAGING_TMPL.format(name=dir_name)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.mkdir(parents=True)
        except OSError as exc:
            raise AppError(ErrorCode.LOCAL_IO, f"无法创建临时目录 {staging}：{exc}") from exc

        done = 0
        count = 0
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                while True:
                    if should_cancel is not None and should_cancel():
                        raise AppError(ErrorCode.CANCELLED, "已取消解压")
                    # 用 next() 逐个取，**不要用 getmembers()**：后者为了枚举成员会把
                    # 整条 bz2 流解压一遍（bz2 是块压缩，跳过数据也只能靠解压推进），
                    # 于是同一个包被解了两次 —— 实测 291MB 的包多花一倍时间
                    # （49.8s → 优化后见 README）。逐成员"校验完立刻解开"只需一遍。
                    member = tar.next()
                    if member is None:
                        break
                    _validate_member(member, staging)
                    try:
                        tar.extract(member, path=str(staging))
                    except OSError as exc:
                        code = (ErrorCode.LOCAL_SPACE if exc.errno == errno.ENOSPC
                                else ErrorCode.LOCAL_IO)
                        raise AppError(code, f"解压 {member.name} 失败：{exc}") from exc
                    done += member.size
                    count += 1
                    if on_progress is not None and count % self.progress_every == 0:
                        # tar.bz2 的总解压量无法预知（要读完整条压缩流才知道），
                        # 所以总量传 0：界面显示"已解压多少"而不是假的百分比
                        on_progress(done, 0, f"已解压 {count} 项")
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        if on_progress is not None:
            on_progress(done, done, f"已解压 {count} 项")
        found = detect_workspace_root(target)
        if found is None:
            raise AppError(
                ErrorCode.REPO_BAD_RESPONSE,
                f"{target} 里没找到工作空间（缺 install/ 或 run_nodes.sh），"
                f"包结构与预期不符，请手工确认")
        return found
