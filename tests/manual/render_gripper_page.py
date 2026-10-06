#!/usr/bin/env python3
"""渲染「夹爪控制」页面在几种关键状态下的样子（离屏），用作交付/评审证据。

    cd <项目根>
    PROBE_SHOT_DIR=/tmp python3 tests/manual/render_gripper_page.py

**探测与下发都换成了桩**：它只构造真实窗口并截图，不会向机器人下发任何指令，
也不会真的跑 `ros2` 命令 —— 可以放心在任何环境里跑。五种情形：

① 主程序在跑 + 双爪已连接（三个动作都可点）
② 主程序在跑 + 一侧未连接（那一侧不给点）
③ 主程序刚起、话题还没就绪（两条路都不走）
④ 主程序没在跑（张开 / 抓取走直测，检测与标定可用，FCC 不给点）
⑤ FCC 抓取进行中（实时 tail 脚本输出显示进度）
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtWidgets import QApplication                              # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                      # noqa: E402

from tests.test_ui_wiring import _app_config, _gripper_probe          # noqa: E402
from data_collector.core import ConfigManager, save_yaml              # noqa: E402
from data_collector import theme                                      # noqa: E402
from data_collector.ui import MainWindow                              # noqa: E402
from data_collector.ui import gripper as gripper_module               # noqa: E402

MODE = "light"

FCC_LOG_LINES = [
    "FCC 实时抓取（左爪）：力矩 0.60 N·m，合拢速度 30 mm/s，夹持 5.0s",
    "  [1/6] 等夹爪关节（最多 15s）",
    "  [2/6] 开启实时模式（FCC）",
    "  [3/6] 张开到位",
    "  [4/6] S 曲线合拢（约 1.3s）",
    "  [5/6] 夹持 5.0s",
]


class _HeldSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _HeldThread:
    """跑任务但不发 finished：让界面停在"动作进行中"那一段。"""

    def __init__(self, task):
        self.task = task
        self.finished = _HeldSignal()

    def start(self):
        self.task()


CASES = (
    ("① 主程序在跑 · 双爪已连接 —— 张开 / 闭合抓取 / FCC 抓取都可点",
     _gripper_probe(True, left=(0.0123, 0.42), right=(-0.02, 0.0))),
    ("② 主程序在跑 · 右爪未连接 —— 只有连上的那一侧可操作",
     _gripper_probe(True, left=(0.0245, 1.10), right=False,
                    reason="主程序在跑，但 右爪 没检测到 —— 末端手掌要在「手臂连接」之后"
                           "才会连上（到遥操作里连接手臂即可）")),
    ("③ 主程序刚起、话题还没就绪 —— 两条路都不走，等几秒再检测",
     _gripper_probe(False, blocked=True,
                    reason="检测到主程序进程，但 ROS 话题还没就绪（可能正在启动中）"
                           "—— 两条路都不适合走，请等几秒后重新检测")),
    ("④ 主程序没在跑 —— 张开 / 抓取走直测工具，检测与标定可用，FCC 不可用",
     _gripper_probe(False, reason="主程序没在跑：走直测工具")),
)


def main() -> None:
    app = QApplication([])
    theme.apply_theme(app, MODE)

    tmp = Path(tempfile.mkdtemp(prefix="render_gripper_"))
    cfg = tmp / "app_config.yaml"
    save_yaml(cfg, _app_config(tmp, nerv_dir=str(tmp / "nerv")))
    save_yaml(tmp / "teleop.yaml", {"teleop": {"calibration": "default"}, "calibrations": {}})

    win = MainWindow(ConfigManager(cfg))
    log = tmp / "fcc.log"
    shots = []

    for title, payload in CASES:
        win.launcher.probe_gripper = (lambda p: (lambda timeout=6.0: p))(payload)
        win._gripper_dialog = None                     # 每种情形新开一个，免得复用旧状态
        win.on_gripper_control()
        dialog = win._gripper_dialog
        dialog.on_recheck()
        for _ in range(4):
            app.processEvents()
        print("  %-16s 路径=%s 左(张=%s,抓=%s,FCC=%s) 工具(检测=%s,标定=%s)" % (
            title[:14] + "…", dialog.route_label.text(),
            dialog.left_open.isEnabled(), dialog.left_grasp.isEnabled(),
            dialog.left_fcc.isEnabled(),
            dialog.btn_scan.isEnabled(), dialog.btn_calibrate.isEnabled()))
        shots.append((title, dialog.grab()))

    # ⑤ FCC 抓取进行中：把下发换成桩（写日志 + 卡住线程），真实地走一遍界面流程
    win.launcher.probe_gripper = (lambda p: (lambda timeout=6.0: p))(
        _gripper_probe(True, left=(0.0123, 0.42), right=(-0.02, 0.0)))
    win._gripper_dialog = None
    win.on_gripper_control()
    dialog = win._gripper_dialog
    dialog.on_recheck()
    for _ in range(4):
        app.processEvents()

    def fake_fcc(side, tau, speed, hold, log_file=None, **kw):
        log.write_text("\n".join(FCC_LOG_LINES) + "\n", encoding="utf-8")
        return list(FCC_LOG_LINES)

    win.launcher.run_gripper_fcc = fake_fcc
    gripper_module.LocalTaskThread = _HeldThread
    dialog._fcc_log_path = lambda side: log
    dialog.hold_spin.setValue(5.0)
    dialog.on_gripper_action("left", "fcc")
    dialog._poll_tail()                                 # 模拟 tail 定时器醒了一次
    for _ in range(4):
        app.processEvents()
    print("  ⑤ FCC 进行中…    左(张=%s,抓=%s,FCC=%s) 闸门=%s" % (
        dialog.left_open.isEnabled(), dialog.left_grasp.isEnabled(),
        dialog.left_fcc.isEnabled(), win.gripper_action_in_flight))
    shots.append(("⑤ FCC 抓取进行中（夹持 5.0s）—— 进度实时 tail 脚本输出；"
                  "该侧三个按钮全禁用、「关闭程序」被拦",
                  dialog.grab()))

    pad, label_h, gap = 16, 30, 12
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

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "夹爪控制页面_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())
    win.close()


if __name__ == "__main__":
    main()
