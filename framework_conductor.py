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
1. FLOW & RETENTION: NEVER change everything at once. Keep transitions smooth by RETAINING most of the currently playing stems. Core rhythmic elements (Kick, Bass, Main Beat) MUST stay consistent across most consecutive loops.
2. HARMONIC MIXING: The backend automatically forces all instruments into the `master_key`. Your ONLY job regarding harmony is to decide if the overall `master_key` should change. Keep it the same for stability, or change it along compatible intervals (like relative major/minor) when doing a large transition.
3. FREQUENCY BALANCING: Prevent a muddy mix by avoiding frequency overlaps. DO NOT use multiple competing sub-basses or heavy low-end instruments simultaneously. Ensure a spread across Lows (Kick/Bass), Mids (Synths/Vocals/Pads), and Highs (Hats/Plucks).
4. ARRANGEMENT & ENERGY: 
   - Build up: Introduce new high-frequency percussion or rising synths.
   - Breakdown: Drop the Lows (Kick and Bass), leaving atmospheric Mid/High elements.
   - The Drop: Bring back the Kick, Bass, and Main Lead simultaneously for maximum impact.
5. Provide a 1-sentence 'reasoning' explaining your DJ choice based on these music theory principles.

Output a valid JSON object matching the requested schema EXACTLY. Do not output any thinking or extra text outside the JSON.
"""
        self.user_message_template = """Current State:
Master BPM: {bpm}
Master Key: {key}
Current Energy Level: {energy} (1-10)
Active Stems (Currently Playing):
{stems}

Recent Track History:
{history}

Available Instrument Types:
{instruments}

YOUR TASK:
Provide the next set of DJ actions and determine the new energy level.
Instead of generating a full tracklist, you must define an array of `actions`:
- `retain`: Keep an active stem playing exactly as it is (REQUIRED for flow). You must provide its exact `stem_index`.
- `add`: Introduce a NEW stem. Provide the full instrument parameters (major_family, sub_family, etc.).
- `remove`: Stop an active stem from playing. Provide its `stem_index`.
- `mix`: Change the volume or mute state of an active stem. Provide `stem_index`, `target_volume` (0.0 to 2.0), and `is_muted`.

To keep the groove flowing, you SHOULD `retain` most of the 'Active Stems'.
To evolve the track, you MAY `add` 1 to 2 new stems, `remove` an existing one to drop the energy, or `mix` an existing one to fade it in/out.

Provide a `next_energy_level` (1-10) to track the intensity progression of the set.
Analyze the Active Stems and History considering the Frequency Balancing and DJ rules, then output the JSON now.
"""
        self._cached_client = None
        self._cached_config = None

    def get_next_state(self, current_bpm, current_key, active_stems, user_override="", available_instruments=None, stem_history=None, llm_config=None, energy_level=5):
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

        # Compact current with indices for actions
        simple_stems = []
        for idx, s in enumerate(active_stems):
            simple_stems.append(f"Index {idx}: {s.get('prompt', 'Unknown')}")

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

        user_prompt = self.user_message_template.format(
            bpm=current_bpm,
            key=current_key,
            energy=energy_level,
            stems="\n".join(simple_stems) if simple_stems else "None",
            history=history_str if history_str else "None",
            instruments=", ".join(available_instruments)
        )
        
        if user_override:
            user_prompt += f"\nOVERRIDE: {user_override}"
            
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
                                "name": { "type": "string", "description": "A creative title for this set of actions/tracks." },
                                "reasoning": { "type": "string", "description": "Brief 1-sentence musical rationale." },
                                "master_bpm": { "type": "integer", "enum": [100, 110, 120, 128, 130, 140, 150] },
                                "master_key": { "type": "string", "enum": ["C major", "C minor", "C# major", "C# minor", "D major", "D minor", "D# major", "D# minor", "E major", "E minor", "F major", "F minor", "F# major", "F# minor", "G major", "G minor", "G# major", "G# minor", "A major", "A minor", "A# major", "A# minor", "B major", "B minor"] },
                                "next_energy_level": { "type": "integer", "description": "1-10 energy level." },
                                "actions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "action_type": { "type": "string", "enum": ["retain", "add", "remove", "mix"] },
                                            "stem_index": { "type": ["integer", "null"] },
                                            "target_volume": { "type": ["number", "null"] },
                                            "is_muted": { "type": ["boolean", "null"] },
                                            "major_family": { "type": ["string", "null"], "enum": ["Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind", "Guitar", "Brass", "Vocal", "Plucked Strings", null] },
                                            "sub_family": { "type": ["string", "null"], "enum": ["Synth Lead", "Synth Bass", "Digital Piano", "Pluck", "Grand Piano", "Bell", "Pad", "Atmosphere", "Digital Strings", "FM Synth", "Violin", "Digital Organ", "Supersaw", "Wavetable Bass", "Rhodes Piano", "Cello", "Texture", "Flute", "Reese Bass", "Wavetable Synth", "Electric Bass", "Marimba", "Synthetic", "Electric Guitar", "Sub Bass", "Trumpet", "Pan Flute", "Picked Bass", "Digital Bass", "Brass", "Saxophone", "Choir", "Harp", "Woodwinds", "Church Organ", "Pipe Organ", "Church Bell", "Koto", "Felt Piano", "Harpsichord", "Steel Drums", "Tubular Bells", "Organ", "Analog Bass", "Sitar", "Fiddle", "Piccolo", "World Winds", "Nylon Guitar", "Alto Sax", "Acoustic Guitar", "Soprano Sax", "FM Bass", "Celesta", "Clavinet", "Celtic Harp", "Concert Harp", "CP Piano", "Guitar", "Hammond Organ", "Tack Piano", "Wurlitzer Piano", "Music Box", "Analog Synth", "Kalimba", "Glockenspiel", "Vibraphone", "Ocarina", "Xylophone", "Viola", "Bass Trombone", "Tenor Trombone", "Tenor Sax", "Bassoon", "Irish Flute", "French Horn", "Synth", "Piano", "Clarinet", "Flugelhorn", "Baritone Sax", "Tuba", "Oboe", null] },
                                            "timbre_tags": { 
                                                "type": ["array", "null"], 
                                                "items": { "type": "string", "enum": ["Upper Mids", "Mids", "Highs", "Warm", "Wide", "Bright", "Low Mids", "Thick", "Airy", "Rich", "Tight", "Full", "Bass", "Gritty", "Clean", "Retro", "Saw", "Snappy", "Pluck", "Crisp", "Focused", "Metallic", "Chiptune", "Dark", "Shiny", "Analog", "Square", "Present", "Silky", "Sparkly", "Ambient", "Near", "Thin", "Soft", "Spacey", "Smooth", "Cold", "Buzzy", "Big", "Subdued", "Plucked", "Far", "Overdriven", "Sub Bass", "Deep", "Woody", "Dubstep", "Round", "Biting", "Sine", "Hollow", "Fat", "Punchy", "Staccato", "Nasal", "Vintage", "Growl", "Intimate", "Pulse", "Harsh", "Pitch Bend", "Knock", "Triangle", "Bitcrush", "Atmosphere", "Formant Vocal", "Ensemble", "Acid", "Muddy", "Glassy", "Breathy", "Muffled", "Laser", "White Noise", "Steel", "Veiled", "Rubbery", "Mono", "Reese", "Synthetic Vox", "Sub", "Rumble", "Noisy", "Distant", "Spiccato", "Small", "Bell", "Boomy", "Crispy", "Bitcrushed", "808", "Lead", "Filter", "Digital", "Synthetic Choir", "Nylon", "Organ", "Supersaw", "Pizzicato", "Armosphere", "Pad", "Choir", "Siren", "FX", "Heavy", "Electric Guitar", "Dreamy", "Tiny"] },
                                                "maxItems": 3
                                            },
                                            "notation_tag": { "type": ["string", "null"], "enum": ["chord progression", "melody", "top melody", "arp", "triplets", "simple", "complex", "rising", "falling", "strummed", "sustained", "catchy", "epic", "slow", "fast", null] },
                                            "fx_tag": { "type": ["string", "null"], "enum": ["Low Reverb", "Medium Reverb", "High Reverb", "Plate Reverb", "Low Delay", "Medium Delay", "High Delay", "Ping Pong Delay", "Stereo Delay", "Cross Delay", "Mono Delay", "Low Distortion", "Medium Distortion", "High Distortion", "Phaser", "Low Phaser", "Medium Phaser", "High Phaser", "Bitcrush", "High Bitcrush", "Dry", "Wet", null] },
                                            "bars": { "type": ["integer", "null"], "enum": [4, 8, null] }
                                        },
                                        "required": ["action_type"],
                                        "additionalProperties": False
                                    }
                                }
                            },
                            "required": ["name", "reasoning", "master_bpm", "master_key", "actions", "next_energy_level"],
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
                "next_energy_level": energy_level,
                "actions": fallback_actions,
                "reasoning": f"LLM FAILED ({e}). Automatically retaining current groove."
            }

