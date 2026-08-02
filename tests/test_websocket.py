"""Tests for WebSocket endpoints: /ws/state, /ws/stems, /ws/conductor.

These tests avoid importing app.app_ui (which triggers a slow openai import chain)
by constructing a minimal FastAPI app with just the ws_router mounted.
"""

import pytest
import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from app.routes.ws import ws_manager, _state_snapshot, _stems_snapshot, ws_router
from app.framework.framework_state import state


@pytest.fixture(autouse=True)
def reset_state():
    """Reset application state before each test."""
    state.reset()
    state.active_stems = []
    state.stem_volumes = {}
    state.muted_stems = set()
    state.soloed_stems = set()
    # Clear any leftover WS connections
    for topic in list(ws_manager._connections.keys()):
        ws_manager._connections[topic].clear()
    yield


@pytest.fixture
def app():
    """Create a minimal FastAPI app with just the ws_router mounted."""
    application = FastAPI()
    application.include_router(ws_router)
    return application


class TestWebSocketState:
    """Tests for /ws/state endpoint."""

    def test_connect_and_receive_initial_snapshot(self, app):
        """Connecting to /ws/state should immediately receive a full state snapshot."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/state") as ws:
            data = ws.receive_json()
            assert data["type"] == "state"
            assert "current_bpm" in data
            assert "current_key" in data
            assert "is_generating" in data
            assert data["current_bpm"] == 120
            assert data["current_key"] == "C minor"

    def test_get_state_action(self, app):
        """Sending {"action": "get_state"} should return a fresh snapshot."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()  # initial snapshot
            ws.send_json({"action": "get_state"})
            data = ws.receive_json()
            assert data["type"] == "state"
            assert "ts" in data

    def test_state_snapshot_contains_expected_fields(self, app):
        """State snapshot should contain all required fields."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/state") as ws:
            data = ws.receive_json()
            expected_fields = {
                "type",
                "ts",
                "current_set_name",
                "current_bpm",
                "current_key",
                "target_bpm_override",
                "target_key_override",
                "user_override",
                "available_instruments",
                "muted_stems",
                "soloed_stems",
                "stem_volumes",
                "active_stems",
                "llm_reasoning",
                "is_generating",
                "loop_count",
                "last_actions",
                "is_show_started",
                "audience_message",
                "audience_message_ts",
                "currently_playing_loop_index",
                "currently_playing_stems",
                "currently_playing_set_name",
                "currently_playing_reasoning",
                "loop_history",
                "next_queued_stems",
                "is_show_recording",
                "is_playback_active",
            }
            assert expected_fields.issubset(set(data.keys()))

    def test_multiple_clients_receive_updates(self, app):
        """Multiple clients on /ws/state should all receive initial snapshots."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/state") as ws1:
            with client.websocket_connect("/ws/state") as ws2:
                data1 = ws1.receive_json()
                data2 = ws2.receive_json()
                assert data1["type"] == "state"
                assert data2["type"] == "state"


class TestWebSocketStems:
    """Tests for /ws/stems endpoint."""

    def test_connect_and_receive_stems_snapshot(self, app):
        """Connecting to /ws/stems should receive a stems snapshot."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/stems") as ws:
            data = ws.receive_json()
            assert data["type"] == "stems"
            assert "stems" in data
            assert isinstance(data["stems"], list)

    def test_get_stems_action(self, app):
        """Sending {"action": "get_stems"} should return a fresh stems snapshot."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/stems") as ws:
            ws.receive_json()  # initial snapshot
            ws.send_json({"action": "get_stems"})
            data = ws.receive_json()
            assert data["type"] == "stems"
            assert "stems" in data

    def test_stems_with_active_stems(self, app):
        """Stems endpoint should return configured stems with volume/mute/solo."""
        # Real stems are dicts; _stems_snapshot spreads each with ``**`` so a
        # bare string would raise TypeError (swallowed by the handler, leaving
        # the client to hang).
        state.active_stems = [{"instrument": "Drums"}, {"instrument": "Bass"}]
        state.stem_volumes = {0: 0.8}
        state.muted_stems = {0}

        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/stems") as ws:
            data = ws.receive_json()
            stems = data["stems"]
            assert len(stems) == 2
            assert stems[0]["is_muted"] is True
            assert stems[0]["volume"] == 0.8
            assert stems[1]["is_muted"] is False


class TestWebSocketConductor:
    """Tests for /ws/conductor endpoint."""

    def test_connect_and_receive_initial_reasoning(self, app):
        """Connecting to /ws/conductor should receive current reasoning text."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/conductor") as ws:
            data = ws.receive_json()
            assert data["type"] == "reasoning"
            assert "text" in data
            assert "loop_index" in data

    def test_get_reasoning_action(self, app):
        """Sending {"action": "get_reasoning"} should return current reasoning."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/conductor") as ws:
            ws.receive_json()  # initial
            ws.send_json({"action": "get_reasoning"})
            data = ws.receive_json()
            assert data["type"] == "reasoning"
            assert "text" in data

    def test_conductor_when_no_reasoning(self, app):
        """Conductor should return empty string when no reasoning has happened."""
        state.llm_reasoning = ""
        state.currently_playing_loop_index = -1
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/conductor") as ws:
            data = ws.receive_json()
            assert data["type"] == "reasoning"
            assert data["text"] == ""
            assert data["loop_index"] == -1


class TestConnectionManager:
    """Tests for the ConnectionManager class."""

    def test_connect_and_disconnect(self):
        """ConnectionManager should track connect/disconnect properly."""
        mock_ws = MagicMock()
        # connect() awaits ``websocket.accept()``; a bare MagicMock returns a
        # non-awaitable, so accept must be an AsyncMock.
        mock_ws.accept = AsyncMock()

        async def run():
            await ws_manager.connect(mock_ws, "state")
            assert mock_ws in ws_manager._connections["state"]
            await ws_manager.disconnect(mock_ws, "state")
            assert mock_ws not in ws_manager._connections["state"]

        asyncio.run(run())

    def test_different_topics_isolated(self, app):
        """Different WebSocket topics should not interfere."""
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/state") as ws_state:
            with client.websocket_connect("/ws/stems") as ws_stems:
                data_state = ws_state.receive_json()
                data_stems = ws_stems.receive_json()
                assert data_state["type"] == "state"
                assert data_stems["type"] == "stems"


class TestBroadcastFunctions:
    """Tests for the broadcast helper functions."""

    def test_broadcast_state_update(self, app):
        """broadcast_state_update should not raise."""
        from app.routes.ws import broadcast_state_update

        broadcast_state_update()

    def test_broadcast_stems_update(self, app):
        """broadcast_stems_update should not raise."""
        from app.routes.ws import broadcast_stems_update

        broadcast_stems_update()

    def test_broadcast_conductor_reasoning(self, app):
        """broadcast_conductor_reasoning should not raise."""
        from app.routes.ws import broadcast_conductor_reasoning

        broadcast_conductor_reasoning("test reasoning", 0, done=False)
        broadcast_conductor_reasoning("test reasoning", 0, done=True)


class TestSnapshotFunctions:
    """Tests for the snapshot helper functions."""

    def test_state_snapshot_returns_dict(self):
        """_state_snapshot should return a valid dict."""
        snap = asyncio.run(_state_snapshot())
        assert isinstance(snap, dict)
        assert snap["type"] == "state"
        assert "ts" in snap

    def test_stems_snapshot_returns_dict(self):
        """_stems_snapshot should return a valid dict."""
        snap = asyncio.run(_stems_snapshot())
        assert isinstance(snap, dict)
        assert snap["type"] == "stems"
        assert "stems" in snap
        assert isinstance(snap["stems"], list)

    def test_state_snapshot_does_not_alias_live_state(self):
        """Returned mutable containers must be copies, not aliases (A5).

        The framework loop mutates active_stems/volumes/muted_stems under
        state.lock; the snapshot must hand out deep copies so a later
        mutation of the payload cannot corrupt live state.
        """
        state.active_stems = [{"instrument": "Drums", "tags": ["hard"]}]
        state.stem_volumes = {0: 0.5}
        state.muted_stems = {0}

        snap = asyncio.run(_state_snapshot())

        # Mutate every mutable field of the returned snapshot.
        snap["active_stems"][0]["instrument"] = "MUTATED"
        snap["active_stems"][0]["tags"].append("extra")
        snap["stem_volumes"][0] = 9.9
        # muted_stems serializes as a JSON-native list (sorted); mutate it and
        # confirm live state (a set) is untouched.
        snap["muted_stems"].append(99)

        assert state.active_stems[0]["instrument"] == "Drums"
        assert state.active_stems[0]["tags"] == ["hard"]
        assert state.stem_volumes[0] == 0.5
        assert 99 not in state.muted_stems

    def test_stems_snapshot_does_not_alias_live_state(self):
        """_stems_snapshot must deep-copy each stem dict (A5)."""
        state.active_stems = [{"instrument": "Bass", "tags": ["deep"]}]

        snap = asyncio.run(_stems_snapshot())
        snap["stems"][0]["tags"].append("x")
        snap["stems"][0]["instrument"] = "MUTATED"

        assert state.active_stems[0]["tags"] == ["deep"]
        assert state.active_stems[0]["instrument"] == "Bass"

    def test_broadcast_survives_set_mutation_during_send(self):
        """A connection dropped mid-broadcast must not crash iteration (A8).

        Before the fix, ``broadcast`` iterated the live connection set while
        ``send_text`` awaited; a concurrent discard raised
        'Set changed size during iteration'. It now iterates a list snapshot.
        """
        from app.routes.ws import ConnectionManager

        mgr = ConnectionManager()
        ws_keep = MagicMock()
        ws_drop = MagicMock()

        async def send_keep(_payload):
            # Mimic a concurrent disconnect during iteration.
            mgr._connections["state"].discard(ws_drop)

        ws_keep.send_text = AsyncMock(side_effect=send_keep)
        ws_drop.send_text = AsyncMock()
        mgr._connections["state"].update({ws_keep, ws_drop})

        async def run():
            await mgr.broadcast("state", {"type": "ping"})

        asyncio.run(run())  # must not raise RuntimeError
