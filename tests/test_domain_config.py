"""「配置域…」：调机器人侧 configure_domain.sh 的守卫（v6.9.13）。

这一层的契约（见 ADR-002）：

1. 上位机**只调脚本**，不自己写 setup_domain.bash / fastdds.xml，也不 export ROS_DOMAIN_ID；
2. `local_ip` 的默认值来自配置（`domain_local_ip`，默认 192.168.2.100），可在配置文件改；
3. 越界/非数字**先拦住**，不把坏参数交给脚本；
4. 脚本不存在时给出**带路径**的失败信息，而不是让界面崩。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_collector.core import ConfigManager, save_yaml          # noqa: E402
from data_collector.core_operations import Launcher               # noqa: E402


def _config(tmp_path, workspace=None, **extra):
    ws = workspace or (tmp_path / "ws")
    ws.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "app_config.yaml"
    data = {"workspace_root": str(ws), "teleop_config_path": "t.yaml"}
    data.update(extra)
    save_yaml(cfg, data)
    return ConfigManager(cfg), ws


def _fake_script(workspace: Path, body: str = "") -> Path:
    script = workspace / "configure_domain.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"{body}\n"
        'printf "cwd=%s\\n" "$(pwd)" >> "$(dirname "$0")/calls.txt"\n'
        'for a in "$@"; do printf "arg=%s\\n" "$a" >> "$(dirname "$0")/calls.txt"; done\n'
        'echo "domain script done"\n',
        encoding="utf-8")
    script.chmod(0o755)
    return script


def test_script_path_and_local_ip_have_documented_defaults(tmp_path):
    """脚本默认在工作空间根；local_ip 默认 192.168.2.100（现场 2026-11-05 确认）。"""
    cfg, ws = _config(tmp_path)

    assert cfg.get_configure_domain_script() == ws / "configure_domain.sh"
    assert cfg.get_domain_local_ip() == "192.168.2.100"


def test_config_can_override_the_script_and_the_ip(tmp_path):
    """两项都能在配置文件里改：换脚本位置、换网段（local_ip 是**本机**地址）。"""
    cfg, ws = _config(tmp_path, configure_domain_script="tools/set_domain.sh",
                      domain_local_ip="10.0.0.5")

    assert cfg.get_configure_domain_script() == ws / "tools" / "set_domain.sh"
    assert cfg.get_domain_local_ip() == "10.0.0.5"


def test_configure_domain_passes_domain_and_local_ip(tmp_path):
    """配置时按 `domain=N local_ip=IP` 传，并且**在工作空间根执行**（脚本的相对输出路径依赖它）。"""
    cfg, ws = _config(tmp_path)
    _fake_script(ws)
    ok, text = Launcher(cfg).configure_domain(10, "192.168.2.100")

    assert ok is True, text
    assert "domain script done" in text, "脚本输出要原样回给界面"
    calls = (ws / "calls.txt").read_text(encoding="utf-8")
    assert f"cwd={ws}" in calls, calls
    assert "arg=domain=10" in calls, calls
    assert "arg=local_ip=192.168.2.100" in calls, calls


def test_removing_domain_passes_no_domain(tmp_path):
    """`None` = 移除域，参数是 `no_domain`（真正删文件的是脚本，不是我们）。"""
    cfg, ws = _config(tmp_path)
    _fake_script(ws)

    ok, _text = Launcher(cfg).configure_domain(None, "")

    assert ok is True
    calls = (ws / "calls.txt").read_text(encoding="utf-8")
    assert "arg=no_domain" in calls, calls
    assert "arg=domain=" not in calls, calls


def test_empty_local_ip_is_not_passed(tmp_path):
    """local_ip 留空就不传 —— 让脚本用它自己的默认值，别塞一个空 `local_ip=` 进去。"""
    cfg, ws = _config(tmp_path)
    _fake_script(ws)

    Launcher(cfg).configure_domain(7, "")

    calls = (ws / "calls.txt").read_text(encoding="utf-8")
    assert "arg=domain=7" in calls, calls
    assert "arg=local_ip=" not in calls, calls   # 别只看子串：临时目录名里就带 local_ip


def test_missing_script_fails_with_the_path_in_the_message(tmp_path):
    """脚本不在时要**说清找的是哪个路径** —— 现场十次有九次是 workspace 配错了。"""
    cfg, ws = _config(tmp_path)

    ok, text = Launcher(cfg).configure_domain(5, "192.168.2.100")

    assert ok is False
    assert str(ws / "configure_domain.sh") in text, text
    assert "configure_domain_script" in text, "提示里要带上配置项名，便于现场自己改"


def test_out_of_range_domain_is_rejected_before_running(tmp_path):
    """999 这种值不交给脚本：脚本可能不校验，写进 xml 就是一个坏 profile。"""
    cfg, ws = _config(tmp_path)
    _fake_script(ws)

    ok, text = Launcher(cfg).configure_domain(999, "192.168.2.100")

    assert ok is False and "0~255" in text, text
    assert not (ws / "calls.txt").exists(), "越界值不该真的执行脚本"


def test_a_failing_script_is_reported_with_its_output(tmp_path):
    """脚本非零退出 → `(False, 输出)`，界面照实贴出来（不要吞成「失败了」三个字）。"""
    cfg, ws = _config(tmp_path)
    script = ws / "configure_domain.sh"
    script.write_text("#!/bin/bash\necho 'ip 不在本机网卡上' >&2\nexit 3\n", encoding="utf-8")

    ok, text = Launcher(cfg).configure_domain(10, "192.168.2.100")

    assert ok is False
    assert "ip 不在本机网卡上" in text, text


# ---------- 对话框（界面层） ----------

@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    return app


class _LauncherStub:
    """只记「被调了什么」，不真的跑脚本。"""

    def __init__(self, ok=True, text="ok"):
        self.calls = []
        self.ok = ok
        self.text = text

    def configure_domain(self, domain, local_ip=""):
        self.calls.append((domain, local_ip))
        return self.ok, self.text


def _dialog(tmp_path, qapp, launcher=None, **extra):
    from data_collector.ui.domain_dialog import DomainDialog

    cfg, _ws = _config(tmp_path, **extra)
    return DomainDialog(cfg, launcher or _LauncherStub())


def test_dialog_prefills_local_ip_from_the_config(tmp_path, qapp):
    """输入框的默认 IP 就是配置里那个（`domain_local_ip`）—— 否则现场每次都要手打。"""
    dlg = _dialog(tmp_path, qapp, domain_local_ip="10.0.0.9")
    try:
        assert dlg.ip_edit.text() == "10.0.0.9"
    finally:
        dlg.close()


def test_dialog_declined_confirmation_does_not_touch_the_robot(tmp_path, qapp, monkeypatch):
    """二次确认点「否」→ **一条命令都不发**（这是写操作，不能点错就改了现场配置）。"""
    from PyQt5.QtWidgets import QMessageBox

    launcher = _LauncherStub()
    dlg = _dialog(tmp_path, qapp, launcher=launcher)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    try:
        dlg.domain_edit.setText("10")
        dlg.on_apply()
        assert launcher.calls == [], launcher.calls
        assert dlg.changed is False
    finally:
        dlg.close()


def test_dialog_confirmed_applies_domain_and_ip(tmp_path, qapp, monkeypatch):
    """确认后按 `(domain, local_ip)` 调脚本，并标记"改过了"（主窗口据此记一条日志）。"""
    from PyQt5.QtWidgets import QMessageBox

    launcher = _LauncherStub(text="写好了")
    dlg = _dialog(tmp_path, qapp, launcher=launcher)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    try:
        dlg.domain_edit.setText("10")
        dlg.ip_edit.setText("192.168.2.101")
        dlg.on_apply()

        assert launcher.calls == [(10, "192.168.2.101")], launcher.calls
        assert dlg.changed is True
        assert "写好了" in dlg.result.text()
        assert "重启上位机" in dlg.result.text(), "成功提示必须写明要重启，否则现场会以为没生效"
    finally:
        dlg.close()


def test_dialog_refuses_bad_domain_without_running_anything(tmp_path, qapp):
    """非数字 / 空 / 越界 → 就地提示，不弹确认、不调脚本。"""
    launcher = _LauncherStub()
    dlg = _dialog(tmp_path, qapp, launcher=launcher)
    try:
        for bad in ("", "abc", "999"):
            dlg.domain_edit.setText(bad)
            dlg.on_apply()
            assert launcher.calls == [], f"「{bad}」居然调了脚本：{launcher.calls}"
            assert dlg.result.isVisible() or dlg.result.text(), f"「{bad}」没有给出提示"
    finally:
        dlg.close()


def test_dialog_remove_needs_confirmation(tmp_path, qapp, monkeypatch):
    """`no_domain` 会删文件 —— 必须先确认；点「否」就什么都不做。"""
    from PyQt5.QtWidgets import QMessageBox

    launcher = _LauncherStub(text="已移除")
    dlg = _dialog(tmp_path, qapp, launcher=launcher)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.No))
    try:
        dlg.on_remove()
        assert launcher.calls == [], launcher.calls
    finally:
        dlg.close()

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dlg = _dialog(tmp_path, qapp, launcher=launcher)
    try:
        dlg.on_remove()
        assert launcher.calls == [(None, "")], launcher.calls
        assert dlg.changed is True
    finally:
        dlg.close()
