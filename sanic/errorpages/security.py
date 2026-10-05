"""错误表示的安全级别与公开性判定。

错误响应同时面向机器客户端与运维人员，同一条异常在不同环境下
（调试 / 生产）可公开的信息量不同。安全级别是注册表选择渲染器的
四个维度之一（异常层级 × 路由范围 × 媒体类型 × 安全级别）。
"""

from __future__ import annotations

import enum


class SecurityLevel(enum.Enum):
    """渲染错误时所处的信息披露环境。

    - ``DEBUG``：开发 / 调试环境，可以输出堆栈、``extra`` 等完整诊断
      信息，仅供本机或受控运维网络使用。
    - ``SAFE``：生产环境，只允许输出可公开的上下文（异常消息、
      ``context``），禁止泄露堆栈、内部路径与 ``extra``。
    """

    DEBUG = "debug"
    SAFE = "safe"

    @classmethod
    def from_debug(cls, debug: bool) -> "SecurityLevel":
        """根据应用是否开启调试模式选择安全级别。"""
        return cls.DEBUG if debug else cls.SAFE

    @property
    def debug(self) -> bool:
        return self is SecurityLevel.DEBUG


# 4xx 属于客户端可预期、可纠正的错误，其消息与 context 天然可以公开；
# 5xx（以及所有非 :class:`~sanic.exceptions.SanicException` 的未预期异常）
# 在生产模式下只能返回通用文案，避免泄露内部实现细节。
CLIENT_ERROR_MAX_STATUS = 499


def is_safe_status(status: int) -> bool:
    """4xx 状态码的细节被视为可公开，5xx 则不可。"""
    return status <= CLIENT_ERROR_MAX_STATUS


def security_level(debug: bool) -> SecurityLevel:
    """便捷函数：布尔 debug 标志 → :class:`SecurityLevel`。"""
    return SecurityLevel.from_debug(debug)


def safe_text(exception: Exception, fallback_text: str, debug: bool) -> str:
    """决定对端能看到的异常文案。

    * 调试模式：完整 ``str(exception)``；
    * 生产 + :class:`SanicException`（带明确 HTTP 语义，多为 4xx）：
      返回异常自身消息；
    * 生产 + 未预期异常：返回不包含任何内部信息的通用文案。
    """
    from sanic.exceptions import SanicException

    if debug or isinstance(exception, SanicException):
        return str(exception)
    return fallback_text


def can_disclose(attr: str, debug: bool) -> bool:
    """判断异常上的附加上下文是否允许出现在响应中。

    ``context`` 被设计为“面向调用方的公开上下文”（例如校验失败的
    字段列表），生产模式也可输出；``extra`` 是内部诊断数据，仅在调试
    模式输出。堆栈等其它信息同理，只在调试模式公开。
    """
    if attr == "context":
        return True
    if attr == "extra":
        return debug
    return debug
