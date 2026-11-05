"""「配置 ROS 域」对话框（v6.9.13）。

## 它为什么存在

机器人侧 v1.4.0-develop 起，域由 `configure_domain.sh` 生成
`install/setup_domain.bash` + `install/config/fastdds.xml` 决定（见 ADR-002）。
现场每次换域都要开终端、敲 `./configure_domain.sh domain=… local_ip=…` ——
而这个界面就在机器人跟前，让人去开终端是多余的。

所以这里只做一件事：**替操作员把那条命令发起去**，并把脚本的输出原样贴回来。
**写文件、定格式仍然是机器人脚本的事**（我们照格式自己写一份，它一改格式就静默错位）。

## 三个必须的防线

1. **二次确认**：`no_domain` 会**删掉** `setup_domain.bash`，配错域现场可能连不上机器人；
2. **越界拦住**：domain 只允许 0~255（`QIntValidator` + 应用前再验一次，程序化塞值也拦得住）；
3. **结果照实说**：成功/失败 + 脚本原始输出都显示在对话框里，并且**写明要重启上位机**
   —— 子进程继承的是启动那一刻的环境，不重启会以为"改了没用"。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QVBoxLayout)

from .. import theme
from .common import SP, fit_dialog_to_screen

#: 触摸可点目标的最小短边（与 actions.py 的 TOUCH_BOX_HEIGHT 同一口径）。
TOUCH_H = 32


class DomainDialog(QDialog):
    """填 domain + local_ip，调机器人侧脚本；也能移除域。"""

    MIN_WIDTH = 460

    def __init__(self, config, launcher, parent=None):
        super().__init__(parent)
        self.config = config
        self.launcher = launcher
        #: 有没有真的改过域（主窗口据此往日志区记一条，重启前不会忘）
        self.changed = False
        self.setWindowTitle("配置 ROS 域")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(theme.px(16),) * 4)
        layout.setSpacing(SP["sp3"])

        layout.addWidget(self._paragraph(
            "会调用机器人侧的 configure_domain.sh，写入 install/setup_domain.bash 与 "
            "install/config/fastdds.xml（后者含 local_ip）。"
            "配置后需要重启上位机才生效。"))

        layout.addWidget(self._label("domain"))
        self.domain_edit = QLineEdit()
        self.domain_edit.setMaxLength(3)
        self.domain_edit.setPlaceholderText("0-255")
        self.domain_edit.setValidator(QIntValidator(0, 255, self))
        self.domain_edit.setMinimumHeight(theme.px(TOUCH_H))
        layout.addWidget(self.domain_edit)

        layout.addWidget(self._label("local_ip"))
        self.ip_edit = QLineEdit()
        self.ip_edit.setText(self._default_ip())
        self.ip_edit.setMinimumHeight(theme.px(TOUCH_H))
        self.ip_edit.setToolTip("这台机器自己的监听地址。换网段时可改；"
                                "配置项 domain_local_ip 是它的默认值")
        layout.addWidget(self.ip_edit)

        self.result = QLabel()
        self.result.setObjectName("Hint")
        self.result.setWordWrap(True)
        self.result.hide()
        layout.addWidget(self.result)

        buttons = QHBoxLayout()
        self.btn_remove = QPushButton("移除域")
        self.btn_remove.setProperty("variant", "ghost")
        self.btn_remove.setMinimumHeight(theme.px(TOUCH_H))
        self.btn_remove.setToolTip("删除 install/setup_domain.bash，所有 ROS 进程回到默认域")
        self.btn_remove.clicked.connect(self.on_remove)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch()
        cancel = QPushButton("关闭")
        cancel.setProperty("variant", "ghost")
        cancel.setMinimumHeight(theme.px(TOUCH_H))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.btn_apply = QPushButton("配置")
        self.btn_apply.setProperty("variant", "primary")
        self.btn_apply.setDefault(True)
        self.btn_apply.setMinimumHeight(theme.px(TOUCH_H))
        self.btn_apply.clicked.connect(self.on_apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)

        self.setMinimumWidth(theme.px(self.MIN_WIDTH))
        fit_dialog_to_screen(self)

    # ---------- 小工具 ----------

    def _default_ip(self) -> str:
        try:
            return self.config.get_domain_local_ip()
        except Exception:                            # noqa: BLE001 (配置缺失不该炸界面)
            return ""

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(theme.mono_font(theme.TYPE_SCALE["fs_micro"], letter_spacing=0.6))
        return label

    @staticmethod
    def _paragraph(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        return label

    def _show_result(self, ok: bool, text: str) -> None:
        if ok:
            self.changed = True
        prefix = "✅ " if ok else "❌ "
        body = text.strip() or ("已写入" if ok else "没有输出")
        tail = "\n\n重启上位机后生效。" if ok else ""
        self.result.setText(prefix + body + tail)
        self.result.setVisible(True)

    # ---------- 动作 ----------

    def on_apply(self):
        raw = self.domain_edit.text().strip()
        if not raw.isdigit():
            self._show_result(False, "domain 只能填数字（0~255）。留空不是「移除」，移除请点左下角的按钮。")
            return
        domain = int(raw)
        if not 0 <= domain <= 255:
            self._show_result(False, f"domain 必须在 0~255 之间（填的是 {domain}）。")
            return
        ip = self.ip_edit.text().strip()

        # 二次确认：这是**写操作**，会改掉机器人侧两个文件。
        answer = QMessageBox.question(
            self, "确认配置域",
            f"将执行：\n    ./configure_domain.sh domain={domain}"
            f"{(' local_ip=' + ip) if ip else ''}\n\n"
            "会覆盖 install/setup_domain.bash 与 install/config/fastdds.xml。\n"
            "配置后需要重启上位机才生效。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        ok, text = self.launcher.configure_domain(domain, ip)
        self._show_result(ok, text)

    def on_remove(self):
        # 删文件的动作：措辞要点明后果，不写「是否确认」这种含糊问法。
        answer = QMessageBox.warning(
            self, "确认移除域",
            "将执行：\n    ./configure_domain.sh no_domain\n\n"
            "这会删除 install/setup_domain.bash：\n"
            "所有 ROS 进程（主程序、录制、遥操作、验证、监控）都会回到默认域。\n"
            "重启上位机后生效。\n\n确定移除吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        ok, text = self.launcher.configure_domain(None, "")
        self._show_result(ok, text)

    def keyPressEvent(self, event):  # noqa: N802 (Qt 命名)
        """回车只作用于「配置」，不给「移除域」任何快捷键。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.on_apply()
            return
        super().keyPressEvent(event)
