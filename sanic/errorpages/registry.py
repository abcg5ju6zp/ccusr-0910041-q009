"""版本化的错误表示注册表。

注册表回答一个问题：**在当前请求下，应该用哪个渲染器呈现这条异常？**
选择依据四个正交维度，外加表示版本：

1. 异常层级：沿异常的 ``mro`` 从具体类向基类查找，最贴近的注册优先；
2. 路由范围：注册到具体路由（含蓝图路由名）的渲染器优先于全局注册；
3. 媒体类型：协商出的具体 MIME 优先于通配注册；
4. 安全级别：为 DEBUG / SAFE 精确注册的渲染器优先于通配注册；
5. 版本：仅在当前生效的错误表示版本内选择，旧版本注册保留不删，
   切换 ``ERROR_REPRESENTATION_VERSION`` 即可整体换代表述方式。

未命中任何注册时，退回注册表的基础渲染器（默认 :class:`TextRenderer`），
保证“总有东西可渲染”。
"""

from __future__ import annotations

import dataclasses
import typing as t

from .base import DEFAULT_ERROR_VERSION
from .renderers import HTMLRenderer, JSONRenderer, TextRenderer
from .security import SecurityLevel

if t.TYPE_CHECKING:  # pragma: no cover
    from .base import BaseRenderer

#: 已发布的错误表示版本集合。
ERROR_REPRESENTATION_VERSIONS = frozenset({"1.0"})

_SPEC_MATCH = 2
_WILDCARD_MATCH = 1


@dataclasses.dataclass(frozen=True)
class RendererRegistration:
    """一条渲染器注册记录。"""

    version: str
    exception_type: type[BaseException]
    route: str | None
    media_type: str | None
    security: SecurityLevel | None
    renderer: type["BaseRenderer"]


class ErrorRepresentationRegistry:
    """按异常层级 / 路由 / 媒体类型 / 安全级别索引渲染器的注册表。"""

    def __init__(
        self,
        base: type["BaseRenderer"] = TextRenderer,
        version: str = DEFAULT_ERROR_VERSION,
    ):
        self.base = base
        self.version = version
        self._entries: list[RendererRegistration] = []
        # 解析结果缓存：注册表内容变化（register/unregister）时清空。
        self._cache: dict[
            tuple[type[BaseException], str | None, str, SecurityLevel, str],
            type["BaseRenderer"],
        ] = {}
        self._install_defaults()

    # -- 注册 / 注销 -----------------------------------------------------

    def register(
        self,
        renderer: type["BaseRenderer"],
        *,
        exception_type: type[BaseException] = Exception,
        route: str | None = None,
        media_type: str | None = None,
        security: SecurityLevel | None = None,
        version: str | None = None,
    ) -> RendererRegistration:
        """注册一个渲染器。

        各维度省略（``None``）表示通配；越具体的注册优先级越高。
        ``route`` 可传路由名（蓝图路由使用其完整限定名）。
        """
        entry = RendererRegistration(
            version=version or self.version,
            exception_type=exception_type,
            route=route,
            media_type=media_type,
            security=security,
            renderer=renderer,
        )
        self._entries.append(entry)
        self._cache.clear()
        return entry

    def unregister(self, entry: RendererRegistration) -> None:
        """注销先前注册（内置默认项也可被移除）。"""
        self._entries.remove(entry)
        self._cache.clear()

    def for_version(self, version: str) -> "VersionedRegistryView":
        """返回某个表示版本的注册视图。"""
        return VersionedRegistryView(self, version)

    # -- 解析 ------------------------------------------------------------

    def resolve(
        self,
        exception_type: type[BaseException],
        route_name: str | None,
        media_type: str,
        security: SecurityLevel,
        version: str | None = None,
    ) -> type["BaseRenderer"]:
        """按四个维度选择渲染器，找不到时返回基础渲染器。"""
        version = version or self.version
        cache_key = (exception_type, route_name, media_type, security, version)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        entries = [e for e in self._entries if e.version == version]
        for ancestor in type.mro(exception_type):
            best: tuple[int, int, int, type["BaseRenderer"]] | None = None
            for entry in entries:
                if entry.exception_type is not ancestor:
                    continue
                route_score = self._score(entry.route, route_name)
                if not route_score:
                    continue
                media_score = self._score(entry.media_type, media_type)
                if not media_score:
                    continue
                security_score = self._score_security(entry.security, security)
                if not security_score:
                    continue
                score = (route_score, media_score, security_score)
                # 同分以后注册者优先：允许应用用相同维度覆盖内置默认
                # 表示；维度更具体的注册不受顺序影响，总是胜出。
                if best is None or score >= best[:3]:
                    best = (*score, entry.renderer)
            if best is not None:
                renderer = best[3]
                self._cache[cache_key] = renderer
                return renderer
            if ancestor is BaseException:
                break

        self._cache[cache_key] = self.base
        return self.base

    @staticmethod
    def _score(expected: str | None, actual: str | None) -> int:
        if expected is None:
            return _WILDCARD_MATCH
        if actual is not None and expected == actual:
            return _SPEC_MATCH
        return 0

    @staticmethod
    def _score_security(
        expected: SecurityLevel | None, actual: SecurityLevel
    ) -> int:
        if expected is None:
            return _WILDCARD_MATCH
        if expected is actual:
            return _SPEC_MATCH
        return 0

    # -- 内置默认表示（v1.0） --------------------------------------------

    def _install_defaults(self) -> None:
        defaults = {
            "text/plain": TextRenderer,
            "application/json": JSONRenderer,
            # 浏览器表单 / 直接访问时给出可读 HTML。
            "multipart/form-data": HTMLRenderer,
            "text/html": HTMLRenderer,
        }
        for media_type, renderer in defaults.items():
            self.register(
                renderer,
                exception_type=Exception,
                media_type=media_type,
                version=DEFAULT_ERROR_VERSION,
            )

    @property
    def content_type_map(self) -> dict[str, type["BaseRenderer"]]:
        """兼容视图：MIME → 当前版本全局注册的渲染器。"""
        return {
            e.media_type: e.renderer
            for e in self._entries
            if e.version == self.version
            and e.route is None
            and e.security is None
            and e.exception_type is Exception
            and e.media_type is not None
        }


class VersionedRegistryView:
    """固定版本号的注册表便捷视图。"""

    def __init__(self, registry: ErrorRepresentationRegistry, version: str):
        self.registry = registry
        self.version = version

    def register(self, renderer: type["BaseRenderer"], **kwargs: t.Any):
        return self.registry.register(renderer, version=self.version, **kwargs)

    def resolve(
        self,
        exception_type: type[BaseException],
        route_name: str | None,
        media_type: str,
        security: SecurityLevel,
    ) -> type["BaseRenderer"]:
        return self.registry.resolve(
            exception_type, route_name, media_type, security, self.version
        )
