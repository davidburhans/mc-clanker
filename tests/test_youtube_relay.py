"""Tests for the YouTube RTMP relay: argv construction, lifecycle, restarts,
and the /api/youtube routes.

FFmpeg is faked (no real subprocess): FakeProc stands in for Popen, and death
is simulated by flipping its returncode + breaking the stdin pipe.
"""

import base64
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.framework.framework_state import state
from app.youtube_relay import RelayConfig, RelayError, YouTubeRelay, _scrub, build_ffmpeg_args

RTMP_URL = "rtmp://a.rtmp.youtube.com/live2"
STREAM_KEY = "abcd-1234-efgh-5678"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStdin:
    """Collects written bytes; can be flipped to raise like a broken pipe."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.fail_writes = False

    def write(self, data: bytes) -> None:
        if self.closed or self.fail_writes:
            raise BrokenPipeError("fake broken pipe")
        self.buffer.extend(data)

    def close(self) -> None:
        self.closed = True


class FakeProc:
    """Popen stand-in; call die() to simulate process death."""

    def __init__(self, argv, **kwargs) -> None:
        self.argv = argv
        self.stdin = FakeStdin()
        self.stderr = io.BytesIO(b"")
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def die(self, code: int = 1) -> None:
        self.returncode = code
        self.stdin.fail_writes = True


@pytest.fixture
def fake_popen(monkeypatch):
    """Patch subprocess.Popen inside the relay module; returns created procs."""
    created: list[FakeProc] = []

    def _popen(argv, **kwargs) -> FakeProc:
        proc = FakeProc(argv, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr("app.youtube_relay.subprocess.Popen", _popen)
    return created


@pytest.fixture(autouse=True)
def reset_relay_state():
    """Isolate YouTube state; stop any relay left running by a test."""
    relay = getattr(state, "youtube_relay", None)
    if relay is not None and relay.active:
        relay.stop()
    state.youtube_relay = None
    state.audio_clients = []
    state.youtube_stream_key = ""
    state.youtube_ingest_url = RTMP_URL
    state.dj_password = ""
    state.audience_password = ""
    yield
    relay = getattr(state, "youtube_relay", None)
    if relay is not None and relay.active:
        relay.stop()
    state.youtube_relay = None
    state.audio_clients = []


def make_cfg(**overrides) -> RelayConfig:
    params = dict(
        ingest_url=RTMP_URL,
        stream_key=STREAM_KEY,
        queue_poll_s=0.02,
        restart_backoff_s=0.0,
    )
    params.update(overrides)
    return RelayConfig(**params)


def make_relay(**overrides) -> YouTubeRelay:
    return YouTubeRelay(make_cfg(**overrides), state)


def wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# RelayConfig
# ---------------------------------------------------------------------------


class TestRelayConfig:
    def test_rejects_bad_resolution(self):
        with pytest.raises(RelayError, match="resolution"):
            make_cfg(resolution="640x480")

    def test_rejects_bad_fps(self):
        with pytest.raises(RelayError, match="fps"):
            make_cfg(fps=25)

    def test_rejects_bad_visualizer(self):
        with pytest.raises(RelayError, match="visualizer"):
            make_cfg(visualizer="fireworks")

    def test_rejects_empty_key(self):
        with pytest.raises(RelayError, match="stream_key"):
            make_cfg(stream_key="  ")

    def test_rejects_non_rtmp_url(self):
        with pytest.raises(RelayError, match="rtmp://"):
            make_cfg(ingest_url="http://youtube.com")

    def test_default_bitrate_follows_resolution(self):
        assert make_cfg(resolution="1280x720").resolved_video_bitrate_kbps() == 2500
        assert make_cfg().resolved_video_bitrate_kbps() == 4500

    def test_explicit_bitrate_wins(self):
        cfg = make_cfg(video_bitrate_kbps=3000)
        assert cfg.resolved_video_bitrate_kbps() == 3000


# ---------------------------------------------------------------------------
# build_ffmpeg_args
# ---------------------------------------------------------------------------


class TestBuildFFmpegArgs:
    def test_rtmp_url_is_final_argument(self):
        args = build_ffmpeg_args(make_cfg())
        assert args[-1] == f"{RTMP_URL}/{STREAM_KEY}"

    def test_uses_showcqt_with_resolution_and_fps(self):
        args = build_ffmpeg_args(make_cfg(resolution="1280x720", fps=30))
        filter_idx = args.index("-filter_complex")
        assert "showcqt=size=1280x720:rate=30:axis=0" in args[filter_idx + 1]

    @pytest.mark.parametrize("vis", ["waves", "spectrum"])
    def test_visualizer_variants(self, vis):
        args = build_ffmpeg_args(make_cfg(visualizer=vis))
        assert vis in " ".join(args)

    def test_youtube_required_encoder_settings(self):
        args = build_ffmpeg_args(make_cfg())
        joined = " ".join(args)
        assert "-pix_fmt yuv420p" in joined
        assert "-f flv" in joined
        assert "aac" in joined
        assert "libx264" in joined

    def test_keyframe_interval_is_two_seconds(self):
        args = build_ffmpeg_args(make_cfg(fps=30))
        assert args[args.index("-g") + 1] == "60"

    def test_bitrate_bounds_present(self):
        args = build_ffmpeg_args(make_cfg())  # 1080p default → 4500k
        assert args[args.index("-b:v") + 1] == "4500k"
        assert args[args.index("-maxrate") + 1] == "4500k"
        assert args[args.index("-bufsize") + 1] == "9000k"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestRelayLifecycle:
    def test_start_registers_audio_client(self, fake_popen):
        relay = make_relay()
        relay.start()
        try:
            assert relay.active
            assert relay._queue in state.audio_clients
            assert relay.status().process_alive
        finally:
            relay.stop()

    def test_double_start_raises(self, fake_popen):
        relay = make_relay()
        relay.start()
        try:
            with pytest.raises(RelayError, match="already started"):
                relay.start()
        finally:
            relay.stop()

    def test_stop_unregisters_and_is_idempotent(self, fake_popen):
        relay = make_relay()
        relay.start()
        summary = relay.stop()
        assert not summary.active
        assert relay._queue not in state.audio_clients
        again = relay.stop()  # idempotent, no raise
        assert not again.active

    def test_pcm_flows_to_ffmpeg_stdin(self, fake_popen):
        relay = make_relay()
        relay.start()
        try:
            relay._queue.put(b"\x01\x02\x03\x04")
            assert wait_until(lambda: len(fake_popen[0].stdin.buffer) == 4)
            assert relay.status().bytes_sent == 4
        finally:
            relay.stop()

    def test_stop_closes_ffmpeg_stdin(self, fake_popen):
        relay = make_relay()
        relay.start()
        relay.stop()
        assert fake_popen[0].stdin.closed


# ---------------------------------------------------------------------------
# Restart on death
# ---------------------------------------------------------------------------


class TestRelayRestart:
    def test_respawns_after_process_death(self, fake_popen):
        relay = make_relay(max_restarts=3)
        relay.start()
        try:
            fake_popen[0].die(code=1)
            assert wait_until(lambda: len(fake_popen) == 2)
            assert relay.status().restarts == 1
            # New process receives subsequent audio.
            relay._queue.put(b"\x05\x06")
            assert wait_until(lambda: len(fake_popen[1].stdin.buffer) == 2)
        finally:
            relay.stop()

    def test_gives_up_after_max_restarts(self, fake_popen):
        relay = make_relay(max_restarts=1)
        relay.start()
        try:
            fake_popen[0].die(code=1)
            assert wait_until(lambda: len(fake_popen) == 2), "first respawn did not happen"
            fake_popen[1].die(code=1)  # second death exceeds the budget
            assert wait_until(lambda: not relay.active)
            assert relay._queue not in state.audio_clients
            assert "gave up" in relay.status().last_error
        finally:
            relay.stop()

    def test_stream_key_never_in_status(self, fake_popen):
        relay = make_relay()
        relay.start()
        try:
            fake_popen[0].die(code=1)
            assert wait_until(lambda: len(fake_popen) == 2)
            assert STREAM_KEY not in relay.status().last_error
        finally:
            relay.stop()

    def test_scrub_removes_secret(self):
        assert _scrub(f"failed {STREAM_KEY} push", STREAM_KEY) == "failed *** push"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from app.app_ui import app

    return TestClient(app)


@pytest.fixture
def auth_headers():
    state.dj_password = "testpass"
    creds = base64.b64encode(b"dj:testpass").decode()
    return {"Authorization": f"Basic {creds}"}


class TestYouTubeRoutes:
    def test_status_requires_auth(self, client):
        assert client.get("/api/youtube/stream/status").status_code == 401

    def test_start_without_key_is_400(self, client, auth_headers, fake_popen):
        response = client.post("/api/youtube/stream/start", json={}, headers=auth_headers)
        assert response.status_code == 400
        assert "stream key" in response.json()["detail"].lower()

    def test_start_stop_roundtrip(self, client, auth_headers, fake_popen):
        state.youtube_stream_key = STREAM_KEY
        started = client.post("/api/youtube/stream/start", json={}, headers=auth_headers)
        assert started.status_code == 200
        body = started.json()
        assert body["status"] == "started"
        assert body["active"] is True
        assert body["stream_key"] == f"****{STREAM_KEY[-4:]}"
        assert STREAM_KEY not in body["stream_key"]

        stopped = client.post("/api/youtube/stream/stop", headers=auth_headers)
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

        again = client.post("/api/youtube/stream/stop", headers=auth_headers)
        assert again.json()["status"] == "already_stopped"

    def test_double_start_conflicts(self, client, auth_headers, fake_popen):
        state.youtube_stream_key = STREAM_KEY
        first = client.post("/api/youtube/stream/start", json={}, headers=auth_headers)
        assert first.status_code == 200
        second = client.post("/api/youtube/stream/start", json={}, headers=auth_headers)
        assert second.status_code == 409
        client.post("/api/youtube/stream/stop", headers=auth_headers)

    def test_start_with_invalid_options_is_422(self, client, auth_headers, fake_popen):
        state.youtube_stream_key = STREAM_KEY
        response = client.post(
            "/api/youtube/stream/start", json={"visualizer": "fireworks"}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_config_roundtrip_masks_key(self, client, auth_headers):
        put = client.put(
            "/api/youtube/config", json={"stream_key": STREAM_KEY}, headers=auth_headers
        )
        assert put.status_code == 200
        assert put.json()["stream_key"] == f"****{STREAM_KEY[-4:]}"
        got = client.get("/api/youtube/config", headers=auth_headers)
        assert STREAM_KEY not in got.json()["stream_key"]

    def test_config_rejects_http_ingest(self, client, auth_headers):
        response = client.put(
            "/api/youtube/config", json={"ingest_url": "http://x"}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_config_rejects_empty_update(self, client, auth_headers):
        response = client.put("/api/youtube/config", json={}, headers=auth_headers)
        assert response.status_code == 422
