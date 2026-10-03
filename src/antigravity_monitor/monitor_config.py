"""Configuration module for Antigravity Session Monitor.

Handles loading and validating user settings from a JSON configuration file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("data/monitor_config.json")
DEFAULT_HUB_DONE_TIMEOUT_SECONDS: float = 300.0


@dataclass
class MonitorConfig:
    """Configuration settings for Antigravity Session Monitor."""

    # Timeout in seconds to switch summary hub from DONE to READY/待機.
    # If >= 0 (positive or zero), hub switches to READY after this duration.
    # If None or negative (or invalid), legacy behavior is preserved (stays DONE).
    hub_done_timeout_seconds: float | None = DEFAULT_HUB_DONE_TIMEOUT_SECONDS

    @property
    def is_hub_done_timeout_enabled(self) -> bool:
        """Return True if hub done timeout is enabled (>= 0)."""
        return (
            self.hub_done_timeout_seconds is not None
            and self.hub_done_timeout_seconds >= 0
        )


def _validate_timeout(value: Any) -> float | None:
    """Validate hub_done_timeout_seconds.

    Returns float if value is a number >= 0.
    Returns None if value is negative, non-numeric, or explicitly disabled,
    indicating legacy behavior should be preserved.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value >= 0:
            return float(value)
        # Negative value -> keep legacy behavior (no timeout)
        return None
    # None or invalid type -> keep legacy behavior
    return None


def load_monitor_config(config_path: Path | str | None = None) -> MonitorConfig:
    """Load configuration from file, falling back to defaults if not found or invalid."""
    candidate_paths: list[Path] = []
    if config_path is not None:
        candidate_paths.append(Path(config_path))
    else:
        candidate_paths.append(DEFAULT_CONFIG_PATH)
        candidate_paths.append(Path("monitor_config.json"))

    target_path: Path | None = None
    for p in candidate_paths:
        if p.exists() and p.is_file():
            target_path = p
            break

    if target_path is None:
        logger.debug("Config file not found in %s, using defaults.", candidate_paths)
        return MonitorConfig(hub_done_timeout_seconds=DEFAULT_HUB_DONE_TIMEOUT_SECONDS)

    try:
        content = target_path.read_text(encoding="utf-8")
        raw_data = json.loads(content)
        if not isinstance(raw_data, dict):
            logger.warning("Config root must be a JSON object: %s", target_path)
            return MonitorConfig(hub_done_timeout_seconds=DEFAULT_HUB_DONE_TIMEOUT_SECONDS)

        if "hub_done_timeout_seconds" in raw_data:
            timeout_val = _validate_timeout(raw_data["hub_done_timeout_seconds"])
        else:
            timeout_val = DEFAULT_HUB_DONE_TIMEOUT_SECONDS

        return MonitorConfig(hub_done_timeout_seconds=timeout_val)
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s, using defaults.", target_path, exc)
        return MonitorConfig(hub_done_timeout_seconds=DEFAULT_HUB_DONE_TIMEOUT_SECONDS)
