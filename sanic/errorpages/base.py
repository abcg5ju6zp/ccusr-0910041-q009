"""错误渲染器基类与渲染上下文。

渲染器只负责“怎么把一条异常渲染成某个媒体类型的响应”；
*选哪个*渲染器由 :mod:`sanic.errorpages.registry` 决定。

所有渲染器产出的响应都会经过统一的收尾处理
（:meth:`BaseRenderer.render`）：

- 状态码与异常自带响应头（如 ``Allow``、``WWW-Authenticate``）；
- 关联标识（``X-Request-ID``，头名跟随 ``REQUEST_ID_HEADER``）；
- 协商头 ``Vary``；
- 默认的 ``Cache-Control: no-store``，防止错误响应被中间缓存复用。

无论哪种媒体类型，机器客户端都能凭关联标识把服务端日志与本次
失败对应起来。
"""

from __future__ import annotations

import sys
import typing as t
from traceback import extract_tb

from sanic.helpers import STATUS_CODES

from .security import can_disclose, safe_text
from .negotiation import NegotiationResult


dumps: t.Callable[..., str]
try:
    from ujson import dumps

    from functools import partial

    dumps = partial(dumps, escape_forward_slashes=False)
except ImportError:  # noqa
    from json import dumps

if t.TYPE_CHECKING:  # pragma: no cover
    from sanic import HTTPResponse, Request
    from sanic.errorpages.security import SecurityLevel

FALLBACK_TEXT = """\
The application encountered an unexpected error and could not continue.\
"""
FALLBACK_STATUS = 500

#: 当前内置错误表示版本。不兼容的字段 / 结构调整走新版本号，旧版本
#: 注册继续保留，由 ERROR_REPRESENTATION_VERSION 选择生效版本。
DEFAULT_ERROR_VERSION = "1.0"

# 错误响应描述的是某次具体请求的失败结果，默认不允许共享缓存或
# 浏览器缓存把它复用到后续请求上；处理器若另有考虑可以自行覆盖。
DEFAULT_CACHE_CONTROL = "no-store"


class RenderingContext:
    """一次错误渲染的全部输入。

    把散落在 request / exception / 配置上的信息收敛成一个值对象，
    注册表选择与渲染器实现都只依赖它，便于为单个表示版本演进字段。
    """

    __slots__ = (
        "request",
        "exception",
        "debug",
        "version",
        "negotiation",
        "fallback_text",
    )

    def __init__(
        self,
        request: "Request",
        exception: Exception,
        debug: bool,
        version: str,
        negotiation: NegotiationResult | None,
        fallback_text: str = FALLBACK_TEXT,
    ):
        self.request = request
        self.exception = exception
        self.debug = debug
        self.version = version
        self.negotiation = negotiation
        self.fallback_text = fallback_text

    @property
    def show_full(self) -> bool:
        """完整诊断视图（堆栈 / extra）需要调试模式且非 quiet。"""
        return self.debug and not getattr(self.exception, "quiet", False)

    def can_disclose(self, attr: str) -> bool:
        return can_disclose(attr, self.debug)

    @property
    def correlation_id(self) -> t.Optional[str]:
        """本次请求的关联标识；取不到时返回 ``None`` 而不是抛出。"""
        request = self.request
        try:
            rid = request.id
        except Exception:  # pragma: no cover - 防御构造不完整的请求
            return None
        if rid is None:
            return None
        return str(rid)

    @property
    def correlation_header(self) -> t.Optional[str]:
        try:
            return self.request.app.config.REQUEST_ID_HEADER
        except Exception:  # pragma: no cover
            return "X-Request-ID"


class BaseRenderer:
    """所有错误渲染器的基类。"""

    dumps = staticmethod(dumps)
    media_type: str = "text/plain"

    def __init__(
        self,
        ctx_or_request,
        exception: Exception | None = None,
        debug: bool = False,
    ):
        # 兼容两种构造方式：
        # - 新：BaseRenderer(RenderingContext(...))（注册表管线使用）；
        # - 旧：BaseRenderer(request, exception, debug)（外部直接实例化）。
        if isinstance(ctx_or_request, RenderingContext):
            ctx = ctx_or_request
        else:
            ctx = RenderingContext(
                request=ctx_or_request,
                exception=exception,  # type: ignore
                debug=debug,
                version=DEFAULT_ERROR_VERSION,
                negotiation=None,
            )
        self.ctx = ctx
        self.request = ctx.request
        self.exception = ctx.exception
        self.debug = ctx.debug

    # -- 异常的安全投影 --------------------------------------------------

    @property
    def headers(self) -> t.Dict[str, str]:
        """异常显式携带的响应头（如 Allow / WWW-Authenticate）。"""
        from sanic.exceptions import SanicException

        if isinstance(self.exception, SanicException):
            return getattr(self.exception, "headers", {})
        return {}

    @property
    def status(self) -> int:
        from sanic.exceptions import SanicException

        if isinstance(self.exception, SanicException):
            return getattr(self.exception, "status_code", FALLBACK_STATUS)
        return FALLBACK_STATUS

    @property
    def text(self) -> str:
        """经安全级别过滤后允许公开的异常文案。"""
        return safe_text(self.exception, self.ctx.fallback_text, self.debug)

    @property
    def title(self) -> str:
        status_text = STATUS_CODES.get(self.status, b"Error Occurred").decode()
        return f"{self.status} — {status_text}"

    def context_items(
        self, attr: str
    ) -> t.Optional[t.Mapping[str, t.Any]]:
        """返回允许公开的 ``context`` / ``extra``，否则为 ``None``。"""
        info = getattr(self.exception, attr, None)
        if info and self.ctx.can_disclose(attr):
            return info
        return None

    # -- 渲染入口 --------------------------------------------------------

    def render(self) -> "HTTPResponse":
        """渲染并叠加协商 / 缓存 / 关联标识等统一响应头。"""
        output = (self.full if self.ctx.show_full else self.minimal)()
        output.status = self.status
        output.headers.update(self.headers)
        self._add_representation_headers(output)
        return output

    def _add_representation_headers(self, output: "HTTPResponse") -> None:
        headers = output.headers
        # 关联标识：机器客户端与运维靠它串联日志与告警。
        rid = self.ctx.correlation_id
        header_name = self.ctx.correlation_header
        if rid is not None and header_name:
            headers.setdefault(header_name, rid)

        # 协商结果要反映到 Vary 上，避免缓存把一种媒体类型的错误页
        # 发给另一种客户端。
        if self.ctx.negotiation is not None:
            for name in self.ctx.negotiation.vary:
                existing = headers.get("vary")
                if not existing:
                    headers["Vary"] = name
                elif name.lower() not in existing.lower():
                    headers["Vary"] = f"{existing}, {name}"

        # 错误响应默认不缓存。显式设置过 Cache-Control 时不覆盖。
        headers.setdefault("Cache-Control", DEFAULT_CACHE_CONTROL)

    def minimal(self) -> "HTTPResponse":  # noqa
        raise NotImplementedError

    def full(self) -> "HTTPResponse":  # noqa
        raise NotImplementedError

    # -- 调试用堆栈收集（仅在调试模式调用） ------------------------------

    def _traceback_entries(self) -> list[dict[str, t.Any]]:
        _, exc_value, __ = sys.exc_info()
        exceptions: list[dict[str, t.Any]] = []
        while exc_value:
            exceptions.append(
                {
                    "type": exc_value.__class__.__name__,
                    "exception": str(exc_value),
                    "frames": [
                        {
                            "file": frame.filename,
                            "line": frame.lineno,
                            "name": frame.name,
                            "src": frame.line,
                        }
                        for frame in extract_tb(exc_value.__traceback__)
                    ],
                }
            )
            exc_value = exc_value.__cause__
        return exceptions[::-1]
