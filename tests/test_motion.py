"""姿势控制的数据层：轴表 / 量程 / payload / 正运动学 / 预设读取 / 逐步执行器。

这一组用例守的是**下发命令本身**（沙箱里没有 rclpy，跑不了真机，所以命令串与
流任务参数就是唯一能在这里钉住的东西）：话题名、消息类型、单位（度 → 弧度）、
数组长度、量程拦截、正运动学的杆长与量程，以及"每一步什么时候算完成"的判据
（实时流看脚本退出码、预设里的手臂等 `moving`、手等 `wait_ms`）。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_collector import core_motion as motion
from data_collector.core import ConfigManager, save_yaml

#: 与机器人侧同构的样本预设（含"某一步只动一只手臂""只有手型的一步"）
ARM_PRESETS = {
    "A": [{"left_arm": [62.238, 68.3, -22.6, 22.3, 31.6, 17.3, 0],
           "right_arm": [-62.238, 68.3, 22.6, 22.3, -31.6, 17.3, 0],
           "left_end_effector": [100, 100, 100, 100, 500, 600],
           "right_end_effector": [100, 100, 100, 100, 500, 600],
           "scale": 0.4}],
    "LB": [{"left_arm": [39.862, 44.9, -46.6, 64.5, 32.8, 33.0, 0],
            "scale": 0.5, "wait_ms": 1000},
           {"left_end_effector": [100, 100, 100, 100, 500, 600], "wait_ms": 500}],
    "BROKEN": [{"left_arm": [1, 2, 3]}],
}

TRUNK_PRESETS = {
    "StandUp": [0.0, 0.0, 0.0],
    "KneeBend_2": [45.0, 81.0, 36.0],
    "TooMany": [1.0, 2.0, 3.0, 4.0],
}


def _workspace(tmp_path: Path, *, arm=ARM_PRESETS, trunk=TRUNK_PRESETS,
               custom=None) -> ConfigManager:
    """造一个最小工作空间：两份预设 JSON（机器人侧）+ 一份自定义预设（上位机侧）。"""
    workspace = tmp_path / "ws"
    (workspace / "install" / "product" / "config").mkdir(parents=True, exist_ok=True)
    (workspace / "install" / "product" / "config" / "arm_motion.json").write_text(
        json.dumps(arm), encoding="utf-8")
    (workspace / "install" / "product" / "config" / "trunk_motion.json").write_text(
        json.dumps(trunk), encoding="utf-8")
    cfg_file = tmp_path / "app_config.yaml"
    save_yaml(cfg_file, {
        "workspace_root": str(workspace),
        "teleop_config_path": "install/config/teleop_config.yaml",
        "local_log_dir": str(tmp_path / "logs"),
        "motion_presets_file": str(tmp_path / "motion_presets.yaml"),
    })
    config = ConfigManager(cfg_file)
    if custom is not None:
        save_yaml(config.get_motion_presets_file(), custom)
    return config


# ============================================================
# 轴表与量程
# ============================================================

def test_axis_tables_follow_the_sdk_order_and_naming():
    """轴名不带中文（现场要求），顺序与 `trunk_state.joint_position` / 下发数组一致。"""
    assert [axis[0] for axis in motion.TRUNK_AXES] == ["H1", "H2", "H3", "H4", "H5", "H6"]
    assert [axis[0] for axis in motion.ARM_AXES["left"]] == \
        [f"L{index}" for index in range(1, 8)]
    assert [axis[0] for axis in motion.ARM_AXES["right"]] == \
        [f"R{index}" for index in range(1, 8)]
    assert motion.HAND_FINGERS == ("F1", "F2", "F3", "F4", "F5", "F6")

    # 量程抽查（SDK 文档原文）：躯干脚踝 ±85、颈抬头 ±30；手臂第 4 轴（肘）-58~143
    assert motion.TRUNK_AXES[0][1:3] == (-85.0, 85.0)
    assert motion.TRUNK_AXES[5][1:3] == (-30.0, 30.0)
    assert motion.ARM_AXES["left"][3][1:3] == (-58.0, 143.0)
    assert motion.ARM_AXES["left"][6][1:3] == (-58.0, 58.0)
    # 实体含义只放在悬停提示里，不是轴名
    assert motion.TRUNK_AXES[0][3] == "脚踝"


def test_validate_reports_range_and_count_without_changing_values():
    """越界只拦下并说明范围，**不静默钳位**（位置题：钳位会让现场以为自己的值生效了）。"""
    problems = motion.check_axis_values(motion.TRUNK_AXES, [0, 0, 0, 0, 0, 999])
    assert problems == ["H6（颈 · 抬头）999 超出范围 -30~30°"]

    assert motion.check_axis_values(motion.ARM_AXES["left"], [1, 2, 3]) == \
        ["需要 7 个值，收到 3 个"]
    assert motion.check_hand_values([0, 0, 0, 0, 0, 9999]) == ["第 6 个值 9999 超出 0~1000"]
    assert motion.check_scale(1.5) == ["速度比例 1.5 超出 0.01~1"]
    assert motion.check_scale("x") == ["速度比例不是数字：'x'"]
    # 合法值一律没有意见
    assert motion.check_axis_values(motion.TRUNK_AXES, [0] * 6) == []
    assert motion.check_scale(0.4) == []


# ============================================================
# 下发命令串（话题 / 类型 / 单位）
# ============================================================

def test_payloads_use_documented_topics_types_and_units():
    """度进弧度出、6/7 个值、手 ID 左 0x28 右 0x27 —— 都由命令串本身钉住。"""
    assert motion.TRUNK_TARGET_TOPIC == "/trunk_control/joint_target_position"
    assert motion.ARM_MOVE_MSG == "wheeled_robot_msgs/msg/ArmMove"

    # 躯干目标的形状**逐字对齐机器人侧 README**（含 `layout.data_offset: 1`）：
    # 缺了 layout（缺省 0）时命令"发得出去、订阅者也匹配，但机构一动不动"——
    # 现场 2026-09-29 的「躯干下发 3 次后仍未见动作」就是这个。详见 v6.3.3 的说明。
    assert motion.trunk_target_payload([45, 81, 36, 0, 0, 0]) == \
        "{layout: {dim: [], data_offset: 1}, data: [45, 81, 36, 0, 0, 0]}"
    # 22.3° = 0.3892 rad（消息定义里 position 是弧度）
    payload = motion.arm_move_payload([62.238, 68.3, -22.6, 22.3, 31.6, 17.3, 0], 0.4)
    assert payload.startswith("{targets: [{position: [1.0863, 1.1921, -0.3944, 0.3892,")
    assert payload.endswith("]}], scale: 0.4}")

    assert motion.hand_posture_payload("left", [100, 100, 100, 100, 500, 600]) == \
        "{side: 0, id: 40, start_addr: 0, position: [100, 100, 100, 100, 500, 600]}"
    assert motion.hand_posture_payload("right", [0] * 6).startswith("{side: 1, id: 39,")
    assert motion.trunk_mode_payload(0) == "data: 0"


def test_fmt_number_stays_short():
    """角度字面量要短：`22.300000000000001` 那种会让命令串与日志都没法看。"""
    assert motion.fmt_number(22.3) == "22.3"
    assert motion.fmt_number(-0.0) == "0"
    assert motion.fmt_number(60) == "60"
    assert motion.fmt_number(-0.05) == "-0.05"


# ============================================================
# 预设读取（机器人侧 JSON + 上位机自定义）
# ============================================================

def test_trunk_preset_pads_the_three_values_to_six_axes(tmp_path):
    """`trunk_motion.json` 只有 3 个值 → 补成 6 轴（其余轴回中）；照原样不取反。

    **v6.1.1 起躯干回到非实时（点位）**：一条 `Float32MultiArray`（度）出去，控制节点内部插值
    —— 所以 `streams` 为空。

    **v6.3.5 起一条步骤带两条条目**：`["躯干限速", "躯干目标"]`。
    限速那条是照现场示教脚本 `teach_arm_publisher.publish_trunk_position()` 补的
    （它就是"目标 + 紧挨着一条 speed_rate"），**放在目标之前**（理由见 `trunk_preset_steps`）。
    """
    config = _workspace(tmp_path)
    overview = motion.preset_overview(config)
    assert sorted(overview["trunk"]["presets"]) == ["KneeBend_2", "StandUp", "TooMany"]

    steps = motion.trunk_preset_steps("KneeBend_2", [45.0, 81.0, 36.0])
    assert len(steps) == 1
    step = steps[0]
    assert step["trunk"] is True and step["arms"] == () and step["streams"] == []
    assert [entry[0] for entry in step["entries"]] == ["躯干限速", "躯干目标"]
    name, topic, msg_type, payload = step["entries"][1]              # [0] 是限速，目标是 [1]
    assert (topic, msg_type) == (motion.TRUNK_TARGET_TOPIC, motion.TRUNK_TARGET_MSG)
    assert payload == ("{layout: {dim: [], data_offset: 1}, "
                       "data: [45, 81, 36, 0, 0, 0]}")            # 正值照原样，不取反
    assert step["target"]["trunk"] == [45.0, 81.0, 36.0, 0.0, 0.0, 0.0]

    # 速率那条：话题/类型/值都对，而且**必须在目标之前**
    rate_topic, rate_msg = step["entries"][0][1], step["entries"][0][2]
    assert (rate_topic, rate_msg) == (motion.TRUNK_SPEED_TOPIC, motion.TRUNK_SPEED_MSG)
    assert step["entries"][0][3] == f"data: {motion.fmt_number(motion.TRUNK_SPEED_RATE)}"

    assert motion.trunk_preset_steps("StandUp", [0, 0, 0])[0]["entries"][1][3] == \
        "{layout: {dim: [], data_offset: 1}, data: [0, 0, 0, 0, 0, 0]}"

    # `speed_rate=None` 时**只发目标**（留一个"不带速率"的口子，便于对照与排障）
    without = motion.trunk_preset_steps("StandUp", [0, 0, 0], speed_rate=None)[0]
    assert [entry[0] for entry in without["entries"]] == ["躯干目标"]

    # 显式给值就用给的值
    with_rate = motion.trunk_preset_steps("StandUp", [0, 0, 0], speed_rate=0.5)[0]
    assert with_rate["entries"][0][3] == "data: 0.5"


def test_stream_jobs_carry_the_right_units_and_cli():
    """**单位只在一处换算**：躯干话题是度、手臂 follow_position 是弧度，脚本本身不做换算。

    另外钉住几件容易漂的事：躯干要带 `--prepare`（实时模式的三步准备），
    手臂必须带 `--side`、且频率/时长与 `core_motion` 的常量一致。
    """
    trunk = motion.trunk_stream_job([30, 54, 24, 0, 0, 0])
    assert motion.stream_job_topic_values(trunk) == [30.0, 54.0, 24.0, 0.0, 0.0, 0.0]
    trunk_args = motion.stream_job_args(trunk)
    assert trunk_args[0] == "trunk"
    assert "--prepare" in trunk_args and "--hold" in trunk_args
    assert trunk_args[trunk_args.index("--rate") + 1] == str(motion.TRUNK_STREAM_RATE)

    arm = motion.arm_stream_job("left", [45.0] + [0.0] * 6)
    values = motion.stream_job_topic_values(arm)
    assert values[0] == pytest.approx(0.785398, abs=1e-6)      # 45° = 0.7854 rad
    arm_args = motion.stream_job_args(arm)
    assert arm_args[0] == "arm" and arm_args[arm_args.index("--side") + 1] == "left"
    assert arm_args[arm_args.index("--rate") + 1] == str(motion.ARM_STREAM_RATE)
    assert "--prepare" not in arm_args                          # follow_position 的准备在脚本里做
    assert motion.stream_job_key(arm) == "左臂实时流"
    assert "follow_position" in motion.stream_job_summary(arm)
    assert "度" in motion.stream_job_summary(arm)                # 给人看的一律是度


def test_arm_move_step_is_non_realtime_and_matches_presets():
    """手臂：**手动调节与预设走同一条** —— `ArmMove`（非实时，臂内部插值）。

    v6.1.1 起手动调节也改回非实时（现场 2026-09-28 要求），所以这里不再有实时流步骤；
    `ArmMove.position` 是**弧度**，换算只在 `arm_move_payload` 一处。
    """
    step = motion.arm_move_step("左臂 7 轴 运动", "left", [45.0] + [0.0] * 6)
    assert step["streams"] == []
    name, topic, msg_type, payload = step["entries"][0]
    assert name == "左臂运动"
    assert (topic, msg_type) == (motion.ARM_MOVE_TOPICS["left"], motion.ARM_MOVE_MSG)
    assert payload.startswith("{targets: [{position: [0.7854,")
    assert "scale: 0.4" in payload
    assert step["target"]["left_arm"][0] == 45.0

    preset = motion.arm_preset_steps("A", [{"left_arm": [1.0] * 7, "scale": 0.4}])[0]
    assert preset["streams"] == []
    name, topic, msg_type, _payload = preset["entries"][0]
    assert (topic, msg_type) == (motion.ARM_MOVE_TOPICS["left"], motion.ARM_MOVE_MSG)


# ============================================================
# 正运动学（示意图的几何；参数出自 URDF 原文）
# ============================================================

def test_trunk_fk_uses_the_urdf_link_lengths():
    """躯干：真实杆长 0.112 / 0.287 / 0.295 / 0.099 + 躯干段 0.4885（URDF 原文）。

    站直时应当是一根**竖直**的柱子（x 不随高度变），而且总高与 URDF 一致；
    屈膝时膝关节要**向后**突出去（现场贴的渲染图就是这个形状）。
    """
    up = motion.fk_trunk([0, 0, 0, 0, 0, 0])
    heights = [round(point[2], 4) for point in up["points"]]
    assert heights == [0.262, 0.374, 0.661, 0.956, 1.055, 1.5435, 1.5845]
    assert {round(point[0], 3) for point in up["points"]} == {-0.031, -0.055} or         len({round(point[0], 3) for point in up["points"]}) >= 1
    hip = up["hip"]
    assert round(hip[2] - up["base"][2], 4) == pytest.approx(0.793, abs=0.002)

    bent = motion.fk_trunk([30, 54, 24, 0, 0, 0])
    # 膝（H2）相对底座向后（+X 是前，所以是负得更多）突出去
    assert bent["knee"][0] < up["knee"][0] - 0.1
    # 抬高量级：屈膝之后整体变矮
    assert bent["neck"][2] < up["neck"][2]
    # 腰的偏航（H4）只转方向，不改高度
    yawed = motion.fk_trunk([30, 54, 24, 30, 0, 0])
    assert round(yawed["hip"][0], 3) == round(bent["hip"][0], 3)


def test_arm_fk_matches_the_ar5_chain():
    """手臂：上臂 0.314 / 前臂 0.272 / 手 0.1432；肩在腰上方 0.4135、向两侧 0.067。"""
    left = motion.fk_arm("left", [0] * 7)
    right = motion.fk_arm("right", [0] * 7)
    assert left["shoulder"][1] == pytest.approx(0.2415, abs=1e-3)     # 0.067 + 0.08725
    assert right["shoulder"][1] == pytest.approx(-0.2415, abs=1e-3)
    assert left["shoulder"][2] == pytest.approx(0.4135, abs=1e-3)

    # 姿态 A：手落在肩下 0.48m、前伸 0.30m 左右（与现场渲染的"手前下垂"一致）
    pose_a = motion.fk_arm("left", [62.238, 68.3, -22.6, 22.3, 31.6, 17.3, 0])
    assert pose_a["wrist"][2] < pose_a["shoulder"][2] - 0.4
    assert pose_a["wrist"][0] > 0.2
    # 肘在肩与腕之间（不是在两端外面）
    assert pose_a["shoulder"][2] > pose_a["elbow"][2] > pose_a["wrist"][2]
    # 左右对称的关节角 → 镜像的点（y 取反，x/z 相同）
    pose_a_right = motion.fk_arm("right", [-62.238, 68.3, 22.6, 22.3, -31.6, 17.3, 0])
    assert pose_a_right["wrist"][0] == pytest.approx(pose_a["wrist"][0], abs=1e-6)
    assert pose_a_right["wrist"][1] == pytest.approx(-pose_a["wrist"][1], abs=1e-6)


def test_side_view_puts_the_front_on_the_left():
    """侧视图里 **+X（前）落在画面左边** —— 图上那句「前」就是按这条画的。

    投影方向改了而不改标注，等于给现场一张"前后标反了"的图，比不标更危险。
    """
    front = motion.project_side((1.0, 0.0, 0.0))[0]
    back = motion.project_side((-1.0, 0.0, 0.0))[0]
    assert front < back


def test_projection_and_fit_are_deterministic():
    """投影方向与缩放是纯函数（示意图的位置必须可预期，不能"看着差不多"）。"""
    # 侧视：机头朝左（+X → 画面左），上下不翻（+Z → 画面上）
    assert motion.project_side((0.3, 0.2, 1.0)) == (-0.3, -1.0)
    # 正视：机器人左（+Y）落在画面右侧
    assert motion.project_front((0.3, 0.2, 1.0)) == (0.2, -1.0)

    fitted = motion.fit_projection([(0.0, 0.0), (10.0, 5.0)], 100.0, 100.0, 10.0)
    assert fitted["scale"] == pytest.approx(8.0)                 # (100-20)/10
    left, _ = motion.map_point((0.0, 0.0), fitted)
    right, _ = motion.map_point((10.0, 5.0), fitted)
    assert left == pytest.approx(10.0) and right == pytest.approx(90.0)
    assert motion.fit_projection([], 100.0, 100.0, 10.0)["scale"] == 1.0


def test_arm_preset_keeps_the_step_sequence_and_only_sends_what_is_written(tmp_path):
    """多步序列逐步发（合成一条会变成一条直线插值到终点），某步没写的部位不动。"""
    config = _workspace(tmp_path)
    overview = motion.preset_overview(config)
    assert sorted(overview["arm"]["presets"]) == ["A", "BROKEN", "LB"]

    steps = motion.arm_preset_steps("LB", overview["arm"]["presets"]["LB"]["steps"])
    assert len(steps) == 2
    assert steps[0]["arms"] == ("left",)
    assert [entry[0] for entry in steps[0]["entries"]] == ["左臂运动"]
    assert steps[0]["wait_ms"] == 1000
    # 第二步只有手型：不发任何 ArmMove，只发 hand_posture
    assert steps[1]["arms"] == () and steps[1]["hand"] is True
    assert [entry[1] for entry in steps[1]["entries"]] == [motion.HAND_POSTURE_TOPIC]

    single = motion.arm_preset_steps("A", overview["arm"]["presets"]["A"]["steps"])
    assert [entry[0] for entry in single[0]["entries"]] == \
        ["左臂运动", "左臂手型", "右臂运动", "右臂手型"]


def test_preset_read_reports_missing_and_broken_files_without_crashing(tmp_path):
    """预设文件缺失 / 不是 JSON / 值不对：一律变成一句人话，绝不抛到界面。"""
    config = _workspace(tmp_path)
    config.arm_motion_file = tmp_path / "nope.json"
    view = motion.read_arm_presets(config)
    assert view["available"] is False and "不存在" in view["reason"]

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    config.arm_motion_file = tmp_path / "bad.json"
    view = motion.read_arm_presets(config)
    assert view["available"] is False and "不是合法 JSON" in view["reason"]

    # 值不对的预设**照样列出来**，但带 problems（不静默丢弃，也不假装可用）
    config = _workspace(tmp_path, trunk={"TooMany": [1.0, 2.0, 3.0, 4.0],
                                         "NotList": 7})
    overview = motion.preset_overview(config)
    assert overview["trunk"]["presets"]["TooMany"]["problems"] == \
        ["关节角个数是 4，文件里应写 3 个（脚踝 / 膝 / 大腿），其余轴按 0 补"]
    assert "非空的数组" in overview["trunk"]["presets"]["NotList"]["problems"][0]


def test_custom_presets_are_kept_beside_the_robot_ones(tmp_path):
    """自定义预设与机器人侧预设**并排显示**，同名不覆盖（覆盖是静默改语义）。"""
    config = _workspace(tmp_path, custom={"trunk": {"StandUp": [1.0, 2.0, 3.0],
                                                    "我的站直": [0, 0, 0, 0, 0, 0]},
                                          "arm": {"我的挥手": [{"left_arm": [0] * 7}]}})
    overview = motion.preset_overview(config)
    trunk = overview["trunk"]["presets"]
    assert "我的站直" in trunk and trunk["我的站直"]["source"] == "custom"
    # 同名的两条**都在**：交付里的那条保持原名，自定义那条改键 + 加显示名后缀
    assert trunk["StandUp"]["source"] == "robot"
    assert trunk["StandUp"]["values"] == [0.0, 0.0, 0.0]
    assert trunk["custom:StandUp"]["name"] == "StandUp（上位机）"
    assert trunk["custom:StandUp"]["value"] == [1.0, 2.0, 3.0]
    assert overview["arm"]["presets"]["我的挥手"]["source"] == "custom"


def test_save_and_delete_custom_preset(tmp_path):
    """自定义预设的写入 / 删除：首次创建带写法说明，删除不留空壳。"""
    config = _workspace(tmp_path)
    path = motion.save_custom_preset(config, "trunk", "我的屈膝", [10, 20, 30, 0, 0, 0])
    text = Path(path).read_text(encoding="utf-8")
    assert "自定义预设" in text and "我的屈膝" in text
    assert motion.load_custom_presets(config)["trunk"]["我的屈膝"] == [10, 20, 30, 0, 0, 0]

    assert motion.delete_custom_preset(config, "trunk", "我的屈膝") is True
    assert motion.load_custom_presets(config)["trunk"] == {}
    assert motion.delete_custom_preset(config, "trunk", "没这条") is False


# ============================================================
# 逐步执行器
# ============================================================

class _FakeLauncher:
    """只回答执行器用到的两个方法：`publish_motion`（一次性）与 `run_motion_streams`（实时流）。"""

    def __init__(self, codes=None, stream_ok=True):
        self.calls = []
        self.stream_calls = []
        self.codes = codes
        self.stream_ok = stream_ok
        self.config = SimpleNamespace(check_workspace=lambda: True)

    def publish_motion(self, entries, timeout, label):
        self.calls.append({"label": label, "names": [entry[0] for entry in entries],
                           "topics": [entry[1] for entry in entries],
                           "entries": list(entries), "timeout": timeout})
        if callable(self.codes):
            return self.codes(entries)
        if self.codes is None:
            return {entry[0]: 0 for entry in entries}
        return self.codes

    def run_motion_streams(self, jobs, on_line=None, is_aborted=None, timeout=None):
        self.stream_calls.append({"jobs": list(jobs), "timeout": timeout})
        results = {}
        for job in jobs:
            key = motion.stream_job_key(job)
            if callable(self.stream_ok):
                results[key] = self.stream_ok(job)
            else:
                results[key] = self.stream_ok
        return results


class _FakeClock:
    """假时钟：sleep 推进时间，于是"等了多久"可以精确断言。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _runner(launcher, steps, clock, status_reader=None, **kwargs):
    return motion.MotionRunner(launcher, steps, status_reader=status_reader,
                               sleeper=clock.sleep, clock=clock, **kwargs)


def _stream_step(label: str, values, kind: str = "trunk", side=None) -> dict:
    """手搭一条**实时流**步骤（v6.1.1 起页面不再产生它，但执行器这条分支仍保留）。

    为什么还要测：那是一条**已验证**的备用通道（现场来回切过一次），
    `MotionRunner` 的 `streams` 分支与 `scripts/motion_stream.py` 都还在，
    留在这里才不会"哪天切回去发现它已经烂了"。
    """
    job = (motion.trunk_stream_job(list(values)) if kind == "trunk"
           else motion.arm_stream_job(side, list(values)))
    return {"label": label, "entries": [], "streams": [job], "trunk": kind == "trunk",
            "arms": () if kind == "trunk" else (side,), "hand": False, "wait_ms": None,
            "target": {}}


def test_runner_publishes_trunk_targets_and_waits_the_gap():
    """躯干每条目标 = **一条点位消息**（非实时）；两条之间必须等够 ≥1.5s（文档要求）。"""
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = [motion.trunk_preset_steps("A", [0, 0, 0])[0],
             motion.trunk_preset_steps("B", [10, 0, 0])[0]]
    # settle 调成 0.3s（默认 1.5s 与 gap 相等，间隔检查永远满足、看不到那条日志）
    result = _runner(launcher, steps, clock, trunk_settle=0.3)()

    assert result["ok"] is True
    assert launcher.stream_calls == []                           # 一条实时流都没起
    assert len(launcher.calls) == 2                              # 两条点位目标
    # v6.3.5：每条目标之前先带一条 speed_rate（照现场示教脚本），话题表因此是两条
    assert launcher.calls[0]["topics"] == [motion.TRUNK_SPEED_TOPIC,
                                           motion.TRUNK_TARGET_TOPIC]
    assert any("间隔 ≥1.5s" in line for line in result["lines"])
    assert clock.now == pytest.approx(0.3 + 1.2 + 0.3, abs=0.01)


def test_runner_waits_for_arm_moving_to_return_false():
    """预设里手臂那一步的完成判据是 `*_arm_moving` 由 true→false（与教学脚本一致）。"""
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = motion.arm_preset_steps("A", [{"left_arm": [0] * 7, "scale": 0.4}])

    def status():
        moving = clock.now < 2.0                 # 前 2 秒在动
        return {"module_state": {"arm_state": {"has_data": True,
                                               "left": {"moving": moving},
                                               "right": {"moving": False}}}}

    result = _runner(launcher, steps, clock, status_reader=status)()

    assert result["ok"] is True
    assert any("已确认 moving=false" in line for line in result["lines"])
    assert clock.now >= 2.0


def test_runner_admits_when_it_cannot_confirm_a_step():
    """取不到判据就**如实说未经确认**，不假装成功（三种取不到的情形各一条）。"""
    # ① 状态读不到（监控没跑）
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = motion.arm_preset_steps("A", [{"left_arm": [0] * 7, "scale": 0.4}])
    lines = _runner(launcher, steps, clock)()["lines"]
    assert any("读不到 arm_state" in line and "未经确认" in line for line in lines)

    # ② 状态有，但一直没看到 moving=true
    launcher, clock = _FakeLauncher(), _FakeClock()
    status = lambda: {"module_state": {"arm_state": {          # noqa: E731
        "has_data": True, "left": {"moving": False}, "right": {"moving": False}}}}
    lines = _runner(launcher, steps, clock, status_reader=status)()["lines"]
    assert any("没看到 左臂 的 moving=true" in line and "未经确认" in line
               for line in lines)

    # ③ moving 一直是 true（超时）
    launcher, clock = _FakeLauncher(), _FakeClock()
    status = lambda: {"module_state": {"arm_state": {          # noqa: E731
        "has_data": True, "left": {"moving": True}, "right": {"moving": False}}}}
    result = _runner(launcher, steps, clock, status_reader=status, arm_timeout=3.0)()
    assert any("超时" in line and "未经确认" in line for line in result["lines"])
    assert clock.now >= 3.0


def test_runner_stops_and_says_so_when_a_publish_is_unconfirmed():
    """某一步没送达就**停在那里**并说明（不是继续把后面的姿势叠上去）。"""
    launcher = _FakeLauncher(codes=lambda entries: {"左臂运动": 124})
    clock = _FakeClock()
    steps = [motion.arm_preset_steps("A", [{"left_arm": [0] * 7}])[0],
             motion.arm_preset_steps("B", [{"right_arm": [0] * 7}])[0]]
    result = _runner(launcher, steps, clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    # 兜底路径会先发一次"退出跟随"（`*_arm_move` 只在非 follow 模式生效），
    # 所以这里数的是**运动那一次**，不是全部调用次数
    moves = [call for call in launcher.calls if "左臂运动" in call["names"]]
    assert len(moves) == 1
    assert any("等待超时" in line for line in result["lines"])


def test_runner_reports_unconfirmed_realtime_stream():
    """实时流取不到结果（脚本没回退出码）→ 如实说"未经确认"并停下，不假装成功。"""
    launcher, clock = _FakeLauncher(stream_ok=None), _FakeClock()
    result = _runner(launcher, [_stream_step("走一个", [0] * 7, "arm", "left")], clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    assert any("未确认" in line for line in result["lines"])
    assert len(launcher.stream_calls) == 1


def test_runner_stops_when_a_stream_fails():
    """实时流脚本非 0 退出（读不到当前角度之类）→ 停下并说明是哪一条。"""
    launcher, clock = _FakeLauncher(stream_ok=False), _FakeClock()
    result = _runner(launcher, [_stream_step("躯干实时流", [0] * 6)], clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    assert any("没有正常结束" in line for line in result["lines"])


def test_runner_tells_the_stream_to_stop_when_aborted():
    """中止：把"已请求中止"传进实时流执行体（它负责结束子进程），并如实报停在第几步。"""
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = [_stream_step("躯干实时流 A", [0] * 6),
             _stream_step("躯干实时流 B", [10] * 6)]
    seen = []
    runner = _runner(launcher, steps, clock)
    runner.progress = None

    def streams(jobs, on_line=None, is_aborted=None, timeout=None):
        seen.append(bool(is_aborted and is_aborted()))
        runner.abort()
        return {motion.stream_job_key(job): True for job in jobs}

    runner.stream_runner = streams
    result = runner()

    assert seen == [False]                       # 第一段开跑时还没中止
    assert result["ok"] is False and result["aborted"] is True
    assert result["stopped_at"] == 1             # 第 1 步已下发、没走完
    assert result["stop_reason"] == "during"     # 与"没下发"分开报（措辞靠它）
    assert len(launcher.stream_calls) == 0       # 用了注入的执行体


def test_runner_can_be_aborted_between_steps():
    """中止：报"停在第 N 步之前"，不假装跑完（界面靠这个如实汇报）。"""
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = [motion.arm_preset_steps("A", [{"left_arm": [0] * 7}])[0],
             motion.arm_preset_steps("B", [{"left_arm": [1] * 7}])[0],
             motion.arm_preset_steps("C", [{"left_arm": [2] * 7}])[0]]

    runner = _runner(launcher, steps, clock)
    runner.progress = None
    original = launcher.publish_motion

    def publish_then_abort(entries, timeout, label):
        codes = original(entries, timeout, label)
        runner.abort()                            # 模拟现场在第 1 步后按了「中止」
        return codes

    launcher.publish_motion = publish_then_abort
    result = runner()

    assert result["aborted"] is True and result["ok"] is False
    assert result["stopped_at"] == 2
    assert any("中止" in line and "2/3" in line for line in result["lines"])
    # 同上：只数"运动"那一次（退跟随那两次是前置条件，不算动作步骤）
    moves = [call for call in launcher.calls if "运动" in " ".join(call["names"])]
    assert len(moves) == 1


def test_runner_skips_steps_without_entries():
    """空步骤（JSON 里只有 wait_ms 的那种）不该往下发，也不该卡住。"""
    launcher, clock = _FakeLauncher(), _FakeClock()
    steps = [{"label": "空步骤", "entries": [], "streams": [], "trunk": False,
              "arms": (), "hand": False, "wait_ms": 500, "target": {}}]
    result = _runner(launcher, steps, clock)()

    assert result["ok"] is True
    assert launcher.calls == []
    assert any("没有任何可下发的条目" in line for line in result["lines"])


# ============================================================
# 逐步执行器：闭环到位判据（v6.2.0）
# ============================================================

class _LoopLauncher(_FakeLauncher):
    """在 `_FakeLauncher` 之上加**闭环**能力：`move_trunk` / `move_arm` 按剧本回结果。

    真身是 `Launcher.move_trunk/move_arm` —— 它们把"等订阅者、重发、回读关节角确认到位"
    都放在常驻通道里做（见 `scripts/ros_command_bridge.py`）。执行器只认三件事：
    `ok=True/False/None`、`residual`、以及 `attempts == 0`（= 通道压根没跑起来，要降级）。
    """

    def __init__(self, moves=None, **kwargs):
        super().__init__(**kwargs)
        self.move_calls = []
        self.moves = dict(moves or {})

    def _result(self, kind, side):
        default = {"ok": True, "attempts": 1, "matched": 1, "reason": "",
                   "residual": [0.0] * (6 if kind == "trunk" else 7)}
        return dict(self.moves.get((kind, side), default))

    def move_trunk(self, values, **kwargs):
        self.move_calls.append(("trunk", None, list(values)))
        return self._result("trunk", None)

    def move_arm(self, side, values, **kwargs):
        self.move_calls.append(("arm", side, list(values)))
        return self._result("arm", side)


def test_runner_closed_loop_confirms_each_trunk_step_and_skips_fixed_waits():
    """躯干步骤走闭环：**确认到位就不再固定睡 1.5s**，只留协议要求的 1.5s 间隔。

    这是"效果不满意"里被忽视的一半：原来每步都固定睡 1.5s（不管机构 0.3s 就到了），
    多步预设会前松后紧。闭环之后节奏是"到位就走下一步"，代价是必须真能回读到角度
    （读不到就如实报"未经确认"，见下一条用例）。
    """
    launcher, clock = _LoopLauncher(), _FakeClock()
    steps = [motion.trunk_preset_steps("A", [0, 0, 0])[0],
             motion.trunk_preset_steps("B", [10, 0, 0])[0]]

    result = _runner(launcher, steps, clock)()

    assert result["ok"] is True
    assert launcher.calls == []                       # 一条都不走一次性 CLI
    assert [call[0] for call in launcher.move_calls] == ["trunk", "trunk"]
    assert launcher.move_calls[1][2][0] == 10.0
    assert any("已到位" in line for line in result["lines"])
    # 只等了"两条躯干目标之间的 1.5s"（第一与第二步之间）—— 不再是每步各 1.5s（共 3.0s）
    assert clock.now == pytest.approx(1.5, abs=0.01)


def test_trunk_speed_rate_goes_through_the_closed_loop_too():
    """「躯干限速」必须和「躯干目标」走**同一条闭环路**，不能掉进一次性 CLI 兜底。

    掉进去的后果不是"发不出去"，而是**顺序与时序都错**：一次性 CLI 是"新进程 +
    重做一遍 DDS 发现"（几秒起步），而且是**在目标之后**才发 —— 而速率本该在目标
    之前生效。写这条是因为 v6.3.5 的第一版就是这么错的（被上面那条闭环用例抓到）。

    闭环那条路里它由 `Bridge._send_target` 先发、再发目标；降级那条路用的是
    `step["entries"]` 全量，本来就带着它 —— 所以这里只盯"没掉进 CLI"。
    """
    launcher, clock = _LoopLauncher(), _FakeClock()
    step = motion.trunk_preset_steps("A", [0, 0, 0])[0]

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is True
    assert launcher.calls == [], \
        "「躯干限速」掉进了一次性 CLI 兜底：每步都会多等几秒，而且顺序反了"
    assert [call[0] for call in launcher.move_calls] == ["trunk"]


def test_runner_closed_loop_arm_step_needs_no_moving_flag():
    """手臂走闭环时**不需要 `arm_state.*_arm_moving`** —— 到位由关节角回读判定。

    旧路径在监控没跑时会打印"读不到 arm_state → 固定等 1.5s，该步未经确认"；
    闭环把这条不确定性去掉了（`arm_state` 由通道自己订阅）。
    """
    launcher, clock = _LoopLauncher(), _FakeClock()
    step = motion.arm_move_step("左臂 7 轴 运动", "left", [45.0] + [0.0] * 6)

    result = _runner(launcher, [step], clock, status_reader=None)()

    assert result["ok"] is True
    assert launcher.calls == []
    assert launcher.move_calls == [("arm", "left", [45.0] + [0.0] * 6)]
    assert not any("读不到 arm_state" in line for line in result["lines"])
    assert any("已到位" in line for line in result["lines"])


def test_runner_falls_back_to_cli_when_the_channel_is_not_available():
    """通道没跑起来（`attempts == 0`）→ 退回一次性下发 + 固定等待，并**如实标注未确认**。

    降级不能变成"不能用了"：本机没 ROS、没装 rclpy、包被裁过一半时，这一页照旧要能下发。
    而"确认"这件事降级之后就不再成立 —— 所以必须写进日志，不能假装闭环过。
    """
    launcher, clock = _LoopLauncher(moves={
        ("trunk", None): {"ok": None, "attempts": 0, "matched": -1, "residual": None,
                          "reason": "常驻通道不可用：找不到通道脚本"}}), _FakeClock()
    steps = [motion.trunk_preset_steps("A", [0, 0, 0])[0]]

    result = _runner(launcher, steps, clock)()

    assert result["ok"] is True
    assert len(launcher.move_calls) == 1                  # 试过闭环，通道没起来
    assert len(launcher.calls) == 1                       # 退回了一次性 CLI（躯干目标）
    assert any("常驻通道不可用" in line and "未经确认" in line for line in result["lines"])
    assert clock.now >= motion.TRUNK_SETTLE_SEC           # 兜底路径仍按固定时长等


def test_runner_stops_when_the_target_is_not_reached():
    """没走到位 → **停下并报残差**，且不再改发一遍（那会把"没到位"变成"又发了一次"）。"""
    launcher, clock = _LoopLauncher(moves={
        ("trunk", None): {"ok": False, "attempts": 3, "matched": 1, "residual": [8.0] * 6,
                          "reason": "躯干下发 3 次后仍未见动作（最大还差 8.0°）"}}), _FakeClock()
    steps = [motion.trunk_preset_steps("A", [30, 0, 0, 0, 0, 0])[0],
             motion.trunk_preset_steps("B", [10, 0, 0, 0, 0, 0])[0]]

    result = _runner(launcher, steps, clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    assert result["stop_reason"] == "failed"
    assert len(launcher.move_calls) == 1                  # 第 2 步没有下发
    assert launcher.calls == []                           # 也不改发 CLI
    assert any("没走到位" in line and "未见动作" in line for line in result["lines"])


def test_runner_reports_unconfirmed_when_the_readback_is_missing():
    """通道在、但读不到当前角度 → `ok=None`：**未经确认**，既不能当成功也不能当失败。"""
    launcher, clock = _LoopLauncher(moves={
        ("trunk", None): {"ok": None, "attempts": 1, "matched": 1, "residual": None,
                          "reason": "读不到躯干的当前角度 —— 是否到位未经确认"}}), _FakeClock()

    result = _runner(launcher, [motion.trunk_preset_steps("A", [0] * 6)[0]], clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    assert any("未经确认" in line for line in result["lines"])
    assert launcher.calls == []                           # 已经发过了，不重复发


def test_runner_keeps_hand_posture_in_the_same_arm_step():
    """手臂 + 手型在**同一步**时：手臂走闭环、手型照旧单独发（协议没有回读话题）。

    顺序与官方手柄示例一致（`arm_proxy.cpp`：先 `ArmMove`、再 `HandPosture`）。
    """
    launcher, clock = _LoopLauncher(), _FakeClock()
    step = motion.arm_preset_steps("A", [{"left_arm": [1.0] * 7,
                                          "left_end_effector": [100] * 6,
                                          "wait_ms": 600}])[0]

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is True
    assert launcher.move_calls == [("arm", "left", [1.0] * 7)]
    assert [call["names"] for call in launcher.calls] == [["左臂手型"]]      # 只有手型走 CLI
    assert any("没有回读话题" in line for line in result["lines"])


def test_runner_releases_follow_before_arm_moves_in_the_fallback():
    """**兜底路径**（通道不可用）也要先退 follow / servoJ。

    依据（SDK 原文）：`*_arm_move` 只在"非 follow position 模式"生效，而遥操作会把手臂
    留在 follow 模式，**停止遥操作不会复位**。通道那条路由脚本的 `ensure_arm_point_mode`
    做；一次性 CLI 这条路原来没做 —— 现场 2026-09-29 恰恰是用它跑的（通道被一个崩溃打死），
    右臂的 `ArmMove` 就那样被静默吃掉了。
    """
    launcher, clock = _LoopLauncher(moves={
        ("arm", "left"): {"ok": None, "attempts": 0, "matched": -1, "residual": None,
                          "reason": "常驻通道不可用"}}), _FakeClock()
    step = motion.arm_move_step("左臂 7 轴 运动", "left", [45.0] + [0.0] * 6)

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is True
    releases = [call for call in launcher.calls
                if any("退出" in name for name in call["names"])]
    assert len(releases) == 1                                   # 两条 false 一次并发下发
    assert releases[0]["names"] == ["左臂退出跟随", "左臂退出servoJ"]
    assert releases[0]["topics"] == ["/arm_control/left_arm_follow_position_set",
                                     "/arm_control/left_arm_servoj_set"]
    assert [entry[3] for entry in releases[0]["entries"]] == ["data: false", "data: false"]
    # **顺序**：退跟随必须在运动之前发出去
    release_at = launcher.calls.index(releases[0])
    move_at = next(i for i, call in enumerate(launcher.calls)
                   if any("运动" in name for name in call["names"]))
    assert release_at < move_at
    assert any("退出跟随" in line for line in result["lines"])


def test_runner_does_not_release_follow_for_trunk_only_steps():
    """只动躯干时**不发**那两条 false —— 无关的指令一条都不要多发。"""
    launcher, clock = _LoopLauncher(moves={
        ("trunk", None): {"ok": None, "attempts": 0, "matched": -1, "residual": None,
                          "reason": "常驻通道不可用"}}), _FakeClock()

    _runner(launcher, [motion.trunk_preset_steps("A", [0, 0, 0])[0]], clock)()

    assert not any("退出" in name for call in launcher.calls for name in call["names"])


class _BatchLauncher(_LoopLauncher):
    """带**批量**接口的替身（真身的 `Launcher.move_arms`）。"""

    def __init__(self, batch=None, **kwargs):
        super().__init__(**kwargs)
        self.batch_calls = []
        self.batch = batch

    def move_arms(self, targets_deg, **kwargs):
        self.batch_calls.append(dict(targets_deg))
        if self.batch is not None:
            return dict(self.batch)
        return {"ok": True, "attempts": 1, "reason": "",
                "items": {f"arm:{side}": {"ok": True, "residual": [0.0] * 7, "attempts": 1}
                          for side in targets_deg}}


def test_runner_sends_both_arms_as_one_batch():
    """左右臂**一次都发出去**再一起等（不是"先左到位、再动右"）。

    依据：预设里左右臂是同一时刻的目标，官方手柄示例也是先发两条 `ArmMove`、再一起等
    `arm_move_over_`。逐条走会让镜像动作变成"先左后右"，现场一眼能看出来。
    """
    launcher, clock = _BatchLauncher(), _FakeClock()
    step = motion.arm_preset_steps("A", [{"left_arm": [1.0] * 7, "right_arm": [2.0] * 7}])[0]

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is True
    assert len(launcher.batch_calls) == 1                       # 只发一次，里面两条
    assert set(launcher.batch_calls[0]) == {"left", "right"}
    assert launcher.move_calls == []                            # 没有逐条走
    assert launcher.calls == []                                 # 也不走一次性 CLI
    assert any("左臂已到位" in line for line in result["lines"])
    assert any("右臂已到位" in line for line in result["lines"])


def test_runner_reports_the_arm_that_did_not_arrive():
    """批量里**哪一侧没到就点哪一侧** —— 现场"只有一只手臂动"要靠这条一眼定位。"""
    launcher, clock = _BatchLauncher(batch={
        "ok": False, "attempts": 3, "reason": "右臂下发 3 次后仍未见动作（最大还差 12.5°）",
        "items": {"arm:left": {"ok": True, "residual": [0.1] * 7, "attempts": 1},
                  "arm:right": {"ok": False, "residual": [12.5] * 7, "attempts": 3,
                                "reason": "右臂下发 3 次后仍未见动作（最大还差 12.5°）"}}}), _FakeClock()
    step = motion.arm_preset_steps("A", [{"left_arm": [1.0] * 7, "right_arm": [2.0] * 7}])[0]

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is False and result["stopped_at"] == 1
    text = "\n".join(result["lines"])
    assert "左臂已到位" in text
    assert "右臂没走到位" in text and "12.5" in text


def test_runner_reports_unconfirmed_arm_in_a_batch():
    """批量里某一侧读不到角度 → 那一侧报**未经确认**（不假装两头都到了）。"""
    launcher, clock = _BatchLauncher(batch={
        "ok": None, "attempts": 1, "reason": "读不到右臂的当前角度 —— 是否到位未经确认",
        "items": {"arm:left": {"ok": True, "residual": [0.0] * 7, "attempts": 1},
                  "arm:right": {"ok": None, "residual": None, "attempts": 1}}}), _FakeClock()
    step = motion.arm_preset_steps("A", [{"left_arm": [1.0] * 7, "right_arm": [2.0] * 7}])[0]

    result = _runner(launcher, [step], clock)()

    assert result["ok"] is False
    assert any("右臂未经确认" in line for line in result["lines"])
