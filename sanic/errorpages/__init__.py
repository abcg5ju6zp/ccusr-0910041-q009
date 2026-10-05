"""版本化错误表示注册表与渲染管线。

公共入口：

- :class:`ErrorRepresentationRegistry`：按异常层级 × 路由范围 ×
  媒体类型 × 安全级别 × 版本选择渲染器；
- :func:`render_error_response` / 旧名 :func:`exception_response`：
  协商、解析、渲染的完整编排，渲染失败时自动走最小回退；
- :class:`SecurityLevel`：DEBUG / SAFE 信息披露环境；
- :class:`BaseRenderer` / :class:`TextRenderer` / :class:`HTMLRenderer`
  / :class:`JSONRenderer`：可继承的内置渲染器；
- :func:`guess_mime` / :func:`negotiate`：媒体类型协商；
- :func:`minimal_fallback`：可预测的触底响应。

历史名称（``DEFAULT_FORMAT``、``MIME_BY_CONFIG``、
``RESPONSE_MAPPING``、``check_error_format`` 等）继续可用。
"""

from __future__ import annotations

from .base import (
    FALLBACK_STATUS,
    FALLBACK_TEXT,
    BaseRenderer,
    RenderingContext,
)
from .fallback import GENERIC_MESSAGE, minimal_fallback
from .negotiation import (
    JSON,
    CONFIG_BY_MIME,
    DEFAULT_FORMAT,
    MIME_BY_CONFIG,
    NEGOTIATION_VARY,
    NegotiationResult,
    RESPONSE_MAPPING,
    guess_mime,
    negotiate,
)
from .registry import (
    DEFAULT_ERROR_VERSION,
    ERROR_REPRESENTATION_VERSIONS,
    ErrorRepresentationRegistry,
    RendererRegistration,
    VersionedRegistryView,
)
from .renderers import HTMLRenderer, JSONRenderer, TextRenderer
from .security import SecurityLevel, can_disclose, is_safe_status, safe_text
from .service import render_error_response


def check_error_format(format: str) -> None:
    """校验错误格式配置值（历史公共接口）。"""
    if format not in MIME_BY_CONFIG and format != DEFAULT_FORMAT:
        from sanic.exceptions import SanicException

        raise SanicException(f"Unknown format: {format}")


# 历史兼容：MIME → 渲染器的固定映射。新代码应使用注册表。
RENDERERS_BY_CONTENT_TYPE = {
    "text/plain": TextRenderer,
    "application/json": JSONRenderer,
    "multipart/form-data": HTMLRenderer,
    "text/html": HTMLRenderer,
}


def exception_response(
    request,
    exception: Exception,
    debug: bool,
    fallback: str,
    base: type[BaseRenderer],
    renderer: type[BaseRenderer] | None = None,
) -> "HTTPResponse":
    """历史公共接口：用一次性注册表渲染默认错误响应。"""
    registry = ErrorRepresentationRegistry(base=base)
    return render_error_response(
        request,
        exception,
        debug=debug,
        fallback=fallback,
        registry=registry,
        renderer=renderer,
    )


# 向后兼容的工具函数（旧 errorpages 模块导出过）。
def escape(value):
    """转义 HTML 特殊字符（历史公共接口）。"""
    return f"{value}".replace("&", "&amp;").replace("<", "&lt;")


__all__ = (
    "DEFAULT_ERROR_VERSION",
    "DEFAULT_FORMAT",
    "ERROR_REPRESENTATION_VERSIONS",
    "FALLBACK_STATUS",
    "FALLBACK_TEXT",
    "GENERIC_MESSAGE",
    "NEGOTIATION_VARY",
    "BaseRenderer",
    "CONFIG_BY_MIME",
    "ErrorRepresentationRegistry",
    "HTMLRenderer",
    "JSON",
    "JSONRenderer",
    "MIME_BY_CONFIG",
    "NegotiationResult",
    "RendererRegistration",
    "RENDERERS_BY_CONTENT_TYPE",
    "RESPONSE_MAPPING",
    "RenderingContext",
    "SecurityLevel",
    "TextRenderer",
    "VersionedRegistryView",
    "can_disclose",
    "check_error_format",
    "escape",
    "exception_response",
    "guess_mime",
    "is_safe_status",
    "minimal_fallback",
    "negotiate",
    "render_error_response",
    "safe_text",
)
