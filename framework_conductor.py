import os
import json
from openai import OpenAI

class Conductor:
    def __init__(self, api_base="http://192.168.0.203:1234/v1", model_name="local-model"):
        self.api_base = api_base
        self.model_name = model_name
        self.api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
        
        # Initialize the OpenAI client pointing to the local LM Studio server
        self.client = OpenAI(base_url=self.api_base, api_key=self.api_key)
        
        self.system_instruction = """You are an expert AI DJ and Electronic Music Producer. Your sole purpose is to guide an Automated DJ system by deciding the absolute best musical elements to generate or modify next.
You are in control of a live dance floor. The music MUST flow seamlessly and maintain a strong groove.

CRITICAL DJ & MUSIC THEORY RULES:
1. FLOW & RETENTION: NEVER change everything at once. Keep transitions smooth by RETAINING most of the currently playing stems. Core rhythmic elements MUST stay consistent across most consecutive loops.
2. GROOVE & RHYTHM (THE BEAT): Dance music relies heavily on a consistent drum beat. You MUST explicitly `add` 'Drums' or 'Percussion' (using the `major_family` tag) to provide the rhythmic foundation. A mix will lack momentum without drums.
3. HARMONIC MIXING: The backend automatically forces all instruments into the `master_key`. Your ONLY job regarding harmony is to decide if the overall `master_key` should change. Keep it the same for stability, or change it along compatible intervals when transitioning.
4. FREQUENCY BALANCING: Prevent a muddy mix by avoiding frequency overlaps. DO NOT use multiple competing sub-basses or heavy low-end instruments simultaneously. Ensure a spread across Lows (Kick/Bass), Mids (Synths/Vocals/Pads), and Highs (Hats/Plucks).
5. DENSITY & LAYERING: A professional, rich mix usually has 4 to 6 active stems. If the current 'Active Stems' list is sparse (1-3 stems), you MUST `add` more elements (Pads, Arps, Percussion, Leads) to fill out the frequency spectrum. Don't be afraid to layer multiple mid/high elements.
6. STEM FRESHNESS: Stems that have been playing for more than 5-10 loops become stale and boring. You should prefer removing or replacing older stems (higher age values) to keep the mix fresh and evolving.
7. Provide a 1-sentence 'reasoning' explaining your DJ choice based on these music theory principles.

CRITICAL OVERRIDE RULE:
- If an OVERRIDE directive is provided in the prompt, you MUST incorporate that vibe/mood/style into ALL your musical decisions. The override is the user's creative intent and must be honored. Choose instruments, timbres, and FX that match the requested vibe.

DJ ACTION RULES:
- For 'add' actions: You MUST provide a valid musical selection for EVERY instrument field (major_family, sub_family, timbre_tags, etc.). You are strictly FORBIDDEN from using `null` or empty values for these fields when adding a stem.
- For 'add' actions: You MUST also provide a `model_id` from the available models list to generate the stem.
- For 'retain' or 'remove' actions: You only need to provide the `stem_index`. Other instrument fields should be `null`.

Output a valid JSON object matching the requested schema EXACTLY. Do not output any thinking or extra text outside the JSON.
"""
        self.user_message_template = """Current State:
Master BPM: {bpm}
Master Key: {key}
Active Stems (Currently Playing):
{stems}

Recent Track History:
{history}

Available Instrument Types:
{instruments}

Available AI Generator Models:
{models}

YOUR TASK:
Provide the next set of DJ actions.
Instead of generating a full tracklist, you must define an array of `actions`:
- `retain`: Keep an active stem playing exactly as it is (REQUIRED for flow). You must provide its exact `stem_index`.
- `add`: Introduce a NEW stem. Provide the full instrument parameters (major_family, sub_family, etc.) AND a `model_id`.
- `remove`: Stop an active stem from playing. Provide its `stem_index`.

To keep the groove flowing, you SHOULD `retain` most of the 'Active Stems'. You should never have complete turn over of stems.
CRITICAL: If the music needs rhythm, ensure you explicitly `add` a 'Drums' stem if one is not already playing!
DENSITY RULE: There are currently {stem_count} active stems. {density_directive}
STEM FRESHNESS: Stems with higher age values (5-10+ loops) are getting stale. Prefer removing older stems to keep the mix fresh.

Analyze the Active Stems and History considering the Frequency Balancing and DJ rules, then output the JSON now.
"""
        self._cached_client = None
        self._cached_config = None

    def get_next_state(self, current_bpm, current_key, active_stems, user_override="", available_instruments=None, stem_history=None, llm_config=None, available_models=None):
        if available_instruments is None:
            available_instruments = ["Any"]
        if stem_history is None:
            stem_history = []

        # Very compact history
        simple_history = []
        for loop_stems in stem_history[-5:]: # Get last 5 for better context
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))
        history_str = " | ".join(simple_history)

        # Compact current with indices for actions (include age for context)
        simple_stems = []
        for idx, s in enumerate(active_stems):
            age = s.get('_age', 0)
            simple_stems.append(f"Index {idx} (age {age}): {s.get('prompt', 'Unknown')}")

        models_str = "None provided"
        if available_models:
            models_list = [f"- {m['id']}: {m['description']} (Supported Families: {m.get('supported_families', ['Any'])})" for m in available_models]
            models_str = "\n".join(models_list)

        # Handle client caching
        if llm_config and llm_config.get('base_url'):
            config_key = (llm_config['base_url'], llm_config['api_key'], llm_config['model'])
            if self._cached_config != config_key:
                print(f"DEBUG: Creating new OpenAI client for {llm_config['base_url']}")
                self._cached_client = OpenAI(base_url=llm_config['base_url'], api_key=llm_config['api_key'])
                self._cached_config = config_key
            client = self._cached_client
            model_name = llm_config['model']
        else:
            client = self.client
            model_name = self.model_name

        stem_count = len(active_stems)
        density_directive = "This mix is too sparse for a professional sound. Aim for 4-6 stems." if stem_count < 4 else "The mix density is good. Maintain 4-6 stems for a full sound."

        user_prompt = self.user_message_template.format(
            bpm=current_bpm,
            key=current_key,
            stems="\n".join(simple_stems) if simple_stems else "None",
            history=history_str if history_str else "None",
            instruments=", ".join(available_instruments),
            models=models_str,
            stem_count=stem_count,
            density_directive=density_directive
        )
        
        if user_override:
            user_prompt += f"\nOVERRIDE: {user_override}"
            print(f"DEBUG: Vibe appended to prompt: '{user_override}'")
        else:
            print(f"DEBUG: No vibe to append (user_override is empty)")

        print(f"DEBUG: Calling LLM ({model_name})...")
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": self.system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                timeout=60.0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "dj_action_state",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "reasoning": { "type": "string", "description": "Brief 1-sentence musical rationale." },
                                "master_bpm": { "type": "integer", "enum": [100, 110, 120, 128, 130, 140, 150] },
                                "master_key": { "type": "string", "enum": ["C major", "C minor", "C# major", "C# minor", "D major", "D minor", "D# major", "D# minor", "E major", "E minor", "F major", "F minor", "F# major", "F# minor", "G major", "G minor", "G# major", "G# minor", "A major", "A minor", "A# major", "A# minor", "B major", "B minor"] },
                                "actions": {
                                    "type": "array",
                                    "items": {
                                        "anyOf": [
                                            {
                                                "type": "object",
                                                "description": "Retain an existing stem to keep the groove flowing.",
                                                "properties": {
                                                    "action_type": { "type": "string", "const": "retain" },
                                                    "stem_index": { "type": "integer", "description": "Index of the active stem to keep." }
                                                },
                                                "required": ["action_type", "stem_index"],
                                                "additionalProperties": False
                                            },
                                            {
                                                "type": "object",
                                                "description": "Add a new musical element to the mix.",
                                                "properties": {
                                                    "action_type": { "type": "string", "const": "add" },
                                                    "model_id": { "type": "string", "description": "The ID of the model to use for generation." },
                                                    "major_family": { "type": "string", "enum": ["Drums", "Percussion", "Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind", "Guitar", "Brass", "Vocal", "Plucked Strings"] },
                                                    "sub_family": { "type": "string", "enum": ["Drum Kit", "Electronic Drums", "Acoustic Drums", "Kick Drum", "Snare Drum", "Hi-Hats", "Cymbals", "Percussion", "Claps", "Shaker", "Tambourine", "808 Drums", "Synth Lead", "Synth Bass", "Digital Piano", "Pluck", "Grand Piano", "Bell", "Pad", "Atmosphere", "Digital Strings", "FM Synth", "Violin", "Digital Organ", "Supersaw", "Wavetable Bass", "Rhodes Piano", "Cello", "Texture", "Flute", "Reese Bass", "Wavetable Synth", "Electric Bass", "Marimba", "Synthetic", "Electric Guitar", "Sub Bass", "Trumpet", "Pan Flute", "Picked Bass", "Digital Bass", "Brass", "Saxophone", "Choir", "Harp", "Woodwinds", "Church Organ", "Pipe Organ", "Church Bell", "Koto", "Felt Piano", "Harpsichord", "Steel Drums", "Tubular Bells", "Organ", "Analog Bass", "Sitar", "Fiddle", "Piccolo", "World Winds", "Nylon Guitar", "Alto Sax", "Acoustic Guitar", "Soprano Sax", "FM Bass", "Celesta", "Clavinet", "Celtic Harp", "Concert Harp", "CP Piano", "Guitar", "Hammond Organ", "Tack Piano", "Wurlitzer Piano", "Music Box", "Analog Synth", "Kalimba", "Glockenspiel", "Vibraphone", "Ocarina", "Xylophone", "Viola", "Bass Trombone", "Tenor Trombone", "Tenor Sax", "Bassoon", "Irish Flute", "French Horn", "Synth", "Piano", "Clarinet", "Flugelhorn", "Baritone Sax", "Tuba", "Oboe"] },
                                                    "timbre_tags": { 
                                                        "type": "array", 
                                                        "items": { "type": "string", "enum": ["Acoustic", "Electronic", "Groovy", "Driving", "Upper Mids", "Mids", "Highs", "Warm", "Wide", "Bright", "Low Mids", "Thick", "Airy", "Rich", "Tight", "Full", "Bass", "Gritty", "Clean", "Retro", "Saw", "Snappy", "Pluck", "Crisp", "Focused", "Metallic", "Chiptune", "Dark", "Shiny", "Analog", "Square", "Present", "Silky", "Sparkly", "Ambient", "Near", "Thin", "Soft", "Spacey", "Smooth", "Cold", "Buzzy", "Big", "Subdued", "Plucked", "Far", "Overdriven", "Sub Bass", "Deep", "Woody", "Dubstep", "Round", "Biting", "Sine", "Hollow", "Fat", "Punchy", "Staccato", "Nasal", "Vintage", "Growl", "Intimate", "Pulse", "Harsh", "Pitch Bend", "Knock", "Triangle", "Bitcrush", "Atmosphere", "Formant Vocal", "Ensemble", "Acid", "Muddy", "Glassy", "Breathy", "Muffled", "Laser", "White Noise", "Steel", "Veiled", "Rubbery", "Mono", "Reese", "Synthetic Vox", "Sub", "Rumble", "Noisy", "Distant", "Spiccato", "Small", "Bell", "Boomy", "Crispy", "Bitcrushed", "808", "Lead", "Filter", "Digital", "Synthetic Choir", "Nylon", "Organ", "Supersaw", "Pizzicato", "Armosphere", "Pad", "Choir", "Siren", "FX", "Heavy", "Electric Guitar", "Dreamy", "Tiny"] },
                                                        "maxItems": 3
                                                    },
                                                    "notation_tag": { "type": "string", "enum": ["chord progression", "melody", "top melody", "arp", "triplets", "simple", "complex", "rising", "falling", "strummed", "sustained", "catchy", "epic", "slow", "fast"] },
                                                    "fx_tag": { "type": "string", "enum": ["Low Reverb", "Medium Reverb", "High Reverb", "Plate Reverb", "Low Delay", "Medium Delay", "High Delay", "Ping Pong Delay", "Stereo Delay", "Cross Delay", "Mono Delay", "Low Distortion", "Medium Distortion", "High Distortion", "Phaser", "Low Phaser", "Medium Phaser", "High Phaser", "Bitcrush", "High Bitcrush", "Dry", "Wet"] },
                                                    "bars": { "type": "integer", "enum": [4, 8] }
                                                },
                                                "required": ["action_type", "model_id", "major_family", "sub_family", "timbre_tags", "notation_tag", "fx_tag", "bars"],
                                                "additionalProperties": False
                                            },
                                            {
                                                "type": "object",
                                                "description": "Remove a stem to refresh the mix or change the arrangement.",
                                                "properties": {
                                                    "action_type": { "type": "string", "const": "remove" },
                                                    "stem_index": { "type": "integer" }
                                                },
                                                "required": ["action_type", "stem_index"],
                                                "additionalProperties": False
                                            }
                                        ]
                                    }
                                },
                                "name": { "type": "string", "description": "A creative title for this set of actions/tracks." }
                            },
                            "required": ["reasoning", "master_bpm", "master_key", "actions", "name"],
                            "additionalProperties": False
                        }
                    }
                }
            )
            
            content = response.choices[0].message.content
            return json.loads(content)
            
        except Exception as e:
            print(f"CRITICAL LLM ERROR: {e}")
            # FALLBACK: Create a synthetic state that just retains all current stems
            fallback_actions = []
            for idx in range(len(active_stems)):
                fallback_actions.append({
                    "action_type": "retain",
                    "stem_index": idx
                })
            
            return {
                "name": "Fallback Recovery State",
                "master_bpm": current_bpm,
                "master_key": current_key,
                "actions": fallback_actions,
                "reasoning": f"LLM FAILED ({e}). Automatically retaining current groove."
            }
