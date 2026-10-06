"""上位机 ↔ 机器人侧脚本的接口契约测试。

为什么需要这个文件：这两边的接口是 **shell 参数 + 文件字段**，没有类型、没有版本号、
没有 schema。之前就发生过"上位机传 `label=` 而上位机以为脚本会解析、真机脚本其实不
解析"的情况 —— 功能静默丢失，直到用户发现才被察觉。

契约测试的价值是让这类不一致在**提交时**暴露，而不是等现场发现。它同时把"我们依赖
脚本的哪些行为"从隐含假设变成显式断言：假设一旦变化，这里会先红。

脚本不存在时 skip —— 契约测试的前提是能读到脚本本体（真机脚本不在开发机上时跳过）。
"""
from pathlib import Path

import pytest

from data_collector.core import ConfigManager

RECORD_SCRIPTS = ("record_bag.sh", "record_vla.sh", "record_cmd.sh")


@pytest.fixture
def workspace():
    """机器人侧工作空间。配置文件不存在时跳过。"""
    cfg = Path("configs/app_config.yaml")
    if not cfg.is_file():
        pytest.skip("本地无 configs/app_config.yaml，无法定位工作空间")
    cm = ConfigManager(cfg)
    if not cm.workspace.is_dir():
        pytest.skip(f"工作空间不存在：{cm.workspace}")
    return cm.workspace


def _read(path: Path):
    if not path.is_file():
        pytest.skip(f"{path.name} 不在本机（真机脚本）")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------- 录制脚本：参数契约 ----------

@pytest.mark.parametrize("script_name", RECORD_SCRIPTS)
def test_record_script_accepts_name_and_desc(workspace, script_name):
    """上位机只传 name= / desc= —— 脚本必须处理这两个参数，否则录制的命名与备注丢失。"""
    text = _read(workspace / "scripts" / script_name)

    assert "name=" in text, f"{script_name} 未处理 name= 参数"
    assert "desc=" in text, f"{script_name} 未处理 desc= 参数"


@pytest.mark.parametrize("script_name", RECORD_SCRIPTS)
def test_record_script_does_not_parse_label(workspace, script_name):
    """上位机已改为把动作标签并入 desc。

    如果脚本某天开始支持 label=，说明契约变了：launch_record 应当改回传独立参数，
    而不是继续把标签往 desc 里塞。这条断言就是那个提醒。
    """
    text = _read(workspace / "scripts" / script_name)

    assert "label=" not in text, (
        f"{script_name} 开始解析 label= 了 —— 契约已变，"
        f"请同步更新 Launcher.launch_record 的参数拼装"
    )


@pytest.mark.parametrize("script_name", RECORD_SCRIPTS)
def test_record_script_writes_bag_info(workspace, script_name):
    """录制脚本负责落 info.json（数据自描述：名称、版本、机型）。"""
    text = _read(workspace / "scripts" / script_name)

    assert "info.json" in text, f"{script_name} 不再写 info.json，数据集将失去自描述"
    assert "version" in text, f"{script_name} 的 info.json 未写 version，无法追溯录制版本"


@pytest.mark.parametrize("script_name", RECORD_SCRIPTS)
def test_record_script_passes_through_env_dirs(workspace, script_name):
    """上位机通过环境变量注入路径（BAG_DIR 等），脚本要读它而不是写死路径。"""
    text = _read(workspace / "scripts" / script_name)

    assert "BAG_DIR" in text, f"{script_name} 未使用 BAG_DIR，会忽略上位机指定的录制目录"


# ---------- 主程序脚本：不解析 hand= ----------

def test_run_nodes_does_not_parse_hand(workspace):
    """run_nodes.sh 不解析 hand= 参数：手型一律由 Product.json 的 hand 字段决定。

    界面上曾经有手型下拉，选了不生效反而误导 —— 正因为脚本根本不看这个参数。
    """
    text = _read(workspace / "run_nodes.sh")

    assert "hand=" not in text, (
        "run_nodes.sh 开始解析 hand= 了 —— 界面可以恢复手型选择，"
        "但需要同步更新 README 与设置向导"
    )


# ---------- 回放脚本：rviz 与 bag= 参数 ----------

def test_play_bag_accepts_rviz_and_bag(workspace):
    """上位机传 `rviz` 与 `bag=<path>`；两者都必须被脚本识别。"""
    text = _read(workspace / "scripts" / "play_bag.sh")

    assert "rviz" in text, "play_bag.sh 未处理 rviz 参数"
    assert "bag=" in text, "play_bag.sh 未处理 bag= 参数"


def test_play_bag_normalizes_hand_alias(workspace):
    """play_bag 需要把历史手型短名归一化到实际 rviz 配置名。

    历史数据里写的是 hangkai / yinshi / yushu，而配置目录名是带型号后缀的
    （如 yushu_dex11）。不归一化就会 rviz2 -d 指向不存在的文件。
    """
    text = _read(workspace / "scripts" / "play_bag.sh")

    assert "hangkai" in text or "yushu" in text or "yinshi" in text, (
        "play_bag.sh 似乎不再做手型短名归一化，历史 bag 回放会取不到 rviz 配置"
    )


# ---------- 上位机侧：参数拼装与契约一致 ----------
# 上位机侧的反向断言（标签并入 desc、不传 label=）在 test_core.py 的
# test_launcher_record_merges_label_into_desc 里，与上面的脚本契约配套 ——
# 一边改了另一边没改，就会在这里或那里红。
