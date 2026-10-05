"""错误响应的媒体类型协商。

选择渲染器需要知道客户端能解析哪种媒体类型。协商综合三个信号，
优先级与历史行为保持一致：

1. 路由范围：``route.extra.error_format``（蓝图 / 路由可显式覆盖）；
2. 全局配置：``FALLBACK_ERROR_FORMAT``；
3. 自动探测：``Accept`` 头、请求 ``Content-Type``（以及历史遗留的
   请求体 JSON 探测）。

协商结果会记录哪些请求头参与了决策，供渲染层生成 ``Vary``——
缓存据此知道同一路由在不同 ``Accept`` 下可能返回不同表示。
"""

from __future__ import annotations

import dataclasses

from sanic.exceptions import BadRequest
from sanic.log import deprecation, logger


JSON = "application/json"

#: 默认错误格式：根据 Accept / 路由 / Content-Type 自动协商。
DEFAULT_FORMAT = "auto"

#: 自动协商时可能参考的全部请求头（用于 Vary）。
NEGOTIATION_VARY = ("Accept", "Content-Type")

MIME_BY_CONFIG = {
    "text": "text/plain",
    "json": "application/json",
    "html": "text/html",
}
CONFIG_BY_MIME = {v: k for k, v in MIME_BY_CONFIG.items()}

# Handler source code is checked for which response types it returns with the
# route error_format="auto" (default) to determine which format to use.
RESPONSE_MAPPING = {
    "json": "json",
    "text": "text",
    "html": "html",
    "JSONResponse": "json",
    "text/plain": "text",
    "text/html": "html",
    "application/json": "json",
}


@dataclasses.dataclass(frozen=True)
class NegotiationResult:
    """一次媒体类型协商的结果。

    ``mime`` 为空字符串表示没有任何客户端可接受的媒体类型，调用方应
    退回注册表的基础渲染器。``vary`` 记录实际参与决策的请求头。
    """

    mime: str
    format: str | None
    source: str | None
    vary: tuple[str, ...]
    consulted_content_type: bool

    @property
    def matched(self) -> bool:
        return bool(self.mime)


def negotiate(req, fallback: str) -> NegotiationResult:
    """执行协商，返回结构化结果（见模块文档字符串）。"""
    # Attempt to find a suitable MIME format for the response.
    # Insertion-ordered map of formats["html"] = "source of that suggestion"
    formats: dict[str, str] = {}
    name = ""
    # Route error_format (by magic from handler code if auto, the default)
    if getattr(req, "route", None):
        name = req.route.name
        f = req.route.extra.error_format
        if f in MIME_BY_CONFIG:
            formats[f] = name

    if not formats and fallback in MIME_BY_CONFIG:
        formats[fallback] = "FALLBACK_ERROR_FORMAT"

    # 只有路由与全局配置都没有固定表示时，请求侧线索（Accept 精确
    # 匹配、Content-Type、请求体 JSON）才可能改变结果。仅此时表示随
    # Content-Type 变化，需要把它写入 Vary。
    auto_detect = fallback == "auto" and not formats

    # If still not known, check for the request for clues of JSON
    if auto_detect and req.accept.match(JSON):
        if JSON in req.accept:  # Literally, not wildcard
            formats["json"] = "request.accept"
        elif JSON in req.headers.getone("content-type", ""):
            formats["json"] = "content-type"
        # DEPRECATION: Remove this block in 24.3
        else:
            c = None
            try:
                c = req.json
            except BadRequest:
                pass
            if c:
                formats["json"] = "request.json"
                deprecation(
                    "Response type was determined by the JSON content of "
                    "the request. This behavior is deprecated and will be "
                    "removed in v24.3. Please specify the format either by\n"
                    f'  error_format="json" on route {name}, by\n'
                    '  FALLBACK_ERROR_FORMAT = "json", or by adding header\n'
                    "  accept: application/json to your requests.",
                    24.3,
                )

    # Any other supported formats
    if fallback == "auto":
        for k in MIME_BY_CONFIG:
            if k not in formats:
                formats[k] = "any"

    mimes = [MIME_BY_CONFIG[k] for k in formats]
    m = req.accept.match(*mimes)
    if m:
        format = CONFIG_BY_MIME[m.mime]
        source = formats[format]
        logger.debug(
            "Error Page: The client accepts %s, using '%s' from %s",
            m.header,
            format,
            source,
            stacklevel=2,
        )
        return NegotiationResult(
            mime=m.mime,
            format=format,
            source=source,
            vary=_build_vary(auto_detect),
            consulted_content_type=auto_detect,
        )

    logger.debug(
        "Error Page: No format found, the client accepts %s",
        repr(req.accept),
        stacklevel=2,
    )
    return NegotiationResult(
        mime=m.mime,
        format=None,
        source=None,
        vary=_build_vary(auto_detect),
        consulted_content_type=auto_detect,
    )


def _build_vary(consulted_content_type: bool) -> tuple[str, ...]:
    # Accept 永远参与排序（accept.match）；Content-Type 仅在 auto 探测
    # JSON 线索时参与。
    return ("Accept", "Content-Type") if consulted_content_type else ("Accept",)


def guess_mime(req, fallback: str) -> str:
    """历史接口：只返回协商出的 MIME（无匹配时返回空字符串）。"""
    return negotiate(req, fallback).mime
