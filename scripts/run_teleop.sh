#!/bin/bash
# =============================================================================
# 遥操作启动脚本
#
# 与原版（run_teleop.sh.orig）的差异，共 3 处：
#   1. 原来 10 次 `ros2 topic pub --once --keep-alive 2` 逐条下发使能时序，
#      每次都要冷启动 ros2 CLI 进程（约 2~3 秒），累计约 25 秒纯开销。
#      现在改为单个进程 scripts/connect_sequence.py 一次性下发。
#   2. 删除 `/trunk_control/motor_mode 0x00`（显式切非实时）：
#      躯干/手臂下使能即自动切换到非实时模式，这一步已不需要。
#   3. 双臂使能后不再依赖"睡 5 秒"的猜测，改为等 /arm_control/arm_state
#      报出左右臂 connected —— 使能未完成就下发 follow 会被静默丢弃
#      （arm_control_node 内有 "left arm is not connected, ignore setting follow"）。
#
# 用法与原来完全一致：
#   bash run_teleop.sh                 # 硬件遥操作
#   bash run_teleop.sh use_sim         # 仿真
#   bash run_teleop.sh record          # 同时录制 bag
#   bash run_teleop.sh record=xxx      # 指定 bag 名称
#
# 时序可调（在机器人上直接看/调，不必改脚本）：
#   python3 scripts/connect_sequence.py --list                 # 查看各步等待
#   python3 scripts/connect_sequence.py --dry-run              # 只打印不执行
#   python3 scripts/connect_sequence.py --wait trunk_enable1=4 # 单项覆盖
#   python3 scripts/connect_sequence.py --speed 0.8            # 整体缩放
#
# -----------------------------------------------------------------------------
# 路径约定（与 scripts/ 下其它脚本一致，play_bag.sh / monitor_record_performance.sh 同款）
#   ROBOT_WORKSPACE   机器人工作空间根目录。上位机「启动遥操作」会 export 进来；
#                     手工执行时不传则按脚本位置推断：脚本自己所在目录 → 其上一级。
#   脚本里一切相对路径（scripts/… 、install/setup.bash、录制脚本）都相对**工作空间**
#   解析，不相对本文件位置 —— 这样同一份脚本放在 <工作空间>/run_teleop.sh 或在
#   上位机的 scripts/ 下都能跑。
#
#   注：上位机 scripts/ 与机器人工作空间根目录各有一份同名脚本（上位机解析出来的
#   就是 scripts/ 这份）。**改动请两处同步**，或者只保留机器人侧那份。
#
# 依赖：scripts/connect_sequence.py（上电时序）。
#   它是本脚本唯一的外部文件依赖，**常规随上位机交付包一起发布**（机器人侧本来没有它，
#   是这次改时序时新增出来的文件）。查找见下方 CONNECT_SEQUENCE：上位机侧优先，
#   机器人工作空间里那份只在缺文件时兜底。
# =============================================================================

set -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# 定位机器人工作空间：优先用上位机传进来的 ROBOT_WORKSPACE，其次脚本自己所在目录
# （脚本就放在工作空间根目录时走这条），最后按约定退到上一级。
# 判据统一是"有 install/setup.bash"；定位不到就**立刻**说清并退出 ——
# 原先这一步只写 `source install/setup.bash` 且不看返回值，路径不对时它静默失败，
# 接着 python3 拿着相对路径去找文件，现场只看到一句看不懂的 "can't open file"。
resolve_workspace() {
    local cand
    for cand in "${ROBOT_WORKSPACE:-}" "${SCRIPT_DIR}" "$(dirname "${SCRIPT_DIR}")"; do
        if [[ -n "${cand}" && -f "${cand}/install/setup.bash" ]]; then
            printf '%s\n' "${cand}"
            return 0
        fi
    done
    return 1
}

if ! WORKSPACE="$(resolve_workspace)"; then
    echo "错误：定位不到机器人工作空间（判据：<目录>/install/setup.bash 存在）" >&2
    echo "  试过：ROBOT_WORKSPACE=${ROBOT_WORKSPACE:-（未设置）}" >&2
    echo "        ${SCRIPT_DIR}" >&2
    echo "        $(dirname "${SCRIPT_DIR}")" >&2
    echo "  手工执行：ROBOT_WORKSPACE=<工作空间> bash $0 [use_sim] [record[=名称]]" >&2
    exit 2
fi
cd "${WORKSPACE}" || exit 2

if ! source "${WORKSPACE}/install/setup.bash"; then
    echo "错误：source ${WORKSPACE}/install/setup.bash 失败" >&2
    exit 2
fi
# 沿用原版设置：把 DDS 限制在本机（跨机看话题 / 跨机 rviz 需要去掉这一行）
export ROS_LOCALHOST_ONLY=1
export LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH:-}
export PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# 上电时序脚本：两个候选，**上位机侧优先**
#   1) ${SCRIPT_DIR}/connect_sequence.py —— 常规就是这一份。它是随上位机交付包发布的
#      新文件：机器人侧本来没有它，是这次改时序时才新增出来的。
#   2) ${WORKSPACE}/scripts/connect_sequence.py —— 兼容"机器人侧也放了一份"的部署
#      （调试期间两边都拷过）。只在 1) 缺失时使用，且会明确告警 —— 时序与机器人侧
#      节点强耦合，两份不一致必须人工确认，不能悄悄用。
# ---------------------------------------------------------------------------
CONNECT_SEQUENCE=""
for candidate in "${SCRIPT_DIR}/connect_sequence.py" "${WORKSPACE}/scripts/connect_sequence.py"; do
    if [[ -f "${candidate}" ]]; then
        CONNECT_SEQUENCE="${candidate}"
        break
    fi
done

if [[ -z "${CONNECT_SEQUENCE}" ]]; then
    cat >&2 <<EOF

================================================================================
错误：找不到上电时序脚本 connect_sequence.py
      已找过：
        ${SCRIPT_DIR}/connect_sequence.py
        ${WORKSPACE}/scripts/connect_sequence.py

本脚本使能时序改由 connect_sequence.py 一次性下发，该文件是它唯一的外部依赖，
常规随上位机交付包一起发布（机器人侧本来没有它）。

【修复】从交付包或另一台机器上拷一份到上面任一位置，例如：
  scp <用户>@<其它机器>:<上位机目录>/scripts/connect_sequence.py \\
      ${SCRIPT_DIR}/
【应急】若工作空间里有旧版备份，可用它启动（时序较慢，约多 25 秒）：
  bash ${WORKSPACE}/run_teleop.sh.orig

本次未向机器人下发任何指令，可直接重试。
================================================================================
EOF
    exit 2
fi

# 走了工作空间兜底就说清 —— 它与上位机交付的版本可能不一致，而时序是强耦合的
if [[ "${CONNECT_SEQUENCE}" == "${WORKSPACE}/scripts/connect_sequence.py" ]]; then
    echo "警告：上位机侧没有 connect_sequence.py（${SCRIPT_DIR}/），" >&2
    echo "      改用了工作空间里那份：${CONNECT_SEQUENCE}" >&2
    echo "      该文件常规随上位机交付包发布；请确认这份与上位机版本一致。" >&2
fi

# 参数解析（与原来一致）
USE_SIM=false
RECORD=false
RECORD_NAME=""
for arg in "$@"; do
    case "${arg}" in
        use_sim)
            USE_SIM=true
            ;;
        record)
            RECORD=true
            ;;
        record=*)
            RECORD=true
            RECORD_NAME="${arg#*=}"
            ;;
        *)
            echo "Unknown argument: ${arg}"
            ;;
    esac
done

# 选用能导入 rclpy 的解释器（机器上存在多个 python 时避免选错）
PY=python3
if ! ${PY} -c "import rclpy" >/dev/null 2>&1; then
    if /usr/bin/python3 -c "import rclpy" >/dev/null 2>&1; then
        PY=/usr/bin/python3
    fi
fi
# 两个解释器都导不出 rclpy，说明 ROS 环境压根没进来：这里挡住，给一句能直接照做的错，
# 而不是等到 connect_sequence 里抛一堆 ImportError。
if ! ${PY} -c "import rclpy" >/dev/null 2>&1; then
    echo "错误：${PY} 里 import rclpy 失败 —— ROS 环境没进来（工作空间 ${WORKSPACE}）" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# 上电时序
# ---------------------------------------------------------------------------
if [[ "${USE_SIM}" == true ]]; then
    ${PY} "${CONNECT_SEQUENCE}" --sim || exit 1
else
    ${PY} "${CONNECT_SEQUENCE}" || exit 1
fi

if [[ "${RECORD}" == true ]]; then
    # 录制脚本按项目约定收 `name=<名称>`（见 tests/test_contracts.py 的接口契约）。
    # 原来写成 `record_bag.sh "${RECORD_NAME}"`：那个位置参数会被 record_bag.sh 当成
    # "Unknown argument" 忽略掉 —— record=xxx 实际录出来的名字一直是默认的 rosbag2。
    RECORD_ARGS=()
    if [[ -n "${RECORD_NAME}" ]]; then
        RECORD_ARGS+=( "name=${RECORD_NAME}" )
    fi
    bash "${WORKSPACE}/scripts/record_bag.sh" "${RECORD_ARGS[@]}" &
fi

LAUNCH_ARGS=( )
if [[ "${USE_SIM}" == true ]]; then
    LAUNCH_ARGS+=( "use_sim:=true" )
    # TODO 由于仿真躯干暂未实现TrunkHw，所以先通过trunk_controller直接下发初始关节角
    ros2 topic pub /trunk/trunk_controller/joint_trajectory trajectory_msgs/msg/JointTrajectory "{joint_names: ['trunk1_joint', 'trunk2_joint','trunk3_joint','trunk4_joint','head1_joint','head2_joint'], points: [{positions: [-0.697,-1.394,-0.697,0,0,0], velocities: [0,0,0,0,0,0], time_from_start: {sec: 3}}]}" --times 2
fi
echo "ros2 launch teleop teleop_launch.py ${LAUNCH_ARGS[*]}"
ros2 launch teleop teleop_launch.py "${LAUNCH_ARGS[@]}"
