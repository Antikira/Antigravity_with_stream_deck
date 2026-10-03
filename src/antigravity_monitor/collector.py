"""Background data collector for Antigravity Session Monitor.

Executes all disk I/O, SQLite database queries, and external CLI subprocesses
in dedicated worker threads, ensuring the main asyncio/UI event loop remains
completely non-blocking and responsive.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from src.antigravity_monitor.quota_reader import QuotaReader
from src.antigravity_monitor.session_detector import SessionDetector
from src.antigravity_monitor.state_store import StateStore

logger = logging.getLogger(__name__)


class DataCollector:
    """Dedicated background worker that polls sessions and quotas and feeds StateStore."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        session_detector: SessionDetector | None = None,
        quota_reader: QuotaReader | None = None,
        session_poll_interval: float = 1.0,
        quota_poll_interval: float = 120.0,
    ) -> None:
        self.state_store = state_store or StateStore()
        self.detector = session_detector or SessionDetector()
        self.quota_reader = quota_reader or QuotaReader()
        self.session_poll_interval = session_poll_interval
        self.quota_poll_interval = quota_poll_interval

        self._is_running = False
        self._stop_event = threading.Event()
        self._session_thread: threading.Thread | None = None
        self._quota_thread: threading.Thread | None = None
        self._quota_request_queue: queue.Queue[bool] = queue.Queue()

    def start(self) -> None:
        """Start the background collector threads."""
        if self._is_running:
            return
        self._is_running = True
        self._stop_event.clear()

        # 1. Session collection thread
        self._session_thread = threading.Thread(
            target=self._session_loop,
            daemon=True,
            name="SessionCollectorThread",
        )
        self._session_thread.start()

        # 2. Quota collection thread
        self._quota_thread = threading.Thread(
            target=self._quota_loop,
            daemon=True,
            name="QuotaCollectorThread",
        )
        self._quota_thread.start()

        logger.info("DataCollector threads started successfully.")

    def stop(self) -> None:
        """Stop all collector threads."""
        self._is_running = False
        self._stop_event.set()
        self._quota_request_queue.put(False)

        if self._session_thread and self._session_thread.is_alive():
            self._session_thread.join(timeout=1.0)
        if self._quota_thread and self._quota_thread.is_alive():
            self._quota_thread.join(timeout=1.0)

        logger.info("DataCollector stopped.")

    def request_quota_refresh(self) -> None:
        """Trigger an immediate background quota refresh (non-blocking)."""
        self._quota_request_queue.put(True)

    def _session_loop(self) -> None:
        """Poll session states on a short interval in a background thread."""
        logger.info("Session collector running (interval: %.1fs)", self.session_poll_interval)
        while not self._stop_event.is_set():
            try:
                sessions = self.detector.list_sessions(limit=30, only_main_conversations=True)
                self.state_store.update_sessions(sessions)
            except Exception as e:
                logger.debug("Error in session collector: %s", e)

            self._stop_event.wait(self.session_poll_interval)

    def _quota_loop(self) -> None:
        """Fetch quotas periodically or on demand in a background thread."""
        logger.info("Quota collector thread running (interval: %.1fs)", self.quota_poll_interval)
        last_quota_time = 0.0

        # Initial fetch on startup
        try:
            quotas = self.quota_reader.get_quota_stats(force=True)
            if quotas:
                self.state_store.update_quotas(quotas)
            last_quota_time = time.time()
        except Exception as e:
            logger.debug("Error in initial quota fetch: %s", e)

        while not self._stop_event.is_set():
            timeout = max(0.5, self.quota_poll_interval - (time.time() - last_quota_time))
            try:
                # Wait for explicit quota request or timer timeout
                forced = self._quota_request_queue.get(timeout=timeout)
                if self._stop_event.is_set():
                    break
            except queue.Empty:
                forced = False

            if self._stop_event.is_set():
                break

            try:
                quotas = self.quota_reader.get_quota_stats(force=forced)
                if quotas:
                    self.state_store.update_quotas(quotas)
                last_quota_time = time.time()
            except Exception as e:
                logger.debug("Error updating quota stats: %s", e)
