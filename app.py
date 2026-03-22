import torch
import gradio as gr
from stable_audio_tools import create_model_from_config
from stable_audio_tools.inference.generation import generate_diffusion_cond
from safetensors.torch import load_file
from huggingface_hub import hf_hub_download
import os
import tempfile
import json
import scipy.io.wavfile as wavfile
import numpy as np

MODEL_REPO = "RoyalCities/Foundation-1"


def download_model_files():
    model_path = hf_hub_download(
        repo_id=MODEL_REPO, filename="Foundation_1.safetensors"
    )
    config_path = hf_hub_download(repo_id=MODEL_REPO, filename="model_config.json")
    return model_path, config_path


def load_model():
    if not torch.cuda.is_available():
        raise RuntimeError("Foundation-1 requires CUDA GPU with ~8GB VRAM")

    device = "cuda"
    model_path, config_path = download_model_files()

    with open(config_path, "r") as f:
        config = json.load(f)

    model = create_model_from_config(config)
    state_dict = load_file(model_path, device=device)
    model.load_state_dict(state_dict)
    model = model.to(device)

    return model, device


model = None
device = None


def initialize_model():
    global model, device
    if model is None:
        print("Loading Foundation-1 model... This may take a moment.")
        model, device = load_model()
        print("Model loaded successfully!")


def generate(prompt, bpm, bars, duration_seconds, seed, cfg_scale, steps):
    global model, device
    if model is None:
        yield None, "Loading model... (first time ~2GB download, please wait)..."
        initialize_model()
        yield None, "Model loaded, starting generation..."

    try:
        seed = int(seed) if seed else -1
        cfg_scale = float(cfg_scale)
        steps = int(steps)

        if seed == -1:
            seed = np.random.default_rng().integers(0, 2**32, dtype=np.uint32)

        text_prompt = f"{prompt}, {bars} Bars, {bpm} BPM"

        conditioning = [
            {
                "prompt": text_prompt,
                "seconds_start": 0,
                "seconds_total": duration_seconds,
                "batch_size": 1,
                "sample_size": int(duration_seconds * model.sample_rate),
            }
        ]

        output = generate_diffusion_cond(
            model,
            steps=steps,
            cfg_scale=cfg_scale,
            conditioning=conditioning,
            seed=seed,
            device=device,
        )

        audio_data = output.cpu().numpy()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, mode="wb") as f:
            wavfile.write(f.name, model.sample_rate, audio_data.T)
            temp_path = f.name

        yield temp_path, f"Generated with seed: {seed}"

    except Exception as e:
        yield None, f"Error: {str(e)}"


css = """
#title {text-align: center; font-size: 2.5em; font-weight: bold; margin-bottom: 0.5em;}
#subtitle {text-align: center; color: #666; margin-bottom: 1em;}
#examples {background: #f5f5f5; padding: 1em; border-radius: 8px;}
.gr-button {min-width: 120px !important;}
"""

examples = [
    [
        "Bass, FM Bass, Medium Delay, Medium Reverb, Low Distortion, Phaser, Acid, Gritty, Wide, Dubstep, Thick, Silky, Warm, Rich, Overdriven, Crisp, Deep, Clean, Triplets",
        150,
        8,
        19.2,
        "",
    ],
    [
        "Synth, Pad, Chord Progression, Rising, Digital, Bass, Fat, Near, Wide, Silky, Warm, Focused",
        110,
        8,
        21.9,
        "",
    ],
    [
        "Kalimba, Mallet, Medium Reverb, Overdriven, Wide, Metallic, Thick, Sparkly, Upper Mids, Bright, Airy, Alternating, Chord Progression, Atmosphere, Spacey, Fast Speed",
        120,
        4,
        8.0,
        "",
    ],
    [
        "High Saw, Spacey, Lead, Warm, Silky, Smooth, 303, Synth Lead, Medium Reverb, Low Distortion, Upper Mids, Mids, Pitch Bend, Arp",
        140,
        8,
        17.1,
        "",
    ],
    [
        "Trumpet, Warm, Complex Arp Melody, High Reverb, Low Distortion, Smooth, Silky, Texture",
        130,
        8,
        18.5,
        "",
    ],
]

with gr.Blocks(css=css, title="Foundation-1 Player") as demo:
    gr.Markdown('<div id="title">Foundation-1 Player</div>')
    gr.Markdown(
        '<div id="subtitle">Text-to-Sample Generation for Music Production</div>'
    )

    with gr.Row():
        with gr.Column(scale=3):
            prompt = gr.Textbox(
                label="Prompt",
                placeholder="Enter instrument, timbre, FX, and notation tags (e.g., Synth, Pad, Warm, Rich, Chord Progression)",
                lines=3,
            )

            with gr.Row():
                bpm = gr.Dropdown(
                    label="BPM",
                    choices=[100, 110, 120, 128, 130, 140, 150],
                    value=128,
                )
                bars = gr.Dropdown(label="Bars", choices=[4, 8], value=8)
                duration = gr.Slider(
                    label="Duration (seconds)",
                    minimum=4,
                    maximum=30,
                    value=17.1,
                    step=0.1,
                )

            with gr.Row():
                seed = gr.Textbox(label="Seed (leave empty for random)", value="")
                cfg_scale = gr.Slider(
                    label="CFG Scale", minimum=1.0, maximum=10.0, value=7.0, step=0.5
                )
                steps = gr.Slider(
                    label="Steps", minimum=10, maximum=150, value=50, step=1
                )

            generate_btn = gr.Button("Generate", variant="primary")

        with gr.Column(scale=1):
            audio_output = gr.Audio(label="Generated Audio")
            status_output = gr.Textbox(label="Status", lines=2)

    gr.Markdown("### Example Prompts")
    gr.Examples(
        examples=examples,
        inputs=[prompt, bpm, bars, duration, seed],
        label="Click to load example",
    )

    gr.Markdown("""
    ### Prompt Structure
    **[Instrument Family/Sub-Family], [Timbre], [Musical Behavior/Notation], [FX], [Key], [Bars], [BPM]**

    - **Instrument**: Synth, Bass, Keys, Brass, Guitar, etc.
    - **Timbre**: Warm, Bright, Wide, Gritty, Clean, etc.
    - **Notation**: Melody, Arp, Chord Progression, Rising, etc.
    - **FX**: Reverb, Delay, Distortion, Phaser, etc.
    - **Bars**: 4, 8
    - **BPM**: 100-150
    """)

    generate_btn.click(
        fn=generate,
        inputs=[prompt, bpm, bars, duration, seed, cfg_scale, steps],
        outputs=[audio_output, status_output],
    )

    gr.Markdown("---")
    gr.Markdown(
        "**First time:** Click **Generate** to download the model (~2GB) and start. Subsequent runs will skip the download."
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
