"""导出目标写回 `app_config.yaml`：按行改 + 备份 + 回读校验。

## 为什么值得单独一份

这份配置**满是说明性注释**（每行都在告诉现场"这个键干什么用"）。整份
`yaml.dump` 会把注释全丢掉，那是现场唯一的键说明。所以走"按行改"，
而按行改最容易出的两类错**都不会报错、只会静默写坏**：

1. 注释与值之间的空格被吃掉 → `export_target: both# 说明` —— 那个 `#` 前没空白，
   YAML **不认它是注释**，整段说明成了值的一部分（实测踩到，值变成
   `'both# server=只送远端 | disk=只写移动硬盘'`）。
2. 给标量加不加引号判断错 → 路径里的 `#` 被当注释起点截断；
   用 `isalnum()` 判"像不像路径"会漏中文（对中文返回 True）。

两条都在下面有对应用例。
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core import ConfigManager, patch_export_settings_text

BASE = "workspace_root: /tmp\nteleop_config_path: a.yaml\narm_control_config_path: b.yaml\n"

#: 已有这两段时的样子（含行内注释）。
WITH_SECTIONS = (
    "workspace_root: /tmp\n"
    "teleop_config_path: a.yaml\n"
    "arm_control_config_path: b.yaml\n"
    "export_target: server     # server=只送远端 | disk=只写移动硬盘 | both=两边都写\n"
    "disk_export:\n"
    "  mount_point: /old/path    # 旧路径\n"
    "  volume_id_file: .nerv_volume\n"
)


# ---------- 纯函数：替换 ----------

def test_replacing_a_scalar_keeps_the_trailing_comment():
    """换值不能吃掉行内注释 —— 注释是现场唯一的键说明。"""
    r = patch_export_settings_text(WITH_SECTIONS, "both", "/media/new")

    assert "# server=只送远端 | disk=只写移动硬盘 | both=两边都写" in r["text"]
    assert "# 旧路径" in r["text"]


def test_comment_keeps_a_space_before_the_hash():
    """**注释与值之间必须留一个空格** —— 否则 YAML 不认它是注释。

    这条是实测 bug：`export_target: both# 说明` 解析出来的值是
    `'both# server=只送远端 | disk=只写移动硬盘 | both=两边都写'`，
    也就是整段说明被当成了值，而 `export_target` 的实际取值变成非法的。
    而它**不会报任何错**，只会在下一次读配置时悄悄回退成 server。
    """
    r = patch_export_settings_text(WITH_SECTIONS, "both", None)
    line = [ln for ln in r["text"].splitlines() if ln.startswith("export_target:")][0]

    assert " #" in line, f"井号前没有空白，YAML 不会把它当注释：{line!r}"
    assert yaml.safe_load(r["text"])["export_target"] == "both"


def test_replaced_values_parse_back_correctly():
    """改完必须能被 YAML 解析成预期的值（这是最终判据）。"""
    r = patch_export_settings_text(WITH_SECTIONS, "disk", "/media/robot/NERV")
    parsed = yaml.safe_load(r["text"])

    assert parsed["export_target"] == "disk"
    assert parsed["disk_export"]["mount_point"] == "/media/robot/NERV"
    # 段里其它键逐字未动
    assert parsed["disk_export"]["volume_id_file"] == ".nerv_volume"


def test_mount_point_none_means_do_not_touch_it():
    """只改导出目标时，挂载点一个字都不能动。"""
    r = patch_export_settings_text(WITH_SECTIONS, "server")

    assert "/old/path" in r["text"]
    assert yaml.safe_load(r["text"])["disk_export"]["mount_point"] == "/old/path"


def test_quotes_are_added_for_paths_but_not_for_enums():
    """枚举值不加引号（与既有写法一致），路径加引号（防 `#` 与中文）。"""
    r = patch_export_settings_text(WITH_SECTIONS, "server", None)
    line = [ln for ln in r["text"].splitlines() if ln.startswith("export_target:")][0]
    assert 'export_target: server ' in line, f"枚举值不该加引号：{line!r}"

    r2 = patch_export_settings_text(WITH_SECTIONS, "disk", "/media/x")
    assert 'mount_point: "/media/x"' in r2["text"]


def test_a_hash_inside_the_path_is_not_treated_as_a_comment():
    """路径里带 `#` 时不能被截断 —— 这正是"一律加引号"的原因。"""
    r = patch_export_settings_text(WITH_SECTIONS, "disk", "/media/d#1")

    assert yaml.safe_load(r["text"])["disk_export"]["mount_point"] == "/media/d#1"


def test_a_chinese_path_survives_round_trip():
    """中文路径要能原样存回（`isalnum()` 白名单式判断会在这里漏）。"""
    r = patch_export_settings_text(WITH_SECTIONS, "disk", "/media/机器人盘")

    assert yaml.safe_load(r["text"])["disk_export"]["mount_point"] == "/media/机器人盘"


# ---------- 纯函数：追加（现场老配置） ----------

def test_appends_both_sections_when_missing(tmp_path):
    """**现场大多数配置都没有这两段**（实测本机这份就没有）→ 必须能追加。

    只做替换的话，界面写回会静默什么都没改，而用户以为存进去了。
    """
    r = patch_export_settings_text(BASE, "disk", "/media/robot/NERV")
    parsed = yaml.safe_load(r["text"])

    assert parsed["export_target"] == "disk"
    assert parsed["disk_export"]["mount_point"] == "/media/robot/NERV"
    # 追加要带上注释，否则现场不知道这几个键是干什么的
    assert "移动硬盘挂载点" in r["text"]


def test_appending_does_not_create_a_duplicate_top_level_key():
    """追加整段时不能出现两个 `export_target:` —— YAML 取后一个，会静默覆盖。"""
    r = patch_export_settings_text(BASE, "disk", "/media/x")
    count = sum(1 for ln in r["text"].splitlines() if ln.startswith("export_target:"))

    assert count == 1, f"顶层键重复了：{r['text']}"


def test_appending_keeps_the_original_content():
    """追加不能动原有内容（含其它键的注释）。"""
    original = BASE + "verify_content: false      # 挡住大小不变的损坏\n"
    r = patch_export_settings_text(original, "disk", "/media/x")

    assert "verify_content: false      # 挡住大小不变的损坏" in r["text"]


def test_no_change_when_target_is_the_same(tmp_path):
    """幂等：反复存同一个值，结果稳定（不会越存越多段）。"""
    once = patch_export_settings_text(WITH_SECTIONS, "disk", "/media/x")["text"]
    twice = patch_export_settings_text(once, "disk", "/media/x")["text"]

    assert once == twice
    assert twice.count("disk_export:") == 1


# ---------- ConfigManager.save_export_settings ----------

def _mgr(tmp_path, text=None):
    p = tmp_path / "app_config.yaml"
    p.write_text(text or BASE, encoding="utf-8")
    return ConfigManager(p), p


def test_save_writes_backs_up_and_verifies(tmp_path):
    """三步齐全：写对了、备份在、回读通过。"""
    mgr, cfg = _mgr(tmp_path, WITH_SECTIONS)
    backup_dir = tmp_path / "bak"

    res = mgr.save_export_settings("disk", "/media/robot/NERV", backup_dir=backup_dir)

    assert res["ok"] is True, res["errors"]
    assert res["backup"] is not None and res["backup"].is_file()
    assert res["backup"].read_text(encoding="utf-8") == WITH_SECTIONS   # 备份是改前那份
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["export_target"] == "disk"


def test_save_updates_the_in_memory_config(tmp_path):
    """写回后**内存里也要跟着变** —— 否则界面显示的还是旧值，直到重启。"""
    mgr, _ = _mgr(tmp_path, WITH_SECTIONS)
    mgr.save_export_settings("disk", "/media/x", backup_dir=tmp_path / "b")

    assert mgr.get_export_settings()["target"] == "disk"
    assert mgr.get_export_settings()["mount_point"] == "/media/x"


def test_save_rejects_an_illegal_target_without_touching_the_file(tmp_path):
    """非法取值**不写盘** —— 写进去等于把配置改坏。"""
    mgr, cfg = _mgr(tmp_path, WITH_SECTIONS)
    before = cfg.read_text(encoding="utf-8")

    res = mgr.save_export_settings("usb", None, backup_dir=tmp_path / "b")

    assert res["ok"] is False
    assert "非法" in " ".join(res["errors"])
    assert cfg.read_text(encoding="utf-8") == before


def test_save_refuses_when_nothing_could_be_patched(tmp_path, monkeypatch):
    """找不到键时不写盘 —— 写一半的配置比不写更糟。"""
    mgr, cfg = _mgr(tmp_path, WITH_SECTIONS)
    before = cfg.read_text(encoding="utf-8")
    monkeypatch.setattr("data_collector.core.patch_export_settings_text",
                        lambda *a, **k: {"text": "x: 1\n", "applied": [], "appended": []})

    res = mgr.save_export_settings("disk", None, backup_dir=tmp_path / "b")

    assert res["ok"] is False
    assert cfg.read_text(encoding="utf-8") == before


def test_save_refuses_to_write_broken_yaml(tmp_path, monkeypatch):
    """按行改的结果若不是合法 YAML，宁可放弃 —— 写坏会让**整个界面打不开**。"""
    mgr, cfg = _mgr(tmp_path, WITH_SECTIONS)
    before = cfg.read_text(encoding="utf-8")
    monkeypatch.setattr("data_collector.core.patch_export_settings_text",
                        lambda *a, **k: {"text": "key: [unclosed\n", "applied": ["export_target"],
                                         "appended": []})

    res = mgr.save_export_settings("disk", None, backup_dir=tmp_path / "b")

    assert res["ok"] is False
    assert cfg.read_text(encoding="utf-8") == before


def test_save_reports_a_failed_backup_but_still_writes(tmp_path, monkeypatch):
    """备份失败**不拦写入**，但必须如实报出来 —— 否则现场以为有备份。"""
    import re

    mgr, cfg = _mgr(tmp_path, WITH_SECTIONS)

    real_write_text = Path.write_text

    def boom(self, *a, **k):
        # 备份文件名是 `app_config_<时间戳>.yaml`，主配置是 `app_config.yaml` ——
        # 只让前者失败，后者照常写（要验的正是"备份挂了但主配置还是写成了"）。
        if re.search(r"app_config_\d{8}_\d{6}\.yaml$", str(self)):
            raise OSError("no space")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)

    res = mgr.save_export_settings("disk", None, backup_dir=tmp_path / "cannot")

    assert res["ok"] is True, res["errors"]
    assert res["backup"] is None
    assert any("备份失败" in ln for ln in res["lines"])
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["export_target"] == "disk"


def test_save_on_a_config_without_the_keys_still_works(tmp_path):
    """端到端：现场那种"两个键都没有"的配置，存完能读出来。"""
    mgr, cfg = _mgr(tmp_path)
    res = mgr.save_export_settings("disk", "/media/robot/NERV", backup_dir=tmp_path / "b")

    assert res["ok"] is True, res["errors"]
    assert mgr.get_export_settings()["target"] == "disk"
    assert mgr.get_export_settings()["mount_point"] == "/media/robot/NERV"
    # 注释也要在（下次现场打开文件还能看懂）
    assert "移动硬盘挂载点" in cfg.read_text(encoding="utf-8")


# ---------- 连续保存（界面真实用法：切下拉 → 填路径 → 切回来） ----------

def test_a_line_keeps_its_trailing_newline():
    """改过的行**必须保留行尾换行**。

    这条是实测 bug：`cut` 在没有 `#` 时落在行尾，若把换行也算进"要丢掉的部分"，
    这一行就与**下一行粘成一行** —— 下一行那句注释于是成了本行的值
    （实测读到 `'server# 这一段只有真正要写硬盘时才需要配…'`）。
    而它**不报错**，只在下次读配置时静默回退成 server。
    """
    r = patch_export_settings_text("export_target: server\nother: 1\n", "disk", None)
    line = [ln for ln in r["text"].splitlines(keepends=True)
            if ln.startswith("export_target:")][0]

    assert line == "export_target: disk\n", repr(line)


def test_three_saves_in_a_row_stay_correct(tmp_path):
    """**连续保存三次**都正确 —— 这正是界面上的用法（切下拉 → 填路径 → 切回来）。

    单次替换测不出这个类缺陷：问题只在"改过的行接着又被改"时暴露，
    因为第一次改丢了换行、第二次改就把下一行并进来了。
    """
    mgr, cfg = _mgr(tmp_path)
    bak = tmp_path / "b"

    assert mgr.save_export_settings("disk", "", backup_dir=bak)["ok"] is True
    assert mgr.save_export_settings("disk", "/media/x", backup_dir=bak)["ok"] is True
    assert mgr.save_export_settings("server", None, backup_dir=bak)["ok"] is True

    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["export_target"] == "server"
    assert parsed["disk_export"]["mount_point"] == "/media/x"
    # 注释一直要在（每一步都可能把它们吃掉）
    text = cfg.read_text(encoding="utf-8")
    assert "这一段只有真正要写硬盘" in text
    assert text.count("disk_export:") == 1
    assert text.count("export_target:") == 1


def test_repeated_saves_do_not_accumulate_sections(tmp_path):
    """反复存同一个值，段不会越存越多（幂等）。"""
    mgr, cfg = _mgr(tmp_path)
    bak = tmp_path / "b"
    for _ in range(5):
        assert mgr.save_export_settings("disk", "/media/x", backup_dir=bak)["ok"] is True

    text = cfg.read_text(encoding="utf-8")
    assert text.count("disk_export:") == 1, f"段重复了：{text}"
    assert text.count("mount_point:") == 1
