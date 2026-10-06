#!/usr/bin/env python3
"""Offline full-bag statistics.
Usage: python3 scripts/bag_rate_stat.py BAG_DIRECTORY
All recorded frames of the seven target topics are analyzed, without resampling.
Only header.stamp is analyzed; all plots share one window without pagination.
No report files are written.
FILE zstd bags expand beside the compressed file; existing expanded files are reused.
Requires the ROS environment of the robot workspace
(source ${ROBOT_WORKSPACE:-<工作空间>}/install/setup.bash), python3-matplotlib,
zstd and fonts-noto-cjk.
"""
import argparse
from collections import defaultdict
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
                '上位机界面点击「采集统计」时会自动 source，请确认工作空间里存在 install/setup.bash。')


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
    args = parser.parse_args()
    require_ros_environment()
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
    show_results(samples, results, bag.name)


if __name__ == '__main__':
    main()
