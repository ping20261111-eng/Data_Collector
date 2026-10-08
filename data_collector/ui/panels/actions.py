"""动作区（按钮矩阵）构建（MainWindow 的一个面板）。

方法自原 MainWindow 原样搬迁，逻辑未改 —— 拆分只为把 3455 行的单文件
拆回可读规模。改动这个面板时只需要打开这一个文件。
"""
import os
import re
import subprocess
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication, QAction)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygon
from PyQt5.Qt import QDesktopServices
import time
import traceback
import shutil
import zipfile
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from ... import theme
from ...core import ConfigManager, logger, ProcessManager, DEFAULT_ACTION_LABELS
from ...core_operations import (Launcher, TeleopManager, SdkServiceManager,
                              read_robot_status, EMERGENCY_STOP_TARGETS,
                              EMERGENCY_STOP_PART_NAMES)
from ...core_data import RecorderManager, DataUploader, LogFetcher
from ...core_data import UploadCancelledError

from ..common import (SP, make_panel_head)




class ActionsMixin:
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
        self.btn_teleop.setToolTip("启动遥操作（后台运行、日志实时回显；运行中同一按钮变为停止）")
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

        # ---- 第三行（导出目标展开）：只有选了移动硬盘才出现 ----
        # 放在「数据」行下面而不是新起一个卡片：它属于"数据往哪去"这一组，
        # 而这一组现有的高度就是一行 —— 挂载点行默认隐藏，选了才占地方。
        body.addWidget(self._build_disk_mount_row())
        # 首次进来按配置回填，并做一次探测（配置里本来就在用硬盘的话，
        # 打开界面就该看到"盘已就绪"，而不是等点上传才发现）
        self._sync_export_target_widgets(probe=True)

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

        robot_menu = QMenu(self)
        robot_menu.addAction(self.btn_gripper)
        robot_menu.addAction(self.btn_end_effector)
        robot_menu.addAction(self.btn_posture)
        robot_menu.addSeparator()          # 分隔线代替层级：上面是"开窗口"，下面是"立即执行"
        robot_menu.addAction(self.btn_start_calibration)
        robot_menu.addAction(self.btn_pokepoint)

        self.btn_robot_menu = QPushButton("机器人控制 ▾")
        self.btn_robot_menu.setProperty("variant", "ghost")
        self.btn_robot_menu.setMenu(robot_menu)
        self.btn_robot_menu.setToolTip("机器控制类功能：夹爪控制 / 末端参数 / 姿势控制 / "
                                       "开始标定 / 戳点"
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

    # ---------- 导出目标（数据往哪儿送） ----------
    # 逻辑层在 v6.3.6 就接好了（`DataUploader` 按 `export_target` 分流），
    # 这里只解决"让现场知道能选、并且选完记得住"。
    #
    # 放在「数据」行尾而不是新起一行 / 弹窗：那行右侧本来就整片空白，
    # 而导出目标是**偶发改一次**的设置 —— 弹窗会打断手头的事，
    # 新起一行则要为它长期占一行的位置。

    #: 下拉项：值 → 文案。文案直接说"送到哪儿"，不用 `disk` / `both` 这种内部词
    #: （现场非研发人员不看配置文件，只认界面）。
    EXPORT_TARGET_ITEMS = (
        ("server", "送到：服务器"),
        ("disk", "送到：移动硬盘"),
        ("both", "送到：服务器 + 移动硬盘"),
    )

    def _build_export_target_selector(self) -> QComboBox:
        self.cb_export_target = QComboBox()
        self.cb_export_target.setObjectName("ExportTarget")
        for value, label in self.EXPORT_TARGET_ITEMS:
            self.cb_export_target.addItem(label, value)
        self.cb_export_target.setToolTip(
            "这批数据送到哪儿（写入 app_config.yaml，重启后仍然有效）：\n"
            "· 服务器：走 SFTP 送到远端（缺省，与以前一致）\n"
            "· 移动硬盘：写本地挂载点，完全不连服务器（离线现场用）\n"
            "· 两者都送：先写硬盘、再送服务器")
        self.cb_export_target.currentIndexChanged.connect(self.on_export_target_changed)
        return self.cb_export_target

    def _build_disk_mount_row(self) -> QWidget:
        """挂载点那一行（默认隐藏，选了移动硬盘才出现）。"""
        self.disk_mount_row = QWidget()
        # `InlineRow` = "卡片里按需展开的一行"（见 theme.py）：透明背景，
        # 否则会被全局 `QWidget` 规则涂成一条与卡片不同色的灰带
        self.disk_mount_row.setObjectName("InlineRow")
        row = QHBoxLayout(self.disk_mount_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP["sp2"])

        self.lb_disk_mount = QLabel("挂载点")
        self.lb_disk_mount.setObjectName("FactLabel")
        self.lb_disk_mount.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))
        row.addWidget(self.lb_disk_mount)

        self.ed_disk_mount = QLineEdit()
        self.ed_disk_mount.setObjectName("DiskMount")
        self.ed_disk_mount.setPlaceholderText("/media/robot/NERV")
        # **固定宽，不要让它吃掉行末的富余**：`QLineEdit` 默认水平策略是 Expanding，
        # 与行末的 `addStretch()` 按比例分富余 —— 实测被拉到 570px，
        # 状态文案（"盘已就绪"那句）被推到屏幕另一头，看着跟输入框没关系。
        # 300px 够显示 `/media/robot/NERV` 这类常见路径。
        self.ed_disk_mount.setFixedWidth(300)
        self.ed_disk_mount.setToolTip("移动硬盘挂载到哪个目录（填设备目录本身，不是它下面的子目录）")
        # 失焦才存：边打边存会在路径打到一半时写入一个不存在的路径
        self.ed_disk_mount.editingFinished.connect(self.on_disk_mount_edited)
        row.addWidget(self.ed_disk_mount)

        self.lb_disk_state = QLabel("")
        self.lb_disk_state.setObjectName("FactValue")
        row.addWidget(self.lb_disk_state)

        self.btn_disk_recheck = QPushButton("重新检测")
        self.btn_disk_recheck.setProperty("variant", "ghost-compact")
        self.btn_disk_recheck.setToolTip("盘是后来才插上的时候点它")
        self.btn_disk_recheck.clicked.connect(lambda: self._probe_disk_mount())
        row.addWidget(self.btn_disk_recheck)

        row.addStretch()
        self.disk_mount_row.setVisible(False)
        return self.disk_mount_row

    def _sync_export_target_widgets(self, probe: bool = False) -> None:
        """按当前配置回填两个控件。**不发信号**（回填不是用户改的）。"""
        try:
            settings = self.config.get_export_settings()
        except Exception as exc:                      # 配置读不了不该让界面起不来
            logger.warning(f"读取导出设置失败：{exc}")
            settings = {"target": "server", "mount_point": ""}
        target = settings.get("target", "server")

        self.cb_export_target.blockSignals(True)
        index = self.cb_export_target.findData(target)
        self.cb_export_target.setCurrentIndex(index if index >= 0 else 0)
        self.cb_export_target.blockSignals(False)

        self.ed_disk_mount.setText(settings.get("mount_point", "") or "")
        self._set_disk_row_visible(target in ("disk", "both"))
        if probe and target in ("disk", "both"):
            self._probe_disk_mount()

    def _set_disk_row_visible(self, visible: bool) -> None:
        self.disk_mount_row.setVisible(bool(visible))

    def on_export_target_changed(self) -> None:
        """切换导出目标：立刻写回配置，并（选了硬盘时）探一次盘。

        为什么切换就写：这个设置决定"数据送到哪儿"，只放在内存里的话，
        现场下次开程序会退回服务器 —— 而那时没人会记得自己改过。
        """
        target = self.cb_export_target.currentData()
        self._set_disk_row_visible(target in ("disk", "both"))
        if target in ("disk", "both"):
            self._probe_disk_mount()

        mount = self.ed_disk_mount.text().strip() if target in ("disk", "both") else None
        self._save_export_settings(target, mount,
                                   label=self.cb_export_target.currentText())

    def on_disk_mount_edited(self) -> None:
        """挂载点改完（失焦）才存 —— 边打边存会写入打到一半的路径。"""
        target = self.cb_export_target.currentData()
        if target not in ("disk", "both"):
            return
        self._probe_disk_mount()
        self._save_export_settings(target, self.ed_disk_mount.text().strip(),
                                   label="挂载点")

    def _save_export_settings(self, target: str, mount_point, label: str) -> None:
        """写回 `app_config.yaml`，把结果如实报到日志区。

        写失败**必须说出来**：静默失败会让人以为设置生效了，
        而下次上传还按旧目标走 —— 那是最难查的一类问题。
        """
        try:
            result = self.config.save_export_settings(target, mount_point)
        except Exception as exc:
            logger.error(f"保存导出设置异常：{exc}")
            self.append(f"✖ 导出设置没保存成功：{exc}")
            return
        for line in result["lines"]:
            self.append(line)
        if result["ok"]:
            self.append(f"✔ {label} 已保存")
        else:
            for err in result["errors"]:
                self.append(f"✖ 导出设置没保存成功：{err}")

    def _probe_disk_mount(self) -> None:
        """探一次挂载点，把结果写成一句人话。

        **为什么值得单独探**：`disk_export` 的检查只在"点上传那一刻"跑，
        而现场最贵的事故是"以为导成功了、其实没上盘"—— 路径写错时若那个目录
        恰好存在（本机磁盘上的同名目录），数据就落在本机了，界面上却是成功。
        把结果提前显示出来，工程师在**录之前**就知道盘有没有认出来。
        """
        from ...disk_export import DiskExportTarget      # 延迟导入，避免加载期耦合

        mount = self.ed_disk_mount.text().strip()
        if not mount:
            self.lb_disk_state.setText("请填写移动硬盘挂载点")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_recheck.setVisible(False)
            return

        try:
            settings = self.config.get_export_settings()
            probe = DiskExportTarget(
                mount_point=mount,
                volume_id_file=settings.get("volume_id_file", ".nerv_volume"),
                require_volume_id=bool(settings.get("require_volume_id")),
            )
            path_exists = probe.mount_point.exists()
            mounted = probe.is_mounted() if path_exists else False
            has_marker = probe.has_volume_marker() if mounted else False
            fs = probe.filesystem_type() if mounted else ""
            free = probe.free_space() if mounted else 0
        except Exception as exc:
            logger.warning(f"探测挂载点失败：{exc}")
            self.lb_disk_state.setText(f"探测失败：{exc}")
            theme.set_state(self.lb_disk_state, "warn")
            self.btn_disk_recheck.setVisible(True)
            return

        if not path_exists:
            self.lb_disk_state.setText("✖ 该路径不存在。插上硬盘后点「重新检测」")
            theme.set_state(self.lb_disk_state, "warn")
        elif not mounted:
            self.lb_disk_state.setText("✖ 该路径不是已挂载的设备目录（数据会写在本机磁盘上）")
            theme.set_state(self.lb_disk_state, "warn")
        elif probe.require_volume_id and not has_marker:
            self.lb_disk_state.setText(
                f"✖ 该盘不是数据盘（缺少标识文件 {probe.volume_marker_path()}）")
            theme.set_state(self.lb_disk_state, "warn")
        else:
            parts = ["✔ 盘已就绪"]
            if fs:
                parts.append(fs)
            if free:
                parts.append(f"可用 {free / 1024 ** 3:.1f}GB")
            self.lb_disk_state.setText(" · ".join(parts))
            theme.set_state(self.lb_disk_state, "ok")

        self.btn_disk_recheck.setVisible(True)
