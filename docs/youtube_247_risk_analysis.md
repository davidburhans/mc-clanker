# 24/7 Streaming — Risk Analysis

Companion to [`youtube_channel_launch_plan.md`](youtube_channel_launch_plan.md)
(scheduled-set launch) and [`youtube_live.md`](youtube_live.md) (mechanics,
licenses). Written for the decision: *should MC Clanker eventually run a
continuous 24/7 stream, and under what conditions?*

Sources verified 2026-10: YouTube channel monetization policy
(support.google.com/youtube/answer/1311392), July 2026 policy reorganization
coverage (Tubefilter/TechCrunch), December 2025–January 2026 enforcement wave
reporting (Deadline, Kapwing research). Re-verify before acting on it.

---

## TL;DR

24/7 is **viable but only as a deliberate, firewalled second phase** — not as
the launch format, and ideally not on the flagship channel.

1. **Policy risk is real but overstated for us.** Every channel actually
   terminated in the 2025–26 crackdown was an *upload factory* (hundreds of
   near-identical AI videos). No 24/7 music curator has been a named
   enforcement target. But the policy text explicitly covers live streams, is
   enforced at **channel level**, and a YPP reviewer judges your channel by
   "biggest proportion of watch time" — which for a 24/7 channel *is the
   stream*. An unattended machine stream with zero human presence is the
   worst-looking version; a dayparted, curated, interactive one is defensible.
2. **The relay is not 24/7-ready today.** It gives up permanently after 3
   restarts, does not auto-arm on app boot, and drops audio during every
   restart. Each is a small fix; until then, "24/7" means "dead by Tuesday."
3. **Run it on a separate channel** ("MC Clanker Radio"), initially
   unmonetized, as a discovery funnel into the flagship. Channel-level
   enforcement is the reason: it quarantines YPP risk away from the
   scheduled-set channel.
4. **Copyright is the risk nobody prices in.** False Content ID matches on AI
   audio are common enough to plan for — and we have unusually strong
   provenance evidence (DB job rows, prompts, Conductor reasoning logs) to
   dispute with.

---

## Risk Matrix

| # | Risk | Likelihood | Impact | Controllable? |
|---|------|-----------|--------|---------------|
| 1 | YPP reviewer flags the 24/7 stream as inauthentic → **whole-channel** demonetization | Medium | High | Mostly (structure + curation) |
| 2 | Relay gives up after 3 FFmpeg deaths → stream dead until human intervention | High | Medium | Yes (watchdog) |
| 3 | App/host reboot → relay never re-arms (no auto-start on boot) | Medium | High | Yes (boot hook) |
| 4 | Content ID false match on generated audio (live claim) | Medium | Medium | Partially (dispute SOP) |
| 5 | Bad actor rips stream audio and registers it → claims on our own output | Low | Medium | Partially |
| 6 | Generation throughput < consumption → loops audibly repeat (also worsens #1) | Medium | Medium | Yes (alerting, backlog caps) |
| 7 | GPU/worker degradation over multi-day uptime (OOM, fragmentation) | Medium | Medium | Yes (daily restart) |
| 8 | ISP upload: ~1.45 TB/month sustained at 1080p30 | Site-specific | High | Yes (720p fallback) |
| 9 | Musical drift / loop fatigue during unattended hours | High | Medium | Yes (daypart program) |
| 10 | Stability AI license breach | Very low | High | Yes (already handled — see youtube_live.md) |

---

## 1. Policy Risk (YPP "Inauthentic Content") — the big one

### What the policy actually says (verified Oct 2026)

- The policy explicitly includes live streams: *"when we use the term **video**
  on this page, it refers to Shorts, long-form videos, and live streaming."*
- The AI-specific clause: *"AI-generated content made with generic or
  unoriginal templates giving the impression of mass production without adding
  the creator's original, authentic insights or perspective"* is not eligible.
- July 13, 2026: policy reorganized into three named categories (generic/
  repetitive; unsatisfying/off-putting; AI personas on sensitive topics).
  Rules unchanged — every example YouTube gives describes **uploaded videos**.
- Enforcement is **channel-level**: reviewers check *"Main theme, Most viewed
  videos, Newest videos, Biggest proportion of watch time, Video metadata,
  About section."* A violation can suspend monetization for the **entire
  channel**, not just one stream.

### What enforcement has actually looked like

Dec 2025–Jan 2026 terminations: Screen Culture, KH Studio, ~16 channels /
~35M subs / ~4.7B views (Kapwing research). All were automated pipelines
bulk-uploading near-identical AI videos. YouTube's CEO letter framing: "AI
slop factories." No 24/7 live music curator is a known enforcement target,
and third-party analyses (LiveReacting, Jul 2026) read 24/7 curated streams
as outside the pattern. **Caveat:** absence of enforcement ≠ policy
permission; the reviewer at *your* YPP application is the moment of truth.

### Why MC Clanker is unusually defensible — and where it's exposed

Defensible:

- **Every loop is genuinely generated live.** We are the *opposite* of a
  looping playlist: continuous novel output, timestamped, DB-backed. "Mass
  production" is the claim we're best equipped to rebut with evidence.
- **Full provenance trail**: `generator_jobs` rows, prompts, `ShowAction` /
  `LLMInteraction` reasoning logs, Garage objects — auditable originality.
- Chat interactivity: YouTube uses chat density as a live ranking signal;
  interactive streams get recommended, silent ones don't.

Exposed:

- **Unattended hours = zero human perspective.** The clause requires the
  *creator's* original insights or perspective. Overnight, there is no
  creator on the channel — just a machine broadcasting. That's the exact
  sentence we'd be arguing about.
- **Watch-time concentration.** If 24/7 runs on the flagship channel, the
  stream becomes the channel's dominant watch-time item, and it's what the
  reviewer judges the channel by. One weak asset can drag the whole channel.
- **Near-zero CCV initially** makes the stream look like a "single video
  looping forever with zero engagement" — the closest live analog to the
  generic-template pattern.

### Mitigations (in order of leverage)

1. **Separate channel** (see §5) — turns channel-level enforcement into a
   contained blast radius.
2. **Unmonetized 24/7 carries zero YPP risk.** The monetization policy only
   matters if you monetize. Running radio as an unmonetized funnel removes
   risk #1 entirely until you *choose* to apply.
3. **Dayparted programming** (§6): named program blocks with distinct vibe
  seeds, BPM ranges, and visualizers — curation is the visible human act.
4. **Staffed anchor windows**: the flagship Friday set streams *on* the radio
  channel (or cross-promo to it), so the stream has demonstrable live human
  curation events in its history.
5. **Metadata as argument:** the radio channel About section and stream
  description say exactly how it works ("every stem generated live by our own
  models; program grid curated by humans") — the reviewer reads these.

---

## 2. Copyright / Content ID Risk

Separate from monetization policy; applies **even to unmonetized streams**.

- **False positives are a documented phenomenon** for AI-generated music —
  melodic similarity matches happen, and bad-faith actors registering other
  people's AI output have caused real takedowns (2026 Suno incidents).
- **Live mechanics:** a Content ID match on a live stream can mute, redirect
  revenue, or end the broadcast; a formal strike goes on the channel and
  temporarily blocks live streaming; three strikes terminate.
- Our Foundation-1 output is instrumental techno/ambient — lower match
  surface than vocal music. **Do not enable Vocal_Textures on the 24/7
  channel initially** (synthetic vocals both raise match probability and
  strengthen the case for YouTube's AI-disclosure label).
- **Never prompt "in the style of <artist>"** — artist-name prompts create
  trademark/impersonation exposure under Stability's AUP and YouTube policy,
  and bias generation toward existing recordings (match bait).

### Dispute SOP (prepare before launch)

Evidence pack per set (we already store all of this):

1. `generator_jobs` rows: job IDs, model_id, prompt, timestamps.
2. Reasoning-log export (`/api/reasoning/logs` NDJSON) — shows live decisions.
3. Show recording + `config_snapshot` from the Show row.
4. License statement: "Generated at runtime by our own Foundation-1 fine-tune
   of stable-audio-open-1.0 under the Stability AI Community License."

Rule: dispute within 48h of any claim; never let a claim sit undisputed (it
redirects revenue and, on live, can end the stream).

### The rip-and-register scenario

Someone records the stream, uploads/registers it, then claims *us*. Low
likelihood, real precedent. Mitigations: the ident sting + watermark timing
prove priority; our timestamped DB records predate their registration;
periodic search sweeps of distinctive stem phrases; DMCA takedowns of
re-uploads.

---

## 3. Technical Risk — what the code actually does

> **Update:** a four-lane deep reliability audit of the full music-production
> pipeline completed with 5 Critical / 10 High findings — see
> [`reliability_audit.md`](reliability_audit.md). The checklist below is the
> *streaming* slice; the audit covers the mixer, loop, worker, and server.
> Batches 1–3 of its remediation sequence are the real Stage-2 gate.

Read from `app/youtube_relay.py` and cleanup paths; these are facts, not
estimates:

| Behavior | Consequence for 24/7 |
|---|---|
| `max_restarts=3` per session, then `_give_up()` permanently deactivates — **fixed-in rel-15-youtube**: a proc alive ≥ `stability_window_s` (300 s) earns a fresh budget (rate limit, not lifetime count), and the `youtube_lifecycle` watchdog re-arms a fully gave-up relay | Brief network blips self-heal twice over (relay-internal restart + watchdog re-arm). Only a sustained fast-crash loop (rejected key) stays down-ish: ~3 arms per 15 min with one ERROR alert, auto-healing once the key is fixed. `dropped_blocks` delta still worth alerting on. |
| No auto-arm on app boot — relay starts only via `POST /api/youtube/stream/start` — **fixed-in rel-15-youtube**: `youtube_lifecycle.auto_arm_youtube_relay` arms on lifespan startup whenever `YOUTUBE_STREAM_KEY` is set (never fatal; no key = silent no-op), and the watchdog keeps it armed | Host reboot / app crash / deploy self-heal within one watchdog tick (60 s) instead of staying dead. To keep a host down across restarts, remove `YOUTUBE_STREAM_KEY` (the disarm flag is in-memory only). |
| Writer queue = 512 blocks (~24 s), overflow drops, never blocks | Every restart cycle drops the blocks drained while FFmpeg is down (backoff 2 s × restart #, plus spawn time) — seconds of dead air per blip. Acceptable occasionally; alert on `dropped_blocks` delta. |
| Restart backoff is linear, 2 s × restart # | Fine. The watchdog's own storm guard (rel-15) landed as specified here: 60 s arm cooldown; 3 consecutive fast-failure arms → ERROR alert + 15 min backoff, then retry. |
| Cleanup reaps stale job leases; deletes expired Garage objects | Storage growth is handled *if* retention is configured — verify the env-driven retention window before continuous operation. |
| Playback via `ShowPlayback` feeds the same broadcast path | Recorded shows can cover maintenance windows — but looping recordings overnight leans toward the "repetitious" pattern; prefer live generation, keep playback as failover only. |
| FFmpeg `veryfast` @ 1080p30 = ~4.5 Mbps sustained ≈ **1.45 TB/month** upload | Check ISP caps/peering. Fallback: 720p30 (2.5 Mbps, ~810 GB/mo) via the `resolution` param. |

Also pre-decide: **daily scheduled restart at the viewership trough** (e.g.,
03:30 ET). Standard 24/7 hygiene — resets FFmpeg/GPU state, and note that
YouTube does not archive streams beyond ~12 h anyway, so overnight VOD
capture is on us regardless.

### Generation throughput (risk #6)

Each loop needs stems generated by a single GPU worker (5–30 s per stem) while
the mixer consumes loops continuously. If generation falls behind, loops
repeat audibly — which is simultaneously a UX bug and **policy evidence**
(audible repetition). Requirements:

- Measure sustained jobs/hour before committing to a program grid; size the
  grid's stem-add rate to ~60–70% of measured capacity.
- Alert when the mixer has repeated the same loop index > N times.
- Cap `next_stems` backlog; degrade gracefully (extend loop length) rather
  than replay silence.

### 24/7 engineering go-live checklist

- [x] Watchdog service: every 60 s — if relay inactive → re-arm via a fresh
      relay built from current state (`youtube_lifecycle.youtube_watchdog_loop`),
      with storm guard (3 consecutive fast-failure arms → alert + 15 min
      backoff) and operator-disarm respect
- [x] Auto-arm relay on app lifespan startup when `YOUTUBE_STREAM_KEY` is set
      (`youtube_lifecycle.auto_arm_youtube_relay`; failure is never fatal,
      no key = silent no-op)
- [ ] systemd `Restart=always` (or docker `restart: unless-stopped`) for app,
      worker, Postgres, Garage
- [ ] Backlog/stall alerts (job queue depth, loop repeat counter,
      `dropped_blocks` delta, worker heartbeat)
- [x] Kill switch documented (single command → clean `/stream/stop`; rel-15
      also sets `state.youtube_relay_disarmed` so auto-arm/watchdog respect
      it — see [youtube_live.md](youtube_live.md))
- [ ] Retention verified (cleanup cadence + Garage + VOD recordings)
- [ ] 72-hour unlisted soak test, then a 2-week staffed-hours-only trial
      before true unattended 24/7

---

## 4. Musical Risk (unattended hours)

- **Loop fatigue:** the Conductor already rotates stems (5–10 loop age limit)
  and shifts key/BPM, but hours 2–8 of any static configuration will drift
  toward sameness. Countermeasure: scheduled override rotation per daypart
  (distinct seeds, density targets, major-family bias per block).
- **Night has no chat:** no vibe prompts arrive to break equilibria. The
  daypart program grid substitutes structure for interaction.
- **Quality outliers:** unattended generation can produce a dud stem. Mix
  safeguards (per-stem loudness guardrails) matter more at 24/7 than during
  staffed sets.

---

## 5. Structural Decision: Same Channel or Separate Radio Channel?

**Recommendation: separate channel — "MC Clanker Radio"** — when 24/7 starts.

| | Same channel (flagship) | Separate radio channel |
|---|---|---|
| Policy blast radius | 24/7 watch time dominates the channel; any YPP finding hits the sets/VOD business too | Radio risk contained; flagship untouched |
| Audience | One sub base; but YouTube's "returning viewer" signals get muddier | Splits subs initially; radio funnels to flagship via overlay + description |
| YPP thresholds | One channel's numbers | Each channel needs its own (radio: watch hours accrue fast at 24/7 even at <1 avg CCV; subs are the slow part) |
| Brand clarity | "Event channel that never sleeps" muddies the set brand | Clean split: flagship = events; radio = ambient product |
| Ops | One stream pipeline | Same pipeline, second key (trivial — config already per-relay) |

Operating posture: radio launches **unmonetized** (zero YPP exposure, zero
policy downside) and stays that way until (a) the flagship channel is safely
through YPP, (b) radio has an established program grid + staffed anchor
windows in its history, and (c) you've re-verified policy state. This is not
a ban-evasion structure — nothing is being evaded; it's isolating a
higher-risk asset from a lower-risk one.

---

## 6. Graduated Path to 24/7

Each stage gates the next; abort criteria included.

| Stage | Format | What it proves | Gate to next |
|---|---|---|---|
| 0 | Scheduled sets (current launch plan) | Format, audience, YPP trajectory | 4–6 weeks stable, recording archive working |
| 1 | Occasional 12-hour "long sets" (staffed, on-call) | Relay + generation stamina across a real day | <3 restarts/day, zero manual interventions |
| 2 | Engineering block: watchdog, auto-arm, alerts | Unattended recovery actually works | 72h unlisted soak test clean |
| 3 | **MC Clanker Radio** launch, dayparted grid, staffed anchor windows, unmonetized | Viewer demand for continuous mode | Trough CCV > 0 sustained; chat present at some hours |
| 4 | Full 24/7 with daily trough restart | The real thing | Review at 90 days: policy climate, CCV trend, ops burden |

Daypart grid sketch (radio channel):

| Block (ET) | Name | Program | Visualizer |
|---|---|---|---|
| 00–06 | Sleep Cycle | ambient, 60–80 BPM, 3–4 stems | `spectrum` |
| 06–10 | Morning Compile | downtempo, melodic | `waves` |
| 10–17 | Deep Work | hypnotic techno, steady | `cqt` |
| 17–20 | Overclock | peak energy, 126–132 BPM | `waves` |
| 20–24 | Night Session | house/melodic, chat-primed | `cqt` |

Distinct names, seeds, and visuals per block = visible curation, and every
block's metadata argues against "generic template."

---

## 7. Cost & Ops Reality Check (order of magnitude)

| Item | 24/7 estimate |
|---|---|
| Upload bandwidth | ~1.45 TB/mo (1080p30) / ~810 GB (720p30) |
| GPU + host power | ~300–450 W continuous → ~220–320 kWh/mo |
| Ops labor | Watchdog + alerts keep it near-zero after week 1; budget an hour/week review |
| Human risk to flag | GPU running unattended — verify thermals, power-loss recovery (BIOS auto-power-on), and that nothing else shares the box |

If any of these bite, 24/7 at 720p30 with a leaner program grid is a
legitimate intermediate mode — the policy case doesn't hinge on resolution.
