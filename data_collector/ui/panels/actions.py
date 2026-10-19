"""动作区（按钮矩阵）构建（MainWindow 的一个面板）。

方法自原 MainWindow 原样搬迁，逻辑未改 —— 拆分只为把 3455 行的单文件
拆回可读规模。改动这个面板时只需要打开这一个文件。
"""
from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QLabel, QProgressBar, QMenu,
                             QFrame, QSizePolicy, QAction, QMessageBox)
from ... import theme
from ...core_motion import (HEAD_DATA_AXIS_INDEX, HEAD_DATA_DURATION_SEC,
                            HEAD_DATA_HOLD_SEC, HEAD_DATA_RATE, HEAD_DATA_TARGET_DEG,
                            TRUNK_LOCK_AXES_TEXT)

from ..common import (SP, make_panel_head)
from ..threads import LocalTaskThread
from .export_target import ExportTargetMixin




class ActionsMixin(ExportTargetMixin):
    """动作区（按钮矩阵）构建。"""

    def _build_actions(self):
        """操作区：按「设备 / 数据 / 工具」三行分组，行内同权重、同尺寸。

        改按钮之前先读这三条纪律：
        1. **就地状态按钮**：凡是"启动一个长任务"的动作，都用同一个按钮表达两个状态
           （启动遥操作 ⇄ 停止遥操作、数据录制 ⇄ 停止录制、上传数据 ⇄ 取消上传），
           不再另设一个"停止 X"按钮 —— 现场找"停止"不该靠扫视整屏。
        2. **行内同尺寸**：几何统一由 theme.py 的 `role="actions"` 规则给（44px），
           层级只靠描边/底色区分，不靠"谁比谁高"（幽灵药丸与默认按钮原本差 10px，
           并排时肉眼可见地参差）。
        3. **低频进二级菜单**：相机标定/戳点/性能监控不占常驻按钮位；菜单按钮
           文案必须自带 ▾ —— QSS 覆盖过 QPushButton 之后 Qt 不再画下拉箭头，
           不写用户根本看不出能展开。
        """
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setProperty("role", "actions")     # theme.py 据此统一按钮几何
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head, _ = make_panel_head("操作")
        outer.addWidget(head)

        body = QVBoxLayout()
        body.setContentsMargins(SP["sp4"], SP["sp3"], SP["sp4"], SP["sp4"])
        body.setSpacing(SP["sp3"])

        # ---- 第一行：设备生命周期 ----
        # 启动程序：整个界面唯一的主操作。实心强调色 + 更大的尺寸，现场扫一眼
        # 就能找到（见 theme.py 的 variant="launch"）。
        self.btn_launch = QPushButton("启动程序")
        self.btn_launch.setProperty("variant", "launch")
        # 尺寸不在这里写死：高度/内距由 theme.py 的 variant="launch" 决定
        self.btn_launch.setToolTip("启动机器人主程序（会先检查主程序是否已在运行）")
        self.btn_launch.clicked.connect(self.on_launch)

        # 遥操作与录制都是"启停一个长任务"：用就地状态按钮，运行中文案与语义翻转
        # （遥操作在 teleop.py 切换，录制在 data_ops.py 的 on_record_toggle 分派）。
        # 空闲态文案带状态词（"遥操作 · 未启动"）：按钮自己把"现在是什么状态、
        # 按下去会怎样"说完整，现场不用把视线挪到下面那行状态字。
        self.btn_teleop = QPushButton("遥操作 · 未启动")
        # 文案随状态换词（未启动/已停止/停止遥操作），固定最小宽度避免按钮跳动
        self.btn_teleop.setMinimumWidth(150)
        self.btn_teleop.setToolTip("启动遥操作（后台运行、日志实时回显；运行中同一按钮变为停止）\n"
                                   "快捷键 F5 = 遥操作开关")
        self.btn_teleop.clicked.connect(self.on_teleop)

        self.btn_record = QPushButton("数据录制")
        self.btn_record.setToolTip("开始数据录制（弹窗选类型与任务标签；录制中同一按钮变为停止录制）")
        self.btn_record.clicked.connect(self.on_record_toggle)

        # 打点：录制过程中给"这一刻"打标注。**只在录制进行中显示**（见
        # data_ops._set_record_button）—— 打点必须挂在一次录制上（相对时间要有起点），
        # 不录制时摆一个灰按钮只会让人以为它坏了
        self.btn_point = QPushButton("打点")
        self.btn_point.setToolTip(
            "打开打点面板（录制开始时已自动弹出，这里用于收起后再打开）")
        self.btn_point.clicked.connect(self.on_point)
        self.btn_point.setVisible(False)

        # 关闭程序是破坏性动作：靠 stretch 与主操作**拉开整段距离**（原先只隔 12px，
        # 误点代价是整个机器人停机），并且点下去要二次确认
        self.btn_stop_main = QPushButton("关闭程序")
        self.btn_stop_main.setProperty("variant", "danger")
        self.btn_stop_main.setToolTip("停止机器人主程序并清理残留 ROS 节点（会二次确认）")
        self.btn_stop_main.clicked.connect(self.on_stop_main)

        primary = QHBoxLayout()
        primary.setSpacing(SP["sp3"])
        primary.addWidget(self.btn_launch)
        primary.addWidget(self.btn_teleop)
        primary.addWidget(self.btn_record)
        primary.addWidget(self.btn_point)      # 紧挨录制：录的时候才打点
        primary.addStretch()
        primary.addWidget(self.btn_stop_main)
        body.addLayout(primary)

        # ---- 启动参数与运行状态 ----
        # joy / rviz **默认勾选**（现场要求）：这两项是上台调试的常态 ——
        # 手柄遥控与可视化各少点一次，也就少了"启动后发现忘了勾"的重启。
        # 注意这是"默认值"不是"锁定值"：不需要时现场照样可以取消勾选。
        # 不持久化用户的取消动作：默认值一旦被记住，换个人/换台机器会出现
        # "上次谁把它关了"这种查不出来的差异 —— 每次启动都从同一个已知状态出发。
        self.cb_joy = QCheckBox("joy")
        self.cb_rviz = QCheckBox("rviz")
        self.cb_sim = QCheckBox("use_sim")
        for box in (self.cb_joy, self.cb_rviz):
            box.setChecked(True)
            box.setToolTip("默认勾选（不需要时可取消）：不勾就不会作为参数传给 run_nodes.sh")
        # 相机只开 RGB、不启深度与点云（对应 launch 的 rgb_only，用于省算力/省带宽）
        self.cb_rgb_only = QCheckBox("rgb_only")
        self.cb_rgb_only.setToolTip("相机只启动 RGB 模式，禁用深度与点云")

        # 手型：真机与仿真都只读 Product.json 的 hand 字段，界面不做二次选择。
        # （机器人侧 run_nodes.sh 不解析 hand= 参数，手型一律由 product.sh 决定。）
        hand_label = QLabel("末端手型")
        hand_label.setObjectName("FactLabel")
        hand_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))

        self.hand_product_label = QLabel("—")
        self.hand_product_label.setObjectName("FactValue")
        self.hand_product_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))

        self.teleop_progress_bar = QProgressBar()
        self.teleop_progress_bar.setRange(0, 0)
        self.teleop_progress_bar.setTextVisible(False)
        self.teleop_progress_bar.setFixedWidth(120)
        self.teleop_progress_bar.hide()

        self.teleop_status_label = QLabel("遥操作 未启动")
        self.teleop_status_label.setObjectName("FactValue")
        self.teleop_status_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))

        self.record_status_label = QLabel("未录制")
        self.record_status_label.setObjectName("FactValue")
        self.record_status_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_sm"]))

        # ---- 启动参数（第一行）----
        # 参数与运行状态拆成两行：挤在一行时，窗口一缩小布局就把勾选框压到最小宽度，
        # 文字被裁掉、指示器甚至压在文字上（实测缩窗就是这个样子）。
        param_row = QHBoxLayout()
        param_row.setSpacing(SP["sp2"])
        param_row.addWidget(hand_label)
        param_row.addWidget(self.hand_product_label)     # 只读展示 Product.json 的手型
        param_row.addSpacing(SP["sp3"])
        for box in (self.cb_joy, self.cb_rviz, self.cb_sim, self.cb_rgb_only):
            # 勾选框固定为自身尺寸：宁可让窗口撑开，也不要把「rgb_only」裁成「rgb_o」
            box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            param_row.addWidget(box)
        param_row.addStretch()
        body.addLayout(param_row)

        # ---- 运行状态（第二行）----
        state_row = QHBoxLayout()
        state_row.setSpacing(SP["sp2"])
        state_row.addWidget(self.teleop_status_label)
        state_row.addWidget(self.teleop_progress_bar)
        state_row.addSpacing(SP["sp4"])
        state_row.addWidget(self.record_status_label)
        state_row.addStretch()
        body.addLayout(state_row)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        body.addWidget(divider)

        # ---- 第二行（次级）：数据（高频，统一描边按钮，不再用药丸削弱）----
        self.btn_upload = QPushButton("上传数据")
        self.btn_upload.clicked.connect(self.on_upload)
        self.btn_view_data = QPushButton("查看数据")
        self.btn_view_data.clicked.connect(self.on_view_data)
        self.btn_play_data = QPushButton("播放数据")
        self.btn_play_data.clicked.connect(self.on_play_data)
        self.btn_fetch_log = QPushButton("抓取日志")
        self.btn_fetch_log.clicked.connect(self.on_fetch_log)

        data_label = QLabel("数据")
        data_label.setObjectName("FactLabel")
        data_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))

        secondary = QHBoxLayout()
        secondary.setSpacing(SP["sp2"])
        secondary.addWidget(data_label)
        for btn in (self.btn_upload, self.btn_view_data,
                    self.btn_play_data, self.btn_fetch_log):
            secondary.addWidget(btn)
        secondary.addStretch()
        secondary.addWidget(self._build_export_target_selector())
        body.addLayout(secondary)

        # ---- 挂载点配置在哪 ----
        # v6.4.4 起它住在**芯片展开的面板里**，不再占主卡片的一行。
        # 原来那行挂在「数据」按钮行下面，把同一个组从中间劈成上下两块
        # （实测看着像两件事），而它平时还是默认隐藏的 —— 也就是说那行位置
        # 要么空着、要么劈开，没有第三种状态。
        # 首次进来按配置回填，并做一次探测（配置里本来就在用硬盘的话，
        # 打开界面就该看到"硬盘就绪"，而不是等点上传才发现）
        self._sync_export_target_widgets(probe=True)
        # 芯片在默认态就显示状态，所以要有一个低频探测跟着盘的实际状态变
        # （现场是"插上/拔掉"这种物理动作，不可能指望用户点一下才知道）。
        # 幂等：重复调用不会起第二个定时器。
        self._start_disk_probe_timer()

        # ---- 第三行（三级）：工具（低频，药丸幽灵按钮 + 二级菜单）----
        self.btn_bag_stat = QPushButton("采集统计")
        self.btn_bag_stat.setProperty("variant", "ghost")
        self.btn_bag_stat.clicked.connect(self.on_bag_rate_stat)

        # 性能监控：可见的低频工具。原本藏进「更多 ▾」菜单，但菜单里只有它一项 ——
        # 多一次点击换不来任何收纳收益，计划图里也是常驻的（点开才知道有它更糟）。
        self.btn_monitor_perf = QPushButton("性能监控")
        self.btn_monitor_perf.setProperty("variant", "ghost")
        self.btn_monitor_perf.setToolTip("录制性能监控（录制开始后自动采样，结束自动汇总）")
        self.btn_monitor_perf.clicked.connect(self.on_monitor_performance)

        # ---- 机器人控制（一个二级菜单，一层到底）----
        # 现场要求："对机器控制的功能全放在一个二级菜单内"。理由在这里写清楚，
        # 免得以后有人又往工具行加按钮：
        #   ① 工具行原本 6 个按钮，其中 4 个是机器控制类，而且是**每加一个功能塞一个**
        #      （v5.1 模块使能 → v5.2 夹爪控制 → v5.4 末端参数），再往下就会折行或把按钮
        #      压到最小宽度（"启动参数"那行踩过：rgb_only 被裁成 rgb_o）；
        #   ② 同一行里混着"点开是窗口"（模块使能/夹爪控制/末端参数）与"点下去立即动手"
        #      （戳点/开始标定）两类东西，后果不一致，容易误点。
        #
        # 三条菜单约定：
        #   · **一层到底、不做三级**：相机标定原本是「相机标定 ▾」子菜单，塞进来就成了三级 ——
        #     触屏与快速点选下都容易点错，所以平铺进同一个菜单、用分隔线分组代替层级；
        #   · **菜单项一律可点、不置灰**：Qt 菜单项的悬停提示不可靠，灰着又没理由会被读成
        #     "坏了"（按钮上吃过这个亏，才立了"每个禁用按钮都有悬停说明"的规矩）——
        #     点开页面后由页面说明当前为什么不能操作；
        #   · **菜单项名称写全、不带省略号**（现场 2026-09-28 要求：「下拉菜单的名称补全，
        #     不要有…」）—— 原来的「夹爪控制…」那三点省得没道理：现场是触屏点选，
        #     省略号既没提示"会开窗口"（下面有分隔线分组），又让名字看着像没写完。
        #
        # 「开始标定」已合并"启动相机标定节点"（现场：点开始标定后先启动节点，确定节点
        # 启动后开始标定），所以不再单独给那个入口；`on_camera_calibration` 方法留着排障用。
        self.btn_gripper = QAction("夹爪控制", self)
        self.btn_gripper.setToolTip("独立页面：宇树夹爪的左/右张开与闭合抓取"
                                    "（起程序走话题、不起程序走直测工具）")
        self.btn_gripper.triggered.connect(self.on_gripper_control)

        self.btn_end_effector = QAction("末端参数", self)
        self.btn_end_effector.setToolTip("独立页面：改末端执行器的张开/握紧指令"
                                         "（灵巧手 / 宇树夹爪，保存后需重启主程序生效）")
        self.btn_end_effector.triggered.connect(self.on_end_effector)

        # 姿势控制：唯一会**连续动机构**的一页（点一下就走一个姿势）—— 所以它的确认框里
        # 带示意图、默认按钮是「取消」，而且多步预设可中止。入口仍在机器控制菜单里：
        # 它属于"专门去操作"的事，不占主界面的常驻按钮位
        self.btn_posture = QAction("姿势控制", self)
        self.btn_posture.setToolTip("独立页面：躯干 / 双臂各轴角度与姿势预设"
                                    "（点预设即运动，有二次确认与示意图）")
        self.btn_posture.triggered.connect(self.on_posture)

        self.btn_start_calibration = QAction("开始标定（自动启动节点）", self)
        self.btn_start_calibration.setToolTip("先起相机标定节点，等它就绪后再下发采集指令")
        self.btn_start_calibration.triggered.connect(self.on_start_calibration)

        self.btn_pokepoint = QAction("戳点", self)
        self.btn_pokepoint.setToolTip("直接启动戳点节点（会起节点，不必先开标定）")
        self.btn_pokepoint.triggered.connect(self.on_pokepoint)

        # 躯干自锁（v6.4.15，现场要求）：把 H1~H3 转成「仅上电」—— 还能读角度，
        # 但**不接受运动指令**。现场要的是"不进页面、在菜单里点一下就锁"。
        #
        # 与姿势控制页里躯干那一行的「自锁」按钮**共用同一个方法**（`on_trunk_lock`）：
        # 逻辑只有一处，两个入口只是两个触发点，方向也只有一个（自锁）。
        # v6.5.7 起**不再记录"是否已自锁"** —— 那个状态跟着遥操作进程走
        # （重启遥操作即重置），上位机记不住真相（详见 `on_trunk_lock` 的 docstring）。
        #
        # 单独占一组（前面再加一条分隔线）：上面那两条是"起节点"，
        # 这条是**直接改机构状态**（让电机退出使能）—— 混在一组里会被读成"也是起个节点"。
        self.btn_trunk_lock = QAction("躯干自锁", self)
        self.btn_trunk_lock.setToolTip(
            f"把躯干 {TRUNK_LOCK_AXES_TEXT}（脚踝 / 膝 / 大腿）切成「仅上电」："
            "之后仍然能读角度，但不接受运动指令。\n"
            "重复点没有副作用；解除方式：重启遥操作"
            "（自锁状态跟着遥操作进程走，重启即重置）。\n"
            "提示：自锁等于「关节失去保持力矩」—— 若躯干当前不在支撑位，可能自行下沉")
        # ⚠️ **不能直接 `connect(self.on_trunk_lock)`**：`QAction.triggered` 自带一个
        # `checked` 参数（这个 QAction 不可勾选，恒为 False）。现在 `on_trunk_lock`
        # 不收这个参数了，直接连会 TypeError；而且一旦有人给它加回参数，`checked=False`
        # 会被当成"方向"传进去 —— 那正是 2026-10-16 报的「页面里能用、菜单里不行」。
        # 用 lambda 明确不传参。
        self.btn_trunk_lock.triggered.connect(lambda *_args: self.on_trunk_lock())

        # 头部至 -32°（采数，现场 2026-10-15 要求）：与自锁一样"不进页面、菜单里点一下"，
        # 现场用它做采数前的定位。**点一下 = 读一次当前角度 → 只把 H6 改成目标 → 平滑到位**，
        # 其余 5 轴保持不动（做法与依据见 `Launcher.head_to_data_position`）。
        #
        # 与自锁放在同一组（都属于"直接改机构状态"），并**照项目一致的做法走二次确认**：
        # 它会真的让头部动起来，而且目标 -32° 超出文档量程（见下）—— 点错一次就是一次
        # 机构动作。确认框里把三件事说清：只动一个轴、超出文档量程、遥操作在跑时会互相覆盖。
        self.btn_head_minus32 = QAction(f"头部至 {HEAD_DATA_TARGET_DEG:g}°（采数）", self)
        self.btn_head_minus32.setToolTip(
            f"把头部 H{HEAD_DATA_AXIS_INDEX + 1}（颈 · 抬头）走到 "
            f"{HEAD_DATA_TARGET_DEG:g}°，其余轴保持当前角度。\n"
            f"会先读一次当前角度（读不到就不动）；{HEAD_DATA_DURATION_SEC:g}s / "
            f"{HEAD_DATA_RATE}Hz 平滑到位，之后按目标保持 {HEAD_DATA_HOLD_SEC:g}s。\n"
            f"⚠️ 该角度超出文档量程（文档写 H{HEAD_DATA_AXIS_INDEX + 1} 为 ±30°）"
            "—— 这是现场采数要的位姿；通用下发路径仍然按文档量程拦。")
        self.btn_head_minus32.triggered.connect(self.on_head_minus32)

        self.btn_fullscreen = QAction("全屏显示", self)
        self.btn_fullscreen.setShortcut("F11")
        self.btn_fullscreen.setToolTip("铺满整块屏幕（F11 也可切换）。"
                                       "放大后的界面在窗口里看要留出标题栏与边框，"
                                       "全屏能多出约 40px 高度给内容"
                                       "（姿势控制页正卡在这个高度上）")
        self.btn_fullscreen.triggered.connect(self.on_toggle_fullscreen)

        robot_menu = QMenu(self)
        robot_menu.addAction(self.btn_gripper)
        robot_menu.addAction(self.btn_end_effector)
        robot_menu.addAction(self.btn_posture)
        robot_menu.addSeparator()          # 分隔线代替层级：上面是"开窗口"，下面是"立即执行"
        robot_menu.addAction(self.btn_start_calibration)
        robot_menu.addAction(self.btn_pokepoint)
        robot_menu.addSeparator()          # 上面是"起节点"，这两条是"直接改机构状态"
        robot_menu.addAction(self.btn_trunk_lock)
        robot_menu.addAction(self.btn_head_minus32)
        robot_menu.addSeparator()          # 上面都是"操作机器人"，这条是"操作这个窗口"
        robot_menu.addAction(self.btn_fullscreen)
        # 菜单项名字随「本页下发过什么」变（自锁 / 解除自锁）。本菜单的约定是
        # **一律可点、不置灰**（Qt 菜单项的悬停提示不可靠，灰着又没理由会被读成"坏了"），
        # 所以"现在点下去会做什么"只能靠文案说清 —— 每次弹出前刷一次。
        robot_menu.aboutToShow.connect(self._sync_robot_menu)

        self.btn_robot_menu = QPushButton("机器人控制 ▾")
        self.btn_robot_menu.setProperty("variant", "ghost")
        self.btn_robot_menu.setMenu(robot_menu)
        self.btn_robot_menu.setToolTip("机器控制类功能：夹爪控制 / 末端参数 / 姿势控制 / "
                                       "开始标定 / 戳点 / 躯干自锁 / 头部至 -32° / 全屏显示"
                                       "（都直接操作机器人本体；上、下使能在「姿势控制」页里）")

        # 停止相机标定：只在标定节点运行期间出现 —— 空闲时既不该占位、也不该
        # 用灰按钮表达"没什么可停"（那会被现场读成"坏了"）。
        # 语义是"终止指令 + 停止节点进程"，与遥操作里的「取消 VR 标定」不是一回事。
        # **这一条本期没动**（现场："停止标定的设计先不改"）：仍在工具行、仍按需出现。
        self.btn_cancel_calibration = QPushButton("停止相机标定")
        self.btn_cancel_calibration.setProperty("variant", "danger")
        self.btn_cancel_calibration.setToolTip("终止标定指令并停止相机标定节点进程")
        self.btn_cancel_calibration.clicked.connect(self.on_cancel_calibration_call)
        self.btn_cancel_calibration.setVisible(False)

        tool_label = QLabel("工具")
        tool_label.setObjectName("FactLabel")
        tool_label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))

        tools = QHBoxLayout()
        tools.setSpacing(SP["sp2"])
        tools.addWidget(tool_label)
        for btn in (self.btn_bag_stat, self.btn_monitor_perf, self.btn_robot_menu,
                    self.btn_cancel_calibration):
            tools.addWidget(btn)
        tools.addStretch()
        body.addLayout(tools)

        outer.addLayout(body)
        return panel

