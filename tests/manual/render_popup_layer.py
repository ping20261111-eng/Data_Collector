#!/usr/bin/env python3
"""渲染"弹出层"（机器人控制菜单 / 下拉列表）在明暗两套主题下的样子，作交付与评审证据。

    cd <项目根>
    PROBE_SHOT_DIR=/home/nerv/WorkBuddy/<会话目录> python3 tests/manual/render_popup_layer.py

同时打印量出来的几何（圆角是否真透明、行高、选中块的内缩），因为"圆角"这件事
离屏看起来一样、现场可能不一样：凡是"看着对不对"的判断都量像素，不靠眼。

只构造窗口并截图，不下发任何指令、不跑任何脚本。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtCore import Qt                                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox                # noqa: E402
from PyQt5.QtGui import QImage, QPainter, QColor                     # noqa: E402

from tests.test_ui_wiring import (_app_config, _inline_task_thread,  # noqa: E402
                                  _seed_arm_config)

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

from data_collector.core import ConfigManager, save_yaml             # noqa: E402
from data_collector import theme                                     # noqa: E402
from data_collector.ui import MainWindow                             # noqa: E402


class _MP:
    """极简 monkeypatch 替身：只用来换 LocalTaskThread。"""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _corners(image):
    return [image.pixelColor(x, y).alpha() for x, y in
            ((1, 1), (image.width() - 2, 1),
             (1, image.height() - 2), (image.width() - 2, image.height() - 2))]


def main() -> None:
    app = QApplication([])
    shots = []
    for mode in ("light", "dark"):
        theme.apply_theme(app, mode)
        palette = theme.palette(mode)

        tmp = Path(tempfile.mkdtemp(prefix="render_popup_", dir="/home/nerv"))
        cfg = tmp / "app_config.yaml"
        save_yaml(cfg, _app_config(tmp, nerv_dir=str(tmp / "nerv")))
        _seed_arm_config(tmp)

        win = MainWindow(ConfigManager(cfg))
        # **在窗口构造之后再 apply 一次**：`MainWindow.__init__` 会按配置里的主题再 apply 一遍，
        # 把脚本开头设的 mode 覆盖掉（实测：暗色那一屏拍出来是亮色）
        theme.apply_theme(app, mode)
        win.resize(1440, 900)
        win.show()
        _inline_task_thread(_MP())
        for _ in range(6):
            app.processEvents()

        # ---- ① 下拉列表（先量它：菜单会占着 popup 焦点，两个弹层叠着量会互相干扰）----
        win._end_effector_dialog = None
        win.on_end_effector()
        dialog = win._end_effector_dialog
        dialog.adjustSize()
        for _ in range(4):
            app.processEvents()
        combo = dialog.type_combo
        combo.showPopup()
        for _ in range(8):
            app.processEvents()
        view = combo.view()
        popup = view.window()
        combo_shot = popup.grab()
        rows = [view.sizeHintForRow(i) for i in range(view.model().rowCount())]
        print("下拉弹层：%d×%d ｜ 四角 alpha %s ｜ 行高 %s ｜ spacing %d ｜ 视图 %d×%d" % (
            combo_shot.width(), combo_shot.height(), _corners(combo_shot.toImage()),
            rows, view.spacing(), view.width(), view.height()))
        combo.hidePopup()
        for _ in range(3):
            app.processEvents()

        # ---- ② 菜单：选中一项再抓，量选中块的几何 ----
        menu = win.btn_robot_menu.menu()
        actions = [a for a in menu.actions() if a.text()]
        menu.setActiveAction(actions[1])
        menu.popup(win.mapToGlobal(win.rect().center()))
        for _ in range(8):
            app.processEvents()
        menu_image = menu.grab().toImage()
        soft = palette["accent_soft"].lower()
        rows_y = [y for y in range(menu_image.height())
                  if menu_image.pixelColor(menu_image.width() // 2, y).name().lower() == soft]
        if rows_y:
            row_y = rows_y[0] + 4
            xs = [x for x in range(menu_image.width())
                  if menu_image.pixelColor(x, row_y).name().lower() == soft]
            print("菜单：%d×%d ｜ 四角 alpha %s ｜ 选中块 y %d→%d（高 %d）｜ x %d→%d（弹层宽 %d）" % (
                menu_image.width(), menu_image.height(), _corners(menu_image),
                min(rows_y), max(rows_y), max(rows_y) - min(rows_y) + 1,
                min(xs), max(xs), menu_image.width()))
        menu.hide()
        for _ in range(3):
            app.processEvents()

        shots.append((f"{mode}｜「机器人控制 ▾」菜单 —— 白卡片（暗色为 elevated 一档）、圆角 12、"
                      f"选中块内缩 2px、分隔线两侧内缩 12px", menu.grab()))
        shots.append((f"{mode}｜下拉列表 —— 与菜单同一张卡片：同底色 / 同描边 / 同圆角 / 同行高",
                      combo_shot))
        win.close()
        for _ in range(2):
            app.processEvents()

    pad, label_h, gap = 16, 30, 12
    width = max(shot.width() for _t, shot in shots) + pad * 2
    height = sum(label_h + shot.height() + gap for _t, shot in shots) + pad
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

    target = Path(os.environ.get("PROBE_SHOT_DIR", ".")) / "弹出层_实拍.png"
    out.save(str(target))
    print("已渲染", target, out.width(), "x", out.height())


if __name__ == "__main__":
    main()
