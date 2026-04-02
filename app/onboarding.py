"""Onboarding — validates configuration and provides setup guidance."""

from __future__ import annotations

import os
import asyncio
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)


class CheckResult(NamedTuple):
    passed: bool
    category: str  # "required" | "recommended" | "optional"
    name: str
    message: str
    hint: str | None = None


# Known weak/default secrets that should be changed in production
_WEAK_JWT_SECRETS = {
    "change-me-in-production",
    "secret",
    "changeme",
    "password",
    "123456",
}


async def check_database() -> CheckResult:
    """Check if the database is reachable."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # SQLite fallback — always available
        return CheckResult(
            passed=True,
            category="required",
            name="database",
            message="Using SQLite (local database)",
            hint=None,
        )

    # Try to connect and run SELECT 1
    try:
        import asyncpg
        # Parse the URL to extract connection params
        # Expected format: postgresql://user:pass@host:port/db
        url = database_url.replace("postgresql://", "")
        parts = url.split("@")
        if len(parts) == 2:
            user_pass, host_db = parts
            user, password = user_pass.split(":")
            host, port_db = host_db.split(":")
            port = int(port_db.split("/")[0])
            db_name = port_db.split("/")[1] if "/" in port_db else "mcclanker"
        else:
            return CheckResult(
                passed=False,
                category="required",
                name="database",
                message="Could not parse DATABASE_URL",
                hint="Expected format: postgresql://user:pass@host:port/db",
            )

        conn = await asyncpg.connect(
            user=user, password=password, host=host, port=port, database=db_name, timeout=5
        )
        await conn.execute("SELECT 1")
        await conn.close()
        return CheckResult(
            passed=True,
            category="required",
            name="database",
            message=f"PostgreSQL reachable at {host}:{port}",
            hint=None,
        )
    except Exception as e:
        return CheckResult(
            passed=False,
            category="required",
            name="database",
            message=f"PostgreSQL not reachable: {e}",
            hint="Set DATABASE_URL or leave unset to use SQLite fallback",
        )


async def check_llm_server() -> CheckResult:
    """Check if the LLM server is reachable at the configured base URL."""
    import httpx

    base_url = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
    # Normalize: strip trailing slash, then try /models endpoint relative to the base
    # (The base URL already includes /v1, so we just append /models - not /v1/models)
    models_url = base_url.rstrip('/') + "/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(models_url)
            if response.status_code == 200:
                return CheckResult(
                    passed=True,
                    category="required",
                    name="llm_server",
                    message=f"LLM server reachable at {base_url}",
                    hint=None,
                )
            else:
                return CheckResult(
                    passed=False,
                    category="required",
                    name="llm_server",
                    message=f"LLM server returned status {response.status_code}: {response.text[:100]}",
                    hint="Set LLM_BASE_URL to your LM Studio or Ollama endpoint",
                )
    except Exception as e:
        return CheckResult(
            passed=False,
            category="required",
            name="llm_server",
            message=f"LLM server not reachable at {base_url}: {e}",
            hint="Set LLM_BASE_URL to your LM Studio or Ollama endpoint",
        )


async def check_garage_s3() -> CheckResult:
    """Check if Garage S3 is reachable."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("GARAGE_ENDPOINT")
    access_key = os.environ.get("GARAGE_ACCESS_KEY")
    secret_key = os.environ.get("GARAGE_SECRET_KEY")
    bucket = os.environ.get("GARAGE_BUCKET", "mcclanker")

    if not all([endpoint, access_key, secret_key]):
        return CheckResult(
            passed=False,
            category="required",
            name="garage_s3",
            message="Garage S3 credentials not configured",
            hint="Set GARAGE_ENDPOINT, GARAGE_ACCESS_KEY, and GARAGE_SECRET_KEY",
        )

    try:
        config = Config(connect_timeout=5, read_timeout=10)
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=config,
        )
        # Just try to list buckets to verify connectivity
        client.list_buckets()
        return CheckResult(
            passed=True,
            category="required",
            name="garage_s3",
            message=f"Garage S3 reachable at {endpoint}, bucket '{bucket}' accessible",
            hint=None,
        )
    except Exception as e:
        return CheckResult(
            passed=False,
            category="required",
            name="garage_s3",
            message=f"Garage S3 not reachable: {e}",
            hint="Check GARAGE_ENDPOINT, GARAGE_ACCESS_KEY, and GARAGE_SECRET_KEY",
        )


def check_jwt_secret() -> CheckResult:
    """Check if JWT_SECRET is using a weak default."""
    secret = os.environ.get("JWT_SECRET", "")
    if not secret or secret.lower() in _WEAK_JWT_SECRETS:
        return CheckResult(
            passed=False,
            category="recommended",
            name="jwt_secret",
            message="JWT_SECRET is using the default value",
            hint="Set JWT_SECRET to a strong random string: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
        )
    return CheckResult(
        passed=True,
        category="recommended",
        name="jwt_secret",
        message="JWT_SECRET is set",
        hint=None,
    )


def check_auth_passwords() -> CheckResult:
    """Check if DJ or audience passwords are set."""
    dj_password = os.environ.get("DJ_PASSWORD", "")
    audience_password = os.environ.get("AUDIENCE_PASSWORD", "")

    if not dj_password and not audience_password:
        return CheckResult(
            passed=False,
            category="recommended",
            name="auth_passwords",
            message="No DJ or audience passwords set — interface is open",
            hint="Set DJ_PASSWORD or AUDIENCE_PASSWORD to protect your session",
        )
    return CheckResult(
        passed=True,
        category="recommended",
        name="auth_passwords",
        message="At least one auth password is set",
        hint=None,
    )


async def run_onboarding_checks() -> list[CheckResult]:
    """Run all onboarding checks concurrently."""
    results = []

    # Run all async checks concurrently
    async_checks = [
        check_database(),
        check_llm_server(),
        check_garage_s3(),
    ]

    # Run sync checks
    sync_checks = [
        check_jwt_secret(),
        check_auth_passwords(),
    ]

    # Gather async results
    async_results = await asyncio.gather(*async_checks, return_exceptions=True)
    for result in async_results:
        if isinstance(result, Exception):
            results.append(
                CheckResult(
                    passed=False,
                    category="required",
                    name="check_error",
                    message=f"Check failed with exception: {result}",
                    hint=None,
                )
            )
        else:
            results.append(result)

    # Add sync results
    results.extend(sync_checks)

    return results


# -----------------------------------------------------------------------------
# Config persistence — write .env file for docker-compose environments
# -----------------------------------------------------------------------------

def write_env_file(values: dict[str, str]) -> None:
    """Write config values to /app/.env (inside the web container).

    The volume mount ../.env:/app/.env:rw in docker-compose.yaml means
    this writes to the host's .env file.
    """
    env_path = os.environ.get("ENV_FILE_PATH", "/app/.env")

    # Merge: existing values preserved unless overridden
    merged: dict[str, str] = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key = line.split("=", 1)[0]
                    if key not in values:
                        merged[key] = line.split("=", 1)[1]

    # Override with new values
    merged.update(values)

    with open(env_path, "w") as f:
        for key, val in merged.items():
            f.write(f"{key}={val}\n")


def restart_services() -> None:
    """Trigger a restart of the web/worker services.

    In docker-compose, this is done via:
    docker-compose restart web worker
    """
    try:
        import subprocess
        compose_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docker")
        subprocess.run(
            ["docker", "compose", "-f", "compose.yaml", "restart", "web", "worker"],
            cwd=compose_dir,
            check=False,
        )
    except Exception:
        pass
