"""版本下载的回归测试：列目录解析、断点续传、安全解压。

这些用例大多对应"现场踩过的坑"或"一旦错了很难发现"的点：
- autoindex 的大小列精度只有 1 MiB，拿它做精确比对必然误判；
- 服务器不认 Range（返回 200）时若把响应体追加到已有内容后，会得到一个
  字节数正确、内容错乱的包 —— 大小校验对此毫无察觉；
- tar 成员名是外部数据，可以写成 `../../..`，解压前必须逐条校验。
"""
import io
import tarfile
import urllib.error
from pathlib import Path

import pytest

from data_collector.core_release import (ARCHIVE_SUFFIX, PART_SUFFIX, RemoteRelease,
                                         ReleaseDownloader, ReleaseExtractor,
                                         ReleaseIndex, detect_workspace_root,
                                         human_size, inspect_local, parse_autoindex,
                                         parse_size)
from data_collector.errors import AppError, ErrorCode

INDEX_HTML = """<html>
<head><title>文件服务器 /file/wheeled_robot/</title></head>
<body><h1>文件服务器 /file/wheeled_robot/</h1><hr><pre><a href="../">../</a>
<a href="wheeled_robot_x86_64-v1.4.0-develop-202608171511.tar.bz2">wheeled_robot_x86_64-v1.4.0-develop-20260817151..&gt;</a> 17-Aug-2026 07:12    282M
<a href="wheeled_robot_x86_64-v1.4.0-develop-202609161641-fix.tar.bz2">cut..&gt;</a> 16-Sep-2026 08:42    291M
<a href="wheeled_robot_x86_64-v1.4.0-develop-202609151137-end2.tar.bz2">cut..&gt;</a> 15-Sep-2026 03:38    291M
<a href="notes.txt">notes.txt</a> 01-Jan-2020 00:00     1K
<a href="subdir/">subdir/</a> 01-Jan-2020 00:00     -
</pre><hr></body></html>"""


class _Headers(dict):
    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    """够用的 urlopen 返回值替身：支持分块 read 与 headers.get。"""

    def __init__(self, body: bytes, status: int = 200, length: int = None):
        self._body = body
        self.status = status
        self.headers = _Headers({"Content-Length": str(len(body) if length is None else length)})

    def read(self, size: int = -1):
        if not self._body:
            return b""
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, response, recorder=None):
    def fake_urlopen(request, timeout=None):
        if recorder is not None:
            recorder.append(request)
        return response

    monkeypatch.setattr("data_collector.core_release.urllib.request.urlopen", fake_urlopen)


def _release(size: int = 10, name: str = None) -> RemoteRelease:
    return RemoteRelease(
        name=name or ("pkg" + ARCHIVE_SUFFIX),
        url="http://host/file/wheeled_robot/" + (name or ("pkg" + ARCHIVE_SUFFIX)),
        size=size, mtime="01-Jan-2026 00:00", stamp="202601010000", kind="正式")


def _add_file(tar: tarfile.TarFile, name: str, data: bytes):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_symlink(tar: tarfile.TarFile, name: str, target: str):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    info.size = 0
    tar.addfile(info)


def _write_workspace_archive(tmp_path: Path, name: str = "pkg.tar.bz2",
                             prefix: str = "wheeled_robot") -> Path:
    """造一个最小可用包：`<top>/wheeled_robot/{run_nodes.sh,install/,doc/}`。"""
    archive = tmp_path / name
    with tarfile.open(archive, "w:bz2") as tar:
        for relative in ("run_nodes.sh", "install/lib/.keep", "doc/version.txt"):
            _add_file(tar, f"{prefix}/{relative}" if prefix else relative, b"x")
    return archive


# ---------- 列表解析 ----------
def test_parse_autoindex_only_archives_newest_first():
    releases = parse_autoindex(INDEX_HTML, "http://host/file/wheeled_robot/")

    assert [item.stamp for item in releases] == [
        "202609161641", "202609151137", "202608171511"]
    assert all(item.name.endswith(ARCHIVE_SUFFIX) for item in releases)
    assert releases[0].url == ("http://host/file/wheeled_robot/"
                               "wheeled_robot_x86_64-v1.4.0-develop-202609161641-fix.tar.bz2")


def test_parse_autoindex_reads_kind_size_and_display_fields():
    newest = parse_autoindex(INDEX_HTML, "http://host/")[0]

    assert newest.kind == "fix"
    assert newest.size == 291 * 1024 ** 2
    assert newest.stamp_text == "09-16 16:41"
    assert newest.dir_name == "wheeled_robot_x86_64-v1.4.0-develop-202609161641-fix"
    assert newest.size_text == "291.0 MB"


def test_parse_autoindex_marks_untagged_builds_as_official():
    oldest = parse_autoindex(INDEX_HTML, "http://host/")[-1]
    assert oldest.kind == "正式"          # 没有 -test/-end 后缀就是正式包


def test_parse_autoindex_base_url_without_slash_still_joins():
    releases = parse_autoindex(INDEX_HTML, "http://host/file/wheeled_robot")
    assert releases[0].url.startswith("http://host/file/wheeled_robot/")


@pytest.mark.parametrize("text,expected", [
    ("282M", 282 * 1024 ** 2),
    ("291M", 291 * 1024 ** 2),
    ("1K", 1024),
    ("1234", 1234),
    ("-", 0),
    ("", 0),
    ("junk", 0),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(305288138) == "291.1 MB"


# ---------- 远端客户端 ----------
def test_index_fetch_reports_unreachable(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("data_collector.core_release.urllib.request.urlopen", boom)

    with pytest.raises(AppError) as excinfo:
        ReleaseIndex("http://host/file/wheeled_robot/").fetch()

    assert excinfo.value.code is ErrorCode.REPO_UNREACHABLE
    assert "connection refused" in excinfo.value.message      # 原文保留，不翻译


def test_index_fetch_reports_bad_response(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResponse(b"<html><body>nothing</body></html>"))
    with pytest.raises(AppError) as excinfo:
        ReleaseIndex("http://host/file/wheeled_robot/").fetch()
    assert excinfo.value.code is ErrorCode.REPO_BAD_RESPONSE


def test_index_fetch_parses_page(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResponse(INDEX_HTML.encode("utf-8")))
    releases = ReleaseIndex("http://host/file/wheeled_robot").fetch()
    assert len(releases) == 3


# ---------- 工作空间识别 ----------
def test_detect_workspace_root_handles_both_layouts(tmp_path):
    nested = tmp_path / "pkg" / "wheeled_robot"
    (nested / "install").mkdir(parents=True)
    assert detect_workspace_root(tmp_path / "pkg") == nested

    flat = tmp_path / "flat"
    (flat / "run_nodes.sh").parent.mkdir(parents=True)
    (flat / "run_nodes.sh").write_text("#!/bin/bash\n")
    assert detect_workspace_root(flat) == flat

    assert detect_workspace_root(tmp_path / "missing") is None
    (tmp_path / "empty").mkdir()
    assert detect_workspace_root(tmp_path / "empty") is None


def test_inspect_local_walks_through_all_states(tmp_path):
    release = _release(size=100, name="pkg" + ARCHIVE_SUFFIX)
    root = tmp_path / "dl"
    root.mkdir()

    assert inspect_local(release, root).label(release) == "未下载"

    (root / (release.name + PART_SUFFIX)).write_bytes(b"x" * 50)
    assert inspect_local(release, root).label(release) == "断点 50% 可续传"

    (root / release.name).write_bytes(b"x" * 100)
    (root / (release.name + PART_SUFFIX)).unlink()
    assert inspect_local(release, root).label(release) == "已下载包"   # 只下了包，没解压

    workspace = root / "pkg" / "wheeled_robot"
    (workspace / "install").mkdir(parents=True)
    assert inspect_local(release, root).label(release) == "已下载"

    assert inspect_local(release, root, workspace).label(release) == "使用中"
    # 用 `..` / `.` 写出来的同一路径也要认出来，否则会把在用目录当成"未下载"
    assert inspect_local(release, root, root / "pkg" / "." / "wheeled_robot").is_current


# ---------- 下载 ----------
def test_download_writes_archive_atomically(tmp_path, monkeypatch):
    release = _release(size=10)
    progress = []
    _patch_urlopen(monkeypatch, _FakeResponse(b"0123456789"))

    archive = ReleaseDownloader().download(
        release, tmp_path, on_progress=lambda *args: progress.append(args))

    assert archive == tmp_path / release.name
    assert archive.read_bytes() == b"0123456789"
    assert not (tmp_path / (release.name + PART_SUFFIX)).exists()   # 改过名，不留残渣
    assert progress[-1][0] == progress[-1][1] == 10


def test_download_resumes_from_part(tmp_path, monkeypatch):
    release = _release(size=10)
    (tmp_path / (release.name + PART_SUFFIX)).write_bytes(b"01234")
    requests = []
    _patch_urlopen(monkeypatch, _FakeResponse(b"56789", status=206), requests)

    archive = ReleaseDownloader().download(release, tmp_path)

    assert archive.read_bytes() == b"0123456789"
    assert requests[0].get_header("Range") == "bytes=5-"


def test_download_restarts_when_server_ignores_range(tmp_path, monkeypatch):
    """服务器返回 200（不认 Range）时必须从零写。

    把 200 的响应体追加到已下载内容后面，会得到一个**大小正确、内容错乱**的包，
    而结尾的大小校验对此毫无察觉 —— 这是本条用例存在的唯一理由。
    """
    release = _release(size=10)
    (tmp_path / (release.name + PART_SUFFIX)).write_bytes(b"XXXXX")
    _patch_urlopen(monkeypatch, _FakeResponse(b"0123456789", status=200))

    archive = ReleaseDownloader().download(release, tmp_path)

    assert archive.read_bytes() == b"0123456789"


def test_download_detects_truncated_body_and_keeps_part(tmp_path, monkeypatch):
    release = _release(size=10)
    _patch_urlopen(monkeypatch, _FakeResponse(b"0123", length=10))    # 声明 10，只给 4

    with pytest.raises(AppError) as excinfo:
        ReleaseDownloader().download(release, tmp_path)

    assert excinfo.value.code is ErrorCode.INTERRUPTED
    assert (tmp_path / (release.name + PART_SUFFIX)).exists()          # 断点保留，可续传
    assert not (tmp_path / release.name).exists()                      # 没落地成正式名


def test_download_cancel_keeps_part(tmp_path, monkeypatch):
    release = _release(size=1_000_000)
    _patch_urlopen(monkeypatch, _FakeResponse(b"x" * 1000, length=1_000_000))
    calls = {"count": 0}

    def should_cancel():
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(AppError) as excinfo:
        ReleaseDownloader().download(release, tmp_path, should_cancel=should_cancel)

    assert excinfo.value.code is ErrorCode.CANCELLED
    assert (tmp_path / (release.name + PART_SUFFIX)).exists()


def test_download_refuses_when_disk_is_full(tmp_path, monkeypatch):
    release = _release(size=1_000_000)

    class _Usage:
        free = 10

    monkeypatch.setattr("data_collector.core_release.shutil.disk_usage",
                        lambda _path: _Usage())

    with pytest.raises(AppError) as excinfo:
        ReleaseDownloader().download(release, tmp_path)

    assert excinfo.value.code is ErrorCode.LOCAL_SPACE


def test_download_skips_when_archive_already_complete(tmp_path, monkeypatch):
    """上次下完但没解开（解压失败/取消）—— 不该再下一遍 290MB。"""
    release = _release(size=10)
    (tmp_path / release.name).write_bytes(b"0123456789")

    def boom(*args, **kwargs):
        pytest.fail("本地已有完整包时不该再发请求")

    monkeypatch.setattr("data_collector.core_release.urllib.request.urlopen", boom)

    archive = ReleaseDownloader().download(release, tmp_path)
    assert archive.read_bytes() == b"0123456789"


def test_download_retries_when_local_archive_is_short(tmp_path, monkeypatch):
    """本地归档明显偏小（上次下到一半）→ 不能当"下好了"，要走续传。"""
    release = _release(size=10 * 1024 * 1024)
    (tmp_path / release.name).write_bytes(b"x" * 1024)
    requests = []
    _patch_urlopen(monkeypatch, _FakeResponse(b"y" * (10 * 1024 * 1024)), requests)

    ReleaseDownloader().download(release, tmp_path)

    assert requests, "应当重新发起下载"
    assert (tmp_path / release.name).stat().st_size == 10 * 1024 * 1024


# ---------- 解压 ----------
def test_extract_returns_workspace_root(tmp_path):
    archive = _write_workspace_archive(tmp_path)
    download_dir = tmp_path / "dl"

    workspace = ReleaseExtractor().extract(archive, download_dir)

    assert workspace == download_dir / "pkg" / "wheeled_robot"
    assert (workspace / "run_nodes.sh").is_file()
    assert (archive).exists()          # 解压不动归档（删不删由调用方决定）


def test_extract_refuses_existing_target_without_touching_it(tmp_path):
    archive = _write_workspace_archive(tmp_path)
    download_dir = tmp_path / "dl"
    (download_dir / "pkg").mkdir(parents=True)
    (download_dir / "pkg" / "in-use.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        ReleaseExtractor().extract(archive, download_dir)

    assert excinfo.value.code is ErrorCode.TARGET_EXISTS
    assert (download_dir / "pkg" / "in-use.txt").read_text(encoding="utf-8") == "keep me"


def test_extract_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        _add_file(tar, "../escaped.txt", b"pwned")
    download_dir = tmp_path / "dl"

    with pytest.raises(AppError) as excinfo:
        ReleaseExtractor().extract(archive, download_dir)

    assert excinfo.value.code is ErrorCode.PATH_REJECTED
    assert not (tmp_path / "escaped.txt").exists()
    assert not list(download_dir.glob("*.extracting"))     # 临时目录也清掉了


def test_extract_rejects_symlink_pointing_outside(tmp_path):
    archive = tmp_path / "link.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        _add_symlink(tar, "wheeled_robot/evil", "../../../../etc/passwd")

    with pytest.raises(AppError) as excinfo:
        ReleaseExtractor().extract(archive, tmp_path / "dl")

    assert excinfo.value.code is ErrorCode.PATH_REJECTED


def test_extract_cancel_cleans_staging(tmp_path):
    archive = _write_workspace_archive(tmp_path)
    download_dir = tmp_path / "dl"

    with pytest.raises(AppError) as excinfo:
        ReleaseExtractor().extract(archive, download_dir, should_cancel=lambda: True)

    assert excinfo.value.code is ErrorCode.CANCELLED
    assert not (download_dir / "pkg").exists()
    assert not list(download_dir.glob("*.extracting"))


def test_extract_reports_unexpected_layout(tmp_path):
    archive = tmp_path / "weird.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        _add_file(tar, "readme.txt", b"nothing useful")

    with pytest.raises(AppError) as excinfo:
        ReleaseExtractor().extract(archive, tmp_path / "dl")

    assert excinfo.value.code is ErrorCode.REPO_BAD_RESPONSE
    assert "install" in excinfo.value.message


def test_extract_reports_progress_without_fake_percentage(tmp_path):
    """解压进度：总量传 0（tar.bz2 算不出总解压量），界面据此显示不确定进度。"""
    archive = _write_workspace_archive(tmp_path)
    seen = []

    ReleaseExtractor(progress_every=1).extract(
        archive, tmp_path / "dl", on_progress=lambda *args: seen.append(args))

    assert seen
    assert seen[-1][0] == seen[-1][1]      # 最后一次是"完成"，总量=已完成
    assert seen[-1][2].startswith("已解压")
