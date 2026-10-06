#!/usr/bin/env python3
"""轮式机器人状态监控（常驻订阅者）。

把底盘与 SDK 的状态话题汇总成一份 JSON，供上位机界面直接读取：
    ${ROBOT_STATUS_FILE:-${NERV_DIR}/status/robot_status.json}

订阅的话题（全部来自《底盘ROS2接口》与《轮式机器人SDK接口》文档）：
    /power_report                  电量百分比
    /androidmsg_initstatus         初始化状态   11 失败 / 12 成功
    /androidmsg_emergencystatus    急停状态     31 急停 / 32 解锁
    /androidmsg_chargestatus       充电状态     41..48
    /androidmsg_locationstatus     定位状态     9 位置丢失 / 10 定位成功
    /androidmsg_navigationstatus   导航状态     1..84（见下 NAVIGATION_TEXT）
    /map_status                    地图使用状态 0 导航 / 1 建图 / 2 扩展地图
    /trunk_control/trunk_state     躯干关节角（二期：有数据即已上使能）
    /arm_control/arm_state         双臂连接 / 上电 / 移动 / 急停（二期）

设计要点：
* **原子写**：先写同目录临时文件再 os.replace，界面永远读不到半截 JSON。
* **不依赖 ros2 CLI/daemon**：纯 rclpy，也不用 shell 包一层。
* **收不到的状态就不写**：字段缺失比写一个假值好，界面上显示"未知"而不是"正常"。
* **模块使能状态带时间戳**：`收到过` ≠ `现在是使能状态`，见 `MODULE_STALE_AFTER`。
* **数据源断开要单独标**：文件新鲜 ≠ 读数是活的 —— 没人再发这个话题时写
  `source_lost: true`，见 `SOURCE_LOST_AFTER`（判据是"还有没有发布者"，不是"多久没变"）。
* 需要 ROS 环境（先 source 工作空间的 setup.bash），缺 rclpy 时给出明确提示后退出。

用法：
    source <ROBOT_WORKSPACE>/install/setup.bash
    python3 scripts/robot_monitor.py [--period 0.5] [--output <文件>] [--stale-after 2]
"""
import argparse
import json
import math
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_PERIOD = 0.5        # 状态写盘周期（秒）
STATUS_TOPICS = (
    '/power_report',
    '/androidmsg_initstatus',
    '/androidmsg_emergencystatus',
    '/androidmsg_chargestatus',
    '/androidmsg_locationstatus',
    '/androidmsg_navigationstatus',
    '/map_status',
)

# 枚举文本与 SDK 文档逐字对应，不做"合理推测"
INIT_TEXT = {11: '失败', 12: '成功'}
EMERGENCY_TEXT = {31: '急停', 32: '解锁'}
CHARGE_TEXT = {
    41: '没有充电', 42: '充电中(弃用)', 43: '充电完成(弃用)', 44: '充电失败',
    45: '自动充电中', 46: '手动充电中', 47: '自动充电完成', 48: '手动充电完成',
}
LOCATION_TEXT = {9: '位置丢失', 10: '定位成功'}
MAP_TEXT = {0: '导航', 1: '建图', 2: '扩展地图'}
NAVIGATION_TEXT = {
    1: '开始导航', 2: '导航到目的点成功', 3: '导航失败停止', 4: '局部规划失败进入恢复模式',
    5: '进入地图 CLEANING 模式', 6: '找不到导航点', 7: '停止导航',
    8: '全局规划失败，请让一让', 51: '去充电桩导航失败', 52: '没有设置充电桩',
    81: '导航验证成功', 82: '导航验证失败', 83: '导航取消', 84: '位置已丢失需重新定位',
}

# 字段 -> (话题, 枚举文本表)
FIELDS = {
    'power_percent': ('/power_report', None),
    'init': ('/androidmsg_initstatus', INIT_TEXT),
    'emergency': ('/androidmsg_emergencystatus', EMERGENCY_TEXT),
    'charge': ('/androidmsg_chargestatus', CHARGE_TEXT),
    'location': ('/androidmsg_locationstatus', LOCATION_TEXT),
    'navigation': ('/androidmsg_navigationstatus', NAVIGATION_TEXT),
    'map': ('/map_status', MAP_TEXT),
}

ROS_ENV_HINT = (
    '缺少 ROS 2 Python 环境，脚本需要先 source 机器人工作空间的 setup.bash：\n'
    '  source ${ROBOT_WORKSPACE:-<工作空间>}/install/setup.bash\n'
    '上位机界面启动监控时会自动 source，请确认工作空间里存在 install/setup.bash。'
)

# ============ 模块使能状态（二期） ============
# 「模块是否上使能」怎么做出来的：**靠 state 话题有没有在发**。
# 现场澄清：只要上了使能，躯干就会有角度信息（关节角只在使能后才有）。
# 实测佐证：躯干没使能时 `/trunk_control/trunk_state` 连 Publisher 都没有（count 0）。
#
# 三条设计取舍：
# 1. **只报事实、不下结论**：`has_data` / `age_sec` / 原始状态位都写进 JSON，
#    由界面决定怎么显示。判据本身（enabled）也用最朴素的定义：有数据且够新。
# 2. **必须带"多新"**：`收到过` ≠ `现在是使能状态` —— 节点掉线、上一次会话的残留、
#    使能后又断开，这些都要能区分出来，否则界面会一直显示"已使能"（这类假绿灯最危险）。
# 3. **消息包不在就降级**：`wheeled_robot_msgs` 缺失时整段跳过并如实写 `available: false`，
#    不能因为它而让底盘状态监控整个起不来（同 connect_sequence.py 的做法）。
MODULE_STALE_AFTER = 2.0    # 秒：超过这么久没有新数据，按"当前未使能 / 掉线"处理

# ============ 数据源还在不在（"读数冻结"的判据） ============
# 现场问题：底盘掉线 / 控制节点退出之后，**没有任何人再发这个话题**，但本监控还活着、
# 文件每 0.5s 照写 —— 界面看到的是"文件很新鲜 + 一个冻住的旧读数"，没有任何过期标注。
# 最危险的是电量与急停：会一直显示"未急停"和一个正常电量，其实早就不更新了。
#
# 为什么**不能**拿"字段多久没变"当判据：这些话题是**事件驱动**的（状态不变就不发，
# 见 `_make_callback` 只在值变化时打时间戳），健康时 age 也会一直涨 ——
# 加个阈值只会制造"急停状态已过期"这种假警报，比现状更糟。
# 真正的判据是"这个话题在 DDS 图里还有没有发布者"（`Node.count_publishers`）。
#
# 三条实现取舍：
# 1. **连续 `SOURCE_LOST_AFTER` 秒为 0 才认**：DDS 发现需要几秒（刚使能、刚启动时
#    count 可能瞬时为 0），立刻下结论会在现场闪一条假的"数据源断开"。
# 2. **启动后 `PUBLISHER_GRACE_SEC` 内不下结论**：这段时间里 0 说明不了任何问题。
# 3. **只报事实、缺键即"不知道"**：字段级写 `source_lost: true`；不写这个键就是
#    "没查/正常"，界面据此**不做任何标注** —— 这样旧版监控脚本 + 新版界面也不会误报。
SOURCE_LOST_AFTER = 3.0     # 秒：连续这么久没有发布者，才认"数据源不在了"
PUBLISHER_GRACE_SEC = 10.0  # 秒：监控启动后的发现宽限期，其间不报"数据源断开"
PUBLISHER_SAMPLE_PERIOD = 1.0   # 秒：图查询的最小间隔（别每个写盘周期都查一遍）

try:
    from wheeled_robot_msgs.msg import ArmState, TrunkState
    HAVE_MODULE_STATE = True
except ImportError:                                  # 消息包不在：降级，不影响底盘状态
    ArmState = TrunkState = None
    HAVE_MODULE_STATE = False


def module_state_specs():
    """要订阅的模块 state：(键, 话题, 消息类)。消息包不可用时返回空表。"""
    if not HAVE_MODULE_STATE:
        return ()
    return (
        ('trunk_state', '/trunk_control/trunk_state', TrunkState),
        ('arm_state', '/arm_control/arm_state', ArmState),
    )


def describe_module(key: str, msg) -> dict:
    """把一条 state 消息摊成 JSON 字段；msg 为 None 时返回空（"没有数据"由调用方表达）。"""
    if msg is None:
        return {}
    if key == 'trunk_state':
        # 躯干只有关节角（顺序：脚踝 → 颈部，单位度），它本身就是"已使能"的证据
        return {'joint_deg': [round(float(v), 3) for v in msg.joint_position]}
    if key == 'arm_state':
        def side(prefix):
            entry = {
                'connected': bool(getattr(msg, f'{prefix}_arm_connected')),
                'power_on': bool(getattr(msg, f'{prefix}_arm_power_on')),
                'moving': bool(getattr(msg, f'{prefix}_arm_moving')),
                'emergency_stop': bool(getattr(msg, f'{prefix}_arm_emergency_stop')),
            }
            # 当前关节角：消息里是 `ArmPosition.position[7]`，**弧度**；这里换算成**度**再写盘，
            # 与躯干（度）、与预设文件/教学配置（度）保持同一套单位 —— 界面上不能一半弧度一半度。
            # 读不出就不写这个键（与"收不到的状态就不写"同一条纪律），界面据此显示"未知"。
            positions = getattr(msg, f'{prefix}_arm_pos', None)
            angles = getattr(positions, 'position', None) if positions is not None else None
            try:
                if angles is not None and len(angles) == 7:
                    entry['joint_deg'] = [round(math.degrees(float(v)), 2) for v in angles]
            except (TypeError, ValueError):
                pass
            return entry
        return {'left': side('left'), 'right': side('right')}
    return {}


def module_entry(key: str, msg, age_sec, stale_after: float = MODULE_STALE_AFTER,
                 source_lost: bool = False) -> dict:
    """一个模块的使能事实：有没有数据 / 数据多新 / 是否算"当前使能" / 数据源还在不在。

    `source_lost` 只在**已经有读数**时才可能为真（调用方保证）：
    "从未收到数据"是正常状态（未使能），不该被标成数据源故障。
    """
    enabled = msg is not None and age_sec is not None and age_sec <= stale_after
    entry = {
        'has_data': msg is not None,
        'age_sec': None if age_sec is None else round(float(age_sec), 1),
        'enabled': enabled,
    }
    if source_lost:
        entry['source_lost'] = True
    detail = describe_module(key, msg)

    entry.update(detail)
    if key == 'arm_state' and detail:
        # 双臂各自是否算"已使能"：**连接 + 上电**都满足才算 —— 只有 connected 而没上电，
        # 发 follow 会被 arm_control_node 静默丢弃（启动时序里那条门禁就是等这个）
        entry['enabled_left'] = bool(detail['left']['connected'] and detail['left']['power_on'])
        entry['enabled_right'] = bool(detail['right']['connected'] and detail['right']['power_on'])
    return entry


def subscribed_topics() -> tuple:
    """本监控订阅的全部话题（去重、保持定义顺序）：状态字段表 + 模块 state 表。"""
    topics = [topic for topic, _text in FIELDS.values()]
    topics += [topic for _key, topic, _msg_type in module_state_specs()]
    return tuple(dict.fromkeys(topics))


def source_is_lost(zero_since, now: float, started_at: float,
                   *, lost_after: float = SOURCE_LOST_AFTER,
                   grace: float = PUBLISHER_GRACE_SEC) -> bool:
    """某话题的发布者是否"已经不在了"（纯函数，便于单测）。

    Args:
        zero_since: 本轮"连续没有发布者"的起始时刻；当前有发布者时传 None。
        now: 当前时刻。
        started_at: 监控启动时刻（宽限期从这里算）。

    Returns:
        True = 确认数据源不在（连续为 0 已超过 lost_after，且已过启动宽限期）。
    """
    if zero_since is None:
        return False
    if now - started_at < grace:
        return False
    return (now - zero_since) >= lost_after


def default_output() -> Path:
    """状态文件位置：${ROBOT_STATUS_FILE} > ${NERV_DIR}/status/robot_status.json > /var/nerv/status/..."""
    explicit = os.environ.get('ROBOT_STATUS_FILE')
    if explicit:
        return Path(explicit).expanduser()
    nerv_dir = os.environ.get('NERV_DIR') or '/var/nerv'
    return Path(nerv_dir).expanduser() / 'status' / 'robot_status.json'


def require_ros_environment():
    for module in ('rclpy',):
        try:
            __import__(module)
        except ImportError as exc:
            sys.exit(f'{ROS_ENV_HINT}\n(导入 {module} 失败: {exc})')


def describe(raw, text_map):
    """把枚举值翻译成文本；表里没有的值原样保留，不猜。"""
    if raw is None:
        return None
    if text_map is None:
        return raw
    return text_map.get(int(raw), f'未知({raw})')


def write_atomic(path: Path, payload: dict) -> None:
    """原子替换，避免界面读到写了一半的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=str(path.parent),
        prefix=path.name + '.', suffix='.tmp', delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RobotMonitor:
    """订阅状态话题并定期落盘。"""

    def __init__(self, output: Path, period: float,
                 stale_after: float = MODULE_STALE_AFTER):
        import rclpy
        from rclpy.node import Node
        from std_msgs.msg import Int16

        self.output = output
        self.period = period
        self.stale_after = stale_after
        self.raw = {}          # 字段名 -> 原始数值
        self.stamps = {}       # 字段名 -> 该字段最后一次收到的时间
        self.modules = {}      # 模块键 -> 最后一条 state 消息（躯干 / 手臂）
        self.module_stamps = {}   # 模块键 -> 最后一次收到的时间
        self.started_at = time.time()
        self._dirty = True
        # 数据源监视：话题 -> 本轮"连续没有发布者"的起始时刻（有发布者时该键不存在）
        self.topics = subscribed_topics()
        self._zero_since = {}
        self._last_sample = 0.0

        rclpy.init()
        self.node = Node('data_collector_robot_monitor')
        self._Int16 = Int16

        subscribed = set()
        for field, (topic, _text) in FIELDS.items():
            if topic in subscribed:
                continue
            subscribed.add(topic)
            self.node.create_subscription(
                Int16, topic,
                self._make_callback(field),
                # 状态话题是低频标量，用默认 reliable/keep_last 即可
                10,
            )

        self._subscribe_module_states()

        self.node.get_logger().info(
            f'监听 {len(subscribed)} 个状态话题，每 {period}s 写入 {output}'
            + ('' if HAVE_MODULE_STATE else '（未找到 wheeled_robot_msgs，跳过模块使能状态）')
        )
        self.node.create_timer(period, self.flush)

    def _subscribe_module_states(self):
        """订阅躯干 / 手臂的 state（二期：判断模块是否上使能）。"""
        specs = module_state_specs()
        if not specs:
            return
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        # BEST_EFFORT × depth 1：发布端是 reliable 还是 best_effort 都能收到；
        # 状态只关心"最新的那条"，队列深了也没意义（与 connect_sequence.py 的订阅一致）
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        for key, topic, msg_type in specs:
            self.node.create_subscription(
                msg_type, topic, self._make_module_callback(key), qos)

    def _make_module_callback(self, key):
        def callback(msg):
            self.modules[key] = msg
            self.module_stamps[key] = time.time()
            self._dirty = True
        return callback

    def _make_callback(self, field):
        def callback(msg):
            value = int(msg.data)
            if self.raw.get(field) != value:
                self.raw[field] = value
                self.stamps[field] = time.time()
                self._dirty = True
        return callback

    def sample_publishers(self, now: float) -> None:
        """采样"这些话题还有没有发布者"（见 SOURCE_LOST_AFTER 的三条取舍）。

        刻意限流（`PUBLISHER_SAMPLE_PERIOD`）：`count_publishers` 是一次 DDS 图查询，
        按写盘周期（0.5s）× 9 个话题去查纯属浪费；判据本身要的是"连续几秒为 0"，
        1 秒采一次完全够（3 秒阈值 → 最多 3 次采样就能确认）。
        """
        if now - self._last_sample < PUBLISHER_SAMPLE_PERIOD:
            return
        self._last_sample = now
        for topic in self.topics:
            try:
                count = int(self.node.count_publishers(topic))
            except Exception as exc:            # 图查询失败：当作"不知道"，不改变已有判断
                self.node.get_logger().debug(f'查询发布者失败（{topic}）：{exc}')
                continue
            if count > 0:
                self._zero_since.pop(topic, None)
            else:
                self._zero_since.setdefault(topic, now)

    def _source_lost(self, topic: str, now: float) -> bool:
        return source_is_lost(self._zero_since.get(topic), now, self.started_at)

    def build_payload(self) -> dict:
        """has_data 明确区分「收到 0」和「从没收到」——0 电量是有效值。

        字段级 `source_lost`：本字段的数据源是否已确认不在（见 SOURCE_LOST_AFTER）。
        只在为真时才写这个键 —— 缺键表示"不知道/正常"，界面据此不做任何标注。
        """
        now = time.time()
        status = {}
        for field, raw in self.raw.items():
            text_map = FIELDS[field][1]
            item = {
                'raw': raw,
                'text': describe(raw, text_map),
                'age_sec': round(time.time() - self.stamps.get(field, self.started_at), 1),
            }
            if self._source_lost(FIELDS[field][0], now):
                item['source_lost'] = True
            status[field] = item
        return {
            'updated_at': now,
            'started_at': self.started_at,
            'monitor_pid': os.getpid(),
            'has_data': bool(self.raw),
            'status': status,
            'module_state': self.build_module_state(),
        }


    def build_module_state(self) -> dict:
        """模块使能状态（二期）：`trunk_state` / `arm_state` 两条各自的事实。

        键值平铺在同一层（不再套一层 `modules`）：界面直接 `payload['module_state']['arm_state']`
        就能取，少一层最容易写错的嵌套。
        """
        now = time.time()
        payload = {
            'available': HAVE_MODULE_STATE,
            'stale_after': self.stale_after,
            'note': None if HAVE_MODULE_STATE
                    else '未找到 wheeled_robot_msgs，无法读取模块使能状态',
        }
        for key, topic, _msg_type in module_state_specs():
            msg = self.modules.get(key)
            age = None if msg is None else now - self.module_stamps.get(key, now)
            # 只在**已经有读数**时才可能标"数据源断开"：从未收到数据是正常状态
            # （模块没上使能就是这么显示的），标成故障会造成满屏假警告。
            payload[key] = module_entry(
                key, msg, age, self.stale_after,
                source_lost=msg is not None and self._source_lost(topic, now),
            )
        return payload

    def flush(self):
        self.sample_publishers(time.time())
        try:
            write_atomic(self.output, self.build_payload())
        except OSError as exc:
            self.node.get_logger().warning(f'状态写盘失败：{exc}')


    def spin(self):
        import rclpy
        try:
            rclpy.spin(self.node)
        except KeyboardInterrupt:
            pass
        finally:
            # 结束时再写一次，把"监控已退出"这件事留给界面判断（updated_at 会变旧）
            self.flush()
            self.node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=None,
                        help='状态 JSON 输出路径，默认 ${NERV_DIR}/status/robot_status.json')
    parser.add_argument('--period', type=float, default=DEFAULT_PERIOD,
                        help=f'写盘周期（秒），默认 {DEFAULT_PERIOD}')
    parser.add_argument('--stale-after', type=float, default=MODULE_STALE_AFTER,
                        help='模块 state 多久没更新就算"当前未使能/掉线"（秒），'
                             f'默认 {MODULE_STALE_AFTER}')
    args = parser.parse_args()

    require_ros_environment()

    output = (args.output or default_output()).expanduser()
    monitor = RobotMonitor(output, max(args.period, 0.1),
                           stale_after=max(args.stale_after, 0.5))

    # 让 systemd/父进程发 SIGTERM 也能优雅退出（rclpy.spin 只捕 KeyboardInterrupt）
    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    monitor.spin()
    return 0


if __name__ == '__main__':
    sys.exit(main())
