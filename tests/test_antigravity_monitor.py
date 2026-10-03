"""Unit tests for Antigravity Session Monitor components."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.antigravity_monitor.image_generator import generate_key_svg, svg_to_data_uri
from src.antigravity_monitor.image_generator import (
    generate_hub_rocket_svg,
    generate_key_svg,
    svg_to_data_uri,
)
from src.antigravity_monitor.quota_reader import (
    QuotaReader,
    _format_tokens,
    _parse_iso_datetime,
)
from src.antigravity_monitor.session_detector import SessionDetector, SessionStatus
from src.antigravity_monitor.session_page_manager import SessionPageManager
from src.antigravity_monitor.window_focus import WindowFocusManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_subprocess_empty():
    """Return a context manager that mocks subprocess.run to return empty."""

    class FakeResult:
        stdout = ""
        stderr = ""
        returncode = 0

    return patch(
        "src.antigravity_monitor.quota_reader.subprocess.run",
        return_value=FakeResult(),
    )


MOCK_AGY_OUTPUT = (
    "Gemini Models\tWeekly Limit Remaining\t85%\t2026-10-07T08:47:04Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t25%\t2026-10-03T02:53:20Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t99%\t2026-10-10T01:27:25Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t97%\t2026-10-03T06:27:25Z\n"
)


def _mock_subprocess_agy():
    """Return a context manager that mocks subprocess.run with agy output."""

    class FakeResult:
        stdout = MOCK_AGY_OUTPUT
        stderr = ""
        returncode = 0

    return patch(
        "src.antigravity_monitor.quota_reader.subprocess.run",
        return_value=FakeResult(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_antigravity_env(tmp_path: Path):
    """Create a temporary mock Antigravity data directory."""
    base_dir = tmp_path / "antigravity"
    base_dir.mkdir(parents=True, exist_ok=True)

    db_path = base_dir / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
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
        """)

    now_iso = datetime.now(timezone.utc).isoformat()
    # Insert 6 sample sessions
    for i in range(1, 7):
        cid = f"test-session-{i:02d}"
        title = f"Test Project Session {i}"
        status = "CASCADE_RUN_STATUS_RUNNING" if i == 1 else "CASCADE_RUN_STATUS_IDLE"
        not_idle = 1 if i == 1 else 0
        cur.execute(
            """
            INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                title,
                f"Preview for session {i}",
                i * 10,
                now_iso,
                json.dumps([f"file:///c:/projects/project_{i}"]),
                status,
                "",  # Top level
                not_idle,
            ),
        )
    con.commit()
    con.close()

    # Presence lock for session 1
    presence_dir = base_dir / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    (presence_dir / "test-session-01.lock").touch()

    # Brain transcript for session 1 (has tool_call ask_question)
    s1_log_dir = base_dir / "brain" / "test-session-01" / ".system_generated" / "logs"
    s1_log_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = s1_log_dir / "transcript.jsonl"
    with open(transcript_file, "w", encoding="utf-8") as f:
        line1 = {
            "step_index": 1,
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": now_iso,
            "input_tokens": 1000,
            "output_tokens": 500,
        }
        line2 = {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": now_iso,
            "input_tokens": 5000,
            "output_tokens": 1200,
            "tool_calls": [{"function": {"name": "ask_question"}}],
        }
        f.write(json.dumps(line1) + "\n" + json.dumps(line2) + "\n")

    return base_dir


# ---------------------------------------------------------------------------
# Session Detector Tests
# ---------------------------------------------------------------------------


def test_session_detector(mock_antigravity_env: Path):
    detector = SessionDetector(
        base_dir=mock_antigravity_env, ignore_preexisting_done=False
    )
    sessions = detector.list_sessions(limit=10)

    assert len(sessions) == 6
    # Session 1 has ask_question tool call, so its status should be WAITING_FOR_APPROVAL
    s1 = next(s for s in sessions if s.conversation_id == "test-session-01")
    assert s1.status == SessionStatus.WAITING_FOR_APPROVAL
    assert s1.is_active_presence is True
    assert s1.short_title.endswith("…")

    # Other sessions should be DONE
    s2 = next(s for s in sessions if s.conversation_id == "test-session-02")
    assert s2.status == SessionStatus.DONE
    assert s2.is_active_presence is False


@pytest.mark.parametrize(
    ("db_status", "transcript_status"),
    [
        ("CASCADE_RUN_STATUS_WAITING_FOR_APPROVAL", "DONE"),
        ("CASCADE_RUN_STATUS_RUNNING", "AWAITING_USER_INPUT"),
        ("CASCADE_RUN_STATUS_RUNNING", "WAITING_FOR_TOOL_APPROVAL"),
    ],
)
def test_session_detector_recognizes_waiting_statuses(
    mock_antigravity_env: Path,
    db_status: str,
    transcript_status: str,
):
    detector = SessionDetector(base_dir=mock_antigravity_env)

    status = detector.determine_status(
        db_status,
        not_fully_idle=True,
        last_transcript_step={"status": transcript_status},
    )

    assert status == SessionStatus.WAITING_FOR_APPROVAL


def test_session_detector_recognizes_waiting_tool_call():
    detector = SessionDetector(base_dir=Path("."))

    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={
            "tool_calls": [{"name": "run_command", "status": "PENDING"}]
        },
    )

    assert status == SessionStatus.WAITING_FOR_APPROVAL


def test_session_detector_ignores_malformed_tool_calls():
    detector = SessionDetector(base_dir=Path("."))

    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={"tool_calls": [None, "ask_question", {}]},
    )

    assert status == SessionStatus.WORKING


# ---------------------------------------------------------------------------
# Quota Reader Tests
# ---------------------------------------------------------------------------


def test_quota_reader(mock_antigravity_env: Path):
    with _mock_subprocess_agy():
        reader = QuotaReader(base_dir=mock_antigravity_env)
        stats = reader.get_quota_stats()

    assert "gemini_5h" in stats
    assert "gemini_weekly" in stats
    assert "gpt_5h" in stats
    assert "gpt_weekly" in stats
    assert "daily" not in stats

    g5 = stats["gemini_5h"]
    assert g5.remaining_percent == 25.0
    assert abs(g5.used_percent - 75.0) < 0.1
    assert g5.status_level == "warning"
    assert g5.model_group == "Gemini"
    assert "Gemini" in g5.key_title

    gw = stats["gemini_weekly"]
    assert gw.remaining_percent == 85.0
    assert gw.model_group == "Gemini"
    assert gw.status_level == "normal"

    p5 = stats["gpt_5h"]
    assert p5.remaining_percent == 97.0
    assert p5.model_group == "GPT/Claude"

    pw = stats["gpt_weekly"]
    assert pw.remaining_percent == 99.0
    assert pw.model_group == "GPT/Claude"


# ---------------------------------------------------------------------------
# Session Page Manager Tests
# ---------------------------------------------------------------------------


def test_session_page_manager(mock_antigravity_env: Path):
    with _mock_subprocess_empty():
        detector = SessionDetector(
            base_dir=mock_antigravity_env, ignore_preexisting_done=False
        )
        reader = QuotaReader(base_dir=mock_antigravity_env)
        mgr = SessionPageManager(
            session_detector=detector,
            quota_reader=reader,
        )
        mgr.refresh()

    # 6 sessions total with 5 per page -> 2 pages
    assert mgr.total_pages == 2
    assert mgr.current_page == 0

    slots_p0 = mgr.get_sessions_for_current_page()
    assert len(slots_p0) == 5
    assert slots_p0[0] is not None
    assert slots_p0[0].conversation_id == "test-session-01"

    # Go to next page
    changed = mgr.next_page()
    assert changed is True
    assert mgr.current_page == 1

    slots_p1 = mgr.get_sessions_for_current_page()
    assert slots_p1[0] is not None
    assert slots_p1[0].conversation_id == "test-session-06"
    assert slots_p1[1] is None  # Only 1 session on page 2

    # Cannot go beyond total pages
    assert mgr.next_page() is False

    # Go back
    assert mgr.prev_page() is True
    assert mgr.current_page == 0

    # Verify key rendering data
    keys = mgr.build_all_key_data()
    key_ids = [k.key_id for k in keys]
    assert "page_prev" in key_ids
    assert "page_next" in key_ids
    assert "session_slot_0" in key_ids
    assert "session_slot_4" in key_ids
    assert "quota_gemini_5h" in key_ids
    assert "quota_gpt_5h" in key_ids
    assert "summary_hub" in key_ids

    # Verify hub position (0,2)
    hub_key = next(k for k in keys if k.key_id == "summary_hub")
    assert hub_key.column == 0 and hub_key.row == 2


def test_action_id_resolution(mock_antigravity_env: Path):
    with _mock_subprocess_agy():
        detector = SessionDetector(
            base_dir=mock_antigravity_env, ignore_preexisting_done=False
        )
        reader = QuotaReader(base_dir=mock_antigravity_env)
        mgr = SessionPageManager(
            session_detector=detector,
            quota_reader=reader,
        )
        mgr.refresh()

    # 1. Hub
    k_hub = mgr.get_key_data_by_action_id("com.user.antigravity.summary_hub")
    assert k_hub is not None
    assert k_hub.key_id == "summary_hub"

    # 2-6. Session 1..5
    for i in range(1, 6):
        k_sess = mgr.get_key_data_by_action_id(f"com.user.antigravity.session_slot_{i}")
        assert k_sess is not None
        assert k_sess.key_id == f"session_slot_{i - 1}"

    # 7-8. Prev / Next
    k_prev = mgr.get_key_data_by_action_id("com.user.antigravity.prev_page")
    assert k_prev is not None
    assert k_prev.key_id == "page_prev"

    k_next = mgr.get_key_data_by_action_id("com.user.antigravity.next_page")
    assert k_next is not None
    assert k_next.key_id == "page_next"

    # 9-12. 4 Quota buttons
    k_g5 = mgr.get_key_data_by_action_id("com.user.antigravity.quota_gemini_5h")
    assert k_g5 is not None
    assert k_g5.key_id == "quota_gemini_5h"

    k_gw = mgr.get_key_data_by_action_id("com.user.antigravity.quota_gemini_weekly")
    assert k_gw is not None
    assert k_gw.key_id == "quota_gemini_weekly"

    k_p5 = mgr.get_key_data_by_action_id("com.user.antigravity.quota_gpt_5h")
    assert k_p5 is not None
    assert k_p5.key_id == "quota_gpt_5h"

    k_pw = mgr.get_key_data_by_action_id("com.user.antigravity.quota_gpt_weekly")
    assert k_pw is not None
    assert k_pw.key_id == "quota_gpt_weekly"


def test_summary_hub_key_priority(mock_antigravity_env: Path):
    with _mock_subprocess_empty():
        detector = SessionDetector(
            base_dir=mock_antigravity_env, ignore_preexisting_done=False
        )
        mgr = SessionPageManager(session_detector=detector)
        mgr.refresh()

    hub = mgr.build_summary_hub_key_data()
    # Session 1 is WAITING_FOR_APPROVAL, so hub status must be "waiting" (Red)
    assert hub.status == "waiting"
    assert hub.bg_color == "#E53935"
    assert "承認待ち" in hub.title
    assert hub.title == ""
    assert hub.subtitle == "WAITING"


def test_ignore_preexisting_done_filtering(mock_antigravity_env: Path):
    # Startup time set to now; test sessions 2..6 are older DONE without presence
    now_utc = datetime.now(timezone.utc)
    detector = SessionDetector(
        base_dir=mock_antigravity_env,
        ignore_preexisting_done=True,
        startup_time=now_utc,
    )
    sessions = detector.list_sessions()
    # Only session 1 (has presence lock & waiting) should be tracked
    assert len(sessions) == 1
    assert sessions[0].conversation_id == "test-session-01"


# ---------------------------------------------------------------------------
# Image Generator Tests
# ---------------------------------------------------------------------------


def test_image_generator():
    svg = generate_key_svg(
        title="Test Session",
        subtitle="WORKING",
        status_label="稼働中",
        bg_color="#1E88E5",
        is_active=True,
    )
    assert "<svg" in svg
    assert "Test Session" in svg
    assert "稼働中" in svg

    data_uri = svg_to_data_uri(svg)
    assert data_uri.startswith("data:image/svg+xml;charset=utf8,")


def test_hub_rocket_svg_generator():
    # Test all 4 states
    for status, expected_label in [
        ("working", "WORKING"),
        ("done", "DONE"),
        ("waiting", "WAITING"),
        ("empty", "READY"),
        ("idle", "READY"),
    ]:
        svg = generate_hub_rocket_svg(status)
        assert "<svg" in svg
        assert expected_label in svg
        data_uri = svg_to_data_uri(svg)
        assert data_uri.startswith("data:image/svg+xml;charset=utf8,")


def test_window_focus_helpers():
    # Workspace URI parsing helper test
    hwnd = WindowFocusManager.find_window_for_workspace(
        "file:///non_existent_path_xyz_1234"
    )
    assert hwnd is None

    # Invalid handle focus test
    success = WindowFocusManager.bring_to_foreground(0)
    assert success is False


def test_token_helpers():
    assert _format_tokens(500) == "500"
    assert _format_tokens(1500) == "2K"
    assert _format_tokens(1_500_000) == "1.5M"

    dt = _parse_iso_datetime("2026-10-02T22:38:57.1234567Z")
    assert dt is not None
    assert dt.year == 2026


# ---------------------------------------------------------------------------
# Permission Detection Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "run_command",
        "ask_question",
        "write_to_file",
        "replace_file_content",
        "ask_permission",
        "request_permission",
        "confirm_browser_setup",
        "tool_ask_permission",
    ],
)
def test_permission_tools_trigger_waiting_for_approval(tool_name: str):
    detector = SessionDetector(base_dir=Path("."))

    # Realistic Antigravity transcript format: status is DONE for planner response,
    # tool_calls contains the proposed tool, db is running
    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": tool_name,
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
    )

    assert status == SessionStatus.WAITING_FOR_APPROVAL


@pytest.mark.parametrize(
    "tool_name",
    [
        "view_file",
        "list_dir",
        "grep_search",
        "find_by_name",
        "search_web",
        "read_url_content",
    ],
)
def test_readonly_tools_remain_working_status(tool_name: str):
    detector = SessionDetector(base_dir=Path("."))

    # Read-only tools do not prompt for user permission
    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": tool_name,
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
    )

    assert status == SessionStatus.WORKING


def test_permission_flag_in_tool_call():
    detector = SessionDetector(base_dir=Path("."))

    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "custom_extension_action",
                    "permission": True,
                }
            ],
        },
    )

    assert status == SessionStatus.WAITING_FOR_APPROVAL


def test_permission_turns_hub_and_slots_red(tmp_path: Path):
    base_dir = tmp_path / "antigravity"
    base_dir.mkdir(parents=True, exist_ok=True)
    db_path = base_dir / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
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
        """)
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sess-perm-01",
            "Command Approval Test",
            "Waiting for run_command permission",
            10,
            now_iso,
            json.dumps(["file:///c:/projects/test"]),
            "CASCADE_RUN_STATUS_RUNNING",
            "",
            1,
        ),
    )
    con.commit()
    con.close()

    # Presence lock
    presence_dir = base_dir / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    (presence_dir / "sess-perm-01.lock").touch()

    # Transcript with run_command tool call
    log_dir = base_dir / "brain" / "sess-perm-01" / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "transcript.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "step_index": 5,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "function": {
                                "name": "run_command",
                                "arguments": '{"CommandLine": "pytest"}',
                            },
                        }
                    ],
                }
            )
            + "\n"
        )

    detector = SessionDetector(base_dir=base_dir, ignore_preexisting_done=False)
    sessions = detector.list_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.status == SessionStatus.WAITING_FOR_APPROVAL
    assert session.display_status_label == "承認待"

    # Verify Page Manager renders key as RED (#E53935)
    with _mock_subprocess_empty():
        page_mgr = SessionPageManager(session_detector=detector)
        page_mgr.refresh()
    all_keys = page_mgr.build_all_key_data()

    # Hub key must be Red and state "waiting"
    hub_key = next(k for k in all_keys if k.key_id == "summary_hub")
    assert hub_key.status == "waiting"
    assert hub_key.bg_color == "#E53935"
    assert "承認待ち (1)" in hub_key.title
    assert hub_key.title == ""
    assert hub_key.subtitle == "WAITING"

    # Slot 0 key must be Red and display [承認待]
    slot_key = next(k for k in all_keys if k.key_id == "session_slot_0")
    assert slot_key.bg_color == "#E53935"
    assert "[承認待]" in slot_key.title


def test_sqlite_permissions_detection(tmp_path: Path):
    base_dir = tmp_path / "antigravity"
    conv_dir = base_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    cid = "sess-sqlite-01"
    cid_db = conv_dir / f"{cid}.db"
    con = sqlite3.connect(cid_db)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE steps (
            idx INTEGER PRIMARY KEY,
            step_type INTEGER,
            status INTEGER,
            permissions BLOB
        )
        """)
    # Step 1: In progress (status 2) with permissions blob
    cur.execute(
        "INSERT INTO steps VALUES (?, ?, ?, ?)",
        (1, 132, 2, b"\x12\x10\x08command_permission_blob"),
    )
    con.commit()
    con.close()

    detector = SessionDetector(base_dir=base_dir)
    status = detector.determine_status(
        "CASCADE_RUN_STATUS_RUNNING",
        not_fully_idle=True,
        last_transcript_step={
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [],
        },
        conversation_id=cid,
    )
    assert status == SessionStatus.WAITING_FOR_APPROVAL


def test_state_store_atomic_update_and_listener():
    from src.antigravity_monitor.quota_reader import QuotaInfo
    from src.antigravity_monitor.session_detector import SessionInfo, SessionStatus
    from src.antigravity_monitor.state_store import StateStore

    store = StateStore()
    notified_snapshots = []
    store.add_listener(lambda s: notified_snapshots.append(s))

    # Initial snapshot is empty
    snap0 = store.get_snapshot()
    assert len(snap0.sessions) == 0
    assert snap0.version == 0

    # Update sessions
    sess1 = SessionInfo(
        conversation_id="c-01",
        title="Session One",
        status=SessionStatus.WORKING,
    )
    changed = store.update_sessions([sess1])
    assert changed is True
    assert len(notified_snapshots) == 1
    assert notified_snapshots[0].version == 1
    assert len(notified_snapshots[0].sessions) == 1
    assert notified_snapshots[0].sessions[0].conversation_id == "c-01"

    # Redundant update with identical sessions should return False and not notify
    changed_no = store.update_sessions([sess1])
    assert changed_no is False
    assert len(notified_snapshots) == 1

    # Update quotas
    q_info = QuotaInfo(
        window_name="5H",
        label="5H",
        model_group="Gemini",
        remaining_percent=80.0,
    )
    changed_q = store.update_quotas({"gemini_5h": q_info})
    assert changed_q is True
    assert len(notified_snapshots) == 2
    assert "gemini_5h" in notified_snapshots[1].quotas


def test_data_collector_and_page_manager(mock_antigravity_env: Path):
    import time
    from unittest.mock import MagicMock

    from src.antigravity_monitor.collector import DataCollector
    from src.antigravity_monitor.state_store import StateStore

    store = StateStore()
    detector = SessionDetector(
        base_dir=mock_antigravity_env, ignore_preexisting_done=False
    )
    quota_reader = MagicMock()
    quota_reader.get_quota_stats.return_value = {}

    collector = DataCollector(
        state_store=store,
        session_detector=detector,
        quota_reader=quota_reader,
        session_poll_interval=0.05,
        quota_poll_interval=10.0,
    )

    collector.start()
    try:
        # Wait briefly for collector thread to populate state store
        time.sleep(0.2)
        snap = store.get_snapshot()
        assert len(snap.sessions) == 6

        # SessionPageManager reads directly from StateStore in 0ms
        page_mgr = SessionPageManager(state_store=store)
        page_mgr.refresh()
        assert page_mgr.total_pages == 2
        keys = page_mgr.build_all_key_data()
        assert len(keys) == 12

        hub = next(k for k in keys if k.key_id == "summary_hub")
        assert hub.status == "waiting"
    finally:
        collector.stop()


def test_streamdeck_bridge_dirty_render_cache():
    import asyncio
    from unittest.mock import AsyncMock

    from src.antigravity_monitor.session_page_manager import KeyRenderData
    from src.antigravity_monitor.streamdeck_bridge import StreamDeckBridge

    async def _test_body():
        bridge = StreamDeckBridge(
            port=1234, plugin_uuid="uuid", register_event="register"
        )
        bridge.ws = AsyncMock()
        bridge.active_contexts["ctx_1"] = {
            "action": "com.user.antigravity.summary_hub",
            "coordinates": {"column": 0, "row": 2},
        }

        key_data = KeyRenderData(
            key_id="summary_hub",
            column=0,
            row=2,
            title="Antigravity\n(待機中)",
            subtitle="EMPTY",
            status="empty",
            bg_color="#263238",
        )

        # First render sends setImage and setTitle (2 ws.send calls)
        await bridge._render_key_to_streamdeck("ctx_1", key_data)
        assert bridge.ws.send.call_count == 2

        # Second render with same data should be skipped
        await bridge._render_key_to_streamdeck("ctx_1", key_data)
        assert bridge.ws.send.call_count == 2

        # Render with forced flag should send
        await bridge._render_key_to_streamdeck("ctx_1", key_data, force=True)
        assert bridge.ws.send.call_count == 4

        # Render with modified title should send
        key_data_modified = KeyRenderData(
            key_id="summary_hub",
            column=0,
            row=2,
            title="承認待ち (1)",
            subtitle="WAITING",
            status="waiting",
            bg_color="#E53935",
        )
        await bridge._render_key_to_streamdeck("ctx_1", key_data_modified)
        assert bridge.ws.send.call_count == 6

    asyncio.run(_test_body())
