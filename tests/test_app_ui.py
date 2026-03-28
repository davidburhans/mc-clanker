import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import queue
import base64


class TestAuthMiddleware:
    """Test authentication middleware logic."""

    def test_no_password_set_passes_through(self):
        """When no password is set, all requests pass through."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = ""
        mock_state.audience_password = ""

        with patch('app_ui.state', mock_state):
            from app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/api/state"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                middleware.dispatch(request, mock_call_next)
            )
            assert result.status_code == 200

    def test_dj_route_requires_auth_without_credentials(self):
        """DJ routes should require auth when password is set."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch('app_ui.state', mock_state):
            from app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/dj"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                middleware.dispatch(request, mock_call_next)
            )
            assert result.status_code == 401

    def test_audience_route_requires_auth_without_credentials(self):
        """Audience routes should require auth when password is set."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = ""
        mock_state.audience_password = "audience_secret"

        with patch('app_ui.state', mock_state):
            from app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                middleware.dispatch(request, mock_call_next)
            )
            assert result.status_code == 401

    def test_valid_credentials_pass_through(self):
        """Valid credentials should pass through."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch('app_ui.state', mock_state):
            from app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/dj"
            request.method = "GET"
            creds = base64.b64encode(b"user:secret").decode("utf-8")
            request.headers.get.return_value = f"Basic {creds}"

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                middleware.dispatch(request, mock_call_next)
            )
            assert result.status_code == 200

    def test_non_protected_routes_bypass_auth(self):
        """Non-protected GET routes should bypass auth."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch('app_ui.state', mock_state):
            from app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/api/health"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                middleware.dispatch(request, mock_call_next)
            )
            assert result.status_code == 200


class TestAudioStreamGenerator:
    """Test audio stream generator edge cases."""

    def test_poison_pill_before_first_chunk(self):
        """Generator should handle None poison pill before first chunk."""
        with patch('app_ui.queue.Queue') as mock_queue_class, \
             patch('app_ui.state') as mock_state, \
             patch('app_ui.subprocess') as mock_subprocess:

            mock_queue = MagicMock()
            mock_queue_class.return_value = mock_queue
            mock_queue.get.side_effect = [None]  # Poison pill immediately
            mock_state.add_audio_client = MagicMock()
            mock_state.remove_audio_client = MagicMock()
            mock_state.is_running = True

            from app_ui import audio_stream_generator
            gen = audio_stream_generator()

            # Should not raise and should return immediately
            try:
                next(gen)
            except StopIteration:
                pass

            mock_state.remove_audio_client.assert_called_once()

    def test_timeout_waiting_for_first_chunk(self):
        """Generator should handle timeout waiting for first chunk."""
        with patch('app_ui.queue.Queue') as mock_queue_class, \
             patch('app_ui.state') as mock_state, \
             patch('app_ui.subprocess'):

            mock_queue = MagicMock()
            mock_queue_class.return_value = mock_queue
            mock_queue.get.side_effect = queue.Empty()
            mock_state.add_audio_client = MagicMock()
            mock_state.remove_audio_client = MagicMock()
            mock_state.is_running = True

            from app_ui import audio_stream_generator
            gen = audio_stream_generator()

            # Should not raise and should return on timeout
            try:
                next(gen)
            except StopIteration:
                pass

            mock_state.remove_audio_client.assert_called()


class TestRedirects:
    """Test route redirects."""

    def test_dj_redirect(self):
        """Test /dj redirects to /dj/."""
        from fastapi.testclient import TestClient
        from app_ui import redirect_to_dj_slash
        from fastapi import FastAPI

        app = FastAPI()
        app.add_api_route("/dj", redirect_to_dj_slash, methods=["GET"])

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/dj", follow_redirects=False)

        assert response.status_code == 307


class TestStreamMp3:
    """Test MP3 streaming endpoint."""

    def test_stream_response_headers(self):
        """Test streaming response has correct headers."""
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from app_ui import stream_mp3, audio_stream_generator

        app = FastAPI()
        app.add_api_route("/stream.mp3", stream_mp3, methods=["GET"])

        with patch('app_ui.state') as mock_state, \
             patch('app_ui.audio_stream_generator') as mock_gen:

            mock_state.is_running = True
            mock_state.add_audio_client = MagicMock()
            mock_state.remove_audio_client = MagicMock()
            mock_state.audio_clients = []

            # Mock generator to yield some data then stop
            def mock_generator():
                yield b"fake mp3 data"
                return

            mock_gen.return_value = mock_generator()

            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/stream.mp3")

            # Should be a streaming response
            assert response.status_code == 200
            assert "audio/mpeg" in response.headers.get("content-type", "")


class TestAppUIModule:
    """Tests for app_ui module functions."""

    def test_app_initialization(self):
        """Test that the app is properly initialized."""
        # The app should be a FastAPI instance with middleware
        from app_ui import app

        assert app is not None

    def test_app_has_auth_middleware(self):
        """Test that the app has AuthMiddleware registered."""
        from app_ui import app

        # Check middleware is registered
        middleware = app.user_middleware
        assert len(middleware) > 0
