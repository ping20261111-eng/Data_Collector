"""结构化错误码的边界测试。

这个文件重点钉住一条设计边界：**上位机侧的错误才有码，机器人侧的错误不枚举**。
理由见 errors.py 的模块 docstring —— 机器人侧的失败种类由脚本与固件决定，上位机
枚举必然不全，而"没有码"会被读成"没有错误"。
"""
import pytest

from data_collector.errors import (AppError, ErrorCode, Owner, describe,
                                   is_retryable, owner_label, spec_of)


def test_every_code_has_a_spec():
    """每个码都必须给出归属、可否重试、可执行建议 —— 缺一项就等于码没用。"""
    missing = [c for c in ErrorCode if spec_of(c) is None]

    assert not missing, f"这些错误码没有定义 spec: {missing}"


def test_specs_are_complete():
    for code in ErrorCode:
        spec = spec_of(code)
        assert isinstance(spec.owner, Owner), code
        assert isinstance(spec.retryable, bool), code
        assert spec.hint.strip(), f"{code} 的建议为空，用户看了等于没看"


def test_retryable_matches_spec_and_defaults_to_true_without_code():
    # 无码时保守地认为值得一试：宁可多试一次，也不要把可恢复的错误直接判死
    assert is_retryable(None) is True

    assert is_retryable(ErrorCode.AUTH) is False
    assert is_retryable(ErrorCode.REMOTE_SPACE) is False
    assert is_retryable(ErrorCode.LOCAL_SPACE) is False
    assert is_retryable(ErrorCode.VERIFY_FAILED) is True
    assert is_retryable(ErrorCode.INTERRUPTED) is True


def test_describe_keeps_original_message():
    """原始信息必须原样保留 —— 翻译丢细节是 _ensure_remote_dir 那个坑的教训。"""
    text = describe(ErrorCode.AUTH, "远端认证失败：Authentication failed.")

    assert text.startswith("远端认证失败：Authentication failed.")
    assert "归属：" in text
    assert "建议：" in text


def test_describe_without_code_returns_message_unchanged():
    """没有码时不硬凑归属，避免给出错误指引。"""
    assert describe(None, "远端写入失败（退出码 1）") == "远端写入失败（退出码 1）"
    assert describe(None, "") == ""


def test_script_failure_is_attributed_to_robot_side():
    """脚本秒退属于机器人侧 —— 这条边界正是"错误码该盖哪一侧"的答案。"""
    assert spec_of(ErrorCode.SCRIPT_FAILED).owner is Owner.ROBOT
    assert owner_label(ErrorCode.SCRIPT_FAILED) == "机器人侧"
    assert "机器人侧" in describe(ErrorCode.SCRIPT_FAILED, "启动后退出")


def test_cancelled_is_user_side_and_not_an_error_to_retry():
    assert spec_of(ErrorCode.CANCELLED).owner is Owner.USER
    assert is_retryable(ErrorCode.CANCELLED) is False


def test_apperror_carries_code_and_message():
    err = AppError(ErrorCode.LOCAL_SPACE, "本地临时目录空间不足")

    assert err.code is ErrorCode.LOCAL_SPACE
    assert err.message == "本地临时目录空间不足"
    assert str(err) == "本地临时目录空间不足"

    with pytest.raises(AppError) as caught:
        raise AppError(ErrorCode.PATH_REJECTED, "路径越界")
    assert caught.value.code is ErrorCode.PATH_REJECTED
