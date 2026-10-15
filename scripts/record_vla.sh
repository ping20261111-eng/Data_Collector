#!/bin/bash
# ============================ 统一约定 ============================
# 本脚本统一存放在项目的 scripts/ 目录，由上位机（Data_Collector）调用，也可单独执行。
# 路径一律通过环境变量传入，未设置时使用下列默认值：
#   ROBOT_WORKSPACE  机器人工作空间根目录（判据：<目录>/install/setup.bash 存在）
#   NERV_DIR         数据根目录，默认 /var/nerv
#   BAG_DIR          录制数据目录，默认 ${NERV_DIR}/bag
#
# 【本文件的地位】录制脚本的**真源就是这一份**（在版本控制里，可回滚、可评审）。
# 机器侧 wheeled_robot_pkg/scripts/ 下同名文件只是**派生来源**，以及本份缺失时的回落；
# 两边若有差异，一律以本文件为准。派生来源、sha256 与差异清单见
# scripts/record_scripts.manifest.json（由测试盯住，防副本被手改坏或与来源悄悄漂移）。
#
# 与机器侧**当前那份**（wheeled_robot_pkg/scripts/record_vla.sh）的差异：
#   1. 工作空间定位：优先 ROBOT_WORKSPACE，定位不到就**说清并退出**。
#      原版是 `source ${ROOT_DIR}/install/setup.bash` 且不看返回值 —— 路径不对时
#      它静默失败，接着 `ros2: command not found`，现场只看到一句看不懂的报错。
#      （判据与报错文案沿用我们 scripts/run_teleop.sh 的既有写法，不是另发明一套。）
#   2. `cd "${BAG_DIR}"` 补上引号。原版 `cd $BAG_DIR` 无引号：路径含空格时
#      `cd: 参数太多`，而脚本没有 set -e 就不退出，`ros2 bag record` 于是把包
#      建在**当前目录**（静默写错位置，且不报错）。
#   3. 前置失败信息改走 stderr（原版走 stdout，会和正常输出混在一起）。
# 话题清单、录制参数、info.json 合并（name/desc/version）**逐字未动**。
#
# 另有一处是机器侧 2026-10-14 按现场授权改的，本份沿用（不算差异，但要记着来源）：
#   BAG_DIR 可被环境变量覆盖（原为硬编码 `BAG_DIR="$NERV_DIR/bag"`）。
#
# 刻意**不加** `set -e`：ros2 bag record 被 Ctrl-C 打断时返回非 0，
# 加了会让下面那段 info.json 合并（写 name/desc/version）被跳过，现场丢标签信息。
# 用法：
#   ./scripts/record_vla.sh [name=名字] [desc=说明]
# =================================================================

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# 沿用机器侧脚本里的旧变量名（脚本其余部分仍引用 CURRENT_DIR），少改一处是一处
CURRENT_DIR="${SCRIPT_DIR}"

# 定位机器人工作空间：优先上位机传进来的 ROBOT_WORKSPACE，其次脚本自己所在目录
# （脚本就放在工作空间根目录时走这条），最后按约定退到上一级。
# 判据统一是"有 install/setup.bash"。
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
    echo "  手工执行：ROBOT_WORKSPACE=<工作空间> bash $0 ${1:-}" >&2
    exit 2
fi
NERV_DIR="${NERV_DIR:-/var/nerv}"
PROJECT_VERSION_FILE="${WORKSPACE}/VERSION"
PRODUCT_JSON_FILE="${NERV_DIR}/product/Product.json"

if [ ! -f "${PROJECT_VERSION_FILE}" ]; then
    echo "Error: ${PROJECT_VERSION_FILE} not found" >&2
    exit 1
fi
if [ ! -f "${PRODUCT_JSON_FILE}" ]; then
    echo "Error: ${PRODUCT_JSON_FILE} not found" >&2
    exit 1
fi

if ! source "${WORKSPACE}/install/setup.bash"; then
    echo "错误：source ${WORKSPACE}/install/setup.bash 失败" >&2
    exit 2
fi


NAME="vla"
DESC=""
for arg in "$@"; do
    case "${arg}" in
        name=*)
            NAME="${arg#*=}"
            ;;
        desc=*)
            DESC="${arg#*=}"
            ;;
        *)
            echo "Unknown argument: ${arg}"
            ;;
    esac
done

BAG_DIR="${BAG_DIR:-$NERV_DIR/bag}"
if [ ! -d "${BAG_DIR}" ]; then
    mkdir -p "${BAG_DIR}"
fi
TIMESTAMP=$(date +"%Y%m%d%H%M%S")
BAG_NAME="${NAME}_${TIMESTAMP}"

cd "${BAG_DIR}"
ros2 bag record \
    /joint_states \
    /vision/rgb \
    /camera_left/color/image_rect_raw \
    /camera_right/color/image_rect_raw \
    --storage mcap \
    --storage-preset-profile fastwrite \
    -b 50000000000 \
    --max-cache-size 1073741824 \
    -o "${BAG_NAME}" \
    --qos-profile-overrides-path "${CURRENT_DIR}/qos_override.yaml"

BAG_PATH="${BAG_DIR}/${BAG_NAME}"
if [ ! -d "${BAG_PATH}" ]; then
    exit 1
fi

# 拷贝Product.json到bag目录，并补上desc/version字段
BAG_INFO_JSON_FILE="${BAG_PATH}/info.json"
NAME="${NAME}" DESC="${DESC}" VERSION="$(cat "${PROJECT_VERSION_FILE}")" PRODUCT_JSON_FILE="${PRODUCT_JSON_FILE}" \
    BAG_INFO_JSON_FILE="${BAG_INFO_JSON_FILE}" python3 -c "
import json
import os

product_json_file = os.environ['PRODUCT_JSON_FILE']
with open(product_json_file, 'r') as f:
    info = json.load(f)

info['version'] = os.environ.get('VERSION', '')
info['name'] = os.environ.get('NAME', '')
info['desc'] = os.environ.get('DESC', '')

with open(os.environ['BAG_INFO_JSON_FILE'], 'w') as f:
    json.dump(info, f, indent=4, ensure_ascii=False)
"
