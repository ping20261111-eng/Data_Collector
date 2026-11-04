# ADR-002：ROS 域（domain）的唯一真源放在机器人侧

- **状态**：Accepted（v6.9.12）
- **日期**：2026-11-04
- **相关**：v6.9.11（回退）、机器人侧 `wheeled_robot_x86_64-v1.4.0-develop-202611041708-test`

---

## Context（为什么要做这个决定）

现场要在**机器人侧**跑多台设备 / 多套数据流，需要 ROS 2 的域隔离。机器人侧
v1.4.0-develop 起提供了统一入口：

```bash
./configure_domain.sh domain=10 local_ip=192.168.2.100   # 配置
./configure_domain.sh no_domain                          # 移除
```

脚本生成两个文件，由**客户端启动时调用**：

| 文件 | 内容 |
|---|---|
| `install/setup_domain.bash` | `ROS_DOMAIN_ID=10`、`ROS_LOCALHOST_ONLY=0`、`RMW_IMPLEMENTATION=rmw_fastrtps_cpp`、`FASTRTPS_DEFAULT_PROFILES_FILE`、`FASTDDS_DEFAULT_PROFILES_FILE` |
| `install/config/fastdds.xml` | FastDDS profiles（含 `local_ip` —— **这块机器自己的地址**） |

与此同时，上位机 v6.9.11 刚加了「启动参数」里的 `domain=` 输入框：填了就把它
**导出成 `ROS_DOMAIN_ID`** 注入所有子进程。

**这就出现了两个域的真源。** 后果不是"冲突报错"，而是更难查的症状：

- 两处填不同的值 → 谁后 export 谁赢，取决于启动顺序，现场无法复现；
- 只填了 domain、没配 `local_ip` → 域对上了，但 FastDDS 用的是默认 profile，
  跨机通信照旧不通（"域设置没错啊？"）；
- 有人跑了 `no_domain` 删掉 `setup_domain.bash`，界面上那个框还留着 10 →
  界面里的进程在 10 域、机器人侧在 0 域，界面"什么都看不到"。

## Decision（决定）

1. **回退界面上的 `domain=`**（撤掉 v6.9.11 的输入框与会话级 `ROS_DOMAIN_ID` 注入）。
2. **域的唯一真源 = 机器人侧生成的 `install/setup_domain.bash`。**
3. 上位机只做一件事：**启动每个 ROS 子进程时 source 那个文件**，且放在**统一前缀**里
   （`Launcher._env_prefix()`），而不是各调用点各加一次。
4. 文件**不存在时完全不变**（`if [ -f … ]; then . …; fi`）：老工作空间、没配域的机器，
   命令与改动前逐字一致。

## Consequences（代价与收益）

**变容易的**

- 域、RMW 实现、FastDDS profiles 三者永远一起生效 —— 不会再出现"域对了但不通"；
- 一条命令覆盖全部入口：主程序、录制（bag/vla/cmd）、遥操作、回放、数据验证、
  状态监控、夹爪 FCC、实时流、`vr_bridge`；
- 机器人侧 `no_domain` 之后，上位机**跟着自动回退**，不需要改任何配置。

**变难的 / 要接受的**

- 界面**不能**改域了：要换域必须去机器人侧跑 `configure_domain.sh`（或在界面上做一个
  调用它的按钮 —— 见下）；
- 上位机的子进程继承的是**启动那一刻**的文件内容：跑完脚本后要**重启上位机**
  （或至少重启对应的进程）才生效；
- `install/setup_domain.bash` 是"生成物"，不在版本控制里 —— 换机器就得重新配一次。

## Alternatives（当时考虑过的另外两条）

### A. 只回退，什么都不加（最小改动）

- 优点：零风险，一行代码都不碰；
- 缺点：**上位机自己跑的 ROS 子进程仍然在 0 域** —— 配置了 domain 之后，数据验证
  （`bag_rate_stat.py`）、状态监控（`robot_monitor.py`）会看不到话题，现场会以为
  "验证功能坏了"。所以必须至少做"source 那个文件"这一件事。

### B. 界面调用 `configure_domain.sh`（比现在更进一步）

- 做法：`domain=` 输入框保留，但**不再自己 export**，而是执行
  `./configure_domain.sh domain=<n> local_ip=<ip>`，然后重新 source；
- 优点：现场不用开终端；
- 缺点/未决：
  1. `configure_domain.sh` 在**工作空间根**，但它属于机器人侧交付物 —— 上位机要配
     一个脚本路径（缺了怎么办）；
  2. `local_ip` 从示例看是与 domain 一起给的，**是否必填、能否省略**未知；
  3. 这是**写操作**（还会删文件），误点一次就可能把现场配好的域清掉，得做二次确认。

→ 本轮**没做** B：三个未决点都需要机器人侧确认；而 A+C（回退 + 统一 source）
已经让"配了域就能用"，先把这条打通。

## Verification

| 用例 | 断言 |
|---|---|
| `test_script_env_does_not_inject_ros_domain_id` | 上位机不再往环境里塞 `ROS_DOMAIN_ID`，也不再有 `set_ros_domain_id` |
| `test_domain_setup_is_sourced_in_every_script_command` | 文件存在时，命令里出现 `[ -f … ]` 与 `. <path>` |
| `test_no_domain_setup_means_identical_command` | 文件不存在时，命令里**不含** `setup_domain.bash`（老工作空间行为不变） |
| `test_the_launch_panel_does_not_hold_a_domain_setting` | 界面上不许再出现 domain 输入框（防止又加回来） |

变异：把 `_env_prefix()` 里的 source 去掉 → 第二条立刻红。

## 待确认（要给现场的问题）

1. `configure_domain.sh` 在**测试包**里的确切路径（工作空间根？`scripts/` 下？）——
   要在界面上做按钮必须先知道；
2. `local_ip` 是否必填；配完是否需要重启 `robot_monitor` 之类的常驻进程；
3. 要不要在界面上做「配置域」按钮（走 B 方案），还是继续只在终端里配。

---

## 附：为什么不是"两个都设、取交集"

听起来"界面填的值优先，机器人侧文件兜底"很稳，实际上是最坏的一种：两个值不一致时，
**没有裁判**。ROS 2 只看进程环境里最后一个 export 的值 —— 于是同一个界面里，
主程序与 `ros2 topic pub` 可能落在不同域，而这种故障在现场表现为"偶尔能看到话题"，
排查成本远高于"少一个输入框"。

一个真源 + 一个执行点（统一前缀），是这类"环境变量即契约"问题唯一稳的解法。
