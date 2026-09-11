# YouTube Live Streaming

Push the live mixer output to a YouTube Live channel as an H.264 video with an
audio-reactive visualizer, encoded and shipped by a single FFmpeg subprocess.

```
Mixer ──broadcast_audio()──▶ audio_clients queue ──▶ YouTubeRelay writer thread
                                                            │ s16le PCM on stdin
                                                            ▼
                                              FFmpeg (AAC + showcqt + libx264)
                                                            │ RTMP/FLV
                                                            ▼
                                              rtmp://a.rtmp.youtube.com/live2/<key>
```

The relay registers through the same `audio_clients` dispatch the browser
streaming endpoint uses — the mixer loop is untouched. The queue stays
registered across FFmpeg restarts; if the process dies it respawns with linear
backoff (up to `max_restarts=3` per session) and drops only the blocks drained
while down.

## First run

1. **Get a stream key** — YouTube Studio → Go Live → "Streaming software".
   Copy the key (it is a secret: anyone holding it can stream to your
   channel).
2. **Configure** — either set `YOUTUBE_STREAM_KEY` in `.env`, or:

   ```bash
   curl -X PUT http://localhost:8000/api/youtube/config \
     -H "Authorization: Basic $(printf 'dj:YOURPASS' | base64)" \
     -H "Content-Type: application/json" \
     -d '{"stream_key": "xxxx-xxxx-xxxx-xxxx"}'
   ```

3. **Arm the relay** (before or during a set — YouTube shows "waiting for
   stream data" until the first PCM block):

   ```bash
   curl -X POST http://localhost:8000/api/youtube/stream/start \
     -H "Authorization: Basic ..." -H "Content-Type: application/json" \
     -d '{"visualizer": "cqt", "resolution": "1920x1080", "fps": 30}'
   ```

4. **Start the music** (`is_generating=true` or play back a show).
5. **Stop** with `POST /api/youtube/stream/stop`.

## API

| Endpoint | Purpose |
|----------|---------|
| `POST /api/youtube/stream/start` | Spawn relay + FFmpeg (409 if active) |
| `POST /api/youtube/stream/stop` | Graceful stop; idempotent |
| `GET /api/youtube/stream/status` | `active`, `process_alive`, `restarts`, `dropped_blocks`, `bytes_sent`, `last_error` (key-scrubbed) |
| `GET/PUT /api/youtube/config` | Ingest URL + stream key (masked `****xxxx` in responses) |

Visualizers: `cqt` (spectrum, default), `waves` (oscilloscope lines),
`spectrum` (scrolling spectrogram). All are FFmpeg-native — no fonts required
(`axis=0` keeps showcqt font-free for containers).

## Requirements

- `ffmpeg` in PATH (already required by the app) built with `libx264` and
  `aac` — standard in distro and static builds.
- Upload bandwidth: ~5 Mbps sustained for 1080p30 (2.7 Mbps for 720p30).
- Auth: these endpoints require DJ credentials like the rest of the
  owner-facing API.

## Going 24/7 (roadmap)

The relay was designed for it: call `relay.reset_restart_budget()` from a
watchdog on an interval, and restart the relay when `status().active` is
false. Persistent stream keys (`YOUTUBE_STREAM_KEY` in env) survive app
restarts; pair with the existing cleanup/onboarding health checks.

## Compliance notes

### Model licenses (verified 2026 — re-check before monetizing)

All three configured models (Foundation-1, RC_Infinite_Pianos, Vocal_Textures) are
fine-tunes of `stabilityai/stable-audio-open-1.0` under the **Stability AI
Community License** (canonical text: stability.ai/community-license-agreement).
The `stable-audio-tools` inference engine itself is **MIT licensed** — no
restrictions.

What it means for a monetized YouTube channel:

- **You own the outputs.** §IV.c(iii): outputs of the Models or Derivative
  Works (fine-tunes) are yours — stream and monetize them at your discretion.
  The license's definition of Derivative Works explicitly *excludes* model
  outputs, so broadcasting audio does not "distribute the model."
- **Revenue cliff:** free commercial use while total annual revenue (any
  source, you + affiliates) is **< USD $1M**. At $1M+ the license terminates
  automatically; an Enterprise License from Stability AI is required.
- **Registration:** commercial use requires registering with Stability AI —
  free "Get license" flow at stability.ai/license.
- **Attribution:** §IV(a) requires prominently displaying
  **"Powered by Stability AI"** for products/services using the models — put
  it in the channel/stream description and this app's UI.
- **AUP:** outputs must comply with Stability's Acceptable Use Policy; outputs
  must not train a *foundational* generative model (fine-tunes of the same
  models are fine).
- No Stability trademarks beyond that attribution; license is revocable on
  breach (outputs you already own remain yours).

### YouTube policies

- **AI disclosure**: required only for *realistic* synthetic content (real
  people saying/doing things, realistic scenes) — see
  support.google.com/youtube/answer/14328491. Instrumental AI music with an
  abstract visualizer does **not** require the "AI use" disclosure label.
  YouTube may still auto-apply an AI label via detection; disclosing does not
  limit monetization, so voluntary disclosure is a cheap hedge. If you enable
  the Vocal Textures model (synthetic singing), lean toward disclosing.
- **Monetization (YPP)**: the "inauthentic content" policy (renamed from
  "repetitious content", July 15 2025 — support.google.com/youtube/answer/1311392)
  explicitly lists as not eligible: *"AI-generated content made with generic or
  unoriginal templates giving the impression of mass production without adding
  the creator's original, authentic insights or perspective."* It applies to
  live streams ("video" in the policy includes them) and is enforced at
  **channel level** — the whole channel can lose monetization. The Conductor's
  continuous re-arrangement (5–10 loop stem rotation, key/BPM changes) is the
  core mitigation; scheduled live sets with per-show titles/descriptions and
  distinct visual identities are lower-risk than a static 24/7 loop. A
  non-monetized stream carries no YPP risk at all.
- This is engineering guidance, not legal advice; verify current terms at
  stability.ai/license and YouTube Help before launch.
