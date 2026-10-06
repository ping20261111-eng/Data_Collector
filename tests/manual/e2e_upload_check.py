"""真实服务器自检：验证「打包 + 上传」四种模式的端到端行为。

默认连到 configs/app_config.yaml 里的远端，只使用隐藏临时子目录 `.dc_selftest`，
逐个用例回传校验（zip/tar 结构与 sha256）后立即删除，不会污染已有数据。

用法：
    python3 tests/manual/e2e_upload_check.py            # 全部用例
    python3 tests/manual/e2e_upload_check.py --size 64  # 单个 64MB 的 bag

注意：这是需要真实网络与远端账号的手动自检，不参与 pytest 自动收集。
"""
import argparse
import hashlib
import random
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from data_collector.core import ConfigManager  # noqa: E402
from data_collector.core_data import DataUploader  # noqa: E402

SUBPATH = ".dc_selftest"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_bag(scratch: Path, name: str, size_mb: int, fill: bytes = None) -> Path:
    bag = scratch / name
    if bag.exists():
        shutil.rmtree(bag)
    bag.mkdir(parents=True)
    total = size_mb * 1024 * 1024
    with (bag / f"{name}_0.mcap").open("wb") as fp:
        if fill is None:
            rnd = random.Random(1234)
            remaining = total
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                fp.write(rnd.randbytes(chunk))
                remaining -= chunk
        else:
            fp.write((fill * (total // len(fill) + 1))[:total])
    (bag / "info.json").write_bytes(b'{"robot": "selftest"}')
    (bag / "metadata.yaml").write_bytes(b"selftest: true\n")
    return bag


def sha256_of(stream) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def fetch(uploader: DataUploader, remote_path: str, local_path: Path) -> Path:
    transport, sftp = uploader._connect_sftp()
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        transport.close()
    return local_path


def cleanup(uploader: DataUploader, remote_name: str):
    transport, sftp = uploader._connect_sftp()
    try:
        try:
            sftp.remove(f"{uploader.remote_base}/{SUBPATH}/{remote_name}")
        except Exception:
            pass
        try:
            sftp.rmdir(f"{uploader.remote_base}/{SUBPATH}")
        except Exception:
            pass
    finally:
        transport.close()


def run_case(cfg, scratch: Path, label: str, bag: Path, tune: dict, suffix: str):
    uploader = DataUploader(cfg)
    for key, value in tune.items():
        setattr(uploader, key, value)
    size_mb = sum(p.stat().st_size for p in bag.rglob("*") if p.is_file()) / 1024 / 1024
    percents = []
    t0 = time.time()
    ok = uploader.upload_bag_folder(bag, SUBPATH, progress_cb=log, progress_percent=percents.append)
    elapsed = max(time.time() - t0, 0.01)
    log(f"--- {label}: ok={ok} 数据量={size_mb:.1f}MB 耗时={elapsed:.1f}s "
        f"平均={size_mb / elapsed:.1f}MB/s err={uploader.last_error!r}")
    assert ok, f"{label} 上传失败"
    assert list(uploader.temp_dir.iterdir()) == [], "临时目录不应残留归档"
    remote_name = f"{bag.name}{suffix}"
    local = fetch(uploader, f"{uploader.remote_base}/{SUBPATH}/{remote_name}",
                  scratch / f"fetched{suffix}")
    log(f"    回传校验 {local.stat().st_size / 1024 / 1024:.2f}MB")
    cleanup(uploader, remote_name)
    log("    远端已清理")
    return local


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=12, help="每个 bag 的 MB 数")
    args = parser.parse_args()

    cfg = ConfigManager(ROOT / "configs" / "app_config.yaml")
    scratch = Path(tempfile.mkdtemp(prefix="dc_upload_selftest_"))
    log(f"远端 {cfg.get_remote_settings()['host']} / 临时目录 {scratch}")

    try:
        # 1) 默认：zip(不压缩) + 流式直传，并校验内容 sha256
        bag = make_bag(scratch, "bag_selftest_stream", args.size)
        local = run_case(cfg, scratch, "默认 zip(store) + 流式直传", bag, {}, ".zip")
        with zipfile.ZipFile(local) as z:
            assert z.testzip() is None
            names = sorted(z.namelist())
            assert names == sorted([f"{bag.name}/info.json", f"{bag.name}/metadata.yaml",
                                    f"{bag.name}/{bag.name}_0.mcap"]), names
            remote_hash = sha256_of(z.open(f"{bag.name}/{bag.name}_0.mcap"))
        local_hash = hashlib.sha256((bag / f"{bag.name}_0.mcap").read_bytes()).hexdigest()
        log(f"    sha256 一致={remote_hash == local_hash}")
        assert remote_hash == local_hash

        # 2) tar 容器
        bag = make_bag(scratch, "bag_selftest_tar", max(args.size // 2, 1))
        local = run_case(cfg, scratch, "tar + 流式直传", bag, {"transfer_format": "tar"}, ".tar")
        with tarfile.open(local, mode="r:") as tar:
            names = sorted(tar.getnames())
            assert names == sorted([bag.name, f"{bag.name}/info.json",
                                    f"{bag.name}/metadata.yaml",
                                    f"{bag.name}/{bag.name}_0.mcap"]), names

        # 3) 本地打包后上传（旧行为，兼容只开 SFTP 的远端）
        bag = make_bag(scratch, "bag_selftest_tmp", max(args.size // 2, 1))
        local = run_case(cfg, scratch, "zip + 本地打包后上传", bag,
                         {"transfer_mode": "tempfile"}, ".zip")
        with zipfile.ZipFile(local) as z:
            assert z.testzip() is None

        # 4) DEFLATE（文本类数据才有意义）
        bag = make_bag(scratch, "bag_selftest_defl", max(args.size // 2, 1),
                       fill=b"teleop-mcap-mock;")
        local = run_case(cfg, scratch, "zip + DEFLATE 流式直传", bag,
                         {"transfer_compression": "deflate"}, ".zip")
        raw = (args.size // 2 or 1) * 1024 * 1024
        log(f"    压缩后体积比={local.stat().st_size / raw:.3f}")

        print("全部通过 ✅", flush=True)
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
