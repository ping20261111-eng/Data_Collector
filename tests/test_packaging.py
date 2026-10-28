"""打包规矩的回归测试：**包里不许有 logs 内容**。

现场要求（原话）："以后打包把 logs 里面的东西删掉了再打包 —— 包括 data_collector/logs"。
这条规矩曾经漏掉一半：`LogFetcher` 的日志目录被写成了包目录 `data_collector/logs/`，
于是"打包前删 logs"只删到根目录那一份，301 个运行日志照样躺在包里被打进交付包。

把规矩写成用例，比写在文档里可靠：以后谁改打包脚本、谁又往包里带日志，CI 会直接红。
"""
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACK_SCRIPT = PROJECT_ROOT / "pack.sh"


def _read_version() -> str:
    text = (PROJECT_ROOT / "data_collector" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise AssertionError("读不到 __version__")


@pytest.mark.skipif(not PACK_SCRIPT.exists(), reason="没有 pack.sh")
def test_pack_script_excludes_logs(tmp_path):
    """跑一次真实打包：产出包里不得有任何 logs 下的文件，且关键文件齐全。"""
    if not shutil.which("bash") or not shutil.which("tar"):
        pytest.skip("环境缺少 bash/tar")

    completed = subprocess.run(["bash", str(PACK_SCRIPT), str(tmp_path)],
                               capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    tarballs = list(tmp_path.glob("Data_Collector_v*.tar.gz"))
    assert len(tarballs) == 1, f"应产出恰好一个包，实际 {tarballs}"
    tarball = tarballs[0]
    assert _read_version() in tarball.name          # 文件名带版本

    with tarfile.open(tarball) as archive:
        names = archive.getnames()

    log_files = [n for n in names if "/logs/" in n and not n.endswith("/logs/")]
    assert log_files == [], f"包里仍有 logs 内容：{log_files[:10]}"
    # 包目录下那个错误的 logs 也不许出现（根因已修，不该再被带进来）
    assert not any(n.startswith("Data_Collector/data_collector/logs") for n in names)

    # 关键文件必须在：打包脚本没把源码漏掉（上一次误删 Launcher 的教训）
    for required in ("Data_Collector/run.sh",
                     "Data_Collector/pack.sh",
                     "Data_Collector/data_collector/core_operations.py",
                     "Data_Collector/data_collector/ui/main_window.py",
                     "Data_Collector/tests/conftest.py",
                     "Data_Collector/configs/app_config.example.yaml"):
        assert required in names, f"包内缺少 {required}"

    # 不带版本控制与缓存
    for forbidden in (".git/", "__pycache__/", ".pytest_cache/", ".ruff_cache/"):
        assert not any(forbidden in n for n in names), f"包里混进了 {forbidden}"

    # 每台机器都不同的运行时文件，一个都不许随包分发。
    # 这三条曾经只点名了前两个：`configs/user_prefs.yaml` **漏排了**，
    # v6.9.2 的包里实测躺着开发机那份（teleop_user: yyt）—— 解包就顶掉现场的选择，
    # 而且完全不报错。工作区里这个文件是真实存在的（就是上面的 yyt），
    # 所以这条断言在修好之前**会真的红**，不是空转。
    for runtime_file in ("Data_Collector/configs/user_prefs.yaml",
                         "Data_Collector/configs/point_labels.yaml",
                         "Data_Collector/configs/app_config.yaml"):
        assert runtime_file not in names, (
            f"包里带上了每台机器各不相同的 {runtime_file} —— "
            f"现场解包会顶掉自己那份，且不会有任何报错")


# ---------- 自检本身：别让它退回"管道 + grep -q"（v6.5.0） ----------
def test_pack_selfchecks_do_not_read_the_listing_through_a_pipe():
    """自检**不许**写成 `tar -tzf "$TARBALL" | grep -q ...`。

    这是实测踩出来的坑，不是洁癖：`grep -q` 一找到就退出，tar 立刻收到 SIGPIPE
    并**以 2 退出**；pack.sh 开头有 `set -o pipefail`，管道状态就成了 2 ——
    于是 `if tar | grep -q ...; then` 在**命中时反而判假**。同一个包、同一行模式实测：

        无 pipefail                → 0 0 0 0 0（命中）
        有 pipefail + 管道 + -q     → 2 2 2 2 2（也"命中"，但状态是 2）
        先取清单再 grep（无管道）    → 0 0 0（命中）

    也就是说下面几道"禁止项"防线（logs / point_labels / app_config / 副本齐全）
    此前是**时好时坏**的：包里真带了它们也不一定报出来。改成先
    `LIST="$(tar -tzf "$TARBALL")"` 再在 $LIST 上查，这条不许退回去。
    """
    text = PACK_SCRIPT.read_text(encoding="utf-8")
    bad = [line.strip() for line in text.splitlines()
           if "tar -tzf" in line and "| grep" in line and not line.strip().startswith("#")]
    assert bad == [], f"自检又用管道读清单了（会因 SIGPIPE 变成时好时坏）：{bad}"
    assert 'LIST="$(tar -tzf "$TARBALL")"' in text, "清单应当只取一次再在它上面查"


def test_both_export_paths_name_the_runtime_config_files():
    """**两套导出方式各自点名**：`pack.sh`（tar 复制）与 `.gitattributes`（git archive）。

    这是 v6.9.3 踩出来的：`.gitattributes` 里早就写了
    `configs/user_prefs.yaml export-ignore`，于是"导出时会排除它"看起来已经成立 ——
    但 `export-ignore` **只对 `git archive` 生效**，而 `pack.sh` 走的是
    `tar --exclude` 复制。两套规矩互不生效，谁也没覆盖谁，结果包里就带上了它
    （实测 v6.9.2 的包：`teleop_user: yyt`）。

    所以这条守卫同时查两处：只要有一处漏了就会红 —— 别再指望"另一处已经写了"。
    """
    runtime_files = ("configs/user_prefs.yaml",
                     "configs/point_labels.yaml",
                     "configs/app_config.yaml")

    pack_text = PACK_SCRIPT.read_text(encoding="utf-8")
    for name in runtime_files:
        assert f"--exclude='{name}'" in pack_text, (
            f"pack.sh 的 tar 复制清单里少了 --exclude='{name}' —— "
            f"`.gitattributes` 的 export-ignore 管不到 tar 复制这条路")

    attrs = (PROJECT_ROOT / ".gitattributes")
    attrs_text = attrs.read_text(encoding="utf-8") if attrs.exists() else ""
    for name in ("configs/user_prefs.yaml", "configs/point_labels.yaml"):
        assert f"{name} export-ignore" in attrs_text, (
            f".gitattributes 里少了 `{name} export-ignore` —— "
            f"`git archive` 导出（不经 pack.sh）就会把带上")


def test_pack_requires_our_record_script_copies(tmp_path):
    """副本缺失必须让打包**失败**（规矩 6）。

    少了它们不会立刻报错，只会让起录制**静默回落**到机器侧那份脚本 —— 于是
    "重刷后退回硬编码 → 落盘位置失效"这个坑又回来了，现场看到的仍是一句
    "脚本启动后立即退出"。所以这道自检不能漏。

    用一个**最小项目**跑 pack.sh：`PROJECT_ROOT` 是按脚本自身位置推导的，
    所以把 pack.sh 复制到临时目录，它就会从那儿当项目根 —— 正好能造出"没有副本"的世界。
    """
    if not shutil.which("bash") or not shutil.which("tar"):
        pytest.skip("环境缺少 bash/tar")

    project = tmp_path / "mini_project"
    (project / "data_collector").mkdir(parents=True)
    (project / "data_collector" / "__init__.py").write_text(
        '__version__ = "0.0.1"\n', encoding="utf-8")
    shutil.copy(PACK_SCRIPT, project / "pack.sh")

    completed = subprocess.run(["bash", str(project / "pack.sh"), str(tmp_path / "out")],
                               capture_output=True, text=True, timeout=300)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, f"缺副本时应当打包失败：\n{output}"
    assert "包里缺 scripts/record_vla.sh" in output
    assert "静默回落" in output, "失败提示要说清后果（会静默回落到机器侧那份）"
    assert not list((tmp_path / "out").glob("*.tar.gz")), "失败时不该留下包"


def test_the_shipped_config_example_parses_into_numbers():
    """交付包里那份**配置样例**的数值字段必须真能解析成数字。

    这条是被**冷启动验证**逮到的：`app_config.example.yaml` 里有一行
    `warn_file_bytes: 3800000000# 超过这个大小的文件会提醒` —— 数字与 `#` 之间
    少一个空格，YAML 会把注释当值的一部分，于是解析出来是字符串
    `"3800000000# 超过…"`，`int()` 直接抛
    `invalid literal for int() with base 10`。

    后果不是"读不到那个字段"而是**首启就崩**（未捕获异常）：
    程序首启会从这份样例生成 `app_config.yaml`，于是**每台新机器第一次打开
    都在启动阶段挂掉**。开发机没事是因为它的 `app_config.yaml` 早于这次损坏
    就已经生成好了 —— 坏的那份只在新机器上生效。

    守卫写法不用正则去猜 YAML 语法（"数字紧跟 #" 只是一例，别的粘连形式还有），
    直接**把每个数值型字段 parse 一次**：任何粘连都会在这里变成类型不对。
    """
    import yaml

    example = PROJECT_ROOT / "configs" / "app_config.example.yaml"
    assert example.is_file(), f"配置样例不在包里：{example}"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))

    disk = data.get("disk_export", {})
    assert isinstance(disk.get("warn_file_bytes"), int), (
        f"warn_file_bytes 解析成了 {disk.get('warn_file_bytes')!r}"
        f"（多半是数字与行尾 # 之间少了空格）")
    assert isinstance(disk.get("min_free_margin_mb"), int), (
        f"min_free_margin_mb 解析成了 {disk.get('min_free_margin_mb')!r}")
    assert disk.get("mount_point") == "", (
        "样例里 mount_point 应当留空（靠自动识别），写了具体路径等于把"
        "某一台机器的挂载点当成通用配置发出去")

def test_the_shipped_config_example_points_at_the_version_repo():
    """交付包里的**配置样例**必须预填版本仓库地址（v6.5.4）。

    为什么值得钉一条：`app.py` 首启是 `shutil.copy(example, app_config.yaml)` ——
    **样例里这一项是什么，新机器首启后就是什么**。实测（2026-10-15）：样例里原本是
    空串，于是每台新机器首启后「版本下载」都是"未启用"，而当时的启动自检里又
    没有这一项 → 现场报"看不到服务器上的版本"时，日志与界面都给不出线索。

    这个地址是**团队共用的内网文件服务器**（对所有机器同一个值），所以适合做默认值；
    不接它的部署把这一项清空即可（留空 = 功能不启用，自检会标一条提醒）。
    """
    import yaml

    example = PROJECT_ROOT / "configs" / "app_config.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    url = str(data.get("version_repo_url") or "").strip()

    assert url, ("样例里 version_repo_url 不能留空 —— 首启是逐字复制这份样例，"
                 "留空等于让每台新机器的版本下载都「未启用」")
    assert url.startswith(("http://", "https://")), f"应当是个地址：{url}"
    assert url.endswith("/"), f"目录地址末尾要有 /：{url}"
    assert not url.rstrip("/").endswith("/file"), (
        f"指向的是上层目录而不是包目录：{url} —— 实测这个地址也能连上（200），"
        f"但那里没有 tar.bz2，界面只会说「没解析到任何版本包」")
