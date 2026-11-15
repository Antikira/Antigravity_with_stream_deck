"""OCP and DIP extensibility verification tests for Antigravity Session Monitor.

Verifies that behavior can be extended or swapped via protocols and registries
defined in ``extensibility_contracts.py`` without modifying existing core classes.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import src.antigravity_monitor as monitor_pkg
from src.antigravity_monitor import (
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
from src.antigravity_monitor import extensibility_contracts as contracts
from src.antigravity_monitor.collector import DataCollector
from src.antigravity_monitor.config import (
    ConversationListConfig,
    HubConfig,
    create_default_filter_sort_registry,
    filter_and_sort_sessions,
    is_session_visible,
    sort_sessions,
)
from src.antigravity_monitor.image_generator import (
    DEFAULT_HUB_THEME_REGISTRY,
    DEFAULT_KEY_SVG_RENDERER_REGISTRY,
    _build_hub_svg,
    _get_done_graphics,
    _get_ready_graphics,
    _get_waiting_graphics,
    _get_working_graphics,
    generate_hub_rocket_svg,
    generate_key_svg,
    render_key_svg,
)
from src.antigravity_monitor.quota_reader import QuotaInfo
from src.antigravity_monitor.session_detector import (
    SessionDetector,
    SessionInfo,
    SessionStatus,
)
from src.antigravity_monitor.session_page_manager import KeyRenderData, SessionPageManager
from src.antigravity_monitor.state_store import StateStore
from src.antigravity_monitor.streamdeck_bridge import SimulatorHTTPHandler, StreamDeckBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_summary_db(base_dir: Path, rows: list[tuple[Any, ...]]) -> None:
    """Create a temporary conversation_summaries.db populated with rows."""
    base_dir.mkdir(parents=True, exist_ok=True)
    db_path = base_dir / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            workspace_uris TEXT,
            status TEXT,
            parent_conversation_id TEXT,
            not_fully_idle INTEGER
        )
        """
    )
    cur.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _clone_default_hub_theme_registry() -> HubStatusThemeRegistry:
    """Copy built-in Hub themes into an isolated registry for safe testing."""
    registry = HubStatusThemeRegistry()
    for status_key in DEFAULT_HUB_THEME_REGISTRY.list_statuses():
        registry.register(DEFAULT_HUB_THEME_REGISTRY.get(status_key))
    return registry


def _make_simulator_handler() -> tuple[SimulatorHTTPHandler, list[int], dict[str, str]]:
    """Build a lightweight SimulatorHTTPHandler instance without a real socket."""
    handler = SimulatorHTTPHandler.__new__(SimulatorHTTPHandler)
    statuses: list[int] = []
    headers: dict[str, str] = {}
    handler.wfile = io.BytesIO()
    handler.address_string = lambda: "127.0.0.1"
    handler.send_response = lambda code, message=None: statuses.append(code)
    handler.send_header = lambda key, val: headers.__setitem__(key, val)
    handler.end_headers = lambda: None
    return handler, statuses, headers


# ---------------------------------------------------------------------------
# Package Exports
# ---------------------------------------------------------------------------


def test_package_reexports_all_extensibility_contracts() -> None:
    """Verify __init__.py re-exports every symbol defined in extensibility_contracts."""
    expected_symbols = {
        "HubStatusTheme": HubStatusTheme,
        "HubStatusThemeRegistry": HubStatusThemeRegistry,
        "KeyActionContext": KeyActionContext,
        "KeyActionHandlerProtocol": KeyActionHandlerProtocol,
        "KeyActionRegistry": KeyActionRegistry,
        "KeySvgRenderer": KeySvgRenderer,
        "KeySvgRendererRegistry": KeySvgRendererRegistry,
        "QuotaSourceProtocol": QuotaSourceProtocol,
        "SessionFilterAndSortRegistry": SessionFilterAndSortRegistry,
        "SessionFilterRuleProtocol": SessionFilterRuleProtocol,
        "SessionSourceProtocol": SessionSourceProtocol,
        "SessionStatusContext": SessionStatusContext,
        "SessionStatusRuleProtocol": SessionStatusRuleProtocol,
        "SessionTitleContext": SessionTitleContext,
        "SortKeyExtractor": SortKeyExtractor,
        "TitleResolverRuleProtocol": TitleResolverRuleProtocol,
        "WindowFocuserProtocol": WindowFocuserProtocol,
    }
    for name, obj in expected_symbols.items():
        assert name in monitor_pkg.__all__
        assert getattr(monitor_pkg, name) is obj
        assert getattr(contracts, name) is obj


# ---------------------------------------------------------------------------
# 1. SessionStatusRuleProtocol and TitleResolverRuleProtocol
# ---------------------------------------------------------------------------


def test_custom_status_rule_registered_on_session_detector(tmp_path: Path) -> None:
    """Custom SessionStatusRuleProtocol extends SessionDetector without editing core code."""
    seen_contexts: list[SessionStatusContext] = []

    class CustomBlockedStatusRule:
        def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
            seen_contexts.append(context)
            if context.last_transcript_step.get("custom_gate") is True:
                return SessionStatus.WAITING_FOR_APPROVAL
            return None

    rule = CustomBlockedStatusRule()
    assert isinstance(rule, SessionStatusRuleProtocol)

    detector = SessionDetector(base_dir=tmp_path, ignore_preexisting_done=False)
    detector.register_status_rule(rule, prepend=True)

    # Custom rule matches and overrides status
    overridden = detector.determine_status(
        db_status="CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=1,
        last_transcript_step={"status": "DONE", "custom_gate": True},
        conversation_id="cid-custom-1",
    )
    assert overridden == SessionStatus.WAITING_FOR_APPROVAL
    assert len(seen_contexts) == 1
    assert seen_contexts[0].is_running is True
    assert seen_contexts[0].conversation_id == "cid-custom-1"
    assert seen_contexts[0].db_status == "CASCADE_RUN_STATUS_RUNNING"

    # Returning None defers to built-in RunningStatusRule
    deferred_running = detector.determine_status(
        db_status="CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=1,
        last_transcript_step={"status": "DONE"},
    )
    assert deferred_running == SessionStatus.WORKING

    # Constructor injection with a custom fallback rule (prepend=False equivalent)
    class FallbackWorkingRule:
        def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
            if context.last_transcript_step.get("soft_active"):
                return SessionStatus.WORKING
            return None

    custom_detector = SessionDetector(base_dir=tmp_path, status_rules=[])
    custom_detector.register_status_rule(FallbackWorkingRule(), prepend=False)
    assert (
        custom_detector.determine_status(
            db_status="IDLE",
            not_fully_idle=0,
            last_transcript_step={"soft_active": True},
        )
        == SessionStatus.WORKING
    )
    # When all rules return None, determine_status falls back to SessionStatus.DONE
    assert (
        custom_detector.determine_status(
            db_status="IDLE",
            not_fully_idle=0,
            last_transcript_step={},
        )
        == SessionStatus.DONE
    )


def test_custom_title_resolver_registered_on_session_detector(tmp_path: Path) -> None:
    """Custom TitleResolverRuleProtocol affects list_sessions and SessionInfo.short_title."""
    now_iso = datetime.now(timezone.utc).isoformat()
    _create_summary_db(
        tmp_path,
        [
            (
                "cid-tagged-01",
                "Original Long Title For Testing",
                "",
                12,
                now_iso,
                json.dumps(["file:///D:/repo/alpha_service"]),
                "CASCADE_RUN_STATUS_RUNNING",
                "",
                1,
            ),
            (
                "cid-plain-02",
                "",
                "",
                3,
                now_iso,
                json.dumps(["file:///D:/repo/beta_app"]),
                "CASCADE_RUN_STATUS_IDLE",
                "",
                0,
            ),
        ],
    )

    class AlphaPrefixResolver:
        def resolve(self, context: SessionTitleContext) -> str | None:
            if any("alpha_service" in uri for uri in context.workspace_uris):
                return "AlphaCustomTitleTag"
            return None

    resolver = AlphaPrefixResolver()
    assert isinstance(resolver, TitleResolverRuleProtocol)

    detector = SessionDetector(base_dir=tmp_path, ignore_preexisting_done=False)
    detector.register_title_resolver(resolver, prepend=True)

    sessions = {s.conversation_id: s for s in detector.list_sessions(limit=10)}
    assert sessions["cid-tagged-01"].title == "AlphaCustomTitleTag"
    assert sessions["cid-tagged-01"].short_title == "AlphaCustom…"
    # Deferring (returning None) lets built-in WorkspaceUriTitleRule resolve beta_app
    assert sessions["cid-plain-02"].title == "beta_app"
    assert sessions["cid-plain-02"].short_title == "beta_app"

    # When custom pipeline returns None for every resolver, fallback is "Session"
    empty_resolver_detector = SessionDetector(
        base_dir=tmp_path,
        ignore_preexisting_done=False,
        title_resolvers=[],
    )
    fallback_sessions = empty_resolver_detector.list_sessions(limit=10)
    assert all(s.title == "Session" for s in fallback_sessions)


# ---------------------------------------------------------------------------
# 2. SessionFilterRuleProtocol and Custom Sort Field Registry
# ---------------------------------------------------------------------------


def test_custom_filter_rule_on_registry() -> None:
    """Custom SessionFilterRuleProtocol can protect, hide, or defer sessions."""

    class PrefixVisibilityRule:
        def check_visibility(
            self,
            session: SessionInfo,
            config: ConversationListConfig,
            now: datetime,
        ) -> bool | None:
            if session.title.startswith("[PIN]"):
                return True
            if session.title.startswith("[HIDE]"):
                return False
            return None

    rule = PrefixVisibilityRule()
    assert isinstance(rule, SessionFilterRuleProtocol)

    registry = create_default_filter_sort_registry()
    registry.register_filter_rule(rule, prepend=True)

    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=30)).isoformat()
    pinned_old_done = SessionInfo(
        conversation_id="s-pin",
        title="[PIN] Old Done",
        status=SessionStatus.DONE,
        updated_at=old_iso,
    )
    hidden_working = SessionInfo(
        conversation_id="s-hide",
        title="[HIDE] Active",
        status=SessionStatus.WORKING,
        updated_at=now.isoformat(),
    )
    normal_old_done = SessionInfo(
        conversation_id="s-drop",
        title="Normal Old Done",
        status=SessionStatus.DONE,
        updated_at=old_iso,
    )

    cfg = ConversationListConfig(done_retention_minutes=0, max_inactivity_days=1)

    assert is_session_visible(pinned_old_done, cfg, now=now, registry=registry) is True
    assert is_session_visible(hidden_working, cfg, now=now, registry=registry) is False
    assert is_session_visible(normal_old_done, cfg, now=now, registry=registry) is False

    kept = filter_and_sort_sessions(
        [pinned_old_done, hidden_working, normal_old_done],
        cfg,
        now=now,
        registry=registry,
    )
    assert [s.conversation_id for s in kept] == ["s-pin"]


def test_custom_sort_field_registration_and_validation() -> None:
    """Custom sort field is validated in configs and used by sort_sessions."""
    registry = create_default_filter_sort_registry()

    def step_count_extractor(session: SessionInfo, ascending: bool) -> int:
        return session.step_count if ascending else -session.step_count

    registry.register_sort_field("step_count", step_count_extractor)
    assert "step_count" in registry.valid_sort_fields()

    with pytest.raises(ValueError):
        registry.register_sort_field("   ", step_count_extractor)

    # Custom registry accepts 'step_count' in HubConfig and ConversationListConfig
    hub_cfg = HubConfig.from_settings(
        {"hub_sort_criteria": [{"field": "step_count", "ascending": False}]},
        registry=registry,
    )
    assert hub_cfg.hub_sort_criteria == [{"field": "step_count", "ascending": False}]

    list_cfg = ConversationListConfig.from_dict(
        {"sort_criteria": [{"field": "step_count", "ascending": True}]},
        registry=registry,
    )
    assert list_cfg.sort_criteria == [{"field": "step_count", "ascending": True}]

    # Default registry does NOT include 'step_count' and falls back safely
    hub_cfg_default = HubConfig.from_settings(
        {"hub_sort_criteria": [{"field": "step_count", "ascending": False}]}
    )
    assert all(c["field"] != "step_count" for c in hub_cfg_default.hub_sort_criteria)

    now_iso = datetime.now(timezone.utc).isoformat()
    s_low = SessionInfo("c1", "Low", SessionStatus.DONE, step_count=5, updated_at=now_iso)
    s_mid = SessionInfo("c2", "Mid", SessionStatus.DONE, step_count=20, updated_at=now_iso)
    s_high = SessionInfo("c3", "High", SessionStatus.DONE, step_count=99, updated_at=now_iso)

    sorted_asc = sort_sessions([s_high, s_low, s_mid], list_cfg.sort_criteria, registry=registry)
    assert [s.conversation_id for s in sorted_asc] == ["c1", "c2", "c3"]

    sorted_desc = filter_and_sort_sessions(
        [s_low, s_high, s_mid],
        ConversationListConfig.from_dict(
            {
                "sort_criteria": [{"field": "step_count", "ascending": False}],
                "done_retention_minutes": -1,
                "max_inactivity_days": 0,
            },
            registry=registry,
        ),
        registry=registry,
    )
    assert [s.conversation_id for s in sorted_desc] == ["c3", "c2", "c1"]


# ---------------------------------------------------------------------------
# 3. HubStatusThemeRegistry and KeySvgRendererRegistry
# ---------------------------------------------------------------------------


def test_custom_hub_status_theme_and_border_opacity_preservation() -> None:
    """Custom HubStatusTheme renders via registry while preserving exact built-in SVGs."""
    # Exact byte-compatibility / border opacity checks on built-in themes
    assert DEFAULT_HUB_THEME_REGISTRY.get("empty").border_opacity == "0.15"
    assert DEFAULT_HUB_THEME_REGISTRY.get("idle").border_opacity == "0.15"
    assert DEFAULT_HUB_THEME_REGISTRY.get("working").border_opacity == "0.2"
    assert DEFAULT_HUB_THEME_REGISTRY.get("done").border_opacity == "0.2"
    assert DEFAULT_HUB_THEME_REGISTRY.get("waiting").border_opacity == "0.2"

    expected_empty = _build_hub_svg(
        gradient_id="bgIdle",
        gradient_start="#37474F",
        gradient_end="#212121",
        label="READY",
        graphics=_get_ready_graphics(),
        border_opacity="0.15",
    )
    assert generate_hub_rocket_svg("empty") == expected_empty
    assert generate_hub_rocket_svg("idle") == expected_empty
    assert generate_hub_rocket_svg("working") == _build_hub_svg(
        gradient_id="bgWorking",
        gradient_start="#1565C0",
        gradient_end="#0D47A1",
        label="WORKING",
        graphics=_get_working_graphics(),
        border_opacity="0.2",
    )
    assert generate_hub_rocket_svg("done") == _build_hub_svg(
        gradient_id="bgDone",
        gradient_start="#2E7D32",
        gradient_end="#1B5E20",
        label="DONE",
        graphics=_get_done_graphics(),
        border_opacity="0.2",
    )
    assert generate_hub_rocket_svg("waiting") == _build_hub_svg(
        gradient_id="bgWaiting",
        gradient_start="#EF6C00",
        gradient_end="#D84315",
        label="WAITING",
        graphics=_get_waiting_graphics(),
        border_opacity="0.2",
    )

    # Custom theme registration in an isolated registry
    registry = _clone_default_hub_theme_registry()
    registry.register(
        HubStatusTheme(
            status_key="maintenance",
            gradient_id="bgMaint",
            gradient_start="#6A1B9A",
            gradient_end="#4A148C",
            label="MAINT",
            graphics_builder=lambda: '  <circle cx="72" cy="56" r="18" fill="#FFFFFF" />',
            border_opacity="0.35",
        )
    )
    assert "maintenance" in registry.list_statuses()

    custom_svg = generate_hub_rocket_svg("  MAINTENANCE ", theme_registry=registry)
    assert 'id="bgMaint"' in custom_svg
    assert 'stop-color="#6A1B9A"' in custom_svg
    assert 'opacity="0.35"' in custom_svg
    assert ">MAINT</text>" in custom_svg
    assert '<circle cx="72" cy="56" r="18" fill="#FFFFFF" />' in custom_svg

    # Unknown status falls back to 'empty' theme
    assert generate_hub_rocket_svg("unknown_status", theme_registry=registry) == expected_empty


def test_custom_key_svg_renderer_registry() -> None:
    """KeySvgRendererRegistry dispatches exact, longest-prefix, and default renderers."""
    empty_registry = KeySvgRendererRegistry()
    sample = KeyRenderData(key_id="unmatched_key", column=0, row=0, title="X")
    with pytest.raises(LookupError):
        empty_registry.render(sample)

    registry = KeySvgRendererRegistry()
    registry.set_default_renderer(lambda k: f"<svg>DEFAULT:{k.key_id}</svg>")
    registry.register("custom_", lambda k: f"<svg>PREFIX_SHORT:{k.key_id}</svg>", prefix_match=True)
    registry.register(
        "custom_vip_",
        lambda k: f"<svg>PREFIX_LONG:{k.key_id}</svg>",
        prefix_match=True,
    )
    registry.register("custom_vip_0", lambda k: f"<svg>EXACT:{k.key_id}</svg>")

    assert (
        render_key_svg(
            KeyRenderData(key_id="custom_vip_0", column=0, row=0, title="A"),
            renderer_registry=registry,
        )
        == "<svg>EXACT:custom_vip_0</svg>"
    )
    assert (
        render_key_svg(
            KeyRenderData(key_id="custom_vip_1", column=0, row=0, title="B"),
            renderer_registry=registry,
        )
        == "<svg>PREFIX_LONG:custom_vip_1</svg>"
    )
    assert (
        render_key_svg(
            KeyRenderData(key_id="custom_other", column=0, row=0, title="C"),
            renderer_registry=registry,
        )
        == "<svg>PREFIX_SHORT:custom_other</svg>"
    )
    assert (
        render_key_svg(
            KeyRenderData(key_id="session_slot_0", column=1, row=0, title="D"),
            renderer_registry=registry,
        )
        == "<svg>DEFAULT:session_slot_0</svg>"
    )

    # Built-in default registry parity check
    hub_key = KeyRenderData(key_id="summary_hub", column=0, row=2, title="Hub", status="working")
    assert render_key_svg(hub_key) == generate_hub_rocket_svg("working")

    slot_key = KeyRenderData(
        key_id="session_slot_0",
        column=1,
        row=0,
        title="S1",
        status="working",
        bg_color="#1E88E5",
    )
    assert render_key_svg(slot_key) == generate_key_svg(
        title="S1",
        subtitle="",
        status_label="working",
        bg_color="#1E88E5",
        is_active=True,
    )


# ---------------------------------------------------------------------------
# 4. KeyActionHandlerProtocol across PageManager, Bridge, and Simulator
# ---------------------------------------------------------------------------


def test_custom_key_action_handler_on_page_manager() -> None:
    """Custom KeyActionHandlerProtocol works with get_key_data_by_action_id and dispatch."""
    received_contexts: list[KeyActionContext] = []

    class CustomPingHandler:
        def matches(self, suffix: str) -> bool:
            return suffix == "custom_ping"

        def build_key_data(self, context: KeyActionContext) -> KeyRenderData | None:
            return KeyRenderData(
                key_id="custom_ping",
                column=context.col,
                row=context.row,
                title=f"PING:{context.settings.get('label', 'OK')}",
                subtitle="CUSTOM",
                status="custom",
                bg_color="#5E35B1",
            )

        def on_press(self, context: KeyActionContext) -> dict[str, Any]:
            received_contexts.append(context)
            focused = False
            if context.window_focuser is not None:
                focused = context.window_focuser.focus_session(title="CustomTarget")
            return {
                "status": "ok",
                "action": context.suffix,
                "focused": focused,
                "refresh_ui": True,
                "custom_tag": "pong",
            }

    handler = CustomPingHandler()
    assert isinstance(handler, KeyActionHandlerProtocol)

    class DummyFocuser:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def focus_session(
            self,
            workspace_uris: list[str] | None = None,
            title: str = "",
            fallback_keywords: list[str] | None = None,
        ) -> bool:
            self.calls.append(title)
            return True

    focuser = DummyFocuser()
    dummy_collector = object()

    page_mgr = SessionPageManager()
    page_mgr.register_key_handler(handler, prepend=True)

    key_data = page_mgr.get_key_data_by_action_id(
        "com.user.antigravity.custom_ping",
        col=2,
        row=2,
        settings={"label": "HI"},
    )
    assert key_data is not None
    assert key_data.key_id == "custom_ping"
    assert key_data.column == 2
    assert key_data.row == 2
    assert key_data.title == "PING:HI"

    dispatched = page_mgr.dispatch_key_action(
        "com.user.antigravity.custom_ping",
        col=2,
        row=2,
        settings={"label": "HI"},
        collector=dummy_collector,
        window_focuser=focuser,
    )
    assert dispatched == {
        "status": "ok",
        "action": "custom_ping",
        "focused": True,
        "refresh_ui": True,
        "custom_tag": "pong",
    }
    assert focuser.calls == ["CustomTarget"]
    assert len(received_contexts) == 1
    assert received_contexts[0].collector is dummy_collector
    assert received_contexts[0].page_manager is page_mgr

    # Unhandled actions return None for key data and status='unhandled' on press
    assert page_mgr.get_key_data_by_action_id("com.user.antigravity.no_such_action") is None
    assert page_mgr.dispatch_key_action("com.user.antigravity.no_such_action") == {
        "status": "unhandled",
        "action": "no_such_action",
    }


def test_bridge_handle_key_down_and_simulator_custom_action() -> None:
    """StreamDeckBridge.handle_key_down and SimulatorHTTPHandler dispatch custom actions."""
    recorded_presses: list[KeyActionContext] = []

    class CustomBridgeHandler:
        def matches(self, suffix: str) -> bool:
            return suffix == "custom_ping"

        def build_key_data(self, context: KeyActionContext) -> KeyRenderData | None:
            return KeyRenderData(
                key_id="custom_ping",
                column=context.col,
                row=context.row,
                title="Ping",
                subtitle="PING",
                status="custom",
                bg_color="#00897B",
            )

        def on_press(self, context: KeyActionContext) -> dict[str, Any]:
            recorded_presses.append(context)
            focused = (
                context.window_focuser.focus_session(title="PingWindow")
                if context.window_focuser is not None
                else False
            )
            return {
                "status": "ok",
                "action": context.suffix,
                "refresh_ui": True,
                "focused": focused,
                "custom_tag": "sim_pong",
            }

    class FakeSessionSource:
        def list_sessions(
            self,
            limit: int = 20,
            only_main_conversations: bool = True,
        ) -> list[SessionInfo]:
            return []

    class FakeQuotaSource:
        def get_quota_stats(self, force: bool = False) -> dict[str, QuotaInfo]:
            return {}

        def get_cached_stats(self) -> dict[str, QuotaInfo]:
            return {}

    class FakeFocuser:
        def __init__(self) -> None:
            self.titles: list[str] = []

        def focus_session(
            self,
            workspace_uris: list[str] | None = None,
            title: str = "",
            fallback_keywords: list[str] | None = None,
        ) -> bool:
            self.titles.append(title)
            return True

    focuser = FakeFocuser()
    store = StateStore()
    page_mgr = SessionPageManager(
        session_detector=FakeSessionSource(),
        quota_reader=FakeQuotaSource(),
        state_store=store,
    )
    page_mgr.register_key_handler(CustomBridgeHandler(), prepend=True)

    svg_registry = KeySvgRendererRegistry()
    svg_registry.set_default_renderer(DEFAULT_KEY_SVG_RENDERER_REGISTRY.render)
    svg_registry.register("custom_ping", lambda k: "<svg><text>CUSTOM_PING_SVG</text></svg>")

    collector = DataCollector(
        state_store=store,
        session_detector=FakeSessionSource(),
        quota_reader=FakeQuotaSource(),
    )
    bridge = StreamDeckBridge(
        port=1234,
        plugin_uuid="uuid-ext",
        register_event="registerPlugin",
        state_store=store,
        collector=collector,
        window_focuser=focuser,
        svg_renderer_registry=svg_registry,
        page_manager=page_mgr,
    )
    bridge.ws = AsyncMock()
    bridge.active_contexts["ctx_ping"] = {
        "action": "com.user.antigravity.custom_ping",
        "coordinates": {"column": 2, "row": 1},
        "settings": {"via": "ws"},
    }

    asyncio.run(
        bridge.handle_key_down(
            context="ctx_ping",
            action="com.user.antigravity.custom_ping",
            coords={"column": 2, "row": 1},
            settings={"via": "ws"},
        )
    )

    assert len(recorded_presses) == 1
    assert recorded_presses[0].collector is collector
    assert recorded_presses[0].window_focuser is focuser
    assert recorded_presses[0].settings == {"via": "ws"}
    assert focuser.titles == ["PingWindow"]

    # refresh_ui=True caused update_all_keys(force=True) using custom svg_renderer_registry
    assert bridge.ws.send.call_count == 2
    sent_image_msg = json.loads(bridge.ws.send.call_args_list[0].args[0])
    assert sent_image_msg["event"] == "setImage"
    decoded_svg = urllib.parse.unquote(sent_image_msg["payload"]["image"])
    assert "CUSTOM_PING_SVG" in decoded_svg

    # Verify SimulatorHTTPHandler.handle_api_press delegates custom actions too
    prev_bridge = SimulatorHTTPHandler.bridge
    SimulatorHTTPHandler.bridge = bridge
    try:
        handler, statuses, _headers = _make_simulator_handler()
        handler.handle_api_press(
            {
                "key_id": "custom_ping",
                "column": 2,
                "row": 1,
                "settings": {"via": "simulator"},
            }
        )
        assert statuses == [200]
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert body["status"] == "ok"
        assert body["action"] == "custom_ping"
        assert body["custom_tag"] == "sim_pong"
        assert body["focused"] is True
        assert len(recorded_presses) == 2
        assert recorded_presses[1].settings == {"via": "simulator"}
        assert recorded_presses[1].collector is collector
        assert recorded_presses[1].window_focuser is focuser
    finally:
        SimulatorHTTPHandler.bridge = prev_bridge


# ---------------------------------------------------------------------------
# 5. SessionSourceProtocol, QuotaSourceProtocol, WindowFocuserProtocol DI
# ---------------------------------------------------------------------------


def test_protocol_injection_into_data_collector() -> None:
    """DataCollector accepts pure Protocol implementations without concrete inheritance."""
    session_called = threading.Event()
    quota_called = threading.Event()

    sample_session = SessionInfo(
        conversation_id="injected-s1",
        title="Injected Session",
        status=SessionStatus.WORKING,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    sample_quota = QuotaInfo(
        window_name="5H",
        label="5H",
        model_group="Gemini",
        remaining_percent=65.0,
    )

    class InjectedSessionSource:
        def list_sessions(
            self,
            limit: int = 20,
            only_main_conversations: bool = True,
        ) -> list[SessionInfo]:
            session_called.set()
            return [sample_session]

    class InjectedQuotaSource:
        def get_quota_stats(self, force: bool = False) -> dict[str, QuotaInfo]:
            quota_called.set()
            return {"gemini_5h": sample_quota}

        def get_cached_stats(self) -> dict[str, QuotaInfo]:
            return {"gemini_5h": sample_quota}

    session_src = InjectedSessionSource()
    quota_src = InjectedQuotaSource()
    assert isinstance(session_src, SessionSourceProtocol)
    assert isinstance(quota_src, QuotaSourceProtocol)

    store = StateStore()
    collector = DataCollector(
        state_store=store,
        session_detector=session_src,
        quota_reader=quota_src,
        session_poll_interval=0.02,
        quota_poll_interval=5.0,
    )

    collector.start()
    try:
        assert session_called.wait(timeout=2.0)
        assert quota_called.wait(timeout=2.0)
        # Allow a tiny moment after event set for StateStore update commit
        deadline = threading.Event()
        for _ in range(20):
            snap = store.get_snapshot()
            if snap.sessions and snap.quotas:
                break
            deadline.wait(0.02)
        snap = store.get_snapshot()
        assert [s.conversation_id for s in snap.sessions] == ["injected-s1"]
        assert snap.quotas["gemini_5h"].remaining_percent == 65.0
    finally:
        collector.stop()


def test_protocol_injection_into_page_manager() -> None:
    """SessionPageManager consumes injected SessionSourceProtocol and QuotaSourceProtocol."""
    sample_session = SessionInfo(
        conversation_id="pm-s1",
        title="PageManager Session",
        status=SessionStatus.WAITING_FOR_APPROVAL,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    sample_quota = QuotaInfo(
        window_name="5H",
        label="5H",
        model_group="Gemini",
        remaining_percent=42.0,
    )

    class FakeSessions:
        def list_sessions(
            self,
            limit: int = 20,
            only_main_conversations: bool = True,
        ) -> list[SessionInfo]:
            return [sample_session]

    class FakeQuotas:
        def __init__(self) -> None:
            self.force_calls: list[bool] = []

        def get_quota_stats(self, force: bool = False) -> dict[str, QuotaInfo]:
            self.force_calls.append(force)
            return {"gemini_5h": sample_quota}

        def get_cached_stats(self) -> dict[str, QuotaInfo]:
            return {"gemini_5h": sample_quota}

    sessions = FakeSessions()
    quotas = FakeQuotas()
    mgr = SessionPageManager(session_detector=sessions, quota_reader=quotas)
    mgr.refresh(include_quota=True, force_quota=True)

    assert quotas.force_calls == [True]
    slots = mgr.get_sessions_for_current_page()
    assert slots[0] is not None
    assert slots[0].conversation_id == "pm-s1"

    q_key = mgr.build_quota_key_data("gemini_5h")
    assert "残42%" in q_key.title


def test_window_focuser_protocol_injection_into_bridge_and_simulator() -> None:
    """StreamDeckBridge and SimulatorHTTPHandler delegate focusing to WindowFocuserProtocol."""
    session = SessionInfo(
        conversation_id="focus-s1",
        title="Focus Target App",
        status=SessionStatus.WAITING_FOR_APPROVAL,
        workspace_uris=["file:///D:/work/focus_target"],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    class FakeSessionSource:
        def list_sessions(
            self,
            limit: int = 20,
            only_main_conversations: bool = True,
        ) -> list[SessionInfo]:
            return [session]

    class FakeQuotaSource:
        def get_quota_stats(self, force: bool = False) -> dict[str, QuotaInfo]:
            return {}

        def get_cached_stats(self) -> dict[str, QuotaInfo]:
            return {}

    class SpyWindowFocuser:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str] | None, str, list[str] | None]] = []

        def focus_session(
            self,
            workspace_uris: list[str] | None = None,
            title: str = "",
            fallback_keywords: list[str] | None = None,
        ) -> bool:
            self.calls.append((workspace_uris, title, fallback_keywords))
            return True

    focuser = SpyWindowFocuser()
    assert isinstance(focuser, WindowFocuserProtocol)

    store = StateStore()
    store.update_sessions([session])
    page_mgr = SessionPageManager(
        session_detector=FakeSessionSource(),
        quota_reader=FakeQuotaSource(),
        state_store=store,
    )
    page_mgr.refresh()

    collector = DataCollector(
        state_store=store,
        session_detector=FakeSessionSource(),
        quota_reader=FakeQuotaSource(),
    )
    bridge = StreamDeckBridge(
        port=1234,
        plugin_uuid="uuid-focus",
        register_event="registerPlugin",
        state_store=store,
        collector=collector,
        window_focuser=focuser,
        page_manager=page_mgr,
    )

    # 1. StreamDeckBridge.handle_key_down for Hub and Session Slot 1
    asyncio.run(
        bridge.handle_key_down(
            context="ctx_hub",
            action="com.user.antigravity.summary_hub",
            coords={"column": 0, "row": 2},
            settings={},
        )
    )
    asyncio.run(
        bridge.handle_key_down(
            context="ctx_slot1",
            action="com.user.antigravity.session_slot_1",
            coords={"column": 1, "row": 0},
            settings={},
        )
    )
    assert focuser.calls == [
        (["file:///D:/work/focus_target"], "Focus Target App", None),
        (["file:///D:/work/focus_target"], "Focus Target App", None),
    ]

    # 2. SimulatorHTTPHandler.handle_api_press for summary_hub and session_slot_0
    prev_bridge = SimulatorHTTPHandler.bridge
    SimulatorHTTPHandler.bridge = bridge
    try:
        handler_hub, statuses_hub, _ = _make_simulator_handler()
        handler_hub.handle_api_press({"key_id": "summary_hub"})
        assert statuses_hub == [200]
        hub_resp = json.loads(handler_hub.wfile.getvalue().decode("utf-8"))
        assert hub_resp["focused"] is True
        assert hub_resp["session"] == "Focus Target App"

        handler_slot, statuses_slot, _ = _make_simulator_handler()
        handler_slot.handle_api_press({"key_id": "session_slot_0"})
        assert statuses_slot == [200]
        slot_resp = json.loads(handler_slot.wfile.getvalue().decode("utf-8"))
        assert slot_resp["focused"] is True
        assert slot_resp["session"] == "Focus Target App"
    finally:
        SimulatorHTTPHandler.bridge = prev_bridge

    assert len(focuser.calls) == 4
