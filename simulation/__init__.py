"""simulation — Stateful Slop Jockey simulation harness.

Simulates concurrent DJ sessions by reusing the actual production
ConductorLLMAsync from app.framework.framework_conductor_async.
No audio generation, no DB — pure LLM saturation with stateful continuity.
"""

__version__ = "0.1.0"
