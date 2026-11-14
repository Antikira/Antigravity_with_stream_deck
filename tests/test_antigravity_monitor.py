"""Unit tests for Antigravity Session Monitor components."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

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
from src.antigravity_monitor.session_detector import (
    SessionDetector,
    SessionStatus,
    _parse_db_datetime,
)
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
    detector = SessionDetector(base_dir=mock_antigravity_env, ignore_preexisting_done=False)
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
        last_transcript_step={"tool_calls": [{"name": "run_command", "status": "PENDING"}]},
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


def test_quota_reader_windows_creationflags(mock_antigravity_env: Path):
    class FakeResult:
        stdout = MOCK_AGY_OUTPUT
        stderr = ""
        returncode = 0

    with patch(
        "src.antigravity_monitor.quota_reader.subprocess.run", return_value=FakeResult()
    ) as mock_run:
        with patch("sys.platform", "win32"):
            reader = QuotaReader(base_dir=mock_antigravity_env)
            reader.get_quota_stats()
            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs.get("stdin") == subprocess.DEVNULL
            assert kwargs.get("creationflags") == getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000
            )
            if hasattr(subprocess, "STARTUPINFO"):
                startupinfo = kwargs.get("startupinfo")
                assert startupinfo is not None
                assert startupinfo.dwFlags & getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                assert startupinfo.wShowWindow == getattr(subprocess, "SW_HIDE", 0)

    with patch(
        "src.antigravity_monitor.quota_reader.subprocess.run", return_value=FakeResult()
    ) as mock_run:
        with patch("sys.platform", "linux"):
            reader = QuotaReader(base_dir=mock_antigravity_env)
            reader.get_quota_stats()
            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs.get("stdin") == subprocess.DEVNULL
            assert "creationflags" not in kwargs


# ---------------------------------------------------------------------------
# Session Page Manager Tests
# ---------------------------------------------------------------------------


def test_session_page_manager(mock_antigravity_env: Path):
    with _mock_subprocess_empty():
        detector = SessionDetector(base_dir=mock_antigravity_env, ignore_preexisting_done=False)
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
        detector = SessionDetector(base_dir=mock_antigravity_env, ignore_preexisting_done=False)
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
        detector = SessionDetector(base_dir=mock_antigravity_env, ignore_preexisting_done=False)
        mgr = SessionPageManager(session_detector=detector)
        mgr.refresh()

    hub = mgr.build_summary_hub_key_data()
    # Session 1 is WAITING_FOR_APPROVAL, so hub status must be "waiting" (Red)
    assert hub.status == "waiting"
    assert hub.bg_color == "#E53935"
    assert "承認待ち" in hub.title
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
    hwnd = WindowFocusManager.find_window_for_workspace("file:///non_existent_path_xyz_1234")
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


def test_parse_db_datetime():
    # Valid strings
    dt1 = _parse_db_datetime("2026-10-02 22:38:57.0189613+00:00")
    assert dt1 is not None
    assert dt1.year == 2026
    assert dt1.month == 10
    assert dt1.day == 2

    # Strings without milliseconds
    dt2 = _parse_db_datetime("2026-10-02T22:38:57+00:00")
    assert dt2 is not None

    # Empty and whitespace strings
    assert _parse_db_datetime("") is None
    assert _parse_db_datetime("   ") is None

    # Invalid strings that trigger exception
    assert _parse_db_datetime("invalid-datetime-string") is None
    assert _parse_db_datetime(None) is None
    assert _parse_db_datetime("2026-10-02 22:38:57.invalid+00:00") is None


# ---------------------------------------------------------------------------
# Permission Detection Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "ask_question",
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
        "run_command",
        "write_to_file",
        "replace_file_content",
    ],
)
def test_execution_tools_remain_working_without_pending_permission(tool_name: str):
    detector = SessionDetector(base_dir=Path("."))

    # Execution tools remain working unless explicit permission is requested
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

    # Conversation DB with pending permission
    conv_dir = base_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    cid_db = conv_dir / "sess-perm-01.db"
    con_cid = sqlite3.connect(cid_db)
    cur_cid = con_cid.cursor()
    cur_cid.execute("""
        CREATE TABLE steps (
            idx INTEGER PRIMARY KEY,
            step_type INTEGER,
            status INTEGER,
            permissions BLOB
        )
    """)
    cur_cid.execute(
        "INSERT INTO steps VALUES (1, 132, 2, ?)",
        (b"\x12\x10\n\x0e\n\x07command\x12\x03cmd\x10\x01*\x07command",),
    )
    con_cid.commit()
    con_cid.close()

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
                            "permission": True,
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
    assert hub_key.subtitle == "WAITING"

    # Slot 0 key must be Red and display [承認待]
    slot_key = next(k for k in all_keys if k.key_id == "session_slot_0")
    assert slot_key.bg_color == "#E53935"
    assert "[承認待]" in slot_key.title


@pytest.mark.parametrize(
    ("status_code", "perm_blob", "expected_status"),
    [
        # (a) status=2 and unapproved pending permission blob -> WAITING_FOR_APPROVAL
        (2, b"\x12\x10\x08command_permission_blob", SessionStatus.WAITING_FOR_APPROVAL),
        # (b) status=2 and approved permission blob (\x10\x01\x18\x01* single approval) -> WORKING
        (
            2,
            b"\x12\x1e\n\x12\n\x07command\x12\x07uv run \x10\x01\x18\x01*\x07command",
            SessionStatus.WORKING,
        ),
        # (b) status=2 and approved permission blob (\x10\x01\x18\x05* rule approval) -> WORKING
        (
            2,
            b"\x12\x1e\n\x12\n\x07command\x12\x07uv run \x10\x01\x18\x05*\x07command",
            SessionStatus.WORKING,
        ),
        # (c) status=2 and permissions=None -> WORKING
        (2, None, SessionStatus.WORKING),
        # (d) status=9 -> WAITING_FOR_APPROVAL
        (9, None, SessionStatus.WAITING_FOR_APPROVAL),
    ],
)
def test_sqlite_permissions_detection(
    tmp_path: Path,
    status_code: int,
    perm_blob: bytes | None,
    expected_status: SessionStatus,
):
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
    cur.execute(
        "INSERT INTO steps VALUES (?, ?, ?, ?)",
        (1, 132, status_code, perm_blob),
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
    assert status == expected_status


def test_cli_session_title_fallback(tmp_path: Path):
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

    # Session 1: title="", preview contains <USER_REQUEST> tags
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cid-001-preview",
            "",
            "<USER_REQUEST>\nFix bug in parser\n</USER_REQUEST>",
            5,
            now_iso,
            json.dumps(["file:///D:/projects/my_cli_app"]),
            "CASCADE_RUN_STATUS_IDLE",
            "",
            0,
        ),
    )

    # Session 2: title="", preview="", workspace_uris set
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cid-002-workspace",
            "",
            "",
            5,
            now_iso,
            json.dumps(["file:///D:/projects/my_cli_app"]),
            "CASCADE_RUN_STATUS_IDLE",
            "",
            0,
        ),
    )

    # Session 3: title="", preview="", workspace_uris=[], has transcript.jsonl with USER_INPUT
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cid-003-transcript",
            "",
            "",
            5,
            now_iso,
            "[]",
            "CASCADE_RUN_STATUS_IDLE",
            "",
            0,
        ),
    )

    # Session 4: all empty
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cid-004-all-empty",
            "",
            "",
            5,
            now_iso,
            "[]",
            "CASCADE_RUN_STATUS_IDLE",
            "",
            0,
        ),
    )
    con.commit()
    con.close()

    # Create transcript for Session 3
    t_log_dir = base_dir / "brain" / "cid-003-transcript" / ".system_generated" / "logs"
    t_log_dir.mkdir(parents=True, exist_ok=True)
    with open(t_log_dir / "transcript.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "content": (
                        "<ADDITIONAL_METADATA>\ninfo\n</ADDITIONAL_METADATA>\n"
                        "<USER_REQUEST>\nRefactor database schema\n</USER_REQUEST>"
                    ),
                }
            )
            + "\n"
        )

    detector = SessionDetector(base_dir=base_dir, ignore_preexisting_done=False)
    sessions = detector.list_sessions()
    sessions_by_id = {s.conversation_id: s for s in sessions}

    # Case 1: Resolved from preview
    s1 = sessions_by_id["cid-001-preview"]
    assert s1.title == "Fix bug in parser"
    assert s1.short_title == "Fix bug in …"

    # Case 2: Resolved from workspace URI
    s2 = sessions_by_id["cid-002-workspace"]
    assert s2.title == "my_cli_app"
    assert s2.short_title == "my_cli_app"

    # Case 3: Resolved from transcript first user prompt
    s3 = sessions_by_id["cid-003-transcript"]
    assert s3.title == "Refactor database schema"
    assert s3.short_title == "Refactor da…"

    # Case 4: Fallback to CLI-{cid[:6]}
    s4 = sessions_by_id["cid-004-all-empty"]
    assert s4.title == "CLI-cid-00"
    assert s4.short_title == "CLI-cid-00"


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
    detector = SessionDetector(base_dir=mock_antigravity_env, ignore_preexisting_done=False)
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
        bridge = StreamDeckBridge(port=1234, plugin_uuid="uuid", register_event="register")
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


def test_inspect_transcript_caching(tmp_path: Path):
    base_dir = tmp_path / "antigravity"
    log_dir = base_dir / "brain" / "cid_cache_test" / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = log_dir / "transcript.jsonl"

    step_1 = json.dumps({"type": "PLANNER_RESPONSE", "status": "WORKING"}) + "\n"
    transcript_file.write_text(step_1, encoding="utf-8")

    detector = SessionDetector(base_dir=base_dir)

    # First call: cache miss, reads from disk
    result1 = detector.inspect_transcript("cid_cache_test")
    assert result1.get("status") == "WORKING"
    assert "cid_cache_test" in detector._transcript_cache
    cached_mtime, cached_data = detector._transcript_cache["cid_cache_test"]
    assert cached_data == result1

    # Second call with unchanged mtime: returns from cache
    # Verify by temporarily mutating cache to confirm it's used
    detector._transcript_cache["cid_cache_test"] = (
        cached_mtime,
        {"type": "PLANNER_RESPONSE", "status": "CACHED"},
    )
    result2 = detector.inspect_transcript("cid_cache_test")
    assert result2.get("status") == "CACHED"

    # Update file with new content and newer mtime
    import time

    time.sleep(0.05)
    step_2 = json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE"}) + "\n"
    transcript_file.write_text(step_1 + step_2, encoding="utf-8")

    result3 = detector.inspect_transcript("cid_cache_test")
    assert result3.get("status") == "DONE"
    assert detector._transcript_cache["cid_cache_test"][1]["status"] == "DONE"


def test_list_sessions_transcript_cache_retention(tmp_path: Path):
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
        INSERT INTO conversation_summaries VALUES
        ('cid_active', 'Active Session', '', 1, ?, '[]', 'CASCADE_RUN_STATUS_RUNNING', '', 1),
        ('cid_past', 'Past Session', '', 1, '2020-01-01T00:00:00Z', '[]', 'DONE', '', 0)
    """,
        (now_iso,),
    )
    con.commit()
    con.close()

    # Create transcripts for both
    for cid in ("cid_active", "cid_past"):
        ldir = base_dir / "brain" / cid / ".system_generated" / "logs"
        ldir.mkdir(parents=True, exist_ok=True)
        (ldir / "transcript.jsonl").write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE"}) + "\n",
            encoding="utf-8",
        )

    detector = SessionDetector(base_dir=base_dir, ignore_preexisting_done=True)
    sessions = detector.list_sessions()

    # cid_past was ignored in output list due to ignore_preexisting_done
    assert [s.conversation_id for s in sessions] == ["cid_active"]
    # But both cids were examined in query, so both should be in cache
    assert "cid_active" in detector._transcript_cache
    assert "cid_past" in detector._transcript_cache


def test_simulator_http_handler_log_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from src.antigravity_monitor.streamdeck_bridge import SimulatorHTTPHandler

    mock_logger = MagicMock()
    monkeypatch.setattr("src.antigravity_monitor.streamdeck_bridge.logger", mock_logger)

    handler = SimulatorHTTPHandler.__new__(SimulatorHTTPHandler)
    handler.client_address = ("127.0.0.1", 54321)

    handler.log_message('"%s" %s %s', "GET /api/status HTTP/1.1", "200", "-")

    mock_logger.debug.assert_called_once()
    args, _ = mock_logger.debug.call_args
    assert args[0] == '%s - "%s" %s %s'
    assert args[1] == "127.0.0.1"
    assert args[2:] == ("GET /api/status HTTP/1.1", "200", "-")


def test_configure_windowless_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging
    import subprocess
    import sys

    from src.antigravity_monitor.streamdeck_bridge import configure_windowless_logging

    log_path = tmp_path / "logs" / "plugin.log"
    root_logger = logging.getLogger()
    existing_handlers = list(root_logger.handlers)
    previous_level = root_logger.level

    console_handler = logging.StreamHandler(sys.__stderr__)
    root_logger.addHandler(console_handler)

    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)

    # Simulate broken StreamHandler(None) registered when pythonw starts with stderr=None
    broken_handler = logging.StreamHandler(None)
    assert broken_handler.stream is None
    root_logger.addHandler(broken_handler)

    created_stream = None

    try:
        configured = configure_windowless_logging(log_path)
        assert configured == log_path
        assert sys.stdout is not None
        assert sys.stderr is not None
        assert sys.stdout is sys.stderr
        assert broken_handler not in root_logger.handlers
        assert console_handler not in root_logger.handlers
        created_stream = sys.stdout

        print("hello windowless")
        test_logger = logging.getLogger("test_windowless")
        test_logger.setLevel(logging.INFO)
        test_logger.info("logger entry")

        for handler in list(root_logger.handlers):
            if handler not in existing_handlers:
                handler.flush()

        content = log_path.read_text(encoding="utf-8")
        assert "hello windowless" in content
        assert content.count("logger entry") == 1
        assert "--- Logging error ---" not in content
        assert "AttributeError" not in content
    finally:
        for handler in list(root_logger.handlers):
            if handler not in existing_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        for handler in (broken_handler, console_handler):
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(previous_level)
        if created_stream is not None:
            created_stream.close()

    # Subprocess verification: import streamdeck_bridge from scratch with stdout=None, stderr=None
    repo_root = Path(__file__).resolve().parent.parent
    sub_log = tmp_path / "logs" / "subprocess_windowless.log"
    code = (
        "import sys; "
        "sys.stdout = None; "
        "sys.stderr = None; "
        "from src.antigravity_monitor import streamdeck_bridge; "
        f"streamdeck_bridge.configure_windowless_logging({str(sub_log)!r}); "
        "streamdeck_bridge.logger.info('subprocess windowless ok'); "
        "print('subprocess print ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--- Logging error ---" not in proc.stderr
    sub_content = sub_log.read_text(encoding="utf-8")
    assert sub_content.count("subprocess windowless ok") == 1
    assert "subprocess print ok" in sub_content
    assert "--- Logging error ---" not in sub_content
    assert "AttributeError" not in sub_content


def test_is_venv_healthy_requires_pythonw_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.antigravity_monitor.installer import get_venv_pythonw, is_venv_healthy

    project_root = tmp_path / "repo"
    scripts_dir = project_root / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "python.exe").write_bytes(b"")

    monkeypatch.setattr("sys.platform", "win32")
    assert get_venv_pythonw(project_root) == scripts_dir / "pythonw.exe"
    # pythonw.exe is missing, so venv must be reported unhealthy
    assert is_venv_healthy(project_root) is False


def test_ensure_uv_available_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.antigravity_monitor.installer import ensure_uv_available

    monkeypatch.setattr("src.antigravity_monitor.installer.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="uv command not found"):
        ensure_uv_available()


def test_setup_isolated_venv_repairs_broken_venv_and_syncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.antigravity_monitor.installer import setup_isolated_venv

    project_root = tmp_path / "repo"
    scripts_dir = project_root / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    # Create dummy broken executables in .venv
    py_exe = scripts_dir / "python.exe"
    pyw_exe = scripts_dir / "pythonw.exe"
    py_exe.write_bytes(b"")
    pyw_exe.write_bytes(b"")

    commands: list[list[str]] = []

    class FakeResult:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd: list[str], **kwargs):
        commands.append(cmd)
        # Simulate broken .venv interpreter check returning exit code 1
        if len(cmd) >= 3 and cmd[1:3] == ["-c", "import sys"]:
            return FakeResult(1)
        return FakeResult(0)

    monkeypatch.setattr(
        "src.antigravity_monitor.installer.shutil.which",
        lambda name: "C:/bin/uv.exe" if name == "uv" else None,
    )
    monkeypatch.setattr("src.antigravity_monitor.installer.subprocess.run", fake_run)

    resolved = setup_isolated_venv(project_root)
    assert resolved == pyw_exe
    assert ["C:/bin/uv.exe", "venv", "--clear"] in commands
    assert ["C:/bin/uv.exe", "sync", "--no-dev"] in commands


def test_build_windowless_launcher_and_install_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.antigravity_monitor.installer import (
        find_csc_compiler,
        install_plugin,
        uninstall_plugin,
    )

    if find_csc_compiler() is None:
        pytest.skip("csc.exe not available in this environment")

    source_dir = tmp_path / "source" / "com.user.antigravity.sdPlugin"
    source_dir.mkdir(parents=True)
    (source_dir / "manifest.json").write_text('{"CodePathWin": "antigravity_launcher.exe"}')

    appdata_dir = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata_dir))

    dest_dir = install_plugin(
        source_dir=source_dir,
        symlink=False,
        setup_venv=False,
        build_launcher=True,
    )

    assert dest_dir == appdata_dir / "Elgato" / "StreamDeck" / "Plugins" / source_dir.name
    assert (dest_dir / "project_path.txt").is_file()
    launcher_path = dest_dir / "antigravity_launcher.exe"
    assert launcher_path.is_file()

    # Verify PE subsystem is IMAGE_SUBSYSTEM_WINDOWS_GUI (2) so no console window opens
    pe_bytes = launcher_path.read_bytes()
    pe_offset = int.from_bytes(pe_bytes[0x3C:0x40], "little")
    subsystem = int.from_bytes(pe_bytes[pe_offset + 24 + 68 : pe_offset + 24 + 70], "little")
    assert subsystem == 2

    assert uninstall_plugin(source_dir.name) is True
    assert not dest_dir.exists()


def test_install_and_uninstall_handles_broken_and_active_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.antigravity_monitor.installer import install_plugin, uninstall_plugin

    appdata_dir = tmp_path / "appdata"
    plugins_dir = appdata_dir / "Elgato" / "StreamDeck" / "Plugins"
    plugins_dir.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata_dir))

    plugin_name = "com.user.antigravity.sdPlugin"
    dest_dir = plugins_dir / plugin_name

    # Create a broken symlink (target does not exist)
    missing_target = tmp_path / "deleted_source" / plugin_name
    try:
        dest_dir.symlink_to(missing_target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires elevated privileges or Developer Mode")

    assert dest_dir.is_symlink()
    assert not dest_dir.exists()

    # uninstall_plugin must remove broken symlink
    assert uninstall_plugin(plugin_name) is True
    assert not dest_dir.is_symlink()

    # Re-create broken symlink and ensure install_plugin replaces it cleanly
    dest_dir.symlink_to(missing_target, target_is_directory=True)
    source_dir = tmp_path / "source" / plugin_name
    source_dir.mkdir(parents=True)
    (source_dir / "manifest.json").write_text('{"CodePathWin": "antigravity_launcher.exe"}')

    installed_dir = install_plugin(
        source_dir=source_dir,
        symlink=True,
        setup_venv=False,
        build_launcher=False,
    )
    assert installed_dir == dest_dir
    assert installed_dir.is_symlink()
    assert (source_dir / "project_path.txt").is_file()
    # Place dummy antigravity_launcher.exe to verify symlink uninstall cleans generated files
    (source_dir / "antigravity_launcher.exe").write_bytes(b"MZ")

    assert uninstall_plugin(plugin_name) is True
    assert not dest_dir.is_symlink()
    assert not (source_dir / "project_path.txt").exists()
    assert not (source_dir / "antigravity_launcher.exe").exists()


def test_windowless_launcher_exe_passes_json_info_and_logs_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sys

    from src.antigravity_monitor.installer import build_windowless_launcher, find_csc_compiler

    if find_csc_compiler() is None:
        pytest.skip("csc.exe not available in this environment")

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True)
    launcher_exe = build_windowless_launcher(plugin_dir / "antigravity_launcher.exe")

    # Dummy project layout expected by launcher
    proj_dir = tmp_path / "dummy_proj"
    pkg_dir = proj_dir / "src" / "antigravity_monitor"
    pkg_dir.mkdir(parents=True)
    (proj_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    captured_file = tmp_path / "captured_args.json"
    (pkg_dir / "streamdeck_bridge.py").write_text(
        "import argparse, json, os, sys\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('-port', type=int)\n"
        "parser.add_argument('-pluginUUID')\n"
        "parser.add_argument('-registerEvent')\n"
        "parser.add_argument('-info')\n"
        "parser.add_argument('--fail', action='store_true')\n"
        "args = parser.parse_args()\n"
        "if args.fail:\n"
        "    sys.stderr.write('simulated startup crash\\n')\n"
        "    sys.exit(42)\n"
        "out = {'port': args.port, 'uuid': args.pluginUUID, 'event': args.registerEvent, "
        "'info': json.loads(args.info), 'log_env': os.environ.get('ANTIGRAVITY_LOG_FILE')}\n"
        f"Path({str(captured_file)!r}).write_text(json.dumps(out), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (plugin_dir / "project_path.txt").write_text(str(proj_dir), encoding="utf-8")

    info_payload = json.dumps(
        {
            "application": {"language": "en", "platform": "windows", "version": "6.5.0"},
            "plugin": {"uuid": "com.user.antigravity", "path": 'C:\\Program Files\\Elgato "SD"\\'},
        }
    )

    env = os.environ.copy()
    env.pop("ANTIGRAVITY_PROJECT_DIR", None)
    env["ANTIGRAVITY_PYTHON"] = sys.executable

    proc_ok = subprocess.run(
        [
            str(launcher_exe),
            "-port",
            "28196",
            "-pluginUUID",
            "uuid-1234",
            "-registerEvent",
            "registerPlugin",
            "-info",
            info_payload,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_ok.returncode == 0
    captured = json.loads(captured_file.read_text(encoding="utf-8"))
    assert captured["port"] == 28196
    assert captured["uuid"] == "uuid-1234"
    assert captured["event"] == "registerPlugin"
    assert captured["info"] == json.loads(info_payload)
    assert Path(captured["log_env"]).resolve() == (plugin_dir / "plugin.log").resolve()

    # Verify stderr and non-zero exit code are logged to plugin.log
    proc_fail = subprocess.run(
        [str(launcher_exe), "--fail"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_fail.returncode == 42
    log_text = (plugin_dir / "plugin.log").read_text(encoding="utf-8")
    assert "[STDERR] simulated startup crash" in log_text
    assert "[ERROR] Python process exited with code 42." in log_text
    assert "[WARN]" not in log_text


def test_build_windowless_launcher_decodes_utf8_compiler_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.antigravity_monitor.installer import build_windowless_launcher

    monkeypatch.setattr(
        "src.antigravity_monitor.installer.find_csc_compiler",
        lambda: Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe"),
    )

    captured_calls: list[tuple[list[str], dict[str, object]]] = []
    japanese_err = "error CS0016: 出力ファイルに書き込めませんでした。別のプロセスが使用中です。"

    def fake_run(cmd: list[str], **kwargs: object):
        captured_calls.append((cmd, kwargs))
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr=japanese_err)

    monkeypatch.setattr("src.antigravity_monitor.installer.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="別のプロセスが使用中です") as exc_info:
        build_windowless_launcher(tmp_path / "antigravity_launcher.exe")

    assert japanese_err in str(exc_info.value)
    assert len(captured_calls) == 1
    cmd, kwargs = captured_calls[0]
    assert "/utf8output" in cmd
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"


def test_replace_executable_stops_locked_launcher_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from src.antigravity_monitor.installer import _replace_executable

    src_exe = tmp_path / "temp" / "launcher.exe"
    src_exe.parent.mkdir(parents=True)
    src_exe.write_bytes(b"MZ-new")
    dest_exe = tmp_path / "plugin" / "antigravity_launcher.exe"

    stopped_names: list[str] = []
    monkeypatch.setattr(
        "src.antigravity_monitor.installer.stop_running_launcher",
        lambda exe_name: stopped_names.append(exe_name),
    )
    monkeypatch.setattr("src.antigravity_monitor.installer.time.sleep", lambda _: None)

    original_copy2 = shutil.copy2
    attempts = 0

    def flaky_copy2(src: Path, dst: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("locked by running process")
        return original_copy2(src, dst)

    monkeypatch.setattr("src.antigravity_monitor.installer.shutil.copy2", flaky_copy2)

    _replace_executable(src_exe, dest_exe)

    assert attempts == 2
    assert stopped_names == ["antigravity_launcher.exe"]
    assert dest_exe.read_bytes() == b"MZ-new"
