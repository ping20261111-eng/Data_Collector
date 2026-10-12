"""版本下载的回归测试：列目录解析、断点续传、安全解压。

这些用例大多对应"现场踩过的坑"或"一旦错了很难发现"的点：
- autoindex 的大小列精度只有 1 MiB，拿它做精确比对必然误判；
- 服务器不认 Range（返回 200）时若把响应体追加到已有内容后，会得到一个
  字节数正确、内容错乱的包 —— 大小校验对此毫无察觉；
- tar 成员名是外部数据，可以写成 `../../..`，解压前必须逐条校验。
"""
import calendar
import io
import tarfile
import time
import urllib.error
from pathlib import Path

import pytest

from data_collector.core_release import (ARCHIVE_SUFFIX, PART_SUFFIX, RemoteRelease,
                                         ReleaseDownloader, ReleaseExtractor,
                                         ReleaseIndex, detect_workspace_root,
                                         human_size, inspect_local, mtime_epoch,
                                         parse_autoindex, parse_size, release_epoch)
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
    """够用的取版本响应替身：支持分块 read 与 headers.get。"""

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


def _patch_direct(monkeypatch, response, recorder=None):
    """顶掉取版本的**统一出口** `core_release.open_direct`。

    这个接缝是 v6.4.10 从 `urllib.request.urlopen` 换过来的：为了让内网流量
    不被系统代理劫持（见 `open_direct` 的注释），取列表与下载都改走自建的
    **无代理** opener。桩必须跟着换 —— 继续打 `urlopen` 会打空，
    用例就会真去连 `http://host/...`。
    """
    def fake_direct(request, timeout=None):
        if recorder is not None:
            recorder.append(request)
        return response

    monkeypatch.setattr("data_collector.core_release.open_direct", fake_direct)


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


# ---------- 排序：按时间（v6.4.12 现场） ----------
def _row(name: str, date: str, size: str = "291M") -> str:
    """造一行 autoindex。**独立于 INDEX_HTML** —— 排序这类用例要能自成一体。"""
    return f'<a href="{name}">{name[:38]}..&gt;</a> {date}    {size}\n'


def test_a_long_suffix_after_the_stamp_does_not_sink_the_package():
    """时间戳后面跟**多段后缀**（`-fix_zyf1010_trunklimit`）时，包不能沉到列表最后。

    现场（v6.4.12）：今天刚传的两个包叫 `…-202610121624-fix_zyf1010_trunklimit`，
    而旧正则只容许时间戳后跟 `[A-Za-z0-9]+` **一段** —— 下划线让它整条匹配不上 →
    `stamp` 变空串 → 被当成最旧的排到最后（"今天的新包跑到去年那些下面"）。
    """
    html = (
        _row("wheeled_robot_x86_64-v1.4.0-develop-202608171511.tar.bz2",
             "17-Aug-2026 07:12")
        + _row("wheeled_robot_x86_64-v1.4.0-develop-202610121624"
               "-fix_zyf1010_trunklimit.tar.bz2", "12-Oct-2026 08:25")
        + _row("wheeled_robot_x86_64-v1.4.0-develop-202610121540"
               "-fix_zyf1010_trunklimit.tar.bz2", "12-Oct-2026 07:42"))
    releases = parse_autoindex(html, "http://host/")

    assert [r.stamp for r in releases] == [
        "202610121624", "202610121540", "202608171511"], \
        "带长后缀的包没被解析出时间戳（于是沉底、看起来最旧）"
    assert releases[0].kind == "fix", "类型只取时间戳后的**第一段**，人名/主题不参与分类"
    assert releases[0].stamp_text == "10-12 16:24", "显示列也要跟着正确"


def test_a_name_without_any_stamp_falls_back_to_the_server_time():
    """名字里没有 12 位时间戳的包按**服务器时间列**落位，不是一律排到最后。

    旧实现直接拿 `stamp` 字符串当排序键，空串最小 → 这类包永远沉底。
    """
    html = (
        _row("wheeled_robot_x86_64-v1.4.0-develop-202608171511.tar.bz2",
             "17-Aug-2026 07:12")
        + _row("wheeled_robot_x86_64-v1.4.0-develop.tar.bz2", "12-Oct-2026 08:25"))
    releases = parse_autoindex(html, "http://host/")

    assert releases[0].name.endswith("-develop.tar.bz2"), \
        "没有时间戳的包应当按服务器时间排到前面，而不是沉底"
    assert releases[0].stamp == ""
    assert releases[0].stamp_text == "12-Oct-2026 08:25", "没有时间戳时显示列退回服务器时间"


def test_releases_are_sorted_by_time_not_by_name():
    """排序键是**时间**，不是名字串 —— 名字字母序与时间序相反时按时间排。"""
    html = (
        _row("zzz_pkg-202601010900.tar.bz2", "01-Jan-2026 01:00")
        + _row("aaa_pkg-202612310900.tar.bz2", "31-Dec-2026 01:00"))
    releases = parse_autoindex(html, "http://host/")

    assert [r.stamp for r in releases] == ["202612310900", "202601010900"]


def test_the_server_time_column_is_read_as_gmt_without_the_locale():
    """时间列是 **GMT**（HTTP 头的 `Date` 也是 GMT），且**不许依赖 LC_TIME**。

    为什么不能 `time.strptime(..., "%d-%b-%Y %H:%M")`：`%b` 按区域设置认月份缩写，
    机器人机 / 开发机的 locale 不是英文时这一列会**静默解析成 0** —— 而 0 在排序里
    就等于"最旧"，表现又是"最新的包沉到底部"（同一个症状、另一个成因）。
    """
    expected = calendar.timegm((2026, 10, 12, 8, 25, 0))
    assert mtime_epoch("12-Oct-2026 08:25") == expected
    assert mtime_epoch("12-OCT-2026 08:25") == expected      # 大小写都认
    assert mtime_epoch("12-Okt-2026 08:25") == 0             # 认不出的缩写 → 0，不乱猜
    assert mtime_epoch("") == 0


def test_a_stamp_is_read_in_local_time_while_the_column_is_utc():
    """两个时间来源的时基不同，必须各自按正确的时基解释（搞混就是整体错 8 小时）。

    时间戳 = **构建时的本机时间**（现场 +08:00）；时间列 = GMT。混排（带时间戳的包与
    只带时间列的包排在一起）时，错 8 小时会让顺序错位 —— 而这类错位不会被任何
    告警暴露，只能靠这条守卫。
    """
    stamp = "202610120900"
    fields = (2026, 10, 12, 9, 0, 0)
    entry = RemoteRelease(name=f"pkg-{stamp}{ARCHIVE_SUFFIX}", url="http://host/p",
                          size=0, mtime="12-Oct-2026 01:00", stamp=stamp, kind="正式")

    assert release_epoch(entry) == int(time.mktime(time.strptime(stamp, "%Y%m%d%H%M")))
    if time.timezone or time.altzone:
        # 非 UTC 机器上两种解释必然不同 —— 正好用来钉住"用的是本机时区"这一条。
        # UTC 机器上这条差异不可观测，所以只在非 UTC 时断言（不做假绿）。
        assert release_epoch(entry) != calendar.timegm(fields)


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

    monkeypatch.setattr("data_collector.core_release.open_direct", boom)

    with pytest.raises(AppError) as excinfo:
        ReleaseIndex("http://host/file/wheeled_robot/").fetch()

    assert excinfo.value.code is ErrorCode.REPO_UNREACHABLE
    assert "connection refused" in excinfo.value.message      # 原文保留，不翻译


def test_index_fetch_reports_bad_response(monkeypatch):
    _patch_direct(monkeypatch, _FakeResponse(b"<html><body>nothing</body></html>"))
    with pytest.raises(AppError) as excinfo:
        ReleaseIndex("http://host/file/wheeled_robot/").fetch()
    assert excinfo.value.code is ErrorCode.REPO_BAD_RESPONSE


def test_index_fetch_parses_page(monkeypatch):
    _patch_direct(monkeypatch, _FakeResponse(INDEX_HTML.encode("utf-8")))
    releases = ReleaseIndex("http://host/file/wheeled_robot").fetch()
    assert len(releases) == 3


def test_version_fetch_ignores_the_environment_proxy(monkeypatch):
    """取版本要**直连**，不被 `http_proxy` 劫持（v6.4.10 现场问题）。

    现场复现：启动它的那个 shell 里带着 `http_proxy=http://127.0.0.1:7897/`，
    `no_proxy=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,::1`
    —— 而 urllib 的 `no_proxy` **只认主机名与域名后缀、不认 CIDR**：

        proxy_bypass("172.16.10.2")  →  False      # 明明落在 172.16.0.0/12 里

    于是这个纯内网请求被送去本机代理，代理没开 → 立刻 `Connection refused`；
    界面上就是"看不到远端版本"。浏览器不吃这几个环境变量，所以
    "浏览器能打开、程序看不到"这个对比特别容易把人带到"服务器坏了"上去。

    这条用一个**真在跑**的本机 HTTP 服务 + 一个**死的**代理地址来钉：
    只要哪天又退回 `urllib.request.urlopen`，请求就会去连那个死代理并失败。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):                                # noqa: N802 (HTTP 命名)
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):                   # 用例里别往 stderr 刷日志
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 死代理：端口 1 上没有任何东西在听
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1/")
    # `no_proxy` 清空：否则本机地址可能正好被放过，这条用例就白跑了
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        url = f"http://127.0.0.1:{server.server_port}/wheeled_robot/"
        releases = ReleaseIndex(url, timeout=5).fetch()
    finally:
        server.shutdown()
        server.server_close()

    assert len(releases) == 3, "环境里挂着代理时，取版本必须仍然直连到仓库"


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
    _patch_direct(monkeypatch, _FakeResponse(b"0123456789"))

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
    _patch_direct(monkeypatch, _FakeResponse(b"56789", status=206), requests)

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
    _patch_direct(monkeypatch, _FakeResponse(b"0123456789", status=200))

    archive = ReleaseDownloader().download(release, tmp_path)

    assert archive.read_bytes() == b"0123456789"


def test_download_detects_truncated_body_and_keeps_part(tmp_path, monkeypatch):
    release = _release(size=10)
    _patch_direct(monkeypatch, _FakeResponse(b"0123", length=10))    # 声明 10，只给 4

    with pytest.raises(AppError) as excinfo:
        ReleaseDownloader().download(release, tmp_path)

    assert excinfo.value.code is ErrorCode.INTERRUPTED
    assert (tmp_path / (release.name + PART_SUFFIX)).exists()          # 断点保留，可续传
    assert not (tmp_path / release.name).exists()                      # 没落地成正式名


def test_download_cancel_keeps_part(tmp_path, monkeypatch):
    release = _release(size=1_000_000)
    _patch_direct(monkeypatch, _FakeResponse(b"x" * 1000, length=1_000_000))
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

    monkeypatch.setattr("data_collector.core_release.open_direct", boom)

    archive = ReleaseDownloader().download(release, tmp_path)
    assert archive.read_bytes() == b"0123456789"


def test_download_retries_when_local_archive_is_short(tmp_path, monkeypatch):
    """本地归档明显偏小（上次下到一半）→ 不能当"下好了"，要走续传。"""
    release = _release(size=10 * 1024 * 1024)
    (tmp_path / release.name).write_bytes(b"x" * 1024)
    requests = []
    _patch_direct(monkeypatch, _FakeResponse(b"y" * (10 * 1024 * 1024)), requests)

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
