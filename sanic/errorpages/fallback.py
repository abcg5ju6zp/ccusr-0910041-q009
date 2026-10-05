"""渲染器自身失败时的可预测最小回退。

错误渲染管线里任何一步（自定义异常处理器、选定的渲染器、甚至 HTML
跟踪页）都可能再次抛出。此时不能再依赖复杂逻辑，否则会无限递归。
:func:`minimal_fallback` 只做固定的几件事，且不允许失败：

1. 用 ``text/plain`` 输出一句**固定**文案（生产模式正文严格固定，
   不附带任何内部信息；调试模式才附加触底异常摘要）；
2. 带上能拿到的关联标识（响应头，不影响固定正文）；
3. 标记 ``Vary: Accept`` 与 ``Cache-Control: no-store``。

响应对象用最简方式手工构造，绕过 json/html 等可能再次出错的高层
构造器。
"""

from __future__ import annotations

from sanic.response.types import HTTPResponse

from .base import DEFAULT_CACHE_CONTROL

GENERIC_MESSAGE = "An error occurred while handling an error"


def _correlation_id(request) -> tuple[str | None, str | None]:
    try:
        rid = request.id
        header_name = request.app.config.REQUEST_ID_HEADER
        return (str(rid) if rid is not None else None), header_name
    except Exception:  # pragma: no cover - request 不完整时的防御路径
        return None, None


def minimal_fallback(
    request,
    status: int = 500,
    *,
    debug: bool = False,
    failure: BaseException | None = None,
) -> HTTPResponse:
    """构造最小回退响应。此函数本身不得抛出。"""
    # 生产模式正文严格固定，机器客户端可以安全地按字面匹配；
    # 调试模式才在固定文案之后追加触底原因。
    body = GENERIC_MESSAGE
    if debug and failure is not None:
        body = (
            f"{GENERIC_MESSAGE}\n"
            f"Caused by: {type(failure).__name__}: {failure}"
        )

    rid, header_name = _correlation_id(request)
    headers: dict[str, str] = {
        "Vary": "Accept",
        "Cache-Control": DEFAULT_CACHE_CONTROL,
    }
    if rid is not None and header_name:
        headers.setdefault(header_name, rid)

    return HTTPResponse(
        body,
        status=status,
        headers=headers,
        content_type="text/plain; charset=utf-8",
    )
