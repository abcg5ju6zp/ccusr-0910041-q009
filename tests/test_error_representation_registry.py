"""版本化错误表示注册表、渲染选择、最小回退与安全/缓存头的测试。"""

from __future__ import annotations

import json

import pytest

from sanic import Blueprint, Sanic
from sanic.errorpages import (
    FALLBACK_TEXT,
    REPRESENTATION_REGISTRY,
    ErrorRepresentation,
    HTMLRenderer,
    JSONRenderer,
    SecurityLevel,
    TextRenderer,
    exception_response,
    get_representation,
    guess_mime,
    minimal_fallback,
    register_representation,
    representation_for_mime,
)
from sanic.exceptions import (
    BadRequest,
    MethodNotAllowed,
    NotFound,
    SanicException,
    ServerError,
)
from sanic.request import Request
from sanic.response import HTTPResponse


@pytest.fixture
def app():
    app = Sanic("error_registry_test")

    @app.get("/unexpected")
    def unexpected(request):
        raise RuntimeError("internal secret value")

    @app.get("/teapot")
    def teapot(request):
        raise SanicException(
            "nope",
            status_code=418,
            context={"user": 42},
            extra={"internal": "hidden"},
        )

    return app


@pytest.fixture
def fake_request(app):
    return Request(b"/foobar", {"accept": "*/*"}, "1.1", "GET", None, app)


# --------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------- #


def test_registry_contains_versioned_default_representations():
    for key in ("json", "text", "html"):
        representation = get_representation(key)
        assert isinstance(representation, ErrorRepresentation)
        assert representation.version == 1

    assert representation_for_mime("application/json").renderer is JSONRenderer
    assert representation_for_mime("text/plain").renderer is TextRenderer
    assert representation_for_mime("text/html").renderer is HTMLRenderer
    # multipart 表单请求历史上回落到 HTML 表示（别名）
    assert (
        representation_for_mime("multipart/form-data").renderer is HTMLRenderer
    )


def test_register_custom_versioned_representation(fake_request):
    class V2JSONRenderer(JSONRenderer):
        pass

    representation = ErrorRepresentation(
        version=2,
        mime="application/vnd.error+json",
        renderer=V2JSONRenderer,
        fallback_keys=("json", "text"),
    )

    try:
        register_representation("error-json-v2", representation)
        assert get_representation("error-json-v2") is representation
        assert (
            representation_for_mime("application/vnd.error+json")
            is representation
        )
        assert representation.fallback_keys == ("json", "text")
    finally:
        REPRESENTATION_REGISTRY.pop("error-json-v2", None)


# --------------------------------------------------------------------- #
# 安全级别
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc_factory,debug,expected",
    (
        (lambda: BadRequest("bad"), False, SecurityLevel.SAFE),
        (lambda: NotFound("missing"), False, SecurityLevel.SAFE),
        (lambda: ServerError("boom"), False, SecurityLevel.SENSITIVE),
        (lambda: RuntimeError("x"), False, SecurityLevel.UNEXPECTED),
        # 调试模式下一切按 SAFE 处理（渲染器再决定是否展示堆栈）
        (lambda: RuntimeError("x"), True, SecurityLevel.SAFE),
    ),
)
def test_security_level_classification(exc_factory, debug, expected):
    assert SecurityLevel.of(exc_factory(), debug) is expected


def test_production_does_not_leak_unexpected_exception(app):
    _, response = app.test_client.get(
        "/unexpected", headers={"accept": "application/json"}
    )
    assert response.status == 500
    assert response.content_type == "application/json"
    body = response.json
    assert body["message"] == FALLBACK_TEXT
    assert "internal secret value" not in response.text
    assert "exceptions" not in body
    assert "path" not in body
    assert "extra" not in body


@pytest.mark.parametrize(
    "accept",
    ("application/json", "text/plain", "text/html"),
)
def test_production_never_leaks_stacktrace(app, accept):
    _, response = app.test_client.get(
        "/unexpected", headers={"accept": accept}
    )
    assert response.status == 500
    assert b"internal secret value" not in response.body
    assert b"Traceback" not in response.body
    assert b"RuntimeError" not in response.body


def test_production_does_not_leak_extra_but_keeps_context(app):
    _, response = app.test_client.get(
        "/teapot", headers={"accept": "application/json"}
    )
    assert response.status == 418
    assert response.json["context"] == {"user": 42}
    assert "extra" not in response.json
    assert "internal" not in response.text


def test_debug_reveals_traceback_and_extra(app):
    _, response = app.test_client.get(
        "/unexpected",
        headers={"accept": "application/json"},
        debug=True,
    )
    assert response.status == 500
    body = response.json
    assert body["exceptions"]
    assert body["exceptions"][0]["type"] == "RuntimeError"
    assert "internal secret value" in body["exceptions"][0]["exception"]
    assert body["path"] == "/unexpected"


def test_sanic_server_error_message_still_public(app):
    # 显式抛出的 SanicException（即使是 5xx）消息是调用方有意给出的，
    # 历史上会原样返回，这里保持该契约。
    @app.get("/explicit")
    def explicit(request):
        raise ServerError("database maintenance")

    _, response = app.test_client.get(
        "/explicit", headers={"accept": "application/json"}
    )
    assert response.status == 500
    assert response.json["message"] == "database maintenance"


# --------------------------------------------------------------------- #
# 关联标识
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("format", ("json", "text", "html"))
def test_request_id_reflected_in_header(app, format):
    app.config.FALLBACK_ERROR_FORMAT = format
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    _, response = app.test_client.get(
        "/unexpected",
        headers={"X-Request-ID": request_id},
    )
    assert response.status == 500
    assert response.headers["X-Request-ID"] == request_id
    if format == "json":
        assert response.json["request_id"] == request_id


def test_generated_request_id_used_when_absent(app):
    app.config.FALLBACK_ERROR_FORMAT = "json"
    _, response = app.test_client.get("/teapot")
    assert response.headers["X-Request-ID"]
    assert response.json["request_id"] == response.headers["X-Request-ID"]


# --------------------------------------------------------------------- #
# 缓存/协商响应头
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "accept,expected_type",
    (
        ("application/json", "application/json"),
        ("text/html", "text/html"),
        ("text/plain", "text/plain"),
    ),
)
def test_vary_and_cache_control_reflect_negotiation(
    app, accept, expected_type
):
    _, response = app.test_client.get("/teapot", headers={"accept": accept})
    assert response.content_type.split(";")[0] == expected_type
    assert response.headers["Vary"] == "Accept"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_vary_merges_with_exception_header(app):
    @app.get("/varying")
    def varying(request):
        raise MethodNotAllowed(
            "no",
            allowed_methods=["POST"],
            headers={"Vary": "Cookie"},
        )

    _, response = app.test_client.get(
        "/varying", headers={"accept": "application/json"}
    )
    values = {v.strip() for v in response.headers["Vary"].split(",")}
    assert values == {"Accept", "Cookie"}
    assert response.headers["Allow"] == "POST"


def test_exception_headers_remain_authoritative(app):
    @app.get("/challenged")
    def challenged(request):
        raise SanicException(
            "no",
            status_code=400,
            headers={"Cache-Control": "max-age=60", "X-Trace": "abc"},
        )

    _, response = app.test_client.get(
        "/challenged", headers={"accept": "application/json"}
    )
    # 异常自带的缓存策略优先于默认 no-store
    assert response.headers["Cache-Control"] == "max-age=60"
    assert response.headers["X-Trace"] == "abc"


# --------------------------------------------------------------------- #
# 渲染器失败时的最小回退
# --------------------------------------------------------------------- #


def test_renderer_failure_falls_back_along_chain(fake_request, monkeypatch):
    def boom(self):
        raise RuntimeError("text renderer exploded")

    monkeypatch.setattr(TextRenderer, "minimal", boom)
    monkeypatch.setattr(TextRenderer, "full", boom)

    try:
        raise BadRequest("bad input")
    except BadRequest as exc:
        response = exception_response(
            fake_request,
            exc,
            debug=False,
            base=TextRenderer,
            fallback="auto",
        )

    assert isinstance(response, HTTPResponse)
    assert response.status == 400
    # text 表示失败后按注册表回退到 JSON
    assert response.content_type == "application/json"
    assert json.loads(response.body)["message"] == "bad input"


def test_all_renderers_failing_use_minimal_fallback(fake_request, monkeypatch):
    def boom(self):
        raise RuntimeError("renderer exploded")

    for renderer in (TextRenderer, JSONRenderer, HTMLRenderer):
        monkeypatch.setattr(renderer, "minimal", boom)
        monkeypatch.setattr(renderer, "full", boom)

    try:
        raise BadRequest("bad input")
    except BadRequest as exc:
        response = exception_response(
            fake_request,
            exc,
            debug=False,
            base=TextRenderer,
            fallback="auto",
        )

    assert response.status == 400
    assert response.content_type == "text/plain; charset=utf-8"
    assert FALLBACK_TEXT.encode() in response.body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Accept"
    assert response.headers["X-Request-ID"]


def test_minimal_fallback_is_predictable_and_never_raises(fake_request):
    try:
        raise ServerError("kaboom")
    except ServerError as exc:
        response = minimal_fallback(fake_request, exc)

    assert response.status == 500
    assert response.content_type == "text/plain; charset=utf-8"
    assert response.body == FALLBACK_TEXT.encode()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Vary"] == "Accept"
    assert response.headers["X-Request-ID"]


def test_minimal_fallback_without_request():
    response = minimal_fallback(None, ServerError("kaboom"))
    assert response.status == 500
    assert response.body == FALLBACK_TEXT.encode()
    assert "X-Request-ID" not in response.headers


def test_end_to_end_renderer_failure_serves_minimal_response(app, monkeypatch):
    def boom(self):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(JSONRenderer, "minimal", boom)
    monkeypatch.setattr(JSONRenderer, "full", boom)
    monkeypatch.setattr(TextRenderer, "minimal", boom)
    monkeypatch.setattr(TextRenderer, "full", boom)
    monkeypatch.setattr(HTMLRenderer, "minimal", boom)
    monkeypatch.setattr(HTMLRenderer, "full", boom)

    _, response = app.test_client.get(
        "/teapot", headers={"accept": "application/json"}
    )
    assert response.status == 418
    assert response.content_type == "text/plain; charset=utf-8"
    assert FALLBACK_TEXT in response.text
    assert response.headers["X-Request-ID"]


# --------------------------------------------------------------------- #
# 选择：异常层级 + 路由范围 + Accept
# --------------------------------------------------------------------- #


def test_route_scope_overrides_global(app):
    app.config.FALLBACK_ERROR_FORMAT = "html"

    @app.get("/forced", error_format="json")
    def forced(request):
        raise NotFound("nope")

    _, response = app.test_client.get("/forced", headers={"accept": "*/*"})
    # 路由 error_format 压过全局 html
    assert response.content_type == "application/json"


def test_blueprint_scope_sets_format(app):
    app.config.FALLBACK_ERROR_FORMAT = "html"
    bp = Blueprint("Scoped", url_prefix="/bp")

    @bp.get("/missing")
    def missing(request):
        raise NotFound("nope")

    # 蓝图注册选项 error_format 对该蓝图下未显式声明格式的路由生效
    bp.register(app, {"error_format": "json"})

    _, response = app.test_client.get("/bp/missing", headers={"accept": "*/*"})
    assert response.content_type == "application/json"
    assert response.json["status"] == 404


def test_accept_used_when_no_forced_format(app):
    _, response = app.test_client.get(
        "/teapot", headers={"accept": "text/html"}
    )
    assert response.content_type == "text/html; charset=utf-8"


def test_unacceptable_accept_falls_back_to_base_text(fake_request):
    fake_request.headers["accept"] = "application/xml"
    try:
        raise NotFound("nope")
    except NotFound as exc:
        response = exception_response(
            fake_request,
            exc,
            debug=False,
            base=TextRenderer,
            fallback="auto",
        )
    assert response.content_type == "text/plain; charset=utf-8"
    assert response.status == 404


def test_guess_mime_carries_negotiation_metadata(fake_request):
    fake_request.headers["accept"] = "application/json"
    result = guess_mime(fake_request, "auto")
    assert str(result) == "application/json"
    assert result.used_accept is True
    assert result.format_name == "json"


def test_guess_mime_forced_format_not_flagged_as_accept(app, fake_request):
    class _Extra:
        error_format = "json"

    class _Route:
        name = "fake"
        extra = _Extra()

    fake_request.route = _Route()
    fake_request.headers["accept"] = "*/*"
    result = guess_mime(fake_request, "auto")
    assert str(result) == "application/json"
    # 选择来自路由 error_format，而非 Accept 通配
    assert result.used_accept is False
