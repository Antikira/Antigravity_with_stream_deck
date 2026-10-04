"""Unit tests for conversation list settings, filtering, sorting, and bridge synchronization.

Verifies the requirements specified in docs/conversation_list_settings_design.md.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.antigravity_monitor.config import (
    ConversationListConfig,
    filter_and_sort_sessions,
    is_session_visible,
    parse_session_datetime,
    sort_sessions,
)
from src.antigravity_monitor.session_detector import SessionInfo, SessionStatus
from src.antigravity_monitor.state_store import StateStore
from src.antigravity_monitor.streamdeck_bridge import StreamDeckBridge


def _make_session(
    cid: str,
    title: str = "Test Session",
    status: SessionStatus = SessionStatus.DONE,
    updated_at: str = "",
    is_active_presence: bool = False,
) -> SessionInfo:
    return SessionInfo(
        conversation_id=cid,
        title=title,
        status=status,
        updated_at=updated_at,
        is_active_presence=is_active_presence,
    )


# ----------------------------------------------------------------------
# 1. ConversationListConfig Tests
# ----------------------------------------------------------------------


def test_parse_session_datetime():
    dt_iso = "2026-10-04T12:00:00+00:00"
    parsed = parse_session_datetime(dt_iso)
    assert parsed is not None
    assert parsed.year == 2026

    # None and empty
    assert parse_session_datetime(None) is None
    assert parse_session_datetime("") is None
    assert parse_session_datetime("invalid-date") is None


def test_config_defaults():
    cfg = ConversationListConfig()
    assert cfg.done_retention_minutes == 60
    assert cfg.max_inactivity_days == 7
    assert cfg.protect_waiting_sessions is True
    assert cfg.protect_working_sessions is True
    assert cfg.exclude_inactive_unlocked is False
    assert cfg.max_tracked_sessions == 20
    assert len(cfg.sort_criteria) == 3


def test_config_from_dict_and_clamping():
    # Out of range done_retention_minutes and max_inactivity_days
    raw = {
        "done_retention_minutes": 1000000,  # Clamped to 525600
        "max_inactivity_days": -5,  # Clamped to 0
        "max_tracked_sessions": 2,  # Clamped to 5
        "protect_waiting_sessions": False,
        "protect_working_sessions": False,
        "exclude_inactive_unlocked": True,
        "sort_criteria": [{"field": "title", "ascending": True}],
    }
    cfg = ConversationListConfig.from_dict(raw)
    assert cfg.done_retention_minutes == 525600
    assert cfg.max_inactivity_days == 0
    assert cfg.max_tracked_sessions == 5
    assert cfg.protect_waiting_sessions is False
    assert cfg.protect_working_sessions is False
    assert cfg.exclude_inactive_unlocked is True
    assert len(cfg.sort_criteria) == 1
    assert cfg.sort_criteria[0]["field"] == "title"

    # Fallback on empty or invalid data
    cfg_empty = ConversationListConfig.from_dict({})
    assert cfg_empty.done_retention_minutes == 60
    assert len(cfg_empty.sort_criteria) == 3


def test_config_file_persistence(tmp_path: Path):
    cfg_file = tmp_path / "conversation_settings.json"
    cfg = ConversationListConfig(
        done_retention_minutes=120,
        max_inactivity_days=14,
        max_tracked_sessions=10,
    )
    cfg.save_to_file(cfg_file)
    assert cfg_file.exists()

    loaded = ConversationListConfig.load_from_file(cfg_file)
    assert loaded.done_retention_minutes == 120
    assert loaded.max_inactivity_days == 14
    assert loaded.max_tracked_sessions == 10


# ----------------------------------------------------------------------
# 2. Session Visibility Filtering Tests
# ----------------------------------------------------------------------


def test_visibility_exclude_inactive_unlocked():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    now_iso = now.isoformat()

    active_sess = _make_session("s1", is_active_presence=True, updated_at=now_iso)
    inactive_sess = _make_session("s2", is_active_presence=False, updated_at=now_iso)

    cfg_default = ConversationListConfig(exclude_inactive_unlocked=False)
    assert is_session_visible(active_sess, cfg_default, now=now) is True
    assert is_session_visible(inactive_sess, cfg_default, now=now) is True

    cfg_exclude = ConversationListConfig(exclude_inactive_unlocked=True)
    assert is_session_visible(active_sess, cfg_exclude, now=now) is True
    assert is_session_visible(inactive_sess, cfg_exclude, now=now) is False


def test_visibility_protection_rules():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    old_iso = (now - timedelta(days=50)).isoformat()

    waiting_sess = _make_session(
        "s_wait",
        status=SessionStatus.WAITING_FOR_APPROVAL,
        updated_at=old_iso,
    )
    working_sess = _make_session(
        "s_work",
        status=SessionStatus.WORKING,
        updated_at=old_iso,
    )

    # With protection enabled (default), even old sessions are visible
    cfg_protect = ConversationListConfig(
        max_inactivity_days=7,
        protect_waiting_sessions=True,
        protect_working_sessions=True,
    )
    assert is_session_visible(waiting_sess, cfg_protect, now=now) is True
    assert is_session_visible(working_sess, cfg_protect, now=now) is True

    # With protection disabled, they are pruned due to max_inactivity_days
    cfg_no_protect = ConversationListConfig(
        max_inactivity_days=7,
        protect_waiting_sessions=False,
        protect_working_sessions=False,
    )
    assert is_session_visible(waiting_sess, cfg_no_protect, now=now) is False
    assert is_session_visible(working_sess, cfg_no_protect, now=now) is False


def test_visibility_done_retention_minutes():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    recent_done = _make_session(
        "s_recent",
        status=SessionStatus.DONE,
        updated_at=(now - timedelta(minutes=30)).isoformat(),
    )
    old_done = _make_session(
        "s_old",
        status=SessionStatus.DONE,
        updated_at=(now - timedelta(minutes=90)).isoformat(),
    )

    # 1. 60 minutes retention
    cfg_60 = ConversationListConfig(done_retention_minutes=60)
    assert is_session_visible(recent_done, cfg_60, now=now) is True
    assert is_session_visible(old_done, cfg_60, now=now) is False

    # 2. 0: Immediate drop
    cfg_0 = ConversationListConfig(done_retention_minutes=0)
    assert is_session_visible(recent_done, cfg_0, now=now) is False
    assert is_session_visible(old_done, cfg_0, now=now) is False

    # 3. -1: Retain indefinitely (subject to max_inactivity_days)
    cfg_forever = ConversationListConfig(
        done_retention_minutes=-1,
        max_inactivity_days=7,
    )
    assert is_session_visible(recent_done, cfg_forever, now=now) is True
    assert is_session_visible(old_done, cfg_forever, now=now) is True


def test_visibility_max_inactivity_days():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sess_3d = _make_session(
        "s_3d",
        status=SessionStatus.DONE,
        updated_at=(now - timedelta(days=3)).isoformat(),
    )
    sess_10d = _make_session(
        "s_10d",
        status=SessionStatus.DONE,
        updated_at=(now - timedelta(days=10)).isoformat(),
    )

    # Retention allows them through, test inactivity threshold
    cfg_7d = ConversationListConfig(
        done_retention_minutes=-1,
        max_inactivity_days=7,
    )
    assert is_session_visible(sess_3d, cfg_7d, now=now) is True
    assert is_session_visible(sess_10d, cfg_7d, now=now) is False

    # 0: unlimited inactivity
    cfg_unlimited = ConversationListConfig(
        done_retention_minutes=-1,
        max_inactivity_days=0,
    )
    assert is_session_visible(sess_10d, cfg_unlimited, now=now) is True


# ----------------------------------------------------------------------
# 3. Sorting & Capacity Limiting Tests
# ----------------------------------------------------------------------


def test_sort_sessions_status_and_time():
    t1 = "2026-10-04T10:00:00+00:00"
    t2 = "2026-10-04T11:00:00+00:00"
    t3 = "2026-10-04T12:00:00+00:00"

    s_done = _make_session("s_done", status=SessionStatus.DONE, updated_at=t3)
    s_work = _make_session("s_work", status=SessionStatus.WORKING, updated_at=t1)
    s_wait = _make_session("s_wait", status=SessionStatus.WAITING_FOR_APPROVAL, updated_at=t2)

    criteria = [
        {"field": "status", "ascending": False},  # WAITING (2) > WORKING (1) > DONE (0)
        {"field": "updated_at", "ascending": False},
    ]

    sorted_list = sort_sessions([s_done, s_work, s_wait], criteria)
    assert [s.conversation_id for s in sorted_list] == ["s_wait", "s_work", "s_done"]


def test_sort_sessions_by_title():
    s_a = _make_session("sa", title="Apple")
    s_b = _make_session("sb", title="Banana")
    s_c = _make_session("sc", title="Cherry")

    crit_asc = [{"field": "title", "ascending": True}]
    sorted_asc = sort_sessions([s_b, s_c, s_a], crit_asc)
    assert [s.title for s in sorted_asc] == ["Apple", "Banana", "Cherry"]

    crit_desc = [{"field": "title", "ascending": False}]
    sorted_desc = sort_sessions([s_b, s_c, s_a], crit_desc)
    assert [s.title for s in sorted_desc] == ["Cherry", "Banana", "Apple"]


def test_filter_and_sort_and_capacity():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sessions = []
    for i in range(15):
        sessions.append(
            _make_session(
                f"s_{i:02d}",
                title=f"Session {i:02d}",
                status=SessionStatus.DONE,
                updated_at=(now - timedelta(minutes=i * 2)).isoformat(),
            )
        )

    cfg = ConversationListConfig(
        done_retention_minutes=60,
        max_tracked_sessions=5,
    )
    result = filter_and_sort_sessions(sessions, cfg, now=now)
    assert len(result) == 5
    # Most recent first
    assert result[0].conversation_id == "s_00"
    assert result[4].conversation_id == "s_04"


# ----------------------------------------------------------------------
# 4. StateStore Configuration Updates
# ----------------------------------------------------------------------


def test_state_store_update_config():
    store = StateStore()
    now = datetime.now(timezone.utc)

    # 2 sessions: 1 recent done (10m ago), 1 older done (45m ago)
    s1 = _make_session("s1", updated_at=(now - timedelta(minutes=10)).isoformat())
    s2 = _make_session("s2", updated_at=(now - timedelta(minutes=45)).isoformat())

    store.update_sessions([s1, s2])
    assert len(store.get_snapshot().sessions) == 2

    # Change config to 30 minutes retention
    new_cfg = ConversationListConfig(done_retention_minutes=30)
    changed = store.update_config(new_cfg)

    assert changed is True
    # Now s2 is excluded because age > 30 min
    snapshot = store.get_snapshot()
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].conversation_id == "s1"


# ----------------------------------------------------------------------
# 5. StreamDeckBridge Dual Persistence & Global Settings
# ----------------------------------------------------------------------


def test_bridge_did_receive_global_settings(tmp_path: Path):
    async def _run():
        cfg_file = tmp_path / "test_settings.json"
        bridge = StreamDeckBridge(
            port=12345,
            plugin_uuid="test_uuid",
            register_event="registerPlugin",
            config_path=cfg_file,
        )

        # Simulate didReceiveGlobalSettings with new configuration
        payload = {
            "event": "didReceiveGlobalSettings",
            "payload": {
                "settings": {
                    "done_retention_minutes": 1440,
                    "max_inactivity_days": 30,
                    "max_tracked_sessions": 25,
                }
            },
        }

        await bridge.handle_streamdeck_event(payload)

        # Verify StateStore and local file were updated
        current_config = bridge.state_store.get_config()
        assert current_config.done_retention_minutes == 1440
        assert current_config.max_inactivity_days == 30
        assert current_config.max_tracked_sessions == 25
        assert cfg_file.exists()

    asyncio.run(_run())


def test_bridge_syncs_local_config_when_global_empty(tmp_path: Path):
    async def _run():
        cfg_file = tmp_path / "test_settings.json"
        local_cfg = ConversationListConfig(done_retention_minutes=120)
        local_cfg.save_to_file(cfg_file)

        bridge = StreamDeckBridge(
            port=12345,
            plugin_uuid="test_uuid",
            register_event="registerPlugin",
            config_path=cfg_file,
        )

        mock_ws = AsyncMock()
        bridge.ws = mock_ws

        # Empty settings received from Stream Deck
        payload = {
            "event": "didReceiveGlobalSettings",
            "payload": {"settings": {}},
        }
        await bridge.handle_streamdeck_event(payload)

        # Bridge should send setGlobalSettings back
        mock_ws.send.assert_called()
        sent_msgs = [json.loads(call.args[0]) for call in mock_ws.send.call_args_list]
        set_global = next((m for m in sent_msgs if m.get("event") == "setGlobalSettings"), None)
        assert set_global is not None
        assert set_global["payload"]["done_retention_minutes"] == 120

    asyncio.run(_run())
