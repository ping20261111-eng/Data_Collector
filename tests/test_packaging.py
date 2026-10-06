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
