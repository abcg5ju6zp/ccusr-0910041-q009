"""版本化错误表示注册表与渲染选择。

同一个服务往往同时面向机器客户端（按固定媒体类型解析响应体）与运维
人员（在浏览器里查看调试页）。本模块把"如何展示一个异常"拆成三层：

1. :class:`ErrorRepresentation` —— 错误表示的版本化注册表条目，绑定
   媒体类型、渲染器与回退顺序；
2. :func:`select_representation` / :func:`guess_mime` —— 根据异常层级、
   路由范围（路由 > 蓝图 > 全局 FALLBACK_ERROR_FORMAT）、Accept 协商
   结果选择表示；
3. :func:`exception_response` —— 以选定表示渲染，渲染器自身再次失败时
   沿注册表声明的回退链降级到可预测的最小响应。

所有错误响应都会：

* 回带关联标识（``X-Request-ID``，头名取自 ``REQUEST_ID_HEADER``），
  JSON 表示体中同时给出 ``request_id``；
* 只暴露允许公开的上下文（``context`` 总是随表示输出，``extra``、
  堆栈等仅调试模式输出）；
* 通过 ``Vary`` / ``Cache-Control`` 反映内容协商结果与"默认不缓存"的
  策略。
"""

from __future__ import annotations

import sys
import typing as t

from dataclasses import dataclass
from enum import IntEnum
from functools import partial
from traceback import extract_tb

from sanic.exceptions import BadRequest, SanicException
from sanic.helpers import STATUS_CODES
from sanic.log import deprecation, logger
from sanic.pages.error import ErrorPage
from sanic.response import HTTPResponse, html, json, text


dumps: t.Callable[..., str]
try:
    from ujson import dumps

    dumps = partial(dumps, escape_forward_slashes=False)
except ImportError:  # noqa
    from json import dumps

if t.TYPE_CHECKING:
    from sanic import Request

DEFAULT_FORMAT = "auto"
FALLBACK_TEXT = """\
The application encountered an unexpected error and could not continue.\
"""
FALLBACK_STATUS = 500
JSON = "application/json"

# 缓存相关响应头
VARY = "Vary"
CACHE_CONTROL = "Cache-Control"
CACHE_CONTROL_VALUE = "no-store"
CONTENT_TYPE_OPTIONS = "X-Content-Type-Options"
CONTENT_TYPE_OPTIONS_VALUE = "nosniff"


class SecurityLevel(IntEnum):
    """异常信息的敏感级别，决定可向客户端公开的内容。

    所有渲染器在非调试模式下都不得输出堆栈与 ``extra``；消息本身仅在
    :attr:`UNEXPECTED` 级别被替换为固定文案。自定义渲染器可按级别
    进一步收敛输出。
    """

    # 客户端错误（4xx）或调试模式：消息是有意返回给调用方的，公开
    SAFE = 1
    # 服务端有意抛出的 5xx SanicException：消息仍可公开，但被标记为
    # 敏感，自定义表示可选择只输出状态短语
    SENSITIVE = 2
    # 非 SanicException 的未预期异常：生产模式只回固定文案，
    # 绝不回显 str(exception)，以免泄露内部细节
    UNEXPECTED = 3

    @classmethod
    def of(cls, exception: Exception, debug: bool) -> "SecurityLevel":
        """项目内部接口说明。"""
        if debug:
            return cls.SAFE
        if not isinstance(exception, SanicException):
            return cls.UNEXPECTED
        status = getattr(exception, "status_code", FALLBACK_STATUS) or 0
        return cls.SAFE if status < 500 else cls.SENSITIVE


@dataclass(frozen=True)
class ErrorRepresentation:
    """注册表中的一个版本化错误表示。

    :param version: 表示版本号，便于未来在不破坏既有媒体类型消费者的
        情况下演进错误体结构。
    :param mime: 该表示的具体媒体类型。
    :param renderer: 渲染器类。
    :param fallback_keys: 该表示渲染失败时的回退顺序（注册表里的
        ``key``），最终回退由最小文本响应兜底。
    """

    version: int
    mime: str
    renderer: t.Type["BaseRenderer"]
    fallback_keys: t.Tuple[str, ...] = ()
    aliases: t.Tuple[str, ...] = ()


class BaseRenderer:
    """项目内部接口说明。"""

    dumps = staticmethod(dumps)

    def __init__(self, request: Request, exception: Exception, debug: bool):
        self.request = request
        self.exception = exception
        self.debug = debug
        self.security = SecurityLevel.of(exception, debug)

    @property
    def headers(self) -> t.Dict[str, str]:
        """项目内部接口说明。"""
        headers: t.Dict[str, str] = {}
        if isinstance(self.exception, SanicException):
            headers.update(getattr(self.exception, "headers", {}) or {})
        return headers

    @property
    def status(self):
        """项目内部接口说明。"""
        if isinstance(self.exception, SanicException):
            return getattr(self.exception, "status_code", FALLBACK_STATUS)
        return FALLBACK_STATUS

    @property
    def text(self):
        """项目内部接口说明。"""
        if self.security is SecurityLevel.UNEXPECTED:
            return FALLBACK_TEXT
        return str(self.exception)

    @property
    def request_id(self) -> t.Optional[str]:
        """关联标识：请求自带则回显，否则惰性生成后回带。"""
        try:
            value = self.request.id
        except Exception:
            return None
        return None if value is None else str(value)

    @property
    def title(self):
        """项目内部接口说明。"""
        status_text = STATUS_CODES.get(self.status, b"Error Occurred").decode()
        return f"{self.status} — {status_text}"

    def render(self) -> HTTPResponse:
        """项目内部接口说明。"""
        output = (
            self.full
            if self.debug and not getattr(self.exception, "quiet", False)
            else self.minimal
        )()
        output.status = self.status
        # 异常自带的响应头（Allow、WWW-Authenticate、Content-Range 等）
        # 拥有最高优先级；协商/缓存/关联头只做补全。
        _update_missing(output.headers, self._negotiation_headers())
        for key, value in self.headers.items():
            output.headers[key] = value
        return output

    def _negotiation_headers(self) -> t.Dict[str, str]:
        """协商结果与安全/缓存相关的通用响应头。"""
        headers = {
            CACHE_CONTROL: CACHE_CONTROL_VALUE,
            CONTENT_TYPE_OPTIONS: CONTENT_TYPE_OPTIONS_VALUE,
        }
        request_id = self.request_id
        if request_id is not None:
            try:
                header_name = self.request.app.config.REQUEST_ID_HEADER
            except Exception:
                header_name = "X-Request-ID"
            headers[header_name] = request_id
        return headers

    def minimal(self) -> HTTPResponse:  # noqa
        """项目内部接口说明。"""
        raise NotImplementedError

    def full(self) -> HTTPResponse:  # noqa
        """项目内部接口说明。"""
        raise NotImplementedError


class HTMLRenderer(BaseRenderer):
    """项目内部接口说明。"""

    def full(self) -> HTTPResponse:
        page = ErrorPage(
            debug=self.debug,
            title=super().title,
            text=super().text,
            request=self.request,
            exc=self.exception,
        )
        return html(page.render())

    def minimal(self) -> HTTPResponse:
        return self.full()


class TextRenderer(BaseRenderer):
    """项目内部接口说明。"""

    OUTPUT_TEXT = "{title}\n{bar}\n{text}\n\n{body}"
    SPACER = "  "

    def full(self) -> HTTPResponse:
        return text(
            self.OUTPUT_TEXT.format(
                title=self.title,
                text=self.text,
                bar=("=" * len(self.title)),
                body=self._generate_body(full=True),
            )
        )

    def minimal(self) -> HTTPResponse:
        return text(
            self.OUTPUT_TEXT.format(
                title=self.title,
                text=self.text,
                bar=("=" * len(self.title)),
                body=self._generate_body(full=False),
            )
        )

    @property
    def title(self):
        return f"⚠️ {super().title}"

    def _generate_body(self, *, full):
        lines = []
        if full:
            _, exc_value, __ = sys.exc_info()
            exceptions = []

            lines += [
                f"{self.exception.__class__.__name__}: {self.exception} while "
                f"handling path {self.request.path}",
                f"Traceback of {self.request.app.name} "
                "(most recent call last):\n",
            ]

            while exc_value:
                exceptions.append(self._format_exc(exc_value))
                exc_value = exc_value.__cause__

            lines += exceptions[::-1]

        for attr, display in (("context", True), ("extra", bool(full))):
            info = getattr(self.exception, attr, None)
            if info and display:
                lines += self._generate_object_display_list(info, attr)

        return "\n".join(lines)

    def _format_exc(self, exc):
        frames = "\n\n".join(
            [
                f"{self.SPACER * 2}File {frame.filename}, "
                f"line {frame.lineno}, in "
                f"{frame.name}\n{self.SPACER * 2}{frame.line}"
                for frame in extract_tb(exc.__traceback__)
            ]
        )
        return f"{self.SPACER}{exc.__class__.__name__}: {exc}\n{frames}"

    def _generate_object_display_list(self, obj, descriptor):
        lines = [f"\n{descriptor.title()}"]
        for key, value in obj.items():
            display = self.dumps(value)
            lines.append(f"{self.SPACER * 2}{key}: {display}")
        return lines


class JSONRenderer(BaseRenderer):
    """项目内部接口说明。"""

    REPRESENTATION_VERSION = 1

    def full(self) -> HTTPResponse:
        output = self._generate_output(full=True)
        return json(output, dumps=self.dumps)

    def minimal(self) -> HTTPResponse:
        output = self._generate_output(full=False)
        return json(output, dumps=self.dumps)

    def _generate_output(self, *, full):
        output = {
            "description": self.title,
            "status": self.status,
            "message": self.text,
        }

        request_id = self.request_id
        if request_id is not None:
            output["request_id"] = request_id

        for attr, display in (("context", True), ("extra", bool(full))):
            info = getattr(self.exception, attr, None)
            if info and display:
                output[attr] = info

        if full:
            _, exc_value, __ = sys.exc_info()
            exceptions = []

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

            output["path"] = self.request.path
            output["args"] = self.request.args
            output["exceptions"] = exceptions[::-1]

        return output

    @property
    def title(self):
        return STATUS_CODES.get(self.status, b"Error Occurred").decode()


def escape(text):
    """项目内部接口说明。"""
    return f"{text}".replace("&", "&amp;").replace("<", "&lt;")


# --------------------------------------------------------------------- #
# 版本化错误表示注册表
# --------------------------------------------------------------------- #
#
# key 既是历史上的 "format" 名（text/json/html，供 FALLBACK_ERROR_FORMAT、
# error_format 与 RESPONSE_MAPPING 使用），也是回退链里引用的名字。
# 注册表按 version 注册，新增表示版本应使用新的 key（如 "json-v2"），
# 并在需要时把别名（既有 mime）指向新版本。
MIME_BY_CONFIG = {
    "text": "text/plain",
    "json": "application/json",
    "html": "text/html",
}
CONFIG_BY_MIME = {v: k for k, v in MIME_BY_CONFIG.items()}

REPRESENTATION_REGISTRY: t.Dict[str, ErrorRepresentation] = {}


def register_representation(
    key: str,
    representation: ErrorRepresentation,
) -> ErrorRepresentation:
    """注册（或覆盖）一个错误表示。"""
    REPRESENTATION_REGISTRY[key] = representation
    return representation


def get_representation(key: str) -> t.Optional[ErrorRepresentation]:
    """项目内部接口说明。"""
    return REPRESENTATION_REGISTRY.get(key)


def representation_for_mime(mime: str) -> t.Optional[ErrorRepresentation]:
    """按具体媒体类型（含注册别名）查找表示。"""
    for representation in REPRESENTATION_REGISTRY.values():
        if mime == representation.mime or mime in representation.aliases:
            return representation
    return None


def _register_defaults() -> None:
    # JSON 最适合机器客户端，故任意表示渲染失败时优先向它回退；
    # text 是最小的人类可读表示；最后的兜底由 minimal_fallback 完成。
    register_representation(
        "json",
        ErrorRepresentation(
            version=1,
            mime="application/json",
            renderer=JSONRenderer,
            fallback_keys=("text",),
        ),
    )
    register_representation(
        "text",
        ErrorRepresentation(
            version=1,
            mime="text/plain",
            renderer=TextRenderer,
            fallback_keys=("json",),
        ),
    )
    register_representation(
        "html",
        ErrorRepresentation(
            version=1,
            mime="text/html",
            renderer=HTMLRenderer,
            # 调试页依赖 tracerite/html5tagger，一旦渲染失败，退回机器与
            # 纯文本消费者都能处理的表示，而不是抛出半个 HTML 页面。
            fallback_keys=("json", "text"),
            aliases=("multipart/form-data",),
        ),
    )


_register_defaults()

# 向后兼容的模块级名字
RENDERERS_BY_CONTENT_TYPE = {
    "text/plain": TextRenderer,
    "application/json": JSONRenderer,
    "multipart/form-data": HTMLRenderer,
    "text/html": HTMLRenderer,
}

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


def check_error_format(format):
    """项目内部接口说明。"""
    if format not in MIME_BY_CONFIG and format != "auto":
        raise SanicException(f"Unknown format: {format}")


def _update_missing(
    target: t.MutableMapping[str, str], values: t.Mapping[str, str]
) -> None:
    for key, value in values.items():
        target.setdefault(key, value)


def _merge_vary(headers: t.MutableMapping[str, str]) -> None:
    """把 Accept 并入已有的 Vary 值（保留大小写与既有条目）。"""
    existing = None
    for key in headers:
        if key.lower() == VARY.lower():
            existing = headers[key]
            break
    if existing:
        values = {part.strip() for part in existing.split(",") if part.strip()}
        values.add("Accept")
        headers[VARY] = ", ".join(sorted(values))
    else:
        headers[VARY] = "Accept"


def minimal_fallback(
    request: t.Optional[Request],
    exception: Exception,
) -> HTTPResponse:
    """最终的可预测最小回退。

    不依赖任何可能再次失败的复杂渲染逻辑：固定文案的 ``text/plain``
    响应，只补缓存/安全头与关联标识。它本身不得抛出。
    """
    status = FALLBACK_STATUS
    headers: t.Dict[str, str] = {
        CACHE_CONTROL: CACHE_CONTROL_VALUE,
        CONTENT_TYPE_OPTIONS: CONTENT_TYPE_OPTIONS_VALUE,
    }
    if isinstance(exception, SanicException):
        status = getattr(exception, "status_code", FALLBACK_STATUS) or status
        for key, value in (getattr(exception, "headers", None) or {}).items():
            headers[key] = value

    request_id: t.Optional[str] = None
    if request is not None:
        try:
            raw_id = request.id
            if raw_id is not None:
                request_id = str(raw_id)
                header_name = request.app.config.REQUEST_ID_HEADER
                headers[header_name] = request_id
        except Exception:
            request_id = None

    try:
        response = text(FALLBACK_TEXT, status, headers)
    except Exception:
        response = HTTPResponse(FALLBACK_TEXT.encode(), status=status)
        for key, value in headers.items():
            try:
                response.headers[key] = value
            except Exception:
                continue

    try:
        _merge_vary(response.headers)
    except Exception:
        pass
    return response


def exception_response(
    request: Request,
    exception: Exception,
    debug: bool,
    fallback: str,
    base: t.Type[BaseRenderer],
    renderer: t.Optional[t.Type[BaseRenderer]] = None,
) -> HTTPResponse:
    """渲染异常响应。

    选择顺序：显式 ``renderer`` → 注册表中按协商结果选出的表示 →
    ``base`` 渲染器。任意渲染器抛出时，沿该表示声明的
    ``fallback_keys`` 回退，最后由 :func:`minimal_fallback` 兜底。
    """
    negotiation = guess_mime(request, fallback)
    representation: t.Optional[ErrorRepresentation] = None
    if renderer is None:
        if negotiation.mime:
            representation = representation_for_mime(negotiation.mime)
        if representation is not None:
            selected: t.Type[BaseRenderer] = representation.renderer
        elif negotiation.mime:
            selected = RENDERERS_BY_CONTENT_TYPE.get(negotiation.mime, base)
        else:
            selected = base
    else:
        selected = renderer
        # 显式渲染器：若它本身已注册，则按其注册表示的回退链降级
        representation = representation_for_mime(_mime_for_renderer(selected))

    chain: t.List[t.Type[BaseRenderer]] = [selected]
    if representation is not None:
        for key in representation.fallback_keys:
            fallback_repr = REPRESENTATION_REGISTRY.get(key)
            if fallback_repr and fallback_repr.renderer not in chain:
                chain.append(fallback_repr.renderer)

    last_error: t.Optional[BaseException] = None
    response: t.Optional[HTTPResponse] = None
    for candidate in chain:
        try:
            response = candidate(request, exception, debug).render()
            break
        except Exception as e:  # renderer failed; try the next representation
            last_error = e
            logger.error(
                "Error renderer %s failed (%s); falling back",
                getattr(candidate, "__name__", candidate),
                e,
            )

    if response is None:
        logger.error(
            "All error renderers failed; using minimal fallback",
            exc_info=last_error,
        )
        return minimal_fallback(request, exception)

    # 错误表示始终会随 Accept 变化：即使路由用 error_format 固定了首选
    # 表示，客户端若完全不接受该媒体类型也会改走别的渲染器/回退链。
    # 因此用 Vary 告知共享缓存按 Accept 区分；若异常自身带了 Vary，
    # 保留并合并其条目。
    try:
        _merge_vary(response.headers)
    except Exception:
        pass

    return response


def _mime_for_renderer(renderer: t.Type[BaseRenderer]) -> str:
    for representation in REPRESENTATION_REGISTRY.values():
        if representation.renderer is renderer:
            return representation.mime
    return ""


class NegotiatedMime(str):
    """协商出的 mime 字符串，同时携带协商元信息。

    对历史调用方它就是普通 ``str``；注册表选择与缓存头逻辑可额外读取
    :attr:`used_accept`、:attr:`source` 等属性。
    """

    mime: str
    used_accept: bool
    format_name: str
    source: str
    forced_format: t.Optional[str]

    def __new__(
        cls,
        mime: str,
        *,
        used_accept: bool = False,
        format_name: str = "",
        source: str = "",
        forced_format: t.Optional[str] = None,
    ) -> "NegotiatedMime":
        obj = super().__new__(cls, mime)
        obj.mime = mime
        obj.used_accept = used_accept
        obj.format_name = format_name
        obj.source = source
        obj.forced_format = forced_format
        return obj


def guess_mime(
    req: Request,
    fallback: str,
) -> "NegotiatedMime":
    """项目内部接口说明。

    返回值在字符串用法下与历史行为一致（协商出的 mime，未命中时为空
    串），同时携带协商元信息（是否依赖 Accept、来源、被强制的格式）
    供注册表选择与缓存头使用。
    """
    # Attempt to find a suitable MIME format for the response.
    # Insertion-ordered map of formats["html"] = "source of that suggestion"
    formats = {}
    name = ""
    # Route error_format (by magic from handler code if auto, the default)
    if req.route:
        name = req.route.name
        f = req.route.extra.error_format
        if f in MIME_BY_CONFIG:
            formats[f] = name

    if not formats and fallback in MIME_BY_CONFIG:
        formats[fallback] = "FALLBACK_ERROR_FORMAT"

    # If still not known, check for the request for clues of JSON
    if not formats and fallback == "auto" and req.accept.match(JSON):
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

    forced_format = next(
        (fmt for fmt, source in formats.items() if source != "any"), None
    )
    if m:
        format_name = CONFIG_BY_MIME[m.mime]
        source = formats[format_name]
        # 显式的路由/回退配置本身就是决定性的；只有在 "any" 候选里
        # 通过 Accept 挑出来时才是真正的协商结果。
        used_accept = source == "any" or (
            source in ("request.accept", "content-type", "request.json")
        )
        logger.debug(
            "Error Page: The client accepts %s, using '%s' from %s",
            m.header,
            format_name,
            source,
        )
        return NegotiatedMime(
            m.mime,
            used_accept=used_accept,
            format_name=format_name,
            source=source,
            forced_format=forced_format,
        )

    logger.debug(
        "Error Page: No format found, the client accepts %s",
        repr(req.accept),
    )
    return NegotiatedMime(m.mime, forced_format=forced_format)
