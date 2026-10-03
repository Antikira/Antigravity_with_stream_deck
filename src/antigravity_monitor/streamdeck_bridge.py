"""Stream Deck Bridge module for Antigravity Session Monitor.

Handles:
1. Native WebSocket communication with Elgato Stream Deck application (I/O-free, zero-latency).
2. Key events (keyDown, willAppear, willDisappear).
3. Dynamic key image/title updates based on StateStore data collected by background worker.
4. Embedded HTTP simulator server for testing and web-based key controller.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

from src.antigravity_monitor.collector import DataCollector
from src.antigravity_monitor.image_generator import (
    generate_hub_rocket_svg,
    generate_key_svg,
    svg_to_data_uri,
)
from src.antigravity_monitor.monitor_config import (
    MonitorConfig,
    _validate_sort_criteria,
    _validate_timeout,
    load_monitor_config,
    save_monitor_config,
)
from src.antigravity_monitor.session_page_manager import (
    KeyRenderData,
    SessionPageManager,
)
from src.antigravity_monitor.state_store import MonitorSnapshot, StateStore
from src.antigravity_monitor.window_focus import WindowFocusManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamdeck_bridge")


class StreamDeckBridge:
    """Manages Stream Deck plugin communication and key interactions with decoupled architecture."""

    def __init__(
        self,
        port: int | None = None,
        plugin_uuid: str | None = None,
        register_event: str | None = None,
        info: dict[str, Any] | None = None,
        state_store: StateStore | None = None,
        collector: DataCollector | None = None,
        config: MonitorConfig | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.port = port
        self.plugin_uuid = plugin_uuid
        self.register_event = register_event
        self.info = info or {}
        self.config_path = config_path
        self.config = config or load_monitor_config(config_path=self.config_path)

        # Shared StateStore and dedicated background collector
        self.state_store = state_store or StateStore()
        self.collector = collector or DataCollector(state_store=self.state_store)
        self.page_manager = SessionPageManager(state_store=self.state_store, config=self.config)

        self.ws: Any = None
        # Track active contexts: context -> dict(action, coordinates, settings)
        self.active_contexts: dict[str, dict[str, Any]] = {}
        # Render cache for dirty checking: context -> (title, subtitle, status, bg_color, is_active)
        self._rendered_key_cache: dict[str, tuple[str, str, str, str, bool]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.is_running = True

    async def connect_and_run(self) -> None:
        """Connect to Stream Deck WebSocket and process events."""
        if not self.port or not self.plugin_uuid or not self.register_event:
            logger.error("Missing required Stream Deck connection arguments.")
            return

        if websockets is None:
            logger.error("Websockets module is not installed.")
            return

        self._loop = asyncio.get_running_loop()
        uri = f"ws://127.0.0.1:{self.port}"
        logger.info("Connecting to Stream Deck at %s...", uri)

        # Start dedicated background collector threads (handles all SQLite/disk/CLI I/O)
        self.collector.start()

        # Listen for state store updates for immediate event-driven rendering
        self.state_store.add_listener(self._on_state_changed)

        # Connect WebSocket with ping disabled to prevent keepalive timeout on Elgato software
        async with websockets.connect(uri, ping_interval=None, ping_timeout=None) as ws:
            self.ws = ws
            # Send registration message
            registration = {
                "event": self.register_event,
                "uuid": self.plugin_uuid,
            }
            await ws.send(json.dumps(registration))
            logger.info("Registered plugin %s with Stream Deck", self.plugin_uuid)

            # Start lightweight UI render loop (reads from memory, skips unchanged keys)
            render_task = asyncio.create_task(self._ui_render_loop())

            try:
                async for message in ws:
                    try:
                        data = json.loads(message)
                        await self.handle_streamdeck_event(data)
                    except Exception as e:
                        logger.error("Error processing message: %s", e)
            finally:
                render_task.cancel()
                self.collector.stop()

    def _on_state_changed(self, snapshot: MonitorSnapshot) -> None:
        """Callback invoked when background collector updates StateStore."""
        if self._loop and self._loop.is_running() and self.ws:
            asyncio.run_coroutine_threadsafe(self.update_all_keys(), self._loop)

    async def _ui_render_loop(self) -> None:
        """Fast UI refresh loop (0.5s) that does zero I/O and renders dirty keys."""
        while self.is_running:
            try:
                await asyncio.sleep(0.5)
                await self.update_all_keys()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in UI render loop: %s", e)

    async def update_all_keys(self, force: bool = False) -> None:
        """Update titles and images of registered Stream Deck keys with dirty checking."""
        if not self.ws or not self.active_contexts:
            return

        # Snapshot local key states instantaneously from memory (0ms)
        self.page_manager.refresh()

        for context, ctx_info in list(self.active_contexts.items()):
            action = ctx_info.get("action", "")
            coords = ctx_info.get("coordinates", {})
            col = coords.get("column", 0)
            row = coords.get("row", 0)

            target_key = self.page_manager.get_key_data_by_action_id(
                action_id=action, col=col, row=row
            )
            if target_key:
                await self._render_key_to_streamdeck(context, target_key, force=force)

    async def _render_key_to_streamdeck(
        self, context: str, key_data: KeyRenderData, force: bool = False
    ) -> None:
        """Send setImage and setTitle events only when visual state changed."""
        if not self.ws:
            return

        is_active = key_data.status == "working"
        cache_key = (
            key_data.title,
            key_data.subtitle,
            key_data.status,
            key_data.bg_color,
            is_active,
        )

        # Skip transmission if visual state hasn't changed (Dirty checking)
        if not force and self._rendered_key_cache.get(context) == cache_key:
            return

        svg = generate_key_svg(
            title=key_data.title,
            subtitle=key_data.subtitle,
            status_label=key_data.status,
            bg_color=key_data.bg_color,
            is_active=is_active,
        )
        if key_data.key_id == "summary_hub":
            svg = generate_hub_rocket_svg(status=key_data.status)
        else:
            svg = generate_key_svg(
                title=key_data.title,
                subtitle=key_data.subtitle,
                status_label=key_data.status,
                bg_color=key_data.bg_color,
                is_active=is_active,
            )
        data_uri = svg_to_data_uri(svg)

        set_image_payload = {
            "event": "setImage",
            "context": context,
            "payload": {
                "image": data_uri,
                "target": 0,
            },
        }
        await self.ws.send(json.dumps(set_image_payload))

        set_title_payload = {
            "event": "setTitle",
            "context": context,
            "payload": {
                "title": "",
                "target": 0,
            },
        }
        await self.ws.send(json.dumps(set_title_payload))

        self._rendered_key_cache[context] = cache_key

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        """Apply dynamic settings received from Stream Deck."""
        if not isinstance(settings, dict):
            return
        updated_global = False
        if "hub_done_timeout_seconds" in settings:
            new_timeout = _validate_timeout(settings.get("hub_done_timeout_seconds"))
            self.config.hub_done_timeout_seconds = new_timeout
            logger.info("Updated hub_done_timeout_seconds from settings: %s", new_timeout)

        if "sort_criteria" in settings:
            new_criteria = _validate_sort_criteria(settings.get("sort_criteria"))
            self.config.sort_criteria = new_criteria
            updated_global = True
            logger.info("Updated sort_criteria from settings: %s", new_criteria)

        if updated_global:
            try:
                save_monitor_config(self.config, config_path=self.config_path)
            except Exception as e:
                logger.warning("Failed to save monitor config: %s", e)

    async def handle_streamdeck_event(self, data: dict[str, Any]) -> None:
        """Handle incoming Stream Deck event."""
        event = data.get("event")
        action = data.get("action", "")
        context = data.get("context", "")
        payload = data.get("payload", {})
        coords = payload.get("coordinates", {})

        if event == "willAppear":
            settings = payload.get("settings", {})
            self._apply_settings(settings)
            self.active_contexts[context] = {
                "action": action,
                "coordinates": coords,
                "settings": settings,
            }
            self._rendered_key_cache.pop(context, None)
            await self.update_all_keys(force=True)

        elif event == "willDisappear":
            self.active_contexts.pop(context, None)
            self._rendered_key_cache.pop(context, None)

        elif event in ("didReceiveSettings", "didReceiveGlobalSettings"):
            settings = payload.get("settings", {})
            self._apply_settings(settings)
            if context in self.active_contexts:
                self.active_contexts[context]["settings"] = settings
            self._rendered_key_cache.clear()
            await self.update_all_keys(force=True)

        elif event == "keyDown":
            await self.handle_key_down(context, action, coords, payload.get("settings", {}))

    async def handle_key_down(
        self,
        context: str,
        action: str,
        coords: dict[str, int],
        settings: dict[str, Any],
    ) -> None:
        """Process key press event."""
        col = coords.get("column", -1)
        row = coords.get("row", -1)
        suffix = action.split(".")[-1]

        logger.info("Key down: action=%s, suffix=%s, coords=(%s, %s)", action, suffix, col, row)

        # 1. Hub: Focus the primary (highest-priority) session window
        if suffix == "summary_hub":
            primary = self.page_manager.get_primary_session()
            if primary:
                logger.info(
                    "Hub clicked: Focusing primary session %s (%s)",
                    primary.title,
                    primary.conversation_id,
                )
                WindowFocusManager.focus_session(
                    workspace_uris=primary.workspace_uris,
                    title=primary.title,
                )
            return

        # 2. Previous Page Navigation (Instant UI update)
        if suffix in ("prev_page", "page_prev"):
            if self.page_manager.prev_page():
                await self.update_all_keys(force=True)
            return

        # 3. Next Page Navigation (Instant UI update)
        if suffix in ("next_page", "page_next"):
            if self.page_manager.next_page():
                await self.update_all_keys(force=True)
            return

        # 4. Session Slot Activation (Session 1..5)
        slot_index: int | None = None
        if suffix.startswith("session_slot_"):
            slot_part = suffix.replace("session_slot_", "")
            if slot_part.isdigit():
                num = int(slot_part)
                # If 1-based (session_slot_1..5), map to 0..4
                slot_index = num - 1 if num in (1, 2, 3, 4, 5) else num

        if slot_index is not None and 0 <= slot_index < self.page_manager.items_per_page:
            session = self.page_manager.get_session_by_slot(slot_index)
            if session:
                logger.info(
                    "Activating session slot %d: %s (%s)",
                    slot_index + 1,
                    session.title,
                    session.conversation_id,
                )
                WindowFocusManager.focus_session(
                    workspace_uris=session.workspace_uris,
                    title=session.title,
                )
            return

        # 5. Quota buttons: Trigger non-blocking quota refresh in background worker
        if "quota" in suffix:
            logger.info(
                "Quota indicator key pressed: %s -> Requesting background refresh",
                suffix,
            )
            self.collector.request_quota_refresh()
            return


# --- Embedded Web Simulator Server ---


class SimulatorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Stream Deck Simulator and REST API."""

    bridge: StreamDeckBridge | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Silence standard request logging."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/status":
            self.handle_api_status()
        elif path == "/key_image":
            self.handle_key_image(parsed.query)
        elif path == "/" or path == "/index.html":
            self.handle_index()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        """Handle POST requests with security validations."""
        # 1. CSRF validation: Validate Host / Origin headers
        host = self.headers.get("Host", "")
        # Remove port if present
        host_name = host.split(":")[0] if ":" in host else host
        if host_name not in ("127.0.0.1", "localhost"):
            self.send_error(403, "Forbidden: Invalid Host")
            return

        origin = self.headers.get("Origin", "")
        if origin:
            parsed_origin = urllib.parse.urlparse(origin)
            if parsed_origin.hostname not in ("127.0.0.1", "localhost"):
                self.send_error(403, "Forbidden: Invalid Origin")
                return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/press":
            # 2. CSRF / Type validation
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                self.send_error(415, "Unsupported Media Type")
                return

            # 3. DoS validation: Limit payload size to 4KB
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 4096:
                self.send_error(413, "Payload Too Large")
                return

            try:
                body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(body) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Bad Request: Invalid JSON")
                return

            self.handle_api_press(data)
        else:
            self.send_error(404, "Not Found")

    def handle_api_status(self) -> None:
        """Return current status of all keys and sessions as JSON (reads from memory, 0ms)."""
        mgr = self.bridge.page_manager if self.bridge else SessionPageManager()
        mgr.refresh()
        keys = mgr.build_all_key_data()

        result = {
            "current_page": mgr.current_page,
            "total_pages": mgr.total_pages,
            "keys": [
                {
                    "key_id": k.key_id,
                    "column": k.column,
                    "row": k.row,
                    "title": k.title,
                    "subtitle": k.subtitle,
                    "status": k.status,
                    "bg_color": k.bg_color,
                    "text_color": k.text_color,
                    "payload": k.payload,
                }
                for k in keys
            ],
        }

        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_key_image(self, query_str: str) -> None:
        """Return dynamic SVG image for requested key."""
        params = urllib.parse.parse_qs(query_str)
        key_id = params.get("id", [""])[0]

        mgr = self.bridge.page_manager if self.bridge else SessionPageManager()
        keys = mgr.build_all_key_data()
        target = next((k for k in keys if k.key_id == key_id), None)

        if target:
            svg = generate_key_svg(
                title=target.title,
                subtitle=target.subtitle,
                status_label=target.status,
                bg_color=target.bg_color,
                is_active=(target.status == "working"),
            )
        else:
            svg = generate_key_svg(title="Empty", bg_color="#222222")

        body = svg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_press(self, data: dict[str, Any]) -> None:
        """Handle key press simulation."""
        key_id = data.get("key_id", "")
        mgr = self.bridge.page_manager if self.bridge else SessionPageManager()

        action_result = {"status": "ok", "action": key_id}

        if key_id == "page_prev":
            mgr.prev_page()
        elif key_id == "page_next":
            mgr.next_page()
        elif key_id == "summary_hub":
            primary = mgr.get_primary_session()
            if primary:
                focused = WindowFocusManager.focus_session(
                    workspace_uris=primary.workspace_uris,
                    title=primary.title,
                )
                action_result["session"] = primary.title
                action_result["focused"] = focused
        elif key_id.startswith("session_slot_"):
            slot_part = key_id.replace("session_slot_", "")
            if slot_part.isdigit():
                slot_idx = int(slot_part)
                session = mgr.get_session_by_slot(slot_idx)
                if session:
                    focused = WindowFocusManager.focus_session(
                        workspace_uris=session.workspace_uris,
                        title=session.title,
                    )
                    action_result["session"] = session.title
                    action_result["focused"] = focused
            else:
                action_result["status"] = "error"
                action_result["info"] = "Invalid session slot ID"
        elif key_id.startswith("quota_"):
            if self.bridge and self.bridge.collector:
                self.bridge.collector.request_quota_refresh()

        body = json.dumps(action_result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_index(self) -> None:
        """Serve simulator HTML interface."""
        html_path = Path(__file__).parent / "simulator_ui.html"
        if html_path.is_file():
            body = html_path.read_bytes()
        else:
            body = b"<h1>Antigravity Stream Deck Simulator</h1>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_simulator_server(
    port: int = 18500,
    bridge: StreamDeckBridge | None = None,
) -> ThreadingHTTPServer:
    """Start the background simulator HTTP server."""
    bridge_inst = bridge or StreamDeckBridge()
    bridge_inst.collector.start()
    SimulatorHTTPHandler.bridge = bridge_inst
    server = ThreadingHTTPServer(("127.0.0.1", port), SimulatorHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("Simulator HTTP Server running at http://127.0.0.1:%d/", port)
    return server


def parse_cli_args() -> argparse.Namespace:
    """Parse command line arguments from Stream Deck or terminal."""
    parser = argparse.ArgumentParser(description="Antigravity Stream Deck Bridge")
    parser.add_argument("-port", type=int, help="Stream Deck WebSocket Port")
    parser.add_argument("-pluginUUID", type=str, help="Stream Deck Plugin UUID")
    parser.add_argument("-registerEvent", type=str, help="Stream Deck Register Event")
    parser.add_argument("-info", type=str, help="Stream Deck Info JSON string")
    parser.add_argument(
        "--server", action="store_true", help="Run interactive simulator HTTP server"
    )
    parser.add_argument("--server-port", type=int, default=18500, help="Simulator server port")
    return parser.parse_args()


def main() -> None:
    """Entry point for the Stream Deck plugin process."""
    args = parse_cli_args()

    info_dict = {}
    if args.info:
        try:
            info_dict = json.loads(args.info)
        except Exception:
            pass

    bridge = StreamDeckBridge(
        port=args.port,
        plugin_uuid=args.pluginUUID,
        register_event=args.registerEvent,
        info=info_dict,
    )

    # If launched by Stream Deck (port and pluginUUID provided)
    if args.port and args.pluginUUID:
        asyncio.run(bridge.connect_and_run())
    else:
        # Standalone simulator mode
        server = run_simulator_server(port=args.server_port, bridge=bridge)
        print("=== Antigravity Stream Deck Controller Started ===")
        print(f"Web Dashboard / Simulator: http://127.0.0.1:{args.server_port}/")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                import time

                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            bridge.collector.stop()
            server.shutdown()


if __name__ == "__main__":
    main()
