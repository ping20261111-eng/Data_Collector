"""导出目标与上传校验配置解析测试。

重点是**向后兼容**：现有部署的 `app_config.yaml` 里没有这些新键，
读取时必须表现和加之前完全一致。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core import ConfigManager

# ConfigManager 构造时需要这两个键存在（它会拿去拼工作空间路径），
# 与本次测试目标无关但缺了会 KeyError。
BASE = "workspace_root: /tmp\nteleop_config_path: a.yaml\narm_control_config_path: b.yaml\n"


def _mgr(tmp_path, extra=""):
    p = tmp_path / "app_config.yaml"
    p.write_text(BASE + extra, encoding="utf-8")
    return ConfigManager(p)


# ---------- 向后兼容（最重要） ----------

def test_absent_keys_mean_zero_behavior_change(tmp_path):
    """老配置里没有这些键 → 行为必须与加之前完全一致。

    这是整个改动的前提：现场已经在跑的部署不能因为加了两个键而变行为。
    """
    s = _mgr(tmp_path).get_export_settings()
    assert s["target"] == "server"          # 仍然只送服务器
    assert s["mount_point"] == ""
    assert s["require_volume_id"] is False  # 没在用硬盘，就不该校验盘标识


def test_verify_content_defaults_off(tmp_path):
    """内容校验默认关闭 —— 不能给现有流程凭空加一次全量回读。"""
    assert _mgr(tmp_path).get_upload_settings()["verify_content"] is False


def test_verify_content_opt_in(tmp_path):
    assert _mgr(tmp_path, "verify_content: true\n").get_upload_settings()["verify_content"] is True


# ---------- 移动硬盘的校验档位（v6.8.1） ----------

def test_disk_verify_mode_defaults_from_the_legacy_flag(tmp_path):
    """没写 `verify_mode` 时**按旧字段推导** —— 两种取值都要对上。

    这条守的是"数据完整性不许静默降级"。`upload.verify_content: true` 在旧版本
    里的含义就是「写完回读比对」，如果新档位把它推导成 `off`（不校验），
    一份**明确要求校验**的配置就会在没人察觉的情况下变成不校验 ——
    这种降级比慢严重得多，所以它必须映射到 `readback`。
    """
    assert _mgr(tmp_path).get_export_settings()["verify_mode"] == "off"
    assert _mgr(tmp_path, "verify_content: true\n").get_export_settings()["verify_mode"] \
        == "readback", "旧配置开了校验，不许降级成不校验"


def test_disk_verify_mode_can_be_switched_to_digest_for_speed(tmp_path):
    """显式写 `verify_mode: digest` → 照它执行（这是现场提速的开关）。

    `digest` 与 `readback` 都留下 `SHA256SUMS`、都不降低"有没有摘要"的保证，
    区别只在**要不要额外读一遍盘**。所以从 `readback` 切到 `digest` 不是
    "放弃校验"，而是"把当场比对改成以后可核"。
    """
    s = _mgr(tmp_path,
             "verify_content: true\n"
             "disk_export:\n"
             "  verify_mode: digest\n").get_export_settings()
    assert s["verify_mode"] == "digest", "显式配置必须优先于旧字段推导"


def test_disk_verify_mode_is_case_insensitive_and_validated(tmp_path):
    """大小写不敏感；写错的值**回落到推导**，不能变成"不校验"。"""
    assert _mgr(tmp_path, "disk_export:\n  verify_mode: READBACK\n") \
        .get_export_settings()["verify_mode"] == "readback"

    s = _mgr(tmp_path,
             "verify_content: true\n"
             "disk_export:\n"
             "  verify_mode: 快一点\n").get_export_settings()
    assert s["verify_mode"] == "readback", \
        f"非法值应回落到旧字段推导（而不是默认 off）：{s['verify_mode']}"


# ---------- export_target ----------

def test_invalid_target_falls_back_to_server(tmp_path):
    """写错的target 不能变成"什么都不做" —— 那会让数据静默留在本机。"""
    s = _mgr(tmp_path, "export_target: 云端\n").get_export_settings()
    assert s["target"] == "server"


@pytest.mark.parametrize("target", ["server", "disk", "both"])
def test_valid_targets_accepted(tmp_path, target):
    assert _mgr(tmp_path, f"export_target: {target}\n").get_export_settings()["target"] == target


def test_target_is_case_insensitive(tmp_path):
    assert _mgr(tmp_path, "export_target: DISK\n").get_export_settings()["target"] == "disk"


# ---------- disk_export ----------

def test_volume_id_required_when_disk_section_present(tmp_path):
    """配了 disk_export 段 → 默认要求盘标识。

    防"把数据写进错盘"是这类操作的主要风险，默认该开。
    """
    s = _mgr(tmp_path, "export_target: disk\ndisk_export:\n  mount_point: /media/usb\n").get_export_settings()
    assert s["mount_point"] == "/media/usb"
    assert s["require_volume_id"] is True


def test_require_volume_id_can_be_disabled(tmp_path):
    s = _mgr(tmp_path, "disk_export:\n  require_volume_id: false\n").get_export_settings()
    assert s["require_volume_id"] is False


def test_malformed_disk_section_does_not_crash(tmp_path):
    """disk_export 写成字符串时忽略它，不能抛异常把界面打不开。"""
    s = _mgr(tmp_path, "disk_export: 不是字典\n").get_export_settings()
    assert s["target"] == "server"
    assert s["mount_point"] == ""


def test_warn_file_bytes_default_below_fat32_limit(tmp_path):
    """预判阈值必须低于 FAT32 的 4GB 硬上限，留出余量。"""
    from data_collector.verify import FAT32_SAFE_FILE_BYTES
    s = _mgr(tmp_path).get_export_settings()
    assert s["warn_file_bytes"] == FAT32_SAFE_FILE_BYTES
    assert s["warn_file_bytes"] < 4_294_967_295
