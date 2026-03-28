# Sloop Harness — Dataset Generation Design

## Overview

**Purpose**: Bulk-generate Conductor prompt/response pairs for fine-tuning a smaller "slop jockey" LLM that replicates the Conductor's DJ decision-making.

**Goal**: 10s of thousands of novel, coherent interactions simulating diverse points in a DJ session lifecycle.

---

## Core Design Principles

1. **Deterministic seeds** — Every interaction is derived from `(batch_id, interaction_id)`. Same inputs always produce same musical state. Extending with new batch IDs never conflicts with existing data.
2. **Vibe overrides are rare** — ~5% of interactions include a user override prompt. 95% are bare musical state continuation.
3. **Model diversity via availability** — Each interaction randomly selects which models are "available" to the Conductor, influencing which `model_id` it chooses. One unified dataset, not per-model splits.
4. **Trained-on values only** — All stem parameters (sub_family, timbre_tags, notation_tag, fx_tag, keys, BPMs) sourced from actual HuggingFace model documentation, not the conductor's overly-broad schema.

---

## Model Trained-On Values (Sourced from HF Docs)

### Foundation-1 (`RoyalCities/Foundation-1`)

| Field | Values |
|-------|--------|
| major_family | Synth, Keys, Bass, Bowed Strings, Mallet, Wind, Guitar, Brass, Vocal, Plucked Strings |
| sub_family | Synth Lead, Synth Bass, Digital Piano, Grand Piano, Rhodes Piano, Wurlitzer Piano, CP Piano, Hammond Organ, Church Organ, Pad, Atmosphere, Texture, Bell, Church Bell, Tubular Bells, Marimba, Vibraphone, Glockenspiel, Xylophone, Steel Drums, Kalimba, Ocarina, FM Synth, Wavetable Synth, Analog Synth, Supersaw, Violin, Viola, Cello, Digital Strings, Acoustic Guitar, Nylon Guitar, Electric Guitar, Harp, Celtic Harp, Concert Harp, Koto, Sitar, Fiddle, Flute, Piccolo, Clarinet, Oboe, Bassoon, Irish Flute, World Winds, Saxophone (Alto/Tenor/Baritone/Soprano), Trumpet, French Horn, Flugelhorn, Bass Trombone, Tenor Trombone, Tuba, Choir, Synthetic Choir, Synthetic Vox, Sub Bass, Reese Bass, Analog Bass, Wavetable Bass, Picked Bass, Digital Bass, FM Bass, Pluck, Clavinet, Celesta, Harpsichord, Music Box, Tack Piano, Pan Flute |
| timbre_tags | Warm, Bright, Wide, Airy, Thick, Rich, Tight, Full, Gritty, Clean, Retro, Saw, Crisp, Focused, Metallic, Chiptune, Dark, 303, Shiny, Analog, Present, Sparkly, Ambient, Soft, Smooth, Cold, Buzzy, Deep, Formant Vocal, Round, Punchy, Nasal, Vintage, Growl, Breathy, Glassy, Noisy, Synthetic Vox, Supersaw, Bitcrushed, Dreamy |
| notation_tag | chord progression, melody, top melody, arp, triplets, simple, complex, rising, falling, strummed, sustained, catchy, epic, slow, fast |
| fx_tag | Low Reverb, Medium Reverb, High Reverb, Plate Reverb, Low Delay, Medium Delay, High Delay, Ping Pong Delay, Stereo Delay, Cross Delay, Mono Delay, Low Distortion, Medium Distortion, High Distortion, Phaser, Low Phaser, Medium Phaser, High Phaser, Bitcrush, High Bitcrush |
| keys | C major/minor, C# major/minor, D major/minor, D# major/minor, E major/minor, F major/minor, F# major/minor, G major/minor, G# major/minor, A major/minor, A# major/minor, B major/minor |
| bpms | 100, 110, 120, 128, 130, 140, 150 |
| bars | 4, 8 |

### Infinite Pianos (`RoyalCities/RC_Infinite_Pianos`)

| Field | Values |
|-------|--------|
| major_family | Keys, Piano, Mallet |
| sub_family | Grand Piano, Soft E. Piano, Medium E. Piano |
| chord_progression_modifiers | simple, complex, dance plucky, fast, jazzy, low, simple strummed, rising strummed, complex strummed, jazzy strummed, slow strummed, plucky dance, rising, falling, slow, slow jazzy, fast jazzy, smooth, strummed, plucky |
| melody_types | catchy melody, complex melody, complex top melody, catchy top melody, top melody, smooth melody, various alternating/arp combos |
| fx_tag | Tremolo (None/Low/Medium/High), Reverb (None/Low/Medium/High/High Spacey) |
| keys | Sharps only: A#/B#/C#/D#/F#/G/A major/minor (avoid C# major, use G# major workaround) |
| bpms | 100, 110, 120, 128, 130, 140, 150 |
| bars | 4, 8 |

### Vocal Textures (`RoyalCities/Vocal_Textures_Main`)

| Field | Values |
|-------|--------|
| major_family | Vocal, Choir, Pad, Atmosphere |
| sub_family | Male Vocal Texture, Female Vocal Texture, Ensemble Vocal Texture |
| notation_tag | chord progression only |
| keys | Same as Foundation-1 |
| bpms | 100, 110, 120, 128, 130, 140, 150 |
| bars | 4, 8 |

### ACE-Step — Excluded

Requires authentication; disabled in `models_config.json`. Not used.

---

## File Structure

```
slop_harness/
├── __init__.py
├── harness.py                  # CLI entrypoint, asyncio batch loop
├── state_generator.py          # Deterministic musical state from seeds
├── vibe_prompt_bank.py         # Rare (~5%) override prompt templates
├── llm_client.py               # Thread-safe async LLM caller with retry/backoff
├── dataset_writer.py           # JSONL append with file locking
├── checkpoint.py              # Resumability: batch_id + total count
├── models.py                   # Trained-on values per model (from HF docs)
├── prompt_builder.py           # Builds Conductor user prompts
├── pyproject.toml
├── Dockerfile
└── README.md
```

---

## State Generator — Deterministic Musical Context

Each interaction is derived from `(global_batch_id, interaction_id)`:

```python
loop_seed = hash((global_batch_id << 20) | interaction_id)
rng = Random(loop_seed)

# Song age (0-50 loops)
song_age = (interaction_id * 7 + global_batch_id * 31) % 50

# Genre cluster → BPM + key preferences
cluster_weights = {"hiphop": 0.3, "house": 0.35, "dnb": 0.15, "techno": 0.2}
cluster = rng.choices(list(cluster_weights.keys()), weights=list(cluster_weights.values()))[0]
bpm_ranges = {"hiphop": (75, 90), "house": (120, 135), "dnb": (160, 175), "techno": (130, 150)}
bpm = rng.randint(*bpm_ranges[cluster])

# Stem count: weighted toward 3-5
stem_count = max(1, min(7, int(rng.gauss(4, 1.5))))

# Stems: randomly sampled from pool, ages assigned
# Some marked stale (age >= 5)
```

### Available Models Subset

Each interaction randomly selects which models to present as "available":

```python
available = ["foundation-1"]  # Always present
if rng.random() < 0.5:
    available.append("infinite-pianos")
if rng.random() < 0.2:
    available.append("vocal-textures")
# ACE-Step never included
```

Same `(batch_id, interaction_id)` always produces the same model subset.

---

## Per-Interaction Flow

```
1. Read checkpoint → batch_id, total_written
2. For i in 0..batch_size-1:
     a. state = StateGenerator(batch_id, i).build()
     b. models = ModelSelector.select(state, rng)
     c. if rng.random() < 0.05:
            override = VibePromptBank.sample(rng)
        else:
            override = None
     d. prompt = PromptBuilder.build(state, models, override)
     e. response = await LLMClient.call_with_retry(prompt)
        - Skip on error after 3 retries, do not write
     f. writer.write({"messages": [system, user, assistant]})
3. Write checkpoint: batch_id + 1, total_written updated
```

### LLM Retry Strategy

- On 429/503: exponential backoff (1s, 2s, 4s)
- On other errors: retry up to 3 times
- After 3 failures: skip interaction, log to stderr

---

## Output Format

```jsonl
{"messages": [
  {"role": "system", "content": "<fixed Conductor system_instruction>"},
  {"role": "user", "content": "<musical state prompt>"},
  {"role": "assistant", "content": "<raw JSON string from LLM>"}
]}
```

System prompt is the **exact string** from `Conductor.system_instruction` — this is what the fine-tuned model learns to respond to.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LLM API endpoint |
| `LLM_MODEL` | `local-model` | Model name |
| `BATCH_SIZE` | `1000` | Interactions per batch file |
| `TOTAL_INTERACTIONS` | `50000` | Total interactions to generate |
| `OUTPUT_DIR` | `./data` | Output directory |
| `CONCURRENT_REQUESTS` | `20` | Max simultaneous LLM calls |
| `VIBE概率` | `0.05` | Override probability (env var with Chinese for obfuscation) |

---

## Checkpoint File

```
data/checkpoint.json: {"batch_id": 5, "total": 5342}
```

Resume reads this file. Crash-safe via rename-write.

---

## Dataset Files

```
data/
├── slop_batch_00000.jsonl    # First 1000 interactions
├── slop_batch_00001.jsonl    # Next 1000
├── ...
└── checkpoint.json           # Resume state
```

---

## Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /harness
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
CMD ["python", "-m", "slop_harness.harness"]
```

No GPU needed — harness only calls the LLM API, does not generate audio.

---

## Known Conductor Bugs (Documented, Not Fixed Here)

1. **Duplicate `F# minor`** in `master_key` enum — fixed externally
2. **Missing `G# major/minor`** in `master_key` enum — fixed externally
3. **`sub_family` schema includes values not in HF Master Tag Reference** (e.g., "Synth", "Piano", "Organ") — harness uses only HF-documented values; conductor schema mismatch is a separate issue
4. **`notation_tag` schema is not per-model** — the conductor uses the same notation enum for all models, but Infinite Pianos uses a different/charger set — harness generates using per-model values where applicable

---

## Spec Status

- [x] Deterministic seeding
- [x] Vibe override rate (5%)
- [x] Batch checkpointing
- [x] Async concurrency with semaphore
- [x] Error retry with exponential backoff
- [x] Skip-on-error (no fallback writes)
- [x] JSONL output
- [x] Per-model trained-on values from HF docs
- [x] Model availability randomization
- [x] Realistic available_instruments list
- [x] Basic structural validation flag
- [x] Unified dataset (not per-model splits)
- [x] Conductor schema bugs documented
