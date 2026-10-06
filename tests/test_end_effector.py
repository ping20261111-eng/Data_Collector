"""末端参数（arm_control_config.yaml）：解析、按行改写、保存回读。

这一组用例守的是**"改的是别人维护的配置文件"**这件事本身：
① 只改目标行，注释与其它手型段逐字保留（那份注释是现场调档位的唯一说明）；
② 校验不过就整笔不写（不能改一半）；
③ 写文件没有退出码，**回读一致**才算成功。
"""
from pathlib import Path

import pytest

from data_collector.core import ConfigManager, load_yaml, save_yaml
from data_collector.core_operations import (END_EFFECTOR_DEFAULTS, Launcher,
                                            format_config_list, parse_arm_control,
                                            patch_arm_control_text, product_section,
                                            validate_end_effector_values)

#: 与机器人侧真实文件同构的样本：四种手型段 + 空行 + 对齐用的双空格 + 注释里的备选档位。
#: 刻意保留这些"看起来没用"的东西 —— 它们正是"整份 YAML 重写"会丢掉的部分。
SAMPLE_ARM_CONFIG = """\
# ARM Control 配置文件
# 
# 末端执行器类型配置
# 0: 灵巧手（yinshi）

ArmControlNode:
  ros__parameters:
    # 左臂末端执行器类型
    left_end_effector_type: 0  # 默认使用灵巧手
    left_end_effector_depth_control: true  # 默认使用深度控制
    
    # 右臂末端执行器类型  
    right_end_effector_type: 0  # 默认使用灵巧手
    right_end_effector_depth_control: true  # 默认使用深度控制

    yinshi:
      left_depth_mask: [ 1, 1, 1, 1, 1, 0,  0, 0, 0, 0, 0, 0 ]
      left_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]
      left_fist_cmd: [ 330, 340, 340, 370, 680, 60,  600, 600, 800, 600, 600, 600  ]
      
      right_depth_mask: [ 1, 1, 1, 1, 1, 0,  0, 0, 0, 0, 0, 0 ]
      right_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]
      right_fist_cmd: [ 330, 340, 340, 370, 680, 60,  600, 600, 800, 600, 600, 600  ]
    zhixing:
      left_depth_mask: [ 0, 0, 0, 0, 0, 0 ]
      left_open_cmd: [ 100, 50, 30, 100, 50, 30 ]
      left_fist_cmd: [ 10, 50, 100, 10, 50, 100 ]

      right_depth_mask: [ 0, 0, 0, 0, 0, 0 ]
      right_open_cmd: [ 100, 50, 30, 100, 50, 30 ]
      right_fist_cmd: [ 10, 50, 100, 10, 50, 100 ]
    hangkai:
      left_depth_mask: [ 0, 1 ]
      left_open_cmd: [ 800, 0 ]
      left_fist_cmd: [ 800, 1000 ]

      right_depth_mask: [ 0, 1 ]
      right_open_cmd: [ 800, 0 ]
      right_fist_cmd: [ 800, 1000 ]
    yushu:
      left_depth_mask: [ 0, 1, 0 ]
      left_open_cmd: [ 800, 0, 500 ]
      left_fist_cmd: [ 800, 1000, 500 ]

      right_depth_mask: [ 0, 1, 0 ]
      right_open_cmd: [ 800, 0, 500 ]
      right_fist_cmd: [ 800, 1000, 500 ]


    # 灵巧手张开指令，前6值为手指角度（0~1000），后6值为力控值（0~3000）
    # hand_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]
"""


def _workspace(tmp_path: Path) -> ConfigManager:
    """造一个最小工作空间（含末端参数文件）并返回 ConfigManager。"""
    workspace = tmp_path / "ws"
    (workspace / "install" / "config").mkdir(parents=True)
    (workspace / "install" / "config" / "arm_control_config.yaml").write_text(
        SAMPLE_ARM_CONFIG, encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(workspace),
        "teleop_config_path": "install/config/teleop_config.yaml",
        "arm_control_config_path": "install/config/arm_control_config.yaml",
        "local_log_dir": str(tmp_path / "logs"),
    })
    return ConfigManager(cfg_file)


def _lines_that_differ(before: str, after: str):
    """只返回真正变化的行（按行号对齐比较）。"""
    pairs = zip(before.splitlines(), after.splitlines())
    return [(index, old, new) for index, (old, new) in enumerate(pairs, start=1)
            if old != new]


# ---------- 解析 ----------

def load_yaml_text(text: str) -> dict:
    """把样本配置文本解析成 dict（与机器人侧同一套 YAML 语义）。"""
    import yaml
    return yaml.safe_load(text)


def test_parse_reads_types_depth_and_values():
    parsed = parse_arm_control(load_yaml_text(SAMPLE_ARM_CONFIG))
    assert parsed["params"] is True
    assert parsed["types"] == {"left": "yinshi", "right": "yinshi"}
    assert parsed["depth_control"] == {"left": True, "right": True}
    assert parsed["values"]["left"]["open"][:5] == [1000] * 5
    assert parsed["values"]["left"]["fist"] == [330, 340, 340, 370, 680, 60,
                                                600, 600, 800, 600, 600, 600]


def test_parse_reads_every_section_not_only_the_current_type():
    """四种手型段**全部**读出来：现场在下拉里换类型时，要立刻显示那一种在文件里的值。

    只读"当前那一种"的话，换成宇树时会把灵巧手的 12 个值填进 3 个格子里
    （前三个恰好是 1000 / 1000 / 1000，看起来还挺像回事）。
    """
    parsed = parse_arm_control(load_yaml_text(SAMPLE_ARM_CONFIG))
    assert set(parsed["sections"]) == {"yinshi", "zhixing", "hangkai", "yushu"}
    assert parsed["sections"]["yushu"]["left"]["open"] == [800, 0, 500]
    assert parsed["sections"]["zhixing"]["right"]["fist"] == [10, 50, 100, 10, 50, 100]
    assert parsed["sections"]["hangkai"]["left"]["open"] == [800, 0]


def test_parse_does_not_claim_unknown_types(tmp_path):
    """编号认不出来 → 类型是 None，**不猜**（也不顺手把 zhixing 当灵巧手）。"""
    parsed = parse_arm_control({"ArmControlNode": {"ros__parameters": {
        "left_end_effector_type": 9, "right_end_effector_type": 3}}})
    assert parsed["types"]["left"] is None
    assert parsed["types"]["right"] == "yushu"
    assert parsed["values"]["left"] == {"open": None, "fist": None}


def test_parse_ignores_partial_and_bool_values():
    """半截数据比没有更危险：个数对不上就整组算"没有"，布尔值别被当成类型编号。"""
    parsed = parse_arm_control({"ArmControlNode": {"ros__parameters": {
        "left_end_effector_type": True,       # 布尔也是 int，不能当 1（zhixing）
        "yushu": {"left_open_cmd": [800, 0],  # 少一个值
                  "left_fist_cmd": [800, "x", 500]}}}})
    assert parsed["types"]["left"] is None
    assert parsed["values"]["left"] == {"open": None, "fist": None}


def test_parse_without_params_section():
    parsed = parse_arm_control({"Other": {}})
    assert parsed["params"] is False
    assert parsed["types"] == {"left": None, "right": None}


def test_product_section_mapping():
    assert product_section("yushu_dex11") == "yushu"
    assert product_section("yinshi_rh56d") == "yinshi"
    assert product_section("empty") == ""
    assert product_section("") == ""


# ---------- 校验 ----------

@pytest.mark.parametrize("hand, values, expected", [
    ("yushu", [800, 0, 500], []),
    ("yushu", [800, 0], ["宇树夹爪 需要 3 个值，实际给了 2 个"]),
    ("yushu", [800, 0, 1001], ["宇树夹爪 第 3 个值 1001 超出范围 0~1000"]),
    ("yushu", [800, 0, -1], ["宇树夹爪 第 3 个值 -1 超出范围 0~1000"]),
    ("yushu", [800, 0, 1.5], ["宇树夹爪 第 3 个值不是整数：1.5"]),
    ("yinshi", list(range(12)), []),
    # 前 6 个位置是角度（0~1000）：填到 3000 只在第 1 个位置报错
    ("yinshi", [3000] + [100] * 11, ["灵巧手 第 1 个值 3000 超出范围 0~1000"]),
    # 第 7~12 个位置是力控（0~3000）：3000 合法，3001 越界
    ("yinshi", [100] * 6 + [3000] * 6, []),
    ("yinshi", [100] * 6 + [3001] + [0] * 5,
     ["灵巧手 第 7 个值 3001 超出范围 0~3000"]),
    ("zhixing", [1, 2], ["不支持的末端类型：zhixing"]),
])
def test_validate_end_effector_values(hand, values, expected):
    # 前 6 个是角度（0~1000）、后 6 个是力控（0~3000）；角度位越界必须报
    assert validate_end_effector_values(hand, values) == expected


def test_validate_lists_every_problem_not_just_the_first():
    problems = validate_end_effector_values("yushu", [2000, 0, 2000])
    assert len(problems) == 2


# ---------- 渲染 ----------

def test_format_config_list_reuses_original_separators():
    """复用原行分隔（含对齐用的双空格），现场眼睛 diff 时才只看到改动的数字。"""
    assert format_config_list([1000, 1000, 60, 300],
                              "[ 1000, 1000, 60,  300 ]") == "[ 1000, 1000, 60,  300 ]"
    assert format_config_list([900, 0, 500]) == "[ 900, 0, 500 ]"
    # 个数对不上（换了手型）→ 退回单空格，不能把值塞错位置
    assert format_config_list([1, 2], "[ 1000, 1000, 60,  300 ]") == "[ 1, 2 ]"


# ---------- 按行改写 ----------

def test_patch_only_touches_target_lines():
    patched = patch_arm_control_text(
        SAMPLE_ARM_CONFIG,
        {"yushu": {"left_open_cmd": [900, 50, 400], "left_fist_cmd": [700, 950, 600]}},
        {"left": "yushu"})
    assert patched["missing"] == []
    assert sorted(patched["applied"]) == ["left_end_effector_type",
                                          "yushu.left_fist_cmd", "yushu.left_open_cmd"]

    changed = _lines_that_differ(SAMPLE_ARM_CONFIG, patched["text"])
    assert [old.strip() for _no, old, _new in changed] == [
        "left_end_effector_type: 0  # 默认使用灵巧手",
        "left_open_cmd: [ 800, 0, 500 ]",
        "left_fist_cmd: [ 800, 1000, 500 ]",
    ]
    # 注释、空行、其它手型段一个字节都没动
    for kept in ("# 灵巧手张开指令，前6值为手指角度（0~1000）",
                 "# hand_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]",
                 "      right_open_cmd: [ 800, 0, 500 ]",
                 "      left_open_cmd: [ 100, 50, 30, 100, 50, 30 ]"):
        assert kept in patched["text"]
    assert "  # 默认使用灵巧手" in patched["text"]      # 行内注释留下


def test_patch_keeps_other_sections_untouched():
    """只改 yinshi 时，yushu / zhixing / hangkai 的值必须原样。"""
    patched = patch_arm_control_text(
        SAMPLE_ARM_CONFIG,
        {"yinshi": {"left_open_cmd": [1] * 6 + [2] * 6}}, {})
    before = SAMPLE_ARM_CONFIG.splitlines()
    after = patched["text"].splitlines()
    assert len(before) == len(after)             # 行数不变
    assert _lines_that_differ(SAMPLE_ARM_CONFIG, patched["text"])[0][1].strip() \
        == "left_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]"
    assert "left_open_cmd: [ 800, 0, 500 ]" in patched["text"]      # yushu 没被误改


def test_patch_reports_missing_keys_without_writing():
    patched = patch_arm_control_text(SAMPLE_ARM_CONFIG.replace("left_open_cmd", "left_open_cmd_x"),
                                     {"yushu": {"left_open_cmd": [1, 2, 3]}}, {"left": "yushu"})
    assert patched["missing"] == ["yushu.left_open_cmd"]
    assert patched["applied"] == ["left_end_effector_type"]


def test_patch_skips_none_values():
    """值为 None = 这一组不动（本期不支持的那一侧就是这么处理的）。"""
    patched = patch_arm_control_text(
        SAMPLE_ARM_CONFIG, {"yushu": {"left_open_cmd": None, "left_fist_cmd": [1, 2, 3]}}, {})
    assert patched["applied"] == ["yushu.left_fist_cmd"]
    assert "left_open_cmd: [ 800, 0, 500 ]" in patched["text"]


def test_patch_is_idempotent_when_values_unchanged():
    """同值写回 → 全文逐字节相同（现场靠 diff 判断"到底改了什么"）。"""
    patched = patch_arm_control_text(
        SAMPLE_ARM_CONFIG,
        {"yinshi": {"left_open_cmd": [1000, 1000, 1000, 1000, 1000, 60,
                                      300, 300, 300, 300, 300, 300]}}, {})
    assert patched["text"] == SAMPLE_ARM_CONFIG


# ---------- 读 ----------

def test_read_end_effector_config(tmp_path):
    config = _workspace(tmp_path)
    state = Launcher(config).read_end_effector_config(product_hand="yushu_dex11")
    assert state["available"] is True
    assert state["reason"] == "已读取"
    assert state["types"] == {"left": "yinshi", "right": "yinshi"}
    assert state["product_hand"] == "yushu_dex11"
    assert state["product_section"] == "yushu"
    assert state["sections"]["yushu"]["left"]["open"] == [800, 0, 500]   # 换类型时要用
    assert Path(state["path"]).name == "arm_control_config.yaml"


def test_read_end_effector_config_missing_file(tmp_path):
    config = _workspace(tmp_path)
    config.arm_control_config_path.unlink()
    state = Launcher(config).read_end_effector_config(product_hand="")
    assert state["available"] is False
    assert "不存在" in state["reason"]


def test_read_end_effector_config_bad_yaml_and_bad_structure(tmp_path):
    config = _workspace(tmp_path)
    config.arm_control_config_path.write_text("a: [1, 2\n", encoding="utf-8")
    assert Launcher(config).read_end_effector_config()["available"] is False
    config.arm_control_config_path.write_text("ArmControlNode: {}\n", encoding="utf-8")
    state = Launcher(config).read_end_effector_config()
    assert state["available"] is False
    assert "ros__parameters" in state["reason"]


# ---------- 保存 ----------

def test_save_writes_values_backs_up_and_verifies(tmp_path):
    config = _workspace(tmp_path)
    launcher = Launcher(config)
    backup_dir = tmp_path / "backup"
    result = launcher.save_end_effector_config(
        {"left": "yushu", "right": "yushu"},
        {"left": {"open": [900, 50, 400], "fist": [700, 950, 600]},
         "right": {"open": [900, 50, 400], "fist": [700, 950, 600]}},
        backup_dir=backup_dir)

    assert result["ok"] is True, result["errors"]
    assert result["errors"] == []
    backups = list(backup_dir.glob("arm_control_config_*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_ARM_CONFIG     # 备份是原样

    text = config.arm_control_config_path.read_text(encoding="utf-8")
    assert "left_open_cmd: [ 900, 50, 400 ]" in text
    assert "right_fist_cmd: [ 700, 950, 600 ]" in text
    assert "    left_end_effector_type: 3" in text                 # 类型跟着换
    assert "left_open_cmd: [ 1000, 1000, 1000, 1000, 1000, 60,  300, 300, 300, 300, 300, 300 ]" in text
    assert any("重启主程序" in line for line in result["lines"])
    assert any("回读校验通过" in line for line in result["lines"])
    # 写完还能读回来（回读校验用的是同一个入口）
    state = launcher.read_end_effector_config()
    assert state["values"]["left"]["open"] == [900, 50, 400]


def test_save_without_backup_dir_uses_local_logs(tmp_path):
    config = _workspace(tmp_path)
    result = Launcher(config).save_end_effector_config(
        {"left": "yinshi", "right": "yinshi"},
        {"left": None, "right": None})
    assert result["ok"] is True
    # 备份落在上位机自己的 logs/config_backup/ 下 —— 不往机器人侧 install/config/ 里塞 .bak
    assert list((tmp_path / "logs" / "config_backup").glob("*.yaml"))
    assert list((tmp_path / "ws" / "install" / "config").iterdir()) == \
        [config.arm_control_config_path]


def test_save_out_of_range_does_not_touch_file(tmp_path):
    config = _workspace(tmp_path)
    result = Launcher(config).save_end_effector_config(
        {"left": "yushu", "right": "yushu"},
        {"left": {"open": [800, 0, 4000], "fist": [800, 1000, 500]},
         "right": {"open": [800, 0, 500], "fist": [800, 1000, 500]}},
        backup_dir=tmp_path / "backup")
    assert result["ok"] is False
    assert any("超出范围" in line for line in result["errors"])
    assert config.arm_control_config_path.read_text(encoding="utf-8") == SAMPLE_ARM_CONFIG
    assert not list((tmp_path / "backup").glob("*.yaml"))      # 校验不过就不备份、不写


@pytest.mark.parametrize("section", ["zhixing", "hangkai", "unknown"])
def test_save_rejects_unsupported_types(tmp_path, section):
    config = _workspace(tmp_path)
    result = Launcher(config).save_end_effector_config(
        {"left": section, "right": "yushu"}, {"left": None, "right": None},
        backup_dir=tmp_path / "backup")
    assert result["ok"] is False
    assert any("不支持编辑" in line for line in result["errors"])
    assert config.arm_control_config_path.read_text(encoding="utf-8") == SAMPLE_ARM_CONFIG


def test_save_type_only_keeps_command_values(tmp_path):
    config = _workspace(tmp_path)
    result = Launcher(config).save_end_effector_config(
        {"left": "yushu", "right": "yushu"}, {"left": None, "right": None},
        backup_dir=tmp_path / "backup")
    assert result["ok"] is True
    changed = _lines_that_differ(
        SAMPLE_ARM_CONFIG, config.arm_control_config_path.read_text(encoding="utf-8"))
    assert len(changed) == 2                                       # 只有两行类型
    assert all(old.strip().startswith(("left_end_effector_type",
                                       "right_end_effector_type"))
               for _no, old, _new in changed)
    assert all("_cmd" not in old for _no, old, _new in changed)    # 一个数值行都没动


def test_save_reports_readback_mismatch(tmp_path):
    """写文件没有退出码：磁盘上若不是我们写进去的值，必须报"回读不一致"。"""
    config = _workspace(tmp_path)
    launcher = Launcher(config)
    original = launcher.read_end_effector_config

    def tampered(product_hand=None):
        state = original()
        state["values"]["left"]["open"] = [7, 7, 7]
        return state

    launcher.read_end_effector_config = tampered
    result = launcher.save_end_effector_config(
        {"left": "yushu", "right": "yushu"},
        {"left": {"open": [800, 0, 500]}, "right": None},
        backup_dir=tmp_path / "backup")
    assert result["ok"] is False
    assert any("回读不一致" in line for line in result["errors"])
    assert config.arm_control_config_path.read_text(encoding="utf-8").count(
        "left_open_cmd: [ 800, 0, 500 ]") == 1     # 值确实写进去了，只是"没确认"


def test_save_missing_file_and_no_content(tmp_path):
    config = _workspace(tmp_path)
    config.arm_control_config_path.unlink()
    result = Launcher(config).save_end_effector_config({"left": "yushu"}, {"left": None})
    assert result["ok"] is False and "不存在" in result["errors"][0]

    config = _workspace(tmp_path / "again")
    result = Launcher(config).save_end_effector_config({}, {})
    assert result["ok"] is False and "没有要写入的内容" in result["errors"][0]


def test_save_reports_missing_keys_instead_of_half_writing(tmp_path):
    config = _workspace(tmp_path)
    text = SAMPLE_ARM_CONFIG.replace("left_fist_cmd", "left_fist_cmd_x")
    config.arm_control_config_path.write_text(text, encoding="utf-8")
    result = Launcher(config).save_end_effector_config(
        {"left": "yushu", "right": "yushu"},
        {"left": {"open": [1, 2, 3], "fist": [4, 5, 6]}, "right": None},
        backup_dir=tmp_path / "backup")
    assert result["ok"] is False
    assert any("结构变了" in line for line in result["errors"])
    assert config.arm_control_config_path.read_text(encoding="utf-8") == text


# ---------- 工作空间切换 ----------

def test_arm_control_config_path_defaults_and_follows_workspace(tmp_path):
    """老配置里没有这一项也要能用（不该因此打不开界面）；切工作空间时必须跟着换。"""
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {"workspace_root": str(tmp_path / "a"),
                         "teleop_config_path": "install/config/teleop_config.yaml"})
    config = ConfigManager(cfg_file)
    assert config.arm_control_config_path == \
        tmp_path / "a" / "install/config/arm_control_config.yaml"

    config.set_workspace_root(str(tmp_path / "b"))
    assert config.arm_control_config_path == \
        tmp_path / "b" / "install/config/arm_control_config.yaml"


def test_defaults_match_the_file(tmp_path):
    """页面的"恢复默认值"必须等于文件里原样那套，否则一按就把配置改歪。"""
    parsed = parse_arm_control(load_yaml_text(SAMPLE_ARM_CONFIG))
    for side in ("left", "right"):
        assert parsed["values"][side]["open"] == END_EFFECTOR_DEFAULTS["yinshi"]["open"]
        assert parsed["values"][side]["fist"] == END_EFFECTOR_DEFAULTS["yinshi"]["fist"]
    assert END_EFFECTOR_DEFAULTS["yushu"] == {"open": [800, 0, 500], "fist": [800, 1000, 500]}


def test_arm_control_type_labels_do_not_repeat_the_section_name():
    """界面上的类型名：段名已经写进名字里的那两种不再重复拼接。

    直接从下拉列表里看出来的问题：`f"{名字}（{段名}）"` 会拼出
    "夹爪（zhixing）（zhixing）"—— 现场反馈"下拉菜单太丑"的截图里就是这样。
    """
    from data_collector.core_operations import (END_EFFECTOR_SECTIONS,
                                                end_effector_label)

    labels = {section: end_effector_label(section) for section in END_EFFECTOR_SECTIONS}
    assert labels["yinshi"] == "灵巧手（yinshi）"
    assert labels["yushu"] == "宇树夹爪（yushu）"
    assert labels["zhixing"] == "夹爪（zhixing）"          # 不重复
    assert labels["hangkai"] == "夹爪（hangkai）"
    for text in labels.values():
        assert text.count("（") == 1
