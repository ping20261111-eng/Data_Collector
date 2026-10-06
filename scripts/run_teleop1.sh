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
# 用法：
#   ./scripts/run_teleop.sh [use_sim] [record] [record=<名称>]
# =================================================================
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKSPACE="${ROBOT_WORKSPACE:-$(dirname "${SCRIPT_DIR}")}"
NERV_DIR="${NERV_DIR:-/var/nerv}"
BAG_DIR="${BAG_DIR:-${NERV_DIR}/bag}"
LOG_DIR="${LOG_DIR:-${NERV_DIR}/log}"

cd "${WORKSPACE}" || exit 1
source "${WORKSPACE}/install/setup.bash" || exit 1

export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH

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

ros2 topic pub --once --keep-alive 2 /arm_control/connect std_msgs/msg/Bool "data: False"
sleep 1
if [[ "${USE_SIM}" == false ]]; then
    ros2 topic pub --once --keep-alive 2 /trunk_control/connect std_msgs/msg/Bool "data: False"
    sleep 1
    ros2 topic pub --once --keep-alive 2 /chassis_control/connect std_msgs/msg/Bool "data: True"
    sleep 3
    ros2 topic pub --once --keep-alive 2 /trunk_control/motor_mode std_msgs/msg/Byte "data: {0x00}"
    sleep 1
    ros2 topic pub --once --keep-alive 2 /trunk_control/connect std_msgs/msg/Bool "data: True"
    sleep 5
    ros2 topic pub --once --keep-alive 2 /trunk_control/motor_mode std_msgs/msg/Byte "data: {0x01}"
    sleep 3
    ros2 topic pub --once --keep-alive 2 /trunk_control/connect std_msgs/msg/Bool "data: True"
    sleep 3
fi
ros2 topic pub --once --keep-alive 2 /arm_control/connect std_msgs/msg/Bool "data: True"
sleep 5
ros2 topic pub --once --keep-alive 2 /arm_control/left_arm_follow_position_set std_msgs/msg/Bool "data: True"
sleep 1
ros2 topic pub --once --keep-alive 2 /arm_control/right_arm_follow_position_set std_msgs/msg/Bool "data: True"
sleep 1
if [[ "${RECORD}" == true ]]; then
    # 与 record_bag.sh 的参数约定保持一致：name=<名称>
    if [[ -n "${RECORD_NAME}" ]]; then
        bash "${SCRIPT_DIR}/record_bag.sh" "name=${RECORD_NAME}" &
    else
        bash "${SCRIPT_DIR}/record_bag.sh" &
    fi
fi

LAUNCH_ARGS=( )
if [[ "${USE_SIM}" == true ]]; then
    LAUNCH_ARGS+=( "use_sim:=true" )
    # TODO 由于仿真躯干暂未实现TrunkHw，所以先通过trunk_controller直接下发初始关节角
    ros2 topic pub /trunk/trunk_controller/joint_trajectory trajectory_msgs/msg/JointTrajectory "{joint_names: ['trunk1_joint', 'trunk2_joint','trunk3_joint','trunk4_joint','head1_joint','head2_joint'], points: [{positions: [-0.697,-1.394,-0.697,0,0,0], velocities: [0,0,0,0,0,0], time_from_start: {sec: 3}}]}" --times 2
fi
echo "ros2 launch teleop teleop_launch.py ${LAUNCH_ARGS[*]}"
ros2 launch teleop teleop_launch.py "${LAUNCH_ARGS[@]}"
