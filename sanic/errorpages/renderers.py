"""内置错误渲染器：text / html / json。

字段与 25.x 保持兼容（JSON 的 ``description``/``status``/``message``，
text 的标题块，html 的 ErrorPage），在此之上：

- JSON / text / html 全部携带关联标识；
- ``extra`` 与堆栈只在调试模式出现（生产模式由安全层拦截）；
- html 同样渲染 ``context``，且不依赖调试开关。
"""

from __future__ import annotations

import typing as t

from sanic.pages.error import ErrorPage
from sanic.response import html, json, text as text_response

from .base import BaseRenderer

if t.TYPE_CHECKING:  # pragma: no cover
    from sanic import HTTPResponse


class HTMLRenderer(BaseRenderer):
    """text/html 错误页（面向运维人员的可读表示）。"""

    media_type = "text/html"

    def full(self) -> "HTTPResponse":
        page = ErrorPage(
            debug=self.debug,
            title=super().title,
            text=self.text,
            request=self.request,
            exc=self.exception,
        )
        return html(page.render())

    def minimal(self) -> "HTTPResponse":
        # 生产与调试共用同一 ErrorPage（页面内部已按 app.debug 收敛）。
        return self.full()


class TextRenderer(BaseRenderer):
    """text/plain 错误页（人与脚本都能读的最小可读表示）。"""

    media_type = "text/plain"
    OUTPUT_TEXT = "{title}\n{bar}\n{text}\n\n{body}"
    SPACER = "  "

    def full(self) -> "HTTPResponse":
        return text_response(
            self.OUTPUT_TEXT.format(
                title=self.title,
                text=self.text,
                bar=("=" * len(self.title)),
                body=self._generate_body(full=True),
            )
        )

    def minimal(self) -> "HTTPResponse":
        return text_response(
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
        # 注意：关联标识通过响应头（X-Request-ID）传递，不插入正文，
        # 以保持 title/bar/text 的固定三行布局，供行式客户端解析。
        if full:
            exceptions = []
            lines += [
                f"{self.exception.__class__.__name__}: {self.exception} while "
                f"handling path {self.request.path}",
                f"Traceback of {self.request.app.name} "
                "(most recent call last):\n",
            ]
            for entry in self._traceback_entries():
                frames = "\n\n".join(
                    [
                        f"{self.SPACER * 2}File {frame['file']}, "
                        f"line {frame['line']}, in {frame['name']}\n"
                        f"{self.SPACER * 2}{frame['src']}"
                        for frame in entry["frames"]
                    ]
                )
                exceptions.append(
                    f"{self.SPACER}{entry['type']}: {entry['exception']}\n"
                    f"{frames}"
                )
            lines += exceptions

        for attr in ("context", "extra"):
            info = self.context_items(attr)
            if info:
                lines += self._generate_object_display_list(info, attr)

        return "\n".join(lines)

    def _generate_object_display_list(self, obj, descriptor):
        lines = [f"\n{descriptor.title()}"]
        for key, value in obj.items():
            display = self.dumps(value)
            lines.append(f"{self.SPACER * 2}{key}: {display}")
        return lines


class JSONRenderer(BaseRenderer):
    """application/json 错误页（面向机器客户端的结构化表示）。"""

    media_type = "application/json"

    def full(self) -> "HTTPResponse":
        output = self._generate_output(full=True)
        return json(output, dumps=self.dumps)

    def minimal(self) -> "HTTPResponse":
        output = self._generate_output(full=False)
        return json(output, dumps=self.dumps)

    def _generate_output(self, *, full):
        output: dict[str, t.Any] = {
            "description": self.title,
            "status": self.status,
            "message": self.text,
        }

        rid = self.ctx.correlation_id
        if rid is not None:
            output["request_id"] = rid

        for attr in ("context", "extra"):
            info = self.context_items(attr)
            if info:
                output[attr] = info

        if full:
            output["path"] = self.request.path
            output["args"] = self.request.args
            output["exceptions"] = self._traceback_entries()

        return output

    @property
    def title(self):
        from sanic.helpers import STATUS_CODES

        return STATUS_CODES.get(self.status, b"Error Occurred").decode()
