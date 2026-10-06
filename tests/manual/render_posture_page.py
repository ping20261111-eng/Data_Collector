#!/usr/bin/env python3
"""渲染「姿势控制」页面与运动确认框（含示意图），作交付/评审证据。

    cd <项目根>
    PROBE_SHOT_DIR=/home/nerv/WorkBuddy/<会话目录> python3 tests/manual/render_posture_page.py

只构造窗口并截图，**不下发任何指令、也不起实时流**（`publish_motion` 与
`run_motion_streams` 都被换成记录器）。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox          # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                       # noqa: E402

from tests.test_ui_wiring import _app_config, _inline_task_thread       # noqa: E402

MODE = "light"
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)

from data_collector import core_motion, theme                          # noqa: E402
from data_collector.core import ConfigManager, save_yaml               # noqa: E402
from data_collector.ui import MainWindow                               # noqa: E402
from data_collector.ui import posture as posture_module                # noqa: E402

SENT = []


class _MP:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _status(nerv: Path, *, trunk=True, arms=True):
    module_state = {
        "available": True, "stale_after": 2.0, "note": None,
        "trunk_state": {"has_data": trunk, "age_sec": 0.2, "enabled": trunk,
                        **({"joint_deg": [0.0, -42.1, -18.6, 1.2, 12.0, -3.4]}
                           if trunk else {})},
        "arm_state": {"has_data": arms, "age_sec": 0.2, "enabled": arms,
                      "enabled_left": arms, "enabled_right": arms,
                      **({} if not arms else {
                          "left": {"connected": True, "power_on": True, "moving": False,
                                   "emergency_stop": False,
                                   "joint_deg": [62.2, 68.3, -22.6, 22.3, 31.6, 17.3, 0.0]},
                          "right": {"connected": True, "power_on": True, "moving": False,
                                    "emergency_stop": False,
                                    "joint_deg": [-62.2, 68.3, 22.6, 22.3, -31.6, 17.3, 0.0]}})},
    }
    path = nerv / "status" / "robot_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "updated_at": time.time(), "started_at": time.time() - 60,
        "monitor_pid": os.getpid(), "has_data": True,
        "status": {"power_percent": 88, "emergency": "解锁"},
        "module_state": module_state}, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    app = QApplication([])
    theme.apply_theme(app, MODE)

    tmp = Path(tempfile.mkdtemp(prefix="render_posture_", dir="/home/nerv"))
    nerv = tmp / "nerv"
    cfg_file = tmp / "app_config.yaml"
    save_yaml(cfg_file, _app_config(tmp, nerv_dir=str(nerv), robot_monitor_ttl=3600))
    save_yaml(tmp / "teleop.yaml", {"teleop": {"calibration": "default"}, "calibrations": {}})
    # 预设直接取机器人侧那两份真文件（照实拍，别造一份"好看的样本"）
    real = Path("/home/nerv/test_ws/wheeled_robot_pkg/install/product/config")
    folder = tmp / "install" / "product" / "config"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("arm_motion.json", "trunk_motion.json"):
        source = real / name
        if source.is_file():
            (folder / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    _status(nerv)

    win = MainWindow(ConfigManager(cfg_file))
    theme.apply_theme(app, MODE)
    win.resize(1440, 980)
    win.show()
    _inline_task_thread(_MP())
    for _ in range(6):
        app.processEvents()

    win.launcher.publish_motion = lambda entries, timeout=6.0, label="": (
        SENT.append((label, list(entries))) or {entry[0]: 0 for entry in entries})
    win.launcher.run_motion_streams = lambda jobs, on_line=None, is_aborted=None, \
        timeout=None: (SENT.append(("stream", list(jobs)))
                       or {core_motion.stream_job_key(job): True for job in jobs})

    win.on_posture()
    dialog = win._posture_dialog
    for _ in range(4):
        app.processEvents()

    snap = None

    def shot(widget):
        widget.adjustSize()
        for _ in range(4):
            app.processEvents()
        return widget.grab()

    print("页面尺寸: %d x %d（主窗口 %d x %d）" % (
        dialog.size().width(), dialog.size().height(), win.size().width(), win.size().height()))
    print("轴行数:", len(dialog.rows), "| 躯干预设:", list(dialog._preset_cards["trunk"]))
    print("手臂预设（默认）:", list(dialog._preset_cards["arm"]))
    # 量版面（别靠估）：预设卡右边缘的「编辑」曾经被列表滚动条那 10px 挤出可视区。
    card = dialog._preset_cards["trunk"]["StandUp"]
    viewport = dialog.preset_areas["trunk"][0].viewport().size().width()
    print("预设卡: %d x %d（最小宽 %d）| 列表 viewport %d | 「编辑」x=%d 右边缘=%d"
          % (card.size().width(), card.size().height(), card.minimumSizeHint().width(),
             viewport, card.btn_edit.x(), card.btn_edit.x() + card.btn_edit.width()))
    print("轴列宽:", [dialog.axis_areas[group].widget().size().width()
                     for group in ("trunk", "left", "right")])
    print("「编辑」文字宽 %d / 按钮宽 %d（裁字就是这里失守）"
          % (card.btn_edit.fontMetrics().width("编辑"), card.btn_edit.width()))

    # ① 初始态：目标=当前（"待运动 0 轴"），模块使能条在卡头下面
    state_one = shot(dialog)

    # ② 改两个轴的目标值（与当前不同的轴转强调色；目标框只读、双击才可编辑）
    dialog.row_of["L4"].set_target(45.0)
    dialog.row_of["H2"].set_target(-60.0)
    dialog.on_select_row("H2")
    dialog.on_row_changed("H2")
    dialog.on_row_changed("L4")
    for _ in range(3):
        app.processEvents()
    state_two = shot(dialog)

    # ③ 躯干确认框（侧视示意图，按 URDF 正运动学画）
    trunk_dialog = posture_module._MoveConfirmDialog(
        dialog, "确认运动 · 躯干 6 轴",
        ["躯干 6 轴：H1、H2、H3、H4、H5、H6",
         "运动方式：非实时模式（点位目标，控制节点内部插值）",
         "H1　0 → 0　（+0.0）",
         "H2　-42.1 → -60　（-17.9）　←",
         "H3　-18.6 → -18.6　（+0.0）",
         "H4　1.2 → 1.2　（+0.0）"],
        _trunk_diagram(dialog, [0.0, -42.1, -18.6, 1.2, 12.0, -3.4],
                       [0.0, -60.0, -18.6, 1.2, 12.0, -3.4]),
        "最大变化 17.9° —— 先确认机器人运动范围内没有遮挡、没有人。")
    trunk_shot = shot(trunk_dialog)

    # ④ 手臂确认框（正视示意图：肩→肘→腕→手）
    arm_dialog = posture_module._MoveConfirmDialog(
        dialog, "确认运动 · 左臂 7 轴",
        ["左臂 7 轴：L1、L2、L3、L4、L5、L6、L7",
         "运动方式：非实时模式（点位目标，控制节点内部插值）",
         "L1　62.2 → 62.2　（+0.0）",
         "L4　22.3 → 45　（+22.7）　←",
         "（其余轴保持当前值）"],
        _arm_diagram(dialog, [0.0, -42.1, -18.6, 1.2, 12.0, -3.4], "left",
                     [62.2, 68.3, -22.6, 22.3, 31.6, 17.3, 0.0],
                     [62.2, 68.3, -22.6, 45.0, 31.6, 17.3, 0.0]))
    arm_shot = shot(arm_dialog)

    # ⑤ 未使能态：角度显示「—」、运动类按钮全禁用
    _status(nerv, trunk=False, arms=False)
    dialog.refresh()
    for _ in range(3):
        app.processEvents()
    state_three = shot(dialog)

    # ⑥ 跑过遥操作之后：躯干模式待确认 + 一键切回非实时（v6.2.0）
    #    遥操作的上电时序会把躯干切成实时，而实时模式下单条点位目标是"又硬又急"的
    _status(nerv, trunk=True, arms=True)
    win.teleop_runs = 1
    dialog.refresh()
    for _ in range(3):
        app.processEvents()
    state_four = shot(dialog)
    win.teleop_runs = 0

    for name, shot_ in (("躯干", trunk_shot), ("手臂", arm_shot)):
        print("确认框（%s）尺寸: %d x %d" % (name, shot_.width(), shot_.height()))

    pad, label_h, gap = 16, 30, 12
    shots = [
        ("① 打开：模块使能一行两栏（每个模块一个按钮，文案随状态）+ 角度与各轴调节（轴名 │ 当前 │ 目标，两列同一套数字规则）",
         state_one),
        ("② 目标与当前不同的轴转强调色；目标框双击才可编辑（不再有滑杆与 ± 按钮）",
         state_two),
        ("③ 点「躯干 6 轴下发」→ 二次确认：侧视示意图按 URDF 正运动学画（虚线=当前，实线=目标）",
         trunk_shot),
        ("④ 点手臂 → 确认框给正视示意图：肩→肘→腕→手，两段折线", arm_shot),
        ("⑤ 未使能：角度显示「—」不显示 0；运动类按钮全禁用并写明原因", state_three),
        ("⑥ 跑过遥操作之后：躯干模式待确认（遥操作把躯干切成了实时）+ 一键切回非实时",
         state_four),
    ]

    width = max(item[1].width() for item in shots) + pad * 2
    height = sum(label_h + item[1].height() + gap for item in shots) + pad
    out = QImage(width, height, QImage.Format_ARGB32)
    out.fill(QColor(theme.palette(MODE)["bg"]))
    painter = QPainter(out)
    painter.setPen(QColor(theme.palette(MODE)["text"]))
    y = pad
    for title, item in shots:
        painter.drawText(pad, y + 16, title)
        y += label_h
        painter.drawPixmap(pad, y, item)
        y += item.height() + gap
    painter.end()

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "姿势控制页面_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())
    print("渲染期间下发调用:", len(SENT), "（应为 0 —— 只截图不发指令、不起实时流）")
    dialog.close()
    win.close()


def _trunk_diagram(parent, current, target):
    diagram = posture_module.TrunkDiagram(parent)
    # 手臂只作陪衬（现场那把渲染里手臂就在躯干两侧），不参与这条指令
    arms = {side: [62.238, 68.3, -22.6, 22.3, 31.6, 17.3, 0.0]
            for side in ("left", "right")}
    diagram.set_values(current, target, arms=arms)
    return diagram


def _arm_diagram(parent, trunk_values, side, current, target):
    diagram = posture_module.ArmDiagram(parent)
    diagram.set_values(trunk_values, {side: target}, currents={side: current})
    return diagram


if __name__ == "__main__":
    main()
