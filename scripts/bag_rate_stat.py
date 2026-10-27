#!/usr/bin/env python3
"""Offline full-bag verification (数据验证).
Usage: python3 scripts/bag_rate_stat.py BAG_DIRECTORY [--report FILE]
All recorded frames of the seven target topics are analyzed, without resampling.
Only header.stamp is analyzed; all plots share one window without pagination.
A plain-text report is written **into the bag directory** by default
(`数据验证结果_<YYYYmmdd-HHMMSS>.txt`), so the result travels with the data.
FILE zstd bags expand beside the compressed file; existing expanded files are reused.
Requires the ROS environment of the robot workspace
(source ${ROBOT_WORKSPACE:-<工作空间>}/install/setup.bash), python3-matplotlib,
zstd and fonts-noto-cjk.
"""
import argparse
from collections import defaultdict, namedtuple
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

TOPICS = ['/vision/rgb', '/vision/depth'] + [
    f'/camera_{side}/{stream}' for side in ('left', 'right')
    for stream in ('color/image_rect_raw', 'aligned_depth_to_color/image_raw')]

TOPIC_TYPES = dict.fromkeys(TOPICS, 'sensor_msgs/msg/Image')
TOPIC_TYPES['/joint_states'] = 'sensor_msgs/msg/JointState'

ROS_ENV_HINT = ('缺少 ROS 2 Python 环境，脚本需要先 source 机器人工作空间的 setup.bash：\n'
                '  source ${ROBOT_WORKSPACE:-<工作空间>}/install/setup.bash\n'
                '上位机界面点击「数据验证」时会自动 source，请确认工作空间里存在 install/setup.bash。')


#: 报告文件名格式。**与 `data_collector/verify.py` 的同名常量必须一致** ——
#: 应用侧要靠它算出"结果会落在哪"并写进日志，而脚本要在没带 `--report` 时自己定名。
#: 两份字面量是刻意的：这个脚本要能**独立**在机器人上跑（不 import 我们的包）。
REPORT_NAME_FMT = '数据验证结果_%Y%m%d-%H%M%S.txt'


#: 报告里的判定行前缀与取值。上位机按它判断"这批数据合格吗"。
#:
#: ⚠️ **不要按行号读判定**：报告正文会随需求增删，行号一定会漂。
#: 上位机侧 `data_collector/verify.py` 有一份同名字面量，两者必须逐字一致 ——
#: 脚本要能**独立**在机器人上跑（不 import 本包），所以刻意不合并。
#: 守卫：`test_bag_verdict_words_match_between_script_and_app`。
VERDICT_PREFIX = '判定: '
VERDICT_PASS = '合格'
VERDICT_FAIL = '不合格'


#: 各部位的合格频率下限。**这是现场给的标尺（2026-10-27），不是随手定的**：
#:
#: | 部位 | 标称 | 允许偏差 | 判定下限 |
#: | --- | --- | --- | --- |
#: | 相机 | 30 Hz | — | 30 Hz |
#: | 手臂 | 1000 Hz | ±5% | **950 Hz** |
#: | 手部 | 100 Hz | — | 100 Hz |
#:
#: 三条口径上的取舍：
#:
#: 1. **只判下限、不判上限**。超频不影响数据可用性，丢帧才是问题；
#:    给上限只会在标称值附近制造无意义的误判。
#: 2. 相机与手部现场只给了标称值、没给容差，所以这里容差取 0（低于标称即不合格）。
#:    若之后发现太严（统计误差会让 30 Hz 实测出 29.9），改这张表的第三列即可，
#:    不要在判定逻辑里另加特例。
#: 3. **躯干/头部不在这张表里**：现场没给它的标称频率。拿一个猜出来的数字
#:    去判"不合格"，比不判更糟 —— 但它的 `max` 间隔仍受 `MAX_INTERVAL_MS` 约束。
#:
#: 真实的关节名（2026-10-27 从机器人侧配置核对过）：
#: 手臂 `AR5_Ljoint_1` … `AR5_Ljoint_7` / `AR5_Rjoint_1` …，手部 `LJoint1` / `RJoint1`。
#:
#: 两组 token **大小写不同**（`Ljoint` vs `LJoint`），所以本来就不会互相误匹配 ——
#: 这里仍然把手臂排在前面，只是因为"更具体的 token 先匹配"是更稳的习惯
#: （将来若有人把某一侧改名成大小写一致的写法，也不会悄悄串味）。
#: 顺序与 `display_name` 保持一致，便于对照报告里的标签。
RATE_TARGETS = (
    ('/vision/', '相机', 30.0, 0.0),
    ('/camera_', '相机', 30.0, 0.0),
    ('AR5_Ljoint', '手臂', 1000.0, 0.05),
    ('AR5_Rjoint', '手臂', 1000.0, 0.05),
    ('LJoint', '手部', 100.0, 0.0),
    ('RJoint', '手部', 100.0, 0.0),
)

#: 任何分组的**最大帧间隔**上限（毫秒）。现场硬要求：任一项都不得超过 150 ms。
#: 这条对**所有**分组生效（含躯干/头部这类没有标称频率的），因为它衡量的是
#: "有没有一段长时间断流"，与频率高低无关。
MAX_INTERVAL_MS = 150.0


def rate_standard_text():
    """一句话概括频率标尺。报告和工具的提示语都用它 —— 数字只从 RATE_TARGETS 出。

    第一版把「相机 30 Hz / 手部 100 Hz」直接写进了报告文案，而同一条结论里
    手臂的下限又是从表里取 `rate_target_for('/vision/x')`（取错了部位，拿成 30）。
    两处各自拼数字必然对不上，所以统一到这里生成。
    """
    camera = rate_target_for('/vision/x')
    arm = rate_target_for('AR5_Ljoint')
    hand = rate_target_for('LJoint')
    return (f'相机 {camera[2]:g} Hz / 手臂 ≥{arm[1]:g} Hz'
            f'（{arm[2]:g} Hz 允许 ±{arm[3] * 100:g}%）/ 手部 {hand[2]:g} Hz')


def rate_target_for(key):
    """这个分组属于哪个部位、合格下限是多少。

    Returns:
        `(部位, 下限 Hz, 标称 Hz, 容差比例)` 或 `None`（该部位没有标称频率）。
    """
    for token, label, hz, tolerance in RATE_TARGETS:
        if token in key:
            return label, hz * (1.0 - tolerance), hz, tolerance
    return None


#: 一次判定的完整结果。用命名元组而不是裸元组：调用方要同时看
#: 频率不达标与间隔超限两类明细，位置元组到第 5 个字段就没人记得住了。
Verdict = namedtuple('Verdict', 'ok detail missing bad slow spiky')


def evaluate_verdict(results, samples, topic_types=None):
    """判定一次验证是"合格"还是"不合格"，并给出原因摘要。

    ## 合格的定义（v6.9.0 起这是**产品定义**，不是随手写的比较条件）

    两条同时满足才算合格：

    1. 每个目标话题都有数据（缺失 = 整段没录到，下游训练会缺一路输入）；
    2. 每个分组的帧间隔都能算（≥2 帧），且没有非递增时间戳
       （时间戳乱序 / 时钟跳变会让时间对齐失效）。

    这两条正好对应现场最常出现的两类坏数据，所以"不合格"这个标记要能让人
    回头去查具体是哪一条。

    ## 为什么必须是纯函数、且只此一处实现

    判定结果有**三个消费方**：报告里的判定行、上位机列表里的标记、测试里的期望。
    三处各写一套比较条件的话，迟早出现"报告说合格、列表显示不合格"——
    而这种不一致比任何一方单独出错都更难查（两边都是"照代码来说没错"）。

    Returns:
        `(ok, detail, missing, bad)`：
        `detail` 是给 tooltip 看的一句话（合格时为空串）。
    """
    topic_types = TOPIC_TYPES if topic_types is None else topic_types

    # ⚠️ 不能直接 `key not in results`：`/joint_states` 会被按关节名集合**分组**
    # 成 `/joint_states [a,b,...]` 这样的键。判"这个话题有没有数据"必须与
    # `analyze_bag` 里那条打印逻辑一致（`key == topic or key.startswith(topic + ' [')`），
    # 否则报告会写出「有数据 -4」这种自相矛盾的数字。
    def _has_data(topic):
        return any(key == topic or key.startswith(topic + ' [') for key in results)

    missing = [topic for topic in topic_types if not _has_data(topic)]
    bad = [key for key, stat in sorted(results.items())
           if not stat or stat['nonpositive']]

    slow, spiky = [], []
    for key, stat in sorted(results.items()):
        if not stat:
            continue                       # 帧数不足：已在 bad 里，算不出频率
        label = display_name(key)
        target = rate_target_for(key)
        if target is not None and stat['hz'] is not None:
            _part, floor, _nominal, _tolerance = target
            if stat['hz'] < floor:
                slow.append(f'{label} {stat["hz"]:.1f}Hz < {floor:g}Hz')
        if stat['max_ms'] > MAX_INTERVAL_MS:
            spiky.append(f'{label} 最大间隔 {stat["max_ms"]:.0f}ms > {MAX_INTERVAL_MS:g}ms')

    reasons = []
    if missing:
        reasons.append(f'缺 {len(missing)} 个话题')
    if bad:
        reasons.append(f'{len(bad)} 个分组需复查')
    if slow:
        reasons.append(f'频率不达标 {len(slow)} 项（{slow[0]}）')
    if spiky:
        reasons.append(f'间隔超限 {len(spiky)} 项（{spiky[0]}）')
    return Verdict(not reasons, '；'.join(reasons), missing, bad, slow, spiky)


def build_report(bag_path, samples, results, *, generated_at=None, host=None):
    """把一次验证的结果组织成**纯文本报告**（纯函数，不碰 IO，便于单测）。

    为什么报告由脚本写、不由上位机截终端输出：分析完就知道全部结论了，而
    `plt.show(block=True)` 会把进程挂在那儿等操作员关窗口 —— 让上位机去"等它退出
    再落盘"，等于把报告的存在绑在"有人去关窗口"上。写完再弹图，
    报告一定在盘上（bag 目录里），与操作员看不看图无关。
    """
    from datetime import datetime as _datetime
    import socket

    when = generated_at or _datetime.now()
    host = host or socket.gethostname()
    v = evaluate_verdict(results, samples)
    # 判定行放在**标题之后第一行**：一是让人一眼看到，二是上位机只需要读报告
    # 头部几行就能拿到结论（列表刷新要扫几十份报告，不必整份读）。
    verdict_line = f'{VERDICT_PREFIX}{VERDICT_PASS if v.ok else VERDICT_FAIL}'
    if v.detail:
        verdict_line += f' | {v.detail}'
    lines = [
        '数据验证结果',
        '=' * 60,
        verdict_line,
        f'生成时间: {when:%Y-%m-%d %H:%M:%S}',
        f'主机: {host}',
        f'数据目录: {bag_path}',
        f'分析分组: {len(results)}；目标话题 {len(TOPIC_TYPES)} 个'
        f'（有数据 {len(TOPIC_TYPES) - len(v.missing)}，缺失 {len(v.missing)}）',
        '',
        '一、逐话题（Header 帧间隔，全量原始帧、无重采样）',
        '-' * 60,
    ]
    for index, key in enumerate(sorted(results), start=1):
        stat = results[key]
        lines.append(f'{index}. {key}')
        lines.append(f'   帧数: {len(samples[key])}')
        if stat:
            hz = f'{stat["hz"]:.3f} Hz' if stat['hz'] is not None else '无效（间隔均值 <= 0）'
            lines.append(f'   频率: {hz}')
            lines.append(f'   间隔: 均值 {stat["mean_ms"]:.3f} ms / 标准差 {stat["std_ms"]:.3f} ms'
                         f' / P95 {stat["p95_ms"]:.3f} ms / 最大 {stat["max_ms"]:.3f} ms')
            lines.append(f'   非正间隔: {stat["nonpositive"]} 帧')
        else:
            lines.append('   频率: 帧数不足（少于 2 帧，无法计算间隔）')
        lines.append('')
    lines += ['二、结论', '-' * 60]
    if v.missing:
        lines.append(f'· 缺失 {len(v.missing)} 个目标话题（整段没有消息）：')
        lines += [f'    - {key}' for key in v.missing]
    else:
        lines.append('· 全部目标分组均有数据。')
    if v.bad:
        lines.append(f'· 需要复查 {len(v.bad)} 个分组（无有效间隔，或存在非递增时间戳）：')
        lines += [f'    - {key}' for key in v.bad]
    else:
        lines.append('· 所有分组的帧间隔均可计算，且没有非递增时间戳。')
    # 频率与最大间隔分开写：现场判断"要不要重录"时看的是这两条，
    # 而它们对应的返工原因完全不同（一个是码率不够，一个是中途中段断流）。
    if v.slow:
        lines.append(f'· 频率不达标 {len(v.slow)} 项（{rate_standard_text()}）：')
        lines += [f'    - {item}' for item in v.slow]
    else:
        lines.append(f'· 频率全部达标（{rate_standard_text()}）。')
    if v.spiky:
        lines.append(f'· 最大间隔超限 {len(v.spiky)} 项（上限 {MAX_INTERVAL_MS:g} ms）：')
        lines += [f'    - {item}' for item in v.spiky]
    else:
        lines.append(f'· 所有分组最大间隔均 ≤ {MAX_INTERVAL_MS:g} ms。')
    lines.append('')
    return '\n'.join(lines)


def write_report(path, text):
    """写报告（先写 `.partial` 再改名）。

    与磁盘导出同一条理由：**读到半份报告比读不到更糟** —— 半份报告看起来像
    "验证过了、某几个话题是空的"，而实际只是写文件被打断。
    """
    from pathlib import Path as _Path

    target = _Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.partial')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    return target


def require_ros_environment():
    """提前检查 rclpy/rosbag2_py，缺失时给出 source setup.bash 的明确提示。"""
    for module in ('rclpy', 'rosbag2_py'):
        try:
            __import__(module)
        except ImportError as exc:
            sys.exit(f'{ROS_ENV_HINT}\n(导入 {module} 失败: {exc})')


def summarize(values):
    """以相邻时间戳差统计；非递增值保留并计数，频率为间隔均值的倒数。"""
    deltas = [(b - a) / 1e6 for a, b in zip(values, values[1:])]
    if not deltas:
        return None
    mean = statistics.fmean(deltas)
    ordered = sorted(deltas)
    return dict(hz=1000 / mean if mean > 0 else None, mean_ms=mean,
                std_ms=statistics.pstdev(deltas),
                p95_ms=ordered[round((len(ordered) - 1) * .95)],
                max_ms=max(deltas), nonpositive=sum(x <= 0 for x in deltas))


def display_name(key):
    if key.startswith('/vision/'):
        return '面部 ' + key
    if key.startswith('/camera_left/'):
        return '左腕 ' + key
    if key.startswith('/camera_right/'):
        return '右腕 ' + key
    for token, label in [('AR5_Ljoint', '左臂'), ('AR5_Rjoint', '右臂'),
                         ('LJoint', '左手'), ('RJoint', '右手'), ('trunk', '躯干/头部')]:
        if token in key:
            return label + ' /joint_states'
    return key


@lru_cache(maxsize=1)
def plotting():
    """使用 Matplotlib 自动选择的图形后端（Qt/Tk），不强制无界面的 Agg。"""
    from matplotlib import pyplot as plt, font_manager
    # Register newly installed CJK fonts even if Matplotlib's cache is older.
    for filename in font_manager.findSystemFonts():
        if any(token in Path(filename).name.lower() for token in ('notosanscjk', 'wqy')):
            font_manager.fontManager.addfont(filename)
    available = {f.name for f in font_manager.fontManager.ttflist}
    candidates = ['Noto Sans CJK SC', 'Noto Sans CJK JP', 'WenQuanYi Zen Hei',
                  'Microsoft YaHei', 'SimHei']
    fonts = [f for f in candidates if f in available]
    if not fonts:
        print('Warning: Chinese font missing; install fonts-noto-cjk for readable labels.')
    plt.rcParams['font.sans-serif'] = fonts + ['DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['path.simplify'] = False
    return plt


def show_results(samples, results, title):
    """所有 Header 曲线在一个窗口平铺；自动布局为标题和坐标轴预留空间。"""
    from math import ceil
    from textwrap import fill
    plt = plotting()
    keys = [key for key in TOPICS if key in results]
    keys += sorted(key for key in results if key.startswith('/joint_states'))
    if not keys:
        print('没有可绘制的消息。')
        return
    columns = min(len(keys), 3 if len(keys) > 6 else 2)
    rows = ceil(len(keys) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(18, max(5, rows * 3.3)),
                             squeeze=False, constrained_layout=True)
    fig.canvas.manager.set_window_title(title)
    fig.suptitle(fill(title, 85) + '\nHeader 帧间隔 · 全量原始帧，无重采样',
                 fontsize=13, fontweight='bold')
    colors = ('#2869d8', '#009775', '#de7900', '#8755b6', '#008a9c')
    camera_axes = []
    camera_min, camera_max = 0.0, 100.0
    for index, ax in enumerate(axes.flat):
        if index >= len(keys):
            fig.delaxes(ax)
            continue
        key = keys[index]
        stamps = samples[key]
        # 第 i 个间隔归属于后一帧，因此 X 从第 2 帧开始。
        values = [(b-a)/1e6 for a, b in zip(stamps, stamps[1:])]
        stat = results[key]
        label = fill(display_name(key), 48)
        if stat:
            hz = f'{stat["hz"]:.2f}' if stat['hz'] is not None else '无效'
            label += (f'\n{len(stamps)} 帧 | {hz} Hz | std {stat["std_ms"]:.2f} ms'
                      f'\nP95 {stat["p95_ms"]:.2f} ms | max {stat["max_ms"]:.2f} ms')
        else:
            label += f'\n{len(stamps)} 帧，样本不足'
        color = colors[index % len(colors)]
        ax.set_title(label, loc='left', fontsize=9, color=color, pad=8)
        ax.plot(range(2, len(stamps)+1), values, color=color,
                linewidth=.7, marker='o', markersize=1.5)
        ax.set_xlim(1, max(2, len(stamps)))
        # 显示完整毫秒值，不使用偏移量或科学计数法。
        ax.ticklabel_format(axis='y', style='plain', useOffset=False)
        if key in TOPICS:
            camera_axes.append(ax)
            if values:
                camera_min = min(camera_min, min(values))
                camera_max = max(camera_max, max(values))
        # 保留零/负间隔，不绘制人为阈值线。
        ax.margins(y=.15)
        ax.set_xlabel('帧序号（各组独立计数）', fontsize=9)
        ax.set_ylabel('Header 间隔 (ms)', fontsize=9)
        ax.tick_params(labelsize=8)
        ax.grid(True, color='#e0e4e8', linewidth=.6)
    # 六路相机默认 0～100 ms；异常时统一扩展，负间隔也不裁掉。
    # 关节图仍独立缩放；这里只改变显示，不改变统计数据。
    if camera_axes:
        from matplotlib.ticker import MaxNLocator, MultipleLocator
        lower = camera_min * 1.05 if camera_min < 0 else 0.0
        upper = camera_max * 1.05 if camera_max > 100 else 100.0
        for ax in camera_axes:
            ax.set_ylim(lower, upper)
            ax.yaxis.set_major_locator(
                MultipleLocator(20) if lower == 0 and upper == 100
                else MaxNLocator(nbins=5, steps=[1, 2, 5, 10]))
    plt.show(block=True)


def decompress_file(source):
    """复用同目录解压文件；新解压先写临时文件，成功后再更名，避免复用半成品。"""
    target = source.with_suffix('')
    if target.is_file():
        if target.stat().st_size == 0:
            raise ValueError(f'已有解压文件为空，请检查后手动删除：{target}')
        print(f'复用解压文件：{target}', flush=True)
        return target
    print(f'解压：{source} → {target}', flush=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=source.parent, prefix=target.name + '.', suffix='.partial', delete=False) as dst:
            temporary = Path(dst.name)
            # 使用 Ubuntu 的 zstd 工具，流式解压，不将整个 bag 装入内存。
            subprocess.run(['zstd', '-d', '-c', '--', str(source)], stdout=dst, check=True)
        temporary.replace(target)
    finally:
        # 仅清理失败的临时文件；最终解压文件和压缩原包均保留。
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def bag_messages(bag_path):
    """自动选择 SQLite/MCAP；FILE zstd 逐个解压复用，MESSAGE 压缩由 ROS 处理。"""
    import rosbag2_py
    import yaml
    metadata = yaml.safe_load((bag_path / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
    compression = metadata.get('compression_format', '')
    file_compressed = compression and metadata.get('compression_mode', '').lower() == 'file'
    paths = [bag_path]
    if file_compressed:
        if compression != 'zstd':
            raise ValueError(f'暂不支持 FILE 压缩格式：{compression}')
        paths = []
        for name in metadata['relative_file_paths']:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.zstd':
                raise ValueError(f'非法压缩文件路径：{name}')
            paths.append(decompress_file(bag_path / relative))
    reader_class = (rosbag2_py.SequentialCompressionReader if compression and not file_compressed
                    else rosbag2_py.SequentialReader)
    for path in paths:
        reader = reader_class()
        try:
            reader.open(rosbag2_py.StorageOptions(uri=str(path),
                        storage_id=metadata['storage_identifier']), rosbag2_py.ConverterOptions('', ''))
            selected = []
            for topic in reader.get_all_topics_and_types():
                if topic.name not in TOPIC_TYPES:
                    continue
                expected = TOPIC_TYPES[topic.name]
                if topic.type != expected:
                    raise ValueError(f'{topic.name}: 类型不匹配 {topic.type}')
                selected.append(topic.name)
            if selected:
                reader.set_filter(rosbag2_py.StorageFilter(topics=selected))
                while reader.has_next():
                    yield reader.read_next()
        finally:
            del reader


def analyze_bag(bag_path):
    """仅保留时间戳和分组信息，图像像素不常驻内存；关节按完整名称集合分组。"""
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import Image, JointState
    samples = defaultdict(list)
    for count, (topic, data, _) in enumerate(bag_messages(bag_path), 1):
        msg = deserialize_message(data, JointState if topic == '/joint_states' else Image)
        if topic == '/joint_states':
            key = topic + ' [' + ','.join(sorted(msg.name)) + ']'
        else:
            key = topic
        stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        samples[key].append(stamp)
        if count % 20000 == 0:
            print(f'已读取 {count} 条目标消息', flush=True)
    for topic in TOPIC_TYPES:
        if not any(key == topic or key.startswith(topic + ' [') for key in samples):
            print(f'{topic}: 无录制消息')
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bag', help='bag directory containing metadata.yaml')
    # ⚠️ help 里**不要拼 `REPORT_NAME_FMT`**：argparse 的 help 走 `%` 展开，
    # 那个格式串里的 `%Y`/`%m` 会让 `--help` 直接抛 ValueError（现场一敲帮助就崩）。
    parser.add_argument('--report', default='',
                        help='报告路径；默认写进 <bag>/数据验证结果_<时间>.txt')
    parser.add_argument('--no-plot', action='store_true',
                        help='只分析并写报告，不弹图（批量验证用：不需要图形后端，也不等人关窗口）')
    args = parser.parse_args()
    require_ros_environment()
    # 批量验证（`--no-plot`）**不碰图形后端**：它要在后台串行跑几十份，
    # 既不该因为"没有 X11"就失败，也不该在无人值守时弹一屋子窗口。
    if not args.no_plot:
        try:
            plt = plotting()
            from matplotlib import get_backend, rcsetup
            if get_backend().lower() not in {name.lower() for name in rcsetup.interactive_bk}:
                parser.error('当前没有可用图形后端。请在桌面终端运行，或配置 SSH X11 转发；'
                             '可安装 sudo apt install python3-tk')
            probe = plt.figure()
            plt.close(probe)
        except (ImportError, RuntimeError) as exc:
            parser.error(f'{exc}; sudo apt install python3-matplotlib fonts-noto-cjk')
    bag = Path(args.bag).resolve()
    if not (bag / 'metadata.yaml').is_file():
        parser.error(f'找不到 {bag / "metadata.yaml"}')
    samples = analyze_bag(bag)
    # 统计只计算一次，终端和窗口复用同一份结果。
    results = {key: summarize(stamps) for key, stamps in sorted(samples.items())}
    for key, value in results.items():
        print(f'{key}: {len(samples[key])} 帧')
        if value:
            hz = f'{value["hz"]:.3f}' if value['hz'] is not None else 'invalid'
            print(f'  Header: {hz} Hz, std {value["std_ms"]:.3f} ms, '
                  f'P95 {value["p95_ms"]:.3f} ms, max {value["max_ms"]:.3f} ms, '
                  f'nonpositive {value["nonpositive"]}')
    # 报告**先落盘再弹图**：`show_results` 里 `plt.show(block=True)` 会一直等操作员
    # 关窗口 —— 把写报告放在它后面，等于"没人关窗口就没有报告"。
    report_path = (Path(args.report) if args.report
                   else bag / datetime.now().strftime(REPORT_NAME_FMT))
    try:
        written = write_report(report_path, build_report(bag, samples, results))
        print(f'验证报告已保存：{written}', flush=True)
    except OSError as exc:
        # 盘只读 / 没权限时不能让整次验证白跑：图照常弹，但必须说清报告没写下来
        print(f'⚠ 验证报告写入失败（分析结果仍会显示）：{exc}', flush=True)
    if not args.no_plot:
        show_results(samples, results, bag.name)


if __name__ == '__main__':
    main()
