"""实时帧率：增量计数、算帧率、以及"清单必须与验证脚本一致"。

## 为什么这个功能要单独测

它读的是**别人正在写的数据库**，而且算出来的数字会直接影响现场判断
（"相机是不是掉帧了"）。两个坑必须钉住：

1. bag 的 `messages.id` 是**全表共享自增**，拿 `MAX(id)` 的差值当帧数是错的
   （实测 340 MB 的真实 bag 上，`MAX(id)` 分组查询还要 371 ms、结果也对不上）。
   所以这里按 `timestamp > 上次上界` 做增量 —— 用例里专门造"多话题交替插入"的库来验。
2. `0.0 Hz` 与 `—`（算不出来）含义完全不同：前者是"相机没出图"（要查），
   后者是"还没采够两次"（等一下就好）。混掉会让现场白折腾。
"""
import sqlite3
from pathlib import Path

import pytest

from data_collector.live_rate import (
    TARGET_TOPICS,
    LiveRateSampler,
    format_rates,
    label_of,
    rate_from_delta,
)


# ---------- 纯函数：帧率怎么算 ----------


def test_rate_is_frames_over_seconds():
    assert rate_from_delta(60, 2.0) == pytest.approx(30.0)
    assert rate_from_delta(3702, 2.0) == pytest.approx(1851.0)


def test_zero_frames_is_zero_not_unknown():
    """**一帧都没有必须报 0.0，不能报"—"**：那正是"相机没出图"这个异常本身。"""
    assert rate_from_delta(0, 2.0) == 0.0


def test_unusable_inputs_give_none_not_zero():
    """时钟没走、或计数倒退（bag 换了）时算不出来 —— 返回 None，不要编一个 0。"""
    assert rate_from_delta(60, 0.0) is None
    assert rate_from_delta(60, -1.0) is None
    assert rate_from_delta(-5, 2.0) is None


# ---------- 清单一致性 ----------


def test_target_topics_match_the_verify_script():
    """界面短标签表必须与 `scripts/bag_rate_stat.py` 的 `TOPICS` 完全一致。

    那边是独立脚本（不 import 本包，要能单独在机器人上跑），所以清单各写一份 ——
    改一边忘另一边，现场就会出现"验证报告里有的、实时看板上没有"。
    """
    import ast

    source = Path("scripts/bag_rate_stat.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # 直接执行与清单有关的那几条赋值（TOPIC_TYPES 引用了 TOPICS，只能一起跑），
    # 避免把话题名手抄一遍 —— 抄错了守卫就失去意义。
    def _touches_topics(node) -> bool:
        """是否给 TOPICS / TOPIC_TYPES 赋值。

        两种写法都要认：`TOPIC_TYPES = dict.fromkeys(...)`（Name 赋值）与
        `TOPIC_TYPES['/joint_states'] = ...`（下标赋值）。漏掉后者时脚本端只有 6 个
        话题，守卫会误报"本包多出 /joint_states"。
        """
        if not isinstance(node, ast.Assign):
            return False
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("TOPICS", "TOPIC_TYPES"):
                return True
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "TOPIC_TYPES"):
                return True
        return False

    assigns = [node for node in tree.body if _touches_topics(node)]
    assert len(assigns) == 3, f"没能在 bag_rate_stat.py 里找全 TOPICS/TOPIC_TYPES 赋值：{len(assigns)}"
    namespace = {}
    exec(compile(ast.Module(body=assigns, type_ignores=[]), "<topics>", "exec"), namespace)

    expected = set(namespace["TOPICS"]) | set(namespace["TOPIC_TYPES"])
    assert expected == set(TARGET_TOPICS), (
        f"清单不一致 —— 验证脚本多出 {expected - set(TARGET_TOPICS)}，"
        f"实时看板多出 {set(TARGET_TOPICS) - expected}")


def test_every_target_topic_has_a_short_label():
    for topic, label in TARGET_TOPICS.items():
        assert label and len(label) <= 4, f"{topic} 的短标签过长：{label}"
    assert label_of("/joint_states") == "关节"
    assert label_of("/unknown/topic") == "/unknown/topic"


# ---------- 读数排版 ----------


def test_format_rates_is_empty_without_data():
    """没有读数 → 空串（界面据此显示"未在录制"，而不是一行破折号）。"""
    assert format_rates({}) == ""


def test_format_rates_distinguishes_unknown_from_zero():
    rates = {topic: None for topic in TARGET_TOPICS}
    rates["/joint_states"] = 0.0
    text = format_rates(rates)

    assert "关节 0.0" in text, "真的 0 帧要显示成 0.0"
    assert "rgb —" in text, "算不出来的要显示成 —"


def test_format_rates_keeps_one_decimal():
    rates = {topic: 30.04 for topic in TARGET_TOPICS}
    text = format_rates(rates)

    assert "rgb 30.0" in text


# ---------- 采样器（模拟录制中的 rosbag2 库） ----------


def _make_bag(path: Path) -> sqlite3.Connection:
    """造一个与 rosbag2 sqlite3 插件同结构的库（含 timestamp 索引）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT)")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, "
        "timestamp INTEGER, data BLOB)")
    conn.execute("CREATE INDEX timestamp_idx ON messages (timestamp ASC)")
    for index, topic in enumerate(TARGET_TOPICS, start=1):
        conn.execute("INSERT INTO topics VALUES (?,?,'x')", (index, topic))
    conn.commit()
    return conn


def _insert(conn, topic_id: int, count: int, start_ns: int, step_ns: int = 33_000_000):
    for i in range(count):
        conn.execute("INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
                     (topic_id, start_ns + i * step_ns, b""))
    conn.commit()


def test_first_sample_only_takes_a_baseline(tmp_path):
    """第一次采样只取基准（没有间隔就不能算帧率），返回空字典。"""
    db3 = tmp_path / "a_0.db3"
    _make_bag(db3).close()

    sampler = LiveRateSampler(db3)
    assert sampler.sample(now=100.0) == {}


def test_second_sample_reports_hz_for_all_targets(tmp_path):
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    conn.close()

    sampler = LiveRateSampler(db3)
    t0 = 1000.0
    sampler.sample(now=t0)

    # 2 秒内每个话题 +60 帧 → 30 Hz。**交替插入**（模拟真实 bag 的共享自增 id），
    # 这样才能证明我们不是拿 MAX(id) 差值当帧数。
    conn = sqlite3.connect(db3)
    for i in range(60):
        for topic_id in range(1, len(TARGET_TOPICS) + 1):
            conn.execute("INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
                         (topic_id, int((t0 + 0.5 + i * (2.0 / 60)) * 1e9), b""))
    conn.commit()
    conn.close()

    rates = sampler.sample(now=t0 + 2.0)

    assert set(rates) == set(TARGET_TOPICS), "每个目标话题都要有读数"
    for topic, value in rates.items():
        assert value == pytest.approx(30.0, abs=0.5), f"{topic} 帧率不对：{value}"


def test_no_new_frames_reports_zero(tmp_path):
    """没有新帧 → 0.0（不是"—"）：这正是"相机停了"的信号。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    _insert(conn, 1, 10, 1_000_000_000)
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)
    rates = sampler.sample(now=12.0)

    assert rates["/vision/rgb"] == 0.0


def test_only_new_frames_are_counted(tmp_path):
    """第二批只数新增的那部分（不能把历史帧再算一遍）。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    _insert(conn, 1, 100, 1_000_000_000)      # 历史 100 帧
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)

    conn = sqlite3.connect(db3)
    _insert(conn, 1, 20, 5_000_000_000)       # 新增 20 帧
    conn.close()

    rates = sampler.sample(now=12.0)
    assert rates["/vision/rgb"] == pytest.approx(10.0, abs=0.6), (
        "只该数新增的 20 帧 / 2 秒 = 10 Hz，不是 120/2")


def test_missing_database_does_not_crash(tmp_path):
    """录制刚开始、db3 还没落盘 → 不抛异常，返回空（界面显示"未在录制"）。"""
    sampler = LiveRateSampler(tmp_path)

    assert sampler.sample(now=1.0) == {}
    assert sampler.sample(now=2.0) == {}


def test_read_failure_keeps_the_previous_reading(tmp_path, monkeypatch):
    """偶发读不到（库被写锁住）时保留上一次读数，界面不要闪成空白。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    _insert(conn, 1, 60, 1_000_000_000)
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)                   # 只取基准
    conn = sqlite3.connect(db3)
    _insert(conn, 1, 60, 9_000_000_000)        # 基准之后才有新帧
    conn.close()
    good = sampler.sample(now=12.0)
    assert good["/vision/rgb"] > 0

    # 真实地制造一次读失败：`sqlite3.Connection.execute` 是只读属性（不能 monkeypatch），
    # 而把连接关掉之后 execute 会抛 `sqlite3.ProgrammingError`（`sqlite3.Error` 的子类），
    # 与录制中偶发的 "database is locked" 走的是同一条兜底分支。
    sampler._files[db3][0].close()

    kept = sampler.sample(now=14.0)
    assert kept == good, "读失败要沿用上一次的读数，而不是清空"


def test_split_bag_files_are_summed(tmp_path):
    """分片录制（_0.db3 / _1.db3）时两片的帧要加起来。"""
    first = tmp_path / "a_0.db3"
    conn = _make_bag(first)
    _insert(conn, 1, 30, 1_000_000_000)       # 采样窗口之前的历史帧
    conn.close()

    sampler = LiveRateSampler(tmp_path)
    sampler.sample(now=10.0)                  # 只取基准（历史帧丢掉）

    # 采样窗口内：老分片继续写 30 帧，同时出现新分片并写入 30 帧
    conn = sqlite3.connect(first)
    _insert(conn, 1, 30, 9_000_000_000)
    conn.close()
    second = tmp_path / "a_1.db3"
    conn = _make_bag(second)
    _insert(conn, 1, 30, 9_000_000_000)
    conn.close()

    rates = sampler.sample(now=12.0)
    assert rates["/vision/rgb"] == pytest.approx(30.0, abs=0.6), (
        "两片各新增 30 帧 / 2 秒 = 30 Hz；漏算新分片会得到 15")


def test_switching_bag_resets_the_baseline(tmp_path):
    """换一场录制（新库）时基准必须重置，否则新库的帧会被算成 0 或负数。"""
    old = tmp_path / "old"
    old.mkdir()
    conn = _make_bag(old / "a_0.db3")
    _insert(conn, 1, 50, 1_000_000_000)
    conn.close()

    sampler = LiveRateSampler(old)
    sampler.sample(now=10.0)
    sampler.sample(now=12.0)

    new = tmp_path / "new"
    new.mkdir()
    conn = _make_bag(new / "a_0.db3")
    _insert(conn, 1, 60, 1_000_000_000)
    conn.close()

    sampler.attach(new)
    assert sampler.sample(now=20.0) == {}, "换库后第一次只该取基准"
    conn = sqlite3.connect(new / "a_0.db3")
    _insert(conn, 1, 60, 9_000_000_000)
    conn.close()
    assert sampler.sample(now=22.0)["/vision/rgb"] == pytest.approx(30.0, abs=0.6)


def test_unknown_topics_are_ignored(tmp_path):
    """bag 里还有别的很多话题（tf / joint_cmds…），只统计目标清单里的。"""
    db3 = tmp_path / "a_0.db3"
    conn = _make_bag(db3)
    conn.execute("INSERT INTO topics VALUES (99,'/tf','x')")
    conn.commit()
    conn.close()

    sampler = LiveRateSampler(db3)
    sampler.sample(now=10.0)

    conn = sqlite3.connect(db3)
    _insert(conn, 99, 200, 9_000_000_000)     # 大量 /tf 消息
    conn.close()

    rates = sampler.sample(now=12.0)
    assert set(rates) == set(TARGET_TOPICS)
    assert rates["/vision/rgb"] == 0.0, "非目标话题不该被算进来"
