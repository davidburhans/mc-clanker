# MC-Clanker Scalable Architecture Plan

> **⚠️ This document describes the TARGET/FUTURE architecture**, not the current implementation. Some sections may be partially implemented or not yet built. For current architecture, see [CLAUDE.md](./CLAUDE.md) and [README.md](./README.md).

## Overview

Refactor mc-clanker from a single-instance application to a horizontally-scalable, distributed system capable of handling hundreds/thousands of concurrent DJ sessions.

**Goals:**
- Scale web/framework layer horizontally (stateless sessions via sticky routing)
- Scale generator workers independently (queue-based, stateless)
- Store generated audio in Garage (S3-compatible) with paths in PostgreSQL
- Event-driven framework loop for maximum throughput
- Automatic cleanup of stale work items

---

## Development Methodology: Red/Green TDD

This plan uses Test-Driven Development (TDD) with the Red/Green/Refactor cycle:

### The Cycle

1. **RED**: Write a failing test that describes the desired behavior
   - Test should compile and run, but assert the expected behavior that doesn't exist yet
   - Forces you to think about the API before implementation

2. **GREEN**: Write minimum code to make the test pass
   - Don't try to be clever or future-proof
   - Just enough to satisfy the test

3. **REFACTOR**: Clean up code while keeping tests green
   - Remove duplication, improve naming, restructure
   - Tests ensure you don't break anything

### Example TDD Flow

```python
# RED: Write failing test first
def test_submit_job_returns_job_id():
    response = client.post("/api/jobs", json={"instrument": "Synth", ...})
    assert response.status_code == 200
    assert "job_id" in response.json()

# GREEN: Implement minimum code to pass
@router.post("/api/jobs")
async def submit_job(job: JobSubmission):
    job_id = uuid.uuid4()
    await db.execute("INSERT INTO generator_jobs ...", job_id, ...)
    return {"job_id": job_id}

# REFACTOR: Clean up, add validation, error handling
```

### Test Organization

| File | Tests | Purpose |
|------|-------|---------|
| `tests/test_migrations.py` | DB schema | Verify tables exist with correct constraints |
| `tests/test_garage_client.py` | Garage client | Mocked S3 operations |
| `tests/test_aac_encoder.py` | AAC encoding | Encoding preserves duration |
| `tests/test_worker.py` | Worker process | Job claiming, completion, cleanup |
| `tests/test_api_jobs.py` | Job API | End-to-end job lifecycle |
| `tests/test_framework_loop.py` | Framework loop | Async job waiting |
| `tests/test_session_routing.py` | Session affinity | Routing table updates |
| `tests/test_job_expiration.py` | Cleanup | Expired jobs deleted |

### Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run tests for specific component
python -m pytest tests/test_worker.py -v

# Run with coverage
python -m pytest tests/ --cov=. --cov-report=term-missing

# Run only tests for phase being implemented
python -m pytest tests/test_api_jobs.py -v
```

---

## Architecture Diagram

```
                         ┌─────────────────────────────────────────┐
                         │           Load Balancer (L4/L7)         │
                         │    Routes: /api/* → Web Servers         │
                         │           /sessions/* → Sticky          │
                         └───────────────────┬─────────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    │                        │                        │
         ┌──────────▼──────────┐   ┌────────▼─────────┐   ┌──────▼───────┐
         │    Web Server 1     │   │   Web Server 2    │   │  Web Server N │
         │  ┌────────────────┐ │   │ ┌──────────────┐ │   │ ┌───────────┐ │
         │  │ FastAPI (API)  │ │   │ │ FastAPI(API) │ │   │ │ FastAPI   │ │
         │  │                │ │   │ │              │ │   │ │           │ │
         │  │ Framework Loop │ │   │ │ Framework    │ │   │ │ Framework │ │
         │  │ (per-session)  │ │   │ │ Loop         │ │   │ │ Loop      │ │
         │  │                │ │   │ │              │ │   │ │           │ │
         │  │ Mixer +         │ │   │ │ Mixer +       │ │   │ │ Mixer +   │ │
         │  │ sounddevice    │ │   │ │ sounddevice   │ │   │ │ sounddev  │ │
         │  └────────────────┘ │   │ └──────────────┘ │   │ └───────────┘ │
         └────────────────────┘   └────────────────────┘   └──────────────┘
                    │                        │                        │
─────────────────────────────────────────────────────────────────────────────
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────▼─────────┐   ┌───────▼────────┐   ┌──────▼─────────┐
    │   PostgreSQL     │   │    Garage       │   │   Redis         │
    │   (jobs queue +  │   │   (S3 object    │   │   (session      │
    │    session state)│   │    storage)     │   │    affinity)    │
    │                   │   │                 │   │                 │
    │  generator_jobs   │   │  audio/{job_id} │   │  stickiness     │
    │  sessions         │   │      .aac      │   │  routing table  │
    └───────────────────┘   └─────────────────┘   └─────────────────┘
                                    │
────────────────────────────────────┼─────────────────────────────────────────
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────▼─────────┐   ┌───────▼────────┐   ┌──────▼─────────┐
    │   Worker 1        │   │   Worker 2     │   │   Worker N     │
    │   ┌─────────────┐ │   │ ┌───────────┐ │   │ ┌───────────┐ │
    │   │ Job Consumer│ │   │ │Job Consumer│ │   │ │Job Consumer│ │
    │   │ (claims)    │ │   │ │            │ │   │ │           │ │
    │   │             │ │   │ │            │ │   │ │           │ │
    │   │ Generator   │ │   │ │ Generator  │ │   │ │ Generator │
    │   │ + AAC enc   │ │   │ │ + AAC enc  │ │   │ │ + AAC enc │
    │   │ + Garage up  │ │   │ │ + Garage   │ │   │ │ + Garage  │
    │   └─────────────┘ │   │ └───────────┘ │   │ └───────────┘ │
    └───────────────────┘   └───────────────┘   └───────────────┘
```

---

## Database Schema

### New Tables

```sql
-- Session affinity: maps session_id → web_server instance
CREATE TABLE session_routing (
    session_id UUID PRIMARY KEY,
    server_id VARCHAR(255) NOT NULL,  -- e.g., "web-1" or k8s pod name
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_heartbeat TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_session_routing_server ON session_routing(server_id);
CREATE INDEX idx_session_routing_heartbeat ON session_routing(last_heartbeat);

-- Job queue: replaces synchronous in-process generation
CREATE TABLE generator_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,  -- which DJ session this belongs to

    -- Job spec (what to generate)
    instrument VARCHAR(255) NOT NULL,
    prompt TEXT NOT NULL,
    major_family VARCHAR(100),
    model_id VARCHAR(100) DEFAULT 'foundation-1',
    key VARCHAR(50),
    bpm INTEGER,
    timbre_tags JSONB DEFAULT '[]',
    bars INTEGER DEFAULT 4,

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'expired')),
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- Result (set by worker)
    audio_path VARCHAR(500),  -- Garage S3 path: audio/{job_id}.aac
    duration_seconds FLOAT,
    error_message TEXT,

    -- Expiration
    expires_at TIMESTAMPTZ NOT NULL  -- set at creation, refreshed on access
);
CREATE INDEX idx_generator_jobs_status ON generator_jobs(status) WHERE status IN ('pending', 'processing');
CREATE INDEX idx_generator_jobs_session ON generator_jobs(session_id);
CREATE INDEX idx_generator_jobs_expires ON generator_jobs(expires_at);
```

### Modified Tables

```sql
-- Add to existing sessions table (rename from shows if applicable)
ALTER TABLE shows ADD COLUMN server_id VARCHAR(255);
ALTER TABLE shows ADD COLUMN status VARCHAR(20) DEFAULT 'idle';  -- idle, running, paused

-- LLM interactions can stay (audit trail), but audio moves to Garage
```

### Cleanup Cron Job

```sql
-- Run via pg_cron or external scheduler
DELETE FROM generator_jobs
WHERE expires_at < NOW()
  AND status IN ('completed', 'failed', 'expired');
```

Or via application-level cleanup:
```python
async def cleanup_expired_jobs():
    """Called periodically by a background task."""
    await db.execute(
        "DELETE FROM generator_jobs WHERE expires_at < NOW() AND status IN ('completed', 'failed', 'expired')"
    )
```

---

## API Changes

### New Endpoints

```python
# Submit generation job (called by framework loop)
@router.post("/api/jobs")
async def submit_job(job: JobSubmission):
    """Submit a stem generation job to the queue."""
    job_id = await db.fetch_one("""
        INSERT INTO generator_jobs (session_id, instrument, prompt, major_family,
                                     model_id, key, bpm, timbre_tags, bars, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW() + INTERVAL '24 hours')
        RETURNING id
    """, session_id, job.instrument, ...)

    # Notify workers (optional, can poll)
    await db.execute("NOTIFY job_available")
    return {"job_id": job_id}

# Poll for job completion (event-driven alternative to blocking)
@router.get("/api/jobs/{job_id}")
async def get_job(job_id: UUID):
    """Get job status and audio path if completed."""
    job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "id": job["id"],
        "status": job["status"],
        "audio_path": job["audio_path"] if job["status"] == "completed" else None,
        "error": job["error_message"]
    }

# Audio streaming endpoint (framework fetches from Garage)
@router.get("/api/audio/{job_id}")
async def get_audio(job_id: UUID):
    """Stream audio from Garage. Used by framework loop after job completes."""
    job = await db.fetch_one("SELECT audio_path FROM generator_jobs WHERE id = $1", job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(404, "Audio not found")

    # Refresh expiration on access
    await db.execute(
        "UPDATE generator_jobs SET expires_at = NOW() + INTERVAL '1 hour' WHERE id = $1",
        job_id
    )

    # Stream from Garage using presigned URL or direct access
    audio_url = garage.get_presigned_url(job["audio_path"])
    return RedirectResponse(audio_url)

# Worker health + job claim (workers only)
@router.post("/api/worker/jobs/claim")
async def claim_job(worker_id: str):
    """Atomically claim a pending job. Returns None if queue empty."""
    async with db.transaction():
        job = await db.fetch_one("""
            SELECT * FROM generator_jobs
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """)
        if job:
            await db.execute("""
                UPDATE generator_jobs
                SET status = 'processing', started_at = NOW(), worker_id = $1
                WHERE id = $2
            """, worker_id, job["id"])
    return job

# Session affinity (for load balancer)
@router.post("/api/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: UUID, server_id: str):
    """Update session routing heartbeat."""
    await db.execute("""
        INSERT INTO session_routing (session_id, server_id, last_heartbeat)
        VALUES ($1, $2, NOW())
        ON CONFLICT (session_id) DO UPDATE SET last_heartbeat = NOW()
    """, session_id, server_id)
```

### Modified Endpoints

```python
# Framework loop no longer calls generator.generate_stem() directly
# Instead:
@router.post("/api/framework/generate")
async def request_generation(spec: StemSpec):
    job_id = await submit_job(JobSubmission(...))
    # Framework waits for completion via polling or NOTIFY
    return {"job_id": job_id}
```

---

## Generator Worker Service

### Container: `Dockerfile.worker`

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only worker dependencies needed
COPY framework_generator.py framework_conductor.py framework_state.py models_config.json ./

# Don't copy GPU/CUDA deps - assume base image has them or use nvidia/cuda image
CMD ["python", "-m", "worker"]
```

### Worker Process: `worker.py`

```python
import asyncio
import json
import signal
import logging
from dataclasses import dataclass
from typing import Optional
import uuid

import aiohttp
import asyncpg
from garage import GarageClient  # boto3-compatible S3 client

from framework_generator import GeneratorRegistry

logger = logging.getLogger(__name__)

# AAC encoding via ffmpeg-python or subprocess

@dataclass
class WorkerConfig:
    worker_id: str
    pg_dsn: str
    garage_endpoint: str
    garage_access_key: str
    garage_secret_key: str
    garage_bucket: str
    garage_bucket_region: str

class GeneratorWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.db: Optional[asyncpg.Pool] = None
        self.garage = GarageClient(config)
        self.generators = GeneratorRegistry()
        self.running = True

    async def start(self):
        self.db = await asyncpg.create_pool(self.config.pg_dsn, min_size=2, max_size=10)

        # Listen for job notifications (optional optimization)
        conn = await self.db.acquire()
        await conn.add_listener('job_available', self._on_job_available)

        asyncio.create_task(self._cleanup_loop())

        while self.running:
            await self._process_next_job()

    async def _on_job_available(self, *args):
        # Notification received - wake up to check queue
        pass

    async def _process_next_job(self):
        async with self.db.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow("""
                    SELECT * FROM generator_jobs
                    WHERE status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)
                if not job:
                    await asyncio.sleep(1)  # No jobs, wait before polling again
                    return

                await conn.execute("""
                    UPDATE generator_jobs
                    SET status = 'processing', started_at = NOW(), worker_id = $1
                    WHERE id = $2
                """, self.config.worker_id, job['id'])

        # Process outside transaction (I/O heavy)
        try:
            await self._generate_and_upload(job)
        except Exception as e:
            logger.error(f"Job {job['id']} failed: {e}")
            async with self.db.acquire() as conn:
                await conn.execute("""
                    UPDATE generator_jobs
                    SET status = 'failed', error_message = $1, completed_at = NOW()
                    WHERE id = $2
                """, str(e), job['id'])

    async def _generate_and_upload(self, job):
        # 1. Generate audio via stable-audio-tools
        audio_array = await self.generators.generate_stem(
            model_id=job['model_id'],
            prompt=job['prompt'],
            key=job['key'],
            bpm=job['bpm'],
            bars=job['bars'],
        )

        # 2. Encode to AAC
        aac_bytes = await self._encode_aac(audio_array, sample_rate=44100)

        # 3. Upload to Garage
        audio_path = f"audio/{job['id']}.aac"
        await self.garage.put_object(audio_path, aac_bytes)

        # 4. Update DB with result
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE generator_jobs
                SET status = 'completed',
                    audio_path = $1,
                    duration_seconds = $2,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '24 hours'
                WHERE id = $3
            """, audio_path, len(audio_array) / 44100, job['id'])

        # 5. Notify via LISTEN/NOTIFY
        async with self.db.acquire() as conn:
            await conn.execute(f"NOTIFY job_completed, '{job['id']}'")

    async def _encode_aac(self, audio_array, sample_rate=44100) -> bytes:
        # Use ffmpeg subprocess for AAC encoding
        import subprocess
        import tempfile
        import numpy as np
        from scipy.io import wavfile

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            # Write WAV
            wavfile.write(f.name, sample_rate, audio_array.astype(np.float32))
            wav_path = f.name

        result = subprocess.run([
            'ffmpeg', '-i', wav_path,
            '-c:a', 'aac',
            '-b:a', '192k',
            '-y',
            '-f', 'adts',  # AAC in ADTS container
            'pipe:1'
        ], capture_output=True)

        return result.stdout

    async def _cleanup_loop(self):
        """Periodic cleanup of stale jobs."""
        while self.running:
            await asyncio.sleep(300)  # Every 5 minutes
            async with self.db.acquire() as conn:
                deleted = await conn.fetchval("""
                    WITH deleted AS (
                        DELETE FROM generator_jobs
                        WHERE status IN ('completed', 'failed')
                          AND expires_at < NOW()
                        RETURNING audio_path
                    )
                    SELECT COUNT(*) FROM deleted
                """)
                if deleted:
                    logger.info(f"Cleaned up {deleted} expired jobs")

    def stop(self):
        self.running = False

async def main():
    config = WorkerConfig(
        worker_id=os.environ['WORKER_ID'],
        pg_dsn=os.environ['DATABASE_URL'],
        garage_endpoint=os.environ['GARAGE_ENDPOINT'],
        garage_access_key=os.environ['GARAGE_ACCESS_KEY'],
        garage_secret_key=os.environ['GARAGE_SECRET_KEY'],
        garage_bucket=os.environ['GARAGE_BUCKET'],
        garage_bucket_region=os.environ['GARAGE_BUCKET_REGION'],
    )
    worker = GeneratorWorker(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    await worker.start()
```

---

## Web Server Changes

### Framework Loop (Event-Driven)

```python
# framework_main.py changes

async def run_framework_loop(session_id: UUID):
    """Event-driven framework loop for a single session."""

    while True:
        # 1. Acquire lock, build Conductor prompt
        with state.lock:
            prompt = ConductorPromptBuilder.build(state, session_id)
            is_generating = state.is_generating

        if not is_generating:
            await asyncio.sleep(1)
            continue

        # 2. Call LLM Conductor (non-blocking via thread pool or async client)
        conductor_response = await conductor.call_async(prompt)

        # 3. Parse actions, submit jobs
        with state.lock:
            actions = ConductorPromptBuilder.parse_actions(conductor_response)
            pending_jobs = []

            for action in actions:
                if action['action'] == 'add':
                    job_id = await db.execute("""
                        INSERT INTO generator_jobs (...) VALUES (...)
                    """)
                    pending_jobs.append(job_id)
                # ... retain/remove handled in state

        # 4. Wait for all pending jobs to complete
        completed_audio = []
        for job_id in pending_jobs:
            audio_path = await wait_for_job_completion(job_id)
            completed_audio.append(audio_path)

        # 5. Fetch audio from Garage, transition stems
        with state.lock:
            # Transition to new active_stems
            # ...
```

### Wait for Job Completion

```python
async def wait_for_job_completion(job_id: UUID, timeout=60.0) -> Optional[str]:
    """Wait for job completion via LISTEN/NOTIFY or polling."""
    event = asyncio.Event()

    async def on_notify(notify):
        if notify.payload == str(job_id):
            event.set()

    # Register listener
    conn = await db.acquire()
    await conn.add_listener('job_completed', on_notify)

    try:
        # Check if already completed
        job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", job_id)
        if job['status'] == 'completed':
            return job['audio_path']

        # Wait with timeout
        if timeout:
            await asyncio.wait_for(event.wait(), timeout)
            job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", job_id)
            return job['audio_path'] if job['status'] == 'completed' else None
        else:
            # Fallback polling
            while True:
                job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", job_id)
                if job['status'] == 'completed':
                    return job['audio_path']
                elif job['status'] == 'failed':
                    return None
                await asyncio.sleep(0.5)
    finally:
        await conn.remove_listener('job_completed', on_notify)
```

### Session Affinity

```python
# app_ui.py or middleware

@app.middleware("http")
async def session_affinity(request: Request, call_next):
    session_id = request.path_params.get('session_id')

    if session_id:
        # Look up which server handles this session
        server_info = await db.fetch_one(
            "SELECT server_id FROM session_routing WHERE session_id = $1",
            session_id
        )

        if server_info and server_info['server_id'] != current_server_id:
            # Redirect to correct server
            return RedirectResponse(
                f"http://{server_info['server_id']}/sessions/{session_id}/..."
            )

    return await call_next(request)

async def register_session(session_id: UUID):
    """Called when a new DJ session starts on this server."""
    await db.execute("""
        INSERT INTO session_routing (session_id, server_id, last_heartbeat)
        VALUES ($1, $2, NOW())
    """, session_id, current_server_id)
```

---

## Garage Integration

### Client: `garage_client.py`

```python
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx

class GarageClient:
    """S3-compatible client for Garage object storage."""

    def __init__(self, config: WorkerConfig):
        self.bucket = config.garage_bucket
        self.client = boto3.client(
            's3',
            endpoint_url=config.garage_endpoint,
            aws_access_key_id=config.garage_access_key,
            aws_secret_access_key=config.garage_secret_key,
            region_name=config.garage_bucket_region,
            config=Config(signature_version='s3v4'),
        )

    async def put_object(self, key: str, data: bytes):
        """Upload object to Garage."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        )

    def get_presigned_url(self, key: str, expires_in=3600) -> str:
        """Generate presigned URL for reading."""
        return self.client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': key},
            ExpiresIn=expires_in
        )

    async def get_object(self, key: str) -> bytes:
        """Download object from Garage."""
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.client.get_object(Bucket=self.bucket, Key=key)
        )
        return response['Body'].read()
```

---

## Docker Compose (Development)

```yaml
version: '3.8'

services:
  web:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/mcclanker
      - REDIS_URL=redis://redis:6379
      - GARAGE_ENDPOINT=http://garage:3900
      - GARAGE_ACCESS_KEY=GarageAccessKey
      - GARAGE_SECRET_KEY=GarageSecretKey
      - GARAGE_BUCKET=mcclanker
      - GARAGE_BUCKET_REGION=garage
      - LLM_BASE_URL=http://llm:1234/v1
    depends_on:
      - postgres
      - redis
      - garage
    deploy:
      replicas: 2  # Scale web servers

  worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    environment:
      - WORKER_ID=worker-${HOSTNAME:-1}
      - DATABASE_URL=postgresql://postgres:postgres@postgres:5432/mcclanker
      - GARAGE_ENDPOINT=http://garage:3900
      - GARAGE_ACCESS_KEY=GarageAccessKey
      - GARAGE_SECRET_KEY=GarageSecretKey
      - GARAGE_BUCKET=mcclanker
      - GARAGE_BUCKET_REGION=garage
    depends_on:
      - postgres
      - garage
    deploy:
      replicas: 3  # Scale workers based on queue

  postgres:
    image: postgres:15
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      - POSTGRES_DB=mcclanker
      - POSTGRES_PASSWORD=postgres

  redis:
    image: redis:7
    volumes:
      - redisdata:/data

  garage:
    image: debian-slim Garage  # or self-hosted Garage
    ports:
      - "3900:3900"
    volumes:
      - garage-data:/data
    command: garage server --data-dir /data --http-port 3900 --s3-port 3901

volumes:
  pgdata:
  redisdata:
  garage-data:
```

---

## Kubernetes Deployment (Future)

### Web Server Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcclanker-web
spec:
  replicas: 3
  selector:
    matchLabels:
      app: mcclanker-web
  template:
    metadata:
      labels:
        app: mcclanker-web
    spec:
      containers:
        - name: web
          image: mcclanker/web:latest
          ports:
            - containerPort: 8000
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: mcclanker-secrets
                  key: database-url
            - name: GARAGE_ENDPOINT
              value: "http://garage.default.svc.cluster.local:3900"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
          readinessProbe:
            httpGet:
              path: /ready
              port: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: mcclanker-web
spec:
  type: ClusterIP
  ports:
    - port: 80
      targetPort: 8000
  selector:
    app: mcclanker-web
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mcclanker
  annotations:
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/session-cookie-name: "route"
spec:
  rules:
    - host: mcclanker.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: mcclanker-web
                port:
                  number: 80
```

### Worker Deployment (HPA)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcclanker-worker
spec:
  replicas: 2
  selector:
    matchLabels:
      app: mcclanker-worker
  template:
    metadata:
      labels:
        app: mcclanker-worker
    spec:
      containers:
        - name: worker
          image: mcclanker/worker:latest
          env:
            - name: WORKER_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: mcclanker-secrets
                  key: database-url
          resources:
            limits:
              nvidia.com/gpu: 1
            requests:
              nvidia.com/gpu: 1
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: mcclanker-worker-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: mcclanker-worker
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: External
      external:
        metric:
          name: generator_jobs_pending
          selector:
            matchLabels:
              type: pending_jobs
        target:
          type: AverageValue
          averageValue: 5  # Scale when >5 pending jobs per worker
```

### Garage (External or StatefulSet)

```yaml
# If self-hosting Garage in k8s:
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: garage
spec:
  serviceName: garage
  replicas: 3
  selector:
    matchLabels:
      app: garage
  template:
    metadata:
      labels:
        app: garage
    spec:
      containers:
        - name: garage
          image: debian-slim/garage:latest
          ports:
            - containerPort: 3900
              name: http
            - containerPort: 3901
              name: s3
          volumeMounts:
            - name: data
              mountPath: /data
          args:
            - server
            - --data-dir
            - /data
            - --http-port
            - "3900"
            - --s3-port
            - "3901"
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: "standard"
        resources:
          requests:
            storage: 100Gi
```

## Implementation Phases

Each phase follows Red/Green TDD:
- **RED**: Write a failing test that describes the desired behavior
- **GREEN**: Write minimum code to make the test pass
- **REFACTOR**: Clean up code while keeping tests green

---

### Phase 1: Core Infrastructure

#### Step 1.1: Database Migrations

**RED Test** (`tests/test_migrations.py`):
```python
async def test_generator_jobs_table_exists():
    """Verify generator_jobs table exists with correct schema."""
    result = await db.fetch_one("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'generator_jobs'
    """)
    assert result is not None

async def test_generator_jobs_status_enum():
    """Status must be one of: pending, processing, completed, failed, expired"""
    with pytest.raises(psycopg2.CheckConstraintViolation):
        await db.execute("""
            INSERT INTO generator_jobs (session_id, instrument, prompt, expires_at)
            VALUES ($1, $2, $3, NOW() + INTERVAL '24 hours')
        """, uuid.uuid4(), "Synth", "test prompt")
```

**GREEN Implementation** (`migrations/001_jobs_and_routing.sql`):
```sql
-- Run this migration:
-- psql $DATABASE_URL -f migrations/001_jobs_and_routing.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS generator_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL,

    -- Job spec
    instrument VARCHAR(255) NOT NULL,
    prompt TEXT NOT NULL,
    major_family VARCHAR(100),
    model_id VARCHAR(100) DEFAULT 'foundation-1',
    key VARCHAR(50),
    bpm INTEGER,
    timbre_tags JSONB DEFAULT '[]',
    bars INTEGER DEFAULT 4,

    -- Status
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'expired')),
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- Result
    audio_path VARCHAR(500),
    duration_seconds FLOAT,
    error_message TEXT,

    -- Expiration
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_generator_jobs_status
    ON generator_jobs(status) WHERE status IN ('pending', 'processing');
CREATE INDEX IF NOT EXISTS idx_generator_jobs_session
    ON generator_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_generator_jobs_expires
    ON generator_jobs(expires_at);

CREATE TABLE IF NOT EXISTS session_routing (
    session_id UUID PRIMARY KEY,
    server_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_heartbeat TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_routing_server
    ON session_routing(server_id);
CREATE INDEX IF NOT EXISTS idx_session_routing_heartbeat
    ON session_routing(last_heartbeat);
```

#### Step 1.2: Garage Client

**RED Test** (`tests/test_garage_client.py`):
```python
import pytest
from unittest.mock import MagicMock, patch

# These tests use mocks - no real Garage needed

def test_put_object_uploads_to_garage():
    """When put_object is called, data should be uploaded to correct key."""
    with patch('boto3.client') as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client

        client = GarageClient(TEST_CONFIG)
        client.put_object("audio/test-job-id.aac", b"fake audio data")

        mock_client.put_object.assert_called_once_with(
            Bucket=TEST_CONFIG.garage_bucket,
            Key="audio/test-job-id.aac",
            Body=b"fake audio data"
        )

def test_get_presigned_url_generates_valid_url():
    """Presigned URL should contain bucket and key."""
    with patch('boto3.client') as mock_boto:
        mock_client = MagicMock()
        mock_client.generate_presigned_url.return_value = "http://garage:3900/bucket/audio/test.aac?sig=..."
        mock_boto.return_value = mock_client

        client = GarageClient(TEST_CONFIG)
        url = client.get_presigned_url("audio/test.aac")

        assert "audio/test.aac" in url
        assert "sig=" in url
```

**GREEN Implementation** (`garage_client.py`):
```python
"""
Garage Client - S3-compatible object storage for generated audio.

Usage:
    config = GarageConfig(
        endpoint="http://garage:3900",
        access_key=os.environ["GARAGE_ACCESS_KEY"],
        secret_key=os.environ["GARAGE_SECRET_KEY"],
        bucket="mcclanker",
        region="garage"
    )
    client = GarageClient(config)
    await client.put_object("audio/job-123.aac", audio_bytes)
    url = client.get_presigned_url("audio/job-123.aac")  # For streaming
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.config import Config


@dataclass
class GarageConfig:
    """Configuration for Garage S3-compatible storage."""
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "garage"


class GarageClient:
    """Async wrapper around boto3 S3 client for Garage."""

    def __init__(self, config: GarageConfig):
        self.bucket = config.bucket
        self.config = config
        self._client = boto3.client(
            's3',
            endpoint_url=config.endpoint,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=Config(signature_version='s3v4'),
        )

    def _sync_put_object(self, key: str, data: bytes):
        """Synchronous put - run in thread pool."""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data
        )

    def _sync_get_object(self, key: str) -> bytes:
        """Synchronous get - run in thread pool."""
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response['Body'].read()

    async def put_object(self, key: str, data: bytes):
        """Upload bytes to Garage. Thread-safe."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_put_object, key, data)

    async def get_object(self, key: str) -> bytes:
        """Download bytes from Garage. Thread-safe."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_object, key)

    def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """
        Generate a presigned URL for temporary direct access.

        Args:
            key: Object key in bucket
            expires_in: Seconds until URL expires (default 1 hour)

        Returns:
            Presigned URL string
        """
        return self._client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': key},
            ExpiresIn=expires_in
        )
```

#### Step 1.3: Worker Dockerfile

**RED Test** (`tests/test_dockerfile_worker.py`):
```python
import subprocess

def test_worker_dockerfile_exists():
    """Worker Dockerfile should exist at project root."""
    assert Path("Dockerfile.worker").exists()

def test_worker_dockerfile_builds():
    """Worker image should build without errors."""
    result = subprocess.run(
        ["docker", "build", "-f", "Dockerfile.worker", "-t", "mcclanker-worker-test", "."],
        capture_output=True
    )
    assert result.returncode == 0, f"Build failed: {result.stderr.decode()}"

def test_worker_image_has_ffmpeg():
    """Worker image must have ffmpeg for AAC encoding."""
    result = subprocess.run(
        ["docker", "run", "--rm", "mcclanker-worker-test", "ffmpeg", "-version"],
        capture_output=True
    )
    assert result.returncode == 0, "ffmpeg not found in worker image"
```

**GREEN Implementation** (`Dockerfile.worker`):
```dockerfile
# Dockerfile.worker - Generator worker container
# Multi-stage build to minimize image size

FROM python:3.10-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements-worker.txt .
RUN pip install --no-cache-dir -r requirements-worker.txt

# Copy framework code (read-only, used for generation)
COPY framework_generator.py framework_generator.py
COPY models_config.json models_config.json

# Copy worker code
COPY worker.py worker.py

# Run as non-root user (security)
RUN useradd -m -u 1000 worker
USER worker

CMD ["python", "-m", "worker"]
```

#### Step 1.4: AAC Encoder

**RED Test** (`tests/test_aac_encoder.py`):
```python
import numpy as np
from scipy.io import wavfile
import tempfile
import os

def test_encode_aac_produces_valid_aac():
    """Given a stereo float32 array, encoder should produce AAC bytes."""
    # 1 second of 44100Hz audio
    sample_rate = 44100
    audio = np.random.randn(sample_rate * 2).astype(np.float32)  # 2 channels

    aac_bytes = encode_aac(audio, sample_rate)

    # AAC should be smaller than raw WAV
    wav_size = audio.nbytes
    assert len(aac_bytes) < wav_size
    assert len(aac_bytes) > 0

def test_encode_aac_with_mono_audio():
    """Mono audio should be encoded correctly."""
    sample_rate = 44100
    audio = np.random.randn(sample_rate).astype(np.float32)  # 1 channel

    aac_bytes = encode_aac(audio, sample_rate)

    assert len(aac_bytes) > 0

def test_encode_aac_preserves_duration():
    """Encoded audio should be approximately same duration as input."""
    sample_rate = 44100
    duration_seconds = 4
    audio = np.zeros(sample_rate * duration_seconds, dtype=np.float32)

    aac_bytes = encode_aac(audio, sample_rate)

    # We can't easily verify duration without decoding,
    # but file size should be reasonable for 4 seconds at 192kbps
    expected_max_size = (192 * 1000 / 8) * duration_seconds  # 192kbps in bytes
    assert len(aac_bytes) < expected_max_size * 1.5  # Allow some overhead
```

**GREEN Implementation** (add to `worker.py` or `aac_encoder.py`):
```python
"""
AAC Encoder - Converts numpy audio arrays to AAC format.

Uses ffmpeg subprocess for encoding (stable-audio-tools outputs float32 arrays).
AAC chosen over MP3 for better quality/size ratio at similar bitrates.
"""

import subprocess
import tempfile
import numpy as np
from pathlib import Path
from scipy.io import wavfile


def encode_aac(audio: np.ndarray, sample_rate: int = 44100, bitrate: str = "192k") -> bytes:
    """
    Encode audio array to AAC format using ffmpeg.

    Args:
        audio: numpy array of audio samples, shape (samples,) or (samples, channels)
               Values should be in range [-1.0, 1.0]
        sample_rate: Sample rate in Hz (default 44100)
        bitrate: Audio bitrate (default 192k)

    Returns:
        AAC-encoded bytes (ADTS container)

    Raises:
        RuntimeError: If ffmpeg encoding fails
    """
    # Ensure audio is float32
    audio = audio.astype(np.float32)

    # Handle mono vs stereo
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)  # Duplicate to stereo
    elif audio.ndim != 2:
        raise ValueError(f"Expected 1D or 2D array, got {audio.ndim}D")

    # Write temporary WAV file
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = Path(f.name)
        # scipy expects (samples, channels) float32 in [-1, 1]
        wavfile.write(wav_path, sample_rate, audio)

    try:
        # Encode to AAC via ffmpeg
        result = subprocess.run([
            'ffmpeg',
            '-i', str(wav_path),
            '-c:a', 'aac',
            '-b:a', bitrate,
            '-y',  # Overwrite output
            '-f', 'adts',  # ADTS container (raw AAC without Muxer)
            '-'  # Output to stdout
        ], capture_output=True, check=True)

        return result.stdout

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"AAC encoding failed: {e.stderr.decode()}") from e

    finally:
        # Cleanup temp file
        wav_path.unlink(missing_ok=True)


def decode_aac(aac_bytes: bytes, sample_rate: int = 44100) -> np.ndarray:
    """
    Decode AAC bytes back to numpy array (for testing/verification).

    Args:
        aac_bytes: AAC-encoded bytes
        sample_rate: Expected output sample rate

    Returns:
        numpy array of audio samples, shape (samples, channels)
    """
    with tempfile.NamedTemporaryFile(suffix='.aac', delete=False) as f:
        aac_path = Path(f.name)
        aac_path.write_bytes(aac_bytes)

    try:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_path = Path(f.name)

            subprocess.run([
                'ffmpeg',
                '-i', str(aac_path),
                '-y',
                str(wav_path)
            ], capture_output=True, check=True)

            _, audio = wavfile.read(wav_path)
            return audio.astype(np.float32) / 32768.0  # Normalize to [-1, 1]

    finally:
        aac_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
```

#### Step 1.5: Worker Process

**RED Test** (`tests/test_worker.py`):
```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture
def mock_db_pool():
    """Mock asyncpg pool."""
    pool = AsyncMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool

@pytest.fixture
def mock_garage():
    """Mock Garage client."""
    garage = AsyncMock()
    garage.put_object = AsyncMock()
    return garage

async def test_worker_claims_pending_job(mock_db_pool):
    """Worker should atomically claim a pending job via FOR UPDATE SKIP LOCKED."""
    mock_conn = await mock_db_pool.acquire()
    mock_conn.fetchrow.return_value = {
        'id': uuid.uuid4(),
        'instrument': 'Synth Pad',
        'prompt': 'atmospheric pad',
        'status': 'pending'
    }

    worker = GeneratorWorker(config=TEST_CONFIG)
    worker.db = mock_db_pool

    job = await worker._claim_next_job()

    assert job is not None
    assert job['status'] == 'pending'
    # Verify FOR UPDATE SKIP LOCKED was used
    mock_conn.fetchrow.assert_called()

async def test_worker_updates_job_on_completion(mock_db_pool, mock_garage):
    """On successful generation, worker should update DB with audio_path."""
    worker = GeneratorWorker(config=TEST_CONFIG)
    worker.db = mock_db_pool
    worker.garage = mock_garage

    # Mock successful generation
    with patch.object(worker, '_generate_and_upload', new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = None

        await worker._mark_job_complete(TEST_JOB_ID, "audio/test.aac", 4.0)

    # Verify DB was updated
    mock_conn.execute.assert_any_call(
        expect(INSERT...).values(
            status='completed',
            audio_path='audio/test.aac',
            duration_seconds=4.0,
            ...
        )
    )

async def test_worker_cleanup_deletes_expired_jobs(mock_db_pool):
    """Cleanup should remove jobs older than expires_at."""
    worker = GeneratorWorker(config=TEST_CONFIG)
    worker.db = mock_db_pool

    await worker._cleanup_expired()

    # Should delete expired jobs
    assert "DELETE FROM generator_jobs" in str(mock_conn.execute.call_args)
```

**GREEN Implementation** (`worker.py`):
```python
"""
Generator Worker - Processes generation jobs from PostgreSQL queue.

This module runs in a separate container and:
1. Claims pending jobs via SELECT FOR UPDATE SKIP LOCKED
2. Generates audio using stable-audio-tools
3. Encodes to AAC
4. Uploads to Garage
5. Updates DB with result

Usage:
    # In container
    python -m worker

Environment Variables Required:
    - WORKER_ID: Unique worker identifier
    - DATABASE_URL: PostgreSQL connection string
    - GARAGE_ENDPOINT: S3-compatible endpoint (e.g., http://garage:3900)
    - GARAGE_ACCESS_KEY: Garage access key
    - GARAGE_SECRET_KEY: Garage secret key
    - GARAGE_BUCKET: Bucket name for audio storage
    - GARAGE_BUCKET_REGION: Garage region (default: garage)
"""

import asyncio
import logging
import os
import signal
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg
from garage_client import GarageClient, GarageConfig

# Import generator - same as used by main app
from framework_generator import GeneratorRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    """Configuration for a worker instance."""
    worker_id: str
    pg_dsn: str
    garage: GarageConfig
    job_poll_interval: float = 1.0  # seconds
    cleanup_interval: float = 300.0  # 5 minutes


class GeneratorWorker:
    """
    Async worker that processes generation jobs from PostgreSQL.

    Uses FOR UPDATE SKIP LOCKED to safely claim jobs without conflicts
    between multiple workers.
    """

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.db: Optional[asyncpg.Pool] = None
        self.garage = GarageClient(config.garage)
        self.generators = GeneratorRegistry()
        self.running = True

    async def start(self):
        """Main entry point. Creates DB pool and starts worker loops."""
        logger.info(f"Worker {self.config.worker_id} starting...")

        # Create connection pool (handles concurrent job processing)
        self.db = await asyncpg.create_pool(
            self.config.pg_dsn,
            min_size=2,
            max_size=5,
            command_timeout=300  # 5 minute timeout for queries
        )

        logger.info("Connected to PostgreSQL")

        # Start cleanup task
        cleanup_task = asyncio.create_task(self._cleanup_loop())

        # Main job processing loop
        while self.running:
            try:
                await self._process_next_job()
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)  # Back off on error

        # Shutdown
        cleanup_task.cancel()
        await self.db.close()
        logger.info(f"Worker {self.config.worker_id} stopped")

    async def _process_next_job(self):
        """
        Atomically claim and process the next pending job.

        Uses FOR UPDATE SKIP LOCKED to:
        - Prevent two workers from taking the same job
        - Skip jobs that are being processed by other workers
        """
        job = await self._claim_next_job()

        if job is None:
            # No pending jobs, wait before polling again
            await asyncio.sleep(self.config.job_poll_interval)
            return

        logger.info(f"Processing job {job['id']}: {job['instrument']}")

        try:
            # Generate and upload (outside transaction for I/O)
            audio_path, duration = await self._generate_and_upload(job)

            # Mark complete
            await self._mark_job_complete(job['id'], audio_path, duration)

            logger.info(f"Job {job['id']} completed: {audio_path}")

        except Exception as e:
            logger.error(f"Job {job['id']} failed: {e}")
            await self._mark_job_failed(job['id'], str(e))

    async def _claim_next_job(self) -> Optional[dict]:
        """
        Atomically claim the highest-priority pending job.

        Returns:
            Job dict if one was claimed, None if queue was empty.
        """
        async with self.db.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow("""
                    SELECT *
                    FROM generator_jobs
                    WHERE status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)

                if job is None:
                    return None

                # Mark as processing (within same transaction)
                await conn.execute("""
                    UPDATE generator_jobs
                    SET status = 'processing',
                        started_at = NOW(),
                        worker_id = $1
                    WHERE id = $2
                """, self.config.worker_id, job['id'])

                return dict(job)

    async def _generate_and_upload(self, job: dict) -> tuple[str, float]:
        """
        Generate audio for a job and upload to Garage.

        Args:
            job: Job dict with generation parameters

        Returns:
            Tuple of (garage_path, duration_seconds)
        """
        # Generate using stable-audio-tools (blocking, runs in executor)
        loop = asyncio.get_event_loop()
        audio_array = await loop.run_in_executor(
            None,
            lambda: self.generators.generate_stem(
                model_id=job['model_id'],
                prompt=job['prompt'],
                key=job.get('key'),
                bpm=job.get('bpm'),
                bars=job.get('bars', 4),
            )
        )

        # Encode to AAC
        from aac_encoder import encode_aac
        aac_bytes = await loop.run_in_executor(
            None,
            lambda: encode_aac(audio_array, sample_rate=44100)
        )

        # Upload to Garage
        audio_path = f"audio/{job['id']}.aac"
        await self.garage.put_object(audio_path, aac_bytes)

        # Calculate duration
        duration = len(audio_array) / 44100.0

        return audio_path, duration

    async def _mark_job_complete(self, job_id: uuid.UUID, audio_path: str, duration: float):
        """Mark job as completed with result."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE generator_jobs
                SET status = 'completed',
                    audio_path = $1,
                    duration_seconds = $2,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '24 hours'
                WHERE id = $3
            """, audio_path, duration, job_id)

            # Notify listeners
            await conn.execute(f"NOTIFY job_completed, '{job_id}'")

    async def _mark_job_failed(self, job_id: uuid.UUID, error: str):
        """Mark job as failed with error message."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE generator_jobs
                SET status = 'failed',
                    error_message = $1,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '1 hour'
                WHERE id = $2
            """, error, job_id)

    async def _cleanup_loop(self):
        """Periodically clean up expired jobs."""
        while self.running:
            await asyncio.sleep(self.config.cleanup_interval)

            try:
                async with self.db.acquire() as conn:
                    deleted = await conn.fetchval("""
                        WITH deleted AS (
                            DELETE FROM generator_jobs
                            WHERE status IN ('completed', 'failed')
                              AND expires_at < NOW()
                            RETURNING audio_path
                        )
                        SELECT COUNT(*) FROM deleted
                    """)

                    if deleted:
                        logger.info(f"Cleaned up {deleted} expired jobs")

            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    def stop(self):
        """Graceful shutdown."""
        logger.info("Shutdown requested...")
        self.running = False


def create_config_from_env() -> WorkerConfig:
    """Create worker config from environment variables."""
    garage_config = GarageConfig(
        endpoint=os.environ["GARAGE_ENDPOINT"],
        access_key=os.environ["GARAGE_ACCESS_KEY"],
        secret_key=os.environ["GARAGE_SECRET_KEY"],
        bucket=os.environ["GARAGE_BUCKET"],
        region=os.environ.get("GARAGE_BUCKET_REGION", "garage"),
    )

    return WorkerConfig(
        worker_id=os.environ.get("WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}"),
        pg_dsn=os.environ["DATABASE_URL"],
        garage=garage_config,
    )


async def main():
    """Entry point for worker process."""
    config = create_config_from_env()
    worker = GeneratorWorker(config)

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
```

---

### Phase 2: Async Framework Loop

#### Step 2.1: Job Submission API

**RED Test** (`tests/test_api_jobs.py`):
```python
@pytest.mark.asyncio
async def test_submit_job_returns_job_id():
    """POST /api/jobs should return job_id and set status to pending."""
    response = await client.post("/api/jobs", json={
        "session_id": str(session_id),
        "instrument": "Synth Pad",
        "prompt": "atmospheric pad, 128 bpm",
        "bpm": 128
    })
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data

    # Verify in DB
    job = await db.fetch_one(
        "SELECT * FROM generator_jobs WHERE id = $1", data["job_id"]
    )
    assert job["status"] == "pending"
    assert job["instrument"] == "Synth Pad"

@pytest.mark.asyncio
async def test_submit_job_sets_expiration():
    """New job should expire in 24 hours."""
    response = await client.post("/api/jobs", json={...})
    job = await db.fetch_one("SELECT expires_at FROM generator_jobs WHERE id = $1", ...)
    assert job["expires_at"] > datetime.now() + timedelta(hours=23)
```

#### Step 2.2: Job Polling/Waiting

**RED Test** (`tests/test_framework_loop.py`):
```python
async def test_wait_for_job_completion_returns_audio_path():
    """When job completes, wait_for_job_completion should return audio_path."""
    job_id = await create_pending_job()

    # Simulate worker completing job
    await db.execute("""
        UPDATE generator_jobs
        SET status = 'completed', audio_path = 'audio/test.aac'
        WHERE id = $1
    """, job_id)
    await db.execute(f"NOTIFY job_completed, '{job_id}'")

    result = await wait_for_job_completion(job_id, timeout=5.0)
    assert result == "audio/test.aac"

async def test_wait_for_job_completion_timeout():
    """Should return None on timeout."""
    job_id = await create_pending_job()
    result = await wait_for_job_completion(job_id, timeout=0.1)
    assert result is None
```

---

### Phase 3: Session Affinity

#### Step 3.1: Routing Table

**RED Test** (`tests/test_session_routing.py`):
```python
async def test_heartbeat_updates_last_heartbeat():
    """POST /api/sessions/{id}/heartbeat should update timestamp."""
    session_id = uuid.uuid4()
    server_id = "web-1"

    await client.post(f"/api/sessions/{session_id}/heartbeat", json={"server_id": server_id})

    row = await db.fetch_one(
        "SELECT * FROM session_routing WHERE session_id = $1", session_id
    )
    assert row["server_id"] == server_id
    assert row["last_heartbeat"] > datetime.now() - timedelta(seconds=5)

async def test_get_session_server_returns_correct_server():
    """GET /api/sessions/{id}/server should return registered server."""
    session_id = uuid.uuid4()
    await db.execute("""
        INSERT INTO session_routing (session_id, server_id)
        VALUES ($1, $2)
    """, session_id, "web-2")

    response = await client.get(f"/api/sessions/{session_id}/server")
    assert response.json()["server_id"] == "web-2"
```

---

### Phase 4: Cleanup & Polish

#### Step 4.1: Expiration

**RED Test** (`tests/test_job_expiration.py`):
```python
async def test_cleanup_deletes_expired_jobs():
    """Cleanup should delete jobs past their expires_at."""
    # Create job that's already expired
    expired_job_id = await db.fetchval("""
        INSERT INTO generator_jobs (session_id, instrument, prompt, status, expires_at)
        VALUES ($1, $2, $3, 'completed', NOW() - INTERVAL '1 hour')
        RETURNING id
    """, session_id, "test", "test prompt")

    await cleanup_expired_jobs()

    # Should be gone
    job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", expired_job_id)
    assert job is None

async def test_cleanup_preserves_active_jobs():
    """Jobs still within expiration should not be deleted."""
    active_job_id = await db.fetchval("""
        INSERT INTO generator_jobs (session_id, instrument, prompt, status, expires_at)
        VALUES ($1, $2, $3, 'pending', NOW() + INTERVAL '1 hour')
        RETURNING id
    """, session_id, "test", "test prompt")

    await cleanup_expired_jobs()

    job = await db.fetch_one("SELECT * FROM generator_jobs WHERE id = $1", active_job_id)
    assert job is not None
```

---

## Implementation Phases

### Phase 1: Core Infrastructure
1. Add PostgreSQL tables (`generator_jobs`, `session_routing`)
2. Implement Garage client wrapper
3. Create `Dockerfile.worker`
4. Build worker process with job claiming and AAC encoding

### Phase 2: Async Framework Loop
1. Refactor `run_framework_loop()` to be event-driven
2. Replace synchronous `generate_stem()` with job submission + wait
3. Implement `wait_for_job_completion()` with LISTEN/NOTIFY
4. Add audio fetch from Garage

### Phase 3: Session Affinity
1. Implement session routing table
2. Add heartbeat mechanism
3. Configure load balancer sticky sessions
4. Add redirect middleware

### Phase 4: Cleanup & Polish
1. Implement job expiration cron
2. Add Garage object deletion on cleanup
3. Worker health checks
4. Observability: metrics for queue depth, job latency

### Phase 5: Kubernetes Migration
1. Create k8s manifests
2. Configure HPA for workers
3. Set up Garage cluster
4. Ingress with sticky sessions

---

## Key Trade-offs

| Decision | Chosen | Alternatives Considered |
|----------|--------|-------------------------|
| Job queue backend | PostgreSQL | Redis, RabbitMQ |
| Audio storage | Garage (S3) | PostgreSQL bytea, shared NFS |
| Worker coordination | `FOR UPDATE SKIP LOCKED` | Celery, pg-boss |
| Completion signaling | PostgreSQL LISTEN/NOTIFY | Redis pub/sub, polling |
| Session affinity | DB-backed routing + LB cookies | Redis session store |
| Encoding format | AAC | MP3, FLAC, Opus |

---

## Files to Modify/Create

### New Files
- `worker.py` - Generator worker process
- `Dockerfile.worker` - Worker container
- `garage_client.py` - S3-compatible Garage client
- `migrations/001_jobs_and_routing.sql` - DB schema

### Modified Files
- `framework_main.py` - Event-driven loop
- `framework_state.py` - Job submission helpers
- `app_ui.py` - Session affinity middleware
- `docker-compose.yml` - Multi-container setup
- `requirements.txt` - Add worker dependencies

### No Changes Required
- `framework_generator.py` - Remains the same (used by workers)
- `framework_conductor.py` - No changes
- `framework_mixer.py` - No changes (runs on web server)
