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
#   ./scripts/play_bag.sh [rviz] [bag=<bag目录>]
# =================================================================
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKSPACE="${ROBOT_WORKSPACE:-$(dirname "${SCRIPT_DIR}")}"
NERV_DIR="${NERV_DIR:-/var/nerv}"
BAG_DIR="${BAG_DIR:-${NERV_DIR}/bag}"
LOG_DIR="${LOG_DIR:-${NERV_DIR}/log}"

ARG_BAG_PATH=""
WITH_RVIZ=false
for arg in "$@"; do
    case "${arg}" in
        bag=*)
            ARG_BAG_PATH="${arg#*=}"
            ;;
        rviz)
            WITH_RVIZ=true
            ;;
        *)
            echo "Unknown argument: ${arg}"
            ;;
    esac
done

# bag 支持绝对路径；相对路径按 ${BAG_DIR} 解析
if [ -n "${ARG_BAG_PATH}" ] && [ "${ARG_BAG_PATH#/}" = "${ARG_BAG_PATH}" ]; then
    ARG_BAG_PATH="${BAG_DIR}/${ARG_BAG_PATH}"
fi

cd "${WORKSPACE}" || exit 1
ros2 daemon stop
ros2 daemon start
source "${WORKSPACE}/install/setup.bash" || exit 1

# 启动bag play
SELECTED_BAG=""
if [ -n "$ARG_BAG_PATH" ]; then
    SELECTED_BAG=$ARG_BAG_PATH
else
    BAG_LIST_DIR="${BAG_DIR}"
    if [ ! -d "${BAG_LIST_DIR}" ]; then
        echo "Error: $BAG_LIST_DIR not found"
        exit 1
    fi
    cd "$BAG_LIST_DIR" || exit
    valid_bags=($(ls -d */ | sed 's/\///'))
    if [ ${#valid_bags[@]} -eq 0 ]; then
        echo "Error: No bag found in $BAG_LIST_DIR"
        exit 1
    fi

    count=${#valid_bags[@]}
    current_idx=$((count - 1))
    echo "------------------------------------------"
    echo
    echo "播放操作: 空格=暂停、上下箭头=调速、右箭头=单帧播放（暂停时）"
    echo
    echo "请选择bag:"
    echo
    tput civis
    trap "tput cnorm; exit" INT TERM
    while true; do
        for i in "${!valid_bags[@]}"; do
            if [ $i -eq $current_idx ]; then
                echo -e " \033[32m>\033[0m \033[1;32m${valid_bags[$i]}\033[0m"
            else
                echo -e "   ${valid_bags[$i]}"
            fi
        done
        read -rsn3 key
        case "$key" in
            $'\x1b[A')
                ((current_idx--))
                [ $current_idx -lt 0 ] && current_idx=$((count - 1))
                ;;
            $'\x1b[B')
                ((current_idx++))
                [ $current_idx -ge $count ] && current_idx=0
                ;;
            "")
                break
                ;;
        esac
        echo -en "\033[${count}A"
    done
    SELECTED_BAG="${valid_bags[$current_idx]}"
    # 无论从哪个目录进入，info.json 都按绝对路径读取
    if [ "${SELECTED_BAG#/}" = "${SELECTED_BAG}" ]; then
        SELECTED_BAG="${BAG_DIR}/${SELECTED_BAG}"
    fi
    tput cnorm
    echo
fi

# 读取录制的手型。
# 合法手型 = hand_plugins 的键，与 rviz/<手型>/wheel_robot.rviz 目录同名：
#   empty / yushu_dex11 / hangkai_tg9801 / yinshi_rh56d
# 老数据与老版本界面里写的是短名（hangkai/yinshi/yushu），这里做一次映射；
# 映射后仍找不到对应配置就回退 empty —— 绝不让 rviz2 -d 指向一个不存在的文件。
RVIZ_CONFIG_DIR="${WORKSPACE}/install/share/robot_description/rviz"
HAND_TYPE="empty"
INFO_JSON_FILE="${SELECTED_BAG}/info.json"
PARSED_HAND_TYPE=""
if [ -f "${INFO_JSON_FILE}" ]; then
    PARSED_HAND_TYPE=$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as handle:
    info = json.load(handle) or {}
print(info.get('hand_type') or '')
" "${INFO_JSON_FILE}" 2>/dev/null)
fi
case "${PARSED_HAND_TYPE}" in
    hangkai) PARSED_HAND_TYPE="hangkai_tg9801" ;;
    yinshi)  PARSED_HAND_TYPE="yinshi_rh56d" ;;
    yushu)   PARSED_HAND_TYPE="yushu_dex11" ;;
esac
if [ -n "${PARSED_HAND_TYPE}" ] && [ "${PARSED_HAND_TYPE}" != "unknown" ]; then
    if [ -f "${RVIZ_CONFIG_DIR}/${PARSED_HAND_TYPE}/wheel_robot.rviz" ]; then
        HAND_TYPE="${PARSED_HAND_TYPE}"
    else
        echo "警告: bag 记录的手型 '${PARSED_HAND_TYPE}' 没有对应 rviz 配置，回退 empty"
        echo "      当前可用手型: $(ls "${RVIZ_CONFIG_DIR}" 2>/dev/null | tr '\n' ' ')"
    fi
fi

# 启动rviz
if [[ "${WITH_RVIZ}" == true ]]; then
    if ! pgrep -f "wheel_robot.rviz" > /dev/null
    then
        echo "使用手型配置: ${HAND_TYPE}"
        rviz2 -d "${RVIZ_CONFIG_DIR}/${HAND_TYPE}/wheel_robot.rviz" --ros-args -p use_sim_time:=true &
    fi
    sleep 1
fi

AUTO_START=""
read -p "自动开始播放(Y/n): " AUTO_START
echo "------------------------------------------"
echo
echo "bag信息:"
ros2 bag info "$SELECTED_BAG"
echo "------------------------------------------"
echo
AUTO_START_LOWER=$(echo "$AUTO_START" | tr '[:upper:]' '[:lower:]')
if [[ "$AUTO_START_LOWER" == "n" || "$AUTO_START_LOWER" == "no" ]]; then
    echo "按空格键开始播放"
    echo
    ros2 bag play "$SELECTED_BAG" --clock --start-paused
else
    echo "自动开始播放"
    echo
    ros2 bag play "$SELECTED_BAG" --clock
fi

# 播放结束
if [[ "${WITH_RVIZ}" == true ]]; then
    EXIT_RVIZ=""
    read -p "播放结束, 是否关闭窗口(Y/n): " EXIT_RVIZ
    EXIT_RVIZ_LOWER=$(echo "$EXIT_RVIZ" | tr '[:upper:]' '[:lower:]')
    if [[ "$EXIT_RVIZ_LOWER" != "n" && "$EXIT_RVIZ_LOWER" != "no" ]]; then
        pkill -f "wheel_robot.rviz" 2>/dev/null
    fi
else
    echo "播放结束"
fi