# 数据采集工具

轮式人形机器人上位机，提供启动主程序、遥操作配置/标定、数据录制功能。

## 安装
```bash
pip install -r requirements.txt
sudo apt install sshpass
```

> 说明：`sshpass` 只在需要免交互 SSH 时才用；SFTP 数据回传由 `paramiko` 完成。

## 运行
```bash
cd ~/test_ws/Data\ Collector
./run.sh
```

## 配置
配置文件位于 `configs/app_config.yaml`（首次运行会自动由 `app_config.example.yaml` 复制生成），
可指定工作空间、脚本路径、数据目录（`bag_dir`）、日志目录（`log_dir`）以及数据回传的
SFTP 主机（`remote_host/remote_port/remote_user/remote_base`）。

密码不推荐写进配置文件，可用环境变量覆盖：
```bash
export DATA_COLLECTOR_REMOTE_PASSWORD=xxx   # SFTP 密码
export DATA_COLLECTOR_SUDO_PASSWORD=xxx     # sudo 密码
```

> 注意：`teleop_config.yaml` 由本工具读写（标定、切换用户）。切换用户/新增标定时
> 会保留 `limits` 等其它顶层字段，不会覆盖机器人控制参数。

## 测试
```bash
python3 -m pytest -q
```
