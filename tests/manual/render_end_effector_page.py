#!/usr/bin/env python3
"""渲染「末端参数」页面在几种关键状态下的样子（离屏），用作交付/评审证据。

    cd <项目根>
    PROBE_SHOT_DIR=/home/nerv/WorkBuddy/<会话目录> python3 tests/manual/render_end_effector_page.py

**全程在临时工作空间里操作**：真的会读写一份 `arm_control_config.yaml`，但那是从样本
复制出来的副本，**不会碰机器人侧的真实配置**。四种情形：

① 文件里是灵巧手（12 格：角度 6 + 力控 6）
② 在下拉里换成宇树夹爪（3 格）+ 有未保存修改
③ 保存成功：结果区写清备份位置、改了哪些键、要重启主程序
④ 配置不可读（文件缺失）：输入与保存全禁用，并写明原因
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtWidgets import QApplication, QMessageBox                # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                      # noqa: E402

# 「换末端类型」的二次确认要换成"直接点是"：离屏环境里真弹模态框会一直等下去。
# 这是截图脚本，改的又是临时工作空间里的**副本**，不会碰机器人侧的真实配置。
_YES = QMessageBox.Yes
QMessageBox.question = staticmethod(lambda *args, **kwargs: _YES)

from tests.test_ui_wiring import _app_config, ARM_CONFIG_SAMPLE       # noqa: E402
from data_collector.core import ConfigManager, save_yaml              # noqa: E402
from data_collector import theme                                      # noqa: E402
from data_collector.ui import MainWindow                              # noqa: E402
from data_collector.ui import end_effector as ee_module               # noqa: E402

MODE = "light"


class _InlineSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _InlineThread:
    """跑完就地回调：截图脚本要在同一拍上看到"保存完成"的结果区。"""

    def __init__(self, task):
        self.task = task
        self.finished = _InlineSignal()

    def start(self):
        try:
            payload = self.task()
        except Exception as exc:                              # noqa: BLE001
            self.finished.emit(False, None, str(exc))
        else:
            self.finished.emit(True, payload, "")


def _seed(workspace: Path) -> Path:
    path = workspace / "install" / "config" / "arm_control_config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ARM_CONFIG_SAMPLE, encoding="utf-8")
    return path


def main() -> None:
    app = QApplication([])
    theme.apply_theme(app, MODE)

    tmp = Path(tempfile.mkdtemp(prefix="render_end_effector_", dir="/home/nerv"))
    cfg = tmp / "app_config.yaml"
    save_yaml(cfg, _app_config(tmp, nerv_dir=str(tmp / "nerv")))
    save_yaml(tmp / "teleop.yaml", {"teleop": {"calibration": "default"}, "calibrations": {}})
    config_path = _seed(tmp)

    win = MainWindow(ConfigManager(cfg))
    ee_module.LocalTaskThread = _InlineThread
    shots = []

    def snap(dialog):
        """截图前先按内容定尺寸。

        `grab()` 用的是控件**当前**尺寸，而窗口从没被真正 show 过 —— 尺寸还是构造时
        那一次算出来的（那时配置还没读，卡片是空的），拍出来会多出大片空白。
        """
        dialog.adjustSize()
        for _ in range(4):
            app.processEvents()
        return dialog.grab()

    def open_page():
        win._end_effector_dialog = None
        win.on_end_effector()
        return win._end_effector_dialog

    # ① 文件里是灵巧手
    dialog = open_page()
    for _ in range(4):
        app.processEvents()
    print("① 灵巧手：可见格子 %d，力控上限 %d，保存可用 %s" % (
        sum(spin.isVisible() for spin in dialog.sides["left"]["spins"]["open"]),
        dialog.sides["left"]["spins"]["open"][8].maximum(), dialog.btn_save.isEnabled()))
    shots.append(("① 文件里是灵巧手 —— 一个类型下拉（左右共用）+ 两臂各 12 格"
                  "（角度 6 + 力控 6）；右上角是机型末端（Product.json）的对照", snap(dialog)))

    # ② 换成宇树夹爪（有未保存修改）
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData("yushu"))
    for _ in range(4):
        app.processEvents()
    print("② 宇树：可见格子 %d，dirty=%s" % (
        sum(spin.isVisible() for spin in dialog.sides["left"]["spins"]["open"]),
        dialog.dirty_badge.text()))
    shots.append(("② 换成宇树夹爪 —— 两臂同时变成 3 格（速度 · 位置 · 力矩）；"
                  "下拉里另两种置灰不可选；顶部标出「有未保存修改」", snap(dialog)))

    # ③ 保存成功：真的写这份副本，结果区是真实输出
    dialog.result.clear()
    dialog.on_save()
    for _ in range(4):
        app.processEvents()
    print("③ 保存后的结果区：")
    for line in dialog.result.toPlainText().splitlines():
        print("   " + line)
    print("   文件已写入 left_open_cmd: [ 800, 0, 500 ] 出现次数 =",
          config_path.read_text(encoding="utf-8").count("left_open_cmd: [ 800, 0, 500 ]"))
    shots.append(("③ 保存成功 —— 先备份 → 按行精准改 → 写后回读校验，并提醒重启主程序生效",
                  snap(dialog)))

    # ④ 配置不可读
    config_path.unlink()
    dialog.on_reload()
    for _ in range(4):
        app.processEvents()
    print("④ 不可读：保存可用 %s，提示 %s" % (
        dialog.btn_save.isEnabled(), dialog.path_label.text()[:40]))
    shots.append(("④ 配置不可读 —— 输入与保存全部禁用，类型那行写明「配置文件不存在：…」",
                  snap(dialog)))

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

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "末端参数页面_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())
    win.close()


if __name__ == "__main__":
    main()
