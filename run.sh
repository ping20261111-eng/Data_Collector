#!/usr/bin/env bash
# 在项目根目录下启动上位机（configs/app_config.yaml 相对于项目根定位）
set -e
cd "$(dirname "$0")"
python3 -m data_collector.main
