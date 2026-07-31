"""
WebSocket routes for real-time state updates in mc-clanker.

Provides three WebSocket endpoints:
  /ws/state   — Full state snapshots + delta updates (BPM, key, stems, volumes, etc.)
  /ws/stems   — Per-stem level/mute/solo changes (lightweight, high-frequency)
  /ws/conductor — Live Conductor LLM reasoning stream (token-by-token when available)

All endpoints are unauthenticated for local development.  In production, JWT
authentication should be added via the `Authorization` header or a `token` query
parameter (see AuthMiddleware in app_ui.py for the JWT pattern).
"""

import asyncio
import copy
import json
import logging
import time
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.framework.framework_state import state

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection manager — tracks active WebSocket subscribers per channel
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manage active WebSocket connections grouped by topic prefix."""

    def __init__(self) -> None:
        # topic -> set of WebSocket connections
        # topic is "state", "stems", or "conductor"
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, topic: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[topic].add(websocket)
        log.info("WS client connected to topic=%s (total=%d)", topic, len(self._connections[topic]))

    async def disconnect(self, websocket: WebSocket, topic: str) -> None:
        async with self._lock:
            self._connections[topic].discard(websocket)

    def _connections_sync(self, topic: str) -> set[WebSocket]:
        """Non-async accessor — safe to call from sync contexts."""
        return self._connections.get(topic, set())

    async def broadcast(self, topic: str, message: dict) -> None:
        """Send a JSON payload to all subscribers on a topic.

        Failures (broken pipes, closed sockets) are handled per-connection
        so one bad socket doesn't prevent the rest from receiving.
        """
        payload = json.dumps(message, default=str)
        stale: list[WebSocket] = []

        # Snapshot the subscriber set BEFORE iterating: send_text awaits and
        # yields control, during which connect()/disconnect() can mutate the
        # live set and raise 'Set changed size during iteration'.
        conns = list(self._connections_sync(topic))
        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)

        # Clean up stale connections
        if stale:
            async with self._lock:
                for ws in stale:
                    self._connections[topic].discard(ws)


# Singleton — shared across all websocket routes
ws_manager = ConnectionManager()

# ---------------------------------------------------------------------------
# JSON snapshot helpers — produce the same payload as the REST /api/state
# ---------------------------------------------------------------------------


async def _state_snapshot() -> dict:
    """Return the full application state as a JSON-serialisable dict.

    All state is read under ``state.lock`` and mutable containers are copied
    so callers receive a consistent point-in-time snapshot that cannot alias
    live framework state.  Mirrors the payload shape of GET /api/state.
    """
    async with state.lock:
        loop_history = _snapshot_loop_history(state.loop_history[-10:])
        return {
            "type": "state",
            "ts": int(time.time()),
            "current_set_name": state.current_set_name,
            "current_bpm": state.current_bpm,
            "current_key": state.current_key,
            "target_bpm_override": state.target_bpm_override,
            "target_key_override": state.target_key_override,
            "user_override": state.user_override,
            "available_instruments": list(state.available_instruments),
            # Serialize as JSON-native lists (sorted for deterministic output).
            # ``WebSocket.send_json`` uses bare ``json.dumps`` with no ``default``
            # hook, so returning a ``set`` here raised TypeError inside the
            # handler, was swallowed by its ``except Exception``, and left the
            # client's receive_json() blocking forever (issue A5 follow-up).
            "muted_stems": sorted(state.muted_stems),
            "soloed_stems": sorted(state.soloed_stems),
            "stem_volumes": dict(state.stem_volumes),
            "active_stems": copy.deepcopy(state.active_stems),
            "llm_reasoning": state.llm_reasoning,
            "is_generating": state.is_generating,
            "loop_count": state.loop_count,
            "last_actions": list(state.last_actions[-10:]) if state.last_actions else [],
            "is_show_started": state.is_show_started,
            "audience_message": state.audience_message,
            "audience_message_ts": state.audience_message_ts,
            "currently_playing_loop_index": state.currently_playing_loop_index,
            "currently_playing_stems": copy.deepcopy(state.currently_playing_stems),
            "currently_playing_set_name": state.currently_playing_set_name,
            "currently_playing_reasoning": state.currently_playing_reasoning,
            "loop_history": loop_history,
            "next_queued_stems": copy.deepcopy(state.next_stems),
            "is_show_recording": state.is_show_recording,
            "is_playback_active": state.is_playback_active,
        }


def _snapshot_loop_history(history: list) -> list:
    """Project a slice of loop history into JSON-serialisable dicts.

    ``history`` must already be a snapshot taken under ``state.lock``.  Each
    entry's nested ``stems`` list is deep-copied so the broadcast payload
    cannot be mutated by the framework after the snapshot is taken.
    """
    return [
        {
            "loop_index": h["loop_index"],
            "set_name": h["set_name"],
            "reasoning": h["reasoning"],
            "stems": copy.deepcopy(h["stems"]),
            "timestamp": h["timestamp"],
        }
        for h in history
    ]


async def _stems_snapshot() -> dict:
    """Return per-stem volumes/mute/solo as a lightweight dict.

    Reads ``state.active_stems`` and mixer state under ``state.lock`` and
    deep-copies each stem dict so the payload cannot alias live state.
    """
    async with state.lock:
        stems = [
            {
                **copy.deepcopy(s),
                "volume": state.stem_volumes.get(i, 1.0),
                "is_muted": i in state.muted_stems,
                "is_soloed": i in state.soloed_stems,
            }
            for i, s in enumerate(state.active_stems)
        ]
    return {"type": "stems", "ts": int(time.time()), "stems": stems}


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------

ws_router = APIRouter()


@ws_router.websocket("/ws/state")
async def websocket_state(websocket: WebSocket):
    """Stream full state deltas.

    Protocol:
      Client connects → server sends a full snapshot immediately.
    After that, the server only pushes updates when state actually changes.

    Client can also send {"action": "get_state"} to request a fresh snapshot.
    """
    await ws_manager.connect(websocket, "state")
    try:
        # Send initial snapshot
        await websocket.send_json(await _state_snapshot())

        # Keep-alive + handle client messages
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a ping to keep the connection alive
                try:
                    await websocket.send_json({"type": "ping", "ts": int(time.time())})
                except Exception:
                    break
                continue

            if not msg:
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            action = data.get("action")
            if action == "get_state":
                await websocket.send_json(await _state_snapshot())
            elif action == "pong":
                pass  # keep-alive ack, nothing to do

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS /ws/state error: %s", e)
    finally:
        await ws_manager.disconnect(websocket, "state")


@ws_router.websocket("/ws/stems")
async def websocket_stems(websocket: WebSocket):
    """Stream per-stem level/mute/solo changes.

    Optimised for high-frequency updates: only sends the stems payload,
    not the full application state.  The client connects here when it
    needs real-time mixer feedback without the overhead of full state.
    """
    await ws_manager.connect(websocket, "stems")
    try:
        # Send initial stem snapshot
        await websocket.send_json(await _stems_snapshot())

        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping", "ts": int(time.time())})
                except Exception:
                    break
                continue

            if not msg:
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            action = data.get("action")
            if action == "get_stems":
                await websocket.send_json(await _stems_snapshot())
            elif action == "pong":
                pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS /ws/stems error: %s", e)
    finally:
        await ws_manager.disconnect(websocket, "stems")


@ws_router.websocket("/ws/conductor")
async def websocket_conductor(websocket: WebSocket):
    """Stream Conductor LLM reasoning as it is produced.

    When the Conductor LLM is called, the reasoning text is forwarded
    token-by-token (or as soon as a new chunk is available) so the DJ
    UI can show the AI's thought process in real time.

    Message types:
      {"type": "reasoning", "text": "...", "loop_index": N}  — partial reasoning
      {"type": "reasoning_done", "text": "...", "loop_index": N}  — final reasoning
      {"type": "state_update", {...}  — state changed during reasoning
    """
    await ws_manager.connect(websocket, "conductor")
    try:
        # Send current reasoning so late subscribers see context
        await websocket.send_json(
            {
                "type": "reasoning",
                "text": state.llm_reasoning,
                "loop_index": state.currently_playing_loop_index,
            }
        )

        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping", "ts": int(time.time())})
                except Exception:
                    break
                continue

            if not msg:
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            action = data.get("action")
            if action == "get_reasoning":
                await websocket.send_json(
                    {
                        "type": "reasoning",
                        "text": state.llm_reasoning,
                        "loop_index": state.currently_playing_loop_index,
                    }
                )
            elif action == "pong":
                pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS /ws/conductor error: %s", e)
    finally:
        await ws_manager.disconnect(websocket, "conductor")


# ---------------------------------------------------------------------------
# Helper: broadcast state changes to WebSocket subscribers
# (called from framework code when state actually changes)
# ---------------------------------------------------------------------------


async def _broadcast_state_async() -> None:
    """Build a locked state snapshot and push it to /ws/state subscribers."""
    await ws_manager.broadcast("state", await _state_snapshot())


def broadcast_state_update() -> None:
    """Push a full state snapshot to all /ws/state subscribers.

    Safe to call from sync contexts (e.g. Mixer thread).  The snapshot is
    built and sent on the running event loop (where ``state.lock`` lives);
    if no loop is running this is a no-op.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No running loop — nothing to broadcast to
    asyncio.run_coroutine_threadsafe(_broadcast_state_async(), loop)


async def _broadcast_stems_async() -> None:
    """Build a locked stems snapshot and push it to /ws/stems subscribers."""
    await ws_manager.broadcast("stems", await _stems_snapshot())


def broadcast_stems_update() -> None:
    """Push a stems-only update to all /ws/stems subscribers.

    Safe to call from sync contexts; the snapshot is built on the event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.run_coroutine_threadsafe(_broadcast_stems_async(), loop)


def broadcast_conductor_reasoning(text: str, loop_index: int, done: bool = False) -> None:
    """Push a Conductor reasoning update to /ws/conductor subscribers.

    Args:
        text: The reasoning text produced so far.
        loop_index: Which loop this reasoning is for.
        done: If True, marks the message as the final chunk.
    """
    msg_type = "reasoning_done" if done else "reasoning"
    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast(
                "conductor",
                {
                    "type": msg_type,
                    "text": text,
                    "loop_index": loop_index,
                },
            ),
            loop,
        )
    except RuntimeError:
        pass
