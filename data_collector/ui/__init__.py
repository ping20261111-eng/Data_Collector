"""数据采集上位机的界面层。

原先是单文件 `ui.py`（3455 行，其中 MainWindow 一类占 43%）。按"职责 + 界面区域"拆成包：

| 模块 | 内容 |
| --- | --- |
| `common` | 设计令牌、面板排版辅助、跨面板复用的纯函数 |
| `widgets` | ClickableLabel / LogView / BatteryIndicator |
| `threads` | 6 个后台工作线程 |
| `dialogs` | 一次性对话框（遥操作 / 标定 / 动作标签…）|
| `gripper` | 「夹爪控制」独立页面（宇树 Dex11 张开 / 闭合抓取）|
| `end_effector` | 「末端参数」独立页面（改 arm_control_config.yaml 的张开 / 握紧指令）|
| `posture` | 「姿势控制」独立页面（躯干 / 双臂各轴角度、姿势预设、示意图）|
| `posture_modules` | 「姿势控制」页的**模块使能与躯干模式**（一行两栏、单键随状态、遥操作互斥）|
| `data_view` | 数据浏览对话框 |
| `remote_browser` | 远端 SFTP 目录浏览器 |
| `main_window` + `panels/` | 主窗口与各面板 |

对外接口**保持不变**：`from data_collector.ui import MainWindow` 等写法仍然有效。

PyQt 的名字在这里一并重导出，是因为历史代码与测试直接用 `ui.QDialog` / `ui.QMessageBox`
这样的写法（`import data_collector.ui as ui` 之后访问）。拆包后这些名字落在子模块里，
所以需要在包命名空间上补齐 —— 否则是静默的 AttributeError。
"""
from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QCheckBox, QMessageBox, QTextEdit,
                             QGroupBox, QLabel, QDialog, QLineEdit, QComboBox,
                             QInputDialog, QListWidget, QListWidgetItem,
                             QAbstractItemView, QProgressBar, QFileDialog, QMenu,
                             QFrame, QSizePolicy, QApplication)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygon
from PyQt5.Qt import QDesktopServices

from .common import *           # noqa: F401,F403
from .widgets import *          # noqa: F401,F403
from .threads import *          # noqa: F401,F403
from .dialogs import *          # noqa: F401,F403
from .gripper import *          # noqa: F401,F403
from .end_effector import *     # noqa: F401,F403
from .posture import *          # noqa: F401,F403
from .posture_modules import *  # noqa: F401,F403
from .data_view import *        # noqa: F401,F403
from .remote_browser import *   # noqa: F401,F403
from .main_window import *      # noqa: F401,F403
