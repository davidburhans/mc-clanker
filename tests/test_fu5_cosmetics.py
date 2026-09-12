"""FU-5 (rel-fu-cosmetics) TDD pins — cosmetic residuals, none behavioral.

Spec: refactor/plans/units/rel-fu-5-plan.md (§3 TDD tests); ledger:
refactor/plans/rel-remediation-plan.md follow-ups round 2 closing note.

- S1/S2: stream_fanout session-plumbing split (pure move →
  app/stream_fanout_sessions.py) under the 500-LOC rule, sentinel identity
  preserved across both namespaces, every old import path still resolving
  (zero test edits — FU-3 worker S1 pattern).
- A1: reasoning_stats' 13 moved private helpers + both compute_* entry
  points fully annotated (FU-4 H1 mirror, presence-only pin).
- B1: reliability_audit completion banner — plan-doc pointer + soak gate
  command — above the preserved historical verdict line.
"""

import inspect
from pathlib import Path

# --------------------------------------------------------------------------- #
# S1 — stream_fanout split under the 500-LOC rule (FU-5 item 1)
# --------------------------------------------------------------------------- #


def test_stream_fanout_split_files_under_500_lines():
    """S1 (FU-5 item 1): both stream_fanout files must exist and stay under
    the project's 500-LOC rule (AGENTS.md); the session-plumbing extraction
    (§1.1: _STOP_SENTINEL/_ClientSession/_drain_*/_residual_blocks →
    app/stream_fanout_sessions.py) is the sanctioned split seam, mirroring
    FU-3's worker S1 pin (test_worker_fu3.py) and FU-4's reasoning_stats pin."""
    for rel in ("app/stream_fanout.py", "app/stream_fanout_sessions.py"):
        path = Path(rel)
        assert path.exists(), (
            f"{rel} missing — the stream_fanout session-plumbing split (FU-5 item 1) has not landed"
        )
        line_count = len(path.read_text().splitlines())
        assert line_count < 500, f"{rel} is {line_count} lines — over the project's 500-LOC rule (AGENTS.md)"


# --------------------------------------------------------------------------- #
# S2 — sessions seam + sentinel identity + old import paths (FU-5 item 1)
# --------------------------------------------------------------------------- #


def test_stream_fanout_sessions_seam_and_sentinel_identity():
    """S2 (FU-5 item 1): the moved session plumbing must be importable from
    its new module AND keep resolving from app.stream_fanout (the split is a
    pure move plus re-import — zero test edits); _STOP_SENTINEL must be ONE
    object across both namespaces (queue-feeder poison-pill identity is
    load-bearing: _write_block drops None, the sentinel ends clients)."""
    import app.stream_fanout as fanout
    import app.stream_fanout_sessions as sessions

    moved = ("_STOP_SENTINEL", "_ClientSession", "_drain_one", "_drain_queue", "_residual_blocks")
    for name in moved:
        assert hasattr(sessions, name), (
            f"stream_fanout_sessions.{name} missing — session plumbing not moved (FU-5 §1.1)"
        )
        assert hasattr(fanout, name), (
            f"app.stream_fanout.{name} no longer resolves — the split must keep every old "
            "import path alive (patch-point inventory: zero test edits)"
        )
        assert getattr(fanout, name) is getattr(sessions, name), (
            f"{name} must be the SAME object in both namespaces (sentinel/identity is load-bearing)"
        )


# --------------------------------------------------------------------------- #
# A1 — reasoning_stats helpers fully annotated (FU-5 item 2)
# --------------------------------------------------------------------------- #


def test_reasoning_stats_helpers_fully_annotated():
    """A1 (FU-5 item 2): the 13 moved private helpers plus both compute_*
    entry points carry a full annotation surface — every parameter AND the
    return.

    FU-4 H1 mirror (test_fu4_exports.test_export_chunks_params_fully_annotated):
    deliberately NO ``inspect.signature(..., eval_str=True)`` — the plan's
    annotations reference TYPE_CHECKING-only names (Session/Row/ColumnElement)
    that are intentionally absent at runtime; the pin is PRESENCE;
    type-correctness is the reviewer's check."""
    from app.lib import reasoning_stats

    pinned = (
        reasoning_stats._timeline_segment_key,
        reasoning_stats._timeline_segment_aggregates,
        reasoning_stats._timeline_detail_rows,
        reasoning_stats._timeline_instruments,
        reasoning_stats._timeline_key_changes,
        reasoning_stats._timeline_reasoning_snippets,
        reasoning_stats._timeline_segment_dict,
        reasoning_stats._assemble_timeline_segments,
        reasoning_stats._stats_core_totals,
        reasoning_stats._stats_action_counts,
        reasoning_stats._stats_keys_used,
        reasoning_stats._stats_instruments_used,
        reasoning_stats._stats_response,
        reasoning_stats.compute_timeline_payload,
        reasoning_stats.compute_stats_payload,
    )
    for func in pinned:
        signature = inspect.signature(func)
        for name, param in signature.parameters.items():
            assert param.annotation is not inspect.Parameter.empty, (
                f"reasoning_stats.{func.__name__} param '{name}' is unannotated (FU-5 item 2)"
            )
        assert signature.return_annotation is not inspect.Parameter.empty, (
            f"reasoning_stats.{func.__name__} return is unannotated (FU-5 item 2)"
        )


# --------------------------------------------------------------------------- #
# B1 — reliability_audit completion banner (FU-5 item 4)
# --------------------------------------------------------------------------- #


def test_reliability_audit_completion_banner():
    """B1 (FU-5 item 4): the audit doc must open with a completion banner —
    STATUS line, pointer to the remediation plan, and the soak acceptance
    command — placed ABOVE the preserved historical verdict line.

    Header-scoped on purpose: the doc footer already mentions SOAK=1 (§7
    how-to-run note) — the banner requirement is that the reader sees the
    remediated status before the pre-remediation verdict, not merely that
    the strings exist somewhere in 637 lines."""
    text = Path("docs/reliability_audit.md").read_text()

    verdict_pos = text.find("**Verdict: NOT 24/7-ready.**")
    assert verdict_pos != -1, "historical verdict line must be preserved verbatim (banner is additive)"

    header = text[:verdict_pos]
    assert "**STATUS" in header, (
        "no completion STATUS banner above the verdict (FU-5 item 4) — the audit doc still reads as open"
    )
    assert "refactor/plans/rel-remediation-plan.md" in header, (
        "banner must point at the remediation plan (ledger + fixed-in notes live there)"
    )
    assert "SOAK=1" in header, "banner must carry the 24/7 acceptance gate (soak) command"
