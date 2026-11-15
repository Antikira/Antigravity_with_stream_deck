"""Antigravity session monitor package."""

from src.antigravity_monitor.extensibility_contracts import (
    HubStatusTheme,
    HubStatusThemeRegistry,
    KeyActionContext,
    KeyActionHandlerProtocol,
    KeyActionRegistry,
    KeySvgRenderer,
    KeySvgRendererRegistry,
    QuotaSourceProtocol,
    SessionFilterAndSortRegistry,
    SessionFilterRuleProtocol,
    SessionSourceProtocol,
    SessionStatusContext,
    SessionStatusRuleProtocol,
    SessionTitleContext,
    SortKeyExtractor,
    TitleResolverRuleProtocol,
    WindowFocuserProtocol,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
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
