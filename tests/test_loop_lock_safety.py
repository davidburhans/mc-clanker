"""Phase 7a safety tests: lock invariants the orchestrator must hold.

The round-1 adversarial review (BLOCKERs A4 + the lock-scope concern) required
that the single-lock atomic commit + the "no I/O inside state.lock" rule be
verified by TESTS, not comments. These two tests pin those invariants for the
orchestrator regardless of how _run_loop is later decomposed into _step_* methods.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from uuid import uuid4

from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state

# Reuse the loop-driving harness (brief-04 ssD) instead of duplicating it.
from tests.test_framework_characterization import (  # noqa: E402
    _active_stem_for_retain,
    _conductor_response_with_actions,
    _seed_loop_for_run,
    _wire_loop_no_io,
)


def test_no_io_inside_state_lock_in_orchestrator() -> None:
    """No `await` or `open()` may appear inside any `async with state.lock:` block.

    CLAUDE.md forbids I/O inside the state lock (it stalls the event loop / opens
    races). Source-inspected via AST so it catches regressions even if the lock
    block is later extracted into a _step_* method.
    """
    # The ``_step_*`` methods (which hold the ``async with state.lock:`` blocks)
    # live in loop_steps.py after Phase B; scan BOTH files so the invariant
    # follows the code wherever it moves. CLAUDE.md forbids I/O inside the state
    # lock (it stalls the event loop / opens races — brief-01 risk #2).
    orchestrator_files = [
        Path("app/framework/loop_orchestrator.py"),
        Path("app/framework/loop_steps.py"),
    ]

    def _is_state_lock_ctx(expr: ast.AST) -> bool:
        return (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "state"
            and expr.attr == "lock"
        )

    violations: list[str] = []
    for src_path in orchestrator_files:
        tree = ast.parse(src_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncWith, ast.With)) and any(
                _is_state_lock_ctx(item.context_expr) for item in node.items
            ):
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, ast.Await):
                        violations.append(f"{src_path.name}:L{node.lineno} await at L{child.lineno}")
                    elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "open":
                        violations.append(f"{src_path.name}:L{node.lineno} open() at L{child.lineno}")

    assert not violations, f"I/O inside state.lock forbidden (CLAUDE.md / brief-01 risk #2): {violations}"


async def test_record_loop_transition_runs_outside_state_lock(monkeypatch) -> None:
    """P11 atomicity: record_loop_transition (takes blocking sync_lock) must run
    while state.lock (asyncio) is UNlocked — it is called just AFTER the single
    `async with state.lock:` commit block releases (brief-01 risk #3)."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.current_bpm = 128
    state.current_key = "A minor"
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())

    lock_states: list[bool] = []

    def _spy(idx: int, stems: object, set_name: object, reason: object) -> None:
        lock_states.append(state.lock.locked())

    monkeypatch.setattr(state, "record_loop_transition", _spy)

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    assert lock_states, "expected record_loop_transition to fire for loop 1"
    assert not any(lock_states), (
        f"record_loop_transition must run OUTSIDE state.lock (brief-01 risk #3); locked-at-call={lock_states}"
    )


# --------------------------------------------------------------------------- #
# Phase 11 U1 pins — P10 loop-1 atomicity + dual-lock ordering (EVOLVED for U3).
#
# U3 migrates the orchestrator off its private reach into the concrete
# ``Mixer``. The atomicity invariant MOVES WITH THE CODE: the pins are evolved
# (retargeted to the new home), never deleted. Post-U3a the loop-1 handoff is a
# single ``self.mixer.prime_loop(...)`` call (no ``mixer.lock`` in loop_steps);
# the atomicity now lives inside ``Mixer.prime_loop`` (pinned structurally below).
# Post-U3b the P13 boundary read delegates to ``loop_position_seconds``. The
# permanent source-guard (test_orchestrator_has_no_private_mixer_reach) makes the
# zero-private-reach migration non-regressable. These are RED pre-U3 (TDD) and
# GREEN once U3a + U3b both land.
# --------------------------------------------------------------------------- #


def _is_attr_chain(node: ast.AST, *chain: str) -> bool:
    """True if ``node`` is ``Name(chain[0]).chain[1]…chain[-1]`` (e.g. self.mixer.lock).

    Reversed walk peels each attribute off the outermost, leaving the root Name.
    """
    for attr in reversed(chain[1:]):
        if not isinstance(node, ast.Attribute) or node.attr != attr:
            return False
        node = node.value
    return isinstance(node, ast.Name) and node.id == chain[0]


def _is_mixer_lock_ctx(expr: ast.AST) -> bool:
    """Match ``self.mixer.lock`` (the sync threading.Lock the orchestrator reaches)."""
    return _is_attr_chain(expr, "self", "mixer", "lock")


def _is_state_lock_ctx(expr: ast.AST) -> bool:
    """Match ``state.lock`` (the asyncio.Lock the _step_* methods acquire)."""
    return _is_attr_chain(expr, "state", "lock")


def _is_self_lock_ctx(expr: ast.AST) -> bool:
    """Match ``self.lock`` (the sync threading.Lock inside the Mixer class)."""
    return _is_attr_chain(expr, "self", "lock")


def _lock_items(node: ast.AST) -> list[ast.AST]:
    """Context expressions of a With/AsyncWith's ``items`` (empty for non-with)."""
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in node.items]
    return []


def _loop_steps_tree() -> ast.Module:
    return ast.parse(Path("app/framework/loop_steps.py").read_text())


def _framework_mixer_tree() -> ast.Module:
    return ast.parse(Path("app/framework/framework_mixer.py").read_text())


def _find_async_func(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async function {name!r} not found in loop_steps.py")


def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _descendant_locks(node: ast.AST, matcher) -> list[int]:
    """Line numbers of descendant lock acquisitions matching ``matcher`` (excl. self)."""
    hits: list[int] = []
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.With, ast.AsyncWith)) and any(matcher(c) for c in _lock_items(child)):
            hits.append(child.lineno)
    return hits


def _if_loop_idx_eq_one(func: ast.AsyncFunctionDef) -> ast.If:
    """The ``if self._loop_idx == 1:`` branch that routes loop-1 vs loop>1 in P10."""
    for stmt in func.body:
        test = stmt.test if isinstance(stmt, ast.If) else None
        if (
            isinstance(test, ast.Compare)
            and _is_attr_chain(test.left, "self", "_loop_idx")
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == 1
        ):
            return stmt
    raise AssertionError("no `if self._loop_idx == 1:` found in _step_commit_to_mixer")


def _assert_batch_writes_shape(lock_block: ast.With, *, owner_chain: tuple[str, ...], iter_name: str) -> None:
    """Pin the 4-statement shape of the single lock batch, generalized by owner.

    The batch write shape is identical whether the lock holder is the
    orchestrator (historical, ``self.mixer.*``) or the ``Mixer`` itself
    (``self.*`` post-U3). ``owner_chain`` is the attribute-access prefix to the
    Mixer (``("self", "mixer")`` or ``("self",)``); ``iter_name`` is the
    loop-iteration variable (``tracks_to_use`` in loop_steps, ``tracks`` in
    Mixer.prime_loop). The atomicity invariant moved WITH the code during U3a —
    this is its new (and only) home.
    """
    body = lock_block.body
    assert len(body) == 4, f"expected 4 stmts in lock batch, got {len(body)}"

    cur_sample = owner_chain + ("current_sample",)
    add_internal = owner_chain + ("_add_track_internal",)
    ensure_stereo = owner_chain + ("_ensure_stereo",)
    loop_end = owner_chain + ("current_loop_end_sample",)
    loop_dur = owner_chain + ("_current_loop_duration",)

    # (1) start_sample = <owner>.current_sample  (live read INSIDE the lock).
    read_stmt = body[0]
    assert isinstance(read_stmt, ast.Assign) and len(read_stmt.targets) == 1
    assert isinstance(read_stmt.targets[0], ast.Name) and read_stmt.targets[0].id == "start_sample"
    assert _is_attr_chain(read_stmt.value, *cur_sample), f"batch must read {'.'.join(cur_sample)} INSIDE the lock"

    # (2) for audio_data, stem_idx in <iter_name>: <owner>._add_track_internal(
    #         <owner>._ensure_stereo(audio_data), start_sample, stem_idx)
    loop_stmt = body[1]
    assert isinstance(loop_stmt, ast.For) and _is_attr_chain(loop_stmt.iter, iter_name)
    assert len(loop_stmt.body) == 1, "for-body must be the single _add_track_internal call"
    call = loop_stmt.body[0].value if isinstance(loop_stmt.body[0], ast.Expr) else loop_stmt.body[0]
    assert isinstance(call, ast.Call) and _is_attr_chain(call.func, *add_internal)
    assert len(call.args) >= 1 and isinstance(call.args[0], ast.Call)
    assert _is_attr_chain(call.args[0].func, *ensure_stereo), (
        f"_add_track_internal's first arg must be {'.'.join(ensure_stereo)}(audio_data)"
    )

    # (3) <owner>.current_loop_end_sample = start_sample + duration_samples.
    end_stmt = body[2]
    assert isinstance(end_stmt, ast.Assign) and len(end_stmt.targets) == 1
    assert _is_attr_chain(end_stmt.targets[0], *loop_end)
    assert isinstance(end_stmt.value, ast.BinOp) and isinstance(end_stmt.value.op, ast.Add)

    # (4) <owner>._current_loop_duration = duration_samples.
    dur_stmt = body[3]
    assert isinstance(dur_stmt, ast.Assign) and len(dur_stmt.targets) == 1
    assert _is_attr_chain(dur_stmt.targets[0], *loop_dur)
    assert isinstance(dur_stmt.value, ast.Name) and dur_stmt.value.id == "duration_samples"


def _assert_loop1_calls_prime_loop(if_body: list[ast.stmt]) -> None:
    """Pin the post-U3a loop-1 branch: a SINGLE ``self.mixer.prime_loop(...)`` call.

    The inline 4-statement lock batch no longer lives in loop_steps.py — the
    orchestrator delegates to ``Mixer.prime_loop`` (atomicity pinned on the Mixer
    side by test_prime_loop_is_single_lock_batch). This asserts the branch body
    is one ``ast.Expr`` wrapping the delegation, taking ``tracks_to_use``
    positionally and ``duration_samples`` as a keyword (matching the
    ``Mixer.prime_loop`` signature).
    """
    exprs = [s for s in if_body if isinstance(s, ast.Expr)]
    assert len(exprs) == 1, f"loop-1 branch must be a single Expr call, got {len(exprs)}"
    call = exprs[0].value
    assert isinstance(call, ast.Call), "loop-1 branch body must be a Call"
    assert _is_attr_chain(call.func, "self", "mixer", "prime_loop"), (
        "loop-1 branch must call self.mixer.prime_loop(...)"
    )
    assert len(call.args) >= 1 and _is_attr_chain(call.args[0], "tracks_to_use"), (
        "prime_loop must take tracks_to_use as first positional arg"
    )
    kw = {k.arg: k.value for k in call.keywords}
    assert "duration_samples" in kw, "prime_loop must pass duration_samples= keyword"
    assert _is_attr_chain(kw["duration_samples"], "duration_samples"), (
        "duration_samples must be the local duration_samples name"
    )


def _assert_set_next_loop_else(if_node: ast.If) -> None:
    """Pin the loop>1 branch: ONE set_next_loop call with the two kwargs."""
    assert len(if_node.orelse) == 1, "else-branch must be the single set_next_loop call"
    call = if_node.orelse[0].value if isinstance(if_node.orelse[0], ast.Expr) else if_node.orelse[0]
    assert isinstance(call, ast.Call) and _is_attr_chain(call.func, "self", "mixer", "set_next_loop")
    assert len(call.args) >= 1, "set_next_loop must take tracks_to_use positionally"
    kw_names = {kw.arg for kw in call.keywords}
    assert {"next_loop_duration_samples", "loop_idx"} <= kw_names, (
        f"set_next_loop must pass next_loop_duration_samples + loop_idx kwargs, got {kw_names}"
    )


def test_step_commit_to_mixer_loop1_is_single_prime_loop_call() -> None:
    """Invariant 1 (P10 atomicity, post-U3a): the loop-1 handoff delegates to a
    SINGLE ``self.mixer.prime_loop(...)`` call.

    After U3a the inline 4-statement lock batch no longer lives in
    loop_steps.py — the orchestrator calls ``Mixer.prime_loop`` (the abstraction
    U2 added), and the atomicity invariant moved WITH the code into
    ``Mixer.prime_loop`` (pinned by test_prime_loop_is_single_lock_batch below).
    This pin now asserts: (a) the loop-1 branch is a single ``prime_loop`` Expr
    call carrying the ``tracks_to_use`` positional arg + ``duration_samples``
    keyword; (b) the ENTIRE ``_step_commit_to_mixer`` function has ZERO
    ``with self.mixer.lock:`` blocks (the lock reach now lives inside Mixer). The
    ``else`` set_next_loop branch is unchanged.

    RED pre-U3a (the loop-1 branch is still the inline ``with self.mixer.lock:``
    batch); GREEN once U3a lands.
    """
    func = _find_async_func(_loop_steps_tree(), "_step_commit_to_mixer")
    if_node = _if_loop_idx_eq_one(func)

    # (a) loop-1 branch is a single prime_loop delegation.
    _assert_loop1_calls_prime_loop(if_node.body)

    # (b) zero mixer.lock reach anywhere in _step_commit_to_mixer after U3a.
    mixer_locks = _descendant_locks(func, _is_mixer_lock_ctx)
    assert mixer_locks == [], f"_step_commit_to_mixer must not acquire mixer.lock after U3: {mixer_locks}"

    _assert_set_next_loop_else(if_node)


def test_prime_loop_is_single_lock_batch() -> None:
    """Invariant 1 (P10 atomicity), post-U3 home: ``Mixer.prime_loop`` is ONE sync
    ``with self.lock:`` holding the SAME 4-statement batch the loop-1 handoff used
    to perform inline.

    The atomicity invariant moved WITH the code during U3a: it no longer lives in
    ``loop_steps._step_commit_to_mixer`` (now a single ``prime_loop`` call) but in
    ``Mixer.prime_loop``. This asserts that home is a single sync lock batch with
    the identical write shape (current_sample read → _add_track_internal+
    _ensure_stereo per track → current_loop_end_sample → _current_loop_duration)
    and that no await/open() leaks into the sync critical section (R5 guard,
    relocated from the old loop_steps pin). GREEN from U2 (prime_loop was added
    byte-for-byte); stays green through U3.
    """
    func = _find_func(_framework_mixer_tree(), "prime_loop")

    with_blocks = [s for s in func.body if isinstance(s, ast.With)]
    assert len(with_blocks) == 1, "prime_loop must have exactly one `with self.lock:`"
    lock_block = with_blocks[0]
    assert _is_self_lock_ctx(lock_block.items[0].context_expr), "context must be self.lock"
    assert not isinstance(lock_block, ast.AsyncWith), "must be SYNC with (threading.Lock), not async with"

    # Only one lock block in prime_loop (promotion must not split the batch).
    all_self_locks = _descendant_locks(func, _is_self_lock_ctx)
    assert len(all_self_locks) == 1, f"expected exactly 1 self.lock block, got {all_self_locks}"

    # Inside Mixer the attribute chains are self.* (no .mixer indirection).
    _assert_batch_writes_shape(lock_block, owner_chain=("self",), iter_name="tracks")

    # R5 (relocated): no await/open() inside the sync critical section.
    bad = [
        f"L{c.lineno}:{type(c).__name__}"
        for c in ast.walk(lock_block)
        if c is not lock_block
        and (
            isinstance(c, ast.Await)
            or (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "open")
        )
    ]
    assert not bad, f"no I/O/await inside self.lock (R5 dual-lock deadlock surface): {bad}"


def test_dual_lock_ordering_state_lock_holds_mixer_clear() -> None:
    """Invariant 3a (forward ordering): state.lock is held across mixer.clear().

    P3 ``_step_read_state`` calls ``self.mixer.clear()`` inside the
    ``async with state.lock:`` block; ``clear()`` internally takes the sync
    ``mixer.lock``. This establishes the ONLY safe nesting direction
    (state.lock → mixer.lock). A port that drops clear() out of the state-lock
    block, or makes clear() take state.lock, fails the pin.
    """
    func = _find_async_func(_loop_steps_tree(), "_step_read_state")
    state_lock_blocks = [
        n for n in ast.walk(func) if isinstance(n, ast.AsyncWith) and any(_is_state_lock_ctx(c) for c in _lock_items(n))
    ]
    assert state_lock_blocks, "_step_read_state must hold state.lock"

    clear_under_state = False
    for block in state_lock_blocks:
        for child in ast.walk(block):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and _is_attr_chain(child.func.value, "self", "mixer")
                and child.func.attr == "clear"
            ):
                clear_under_state = True
    assert clear_under_state, "self.mixer.clear() must run inside state.lock (state.lock→mixer.lock ordering)"


def test_no_mixer_lock_nests_state_lock() -> None:
    """Invariant 3a (no reverse nesting): no ``with mixer.lock`` may acquire state.lock.

    Holding the sync ``mixer.lock`` while acquiring the asyncio ``state.lock`` is the
    R5 dual-lock DEADLOCK direction (the daemon _callback holds mixer.lock; the
    orchestrator holds state.lock — reversing the order deadlocks). Scanned across
    the whole orchestrator surface (loop_steps.py + loop_orchestrator.py) so the
    invariant follows the code wherever it moves.
    """
    files = [Path("app/framework/loop_steps.py"), Path("app/framework/loop_orchestrator.py")]
    violations: list[str] = []
    for src_path in files:
        for node in ast.walk(ast.parse(src_path.read_text())):
            if isinstance(node, (ast.With, ast.AsyncWith)) and any(_is_mixer_lock_ctx(c) for c in _lock_items(node)):
                nested = _descendant_locks(node, _is_state_lock_ctx)
                if nested:
                    violations.append(f"{src_path.name}:L{node.lineno} nests state.lock at {nested}")
    assert not violations, "mixer.lock must NEVER nest state.lock (R5 dual-lock deadlock): " + ", ".join(violations)


def test_p13_delegates_boundary_read_no_mixer_lock() -> None:
    """Invariant 3b (P13, post-U3b): boundary read delegates to loop_position_seconds.

    After U3b ``_step_await_pregen`` no longer reaches ``self.mixer.lock`` — it
    reads the live boundary via ``self.mixer.loop_position_seconds()`` (the
    abstraction U2 added, which takes its own lock internally). Pinned
    structurally: (a) P13 STILL snapshots under ``state.lock`` (transition
    recording); (b) P13 has ZERO ``with self.mixer.lock:`` blocks; (c) P13 calls
    ``self.mixer.loop_position_seconds()``. The dual-lock no-nesting invariant is
    preserved (the concrete acquisition moved inside ``Mixer``); the
    state.lock/mixer.lock nesting scan stays valid as a regression guard.

    RED pre-U3b (P13 still reads the boundary under ``with self.mixer.lock:``);
    GREEN once U3b lands.
    """
    func = _find_async_func(_loop_steps_tree(), "_step_await_pregen")
    state_blocks = [
        n for n in ast.walk(func) if isinstance(n, ast.AsyncWith) and any(_is_state_lock_ctx(c) for c in _lock_items(n))
    ]
    mixer_blocks = [
        n for n in ast.walk(func) if isinstance(n, ast.With) and any(_is_mixer_lock_ctx(c) for c in _lock_items(n))
    ]
    # (a) transition snapshot still happens under state.lock.
    assert state_blocks, "P13 must snapshot under state.lock"
    # (b) the orchestrator no longer acquires mixer.lock directly.
    assert mixer_blocks == [], f"P13 must not acquire mixer.lock after U3 (uses loop_position_seconds): {mixer_blocks}"
    # (c) the boundary read is delegated to the public abstraction.
    delegates = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and _is_attr_chain(n.func, "self", "mixer", "loop_position_seconds")
    ]
    assert delegates, "P13 must call self.mixer.loop_position_seconds()"
    # Regression guard: state.lock must not nest mixer.lock (vacuous after U3 —
    # 0 mixer blocks — but catches re-introduction of a merged dual-lock block).
    for sb in state_blocks:
        assert not _descendant_locks(sb, _is_mixer_lock_ctx), f"P13 state.lock (L{sb.lineno}) must not nest mixer.lock"


def test_orchestrator_has_no_private_mixer_reach() -> None:
    """U3 invariant: loop_steps.py + loop_orchestrator.py reach ZERO Mixer privates.

    After U3 the only orchestrator→Mixer handoffs are ``Mixer.prime_loop`` /
    ``loop_position_seconds`` (plus the already-public ``clear``/``set_next_loop``/
    ``pop_transition_event``/``sample_rate``/``current_sample``). No reference to
    ``_add_track_internal`` / ``_ensure_stereo`` / ``_current_loop_duration``, and
    no ``with self.mixer.lock:``, may remain in orchestrator source — that reach
    now lives inside ``Mixer``. Scanned via AST (not text grep) so comments and
    docstrings do not false-positive. ``current_loop_end_sample`` and
    ``current_sample`` are intentionally NOT forbidden — both are declared public
    on the ``MixerController`` Protocol.

    RED pre-U3 (the orchestrator still reaches the privates inline at P10/P13);
    GREEN once U3a + U3b both land. This is the permanent guard that keeps the
    migration from regressing.
    """
    files = [Path("app/framework/loop_steps.py"), Path("app/framework/loop_orchestrator.py")]
    forbidden_attrs = {"_add_track_internal", "_ensure_stereo", "_current_loop_duration"}
    violations: list[str] = []
    for src in files:
        for node in ast.walk(ast.parse(src.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                violations.append(f"{src.name}:L{node.lineno} attr {node.attr}")
            if isinstance(node, (ast.With, ast.AsyncWith)) and any(_is_mixer_lock_ctx(c) for c in _lock_items(node)):
                violations.append(f"{src.name}:L{node.lineno} with self.mixer.lock")
    assert not violations, f"orchestrator must not reach Mixer privates after U3: {violations}"
