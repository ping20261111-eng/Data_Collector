"""我们自己维护的录制脚本副本（scripts/record_vla.sh / record_bag.sh / record_cmd.sh）。

为什么有副本：机器侧 `wheeled_robot_pkg/scripts/` 下那三个文件**不在版本控制里**
（那个工作空间没有 .git），一次重刷就可能把「BAG_DIR 可被环境变量覆盖」退回硬编码 ——
数据会**静默**落回 /var/nerv/bag，而界面、录制清单、打点、空间预检全按"写在盘上"理解。
所以真源搬到本仓（scripts/ 下这三份），机器侧那份退成派生来源 + 副本缺失时的回落。

这些用例把三件事钉住：
  1. 副本本身没被手改坏（sha256 对 `record_scripts.manifest.json`，机器侧变了也会提醒）；
  2. 副本保住了"能被环境变量改落盘位置"这一条，以及顺手修掉的 `cd` 引号；
  3. 副本真的能跑 —— 用假工作空间 + 假 ros2 真跑一遍，不靠"看起来对"。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core import ConfigManager, save_yaml  # noqa: E402
from data_collector.core_operations import Launcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST = SCRIPTS / "record_scripts.manifest.json"
NAMES = ("record_vla.sh", "record_bag.sh", "record_cmd.sh")

#: 机器侧的派生来源。只在本机（有机器人工作空间）时比对；不在则跳过，
#: 而不是假装通过 —— 与 test_scripts.py 里 VENDOR_FCC 的做法一致。
MACHINE_SIDE = Path(os.environ.get(
    "ROBOT_RECORD_SCRIPTS",
    "/home/nerv/test_ws/wheeled_robot_pkg/scripts"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_lines(text: str) -> list:
    """只取**有效命令行**（去空行、去注释行）。

    头注释里刻意写了 `cd $BAG_DIR`、`set -e` 这两个**反例**做说明，
    按整份文本搜会把说明本身当成代码 —— 第一版守卫就是这么误判的。
    """
    return [line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _stub_ros2(tmp_path: Path) -> Path:
    """把 ros2 换成桩：只做 `ros2 bag record` 真正落地的第一件事 —— 建出输出目录。

    注意别指望 PATH 桩能盖住真 ROS：脚本会 `source install/setup.bash`，
    那是**空桩**，所以 PATH 才不会被 /opt/ros/*/bin 抢走（2026-10-15 踩过这个坑）。
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    path = fake_bin / "ros2"
    path.write_text(
        "#!/bin/bash\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'echo "[stub] cwd=$PWD out=$out args=$*"\n'
        '[ -n "$out" ] && mkdir -p "$PWD/$out" && echo stub > "$PWD/$out/metadata.yaml"\n'
        "exit 0\n",
        encoding="utf-8")
    path.chmod(0o755)
    return fake_bin


def _make_world(tmp_path: Path) -> tuple:
    """造最小可运行环境：工作空间（install/setup.bash + VERSION）、NERV_DIR、ros2 桩。"""
    ws = tmp_path / "ws"
    (ws / "install").mkdir(parents=True, exist_ok=True)
    (ws / "install/setup.bash").write_text("# fake setup\n", encoding="utf-8")
    (ws / "VERSION").write_text("v6.4.17-test\n", encoding="utf-8")

    nerv = tmp_path / "nerv"
    (nerv / "product").mkdir(parents=True, exist_ok=True)
    (nerv / "product/Product.json").write_text(
        json.dumps({"serial_number": "SN-TEST", "hardware_model": "NERV-AMA01"}),
        encoding="utf-8")

    return ws, nerv, _stub_ros2(tmp_path)


def _run_copy(tmp_path: Path, script: str, *args: str, bag_dir=None, workspace=None,
              nerv_dir=None, cwd: Path = None):
    """真跑一次副本。返回 (退出码, 输出)。"""
    ws, nerv, fake_bin = _make_world(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "ROBOT_WORKSPACE": str(workspace if workspace is not None else ws),
        "NERV_DIR": str(nerv_dir if nerv_dir is not None else nerv),
    }
    if bag_dir is not None:
        env["BAG_DIR"] = str(bag_dir)
    result = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(cwd) if cwd else str(tmp_path),
    )
    return result.returncode, result.stdout + result.stderr


# ---------- ① 副本本身：存在、语法、没被手改坏 ----------
@pytest.mark.parametrize("name", NAMES)
def test_copy_exists_and_is_executable(name):
    path = SCRIPTS / name
    assert path.is_file(), f"缺副本 {path}"
    assert os.access(path, os.X_OK), f"{name} 没有执行权限（统一用 bash 调用，不影响运行，但该修）"


@pytest.mark.parametrize("name", NAMES)
def test_copy_is_syntactically_valid_bash(name):
    result = subprocess.run(["bash", "-n", str(SCRIPTS / name)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{name} 语法错：{result.stderr}"


@pytest.mark.parametrize("name", NAMES)
def test_copy_matches_the_manifest(name):
    """sha256 必须与清单一致 —— 改了副本就更新清单，别让改动悄悄溜过去。

    清单里带刷新命令；这里失败说明有人改了 `scripts/record_*.sh` 却没登记。
    """
    record = next(r for r in _manifest()["records"] if r["name"] == name)
    assert _sha256(SCRIPTS / name) == record["our_sha256"], (
        f"{name} 与 record_scripts.manifest.json 登记的 sha256 不一致。\n"
        f"  如果这次改动是有意的，按清单里的 refresh 命令刷新 our_sha256；\n"
        f"  如果不是有意的，用 git checkout scripts/{name} 还原。")


@pytest.mark.parametrize("name", NAMES)
def test_machine_side_source_has_not_changed_since_derivation(name):
    """机器侧那份变了要**知道**：它可能带了新功能而我们这份没有。

    这条红了不影响功能（起录制用的是我们这份），它报的是"可能漏了机器侧的改动"。
    机器侧被重刷退回硬编码也算变 —— 那正是要提醒的：说明那份不再是可信的派生来源。
    """
    src = MACHINE_SIDE / name
    if not src.is_file():
        pytest.skip(f"本机没有机器侧脚本：{src}")
    record = next(r for r in _manifest()["records"] if r["name"] == name)
    if _sha256(src) == record["machine_sha256_at_derivation"]:
        return
    pytest.fail(
        f"机器侧的 {name} 与派生时不一致（sha256 不同）。\n"
        f"  我们起录制用的是 scripts/ 下这份，所以**功能不受影响**；\n"
        f"  但机器侧可能带了新改动而我们这份没有 —— 请打开两份对一下：\n"
        f"    diff {src} scripts/{name}\n"
        f"  确认机器侧只是被重刷退回、或改动与我们无关 → 更新清单里的 "
        f"machine_sha256_at_derivation；确认有新功能 → 合并进 scripts/ 下这份再刷新 our_sha256。")


# ---------- ② 副本保住了关键约定 ----------
@pytest.mark.parametrize("name", NAMES)
def test_copy_still_accepts_bag_dir_from_the_environment(name):
    """契约闸门必须放行我们这份 —— 它认的是「BAG_DIR 能不能被环境变量覆盖」。

    副本哪天退回 `BAG_DIR="$NERV_DIR/bag"`，落盘位置就会静默失效，而界面照旧显示盘路径。
    """
    class _Probe:
        _BAG_DIR_OVERRIDABLE = Launcher._BAG_DIR_OVERRIDABLE
        _require_bag_dir_override = Launcher._require_bag_dir_override

    _Probe()._require_bag_dir_override(SCRIPTS / name)   # 不抛异常即放行


@pytest.mark.parametrize("name", ("record_vla.sh", "record_bag.sh"))
def test_copy_keeps_the_quoted_cd(name):
    """`cd "${BAG_DIR}"` 的引号不许丢。

    原版是无引号的 `cd $BAG_DIR`：路径含空格时 `cd: 参数太多`，而脚本没有 set -e
    就不退出，`ros2 bag record` 于是把包建在**当前目录** —— 静默写错位置且不报错。
    我们这份自己维护，所以这处必须保住。
    """
    code = _code_lines((SCRIPTS / name).read_text(encoding="utf-8"))
    quoted = [line for line in code if line.strip() == 'cd "${BAG_DIR}"']
    bare = [line for line in code if line.strip() == "cd $BAG_DIR"]
    assert quoted, "找不到带引号的 cd \"${BAG_DIR}\"（真代码里那一行不见了）"
    assert not bare, f"引号被去掉了（{bare}）：含空格路径会静默落到当前目录"


@pytest.mark.parametrize("name", NAMES)
def test_copy_has_no_set_e(name):
    """刻意不加 `set -e`，别"顺手补上"。

    `ros2 bag record` 被 Ctrl-C 打断时返回非 0；加了 set -e 就会跳过下面那段
    info.json 合并（写 name/desc/version），现场丢标签信息 —— 而且不报错。
    """
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    # 只看**有效命令行**：头注释里那句"刻意不加 set -e"的说明本身含这两个词，
    # 按整份文本搜会误判（第一版就是这么红的）。
    hits = [line for line in _code_lines(text)
            if "set -e" in line or "set -o errexit" in line]
    assert not hits, f"{name} 里有 set -e：{hits}"


# ---------- ③ 起录制用的是我们这份 ----------
def _config(tmp_path: Path, **extra) -> ConfigManager:
    cfg = tmp_path / "app_config.yaml"
    save_yaml(cfg, {"workspace_root": str(tmp_path),
                    "teleop_config_path": "t.yaml", **extra})
    return ConfigManager(cfg)


@pytest.mark.parametrize("key,default,name", [
    ("record_bag_script", "record_bag.sh", "record_bag.sh"),
    ("record_vla_script", "record_vla.sh", "record_vla.sh"),
    ("record_cmd_script", "record_cmd.sh", "record_cmd.sh"),
])
def test_recording_resolves_to_our_own_copy(tmp_path, key, default, name):
    """配置里是裸文件名时，起录制必须解析到 `scripts/` 下**我们这份**。

    `resolve_script_path` 的候选顺序里，统一脚本目录排在工作空间之前，所以副本放进来
    就自动优先 —— 但"自动"不等于"确定"，这里钉死：哪天顺序变了要立刻红。
    """
    # **关键前提**：工作空间里也要有一份同名脚本 —— 现场就是这样（机器侧一直有）。
    # 不造这个前提的话，"工作空间优先"也能让本用例通过，这条守卫就成了假守卫。
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / name).write_text(
        '#!/bin/bash\necho "机器侧那份"\n', encoding="utf-8")

    cm = _config(tmp_path, **{key: default})
    resolved = cm.get_script_path(key, default)
    assert resolved == SCRIPTS / name, (
        f"解析到了 {resolved} —— 现场机器侧有同名脚本，选错就意味着我们维护的副本被绕过")
    assert resolved.is_file(), "副本不在，界面会退到机器侧那份（那就又不受我们维护了）"


def test_machine_side_is_only_a_fallback(tmp_path):
    """我们那份不在时才回落到机器侧 —— 那是缺失时的兜底，不是常态。"""
    empty = tmp_path / "empty_scripts"
    empty.mkdir()
    # workspace_root 就是 tmp_path（见 _config），所以"机器侧那份"要放在它下面的
    # scripts/ 里 —— resolve_script_path 找的是 <workspace>/scripts/<文件名>。
    (tmp_path / "scripts").mkdir()
    fallback = tmp_path / "scripts/record_bag.sh"
    fallback.write_text('#!/bin/bash\necho "机器侧那份"\n', encoding="utf-8")
    cm = _config(tmp_path, scripts_dir=str(empty), record_bag_script="record_bag.sh")
    assert cm.get_script_path("record_bag_script") == fallback


# ---------- ④ 副本真的能跑 ----------
def test_copy_records_into_the_dir_given_by_bag_dir(tmp_path):
    """上位机选移动硬盘时，包必须落在 BAG_DIR 指的地方。

    路径特意**带空格**：上位机那条路上 cwd 与 BAG_DIR 是同一个目录，两处都靠引号兜住。
    """
    target = tmp_path / "disk space" / "S1"
    target.mkdir(parents=True)
    code, out = _run_copy(tmp_path, "record_vla.sh", "name=t1", "desc=带空格路径",
                          bag_dir=target, cwd=target)
    assert code == 0, out
    bags = list(target.glob("t1_*"))
    assert bags, f"BAG_DIR 指的目录里没有包：{out}"
    assert (bags[0] / "info.json").is_file(), "info.json 没写出来（收尾那段没跑到）"


def test_copy_falls_back_to_nerv_dir_bag_when_bag_dir_is_unset(tmp_path):
    """没传 BAG_DIR 时落 `$NERV_DIR/bag` —— 与机器侧原版行为一致，不因为换了副本就变。"""
    _, nerv, _ = _make_world(tmp_path)
    code, out = _run_copy(tmp_path, "record_bag.sh", "name=t2")
    assert code == 0, out
    bags = list((nerv / "bag").glob("t2_*"))
    assert bags, f"没有落到 $NERV_DIR/bag：{out}"
    info = json.loads((bags[0] / "info.json").read_text(encoding="utf-8"))
    assert info["name"] == "t2"
    assert info["serial_number"] == "SN-TEST"      # Product.json 被合并进来了
    assert info["version"].startswith("v6.4.17")  # VERSION 也合并进来了


def test_copy_refuses_to_start_when_the_workspace_cannot_be_found(tmp_path):
    """定位不到工作空间就**说清并退出**，不许"静默 source 失败"。

    原来的写法是 `source $ROOT_DIR/install/setup.bash` 且不看返回值：路径不对时它
    静默失败，紧接着 `ros2: command not found`，现场看到的是一句看不懂的报错。
    这里要求退出码 2 + 打印试过的候选 + 给出手工执行命令。
    """
    solo = tmp_path / "solo"          # 这个目录和它的上一级都没有 install/setup.bash
    solo.mkdir()
    script = solo / "record_vla.sh"
    script.write_text((SCRIPTS / "record_vla.sh").read_text(encoding="utf-8"), encoding="utf-8")
    env = {**os.environ, "ROBOT_WORKSPACE": "", "NERV_DIR": str(tmp_path / "nerv"),
           "PATH": os.environ["PATH"]}
    (tmp_path / "nerv/product").mkdir(parents=True, exist_ok=True)
    (tmp_path / "nerv/product/Product.json").write_text("{}", encoding="utf-8")
    result = subprocess.run(["bash", str(script), "name=x"], capture_output=True,
                            text=True, env=env, timeout=60, cwd=str(tmp_path))
    out = result.stdout + result.stderr
    assert result.returncode == 2, out
    assert "定位不到机器人工作空间" in out
    assert "试过：ROBOT_WORKSPACE=" in out
    assert "手工执行：ROBOT_WORKSPACE=" in out


def test_copy_points_qos_override_at_our_own_copy(tmp_path):
    """`--qos-profile-overrides-path` 必须指向我们 `scripts/` 下那份 qos_override.yaml。

    原版用 `${CURRENT_DIR}`（脚本自己所在目录）。副本在上位机仓里，如果不跟着走，
    就会指向一个不存在的路径 → ros2 报 qos 文件找不到，而录制已经开始了。
    """
    target = tmp_path / "target"
    target.mkdir()
    code, out = _run_copy(tmp_path, "record_vla.sh", "name=t3", bag_dir=target, cwd=target)
    assert code == 0, out
    assert f"--qos-profile-overrides-path {SCRIPTS / 'qos_override.yaml'}" in out


def test_copies_of_all_three_types_record_their_own_topics(tmp_path):
    """三份副本各自的话题清单不能互相串（生成时是逐份派生的，这里防串行）。"""
    seen = {}
    for name, expect in (("record_vla.sh", "/joint_states"),
                         ("record_bag.sh", "/tf_static"),
                         ("record_cmd.sh", "/chassis_control/move")):
        target = tmp_path / name.replace(".sh", "")
        target.mkdir()
        code, out = _run_copy(tmp_path, name, "name=x", bag_dir=target, cwd=target)
        assert code == 0, out
        assert expect in out, f"{name} 里没有话题 {expect}，疑似串了别份的话题清单：{out}"
        seen[name] = out
    assert len(seen) == 3