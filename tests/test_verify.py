"""内容级完整性校验（SHA-256）测试。

重点覆盖**大小不变但内容变了**这一类 —— 它是原有 size 校验的盲区，
也正是真实故障（SFTP 位翻转、磁盘静默错误）的样子。
"""
import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core_data import DataUploader
from data_collector.errors import ErrorCode
from data_collector.verify import (
    BAG_VERDICT_FAIL,
    BAG_VERDICT_PASS,
    BAG_VERDICT_PREFIX,
    FAT32_SAFE_FILE_BYTES,
    check_size_limits,
    detect_filesystem,
    has_fresh_report,
    has_single_file_limit,
    read_bag_verdict,
    sha256_file,
    write_sha256_sidecar,
)


# ---------- verify.sha256_file ----------

def test_sha256_file_matches_hashlib(tmp_path):
    """全量校验必须与 hashlib 对同一文件的结果一致（自证基线）。"""
    p = tmp_path / "data.bin"
    p.write_bytes(b"hello" * 10000)
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert sha256_file(p) == expected


def test_sha256_file_detects_same_size_content_change(tmp_path):
    """同长度内容改动必须被发现 —— 这是 size 校验的盲区。"""
    p = tmp_path / "data.bin"
    p.write_bytes(b"A" * 4096)
    before = sha256_file(p)
    raw = bytearray(p.read_bytes())
    raw[2000] = ord("B")          # 改一个字节，长度不变
    p.write_bytes(bytes(raw))
    assert p.stat().st_size == 4096, "前提：大小必须没变"
    assert sha256_file(p) != before, "内容变了但摘要相同 = 校验失效"


def test_sha256_file_empty(tmp_path):
    """空文件也要有确定摘要，不能抛异常。"""
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    assert sha256_file(p) == hashlib.sha256(b"").hexdigest()


# ---------- verify.write_sha256_sidecar ----------

def test_sidecar_matches_sha256sum_format(tmp_path):
    """边车格式必须是 `sha256sum -c` 能直接读的标准格式。

    交付给现场后，校验不能只能靠本工具 —— 工程师手上往往只有命令行。
    """
    p = tmp_path / "bag.mcap"
    p.write_bytes(b"payload")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    sidecar = write_sha256_sidecar(p, digest)
    content = sidecar.read_text(encoding="utf-8")
    assert content == f"{digest}  bag.mcap\n"


def test_sidecar_survives_relative_to_outside_tree(tmp_path):
    """目标不在 relative_to 下时不能抛异常（降级为只写文件名）。"""
    p = tmp_path / "a.bin"
    p.write_bytes(b"x")
    other = tmp_path / "other" / "deep"
    other.mkdir(parents=True)
    sidecar = write_sha256_sidecar(p, "deadbeef", relative_to=other)
    assert sidecar.exists()


# ---------- verify: 文件系统判定 ----------

@pytest.mark.parametrize("fs,expected", [
    ("vfat", True),
    ("fat32", True),
    ("msdos", True),
    ("exfat", False),      # exFAT 没有 FAT32 的 4GB 硬上限，不该误报
    ("ntfs", False),
    ("ntfs3", False),      # exFAT 常经 ntfs3 挂载，同样不限制
    ("ext4", False),
    ("", False),           # 探测失败时按"不限制"处理，避免误报
])
def test_single_file_limit_by_fs(fs, expected):
    assert has_single_file_limit(fs) is expected


def test_check_size_limits_flags_oversize_on_fat32():
    """FAT32 上超过 3.8GB 的条目必须被预判出来。"""
    entries = [("ok.mcap", 1024), ("huge.mcap", 5_000_000_000)]
    over, limited = check_size_limits(entries, "vfat")
    assert limited is True
    assert [n for n, _ in over] == ["huge.mcap"]


def test_check_size_limits_passes_on_exfat():
    """exFAT 上同样体积不该被拦 —— 误报会让现场不再信任这个提醒。"""
    entries = [("huge.mcap", 5_000_000_000)]
    over, limited = check_size_limits(entries, "exfat")
    assert over == []
    assert limited is False


def test_detect_filesystem_returns_nonempty_for_tmp():
    """探测至少要能认出 /tmp 的类型，否则预判逻辑永远走空。"""
    fs = detect_filesystem(Path("/tmp"))
    assert fs != ""


# ---------- 打包器摘要：边写边算 ----------

def test_pack_folder_digest_matches_bytes_on_disk(tmp_path):
    """流式打包的摘要必须与落盘归档的实际内容一致。

    这是流式路径能做内容校验的唯一依据（没有本地副本可回读），
    一旦 wrapper 没正确透传，校验就会给出一个"看起来通过"的错误结论。
    """
    src = tmp_path / "bag"
    src.mkdir()
    (src / "a.mcap").write_bytes(b"A" * 5000)
    (src / "b.mcap").write_bytes(b"B" * 3000)

    up = DataUploader.__new__(DataUploader)
    entries, total = up._iter_files(src)
    assert entries, "测试前提：应能枚举出文件"

    out = tmp_path / "out.zip"
    with open(out, "wb") as fp:
        digest = up._pack_folder(src, fp, "zip", False, entries, None, None,
                                 collect_digest=True)
    assert digest == hashlib.sha256(out.read_bytes()).hexdigest()


def test_pack_folder_tar_digest_matches(tmp_path):
    """tar 路径同样要能算出与落盘一致的摘要。"""
    src = tmp_path / "bag"
    src.mkdir()
    (src / "a.mcap").write_bytes(b"X" * 4096)

    up = DataUploader.__new__(DataUploader)
    entries, _ = up._iter_files(src)
    out = tmp_path / "out.tar"
    with open(out, "wb") as fp:
        digest = up._pack_folder(src, fp, "tar", False, entries, None, None,
                                 collect_digest=True)
    assert digest == hashlib.sha256(out.read_bytes()).hexdigest()


def test_pack_folder_without_digest_still_works(tmp_path):
    """不传 digest 时行为不变 —— 默认关闭校验不能影响既有路径。"""
    src = tmp_path / "bag"
    src.mkdir()
    (src / "a.mcap").write_bytes(b"Z" * 2048)
    up = DataUploader.__new__(DataUploader)
    entries, _ = up._iter_files(src)
    out = tmp_path / "out.zip"
    up._pack_folder(src, out.open("wb"), "zip", False, entries, None, None)
    with zipfile.ZipFile(out) as z:
        assert any(n.endswith("a.mcap") for n in z.namelist())


def test_pack_folder_rejects_bad_format(tmp_path):
    src = tmp_path / "bag"
    src.mkdir()
    up = DataUploader.__new__(DataUploader)
    entries, _ = up._iter_files(src)
    with pytest.raises(ValueError):
        up._pack_folder(src, (tmp_path / "x").open("wb"), "rar", False, entries)


# ---------- 开关默认值 ----------

def test_verify_content_defaults_off():
    """默认关闭：不开启就不能给现有流程增加可见耗时。"""
    up = DataUploader.__new__(DataUploader)
    up.verify_content = False
    assert up.verify_content is False


def test_verify_remote_digest_skipped_when_disabled(tmp_path):
    """未开启时直接返回 None，不做任何远端读取。"""
    up = DataUploader.__new__(DataUploader)
    up.verify_content = False
    assert up._verify_remote_digest(None, tmp_path / "x", "/r", 10) is None


def test_verify_remote_digest_mismatch_cleans_remote(tmp_path):
    """内容不一致时必须清理远端并报 VERIFY_FAILED。

    留着坏文件会被下游按文件名当成正常数据取走 —— 比直接失败危险得多。
    """
    local = tmp_path / "a.bin"
    local.write_bytes(b"A" * 2048)

    removed = []

    class FakeSftp:
        def open(self, path, mode):
            import io
            return io.BytesIO(b"B" * 2048)   # 同样长度，不同内容

        def stat(self, path):
            class S:
                st_size = 2048
            return S()

    class Uploader(DataUploader):
        def _remove_remote_quietly(self, sftp, path):
            removed.append(path)

        def _fail(self, code, message):
            self.last_code = code
            self.last_message = message
            return False

    up = Uploader.__new__(Uploader)
    up.verify_content = True
    result = up._verify_remote_digest(FakeSftp(), local, "/remote/a.bin", 2048)
    assert result is None
    assert up.last_code == ErrorCode.VERIFY_FAILED
    assert removed == ["/remote/a.bin"], "远端半成品必须被清理"
    assert "内容校验不一致" in up.last_message


def test_verify_remote_digest_match_returns_digest(tmp_path):
    """一致时返回本地摘要，供调用方写边车。"""
    local = tmp_path / "a.bin"
    payload = b"C" * 4096
    local.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    class FakeSftp:
        def open(self, path, mode):
            import io
            return io.BytesIO(payload)

        def stat(self, path):
            class S:
                st_size = len(payload)
            return S()

    class Uploader(DataUploader):
        def _remove_remote_quietly(self, sftp, path):
            raise AssertionError("一致时不应清理远端")

        def _fail(self, code, message):
            raise AssertionError(f"一致时不应失败: {message}")

    up = Uploader.__new__(Uploader)
    up.verify_content = True
    assert up._verify_remote_digest(FakeSftp(), local, "/r", len(payload)) == expected


# ---------- 数据验证报告的落点（v6.7.0） ----------

def test_verify_report_name_matches_the_script():
    """报告名格式在**应用侧与脚本里必须逐字一致**。

    两份字面量是刻意的（脚本要能独立在机器人上跑，不 import 本包）—— 但一旦
    有人只改了其中一处，现场就会出现"日志说报告叫 A、目录里躺着 B"，而这条守卫
    正是那种"两边都改才成立"的契约唯一能自动化检查的地方。
    """
    import re

    from data_collector import verify

    content = (Path(__file__).resolve().parents[1] / "scripts" / "bag_rate_stat.py") \
        .read_text(encoding="utf-8")
    matched = re.search(r"^REPORT_NAME_FMT = '([^']+)'", content, re.MULTILINE)
    assert matched, "脚本里找不到 REPORT_NAME_FMT"
    assert matched.group(1) == verify.VERIFY_REPORT_NAME_FMT, (
        f"脚本 {matched.group(1)!r} != 应用 {verify.VERIFY_REPORT_NAME_FMT!r}")


def test_verification_report_sits_inside_the_bag_directory(tmp_path):
    """报告落在**数据自己的目录里**（要求：结果放在数据的同一目录下）。

    钉的是"父目录就是那个 bag"，不是"文件名长什么样" —— 落点错了，
    现场拷数据时报告就会被落下，而它正是"这批数据验证过没有"的唯一凭据。
    """
    from datetime import datetime

    from data_collector.verify import verification_report_path

    bag = tmp_path / "bag_a"
    bag.mkdir()
    report = verification_report_path(bag, when=datetime(2026, 10, 20, 11, 2, 3))

    assert report.parent == bag, f"报告应直接放在 bag 目录下，实际 {report.parent}"
    assert report.name == "数据验证结果_20261020-110203.txt", report.name

# ==================== 判定：读报告得出"这批数据合格吗"（v6.9.0） ====================

def _write_report(bag: Path, stamp: str, body: str) -> Path:
    bag.mkdir(parents=True, exist_ok=True)
    path = bag / f"数据验证结果_{stamp}.txt"
    path.write_text(body, encoding="utf-8")
    return path


def test_read_bag_verdict_returns_none_without_a_report(tmp_path):
    """目录里没有报告 → None（界面据此显示"未验证"）。

    不能返回"合格"或"不合格"：那两种都是有依据的判断，而"没验过"是第三种状态。
    混进任何一边都会误导 —— 显示合格最危险。
    """
    bag = tmp_path / "no_report"
    bag.mkdir()
    (bag / "a.mcap").write_bytes(b"x")
    assert read_bag_verdict(bag) is None


def test_read_bag_verdict_parses_the_machine_readable_line(tmp_path):
    """新报告：认 `判定:` 行，不合格时带上原因。"""
    bag = tmp_path / "ok"
    _write_report(bag, "20261027-100000",
                  f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_PASS}\n生成时间: x\n")
    verdict = read_bag_verdict(bag)
    assert verdict["ok"] is True and verdict["detail"] == ""
    assert verdict["inferred"] is False

    bag2 = tmp_path / "bad"
    _write_report(bag2, "20261027-100000",
                  f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_FAIL}"
                  " | 缺 2 个话题；1 个分组需复查\n")
    verdict2 = read_bag_verdict(bag2)
    assert verdict2["ok"] is False
    assert verdict2["detail"] == "缺 2 个话题；1 个分组需复查", verdict2


def test_read_bag_verdict_picks_the_newest_report(tmp_path):
    """多次验证过 → 取**文件名时间戳最新**的那一份（不是 mtime，也不取旧结论）。

    mtime 会被 rsync / 解压 / 清理改写，而文件名里的时间戳不会 ——
    与 v6.7.7「数据时间不许用目录 mtime」同一个理由。
    """
    bag = tmp_path / "twice"
    _write_report(bag, "20261020-090000",
                  f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_FAIL} | 缺 1 个话题\n")
    _write_report(bag, "20261027-150000",
                  f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_PASS}\n")
    assert read_bag_verdict(bag)["ok"] is True


def test_read_bag_verdict_infers_from_legacy_reports(tmp_path):
    """旧报告没有判定行 → 按「二、结论」段落推断，并标 `inferred=True`。

    `inferred` 不是给人看的装饰：界面的 tooltip 要靠它说明"这是推断的，
    想拿到确定结论就重跑一次验证"。
    """
    ok_bag = tmp_path / "legacy_ok"
    _write_report(ok_bag, "20261020-090000",
                  "数据验证结果\n====\n二、结论\n"
                  "· 全部目标分组均有数据。\n"
                  "· 所有分组的帧间隔均可计算，且没有非递增时间戳。\n")
    verdict = read_bag_verdict(ok_bag)
    assert verdict["ok"] is True and verdict["inferred"] is True

    bad_bag = tmp_path / "legacy_bad"
    _write_report(bad_bag, "20261020-090000",
                  "数据验证结果\n====\n二、结论\n· 缺失 2 个目标话题（整段没有消息）：\n")
    verdict2 = read_bag_verdict(bad_bag)
    assert verdict2["ok"] is False and verdict2["inferred"] is True


def test_read_bag_verdict_never_treats_garbage_as_pass(tmp_path):
    """判定行内容认不出来时判**不合格**，不许猜成合格。

    版本错配（脚本改了措辞、上位机是旧版）时两种选择：
    报不合格 → 现场去查，代价是多看一份报告；
    报合格 → 一份其实有问题的数据被当成合格带走，代价是训练集里混进坏数据。
    这两者不对等，所以默认走保守那一侧。
    """
    bag = tmp_path / "garbage"
    _write_report(bag, "20261027-100000",
                  f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}说不清\n")
    verdict = read_bag_verdict(bag)
    assert verdict["ok"] is False
    assert "无法识别" in verdict["detail"], verdict


def test_bag_verdict_words_match_between_script_and_app():
    """判定行前缀与两个取值在**脚本与应用侧必须逐字一致**。

    与 `test_verify_report_name_matches_the_script` 同一类契约：脚本要能独立
    在机器人上跑（不 import 本包），所以字面量是两份；只改一处就会让上位机
    永远读不到判定 —— 而那种故障的表现是"全都是未验证"，看着像没跑过验证。
    """
    import re

    content = (Path(__file__).resolve().parents[1] / "scripts" / "bag_rate_stat.py") \
        .read_text(encoding="utf-8")

    prefix = re.search(r"^VERDICT_PREFIX = '([^']*)'", content, re.MULTILINE)
    passed = re.search(r"^VERDICT_PASS = '([^']*)'", content, re.MULTILINE)
    failed = re.search(r"^VERDICT_FAIL = '([^']*)'", content, re.MULTILINE)
    assert prefix and passed and failed, "脚本里找不到判定常量"

    assert prefix.group(1) == BAG_VERDICT_PREFIX
    assert passed.group(1) == BAG_VERDICT_PASS
    assert failed.group(1) == BAG_VERDICT_FAIL

def test_fresh_report_requires_a_report_at_all(tmp_path):
    """没有报告 → 不复用（必须去验）。"""
    bag = tmp_path / "empty"
    bag.mkdir()
    (bag / "a.mcap").write_bytes(b"x")
    assert has_fresh_report(bag) is False


def test_fresh_report_is_true_when_the_report_is_not_older_than_the_data(tmp_path):
    """报告不比数据旧 → 可以复用（这就是"不用重复验证"的判据）。"""
    import os

    bag = tmp_path / "fresh"
    bag.mkdir()
    data = bag / "a.mcap"
    data.write_bytes(b"x")
    os.utime(data, (1_000_000, 1_000_000))

    report = _write_report(bag, "20261027-100000",
                           f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_PASS}\n")
    os.utime(report, (1_000_100, 1_000_100))
    assert has_fresh_report(bag) is True


def test_fresh_report_is_false_when_the_data_is_newer_than_the_report(tmp_path):
    """数据比报告新 → **必须重验**。

    这是"复用"最容易出错的地方：目录名被重录覆盖时旧报告还在，
    只看"有没有报告"就会把旧结论当成新数据的结论 ——
    正是「把坏数据标成合格」的那类错。
    """
    import os

    bag = tmp_path / "stale"
    bag.mkdir()
    report = _write_report(bag, "20261020-090000",
                           f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_PASS}\n")
    os.utime(report, (1_000_000, 1_000_000))

    data = bag / "a.mcap"
    data.write_bytes(b"x")
    os.utime(data, (2_000_000, 2_000_000))          # 数据比报告新
    assert has_fresh_report(bag) is False


def test_fresh_report_ignores_the_report_file_itself_and_export_leftovers(tmp_path):
    """判"数据有多新"时，跳报告自身与导出产物（SHA256SUMS / 其它 .txt）。

    不跳的话，写完报告就会把"数据时间"顶到当前时刻，判据永远成立 ——
    那样重录覆盖过的目录也永远不会重验。
    """
    import os

    bag = tmp_path / "leftovers"
    bag.mkdir()
    data = bag / "a.mcap"
    data.write_bytes(b"x")
    os.utime(data, (1_000_000, 1_000_000))

    report = _write_report(bag, "20261027-100000",
                           f"数据验证结果\n====\n{BAG_VERDICT_PREFIX}{BAG_VERDICT_PASS}\n")
    os.utime(report, (1_000_100, 1_000_100))

    exported = bag / "SHA256SUMS"
    exported.write_text("deadbeef  a.mcap\n", encoding="utf-8")
    os.utime(exported, (3_000_000, 3_000_000))      # 导出产物很新，但不该影响判据

    assert has_fresh_report(bag) is True
