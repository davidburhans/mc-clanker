import base64
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


class TestAuthMiddleware:
    """Test authentication middleware logic."""

    def test_no_password_set_passes_through(self):
        """When no password is set, all requests pass through."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = ""
        mock_state.audience_password = ""

        with patch("app.app_ui.state", mock_state):
            from app.app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/api/state"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio

            result = asyncio.run(middleware.dispatch(request, mock_call_next))
            assert result.status_code == 200

    def test_dj_route_requires_auth_without_credentials(self):
        """DJ routes should require auth when password is set."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch("app.app_ui.state", mock_state):
            from app.app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/dj"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio

            result = asyncio.run(middleware.dispatch(request, mock_call_next))
            assert result.status_code == 401

    def test_audience_route_requires_auth_without_credentials(self):
        """Audience routes should require auth when password is set."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = ""
        mock_state.audience_password = "audience_secret"

        with patch("app.app_ui.state", mock_state):
            from app.app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio

            result = asyncio.run(middleware.dispatch(request, mock_call_next))
            assert result.status_code == 401

    def test_valid_credentials_pass_through(self):
        """Valid credentials should pass through."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch("app.app_ui.state", mock_state):
            from app.app_ui import AuthMiddleware

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

            result = asyncio.run(middleware.dispatch(request, mock_call_next))
            assert result.status_code == 200

    def test_non_protected_routes_bypass_auth(self):
        """Non-protected GET routes should bypass auth."""
        from fastapi import Response

        mock_state = MagicMock()
        mock_state.dj_password = "secret"
        mock_state.audience_password = ""

        with patch("app.app_ui.state", mock_state):
            from app.app_ui import AuthMiddleware

            app_mock = MagicMock()
            middleware = AuthMiddleware(app_mock)

            async def mock_call_next(request):
                return Response("OK", status_code=200)

            request = MagicMock()
            request.url.path = "/api/health"
            request.method = "GET"
            request.headers.get.return_value = None

            import asyncio

            result = asyncio.run(middleware.dispatch(request, mock_call_next))
            assert result.status_code == 200


class TestAudioStreamGeneratorLegacyContracts:
    """Legacy first-chunk contracts moved to the fan-out architecture (REL-10).

    The old per-client ``audio_stream_generator`` (client-owned ffmpeg, queue
    patching, first-chunk wait) was replaced by the process-wide fan-out. Its
    sentinel/poll-timeout/teardown semantics are pinned in
    tests/test_stream_fanout.py (spawn-failure retire, sentinel residual
    flush, is_running teardown) and the route contract lives in
    TestStreamRoute there. Nothing left to pin here directly.
    """


class TestRedirects:
    """Test route redirects."""

    def test_dj_redirect(self):
        """Test /dj redirects to /dj/ (handled by StaticFiles)."""
        import os
        import tempfile

        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.testclient import TestClient

        # Create a minimal static dir with index.html
        with tempfile.TemporaryDirectory() as tmpdir:
            static_dir = os.path.join(tmpdir, "mc-clanker")
            os.makedirs(static_dir)
            with open(os.path.join(static_dir, "index.html"), "w") as f:
                f.write("<html>DJ UI</html>")

            app = FastAPI()
            app.mount("/dj", StaticFiles(directory=static_dir, html=True), name="dj_ui")

            client = TestClient(app, raise_server_exceptions=False)
            # /dj should redirect to /dj/
            response = client.get("/dj", follow_redirects=False)
            assert response.status_code == 307
            # Location might be relative "/dj/" or absolute "http://testserver/dj/"
            location = response.headers["location"]
            assert location.endswith("/dj/"), f"Expected location ending with /dj/, got {location}"


class TestStreamMp3:
    """Test MP3 streaming endpoint."""

    def test_stream_response_headers(self):
        """Route contract: content-type + no-cache headers on the REAL seam.

        REL-10: the route now streams from the process-wide fan-out via
        mp3_client_stream; a finite stand-in generator keeps this test
        deterministic while the header contract stays pinned at route level.
        """
        from fastapi import FastAPI

        from app.app_ui import stream_mp3

        app = FastAPI()
        app.add_api_route("/stream.mp3", stream_mp3, methods=["GET"])

        def finite_stand_in(_state):
            yield b"fake mp3 data"

        with patch("app.app_ui.mp3_client_stream", finite_stand_in):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/stream.mp3")

        assert response.status_code == 200
        assert "audio/mpeg" in response.headers.get("content-type", "")
        assert response.headers.get("cache-control") == "no-cache, no-store, must-revalidate"
        assert response.headers.get("pragma") == "no-cache"
        assert response.headers.get("expires") == "0"
        assert response.headers.get("accept-ranges") == "bytes"
        assert b"fake mp3 data" in response.content



class TestAppUIModule:
    """Tests for app_ui module functions."""

    def test_app_initialization(self):
        """Test that the app is properly initialized."""
        # The app should be a FastAPI instance with middleware
        from app.app_ui import app

        assert app is not None

    def test_app_has_auth_middleware(self):
        """Test that the app has AuthMiddleware registered."""
        from app.app_ui import app

        # Check middleware is registered
        middleware = app.user_middleware
        assert len(middleware) > 0
