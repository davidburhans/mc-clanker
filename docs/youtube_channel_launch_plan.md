# YouTube Channel Launch Plan — MC Clanker

Channel concept built around **scheduled live DJ sets** performed by the AI
Conductor, with a human operator taking chat requests. This doc covers
branding, content strategy, and the launch sequence. Streaming mechanics,
model licenses, and YouTube policy analysis live in
[`youtube_live.md`](youtube_live.md) — read that first; this plan builds on it.

---

## 1. Concept & Positioning

**One-liner:** *Every Friday, an AI builds a techno set from nothing, live.
Chat steers the music.*

Positioning vs. existing formats:

| Existing format | Why we're different |
|---|---|
| Lofi Girl-style 24/7 radio | Every set is a distinct event; nothing is pre-recorded |
| Human DJ livestreams | The set is literally generated live — stems never existed before the stream started |
| AI music upload channels | It's a *performance*: the Conductor decides transitions in real time and its reasoning is visible |

The moat is **liveness + interactivity**: viewers change the music (vibe
prompts, BPM/key overrides via `state.user_override` /
`state.target_bpm_override`), which no uploaded-mix channel can offer, and
which directly supplies the "creator's original, authentic insights or
perspective" YouTube's inauthentic-content policy demands.

---

## 2. Naming

**Recommendation: keep `MC Clanker`.** "MC" reads as master of ceremonies,
"Clanker" is memorable, machine-flavored, and slightly self-deprecating —
perfect for an AI DJ persona. It's also the repo name, so devlog /
build-in-public content slots in later without a second brand.

Alternatives evaluated (keep as backup if trademark search surfaces issues):
Clanker FM, Artificial Selector, Neural Vinyl, Overclocked.

**Persona split:**
- **MC Clanker** — the AI DJ (the Conductor + generators).
- **You** — the operator/host. Credit as "operator" / "human in the loop."
  Chat talks to you; you relay their prompts to the machine. This two-character
  dynamic is the show.

Check before committing: YouTube search for existing "MC Clanker" channels,
same-name artists on Spotify/DistroKid, and domain/handle availability.

---

## 3. Brand Identity

### Palette

| Role | Name | Hex | Use |
|---|---|---|---|
| Background | Void Black | `#0B0B10` | Thumbnails, stream background |
| Primary accent | Circuit Green | `#39FF6E` | Logo, live badge, "data" text |
| Secondary accent | Signal Magenta | `#FF2E92` | Show-specific accents, alerts |
| Neutral | Steel | `#9AA3B2` | Body text |

CRT-terminal duotone (green/magenta on black) — reads "machine" at thumbnail
scale, high contrast on both light and dark YouTube UI.

### Typography (all free/OFL — no licensing landmines)

- **Titles:** Archivo Black (heavy, condensed feel, survives 168px thumbnail scale)
- **Accent/data:** JetBrains Mono — BPM, key, loop count readouts. Monospace =
  machine telemetry.

### Logo

Mark: a schematic robot head wearing headphones, drawn in single-weight
lineart (green on black) — must be legible at 98×98 profile size. Banner and
thumbnail variants reuse the lineart.

### Banner (2048×1152, safe area 1546×423)

Center: "MC CLANKER — LIVE SETS, GENERATED IN REAL TIME". Left of safe area:
logo mark. Right of safe area: schedule strip
("FRI — TECHNO / SUN — AMBIENT"). No text outside the safe area.

### Thumbnail system (1280×720)

Fixed template, color-banded by show (see §4):

```
┌──────────────────────────────────────────────┐
│ [show color band]  FW                        │
│ ───────────────    NIGHT      [visualizer    │
│ MC CLANKER ▸ LIVE  FIRMWARE    still from     │
│ 128 BPM · A MIN    FRIDAY      last set]     │
│                                8PM ET        │
└──────────────────────────────────────────────┘
```

- Left third: black panel, show monogram + show name + LIVE badge.
- Right two-thirds: **actual visualizer still from the previous set** (the
  `cqt`/`waves` frames are distinctive and free).
- Data chips (BPM/key) in JetBrains Mono reinforce the "live telemetry" story.
- Because the template is constant and only the photo + monogram change, every
  set looks like part of a series — which is exactly the "distinct but branded"
  sweet spot for the inauthentic-content policy.

### Stream visual identity per show

Map the three FFmpeg visualizers to shows (per `youtube_live.md`):

| Show | Visualizer | Res/fps |
|---|---|---|
| Firmware Friday (techno) | `cqt` spectrum | 1080p30 |
| Overclock Wednesday (drum & bass / faster) | `waves` | 1080p30 |
| Sleep Mode Sunday (ambient) | `spectrum` spectrogram | 1080p30 |

Distinct visual identities per show — explicitly called out in
`youtube_live.md` as lowering YPP risk.

### Audio branding

Generate a 4-bar ident sting with Foundation-1 itself ("dark electronic
stinger, 128 BPM") and open every set with it. Same sting forever = audio logo.
Keep it in the Garage bucket with the other stems.

---

## 4. Show Formats & Schedule

Launch with **two sets/week**, same slots every week (appointment viewing;
YouTube scheduling creates the watch page + notifies subscribers in advance).

| Show | Slot | Vibe prompt seed | Density | Color band |
|---|---|---|---|---|
| **Firmware Friday** | Fri 20:00 ET, 90 min | peak-time techno, hypnotic, rolling bass | 5–6 stems, 126–132 BPM | Magenta |
| **Sleep Mode Sunday** | Sun 21:00 ET, 60 min | ambient, warm pads, slow evolution | 3–4 stems, 70–90 BPM | Green |

Add **Overclock Wednesday** (DnB/breaks, `waves` visualizer) only after 4–6
weeks of consistent two-show cadence.

In-set segment structure (gives each set a narrative arc — more "original
insight" evidence):

1. **Cold open (0–5 min):** ident sting → operator intro → chat warms up.
2. **The Build (5–30):** Conductor starts minimal (drums + bass), one layer per
   loop; operator narrates what the Conductor is doing and why.
3. **Chat Controllers (30–60):** chat calls vibe prompts; operator relays them.
   Every ~10 min: "vibe check" — top-of-chat prompt wins, applied via
   `user_override`.
4. **The Drop Window (60–75):** operator pushes BPM override +10, keys change.
5. **Cool-down + outro (75–90):** show reasoning log excerpt on screen, next
   set teaser, sub ask.

Vary something visible every set (opening genre seed, guest chat prompt theme,
"BPM ladder night") so no two set descriptions are identical.

---

## 5. Metadata System

### Title template

```
MC Clanker LIVE — {Show Name} #{n} | {genre twist this week} (AI-generated, chat picks the vibe)
```

Example: `MC Clanker LIVE — Firmware Friday #12 | Hypnotic Techno & Modular Bleeps (AI-generated, chat picks the vibe)`

### Description template (every set)

```
An AI DJ builds a {genre} set from nothing, live. Every stem you hear is
generated in real time — nothing is pre-recorded. Chat steers the set.

Tonight: {one specific sentence about this set's twist}

⚙️ How it works: an LLM "Conductor" arranges stems (drums, bass, synth) into
loops, and a generative audio model (Foundation-1) performs them. {operator}
relays chat prompts to the machine.

🎵 Music generated with Foundation-1. Powered by Stability AI.
🤖 All music on this stream is AI-generated.

📅 Next set: {date/time} — subscribe + hit the bell.
🎧 Audio: {MP3 direct link if exposed / or "listen again via playlist}

{chapter: 00:00 Cold open / {t1} The Build / {t2} Chat Controllers / ...}
```

The "Powered by Stability AI" line is **required** by the Stability AI
Community License (see `youtube_live.md` §Compliance). The 🤖 line is the
voluntary AI disclosure — cheap hedge, zero monetization cost.

### Tags

`ai dj, live electronic music, generative music, ai music, techno livestream,
live dj set, ambient stream, foundation-1, procedural music, artificial
intelligence music`

### Channel settings checklist

- Handle: `@mcclanker` (verify availability across platforms now, even if only
  YouTube is used at launch)
- Category: Music · Audience: Not made for kids
- Channel keywords: AI DJ, generative music, live electronic
- Phone-verify the channel (unlocks custom thumbnails + long streams)
- Live defaults: 1080p, "unlisted pre-stream" for test runs, latency =
  low-latency for chat interaction

---

## 6. Interactivity = the show

The API surface maps directly to chat mechanics:

| Chat mechanic | mc-clanker surface |
|---|---|
| Vibe prompt relay ("play something euphoric") | `state.user_override` via `/api/state` |
| BPM/key votes | `target_bpm_override` / `target_key_override` |
| Stem solo ("drums only!") | `/api/stems/{i}/solo` |
| "What is it doing?" moments | `/api/reasoning/logs` — read a Conductor reasoning excerpt on stream |

Once monetized: **Super Chat = priority request.** Paid messages jump the
vibe-prompt queue. Natural live-monetization loop; announce it each set.

Post-set, export the reasoning log (NDJSON route) and screenshot the best 2–3
Conductor decisions for the community tab / Shorts captions. The machine's
"thoughts" are free, unique content no competitor has.

---

## 7. Launch Plan

### Phase 0 — Brand & plumbing (week 0, ~1 week)

- [ ] Name clearance (YT search, Spotify, handle availability) → commit to MC Clanker
- [ ] Design: logo mark, banner, thumbnail template, palette tokens in a
      `brand/` folder (SVG source + PNG exports at platform sizes)
- [ ] Generate ident sting with Foundation-1
- [ ] Phone-verify channel; set category/keywords/defaults
- [ ] **Dry runs (unlisted):** 2× 30-min test streams — verify 1080p30 encode
      (~5 Mbps sustained upload), relay auto-restart behavior
      (`GET /api/youtube/stream/status` → `restarts`, `dropped_blocks`),
      visualizer legibility on phone screens (most live viewers are mobile)
- [ ] Write the per-set runbook (§8) into a checklist

### Phase 1 — Soft launch (weeks 1–4)

- [ ] Schedule first 4 Firmware Fridays + 4 Sleep Mode Sundays via YT Studio
      (schedule = watch page = notification = pre-hype)
- [ ] Record every set (`state.is_show_recording`) — the archive is raw
      material for Phase 2
- [ ] Consistent title/description/thumbnail template from set #1
- [ ] Operator skill-building: practice narrating Conductor decisions; the
      narration is the show
- [ ] Post-set ritual: 1 community post + reasoning-log excerpt within 24h
- [ ] Target: nail the format, not the numbers. Success = you'd watch the VOD

### Phase 2 — Compound (months 2–3)

- [ ] Add Overclock Wednesday if two shows feel routine
- [ ] **VOD uploads:** cut best 20 min of each recording into titled uploads
      ("When chat made it drop the BPM — Firmware Friday #9") — evergreen,
      searchable, feeds the live events
- [ ] **Shorts:** 30–45s visualizer clips synced to the best transition of the
      week + Conductor reasoning caption. Shorts are the discovery engine for
      live channels
- [ ] Community tab polls: vote next week's genre seed / show name
- [ ] Iterate thumbnail A/B through YT's Test & Compare

### Phase 3 — Monetization (when thresholds hit)

- YPP ad tier: 1k subs + 4k public watch hours (12 mo) — **live watch time
  counts toward the 4k**
- Early fan-funding tier (~500 subs, 3 uploads/90d): unlocks Super Thanks /
  Super Chat → enable the "Super Chat = priority request" mechanic
- Before enabling ads: re-verify Stability AI license terms + registration
  (free, stability.ai/license) per `youtube_live.md`; revenue is far below the
  $1M cliff but registration is required for commercial use
- Non-monetized streams carry zero YPP risk — if policy interpretation ever
  feels uncertain, stream with ads off and monetize via Super Chat only

---

## 8. Per-set Runbook

**T-24h:** Schedule stream in YT Studio (title, description, thumbnail,
category). Announce on community tab.

**T-15min:**
1. Run onboarding health checks (DB, LLM endpoint, Garage, GPU worker)
2. `POST /api/youtube/stream/start` with the show's visualizer + 1080p30
3. `GET /api/youtube/stream/status` until `active: true`; YT shows "waiting
   for stream data" — fine
4. Start recording (`is_show_recording = true`)

**T-0:** `is_generating = true` (or play a show warm-up). Open with ident sting.

**During:** monitor `stream/status` (`restarts`, `dropped_blocks`,
`last_error`) on a second screen; relay chat vibe prompts; run the segment
arc from §4.

**Post:**
1. `POST /api/youtube/stream/stop`; confirm graceful drain
2. `is_generating = false`; mark Show ended — recording archived
3. Export reasoning log; pick 2–3 excerpts for community/Shorts
4. Cut VOD clip while the set is fresh

---

## 9. Metrics & Milestones

Track per set (Studio's "Returning viewers" + live dashboard):

| Metric | Why it matters | Healthy signal |
|---|---|---|
| Peak concurrent viewers | Format resonance | Growth across same-show #s |
| Avg view duration | Is the *set* watchable | >15 min by month 3 |
| Chat messages / 10 min | Interactivity is the moat | Rising; prompt quality improving |
| Returning viewers | Appointment TV forming | >30% returning by month 3 |
| Subs per set | Conversion | Uptick on set nights specifically |

Milestones: first set that needs zero firefighting (Phase 1 exit) · 100 subs
(custom URL polish) · 500 subs (fan-funding tier, community polls mature) ·
1k subs + 4k hours (YPP application — re-run the compliance checklist first).

---

## 10. Compliance Quick-Reference

Full analysis in [`youtube_live.md`](youtube_live.md). Launch-blocking items:

1. **"Powered by Stability AI"** attribution in every stream description —
   required by license.
2. **Register** with Stability AI (free) before commercial use.
3. Voluntary **AI-generated music disclosure** line in descriptions — cheap
   hedge; lean harder if Vocal Textures (synthetic vocals) is enabled.
4. **No static 24/7 loop.** Distinct scheduled sets with per-show titles,
   descriptions, visual identities, and narrative arcs are the mitigation.
5. Outputs are yours to stream/monetize (license excludes outputs from
   Derivative Works) — just stay under the $1M/yr revenue cliff (Enterprise
   license above it).
