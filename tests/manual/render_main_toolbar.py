#!/usr/bin/env python3
"""渲染主界面（工具行 + 「机器人控制 ▾」菜单展开）与三个控制页面，作交付/评审证据。

    cd <项目根>
    PROBE_SHOT_DIR=/home/nerv/WorkBuddy/<会话目录> python3 tests/manual/render_main_toolbar.py

只构造窗口并截图，不下发任何指令、不跑任何脚本。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtWidgets import QApplication, QMessageBox                # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                      # noqa: E402

from tests.test_ui_wiring import (_app_config, _gripper_probe,        # noqa: E402
                                  _inline_task_thread, _seed_arm_config)

_YES = QMessageBox.Yes
QMessageBox.question = staticmethod(lambda *a, **k: _YES)

from data_collector.core import ConfigManager, save_yaml              # noqa: E402
from data_collector import theme                                      # noqa: E402
from data_collector.ui import MainWindow                              # noqa: E402

MODE = "light"


class _MP:
    """极简 monkeypatch 替身：只用来换 LocalTaskThread。"""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def main() -> None:
    app = QApplication([])
    theme.apply_theme(app, MODE)

    tmp = Path(tempfile.mkdtemp(prefix="render_main_", dir="/home/nerv"))
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

    # 运行日志区能放几行（现场问"20 行以上？"）
    row = win.log.fontMetrics().lineSpacing()
    print("窗口 %d x %d ｜ 运行日志区 %dpx ≈ %d 行" % (
        win.size().width(), win.size().height(), win.log.height(),
        max(0, win.log.height() // max(1, row))))

    menu = win.btn_robot_menu.menu()
    menu.adjustSize()
    for _ in range(3):
        app.processEvents()
    print("菜单项:", [a.text() for a in menu.actions() if a.text()])

    # **先在打开页面之前抓主界面**：非模态对话框 show() 之后主窗口会重新布局一次
    # （急停柱的内容会重新居中），主界面就会拍成"急停跑到中间去"的样子 —— 那是截图
    # 顺序的问题，不是界面变了。
    pad, label_h, gap = 16, 30, 12
    shots = [("主界面 1440×900 —— 工具行只剩 数据验证 / 性能监控 / 机器人控制 ▾", win.grab()),
             ("「机器人控制 ▾」展开 —— 一层到底 5 项，用分隔线分组（开窗口的带 …）", menu.grab())]

    # 两个控制页面（骨架统一后的样子）。「模块使能」独立页 v6.0.0 已删，并进姿势控制
    win.launcher.probe_gripper = lambda timeout=6.0: _gripper_probe(False)
    pages = []
    for opener, attr in ((win.on_gripper_control, "_gripper_dialog"),
                         (win.on_end_effector, "_end_effector_dialog")):
        setattr(win, attr, None)
        opener()
        dialog = getattr(win, attr)
        dialog.adjustSize()
        for _ in range(4):
            app.processEvents()
        pages.append((dialog.windowTitle(), dialog.grab()))

    shots += [(f"{title} 页面 —— 与另两页同一套骨架（白卡片 + 药丸徽章 + 结果区自适应）", shot)
              for title, shot in pages]

    width = max(shot.width() for _t, shot in shots) + pad * 2
    height = sum(label_h + shot.height() + gap for _t, shot in shots) + pad
    out = QImage(width, height, QImage.Format_ARGB32)
    out.fill(QColor(theme.palette(MODE)["bg"]))
    painter = QPainter(out)
    painter.setPen(QColor(theme.palette(MODE)["text"]))
    y = pad
    for title, shot in shots:
        painter.drawText(pad, y + 16, title)
        y += label_h
        painter.drawPixmap(pad, y, shot)
        y += shot.height() + gap
    painter.end()

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "主界面与控制页面_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())
    win.close()


if __name__ == "__main__":
    main()
