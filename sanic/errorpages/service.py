"""错误渲染编排：协商 → 注册表解析 → 渲染 → 触底回退。"""

from __future__ import annotations

import typing as t

from sanic.log import error_logger

from .base import RenderingContext
from .fallback import minimal_fallback
from .negotiation import negotiate
from .registry import ErrorRepresentationRegistry
from .security import SecurityLevel

if t.TYPE_CHECKING:  # pragma: no cover
    from sanic import HTTPResponse, Request
    from sanic.errorpages.base import BaseRenderer


def render_error_response(
    request: "Request",
    exception: Exception,
    *,
    debug: bool,
    fallback: str,
    registry: ErrorRepresentationRegistry,
    version: str | None = None,
    renderer: t.Optional[t.Type["BaseRenderer"]] = None,
) -> "HTTPResponse":
    """渲染默认错误响应。

    流程：协商媒体类型 → 按（异常层级 × 路由范围 × 媒体类型 ×
    安全级别 × 版本）从注册表解析渲染器 → 渲染；任何一步失败都走
    :func:`sanic.errorpages.fallback.minimal_fallback`。
    """
    version = version or registry.version
    status = _exception_status(exception)
    try:
        negotiation = negotiate(request, fallback)
        security = SecurityLevel.from_debug(debug)

        if renderer is None:
            if negotiation.matched:
                renderer = registry.resolve(
                    type(exception),
                    request.name if request else None,
                    negotiation.mime,
                    security,
                    version,
                )
            else:
                # 客户端不接受任何可渲染媒体类型——使用基础渲染器，
                # 其输出（默认 text/plain）是任何客户端都能读的最小表示。
                renderer = registry.base

        ctx = RenderingContext(
            request=request,
            exception=exception,
            debug=debug,
            version=version,
            negotiation=negotiation,
        )
        return renderer(ctx).render()
    except Exception as render_failure:  # 渲染器再次失败：可预测触底
        try:
            url = repr(request.url)
        except Exception:  # pragma: no cover
            url = "unknown"
        error_logger.exception(
            "Exception raised while rendering an error response for %s", url
        )
        return minimal_fallback(
            request,
            status=status,
            debug=debug,
            failure=render_failure,
        )


def _exception_status(exception: Exception) -> int:
    return getattr(exception, "status_code", 500) or 500
