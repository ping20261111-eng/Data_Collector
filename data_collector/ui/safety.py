"""界面回调的兜底护栏：任何周期性/信号回调抛异常，都不允许把整个界面带走。

## 为什么必须有这一层

PyQt5 的槽函数里抛出**未捕获**异常时，会走 `qFatal()` 直接 abort 整个进程 ——
不是"打印一行继续跑"，而是当场退出。表现就是现场看到的"主页直接崩掉，什么提示都没有"。
而界面里的异常来源大多是我们控制不了的外部输入：机器人状态文件（可能被外部脚本
写到一半）、version.txt、Product.json、/proc 里正在消失的进程、配置文件……
一次半截 JSON 或一个类型不符的字段，就足以让采集现场的工具机退出。

所以这里做两件事：

1. `guard_slot` —— 包住定时器与信号回调：异常 → 写日志（带堆栈）+ 界面提示一次，
   界面继续可用；
2. `install_excepthook` —— 兜住非槽路径（主线程其它地方）的异常，至少留下堆栈，
   不再"静默退出"。

## 复现时怎么定因

`logs/ui_error.log` 里会追加完整的类型/消息/堆栈与发生时刻。现场再遇到崩溃时，
把这份文件发回来就能直接定位到具体那一行，不用凭现象猜。
"""
import inspect
import sys
import time
import traceback
from functools import wraps
from pathlib import Path

from ..core import logger, PROJECT_ROOT

# 弹窗只弹**一次**（整个进程运行期间）：定时器槽里弹模态框会阻塞界面，
# 而这类错误往往每秒都会重犯（比如状态文件一直是坏的）—— 那不叫提示，叫折腾。
# 日志区是主要出口：现场本来就一直盯着它（这是本项目日志回显设计的初衷）。
_notified_once = {"done": False}

_CRASH_LOG = PROJECT_ROOT / "logs" / "ui_error.log"


def write_crash_log(exc_type, exc, tb) -> str:
    """把一次异常追加到 logs/ui_error.log，返回落盘路径（失败返回空串）。

    刻意不用 logging：那条链路可能本身正是因为异常而不可用（磁盘满、目录只读），
    这里只做"尽力留证"，失败也不抛。
    """
    try:
        _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        with open(_CRASH_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{detail}")
        return str(_CRASH_LOG)
    except Exception:                                  # noqa: BLE001
        return ""


def _notify(owner, func_name: str, detail: str) -> None:
    """首次出错时弹一次提示（之后只写日志）。任何失败都咽掉：提示不该再引发崩溃。"""
    if _notified_once["done"]:
        return
    _notified_once["done"] = True
    try:
        from PyQt5.QtWidgets import QMessageBox
        box = QMessageBox(owner)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("界面遇到一个问题（已记录，可继续使用）")
        box.setText(f"处理「{func_name}」时出错，本次刷新已跳过：{detail}")
        box.setInformativeText(
            f"完整堆栈已写入：{_CRASH_LOG}\n"
            "界面不会因此退出；同一问题后续只记日志，不再弹窗。")
        box.exec_()
    except Exception:                                  # noqa: BLE001
        pass


def guard_slot(func):
    """包住槽函数：异常只记录，不让它冒到 Qt（冒上去就是 abort 进程）。

    只用于**被 Qt 调用的**回调（定时器 timeout、线程信号、按钮 clicked）。
    纯逻辑函数不要套这层——那会把真正的编程错误也吞掉。

    **必须按被包函数的形参个数裁剪多余实参**：PyQt 连接槽时会看可调用对象的
    签名来决定传几个参数（`clicked` 带 `checked=False`）。套上装饰器后 pyqt 看到
    的是 `(*args, **kwargs)`，于是把 `checked` 也传进来 —— 没写这个裁剪的话，
    所有"无参按钮槽"都会 `TypeError: takes 1 positional argument but 2 were given`
    （实测：`on_toggle_robot_monitor` 直接挂）。这里复刻 Qt 原本的行为：能收几个就传几个。
    """
    limit = _positional_limit(func)

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if limit is not None and len(args) > limit:
            args = args[:limit]
        try:
            return func(self, *args, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            exc_type, exc_value, tb = sys.exc_info()
            logger.error(f"界面回调 {func.__name__} 异常：{exc}", exc_info=True)
            path = write_crash_log(exc_type, exc_value, tb)
            detail = f"{type(exc).__name__}: {exc}"
            append = getattr(self, "append", None)
            if callable(append):
                try:
                    append(f"界面回调 {func.__name__} 异常：{detail}"
                           + (f"（堆栈见 {path}）" if path else ""))
                except Exception:                      # noqa: BLE001
                    pass
            _notify(self, func.__name__, detail)
            return None
    return wrapper


def _positional_limit(func) -> int:
    """被包函数能接收的**额外**位置参数个数（不含 self）；收不完就返回 None。

    返回 None 表示对方带 `*args`（那就原样转发，不做裁剪）。
    """
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return 0
    if any(p.kind is p.VAR_POSITIONAL for p in parameters):
        return None
    positional = [p for p in parameters
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return max(len(positional) - 1, 0)      # 去掉 self


def append_note(text: str) -> str:
    """往 logs/ui_error.log 追加一行说明（没有堆栈的事件：信号、心跳、启动信息）。

    为什么单独一个入口：`write_crash_log` 要三件套（type/value/tb），而"收到
    SIGTERM""启动于某时刻"这类事件没有堆栈 —— 但它们恰恰是排查"界面忽然没了"
    最需要的证据。
    """
    try:
        _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_CRASH_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {text}\n")
        return str(_CRASH_LOG)
    except Exception:                                  # noqa: BLE001
        return ""


def install_excepthook() -> None:
    """主线程兜底：未捕获异常写堆栈 + 落盘，不改变"进程继续"的前提。

    注意：PyQt5 的槽异常**不走这里**（槽内部先被 guard_slot 拦住，没拦住的话
    Qt 会直接 abort）。这条主要覆盖 main 启动流程与后台线程之外的路径。
    """
    previous = sys.excepthook

    def hook(exc_type, exc_value, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, tb)
            return
        logger.error("未捕获异常", exc_info=(exc_type, exc_value, tb))
        write_crash_log(exc_type, exc_value, tb)

    sys.excepthook = hook


# ============ 外部信号：SIGTERM / SIGINT ============
# 现场现象："界面忽然没了，日志一个字都没有"，终端只留一行 `已杀死` 或直接回到提示符。
# Python 默认对 SIGTERM 是**立即终止、不清理、不打印**，所以任何 except 都拦不到 ——
# 想定位只能靠自己留证据。这里做两件事：
#   1. 收到 SIGTERM/SIGINT → 写日志区 + logs/ui_error.log（含来源推测）；
#   2. 顺手把本会话拉起的辅助节点收干净，再干净退出（不再留残留节点）。
# 注意：SIGKILL 无法捕获，那种情况只能靠"启动心跳 + journalctl/dmesg"来判断。
_signal_state = {"received": 0, "name": ""}


def install_signal_handlers(app, get_window=None, interval_ms: int = 200):
    """把 SIGTERM / SIGINT 接到"留遗言 + 收节点 + 干净退出"上。

    Args:
        app: QApplication 实例。
        get_window: 返回主窗口的 callable（用于收辅助节点）；可为 None。
        interval_ms: 轮询间隔。Qt 事件循环阻塞在 C 层时 Python 的信号处理器
            **不会被执行**，必须用一个短定时器把解释器"戳醒"，
            否则装了 handler 也等不到（这是很常见的踩坑点）。

    Returns:
        启动的 QTimer（持有引用即可，不要让它被回收）。
    """
    import signal as _signal

    from PyQt5.QtCore import QTimer

    def _handler(signum, _frame):
        _signal_state["received"] = signum
        try:
            name = _signal.Signals(signum).name
        except ValueError:
            name = f"信号 {signum}"
        _signal_state["name"] = name

        detail = (f"收到 {name}：外部要求本进程退出。"
                  "常见来源：systemctl stop <服务>、pkill、终端/会话结束、"
                  "或整组信号（killpg）。")
        logger.warning(detail)
        append_note(detail)
        append_note("（若是停止机器人服务触发的，说明本界面与该服务同属一个会话/进程组）")

    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _signal.signal(sig, _handler)
        except (ValueError, OSError):        # 非主线程或不支持：忽略，不影响主流程
            logger.debug(f"无法为 {sig} 安装处理器（忽略）")

    def _check():
        if not _signal_state["received"]:
            return
        timer.stop()
        window = None
        try:
            window = get_window() if get_window else None
        except Exception:                    # noqa: BLE001
            window = None
        # 退出兜底：先把上位机拉起的辅助节点收掉，再退出。
        # 刻意**不弹任何对话框**（此刻是被外部要求退出，没人会来点确认）。
        launcher = getattr(window, "launcher", None)
        if launcher is not None:
            try:
                for line in launcher.stop_aux_nodes():
                    logger.info(line)
            except Exception as exc:         # noqa: BLE001
                logger.warning(f"收到信号后清理辅助节点失败：{exc}")
        append_note(f"因 {_signal_state['name']} 退出（已尝试清理本会话拉起的节点）")
        try:
            app.quit()
        except Exception:                    # noqa: BLE001
            pass
        return

    timer = QTimer(app)
    timer.setInterval(interval_ms)
    timer.timeout.connect(_check)
    timer.start()
    return timer
