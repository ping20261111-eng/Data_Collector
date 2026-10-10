"""结构化错误：把「谁的问题」和「什么问题」分开表达。

## 为什么要做

界面原先把错误当成一串中文（`last_error`），并且用字符串判断控制流
（`"取消" in msg`）—— 网络错误的文案里只要出现"取消"二字，就会被误判成
用户取消，进而静默清空整个上传队列。这类判断无法靠"文案写得更小心"根除。

码化之后有两个实际收益：
1. **决定重试有没有意义** —— 凭据错、远端没有 `cat`、本地空间不足，重试一万次
   也是同样结果。现在会自动重试所有失败，白等 2 秒还给出误导性提示。
2. **界面能说清"该找谁"** —— 用户自己改配置、要找网络、还是要找机器人侧。

## 设计边界（重要，别把边界抹平）

- **上位机侧的错误用封闭枚举**：种类有限（凭据 / 网络 / 空间 / 校验 / 取消……），
  能穷尽，码化才有意义。
- **机器人侧的错误不枚举**：种类由脚本与固件决定，上位机**枚举必然不全** ——
  而"没有码"会被读成"没有错误"。所以机器人侧保留**原始证据**（脚本名 + 退出码 +
  stderr），只标注归属。
- 同理，`describe()` 一律**保留原始信息原文**再补建议。把认证失败/权限不足都
  归并成一句"目录已存在"，就是翻译丢信息的典型后果（见 `_ensure_remote_dir` 的注释）。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Owner(str, Enum):
    """该由谁处理 —— 决定界面怎么提示。"""

    USER = "user"          # 用户操作本身（取消、选的目录不对）
    CONFIG = "config"      # 上位机配置或本机环境
    NETWORK = "network"    # 网络与远端主机
    ROBOT = "robot"        # 机器人侧脚本 / 固件 / 运行环境
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorSpec:
    owner: Owner
    retryable: bool
    hint: str


# 上位机侧错误的封闭枚举。新增条目时请同时给：归属、是否值得重试、可执行建议。
SPECS = {}


def _spec(code: str, owner: Owner, retryable: bool, hint: str):
    SPECS[code] = ErrorSpec(owner, retryable, hint)


class ErrorCode(str, Enum):
    """上位机侧错误码（封闭集合）。

    机器人侧**不在这里** —— 见模块 docstring 的边界说明。
    """

    CANCELLED = "cancelled"
    AUTH = "auth"
    CONNECT = "connect"
    INTERRUPTED = "interrupted"
    VERIFY_FAILED = "verify_failed"
    REMOTE_SPACE = "remote_space"
    REMOTE_COMMAND = "remote_command"
    LOCAL_SPACE = "local_space"
    LOCAL_IO = "local_io"
    PATH_REJECTED = "path_rejected"
    CONFIG_MISSING = "config_missing"
    NOT_FOUND = "not_found"
    REPO_UNREACHABLE = "repo_unreachable"
    REPO_BAD_RESPONSE = "repo_bad_response"
    TARGET_EXISTS = "target_exists"
    SCRIPT_FAILED = "script_failed"
    UNKNOWN = "unknown"


_spec(ErrorCode.CANCELLED, Owner.USER, False,
      "无需处理")
_spec(ErrorCode.AUTH, Owner.CONFIG, False,
      "在 configs/app_config.yaml 填 remote_password，"
      "或设置环境变量 DATA_COLLECTOR_REMOTE_PASSWORD")
_spec(ErrorCode.CONNECT, Owner.NETWORK, True,
      "确认远端地址与网络可达（remote_host / remote_port）后重试")
_spec(ErrorCode.INTERRUPTED, Owner.NETWORK, True,
      "网络中断。远端半成品已保留，重试会从断点继续")
_spec(ErrorCode.VERIFY_FAILED, Owner.NETWORK, True,
      "远端落盘不完整（已自动清理），可直接重试")
_spec(ErrorCode.REMOTE_SPACE, Owner.NETWORK, False,
      "清理远端磁盘或改 remote_base 指向其他分区后重试")
_spec(ErrorCode.REMOTE_COMMAND, Owner.CONFIG, False,
      "远端不允许执行 shell 命令（没有 cat）。已自动改用「本地打包 + SFTP 上传」；"
      "若持续失败请检查远端 sshd 配置")
_spec(ErrorCode.LOCAL_SPACE, Owner.CONFIG, False,
      "清理本地临时目录（upload.temp_dir），或调小 temp_margin_mb")
_spec(ErrorCode.LOCAL_IO, Owner.CONFIG, True,
      "检查本地文件权限与磁盘状态")
_spec(ErrorCode.PATH_REJECTED, Owner.CONFIG, False,
      "目标路径超出 remote_base 允许范围，请检查远端子路径")
_spec(ErrorCode.CONFIG_MISSING, Owner.CONFIG, False,
      "检查 configs/app_config.yaml 的必填项")
_spec(ErrorCode.NOT_FOUND, Owner.USER, False,
      "重新选择数据目录")
_spec(ErrorCode.REPO_UNREACHABLE, Owner.NETWORK, True,
      "确认能访问版本服务器（app_config.yaml 的 version_repo_url）：地址、端口、"
      "以及本机是否在该内网里。本程序对版本仓库「直连、不走代理」（忽略 http_proxy "
      "等环境变量），所以这里报连不上时不用去查代理设置")
_spec(ErrorCode.REPO_BAD_RESPONSE, Owner.NETWORK, True,
      "版本服务器返回的内容里解析不出包列表：确认 version_repo_url 指向「包所在目录」"
      "（例如 …/file/wheeled_robot/），而不是它的上层")
_spec(ErrorCode.TARGET_EXISTS, Owner.CONFIG, False,
      "目标目录已存在，程序不会覆盖（那可能就是正在使用的工作空间）："
      "改用其他下载目录，或先切走/改名再下")
_spec(ErrorCode.SCRIPT_FAILED, Owner.ROBOT, False,
      "请看脚本输出与机器人侧日志（依赖未装 / 环境未 source / 权限不足）")
_spec(ErrorCode.UNKNOWN, Owner.UNKNOWN, True,
      "查看运行日志获取详情")


class AppError(Exception):
    """带错误码的异常。

    `message` 保留原始细节（排障要看），`code` 供界面决定提示与重试。
    """

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def spec_of(code: Optional[ErrorCode]) -> Optional[ErrorSpec]:
    if code is None:
        return None
    return SPECS.get(code)


def is_retryable(code: Optional[ErrorCode]) -> bool:
    """该错误重试有没有意义。无码时保守地认为值得一试。"""
    spec = spec_of(code)
    return True if spec is None else spec.retryable


def owner_label(code: Optional[ErrorCode]) -> str:
    names = {
        Owner.USER: "你自己（操作）",
        Owner.CONFIG: "上位机配置 / 本机环境",
        Owner.NETWORK: "网络或远端主机",
        Owner.ROBOT: "机器人侧",
        Owner.UNKNOWN: "未确定",
    }
    spec = spec_of(code)
    return names.get(spec.owner, "未确定") if spec else "未确定"


def describe(code: Optional[ErrorCode], message: str = "") -> str:
    """组合成界面文案：**原文保留** + 归属 + 建议。

    没有码时原样返回 —— 不硬凑归属，避免给出错误指引。
    """
    text = (message or "").strip()
    spec = spec_of(code)
    if spec is None:
        return text
    lines = [text] if text else []
    lines.append(f"归属：{owner_label(code)}")
    lines.append(f"建议：{spec.hint}")
    return "\n".join(lines)
