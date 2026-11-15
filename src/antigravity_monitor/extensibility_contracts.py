"""Extensibility contracts, protocols, and registries for Antigravity Monitor.

Defines Dependency Inversion Principle (DIP) protocols and Open-Closed Principle (OCP)
extension registries shared across session detection, configuration, UI rendering,
key layout management, and the Stream Deck bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class SessionSourceProtocol(Protocol):
    """Protocol for session data sources used by collectors and page managers."""

    def list_sessions(
        self,
        limit: int = 20,
        only_main_conversations: bool = True,
    ) -> list[Any]:
        """Return a list of detected sessions."""
        ...


@runtime_checkable
class QuotaSourceProtocol(Protocol):
    """Protocol for quota/rate-limit providers."""

    def get_quota_stats(self, force: bool = False) -> dict[str, Any]:
        """Fetch quota stats, optionally bypassing cache."""
        ...

    def get_cached_stats(self) -> dict[str, Any]:
        """Return currently cached quota stats without blocking I/O."""
        ...


@runtime_checkable
class WindowFocuserProtocol(Protocol):
    """Protocol for bringing a session's host window to the foreground."""

    def focus_session(
        self,
        workspace_uris: list[str] | None = None,
        title: str = "",
        fallback_keywords: list[str] | None = None,
    ) -> bool:
        """Focus the window matching the given session metadata."""
        ...


@dataclass(frozen=True)
class SessionStatusContext:
    """Immutable evaluation context for session status classification rules."""

    db_status: str
    not_fully_idle: int | bool
    last_transcript_step: dict[str, Any]
    conversation_id: str = ""
    is_running: bool = False
    conversation_db_step: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SessionStatusRuleProtocol(Protocol):
    """Rule evaluating session execution context into a status or None to defer."""

    def evaluate(self, context: SessionStatusContext) -> Any | None:
        """Return a status value if matched, or None to fall through to next rule."""
        ...


@dataclass(frozen=True)
class SessionTitleContext:
    """Immutable context for resolving a displayable session title."""

    title: str = ""
    preview: str = ""
    workspace_uris: list[str] = field(default_factory=list)
    first_user_prompt: str = ""
    conversation_id: str = ""


@runtime_checkable
class TitleResolverRuleProtocol(Protocol):
    """Rule attempting to resolve a session title or returning None to defer."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        """Return a non-empty resolved title string, or None to defer."""
        ...


@runtime_checkable
class SessionFilterRuleProtocol(Protocol):
    """Rule deciding session visibility: True=show, False=hide, None=continue."""

    def check_visibility(self, session: Any, config: Any, now: datetime) -> bool | None:
        """Evaluate visibility decision for a session."""
        ...


SortKeyExtractor = Callable[[Any, bool], Any]


class SessionFilterAndSortRegistry:
    """Registry holding pluggable session visibility filter rules and sort extractors."""

    def __init__(self) -> None:
        self._filter_rules: list[SessionFilterRuleProtocol] = []
        self._sort_extractors: dict[str, SortKeyExtractor] = {}

    def register_filter_rule(
        self,
        rule: SessionFilterRuleProtocol,
        prepend: bool = False,
    ) -> None:
        """Register a visibility filter rule at the front or back of the pipeline."""
        if prepend:
            self._filter_rules.insert(0, rule)
        else:
            self._filter_rules.append(rule)

    def get_filter_rules(self) -> list[SessionFilterRuleProtocol]:
        """Return ordered copy of registered filter rules."""
        return list(self._filter_rules)

    def register_sort_field(self, field_name: str, extractor: SortKeyExtractor) -> None:
        """Register or replace a sort key extractor for the given field name."""
        cleaned = field_name.strip()
        if not cleaned:
            raise ValueError("field_name must be a non-empty string")
        self._sort_extractors[cleaned] = extractor

    def get_sort_extractor(self, field_name: str) -> SortKeyExtractor | None:
        """Look up the sort key extractor for a field name."""
        return self._sort_extractors.get(field_name)

    def valid_sort_fields(self) -> set[str]:
        """Return set of all registered sort field names."""
        return set(self._sort_extractors.keys())


@dataclass(frozen=True)
class HubStatusTheme:
    """SVG theme specification for a Summary Hub status state."""

    status_key: str
    gradient_id: str
    gradient_start: str
    gradient_end: str
    label: str
    graphics_builder: Callable[[], str]
    border_opacity: str = "0.2"


class HubStatusThemeRegistry:
    """Registry mapping normalized Summary Hub status keys to HubStatusTheme objects."""

    def __init__(self) -> None:
        self._themes: dict[str, HubStatusTheme] = {}

    def register(self, theme: HubStatusTheme) -> None:
        """Register or replace a theme under its normalized status_key."""
        normalized = theme.status_key.lower().strip()
        if not normalized:
            raise ValueError("theme.status_key must be a non-empty string")
        self._themes[normalized] = theme

    def get(self, status_key: str) -> HubStatusTheme:
        """Retrieve theme for status_key (normalized), falling back to 'empty' theme."""
        normalized = status_key.lower().strip() if isinstance(status_key, str) else ""
        if normalized in self._themes:
            return self._themes[normalized]
        if "empty" in self._themes:
            return self._themes["empty"]
        if "idle" in self._themes:
            return self._themes["idle"]
        raise KeyError(f"Unknown hub status theme: {status_key!r}")

    def list_statuses(self) -> list[str]:
        """Return all registered normalized status keys."""
        return list(self._themes.keys())


KeySvgRenderer = Callable[[Any], str]


class KeySvgRendererRegistry:
    """Registry dispatching key SVG rendering by exact key_id, prefix, or default."""

    def __init__(self) -> None:
        self._exact_renderers: dict[str, KeySvgRenderer] = {}
        self._prefix_renderers: dict[str, KeySvgRenderer] = {}
        self._default_renderer: KeySvgRenderer | None = None

    def register(
        self,
        key_id_or_prefix: str,
        renderer: KeySvgRenderer,
        prefix_match: bool = False,
    ) -> None:
        """Register an SVG renderer for an exact key_id or key_id prefix."""
        if not key_id_or_prefix:
            raise ValueError("key_id_or_prefix must be a non-empty string")
        if prefix_match:
            self._prefix_renderers[key_id_or_prefix] = renderer
        else:
            self._exact_renderers[key_id_or_prefix] = renderer

    def set_default_renderer(self, renderer: KeySvgRenderer) -> None:
        """Set the fallback SVG renderer used when no key_id or prefix matches."""
        self._default_renderer = renderer

    def render(self, key_data: Any) -> str:
        """Render key_data to an SVG string using the most specific matching renderer."""
        key_id = getattr(key_data, "key_id", "")
        if key_id in self._exact_renderers:
            return self._exact_renderers[key_id](key_data)

        for prefix, renderer in sorted(
            self._prefix_renderers.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if key_id.startswith(prefix):
                return renderer(key_data)

        if self._default_renderer is not None:
            return self._default_renderer(key_data)

        raise LookupError(f"No SVG renderer registered for key_id={key_id!r}")


@dataclass
class KeyActionContext:
    """Context passed to KeyActionHandlerProtocol during rendering and key press."""

    page_manager: Any
    collector: Any | None = None
    window_focuser: WindowFocuserProtocol | None = None
    action_id: str = ""
    suffix: str = ""
    col: int = 0
    row: int = 0
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.settings is None:
            self.settings = {}
        if not self.suffix and self.action_id:
            self.suffix = self.action_id.split(".")[-1]
        elif not self.action_id and self.suffix:
            self.action_id = self.suffix


@runtime_checkable
class KeyActionHandlerProtocol(Protocol):
    """Protocol for Stream Deck key builders and press handlers."""

    def matches(self, suffix: str) -> bool:
        """Return True if this handler owns the given action suffix."""
        ...

    def build_key_data(self, context: KeyActionContext) -> Any | None:
        """Build render data for the matching key, or None if unavailable."""
        ...

    def on_press(self, context: KeyActionContext) -> dict[str, Any]:
        """Handle key press event and return a structured result dictionary."""
        ...


class KeyActionRegistry:
    """Registry resolving and dispatching Stream Deck key data builders and actions."""

    def __init__(self) -> None:
        self._handlers: list[KeyActionHandlerProtocol] = []

    def register(
        self,
        handler: KeyActionHandlerProtocol,
        prepend: bool = False,
    ) -> None:
        """Register a key action handler at the front or back of resolution order."""
        if prepend:
            self._handlers.insert(0, handler)
        else:
            self._handlers.append(handler)

    def resolve_handler(self, action_id_or_suffix: str) -> KeyActionHandlerProtocol | None:
        """Return the first handler matching the action ID or extracted suffix."""
        suffix = action_id_or_suffix.split(".")[-1] if action_id_or_suffix else ""
        for handler in self._handlers:
            if handler.matches(suffix):
                return handler
        return None

    def build_key_data(self, context: KeyActionContext) -> Any | None:
        """Resolve a handler from context and build its KeyRenderData."""
        target = context.suffix or context.action_id
        handler = self.resolve_handler(target)
        if handler is None:
            return None
        return handler.build_key_data(context)

    def dispatch_press(self, context: KeyActionContext) -> dict[str, Any]:
        """Resolve a handler from context and dispatch its on_press action."""
        target = context.suffix or context.action_id
        handler = self.resolve_handler(target)
        if handler is None:
            return {"status": "unhandled", "action": target}
        return handler.on_press(context)


__all__ = [
    "HubStatusTheme",
    "HubStatusThemeRegistry",
    "KeyActionContext",
    "KeyActionHandlerProtocol",
    "KeyActionRegistry",
    "KeySvgRenderer",
    "KeySvgRendererRegistry",
    "QuotaSourceProtocol",
    "SessionFilterAndSortRegistry",
    "SessionFilterRuleProtocol",
    "SessionSourceProtocol",
    "SessionStatusContext",
    "SessionStatusRuleProtocol",
    "SessionTitleContext",
    "SortKeyExtractor",
    "TitleResolverRuleProtocol",
    "WindowFocuserProtocol",
]
