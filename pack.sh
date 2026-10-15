#!/usr/bin/env bash
# 打包上位机：产出 Data_Collector_v<版本>_<日期>.tar.gz
#
# 用法：
#   ./pack.sh                    # 输出到 <项目根>/dist
#   ./pack.sh /path/to/outdir    # 输出到指定目录
#   PACK_OUT_DIR=/path ./pack.sh # 也可以用环境变量
#
# 三条硬规矩（都是踩过才写进来的）：
# 1. **logs 里的东西一律不进包**：根目录 `logs/` 与包目录 `data_collector/logs/` 都清空。
#    后者是 v4.4 拆包时路径算错留下的第二个日志目录 —— 它曾把 301 个运行日志
#    悄悄带进交付包（"打包前删 logs"这条规矩当时只覆盖了根目录那一半）。
# 2. **不带 .git / 缓存 / 状态文件**：`*.state.json` 里有 PID，缓存是本地噪音。
# 3. **不动工作区的 logs**：先整份复制到暂存目录、在暂存目录里清，打包过程不删现场证据。
# 4. **不带 `configs/point_labels.yaml`**：它是运行时产物（首次打开界面时由
#    `app_config.yaml` 的 `point_labels` 播种生成）。带进去有两个坏处：一是会把开发机上
#    试打的标签（曾实测混进一条 "asda"）发到现场；二是覆盖掉现场自己维护的标签。
#    要改初始标签，改 `app_config.yaml` 的 `point_labels` 播种段。
# 5. **不带 `configs/app_config.yaml`**（v6.4.15 加的，现场报障后补的）：
#    它**不是程序的一部分，是"这台机器自己的配置"** —— 远端密码、移动硬盘挂载点、
#    序列号、导出目标这些都是现场特有的值。带进去有两个坏处：
#      ① **现场解包就把自己那份覆盖掉**。实测过整条链：包里那份的
#         `remote_password` 是空的（开发机本来就没填），解包后现场密码被清空 →
#         「远端目录打不开」（SFTP 认证失败）。而界面上只表现为
#         "未配置远端 SFTP 密码"，几乎不可能联想到是包覆盖的。
#      ② 工作区那份**一旦填了密码，就等于每次交付都在外发凭据**。
#    程序首次启动会自己从 `configs/app_config.example.yaml` 生成一份（见 `app.py`），
#    所以不带它不影响新环境部署。**要改默认值，改 example 那份。**
#
# 6. **必须带上我们自己的录制脚本副本**（v6.5.0 加的）：`scripts/record_vla.sh` /
#    `record_bag.sh` / `record_cmd.sh` 与它们的清单 `scripts/record_scripts.manifest.json`。
#    起录制跑的是这几份（真源在本仓，见 ADR-002）。漏进包不会立刻报错 —— 解析会
#    **静默回落**到机器侧那份，于是"重刷后退回硬编码 → 落盘位置失效"这个坑又回来了，
#    现场看到的仍是一句"脚本启动后立即退出"。所以这里跟上面几条一样用自检拦。
#
# 打包末尾会**自检**：包里只要还有 `logs/` 下的文件、或带上
# `configs/point_labels.yaml` / `configs/app_config.yaml`、或缺了上面第 6 条那几个文件，
# 就直接失败退出，不靠人记得检查。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-${PACK_OUT_DIR:-$PROJECT_ROOT/dist}}"

VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
           "$PROJECT_ROOT/data_collector/__init__.py")"
if [[ -z "$VERSION" ]]; then
    echo "✗ 读不到版本号（data_collector/__init__.py 的 __version__）" >&2
    exit 1
fi

STAMP="$(date +%Y%m%d)"
NAME="Data_Collector_v${VERSION}_${STAMP}"
OUT_DIR="${OUT_DIR%/}"
mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/${NAME}.tar.gz"

STAGE="$(mktemp -d "${TMPDIR:-$HOME}/dc_pack_XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

echo "→ 暂存目录：$STAGE"
echo "→ 版本：v$VERSION，日期：$STAMP"

# 复制（排除不该进包的东西）。用 tar 管道复制：rsync 在部分机器上没有。
# 直接复制进 $STAGE/Data_Collector，包内顶层目录名就是它。
mkdir -p "$STAGE/Data_Collector"
tar -C "$PROJECT_ROOT" \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.state.json' \
    --exclude='*.pid' \
    --exclude='dist' \
    --exclude='configs/point_labels.yaml' \
    --exclude='configs/app_config.yaml' \
    -cf - . | tar -C "$STAGE/Data_Collector" -xf -

TARGET="$STAGE/Data_Collector"

# 规矩 1：清空两个 logs 目录的内容（保留目录本身，程序运行时会往里写）
CLEARED=0
for LOGS in "$TARGET/logs" "$TARGET/data_collector/logs"; do
    if [[ -d "$LOGS" ]]; then
        COUNT="$(find "$LOGS" -mindepth 1 | wc -l | tr -d ' ')"
        find "$LOGS" -mindepth 1 -delete
        echo "  已清空 ${LOGS#$STAGE/}（$COUNT 项）"
        CLEARED=$((CLEARED + COUNT))
    fi
done
# 包目录下不该有 logs（那是路径算错才出现的第二处），留个空目录只会再被误用
rmdir "$TARGET/data_collector/logs" 2>/dev/null || true
echo "  合计清理 $CLEARED 项日志"

# 打包
tar -C "$STAGE" -czf "$TARBALL" Data_Collector

# ---------------------------------------------------------------------------
# 自检
#
# **清单只取一次，下面全部在它上面查。** 不要再写 `tar -tzf "$TARBALL" | grep -q ...`：
# `grep -q` 一找到就退出，tar 立刻收到 SIGPIPE 并**以 2 退出**；脚本开头有
# `set -o pipefail`，管道状态就成了 2 —— 于是 `if tar | grep -q ...; then`
# 在**命中时反而判假**，自检形同虚设。实测（同一个包、同一行模式）：
#   无 pipefail              → 0 0 0 0 0（命中）
#   有 pipefail + 管道 + -q  → 2 2 2 2 2（也"命中"，但状态是 2）
#   先取清单再 grep（无管道） → 0 0 0（命中）
# 也就是说下面三道"禁止项"防线（logs / point_labels / app_config）此前一直是**漏的**：
# 真带上了它们也不会报错。v6.5.0 一并修好。
LIST="$(tar -tzf "$TARBALL")"

# 自检 1：包里不许有 logs 内容
if grep -qE '/logs/.+' <<< "$LIST"; then
    echo "✗ 自检失败：包里仍有 logs 内容" >&2
    grep -E '/logs/.+' <<< "$LIST" | head -10 >&2
    rm -f "$TARBALL"
    exit 1
fi

# 自检 2：运行时生成的标签文件不该随包分发
if grep -qE 'configs/point_labels\.yaml$' <<< "$LIST"; then
    echo "✗ 自检失败：包里带上了运行时生成的 configs/point_labels.yaml" >&2
    echo "  （它应由 app_config.yaml 的 point_labels 播种段生成，不该随包分发）" >&2
    rm -f "$TARBALL"
    exit 1
fi

# 自检 3：现场自己的配置（远端密码 / 挂载点 / 序列号）绝不能被包覆盖 —— 见文件头规矩 5。
if grep -qE 'configs/app_config\.yaml$' <<< "$LIST"; then
    echo "✗ 自检失败：包里带上了 configs/app_config.yaml" >&2
    echo "  （那是现场自己的配置；覆盖后现场表现为「远端目录打不开」——密码被清空）" >&2
    rm -f "$TARBALL"
    exit 1
fi

# 自检 4：我们自己维护的录制脚本副本必须都在包里（规矩 6）。
# 漏了不会报错，只会让起录制**静默回落**到机器侧那份 —— 那是本规则要挡的降级。
MISSING=0
for F in scripts/record_vla.sh scripts/record_bag.sh scripts/record_cmd.sh \
         scripts/record_scripts.manifest.json; do
    if ! grep -qx "Data_Collector/$F" <<< "$LIST"; then
        echo "✗ 自检失败：包里缺 $F" >&2
        MISSING=$((MISSING + 1))
    fi
done
if [ "$MISSING" -ne 0 ]; then
    echo "  （起录制会静默回落到机器侧那份脚本 —— 本仓维护的副本就白改了）" >&2
    rm -f "$TARBALL"
    exit 1
fi

ENTRIES="$(wc -l <<< "$LIST" | tr -d ' ')"
SIZE="$(du -h "$TARBALL" | cut -f1)"
echo "✓ 已打包：$TARBALL"
echo "  条目 $ENTRIES，大小 $SIZE，logs 内容 0 项 + 未带运行时标签文件与现场配置 + 录制脚本副本齐全（自检通过）"
