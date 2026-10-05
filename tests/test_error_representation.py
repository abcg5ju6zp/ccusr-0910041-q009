"""版本化错误表示注册表、协商、安全级别与最小回退的测试。"""

from __future__ import annotations

import pytest

from sanic import Sanic
from sanic.errorpages import (
    BaseRenderer,
    ErrorRepresentationRegistry,
    JSONRenderer,
    SecurityLevel,
    TextRenderer,
    minimal_fallback,
    render_error_response,
)
from sanic.exceptions import NotFound, SanicException, ServerError
from sanic.request import Request
from sanic.response import json as json_response


# --------------------------------------------------------------------- #
# 注册表：异常层级 × 路由范围 × 媒体类型 × 安全级别 × 版本
# --------------------------------------------------------------------- #


class MarkerRenderer(JSONRenderer):
    """渲染结果带标记的渲染器，便于断言注册表选中了谁。"""

    marker = "marker"

    def minimal(self):
        output = self._generate_output(full=False)
        output["marker"] = self.marker
        return json_response(output)


def test_registry_falls_back_to_base_for_unknown_media():
    registry = ErrorRepresentationRegistry(base=TextRenderer)
    renderer = registry.resolve(
        Exception, None, "application/xml", SecurityLevel.SAFE
    )
    assert renderer is TextRenderer


def test_registry_exception_hierarchy_prefers_specific_class():
    registry = ErrorRepresentationRegistry()

    class MyServerError(ServerError):
        pass

    registry.register(
        MarkerRenderer, exception_type=MyServerError, media_type="application/json"
    )
    resolved = registry.resolve(
        MyServerError, None, "application/json", SecurityLevel.SAFE
    )
    assert resolved is MarkerRenderer
    # 基类异常不受影响
    assert (
        registry.resolve(
            ServerError, None, "application/json", SecurityLevel.SAFE
        )
        is JSONRenderer
    )


def test_registry_route_scope_beats_global(app: Sanic):
    registry = app.error_handler.registry

    class RoutedRenderer(MarkerRenderer):
        marker = "routed"

    @app.get("/routed", name="routed")
    async def routed(request):
        raise ServerError("boom")

    @app.get("/global", name="global")
    async def global_(request):
        raise ServerError("boom")

    # 路由名使用 Sanic 的全限定名（app 名.路由名）
    registry.register(
        RoutedRenderer,
        exception_type=Exception,
        route=f"{app.name}.routed",
        media_type="application/json",
    )

    _, response = app.test_client.get("/routed", headers={"accept": "application/json"})
    assert response.json["marker"] == "routed"

    _, response = app.test_client.get("/global", headers={"accept": "application/json"})
    assert "marker" not in response.json


def test_registry_security_level_dimension():
    registry = ErrorRepresentationRegistry()

    class SafeOnly(MarkerRenderer):
        marker = "safe-only"

    registry.register(
        SafeOnly,
        exception_type=ServerError,
        media_type="application/json",
        security=SecurityLevel.SAFE,
    )

    assert (
        registry.resolve(
            ServerError, None, "application/json", SecurityLevel.SAFE
        )
        is SafeOnly
    )
    # DEBUG 级别没有精确注册，退回 v1.0 默认的 JSONRenderer
    assert (
        registry.resolve(
            ServerError, None, "application/json", SecurityLevel.DEBUG
        )
        is JSONRenderer
    )


def test_registry_specificity_beats_wildcard_but_wildcard_matches_other_media():
    registry = ErrorRepresentationRegistry()

    class JsonOnly(MarkerRenderer):
        marker = "json-only"

    registry.register(
        JsonOnly, exception_type=Exception, media_type="application/json"
    )
    # text/plain 没有精确注册之外的东西，默认 TextRenderer 仍生效
    assert (
        registry.resolve(Exception, None, "application/json", SecurityLevel.SAFE)
        is JsonOnly
    )
    assert (
        registry.resolve(Exception, None, "text/plain", SecurityLevel.SAFE)
        is TextRenderer
    )


def test_registry_version_isolation():
    registry = ErrorRepresentationRegistry()

    class V2Renderer(MarkerRenderer):
        marker = "v2"

    registry.register(
        V2Renderer,
        exception_type=Exception,
        media_type="application/json",
        version="2.0",
    )
    assert (
        registry.resolve(
            Exception, None, "application/json", SecurityLevel.SAFE, "1.0"
        )
        is JSONRenderer
    )
    assert (
        registry.resolve(
            Exception, None, "application/json", SecurityLevel.SAFE, "2.0"
        )
        is V2Renderer
    )
    registry.version = "2.0"
    assert (
        registry.resolve(
            Exception, None, "application/json", SecurityLevel.SAFE
        )
        is V2Renderer
    )


def test_registry_same_specificity_later_registration_wins():
    registry = ErrorRepresentationRegistry()

    class First(MarkerRenderer):
        marker = "first"

    class Second(MarkerRenderer):
        marker = "second"

    registry.register(First, media_type="application/x-custom")
    registry.register(Second, media_type="application/x-custom")
    resolved = registry.resolve(
        Exception, None, "application/x-custom", SecurityLevel.SAFE
    )
    # 同维度后注册者覆盖，应用可以替换内置/先前的表示
    assert resolved is Second


def test_registry_resolve_is_cached_then_invalidated():
    registry = ErrorRepresentationRegistry()
    assert (
        registry.resolve(
            Exception, None, "text/plain", SecurityLevel.SAFE
        )
        is TextRenderer
    )

    class Override(MarkerRenderer):
        marker = "override"

    entry = registry.register(Override, media_type="text/plain")
    # 注册后缓存失效，新注册生效
    assert (
        registry.resolve(
            Exception, None, "text/plain", SecurityLevel.SAFE
        )
        is Override
    )
    registry.unregister(entry)
    assert (
        registry.resolve(
            Exception, None, "text/plain", SecurityLevel.SAFE
        )
        is TextRenderer
    )


def test_config_switches_representation_version(app: Sanic):
    class V2Renderer(MarkerRenderer):
        marker = "v2"

    app.error_handler.registry.register(
        V2Renderer,
        exception_type=Exception,
        media_type="application/json",
        version="2.0",
    )

    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get("/boom", headers={"accept": "application/json"})
    assert "marker" not in response.json

    app.config.ERROR_REPRESENTATION_VERSION = "2.0"
    _, response = app.test_client.get("/boom", headers={"accept": "application/json"})
    assert response.json["marker"] == "v2"


# --------------------------------------------------------------------- #
# 关联标识
# --------------------------------------------------------------------- #


def test_json_error_carries_correlation_id(app: Sanic):
    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get(
        "/boom", headers={"accept": "application/json", "x-request-id": "abc-123"}
    )
    assert response.status == 500
    assert response.headers["x-request-id"] == "abc-123"
    assert response.json["request_id"] == "abc-123"


def test_correlation_header_name_follows_config(app: Sanic):
    app.config.REQUEST_ID_HEADER = "X-Correlation-ID"

    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get(
        "/boom",
        headers={"accept": "application/json", "x-correlation-id": "cid-9"},
    )
    assert response.headers["x-correlation-id"] == "cid-9"
    assert response.json["request_id"] == "cid-9"
    assert "x-request-id" not in response.headers


def test_html_and_text_errors_echo_correlation_header(app: Sanic):
    @app.get("/not-found")
    async def missing(request):
        raise NotFound("nope")

    _, response = app.test_client.get(
        "/not-found",
        headers={"accept": "text/html", "x-request-id": "html-cid"},
    )
    assert response.headers["x-request-id"] == "html-cid"
    assert "html-cid" in response.text

    _, response = app.test_client.get(
        "/not-found",
        headers={"accept": "text/plain", "x-request-id": "text-cid"},
    )
    assert response.headers["x-request-id"] == "text-cid"


def test_generated_correlation_id_when_client_sends_none(app: Sanic):
    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get("/boom", headers={"accept": "application/json"})
    generated = response.headers["x-request-id"]
    assert generated
    assert response.json["request_id"] == generated


# --------------------------------------------------------------------- #
# 安全级别：生产模式不得泄露堆栈 / 内部信息
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("accept", ("application/json", "text/plain", "text/html"))
def test_production_never_leaks_unexpected_exception(app: Sanic, accept):
    secret = "super-secret-internal-detail"

    @app.get("/boom")
    async def boom(request):
        raise RuntimeError(secret)

    _, response = app.test_client.get("/boom", headers={"accept": accept})
    assert response.status == 500
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_production_hides_extra_but_keeps_context(app: Sanic):
    @app.get("/teapot", error_format="json")
    async def teapot(request):
        raise SanicException(
            "bad input",
            status_code=418,
            context={"field": "name"},
            extra={"internal": "hidden"},
        )

    _, response = app.test_client.get("/teapot")
    assert response.json["context"] == {"field": "name"}
    assert "extra" not in response.json


def test_debug_exposes_stack_and_extra(app: Sanic):
    @app.get("/boom", error_format="json")
    async def boom(request):
        raise ServerError(
            "visible in debug", extra={"internal": "shown"}
        )

    _, response = app.test_client.get("/boom", debug=True)
    assert response.json["extra"] == {"internal": "shown"}
    assert response.json["exceptions"]
    assert response.json["path"] == "/boom"


def test_expected_client_error_message_is_public(app: Sanic):
    @app.get("/missing", error_format="json")
    async def missing(request):
        raise NotFound("widget 42 not found")

    _, response = app.test_client.get("/missing")
    assert response.status == 404
    assert response.json["message"] == "widget 42 not found"


# --------------------------------------------------------------------- #
# 协商响应头：Vary / Cache-Control
# --------------------------------------------------------------------- #


def test_vary_accept_only_when_route_pins_format(app: Sanic):
    @app.get("/pinned", error_format="json")
    async def pinned(request):
        raise ServerError("boom")

    _, response = app.test_client.get("/pinned")
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert "accept" in vary
    assert "content-type" not in vary


def test_vary_includes_content_type_in_auto(app: Sanic):
    app.config.FALLBACK_ERROR_FORMAT = "auto"

    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get(
        "/boom", headers={"accept": "application/json"}
    )
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert vary == {"accept", "content-type"}


@pytest.mark.parametrize("accept", ("application/json", "text/plain", "text/html"))
def test_error_responses_are_no_store(app: Sanic, accept):
    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get("/boom", headers={"accept": accept})
    assert response.headers["cache-control"] == "no-store"


def test_exception_cache_control_header_is_respected(app: Sanic):
    @app.get("/teapot", error_format="json")
    async def teapot(request):
        raise SanicException(
            "stable",
            status_code=418,
            headers={"Cache-Control": "max-age=60"},
        )

    _, response = app.test_client.get("/teapot")
    assert response.headers["cache-control"] == "max-age=60"


# --------------------------------------------------------------------- #
# 渲染器再次失败 → 可预测的最小回退
# --------------------------------------------------------------------- #


class ExplodingRenderer(BaseRenderer):
    media_type = "application/json"

    def minimal(self):
        raise RuntimeError("renderer exploded")

    def full(self):  # pragma: no cover - 测试走 minimal
        raise RuntimeError("renderer exploded")


def test_renderer_failure_uses_minimal_fallback(app: Sanic):
    app.error_handler.registry.register(
        ExplodingRenderer,
        exception_type=ServerError,
        media_type="application/json",
    )

    @app.get("/boom")
    async def boom(request):
        raise ServerError("original failure")

    _, response = app.test_client.get(
        "/boom", headers={"accept": "application/json", "x-request-id": "fb-1"}
    )
    # 即使渲染器炸掉，响应仍然结构完整、可预测
    assert response.status == 500
    assert response.content_type == "text/plain; charset=utf-8"
    assert response.text == "An error occurred while handling an error"
    assert response.headers["x-request-id"] == "fb-1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Accept"
    assert "renderer exploded" not in response.text


def test_renderer_failure_debug_mode_appends_reason(app: Sanic):
    app.error_handler.registry.register(
        ExplodingRenderer,
        exception_type=ServerError,
        media_type="application/json",
    )

    @app.get("/boom")
    async def boom(request):
        raise ServerError("original failure")

    _, response = app.test_client.get(
        "/boom",
        headers={"accept": "application/json"},
        debug=True,
    )
    assert response.status == 500
    assert response.text.startswith("An error occurred while handling an error")
    assert "RuntimeError" in response.text
    assert "renderer exploded" in response.text


def test_custom_exception_handler_failure_uses_fallback(app: Sanic):
    @app.exception(ServerError)
    def bad_handler(request, exception):
        raise RuntimeError("handler exploded")

    @app.get("/boom")
    async def boom(request):
        raise ServerError("original")

    _, response = app.test_client.get(
        "/boom", headers={"x-request-id": "handler-fb"}
    )
    assert response.status == 500
    assert response.text == "An error occurred while handling an error"
    assert response.headers["x-request-id"] == "handler-fb"


def test_unacceptable_media_falls_back_to_base_text(app: Sanic):
    @app.get("/boom")
    async def boom(request):
        raise ServerError("boom")

    _, response = app.test_client.get(
        "/boom", headers={"accept": "application/xml"}
    )
    assert response.status == 500
    assert response.content_type == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"


def test_render_error_response_keeps_exception_status_on_failure(app: Sanic):
    class ExplodingTeapot(ExplodingRenderer):
        pass

    app.error_handler.registry.register(
        ExplodingTeapot,
        exception_type=SanicException,
        media_type="application/json",
    )

    @app.get("/teapot", error_format="json")
    async def teapot(request):
        raise SanicException("x", status_code=418)

    _, response = app.test_client.get("/teapot")
    assert response.status == 418
    assert response.text == "An error occurred while handling an error"


# --------------------------------------------------------------------- #
# 自定义处理器返回响应时注册表不介入
# --------------------------------------------------------------------- #


def test_custom_handler_response_bypasses_registry(app: Sanic):
    @app.exception(NotFound)
    def handle_not_found(request, exception):
        return json_response({"custom": True}, status=404)

    @app.get("/missing")
    async def missing(request):
        raise NotFound("gone")

    _, response = app.test_client.get("/missing")
    assert response.status == 404
    assert response.json == {"custom": True}


def test_legacy_renderer_constructor_still_works(app: Sanic):
    # 旧签名 Renderer(request, exception, debug) 直接实例化仍可用，
    # 并带上关联标识与缓存头。
    request = Request(
        b"/boom",
        {"accept": "*/*", "x-request-id": "legacy-1"},
        "1.1",
        "GET",
        None,
        app,
    )
    response = TextRenderer(request, ServerError("x"), False).render()
    assert response.status == 500
    assert response.headers["x-request-id"] == "legacy-1"
    assert response.headers["cache-control"] == "no-store"


def test_minimal_fallback_direct_contract(app: Sanic):
    request = Request(
        b"/boom",
        {"accept": "*/*", "x-request-id": "direct-fb"},
        "1.1",
        "GET",
        None,
        app,
    )
    response = minimal_fallback(request, debug=False, failure=RuntimeError("x"))
    assert response.status == 500
    assert response.body == b"An error occurred while handling an error"
    assert response.headers["x-request-id"] == "direct-fb"
    assert response.headers["vary"] == "Accept"
    assert response.headers["cache-control"] == "no-store"


def test_render_error_response_direct_resolves_by_accept(app: Sanic):
    import json

    request = Request(
        b"/boom", {"accept": "application/json"}, "1.1", "GET", None, app
    )
    registry = ErrorRepresentationRegistry()
    response = render_error_response(
        request,
        ServerError("x"),
        debug=False,
        fallback="auto",
        registry=registry,
    )
    assert response.content_type == "application/json"
    assert json.loads(response.body)["request_id"]


