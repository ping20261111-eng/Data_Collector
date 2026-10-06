#!/usr/bin/env python3
"""渲染"选中态"配色（菜单项 / 下拉列表项 / 侧栏按钮）作为证据。

    cd <项目根>
    PROBE_SHOT_DIR=/home/nerv/WorkBuddy/<会话目录> python3 tests/manual/render_selection_colors.py

选中态要一眼看出是**强调色（青碧）**，不是 Qt 默认的蓝，也不是"浅灰"。
只构造窗口，不下发任何指令。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtWidgets import QApplication, QMessageBox                 # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                       # noqa: E402

from tests.test_ui_wiring import (_app_config, _gripper_probe,         # noqa: E402
                                  _inline_task_thread, _seed_arm_config)

_YES = QMessageBox.Yes
QMessageBox.question = staticmethod(lambda *a, **k: _YES)

from data_collector.core import ConfigManager, save_yaml               # noqa: E402
from data_collector import theme                                       # noqa: E402
from data_collector.ui import MainWindow                               # noqa: E402


class _MP:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def main() -> None:
    app = QApplication([])
    theme.apply_theme(app, "light")

    tmp = Path(tempfile.mkdtemp(prefix="render_sel_", dir="/home/nerv"))
    cfg = tmp / "app_config.yaml"
    save_yaml(cfg, _app_config(tmp, nerv_dir=str(tmp / "nerv")))
    save_yaml(tmp / "teleop.yaml", {"teleop": {"calibration": "default"}, "calibrations": {}})
    _seed_arm_config(tmp)

    win = MainWindow(ConfigManager(cfg))
    win.resize(1440, 900)
    win.show()
    _inline_task_thread(_MP())
    for _ in range(6):
        app.processEvents()

    # ① 菜单：把第 2 项设为 active（= 悬停/键盘选中），Qt 两者同一个状态
    menu = win.btn_robot_menu.menu()
    actions = [a for a in menu.actions() if a.text()]
    menu.setActiveAction(actions[1])
    menu.adjustSize()
    for _ in range(3):
        app.processEvents()
    print("菜单选中项:", actions[1].text())

    # ② 下拉列表：打开末端参数页的类型下拉，当前项就是选中态
    win._end_effector_dialog = None
    win.on_end_effector()
    dialog = win._end_effector_dialog
    dialog.adjustSize()
    for _ in range(4):
        app.processEvents()
    combo = dialog.type_combo
    view = combo.view()
    combo.showPopup()
    for _ in range(4):
        app.processEvents()
    print("下拉选中项:", combo.currentText())

    pad, label_h, gap = 16, 30, 12
    # 下拉弹层的高亮在离屏环境画不出来（弹层没真的 map 上），所以证据里只放菜单那一张 ——
    # 下拉的两条 QSS 由 `test_theme.py::test_selection_states_use_the_accent_color` 钉住，
    # 比截一张"看不出高亮"的图可靠。
    shots = [("① 菜单选中项 = 浅青底 + 强调色文字（实测 #E3F1F2 底 / #0E7C86 字，不是 Qt 默认蓝）",
              menu.grab()),
             ("② 主张调色按钮（对照）：「启动程序」实心强调色 + 白字 —— 同一套语言",
              win.btn_launch.grab())]
    combo.hidePopup()

    width = max(s.width() for _t, s in shots) + pad * 2
    height = sum(label_h + s.height() + gap for _t, s in shots) + pad
    out = QImage(width, height, QImage.Format_ARGB32)
    out.fill(QColor(theme.palette("light")["bg"]))
    painter = QPainter(out)
    painter.setPen(QColor(theme.palette("light")["text"]))
    y = pad
    for title, shot in shots:
        painter.drawText(pad, y + 16, title)
        y += label_h
        painter.drawPixmap(pad, y, shot)
        y += shot.height() + gap
    painter.end()

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "选中态配色_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())
    win.close()


if __name__ == "__main__":
    main()
