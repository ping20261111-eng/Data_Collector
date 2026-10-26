"""实时帧率：按「数据验证」的分组算，以及不依赖 ROS 的关节名解析。

## 为什么要测这一层

1. **分组口径**必须与验证报告/图一致：`/joint_states` 在那边是按**完整关节名集合**
   拆开的（左臂/右臂约 999 Hz、左手/右手 100 Hz、躯干 30 Hz，差一个数量级）。
   只看话题总数会把这种差异完全掩盖 —— 所以这里逐条解析关节名。
2. **关节名解析不能依赖 ROS**：上位机是 Qt 进程，不装 rclpy。手工解析 CDR 的
   正确性必须钉住（与 `deserialize_message` 逐条对照过 3000/3000 一致）。
3. `0.0` 与 `—` 含义不同：前者是"这一路一帧没出"（要查），后者是"还没采够两次"。
"""
import sqlite3
import struct
from pathlib import Path

import pytest

from data_collector.live_rate import (
    IMAGE_LABELS,
    JOINT_TOPIC,
    STATE_MEASURING,
    STATE_OK,
    STATE_WAITING,
    LiveRateSampler,
    format_rates,
    group_sort_key,
    joint_group_label,
    joint_names,
    rate_from_delta,
)


# ---------- 造一条 JointState 的 CDR 字节（只到 name 列表，够解析即可） ----------


def build_joint_state(names, frame_id="base_link", sec=0, nanosec=0) -> bytes:
    """按 rosbag2 存的 CDR 布局造前缀：

    4 字节封装头 → int32 sec → uint32 nanosec → uint32 frame_id 长度 + 字节
    → 对齐 → uint32 name 个数 → 每个 name = uint32 长度 + 字节（含 `\\0`），各自对齐。
    """
    out = bytearray(b"\x00\x01\x00\x00")
    out += struct.pack("<iI", sec, nanosec)
    frame = frame_id.encode() + b"\x00"
    out += struct.pack("<I", len(frame)) + frame
    while len(out) % 4:
        out += b"\x00"
    out += struct.pack("<I", len(names))
    for name in names:
        raw = name.encode() + b"\x00"
        out += struct.pack("<I", len(raw)) + raw
        while len(out) % 4:
            out += b"\x00"
    return bytes(out)


# ---------- 关节名解析（纯函数） ----------


def test_joint_names_reads_the_name_list():
    names = ("AR5_Ljoint_1", "AR5_Ljoint_2", "AR5_Ljoint_3")
    assert joint_names(build_joint_state(names)) == names


def test_joint_names_handles_variable_length_frames():
    """frame_id 长度变化要能正确对齐（对齐是这里最容易算错的地方）。"""
    assert joint_names(build_joint_state(("a",), frame_id="")) == ("a",)
    assert joint_names(build_joint_state(("a",), frame_id="x" * 33)) == ("a",)


def test_joint_names_returns_empty_on_garbage():
    """脏数据不能抛异常：采样循环里一条坏消息不该打断整轮。"""
    assert joint_names(b"") == ()
    assert joint_names(b"\x00\x01\x00\x00") == ()
    assert joint_names(b"\xff" * 40) == ()


def test_joint_names_matches_rclpy_when_available():
    """与 rclpy 的 `deserialize_message` 对照。

    只在装了 ROS 的环境里跑（上位机本身不需要它，但**这条对照是手工解析的正确性来源**）。
    """
    rclpy_ser = pytest.importorskip("rclpy.serialization")
    sensor = pytest.importorskip("sensor_msgs.msg")

    msg = sensor.JointState()
    msg.name = ["AR5_Ljoint_1", "AR5_Ljoint_2", "trunk_1"]
    msg.position = [0.1, 0.2, 0.3]
    blob = rclpy_ser.serialize_message(msg)     # 签名只收消息对象

    assert joint_names(blob) == tuple(msg.name)


# ---------- 分组标签 ----------


def test_joint_group_label_maps_the_five_groups():
    assert joint_group_label(["AR5_Ljoint_1", "AR5_Ljoint_2"]) == "左臂"
    assert joint_group_label(["AR5_Rjoint_1", "AR5_Rjoint_2"]) == "右臂"
    assert joint_group_label(["LJoint_1", "LJoint_2"]) == "左手"
    assert joint_group_label(["RJoint_1", "RJoint_2"]) == "右手"
    assert joint_group_label(["trunk_1", "head_1"]) == "躯干"


def test_left_arm_is_not_mistaken_for_left_hand():
    """**最容易错的一条**：`AR5_Ljoint` 与 `LJoint` 只差一个字母的大小写。

    左臂是 999 Hz、左手是 100 Hz，判错的话现场会拿错数去看错机构。
    这条同时钉住"别改成大小写不敏感比较"（把 token 与关节名都 `.lower()`
    之后 `ljoint` 会命中 `ar5_ljoint`，左臂就被贴成左手了）。
    """
    assert joint_group_label(["AR5_Ljoint_7"]) == "左臂"
    assert joint_group_label(["LJoint_7"]) == "左手"


def test_joint_group_label_is_order_insensitive():
    """关节名顺序不影响分组（与验证脚本的 `sorted(msg.name)` 同口径）。"""
    a = joint_group_label(["AR5_Ljoint_2", "AR5_Ljoint_1"])
    b = joint_group_label(["AR5_Ljoint_1", "AR5_Ljoint_2"])
    assert a == b == "左臂"


def test_unknown_joint_set_gets_a_fallback_label():
    assert joint_group_label(["something_else"]) == "其它关节"
    assert joint_group_label([]) == "其它关节"


def test_group_sort_key_puts_cameras_first_then_joints():
    ordered = sorted(["躯干", "右腕·彩", "左臂", "面部·彩"], key=group_sort_key)

    assert ordered == ["面部·彩", "右腕·彩", "左臂", "躯干"]


# ---------- 帧率与排版 ----------


def test_rate_is_frames_over_seconds():
    assert rate_from_delta(60, 2.0) == pytest.approx(30.0)
    assert rate_from_delta(1999, 2.0) == pytest.approx(999.5)


def test_zero_frames_is_zero_not_unknown():
    """一帧都没有必须报 0.0：那正是"这一路没出图"这个异常本身。"""
    assert rate_from_delta(0, 2.0) == 0.0


def test_unusable_inputs_give_none_not_zero():
    assert rate_from_delta(60, 0.0) is None
    assert rate_from_delta(60, -1.0) is None
    assert rate_from_delta(-5, 2.0) is None


def test_format_rates_is_empty_without_data():
    assert format_rates({}) == ""


def test_format_rates_distinguishes_unknown_from_zero():
    text = format_rates({"左臂": 999.4, "右手": 0.0, "躯干": None})

    assert "左臂 999.4" in text
    assert "右手 0.0" in text, "真的 0 帧要显示成 0.0"
    assert "躯干 —" in text, "算不出来的要显示成 —"


def test_format_rates_wraps_into_several_lines():
    """分组多的时候要换行 —— 一行塞 11 个分组会被日志区右边裁掉。"""
    rates = {label: 30.0 for label in
             ("面部·彩", "面部·深", "左腕·彩", "左腕·深", "右腕·彩", "右腕·深",
              "左臂", "右臂", "左手", "右手", "躯干")}
    text = format_rates(rates)

    assert text.count("\n") >= 2, f"11 个分组应该折成多行：{text}"
    assert all(line.count(" · ") <= 3 for line in text.splitlines())


# ---------- 采样器（模拟录制中的 rosbag2 库） ----------


def _make_bag(path: Path) -> sqlite3.Connection:
    """造一个与 rosbag2 sqlite3 插件同结构的库（含 timestamp 索引）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, "
                 "timestamp INTEGER, data BLOB)")
    conn.execute("CREATE INDEX timestamp_idx ON messages (timestamp ASC)")
    for index, topic in enumerate(list(IMAGE_LABELS) + [JOINT_TOPIC, "/tf"], start=1):
        conn.execute("INSERT INTO topics VALUES (?,?,'x')", (index, topic))
    conn.commit()
    return conn


def _topic_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()[0]


def _insert(conn, topic_id: int, count: int, start_ns: int, step_ns: int = 33_000_000,
            payload: bytes = b""):
    for i in range(count):
        conn.execute("INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
                     (topic_id, start_ns + i * step_ns, payload))
    conn.commit()
    return start_ns + count * step_ns


# ---------- 分片尚未就绪（v6.7.7 现场「一直正在测量」） ----------

def test_pending_fragment_does_not_stop_the_whole_board(tmp_path):
    """一个还没就绪的分片，**不许**让整块看板停在"正在测量"。

    现场 2026-10-26：录制中界面一直显示"正在测量…"，一个数字都没有。
    根因是 `attach()` 用"已成功打开的文件"判换库 —— rosbag2 刚创建分片时表还没建，
    这个文件打不开，于是 `found != 已打开集合` 恒成立，**每轮都 close() 重置基准**，
    `_last_at` 永远是 None，永远走"第一次只取基准"的分支。

    实测：1 个正常 db3 + 1 个空文件，连跑四次全是 0 项；改成按路径判换库后
    第二次就出 30 Hz。
    """
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()
    (tmp_path / "a_1.db3").write_bytes(b"")        # 刚创建的分片：表还没建

    sampler = LiveRateSampler(tmp_path)
    # **按界面的真实序列**：每 2 秒先 attach 再 sample。少了这一步就复现不出
    # 现场 bug —— "每轮 attach 都重置基准"正是发生在第二次及以后的 attach 上。
    sampler.attach(tmp_path)
    sampler.sample(now=10.0)                       # 只取基准
    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)
    conn.close()

    sampler.attach(tmp_path)                       # 2 秒后的下一轮
    rates = sampler.sample(now=12.0)

    assert rates["面部·彩"] == pytest.approx(30.0, abs=0.7), (
        f"未就绪分片不该影响其余分片的读数：{rates}")
    assert sampler.state == STATE_OK


def test_all_fragments_pending_is_waiting_and_never_a_fake_zero(tmp_path):
    """所有分片都还没就绪 → 说"等待"，**绝不输出 0**。

    0 在现场的含义是"这一路一帧都没出"（要去查相机）。把"还没写进来"报成 0，
    会让人白拆一次相机 —— 这个区分是这一版专门加的状态机。
    """
    (tmp_path / "a_0.db3").write_bytes(b"")

    sampler = LiveRateSampler(tmp_path)
    for index in range(3):
        rates = sampler.sample(now=10.0 + index * 2)

    assert rates == {}, f"未就绪时不许产出读数（更不能是 0）：{rates}"
    assert sampler.state == STATE_WAITING
    assert "尚未就绪" in sampler.last_note, sampler.last_note


def test_attach_does_not_reset_the_baseline_for_the_same_path(tmp_path):
    """同一个目录再 attach 一次，**不许**重置时间基准（否则永远出不了读数）。

    目录里还放一个**打不开的分片**：现场就是这样 —— rosbag2 刚创建 `_1.db3` 时
    表还没建。旧实现拿"已成功打开的文件集合"判换库，于是每轮 attach 都把基准
    清掉，界面永远停在"正在测量"。
    """
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()
    (tmp_path / "a_1.db3").write_bytes(b"")

    sampler = LiveRateSampler(tmp_path)
    assert sampler.attach(tmp_path) is True
    sampler.sample(now=10.0)
    baseline = sampler._last_at
    assert baseline == 10.0

    assert sampler.attach(tmp_path) is True          # 每 2 秒都会调一次
    assert sampler._last_at == baseline, "同路径重复 attach 重置了基准 → 永远出不了数"

    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)
    conn.close()
    assert sampler.sample(now=12.0)["面部·彩"] == pytest.approx(30.0, abs=0.7)


def test_state_reports_measuring_then_ok(tmp_path):
    """状态要能区分"已有基准但还没读数"和"已有读数" —— 界面靠它选文案。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()

    sampler = LiveRateSampler(tmp_path)
    sampler.sample(now=10.0)
    assert sampler.state == STATE_MEASURING

    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)
    conn.close()
    sampler.sample(now=12.0)
    assert sampler.state == STATE_OK


def test_a_file_that_cannot_be_read_is_not_counted_as_data(tmp_path):
    """目录里一个 db3 都不能读时，不能因为"文件存在"就当作有数据。"""
    (tmp_path / "a_0.db3").write_bytes(b"not a database at all")

    sampler = LiveRateSampler(tmp_path)
    rates = sampler.sample(now=10.0)

    assert rates == {}
    assert sampler.state == STATE_WAITING


def test_second_sample_splits_joint_states_into_groups(tmp_path):
    """**核心**：一个 `/joint_states` 话题要按关节名拆成多组，各自算帧率。

    真实情况就是这样：左臂/右臂 999 Hz、左手/右手 100 Hz、躯干 30 Hz，
    合成一个总数会把"某一路掉帧"完全掩盖。
    """
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    joint = _topic_id(conn, JOINT_TOPIC)
    conn.close()

    sampler = LiveRateSampler(db3)
    t0 = 1000.0
    sampler.sample(now=t0)                      # 只取基准

    conn = sqlite3.connect(db3)
    left_arm = build_joint_state(("AR5_Ljoint_1", "AR5_Ljoint_2"))
    left_hand = build_joint_state(("LJoint_1", "LJoint_2"))
    trunk = build_joint_state(("trunk_1",))
    base = int((t0 + 0.1) * 1e9)
    _insert(conn, joint, 60, base, step_ns=33_000_000, payload=left_arm)     # 30 Hz
    _insert(conn, joint, 20, base, step_ns=100_000_000, payload=left_hand)   # 10 Hz
    _insert(conn, joint, 6, base, step_ns=333_000_000, payload=trunk)        # 3 Hz
    conn.close()

    rates = sampler.sample(now=t0 + 2.0)

    assert rates["左臂"] == pytest.approx(30.0, abs=0.7)
    assert rates["左手"] == pytest.approx(10.0, abs=0.7)
    assert rates["躯干"] == pytest.approx(3.0, abs=0.7)


def test_camera_topics_are_counted_by_topic(tmp_path):
    """图像话题不需要反序列化，按话题直接计数。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)       # 60 帧 / 2 秒 = 30 Hz
    conn.close()

    assert sampler.sample(now=12.0)["面部·彩"] == pytest.approx(30.0, abs=0.7)


def test_groups_that_disappear_report_zero_not_missing(tmp_path):
    """某个分组这一窗口没有帧 → 报 0.0 并且**仍在**（不能整行消失）。

    整行消失会让人以为"这一路没录"，而实际是"录了但没数据" —— 含义不同。
    """
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    joint = _topic_id(conn, JOINT_TOPIC)
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    conn = sqlite3.connect(db3)
    _insert(conn, joint, 30, 9_000_000_000, payload=build_joint_state(("LJoint_1",)))
    conn.close()
    first = sampler.sample(now=12.0)
    assert first["左手"] > 0

    second = sampler.sample(now=14.0)           # 下一个窗口一帧都没有
    assert second["左手"] == 0.0, "消失的分组要报 0，不能从读数里去掉"


def test_all_camera_groups_appear_even_without_frames(tmp_path):
    """6 路相机分组**固定出现**，没帧就是 0.0。

    这是验证最关心的一件事：某个 depth 话题压根没录进来（验证报告叫"缺失目标话题"）时，
    界面上必须能一眼看到那条 0，而不是"少一行"就此消失。
    """
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)      # 只给 rgb，其余 5 路都没有
    conn.close()

    rates = sampler.sample(now=12.0)

    assert set(IMAGE_LABELS.values()) <= set(rates), "6 路相机分组都要在读数里"
    assert rates["面部·彩"] == pytest.approx(30.0, abs=0.7)
    assert rates["面部·深"] == 0.0, "没数据的分组要报 0，不能缺席"


def test_non_target_topics_are_ignored(tmp_path):
    """/tf、joint_cmds 这些不在验证口径里，不该出现在读数中。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    tf = _topic_id(conn, "/tf")
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    conn = sqlite3.connect(db3)
    _insert(conn, tf, 500, 9_000_000_000)
    conn.close()

    rates = sampler.sample(now=12.0)

    # 只剩"固定的 6 路相机分组"（都报 0），没有任何 /tf 的痕迹
    assert set(rates) == set(IMAGE_LABELS.values()), rates
    assert all(value == 0.0 for value in rates.values()), (
        f"500 条 /tf 不该被算进任何分组：{rates}")


def test_split_bag_files_are_summed(tmp_path):
    """分片录制时两片的帧要加起来（新分片从 0 起算，不能被当历史丢掉）。"""
    first = tmp_path / "a_0.db3"
    conn = _make_bag(first)
    rgb = _topic_id(conn, "/vision/rgb")
    _insert(conn, rgb, 30, 1_000_000_000)       # 窗口之前的历史帧
    conn.close()

    sampler = LiveRateSampler(tmp_path)
    sampler.sample(now=10.0)                    # 基准

    conn = sqlite3.connect(first)
    _insert(conn, rgb, 30, 9_000_000_000)
    conn.close()
    second = tmp_path / "a_1.db3"
    conn = _make_bag(second)
    _insert(conn, _topic_id(conn, "/vision/rgb"), 30, 9_000_000_000)
    conn.close()

    assert sampler.sample(now=12.0)["面部·彩"] == pytest.approx(30.0, abs=0.7)


def test_missing_database_does_not_crash(tmp_path):
    sampler = LiveRateSampler(tmp_path)

    assert sampler.sample(now=1.0) == {}
    assert sampler.sample(now=2.0) == {}


def test_read_failure_keeps_the_previous_reading(tmp_path):
    """偶发读不到（库被写锁住）时保留上一次读数，不闪成空白也不跳成 0。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    rgb = _topic_id(conn, "/vision/rgb")
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    conn = sqlite3.connect(db3)
    _insert(conn, rgb, 60, 9_000_000_000)
    conn.close()
    good = sampler.sample(now=12.0)
    assert good["面部·彩"] > 0

    # 制造真实读失败：连接关掉后 execute 抛 sqlite3.ProgrammingError
    sampler._files[db3][0].close()

    assert sampler.sample(now=14.0) == good


def test_switching_bag_resets_the_baseline(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    conn = _make_bag(old / "a_0.db3")
    _insert(conn, _topic_id(conn, "/vision/rgb"), 50, 1_000_000_000)
    conn.close()

    sampler = LiveRateSampler(old)
    sampler.sample(now=10.0)
    sampler.sample(now=12.0)

    new = tmp_path / "new"
    new.mkdir()
    _make_bag(new / "a_0.db3").close()

    sampler.attach(new)
    assert sampler.sample(now=20.0) == {}, "换库后第一次只该取基准"
