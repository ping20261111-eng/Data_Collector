#!/bin/bash
# ============================ 统一约定 ============================
# 本脚本统一存放在项目的 scripts/ 目录，由上位机（Data_Collector）调用，也可单独执行。
# 路径一律通过环境变量传入，未设置时使用下列默认值：
#   ROBOT_WORKSPACE  机器人工作空间根目录，默认取脚本目录的上一级
#   NERV_DIR         数据根目录，默认 /var/nerv
#   BAG_DIR          录制数据目录，默认 ${NERV_DIR}/bag
#   LOG_DIR          日志目录，默认 ${NERV_DIR}/log
#   ROBOT_STATUS_FILE 机器人状态 JSON（robot_monitor.py 输出），
#                     默认 ${NERV_DIR}/status/robot_status.json
# 说明：监控 rosbag 录制期间的 CPU、内存、磁盘和相关进程资源占用。
# 用法：
#   ./scripts/monitor_record_performance.sh
#   ./scripts/monitor_record_performance.sh /path/to/output_dir
# =================================================================
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKSPACE="${ROBOT_WORKSPACE:-$(dirname "${SCRIPT_DIR}")}"
NERV_DIR="${NERV_DIR:-/var/nerv}"
BAG_DIR="${BAG_DIR:-${NERV_DIR}/bag}"
LOG_DIR="${LOG_DIR:-${NERV_DIR}/log}"

set -u

DEFAULT_OUTPUT_ROOT="${NERV_DIR}/monitor"
TIMESTAMP=$(date +"%Y%m%d%H%M%S")
OUTPUT_DIR="${1:-${DEFAULT_OUTPUT_ROOT}/record_${TIMESTAMP}}"

missing_commands=()
for command_name in iostat pidstat vmstat pgrep; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        missing_commands+=("${command_name}")
    fi
done

if ((${#missing_commands[@]} > 0)); then
    echo "缺少监控命令: ${missing_commands[*]}"
    echo "请先安装: sudo apt-get update && sudo apt-get install -y sysstat procps"
    exit 1
fi

mkdir -p "${OUTPUT_DIR}" || {
    echo "无法创建输出目录: ${OUTPUT_DIR}"
    exit 1
}

echo "监控日志目录: ${OUTPUT_DIR}"
echo "正在等待 ros2 bag record 启动，请在另一个终端运行 record_bag.sh（或在上位机点击“数据录制”）..."

recorder_pid=""
while [ -z "${recorder_pid}" ]; do
    recorder_pid=$(pgrep -n -f '[r]os2 bag record|[r]osbag2_recorder' || true)
    sleep 1
done

echo "检测到录制进程 PID=${recorder_pid}，开始监控。"
echo "录制结束后脚本会自动退出；也可以按 Ctrl+C 提前停止监控。"

{
    echo "monitor_start=$(date --iso-8601=ns)"
    echo "recorder_pid=${recorder_pid}"
    echo
    uname -a
    echo
    lscpu
    echo
    free -h
    echo
    df -h "${OUTPUT_DIR}"
    echo
    lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,ROTA,TRAN
    echo
    ps -eo pid,ppid,psr,ni,pri,stat,%cpu,%mem,rss,vsz,comm,args --sort=-%cpu | head -80
} > "${OUTPUT_DIR}/system_info.log" 2>&1

related_pids=$(pgrep -d, -f \
    '[r]os2 bag record|[r]osbag2_recorder|[v]ision_node|[p]oint_cloud_xyzrgb_node|[r]ealsense2_camera_node' \
    || true)

if [ -z "${related_pids}" ]; then
    related_pids="${recorder_pid}"
fi

echo "监控相关进程 PID: ${related_pids}" | tee "${OUTPUT_DIR}/monitored_pids.log"
ps -p "${related_pids}" -o pid,ppid,psr,ni,pri,stat,%cpu,%mem,rss,vsz,comm,args \
    >> "${OUTPUT_DIR}/monitored_pids.log" 2>&1 || true

monitor_pids=()

cleanup()
{
    for monitor_pid in "${monitor_pids[@]}"; do
        if kill -0 "${monitor_pid}" 2>/dev/null; then
            kill "${monitor_pid}" 2>/dev/null || true
        fi
    done
    wait "${monitor_pids[@]}" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 130' INT TERM

iostat -xz -y 1 > "${OUTPUT_DIR}/iostat.log" 2>&1 &
monitor_pids+=("$!")

vmstat -w 1 > "${OUTPUT_DIR}/vmstat.log" 2>&1 &
monitor_pids+=("$!")

pidstat -h -u -r -d -p "${related_pids}" 1 > "${OUTPUT_DIR}/pidstat.log" 2>&1 &
monitor_pids+=("$!")

if [ -f "${WORKSPACE}/install/setup.bash" ]; then
    # shellcheck disable=SC1091
    set +u
    source "${WORKSPACE}/install/setup.bash"
    set -u
    sleep 3
    {
        echo "capture_time=$(date --iso-8601=ns)"
        for topic_name in \
            /vision/rgb \
            /vision/depth \
            /camera_left/color/image_rect_raw \
            /camera_left/aligned_depth_to_color/image_raw \
            /camera_right/color/image_rect_raw \
            /camera_right/aligned_depth_to_color/image_raw; do
            echo
            echo "===== ${topic_name} ====="
            timeout 5 ros2 topic info -v "${topic_name}" || true
        done
    } > "${OUTPUT_DIR}/topic_info.log" 2>&1
fi

while kill -0 "${recorder_pid}" 2>/dev/null; do
    sleep 1
done

echo "检测到录制进程已退出，停止监控。"
echo "monitor_end=$(date --iso-8601=ns)" >> "${OUTPUT_DIR}/system_info.log"
echo "请将整个目录发回分析: ${OUTPUT_DIR}"
