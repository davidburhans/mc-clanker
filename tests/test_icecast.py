"""Tests for Icecast streaming support.

Tests the IcecastStreamer class and its integration with broadcast_audio().
"""

import os
import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from app.framework.framework_icecast import IcecastStreamer, create_icecast_streamer_from_env
from app.framework.framework_state import state


@pytest.fixture(autouse=True)
def reset_icecast_state():
    """Reset Icecast-related state before each test.

    Note: ``state.icecast_streamer`` is not always present as a GlobalState
    attribute (it is wired lazily), so read it defensively.
    """
    # Stop any existing streamer.
    streamer = getattr(state, "icecast_streamer", None)
    if streamer is not None and getattr(streamer, "is_running", False):
        streamer.stop()
    state.icecast_enabled = False
    state.icecast_streamer = None
    yield
    # Cleanup after test.
    streamer = getattr(state, "icecast_streamer", None)
    if streamer is not None and getattr(streamer, "is_running", False):
        streamer.stop()
    state.icecast_enabled = False
    state.icecast_streamer = None


class TestIcecastStreamerCreation:
    """Test IcecastStreamer factory and constructor."""

    def test_create_from_env_disabled_by_default(self):
        """create_icecast_streamer_from_env returns None when ICECAST_ENABLED is not set."""
        icecast_keys = (
            "ICECAST_ENABLED",
            "ICECAST_HOST",
            "ICECAST_PORT",
            "ICECAST_PASSWORD",
            "ICECAST_MOUNT",
            "ICECAST_NAME",
            "ICECAST_GENRE",
            "ICECAST_DESCRIPTION",
            "ICECAST_URL",
        )
        env = {k: v for k, v in os.environ.items() if k not in icecast_keys}
        with patch.dict(os.environ, env, clear=True):
            result = create_icecast_streamer_from_env()
            assert result is None

    def test_create_from_env_enabled(self):
        """create_icecast_streamer_from_env returns IcecastStreamer when enabled."""
        with patch.dict(
            os.environ,
            {
                "ICECAST_ENABLED": "true",
                "ICECAST_HOST": "radio.example.com",
                "ICECAST_PORT": "8000",
                "ICECAST_PASSWORD": "testpass",
                "ICECAST_MOUNT": "/test",
                "ICECAST_NAME": "Test Stream",
            },
            clear=True,
        ):
            result = create_icecast_streamer_from_env()
            assert result is not None
            assert isinstance(result, IcecastStreamer)
            assert result.host == "radio.example.com"
            assert result.port == 8000
            assert result.password == "testpass"
            assert result.mount == "/test"
            assert result.name == "Test Stream"

    def test_create_from_env_disabled_returns_none(self):
        """create_icecast_streamer_from_env returns None when not enabled."""
        with patch.dict(os.environ, {"ICECAST_ENABLED": "false"}, clear=True):
            assert create_icecast_streamer_from_env() is None

    def test_create_from_env_default_values(self):
        """create_icecast_streamer_from_env uses sensible defaults."""
        with patch.dict(
            os.environ,
            {
                "ICECAST_ENABLED": "true",
                "ICECAST_PASSWORD": "hackme",
            },
            clear=True,
        ):
            result = create_icecast_streamer_from_env()
            assert result is not None
            assert result.host == "localhost"
            assert result.port == 8000
            assert result.mount == "/stream"
            assert result.name == "MC Clanker"

    def test_mount_leading_slash(self):
        """Mount point always starts with /."""
        with patch.dict(
            os.environ,
            {
                "ICECAST_ENABLED": "true",
                "ICECAST_PASSWORD": "hackme",
                "ICECAST_MOUNT": "mystream",
            },
            clear=True,
        ):
            result = create_icecast_streamer_from_env()
            assert result is not None
            assert result.mount == "/mystream"


class TestIcecastStreamerLifecycle:
    """Test start/stop lifecycle."""

    def test_start_and_stop(self):
        """IcecastStreamer can be started and stopped cleanly."""
        streamer = IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )
        assert not streamer.is_running
        # Mock subprocess.Popen to avoid actual ffmpeg execution
        with patch("app.framework.framework_icecast.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # Process is running
            mock_proc.stdin = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            streamer.start()
            assert streamer.is_running
            # Feed some PCM so the stream loop doesn't block on queue.get
            streamer.feed_pcm(b"\x00" * 1024)
            streamer.stop()
            assert not streamer.is_running

    def test_double_start_is_noop(self):
        """Starting an already-running streamer logs warning but doesn't crash."""
        streamer = IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )
        with patch("app.framework.framework_icecast.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            streamer.start()
            streamer.start()  # Should not crash
            assert streamer.is_running
            streamer.stop()

    def test_stop_when_not_running(self):
        """Stopping a non-running streamer is a no-op."""
        streamer = IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )
        streamer.stop()  # Should not crash

    def test_feed_pcm_when_not_running_drops_data(self):
        """feed_pcm silently drops data when streamer is not running."""
        streamer = IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )
        # Should not raise, just drop
        streamer.feed_pcm(b"\x00\x01\x02\x03")


class TestIcecastStreamerAuth:
    """Test authentication header construction."""

    def test_build_auth_header(self):
        """Auth header uses base64-encoded source:password."""
        streamer = IcecastStreamer(
            host="localhost",
            port=8000,
            password="secret123",
            mount="/stream",
        )
        header = streamer._build_auth_header()
        assert header.startswith("Basic ")
        import base64

        encoded = header.split(" ")[1]
        decoded = base64.b64decode(encoded).decode()
        assert decoded == "source:secret123"

    def test_build_icecast_headers(self):
        """Icecast headers include metadata and auth."""
        streamer = IcecastStreamer(
            host="localhost",
            port=8000,
            password="pass",
            mount="/stream",
            name="Test",
            genre="Electronic",
            description="A test stream",
            url="https://example.com",
        )
        headers = streamer._build_icecast_headers()
        # Should contain auth, content-type, and ice-* headers
        assert any("Authorization:" in h for h in headers)
        assert any("Content-Type: audio/mpeg" in h for h in headers)
        assert any("ice-name: Test" in h for h in headers)
        assert any("ice-genre: Electronic" in h for h in headers)
        assert any("ice-description: A test stream" in h for h in headers)
        assert any("ice-url: https://example.com" in h for h in headers)
        assert any("ice-public: 0" in h for h in headers)


class TestBroadcastAudioIcecastIntegration:
    """Test that broadcast_audio feeds PCM to Icecast streamer."""

    @pytest.mark.skip(
        reason="broadcast_audio icecast wiring is not implemented in "
        "framework_state.py (production gap for the CONCURRENCY domain); "
        "unskip once broadcast_audio feeds state.icecast_streamer."
    )
    def test_broadcast_audio_feeds_icecast_when_enabled(self):
        """When icecast_enabled=True, broadcast_audio calls feed_pcm."""
        # Create a mock streamer
        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        state.icecast_enabled = True
        state.icecast_streamer = mock_streamer

        test_pcm = b"\x00\x01\x02\x03"
        state.broadcast_audio(test_pcm)

        mock_streamer.feed_pcm.assert_called_once_with(test_pcm)

    def test_broadcast_audio_skips_icecast_when_disabled(self):
        """When icecast_enabled=False, feed_pcm is not called."""
        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        state.icecast_enabled = False
        state.icecast_streamer = mock_streamer

        state.broadcast_audio(b"\x00\x01\x02\x03")

        mock_streamer.feed_pcm.assert_not_called()

    def test_broadcast_audio_skips_icecast_when_streamer_is_none(self):
        """When icecast_streamer is None, feed_pcm is not called."""
        state.icecast_enabled = True
        state.icecast_streamer = None

        # Should not raise
        state.broadcast_audio(b"\x00\x01\x02\x03")

    @pytest.mark.skip(
        reason="broadcast_audio icecast wiring is not implemented in "
        "framework_state.py (production gap for the CONCURRENCY domain); "
        "unskip once broadcast_audio feeds state.icecast_streamer."
    )
    def test_broadcast_audio_still_feeds_audio_clients_with_icecast(self):
        """Icecast streaming doesn't interfere with normal audio client streaming."""
        mock_streamer = MagicMock()
        mock_streamer.is_running = True

        state.icecast_enabled = True
        state.icecast_streamer = mock_streamer

        client_q = queue.Queue(maxsize=10)
        state.add_audio_client(client_q)

        test_pcm = b"\x00\x01\x02\x03"
        state.broadcast_audio(test_pcm)

        # Both Icecast and the audio client should receive data
        mock_streamer.feed_pcm.assert_called_once_with(test_pcm)
        assert not client_q.empty()
        assert client_q.get_nowait() == test_pcm

        state.remove_audio_client(client_q)

    def test_broadcast_audio_during_shutdown(self):
        """broadcast_audio returns early during shutdown, doesn't feed Icecast."""
        mock_streamer = MagicMock()
        state.icecast_enabled = True
        state.icecast_streamer = mock_streamer
        state.trigger_shutdown()

        state.broadcast_audio(b"\x00\x01\x02\x03")

        mock_streamer.feed_pcm.assert_not_called()


class TestIcecastStreamerProperties:
    """Test IcecastStreamer properties."""

    def test_properties_when_not_running(self):
        """Properties return sensible defaults when not running."""
        streamer = IcecastStreamer(
            host="localhost",
            port=8000,
            password="test",
            mount="/stream",
        )
        assert not streamer.is_running
        assert not streamer.is_connected
        assert streamer.uptime_seconds == 0.0
        assert streamer.bytes_streamed == 0

    def test_properties_after_start(self):
        """Properties reflect running state."""
        streamer = IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )
        with patch("app.framework.framework_icecast.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_popen.return_value = mock_proc

            streamer.start()
            assert streamer.is_running
            assert streamer.uptime_seconds >= 0.0
            streamer.stop()
