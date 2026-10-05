from __future__ import annotations

from sanic.errorpages import (
    BaseRenderer,
    ErrorRepresentationRegistry,
    TextRenderer,
    minimal_fallback,
    render_error_response,
)
from sanic.exceptions import ServerError
from sanic.log import error_logger
from sanic.models.handler_types import RouteHandler
from sanic.request.types import Request
from sanic.response import text
from sanic.response.types import HTTPResponse


class ErrorHandler:
    """异常处理器：自定义处理器查找 + 版本化错误表示注册表渲染。

    每个 ErrorHandler 持有一个 :class:`ErrorRepresentationRegistry`，
    默认错误响应按异常层级 × 路由范围 × 媒体类型 × 安全级别选择
    渲染器；处理器或渲染器再次失败时走可预测的最小回退。
    """

    def __init__(
        self,
        base: type[BaseRenderer] = TextRenderer,
    ):
        self.cached_handlers: dict[
            tuple[type[BaseException], str | None], RouteHandler | None
        ] = {}
        self.debug = False
        self.base = base
        self.registry = ErrorRepresentationRegistry(base=base)

    def _full_lookup(self, exception, route_name: str | None = None):
        return self.lookup(exception, route_name)

    def _add(
        self,
        key: tuple[type[BaseException], str | None],
        handler: RouteHandler,
    ) -> None:
        if key in self.cached_handlers:
            exc, name = key
            if name is None:
                name = "__ALL_ROUTES__"

            message = (
                f"Duplicate exception handler definition on: route={name} "
                f"and exception={exc}"
            )
            raise ServerError(message)
        self.cached_handlers[key] = handler

    def add(self, exception, handler, route_names: list[str] | None = None):
        """项目内部接口说明。"""
        if route_names:
            for route in route_names:
                self._add((exception, route), handler)
        else:
            self._add((exception, None), handler)

    def lookup(self, exception, route_name: str | None = None):
        """项目内部接口说明。"""
        exception_class = type(exception)

        for name in (route_name, None):
            exception_key = (exception_class, name)
            handler = self.cached_handlers.get(exception_key)
            if handler:
                return handler

        for name in (route_name, None):
            for ancestor in type.mro(exception_class):
                exception_key = (ancestor, name)
                if exception_key in self.cached_handlers:
                    handler = self.cached_handlers[exception_key]
                    self.cached_handlers[(exception_class, route_name)] = (
                        handler
                    )
                    return handler

                if ancestor is BaseException:
                    break
        self.cached_handlers[(exception_class, route_name)] = None
        handler = None
        return handler

    _lookup = _full_lookup

    def response(self, request, exception):
        """项目内部接口说明。"""
        route_name = request.name if request else None
        handler = self._lookup(exception, route_name)
        response = None
        try:
            if handler:
                response = handler(request, exception)
            if response is None:
                response = self.default(request, exception)
        except Exception as handler_failure:
            # 自定义异常处理器（或其渲染过程）失败：不能再让异常向上
            # 传播，统一交给可预测的最小回退表示。
            try:
                url = repr(request.url)
            except AttributeError:  # no cov
                url = "unknown"
            response_message = (
                'Exception raised in exception handler "%s" for uri: %s'
            )
            error_logger.exception(
                response_message, getattr(handler, "__name__", "<unknown>"), url
            )

            if self.debug:
                # 调试模式保留历史上的详细文本，便于定位失败的处理器。
                return text(
                    response_message
                    % (getattr(handler, "__name__", "<unknown>"), url),
                    500,
                )
            return minimal_fallback(
                request,
                status=500,
                debug=False,
                failure=handler_failure,
            )
        return response

    def default(self, request: Request, exception: Exception) -> HTTPResponse:
        """项目内部接口说明。"""
        self.log(request, exception)
        config = request.app.config if request is not None else None
        fallback = getattr(config, "FALLBACK_ERROR_FORMAT", "auto")
        version = getattr(
            config,
            "ERROR_REPRESENTATION_VERSION",
            self.registry.version,
        )
        return render_error_response(
            request,
            exception,
            debug=self.debug,
            fallback=fallback,
            registry=self.registry,
            version=version,
        )

    @staticmethod
    def log(request: Request, exception: Exception) -> None:
        """项目内部接口说明。"""
        quiet = getattr(exception, "quiet", False)
        noisy = getattr(request.app.config, "NOISY_EXCEPTIONS", False)
        if quiet is False or noisy is True:
            try:
                url = repr(request.url)
            except AttributeError:  # no cov
                url = "unknown"

            error_logger.exception(
                "Exception occurred while handling uri: %s", url
            )
