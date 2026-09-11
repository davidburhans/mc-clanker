# Round 3 — Lane 08: Conductor parsing / action semantics

**Lane targets (read fully):** `app/framework/framework_conductor_async.py` (429 L),
`app/framework/conductor_interaction.py` (195 L), `app/framework/loop_steps.py` (712 L),
`app/framework/pregeneration.py` (147 L), `app/framework/loop_orchestrator.py` (427 L,
conductor integration).

**Explicitly NOT re-reported** (round-1/2 or sibling round-3 lanes): `master_key`
enum-validation (06 §2), conductor HTTP-client timeout/transport-retry B12 (06 + SYNTHESIS),
`AsyncOpenAI` client leak (06 S1), stale-pregen zero-yield replay (01 §1), pregen-overwrites-
staged-mixer-loop (02 §2), 120 s batch timeout (03 §Q1). Where a finding below *touches*
06 §2 it is flagged in the text.

All line numbers are valid at HEAD `04791e4`. Proofs were executed with
`.venv/bin/python` against the **real** `_run_loop` / `_step_*` / `process_actions` code,
reusing only the fakes from `tests/test_framework_characterization.py` (`_FakeMixer`,
`AsyncMock` conductor/jobs/audio/audit). No repo file was modified.

---

## CONFIRMED BUGS

### 1. HIGH | `app/framework/loop_steps.py:350` × `app/framework/framework_conductor_async.py:29,38,49` | A *valid-JSON-but-not-an-object* Conductor response wedges the set permanently — the fallback path is never reached, the LLM is hammered forever, and `_loop_idx` runs away while `loop_count` stays 0

**Mechanism.** `parse_llm_json_response` is annotated `-> dict[str, Any]` but performs **no
type check** on what `json.loads` returns: `return json.loads(content)` (`:29`), the fenced
branch (`:38`) and the first-`{`/last-`}` recovery (`:44-49`) all return whatever object the
JSON decodes to. `call_async` returns it verbatim (`framework_conductor_async.py:187`). The
only consumer-side guard is `conductor_response.get("actions", [])`
(`loop_steps.py:350`, `:375`, `pregeneration.py:59`) — which raises `AttributeError` for a
`list` / `None` / `int` / `str` / `bool` top level.
The try/except that produces `build_fallback_response` wraps **only** the conductor *call*
(`loop_steps.py:337-346`); `_step_parse_actions` (`:348-356`) is outside it. The exception
therefore escapes to the B1 catch-all in `_run_loop` (`loop_orchestrator.py:289-296`), which
sleeps `LOOP_RETRY_BACKOFF_SECONDS` and re-runs the whole iteration — including a **fresh LLM
call** — forever. Nothing counts or caps these retries, and the mixer is never fed, so the
audible loop just repeats until an operator intervenes.

**Trigger.** Any backend that ignores / mis-implements `response_format` (the documented
default target is LM Studio `localhost:1234`, and `strict: True` + `anyOf` items
(`app/lib/constants.py:308-320`) is unsupported by many OpenAI-compatible servers) emitting a
top-level JSON array, `null`, or a bare scalar. Also `parse_llm_json_response`'s own recovery
turns `…prose… [{…}] …prose…` into a single **inner action dict**, i.e. a dict with no
`actions` key (see finding 2).

**Impact.** Permanent loss of the music set (no new loop is ever committed), unbounded LLM
spend (one call every ~2 s + the 3× JSON-retry loop inside `call_async`, forever), no
fallback-to-retain behaviour despite that mechanism existing, `_loop_idx` diverging from
`state.loop_count` (audit `loop_index` values and pregen `for_loop_idx` are derived from
`_loop_idx`), and every pending `target_bpm_override` / `target_key_override` /
`should_reset` consumed by P3 (`loop_steps.py:276-285`) and silently re-applied on a mix that
never advances.

**Minimal fix.** In `parse_llm_json_response`, after each successful `json.loads`, accept the
result only `if isinstance(obj, dict)` (otherwise fall through to the next recovery attempt /
the final `raise`, which *is* handled by the fallback). Optionally also coerce a top-level
`list` into `{"actions": obj}`. Additionally widen `loop_steps._step_call_conductor`'s
try/except to cover `_step_parse_actions` so *any* shaping failure degrades to
`build_fallback_response` instead of the B1 hot retry.

**Proof (executed).**
```
$ timeout 60 .venv/bin/python /tmp/probe1.py
  IN '[{"action_type": "retain", "stem_index": 0}]'  -> list:  [{'action_type': 'retain', 'stem_index': 0}]
  IN 'null'                                          -> NoneType: None
  IN '5'                                             -> int: 5
  IN '"keep the pads"'                               -> str: 'keep the pads'
  IN '```json\n[{"a":1}]\n```'                       -> list: [{'a': 1}]
  resp.get('actions', []) -> RAISE AttributeError: 'list' object has no attribute 'get'
  resp.get('actions', []) -> RAISE AttributeError: 'NoneType' object has no attribute 'get'
$ timeout 90 .venv/bin/python /tmp/probe2.py     # real _run_loop, conductor -> [ {...} ]
  conductor calls      = 9          # one LLM call per retry, unbounded
  _loop_idx            = 9
  state.loop_count     = 0          # diverges from _loop_idx; audit never written
  mixer tracks         = 0
  fallback ever used?  = False      # 'Fallback State' never reached
  stderr: "Loop iteration error (will retry): 'list' object has no attribute 'get'" ×9
```

---

### 2. HIGH | `app/framework/conductor_interaction.py:122-152` + `app/framework/loop_steps.py:559` | An unrecognized or empty action list is treated as "remove everything": the mix is emptied and the stream goes silent, with no guard and no error

**Mechanism.** `process_actions` dispatches on `action.get("action_type")` (`:122`). Anything
that is not exactly `"retain"`/`"add"`/`"remove"` falls through all three branches and is
**silently dropped** — no `else`, no log, no counter. `remove` is implemented as *exclusion*
(`:147-149`), so "no recognised action for stem *i*" and "remove stem *i*" are the same
outcome. If the resulting `deduped_tracks` is empty, `_step_build_next_stems` writes
`state.next_stems = []` (`loop_steps.py:383`), `tile_to_loop` returns
`([], loop_duration_samples)`, `_step_commit_to_mixer` primes/queues **zero** tracks, and
`_step_commit_state` sets `state.active_stems = list(state.next_stems)` = `[]` (`:559`).
Nothing anywhere asserts "a loop must contain at least the stems that were just playing",
and the drums-always-required invariant is prompt-only (grep: `'Drums'` never appears in any
action-shaping code path), so there is no automatic recovery either.
Secondary effect: the `if state.active_stems:` guard at `:549` skips the rotation once the mix
is empty, so `state.previous_stems` freezes on the last non-empty mix and `stem_history`
stops recording — `format_action_log(..., state.previous_stems)` (`:586`) then attributes
later loops to a stale stem list.

**Trigger (all realistic).**
* `{"actions": [], "reasoning": "…"}` — a perfectly schema-valid response under `strict: True`
  for a model that decides "no change this loop" or that hit a token/length limit.
* The discriminator key name **is never stated in either prompt string** — `grep -c
  action_type app/framework/framework_conductor_async.py` → **0**. Both the system
  instruction (`:90-95`) and the user template (`:112-118`) only say *"For 'add' actions…"*,
  *"`retain`: Keep an active stem…"*. The only place the key is named is the `response_format`
  schema, which non-supporting backends ignore — and the repo's own `CLAUDE.md:274-276`
  documents the shape as `{"action": "retain", …}`, i.e. exactly the shape that yields zero
  recognised actions.
* `parse_llm_json_response` recovery (`framework_conductor_async.py:44-49`) grabbing an
  inner object: `Here you go:\n[{"action_type":"retain","stem_index":0}]\nHope that helps`
  → `{'action_type': 'retain', 'stem_index': 0}` (a *dict*, so finding 1's AttributeError
  does not fire) → `.get("actions", [])` → `[]` → mix emptied, silently.

**Impact.** Total silence mid-set from a single ambiguous response; `state.last_actions`
becomes `[]` so the UI shows no activity; recovery requires the conductor to `add` stems from
an empty base (or operator intervention). No log line distinguishes "the model asked for
nothing" from "we understood nothing".

**Minimal fix.** In `process_actions`, count unrecognised actions and log them (type + the
raw key set). In `_step_parse_actions`/`_step_build_next_stems`, treat
`deduped_tracks == []` **while `active_stems` is non-empty** as a no-decision and fall back to
retain-all (`build_fallback_response`) rather than committing an empty mix. Name
`action_type` explicitly in both prompt strings so the prompt and the schema agree.

**Proof (executed).**
```
$ timeout 90 .venv/bin/python /tmp/probe2.py
=== D: misnamed discriminator ('action' not 'action_type') -> mix emptied ===
  active_stems   = 0  (was 3)
  previous_stems = 3
  stem_history   = 1
  mixer loop-1 tracks = 0 (0 => silence)
  last_actions   = []
=== D2: empty actions array -> same ===
  active_stems = 0  previous=2 mixer=0
$ timeout 60 .venv/bin/python /tmp/probe1.py
  empty actions                                      -> n=0 []
  'action' instead of 'action_type' (CLAUDE.md shape) -> n=0 []
  unknown action_type                                -> n=0 []
```

---

### 3. HIGH | `app/framework/loop_steps.py:280-285` vs `:570-571` (+ `:598-603`) | The DJ's tempo/key override is silently thrown away on every pre-generated loop (i.e. every loop ≥ 2 — the whole steady state)

**Mechanism.** P3 `_step_read_state` applies `state.target_bpm_override` /
`target_key_override` to `current_bpm` / `current_key` and then **clears both to `None`**
(`:280-285`). On the fresh path P6 re-pins them (`:369-377`), so they survive — that is the
only path `tests/test_framework_characterization.py:906-925` characterizes. On the
**pregen_ready** path P4-P9 are skipped (`loop_orchestrator.py:252`), so nothing re-applies
them, and P11 `_step_commit_state` then overwrites the value P3 just wrote with the
pregen decision's `master_bpm` / `master_key` (`:570-571`) — a decision that was taken by
`run_pregeneration` *before the override existed*, from a snapshot of the previous loop. The
"Apply pending UI overrides" block at `:598-603` — the intended safety net — is dead in this
path, because P3 already set `state.target_bpm_override = None`. The override is therefore
consumed **and** discarded, and the loop's P11 snapshot (`:606-615`) propagates the discarded
value into the *next* pre-gen, so the change never lands at all.

**Trigger.** Any `POST /api/state` (or `PUT` config, `app/routes/config.py:185-195`) setting
`target_bpm_override` / `target_key_override` while a set is running — i.e. the normal case,
since pre-gen covers every loop after loop 1 and each loop spans one full musical loop of
P13 waiting (`loop_steps.py:660-712`). The only window where it works is between P3 and P11
of the same pregen iteration (milliseconds).

**Impact.** The tempo / key controls are effectively non-functional during a live set, and
the failure is silent: the UI reflects the override for a few hundred ms (P3 wrote it), then
it reverts, and the pending flag is gone so nothing retries. Note it is the *user's*
explicit intent being dropped — and `current_key` also silently reverts, so subsequent stems
are generated in the old key.

**Minimal fix.** Do not clear the overrides in P3; read them into the snapshot and clear them
only at the single commit point. Concretely: capture `bpm_override`/`key_override` in
`_step_read_state` without writing/clearing state, and in `_step_commit_state` apply
`override or pregen or conductor` in that precedence order (the existing `:598-603` block can
then stay as the sole clearer).

**Proof (executed)** — real `_step_read_state` + `_step_commit_state`, loop primed into the
pregen branch (`_loop_idx=1`, `_pregen_results["loop_idx"]=2`, `master_bpm=128`,
`master_key="A minor"`):
```
$ timeout 90 .venv/bin/python /tmp/probe2.py
=== E: user override clobbered on the pre-generated path ===
  user asked bpm=150 key=F# minor -> current_bpm=128 current_key='A minor'
  target_bpm_override now = None (consumed, never applied)
```

---

### 4. MED | `app/framework/conductor_interaction.py:123-147,159` | Type confusion inside `process_actions` raises out of P5 (no fallback) and wedges the loop exactly like finding 1

**Mechanism.** Three unchecked assumptions in the action loop:
* `0 <= idx < len(active_stems)` (`:125`, `:147`) assumes `idx` is an `int`; a JSON string
  (`"1"`) raises `TypeError: '<=' not supported between instances of 'int' and 'str'`, a
  float (`1.0`) passes the range test and then raises
  `TypeError: list indices must be integers or slices, not float` at `active_stems[idx]`.
* `action.get(...)` (`:122`) assumes each element is a dict; `null` / a bare string element
  raises `AttributeError`.
* the dedup key does `'_'.join(t.get('timbre_tags', []))` (`:159`), but the `add` branch
  builds `timbre_tags` with `action.get("timbre_tags", ["Warm"])` (`:136`) — `.get` returns
  **`None`** for a present-but-null key, so `'_'.join(None)` raises
  `TypeError: can only join an iterable`.
Because `_step_parse_actions` is outside the conductor try/except (see finding 1), each of
these becomes an unrecovered iteration error → B1 hot retry → frozen set.

**Trigger.** Any schema-non-compliant Conductor response. Note the system prompt itself
*instructs* the model to emit `null` in these fields ("For 'retain' or 'remove' actions: You
only need to provide the `stem_index`. Other instrument fields should be `null`",
`framework_conductor_async.py:94`), and non-`response_format` backends also produce `"1"` /
`1.0` indices routinely.

**Impact.** Same wedge as finding 1 (music frozen, LLM hammered, `_loop_idx` runaway), from
data that is *inside* the documented envelope of "the LLM may send us junk".

**Minimal fix.** Normalize at the boundary: skip (and log) any element that is not a `dict`;
coerce/validate `stem_index` with `isinstance(idx, int) and not isinstance(idx, bool)`; use
`action.get("timbre_tags") or ["Warm"]` (and the same `or`-style default for
`notation_tag`/`fx_tag`/`bars`/`model_id`/`sub_family`); and widen the P4 try/except to cover
P5 so an unexpected shape still yields `build_fallback_response`.

**Proof (executed).**
```
$ timeout 60 .venv/bin/python /tmp/probe1.py
  stem_index as string        -> RAISE TypeError: '<=' not supported between instances of 'int' and 'str'
  stem_index float            -> RAISE TypeError: list indices must be integers or slices, not float
  action element is None      -> RAISE AttributeError: 'NoneType' object has no attribute 'get'
  action element is str       -> RAISE AttributeError: 'str' object has no attribute 'get'
  add with timbre_tags null   -> RAISE TypeError: can only join an iterable
$ timeout 90 .venv/bin/python /tmp/probe2.py   # same payload driven through the real _run_loop
  conductor calls = 9 | _loop_idx = 9 | state.loop_count = 0 | mixer primes/queues = 0/0
  active_stems = 2  (unchanged, music frozen)
```

---

### 5. MED | `app/framework/conductor_interaction.py:125-149` | `retain` beats `remove` for the same `stem_index` in one decision — an explicit "stop this stem" is silently ignored

**Mechanism.** The three branches are independent `if/elif`s accumulating into one
`new_tracks` list, and `remove` is implemented as *absence* (`:147-149`) rather than as an
operation on the set. A decision containing `{retain, i}` **and** `{remove, i}` appends stem
`i` in the retain branch and then no-ops on the remove branch, so stem `i` survives into the
next loop. Because retain also performs the in-place `_age` bump (`:128`), the surviving stem
is even aged as a normal retain. Order-insensitive and completely silent — there is no
remove-wins rule, no conflict log, and no dedup-stage check (the dedup key at `:156-161`
cannot see the conflict, it only collapses duplicates of what survived).

**Trigger.** A single Conductor response that both retains and removes the same index — a
common failure mode when the model reasons per-stem and hedges ("keep 2 for now / actually
drop 2"), and it is exactly what the response schema permits (`actions` is a free-order
array with no uniqueness constraint, `app/lib/constants.py:316-321`).

**Impact.** The Conductor's stated arrangement decision is not what gets played: a stem the
DJ-model explicitly dropped keeps playing (and keeps occupying a slot in the 4-6 stem density
budget, and keeps aging), for as long as subsequent loops retain it. Silent divergence
between the audit trail (`format_action_log` at `:181-195` *does* emit both "Retained X" and
"Removed X" lines, so the persisted audit describes a mix that never happened) and the audio.

**Minimal fix.** Two-pass semantics: first collect the removed index set, then build
`new_tracks` skipping `retain` actions whose index is in that set (remove-wins), and log the
conflict.

**Proof (executed).**
```
$ timeout 60 .venv/bin/python /tmp/probe1.py
  retain + remove same index -> n=1 [('Electronic Drums', 1)]   # stem survived the remove
```

---

### 6. MED | `app/framework/loop_steps.py:372` (+ `pregeneration.py:129`) | Explicit `null` for `master_bpm` / `master_key` defeats the `.get(default)` guard → `state.current_bpm = None` and the GPU generation prompt becomes `"…, None, None BPM, 4 Bars"`, with `bpm`/`key` written as NULL on the job row

**Mechanism.** `conductor_response.get("master_bpm", current_bpm)` returns **`None`** when the
key is present with a null value — the default only fires on a *missing* key. Same at
`:377`/`pregeneration.py:129-130`. `None` is then committed to `state.current_bpm` /
`state.current_key`, used to build every stem's prompt in `_step_build_next_stems`
(`:385`, via `build_track_prompt`, `conductor_interaction.py:96-104`), baked into
`make_cache_key` (`domain_audio.py:63`), and passed to `_submit_job` as `key=None, bpm=None`
(`loop_steps.py:414-421`).

**Trigger.** One Conductor response containing `"master_bpm": null` / `"master_key": null`
(models emit explicit nulls for fields they consider unchanged; again only the ignored
`response_format` schema forbids it).

**Impact.** The text prompt sent to the diffusion model literally contains `None` in the key
slot and `None BPM` — a garbage conditioning string, so the generated stem ignores tempo.
`state.current_bpm`/`current_key` are `None` for the UI and for the next prompt
("Master BPM: None"), and the cache key contains `None`, so stems are re-generated (never
cache-hit) across a `None`↔`128` transition. The `GeneratorJob` row is persisted with
`bpm = NULL`, `key = NULL` (`app/models/generator_job.py:89-92` — nullable), so the audit /
recording metadata loses tempo for that loop.

**Minimal fix.** Guard the commit: `bpm = conductor_response.get("master_bpm"); if not
isinstance(bpm, int) or bpm not in VALID_BPMS: bpm = current_bpm` (same shape for the key
against `VALID_KEYS`), applied to both `_step_build_next_stems` and
`pregeneration.run_pregeneration`.

**Proof (executed).**
```
$ timeout 60 .venv/bin/python /tmp/probe3.py   # real _run_loop, master_bpm/master_key = null
  state.current_bpm = None   state.current_key = None
  submitted job kwargs: prompt='Synth, Pad, warm, melody, dry, None, None BPM, 4 Bars' key=None bpm=None
```

**Overlap disclosure.** Round-3 lane 06 §2 already reports the *invalid-enum* `master_key`
variant at `loop_steps.py:377` and explicitly rejected `master_bpm=None` **as a crash**
(their line 96 / 321). What is new here is the demonstrated non-crash damage: prompt
pollution with `None`/`None BPM`, `None` persisted into `state` and into the `GeneratorJob`
row, and cache-key churn. If 06 §2 is fixed with an allow-list check at the commit point,
this finding is fixed by the same patch — fix once, credit both.

---

## SUSPECTED (UNVERIFIED)

* **MED (inert today)** | `app/framework/framework_conductor_async.py:395-410` |
  `ConductorPromptBuilder.build()` — self-described "main entry point for the async framework
  loop" — reads `state.active_stems` / `available_instruments` / `stem_history` under
  `state.sync_lock`, the **`threading.Lock`** reserved for the mixer audio callback
  (`framework_state.py:92`), not the asyncio `state.lock` the rest of the loop uses. It is
  also a byte-for-byte duplicate of the template inside
  `ConductorLLMAsync.get_next_state_async` (`:100-133`), so the two prompts can drift.
  Falsification: `grep -rn ConductorPromptBuilder app/` finds **no caller** — only tests —
  so the lock misuse cannot fire and the drift has not happened yet. Reported as latent:
  wiring this "entry point" up, as its docstring invites, reintroduces the two-lock class and
  forks the conductor prompt.

* **MED (design gap, could not prove operator-visible harm)** | drums invariant.
  `CLAUDE.md` ("Drums always required (auto-added if missing)") vs reality: the only
  enforcement anywhere is prose in the two prompt strings
  (`framework_conductor_async.py:77`, `:118`, `:347`). `process_actions` has no drums check,
  so a `remove`-the-drummer decision (or finding 2's empty decision) leaves a drumless /
  stemless mix with no automatic correction.

* **LOW** | `app/framework/loop_steps.py:276-285` | On any iteration that later dies (findings
  1 and 4), P3 has already consumed `should_reset` (and cleared it at `:279`) and the BPM/key
  overrides. A reset the operator pressed during a wedged iteration is therefore lost, not
  replayed. Not separately verified end-to-end.

---

## CHECKED AND CLEAN

Falsified candidates (I tried to break these and could not):

* **`zip(pending_jobs, results.values())` positional coupling**
  (`loop_steps.py:456`) vs the `.get(job_id)` form used by
  `pregeneration.py:102-104`. `wait_for_multiple_jobs`
  (`app/job_waiter.py:310-330`) builds `{job_id: r if isinstance(r, str) else None}` over the
  *full* `job_ids` list in order (including `CancelledError`/exception members), and
  `PostgresJobQueueAdapter.await_jobs` returns it verbatim, so key set and insertion order are
  both guaranteed — no audio-to-wrong-stem misattribution in production. (Still worth
  normalizing to the pregen form, but not a defect.)
* **`_age` accounting across the pregen / fresh paths.** `process_actions` bumps
  `active_stems[i]["_original_details"]["_age"]` in place on dicts that are *shared* with the
  live `state.active_stems`, and `run_pregeneration` runs it too. Traced over
  fresh → pregen → pregen-fallback → fresh: the read is `s.get("_age")` from the *stem* dict
  while the write targets `_original_details`, so a failed pre-gen followed by a fresh
  re-process does **not** compound (no premature stem churn). Verified by hand-trace and by
  probe B's `double retain same index -> n=1 [('Electronic Drums', 1)]`.
* **`stem_history` growth / pruning.** `loop_steps.py:550-552` appends then pops above 8 →
  hard cap of 8 entries; the two prompt builders only read the last 5
  (`framework_conductor_async.py:216`, `:300`). No unbounded growth (the *staleness* of
  `previous_stems` after an empty loop is reported inside finding 2, not here).
* **`build_fallback_response` shape** (`conductor_interaction.py:57-67`) — emits
  `action_type` (not `action`), covers every current stem index, and re-commits the *current*
  bpm/key, so the retain-all fallback is structurally valid for `process_actions` and
  `format_action_log`.
* **`format_action_log`** (`conductor_interaction.py:181-195`) — index guard `0 <= idx <
  len(stems)` mirrors `process_actions`, and the `prompt.split(",")[1]` reachability is
  length-checked. Audit `_audit_action_row` (`audit_recording.py:130-140`) reads the same
  `action_type`/`stem_index` keys and clamps to the column width — no `action` vs
  `action_type` drift on the audit side, and no double-append (`_step_append_audit` is called
  exactly once per committed iteration, `loop_orchestrator.py:277`).
* **Markdown-fenced / trailing-prose / truncated JSON recovery** — ` ```json … ``` `,
  prose-prefixed objects, and truncated objects all either parse or raise a `ValueError` that
  *is* converted into the retain-all fallback by `_step_call_conductor`
  (`loop_steps.py:343-346`). Only the non-`dict`-top-level and inner-object-recovery cases
  (findings 1-2) escape.
* **`load_available_models`** (`conductor_interaction.py:26-54`) — missing generator, missing
  file and unreadable/malformed JSON all degrade to `[]` without raising; per-model metadata
  lookups are `.get`-guarded.
* **P4/P5 lock hygiene** — `process_actions` runs outside `state.lock`, and the only in-lock
  work in `_step_parse_actions` is the `state.last_actions` assignment (`:353-354`); no I/O
  inside the lock was introduced on this surface.
* **`_step_read_state` reset + override snapshot ordering** (`loop_steps.py:274-300`) is
  internally consistent (the defect is the *pregen commit* discarding its output, finding 3,
  not P3 itself).

---

## Verification commands

```
git log --oneline -30                     # HEAD = 04791e4
timeout 60 .venv/bin/python /tmp/probe1.py   # parse_llm_json_response + process_actions matrix (findings 1,2,4,5)
timeout 90 .venv/bin/python /tmp/probe2.py   # real _run_loop drives (findings 1,2,3,4)
timeout 60 .venv/bin/python /tmp/probe3.py   # null master_bpm/master_key (finding 6)
```

All probes import the production modules directly and reuse only
`tests/test_framework_characterization.py`'s `_FakeMixer` + `AsyncMock` job/audio/audit
fakes; no repo file was created or modified besides this report.

---

## VERIFICATION

(adversarial re-verification at HEAD 04791e4; probes in /tmp/verify08/, repo untouched)

FINDING CONFIRMED 1 | Verified | Reproduced on the real `_run_loop`: a top-level JSON list passes `parse_llm_json_response` untyped (`app/framework/framework_conductor_async.py:29`) and dies at `conductor_response.get("actions", [])` (`app/framework/loop_steps.py:350`), which is outside P4's try/except (`app/framework/loop_orchestrator.py:253-255`), so B1 retries uncapped (`app/framework/loop_orchestrator.py:284-292`, sleep-only, no counter) — probe gave 9 conductor calls / `_loop_idx`=9 (`app/framework/loop_steps.py:193`) / `state.loop_count`=0 / 0 mixer tracks / fallback never used, and no test pins non-dict parse output.
FINDING CONFIRMED 2 | Verified | `process_actions` silently drops unrecognized types (`app/framework/conductor_interaction.py:122-150`, no else/log) and probe drove empty/`{"action":…}`/inner-recovery dicts through the real `_run_loop` to `active_stems=0`, `set_next_loop` with 0 tracks, `last_actions=[]` (`app/framework/loop_steps.py:559`, `:383`), with `previous_stems`/`stem_history` frozen by the `if state.active_stems:` guard at `app/framework/loop_steps.py:549-553`; `grep -c action_type app/framework/framework_conductor_async.py` = 0 and CLAUDE.md:274-276 documents the non-working `action` key.
FINDING CONFIRMED 3 | Verified | Real P3+P11 drive with `_pregen_results` primed: `target_bpm_override=150/"F# minor"` applied then cleared at `app/framework/loop_steps.py:281-285`, then overwritten by pregen `master_bpm`/`master_key` at `app/framework/loop_steps.py:570-571` (probe: bpm back to 128, key back to "A minor", pending None), the `:598-603` safety net is dead because the flag was already cleared, and the discarded value is propagated into the next pre-gen snapshot (`app/framework/loop_steps.py:606-615`, probe `snapshot_bpm=128`); the fresh path still works (probe 150/F# minor), and the only override test (`tests/test_framework_characterization.py:906-925`) exercises that fresh path only — trigger reachable via `POST /api/state` (`app/routes/config.py:169-186`).
FINDING CONFIRMED 4 | Verified | All five type-confusion payloads raise exactly as claimed from `process_actions` (`app/framework/conductor_interaction.py:122`, `:125`, `:147`, `:159`): string `stem_index` → `TypeError: '<=' not supported…`, float → `list indices must be integers`, `None`/`str` elements → `AttributeError: … has no attribute 'get'`, `add` with `timbre_tags: null` → `TypeError: can only join an iterable` — and each raises inside `_step_parse_actions` (`app/framework/loop_steps.py:350`), outside P4's fallback, so it becomes the same B1 hot-retry wedge proven in finding 1; the prompt even mandates nulls (`app/framework/framework_conductor_async.py:90`).
FINDING CONFIRMED 5 | Verified | Probe: `{retain,i}`+`{remove,i}` (both orders) yields n=1 — stem survives — because retain appends (`app/framework/conductor_interaction.py:125-129`) and remove is mere absence (`:147-149`) with no conflict pass, while `format_action_log` emits both "Retained" and "Removed" lines (`app/framework/conductor_interaction.py:185-195`), so the audit describes a mix that never played.
FINDING CONFIRMED 6 | Verified | Probe through the real `_run_loop` with `"master_bpm": null`/`"master_key": null`: `state.current_bpm`/`current_key` = `None` (`app/framework/loop_steps.py:372,377`), submitted job kwargs `prompt='Synth, Lead, w, melody, dry, None, None BPM, 4 Bars' key=None bpm=None` (`.get` default does not fire on present-null), cache key embeds `None` (`app/framework/domain_audio.py:63`), and the `GeneratorJob` columns are nullable so NULL persists (`app/models/generator_job.py:91-92`); same shape in `app/framework/pregeneration.py:129-130`.
FINDING SUSPECTED 1 | Verified | `ConductorPromptBuilder.build` does read state under the mixer's `threading.Lock` (`app/framework/framework_conductor_async.py:388` vs `app/framework/framework_state.py:92`) and has no production caller (only `build_prompt` is used internally at `app/framework/framework_conductor_async.py:421`), so it is correctly self-scoped as inert/latent — and the fork is already real, not hypothetical: the two templates differ ("if one are not already playing!" at `:118` vs "if one is not already playing!" at `:347`).
FINDING SUSPECTED 2 | Verified | The drums invariant is prompt-prose only (`app/framework/framework_conductor_async.py:77`, `:118`, `:347`); `process_actions` (`app/framework/conductor_interaction.py:105-165`) contains no drums/density check, so CLAUDE.md:291's "Drums always required (auto-added if missing)" is unenforced and the emptied mix proven in finding 2 has no automatic recovery.
FINDING SUSPECTED 3 | Verified | P3 clears `state.should_reset` (`app/framework/loop_steps.py:270-274`) and both overrides (`:281-285`) before P4/P5 run at `app/framework/loop_orchestrator.py:242-255`, so an operator reset taken during a finding-1/4 wedged iteration is consumed and never replayed (no re-arm path exists; the only other clearer is the dead `:598-603` block).

VERDICT: 9 verified, 0 weakened, 0 falsified
