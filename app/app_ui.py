from fastapi import FastAPI
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import threading
import queue
import uvicorn
import os
import subprocess
import socket
import uuid

from app.framework.framework_state import state
from app.framework.framework_main_async import run_framework_loop_async
from app.api_routes import router as api_router
from contextlib import asynccontextmanager, suppress
import atexit


# =============================================================================
# SERVER ID CONFIGURATION (Phase 3: Session Affinity)
# =============================================================================
def get_server_id() -> str:
    """
    Get or generate the unique server ID for this instance.

    Priority:
    1. SERVER_ID environment variable (useful in Kubernetes/docker-compose)
    2. HOSTNAME environment variable (common in container environments)
    3. Generate a UUID and store it in a local file (for persistent IDs)
    """
    # Check environment variables first
    server_id = os.environ.get("SERVER_ID")
    if server_id:
        return server_id

    hostname = os.environ.get("HOSTNAME")
    if hostname:
        return f"server-{hostname}"

    # Try to use hostname
    try:
        hostname = socket.gethostname()
        return f"server-{hostname}"
    except Exception:
        pass

    # Fall back to a generated UUID (not persistent across restarts)
    return f"server-{uuid.uuid4().hex[:8]}"


# Global server ID - set at module load time
current_server_id = get_server_id()
print(f"SESSION AFFINITY: This server's ID is: {current_server_id}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    print("FASTAPI LIFESPAN: Initializing database...")
    from app.db import DatabaseManager
    db_manager = DatabaseManager.get_instance()
    db_manager.create_tables()

    print("FASTAPI LIFESPAN: Running onboarding checks...")
    try:
        from app.onboarding import run_onboarding_checks
        results = await run_onboarding_checks()
        failed_required = [r for r in results if not r.passed and r.category == "required"]
        if failed_required:
            print(f"WARNING: {len(failed_required)} required onboarding checks failed:")
            for r in failed_required:
                print(f"  - {r.name}: {r.message}")
        else:
            print("All required onboarding checks passed")
    except Exception as e:
        print(f"WARNING: Onboarding check error (non-fatal): {e}")

    print("FASTAPI LIFESPAN: Starting framework loop...")
    # Generate a session ID for this app instance
    app_session_id = uuid.uuid4()
    print(f"SESSION AFFINITY: App session ID: {app_session_id}")

    # Start the async framework loop as a background task
    framework_task = asyncio.create_task(run_framework_loop_async(app_session_id))

    # Store the task for proper shutdown
    state.framework_task = framework_task

    yield
    # Shutdown logic
    print("FASTAPI LIFESPAN: Shutting down resources...")
    state.trigger_shutdown()

    # Cancel the async framework task
    framework_task.cancel()
    with suppress(asyncio.CancelledError):
        await framework_task

def cleanup():
    print("Application exiting, cleaning up...")
    state.is_running = False

atexit.register(cleanup)

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, Response
import base64
import re


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Import here to avoid circular imports
        from app.auth import decode_token
        from app.db import DatabaseManager
        from app.models import Show

        auth_header = request.headers.get("Authorization")
        current_user = None

        # Try JWT Bearer token first
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            payload = decode_token(token)
            if payload and "sub" in payload:
                user_id = int(payload["sub"])
                db_manager = DatabaseManager.get_instance()
                with db_manager.session() as session:
                    user = session.query(User).filter(User.id == user_id).first()
                    if user and user.is_active:
                        # Attach user to request state
                        request.state.user = user
                        current_user = user

        # If not JWT, try HTTP Basic auth with env vars (backwards compatibility)
        dj_pass = getattr(state, "dj_password", "")
        aud_pass = getattr(state, "audience_password", "")

        if current_user is None:
            if not dj_pass and not aud_pass:
                # No auth configured
                return await call_next(request)

            provided_pass = None
            if auth_header and auth_header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                    if ":" in decoded:
                        _, provided_pass = decoded.split(":", 1)
                except Exception:
                    pass

            if provided_pass is None:
                # Check if this route requires auth
                is_dj_route = path.startswith("/dj") or \
                              (path.startswith("/api/") and request.method == "POST") or \
                              path.startswith("/api/llm-config") or \
                              path.startswith("/api/stems")
                is_audience_route = path == "/" or \
                                    path == "/index.html" or \
                                    path == "/styles.css" or \
                                    path == "/app.js" or \
                                    path.startswith("/stream.mp3") or \
                                    (path.startswith("/api/") and request.method == "GET")

                # If auth is required for this route but no credentials provided, reject
                if (is_dj_route and dj_pass) or (is_audience_route and aud_pass):
                    return Response(
                        "Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Restricted"'},
                    )

                # Create a pseudo-user for backwards compat when env vars are set
                class CompatUser:
                    id = 0
                    username = "djCompat"
                    email = "compat@local"
                    is_active = True
                    def to_dict(self):
                        return {"id": 0, "username": "djCompat", "email": "compat@local"}

                request.state.user = CompatUser()
                current_user = CompatUser()

        def needs_auth(realm="Restricted"):
            return Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
            )

        # Check for per-show audience password in path
        # Pattern: /api/shows/{id}/playback/* or /api/shows/{id}/audio
        show_password_match = re.match(r"^/api/shows/(\d+)/(playback|audio)(\/.*)?$", path)
        if show_password_match:
            show_id = int(show_password_match.group(1))
            db_manager = DatabaseManager.get_instance()
            with db_manager.session() as session:
                show = session.query(Show).filter(Show.id == show_id).first()
                if show and show.audience_password_hash:
                    # Extract password from Basic auth
                    provided_pass = None
                    if auth_header and auth_header.startswith("Basic "):
                        try:
                            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                            if ":" in decoded:
                                _, provided_pass = decoded.split(":", 1)
                        except Exception:
                            pass

                    if provided_pass:
                        from app.auth import verify_password
                        if not verify_password(provided_pass, show.audience_password_hash):
                            return needs_auth(f"Show {show_id}")
                    else:
                        return needs_auth(f"Show {show_id}")
                elif show:
                    # Show exists but no password - allow access
                    pass
                else:
                    # Show not found - let the route handle 404
                    pass

        return await call_next(request)


class SessionAffinityMiddleware(BaseHTTPMiddleware):
    """
    Middleware for session affinity - redirects requests to the correct server.

    When a session is handled by a different server, this middleware redirects
    the request to that server. This ensures sticky sessions even when the
    load balancer doesn't support cookie-based affinity.
    """

    # Paths that should NOT be redirected (no session affinity needed)
    EXEMPT_PATHS = {
        "/",
        "/dj",
        "/dj/",
        "/stream.mp3",
        "/index.html",
        "/styles.css",
        "/app.js",
        "/api/health",
        "/api/state",
    }

    # Path prefixes that should NOT be redirected
    EXEMPT_PREFIXES = (
        "/static/",
        "/dj/static/",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip exempt paths
        if path in self.EXEMPT_PATHS:
            return await call_next(request)

        # Skip exempt prefixes
        if any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
            return await call_next(request)

        # Extract session_id from path parameters
        # Pattern: /api/sessions/{session_id}/...
        session_id = None
        path_parts = path.split("/")

        # Check if this looks like a session route
        # /api/sessions/{uuid}/... or /sessions/{uuid}/...
        if len(path_parts) >= 3:
            if path_parts[1] == "api" and path_parts[2] == "sessions":
                session_id = path_parts[3] if len(path_parts) > 3 else None
            elif path_parts[1] == "sessions":
                session_id = path_parts[2] if len(path_parts) > 2 else None

        if not session_id:
            # Not a session route, skip
            return await call_next(request)

        # Validate that session_id looks like a UUID
        try:
            uuid.UUID(session_id)
        except ValueError:
            # Not a valid UUID, skip
            return await call_next(request)

        # Look up which server handles this session
        from app.db import DatabaseManager
        from sqlalchemy import text

        db_manager = DatabaseManager.get_instance()

        try:
            with db_manager.session() as session:
                result = session.execute(
                    text("""
                        SELECT server_id FROM session_routing
                        WHERE session_id = :session_id
                    """),
                    {"session_id": session_id}
                ).fetchone()

                if result is None:
                    # No routing entry yet, let the request proceed
                    # (session might not have started yet)
                    return await call_next(request)

                routing_server_id = result[0]

                # If this server is not the routing server, redirect
                if routing_server_id != current_server_id:
                    # Build redirect URL
                    # Use the scheme from the request, or default to http
                    scheme = request.url.scheme or "http"
                    redirect_url = f"{scheme}://{routing_server_id}/{'/'.join(path_parts[1:])}"

                    # Preserve query string
                    if request.url.query:
                        redirect_url += f"?{request.url.query}"

                    print(f"SESSION AFFINITY: Redirecting {path} to {redirect_url}")
                    return RedirectResponse(url=redirect_url, status_code=307)

        except Exception as e:
            # If there's a database error, log it but don't block the request
            print(f"SESSION AFFINITY: Error looking up routing: {e}")

        return await call_next(request)


# 3. Build FastAPI App
app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionAffinityMiddleware)

# Register API routes for DJ UI
app.include_router(api_router)

# Mount static files for DJ UI
static_dir = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "static", "mc-clanker"
)
if os.path.exists(static_dir):
    app.mount("/dj", StaticFiles(directory=static_dir, html=True), name="dj_ui")


@app.get("/dj")
def redirect_to_dj_slash():
    return RedirectResponse(url="/dj/")


@app.get("/setup")
def serve_setup():
    """Serve the setup/onboarding wizard."""
    import pathlib
    setup_path = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "static", "mc-clanker", "setup.html"
    )
    if os.path.exists(setup_path):
        from fastapi.responses import FileResponse
        return FileResponse(setup_path, media_type="text/html")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("Setup page not found", status_code=404)


@app.get("/api/onboarding")
async def onboarding_check():
    """Proxy to onboarding module — checks config and returns status."""
    from app.onboarding import run_onboarding_checks
    from fastapi.responses import JSONResponse
    results = await run_onboarding_checks()
    required_failed = [r for r in results if not r.passed and r.category == "required"]
    return JSONResponse({
        "ready": len(required_failed) == 0,
        "checks": [r._asdict() for r in results],
    })


@app.post("/api/setup/config")
async def save_setup_config(request: Request):
    """Persist config to /app/.env and restart services."""
    from app.onboarding import write_env_file, restart_services
    from fastapi.responses import JSONResponse

    body = await request.json()
    # Filter out empty strings
    values = {k: v for k, v in body.items() if v and v != ""}
    if not values:
        return JSONResponse({"status": "ok", "restarting": False})

    try:
        write_env_file(values)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    restart_services()
    return JSONResponse({"status": "ok", "restarting": True})


def audio_stream_generator():
    """Generator that yields infinite MP3 stream using ffmpeg for transcoding"""
    print("DEBUG: audio_stream_generator() called")
    client_q = queue.Queue(maxsize=100)
    state.add_audio_client(client_q)

    ffmpeg_exe = "/usr/bin/ffmpeg"
    if not os.path.exists(ffmpeg_exe):
        ffmpeg_exe = "ffmpeg"

    # Check if ffmpeg exists and has libmp3lame
    try:
        check = subprocess.run(
            [ffmpeg_exe, "-codecs"], capture_output=True, text=True, timeout=5
        )
        if "libmp3lame" not in check.stdout:
            print(
                "WARNING: ffmpeg does not have libmp3lame encoder. MP3 streaming may not work."
            )
    except Exception as e:
        print(f"WARNING: Could not verify ffmpeg capabilities: {e}")

    # Wait for actual audio data before starting FFmpeg
    # This prevents the browser from timing out waiting for MP3 frames
    # when is_generating=false (only silence being produced)
    try:
        first_chunk = client_q.get(timeout=300.0)
        if first_chunk is None:
            print("DEBUG: audio_stream_generator received poison pill before first chunk")
            state.remove_audio_client(client_q)
            return
    except queue.Empty:
        print("DEBUG: audio_stream_generator timed out waiting for first audio chunk")
        state.remove_audio_client(client_q)
        return

    # ffmpeg tuned for low-latency streaming
    ffmpeg_cmd = [
        ffmpeg_exe,
        "-y",
        "-f",
        "s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "192k",
        "pipe:1",
    ]

    print(f"Starting audio stream with ffmpeg: {' '.join(ffmpeg_cmd)}")
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Pre-feed the first chunk we already received
    try:
        process.stdin.write(first_chunk)
        process.stdin.flush()
        print("DEBUG: Pre-fed first audio chunk to ffmpeg")
    except Exception as e:
        print(f"Warning: Could not pre-feed first chunk to ffmpeg: {e}")
        state.remove_audio_client(client_q)
        return

    def feeder():
        try:
            while state.is_running:
                try:
                    chunk = client_q.get(timeout=1.0)
                    if chunk is None: # Poison pill
                        print("DEBUG: Feeder received poison pill")
                        break
                    if process.poll() is not None:
                        stderr = (
                            process.stderr.read().decode() if process.stderr else ""
                        )
                        print(f"FFmpeg process died. stderr: {stderr}")
                        break
                    process.stdin.write(chunk)
                    process.stdin.flush()
                except (queue.Empty, BrokenPipeError):
                    if not state.is_running:
                        break
                    continue
                except Exception as e:
                    print(f"Feeder error: {e}")
                    break
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass
            stderr = process.stderr.read().decode() if process.stderr else ""
            if stderr:
                print(f"FFmpeg stderr: {stderr}")

    # Register for cleanup
    state.register_subprocess(process)
    
    threading.Thread(target=feeder, daemon=True).start()

    try:
        bytes_yielded = 0
        initial_read = False
        while state.is_running:
            data = process.stdout.read(4096)
            if not data:
                stderr = process.stderr.read().decode() if process.stderr else ""
                print(f"FFmpeg stdout ended. stderr: {stderr}")
                break
            if not initial_read:
                print(f"DEBUG: First MP3 data received: {len(data)} bytes")
                initial_read = True
            bytes_yielded += len(data)
            yield data
        print(f"Audio stream ended. Total bytes yielded: {bytes_yielded}")
    finally:
        state.remove_audio_client(client_q)
        state.unregister_subprocess(process)
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass
        print("Audio stream generator closed")


@app.get("/stream.mp3")
def stream_mp3():
    return StreamingResponse(
        audio_stream_generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Accept-Ranges": "bytes",
        },
    )



# Mount static files for Audience UI at root
audience_dir = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "static", "audience"
)
if os.path.exists(audience_dir):
    app.mount("/", StaticFiles(directory=audience_dir, html=True), name="audience_ui")


if __name__ == "__main__":
    class CustomServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame) -> None:
            print(f"CustomServer: Caught signal {sig}. Triggering state shutdown.")
            state.trigger_shutdown()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host="0.0.0.0", port=7860)
    server = CustomServer(config)
    server.run()
