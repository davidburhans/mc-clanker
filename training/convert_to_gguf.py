#!/usr/bin/env python3
"""
Convert a HuggingFace-format model to GGUF for use with LM Studio / Ollama.

Usage:
    python convert_to_gguf.py --input ./qwen3.5-0.8b-mc-clanker-dpo/final --output ./Qwen3.5-0.8B-McClanker-DPO-Q4_K_M.gguf

Requirements:
    pip install gguf>=0.10 transformers torch

To clone llama.cpp if not present:
    git clone https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp
"""

import argparse
import os
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Convert HF model to GGUF")
    parser.add_argument("--input", "-i", required=True, help="Path to HF model directory")
    parser.add_argument("--output", "-o", required=True, help="Output GGUF file path")
    parser.add_argument(
        "--quantization",
        "-q",
        default="Q4_K_M",
        choices=["F16", "Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q4_K_M", "Q4_K_S", "Q4_0", "Q3_K_M", "Q2_K"],
        help="Quantization type (default: Q4_K_M)",
    )
    parser.add_argument("--llama-cpp", default="/tmp/llama.cpp", help="Path to llama.cpp checkout")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    output_path = os.path.abspath(args.output)
    llama_cpp_path = os.path.abspath(args.llama_cpp)

    if not os.path.isdir(input_path):
        print(f"Error: Input path '{input_path}' is not a directory")
        sys.exit(1)

    # Find convert_hf_to_gguf.py
    script_candidates = [
        os.path.join(llama_cpp_path, "convert_hf_to_gguf.py"),
        os.path.join(llama_cpp_path, "examples", "convert_hf_to_gguf.py"),
    ]
    convert_script = None
    for candidate in script_candidates:
        if os.path.isfile(candidate):
            convert_script = candidate
            break

    if convert_script is None:
        print("llama.cpp conversion script not found.")
        print(f"Clone it with: git clone https://github.com/ggerganov/llama.cpp.git {llama_cpp_path}")
        print(f"Then run this script again with --llama.cpp {llama_cpp_path}")
        sys.exit(1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    print(f"Converting {input_path} -> {output_path}")
    print(f"Using script: {convert_script}")
    print(f"Quantization: {args.quantization}")

    # Step 1: Convert HF -> unquantized GGUF
    fp16_path = output_path.replace(".gguf", "-fp16.gguf")
    if os.path.exists(fp16_path):
        print(f"Found existing FP16 GGUF at {fp16_path}, skipping conversion")
    else:
        print("\n[1/2] Converting HuggingFace -> FP16 GGUF...")
        result = subprocess.run(
            [sys.executable, convert_script, input_path, "--outfile", fp16_path, "--outtype", "f16"],
            capture_output=False,
        )
        if result.returncode != 0:
            print("Error during conversion!")
            sys.exit(1)

    # Step 2: Quantize to target format
    if args.quantization == "F16":
        print(f"\n[2/2] Moving FP16 to output (no quantization)...")
        shutil.copy(fp16_path, output_path)
    else:
        quantize_bin = os.path.join(llama_cpp_path, "bin", "quantize")
        if not os.path.isfile(quantize_bin):
            # Try llama-cli or other bin names
            for name in ["llama-quantize", "quantize"]:
                alt = os.path.join(llama_cpp_path, "bin", name)
                if os.path.isfile(alt):
                    quantize_bin = alt
                    break

        if not os.path.isfile(quantize_bin):
            print("\n[2/2] quantize binary not found — you need to BUILD llama.cpp first:")
            print(f"  cd {llama_cpp_path}")
            print("  mkdir build && cd build")
            print("  cmake .. -DLLAMA_BUILD_TOOLS=ON -DCMAKE_BUILD_TYPE=Release")
            print("  cmake --build . --config Release")
            print(f"\nMeanwhile, your FP16 GGUF is ready at: {fp16_path}")
            print("Copy it to LM Studio as-is (LM Studio handles quantization on load).")
            shutil.copy(fp16_path, output_path)
        else:
            print(f"\n[2/2] Quantizing to {args.quantization}...")
            result = subprocess.run([quantize_bin, fp16_path, output_path, args.quantization], capture_output=False)
            if result.returncode != 0:
                print("Quantization failed!")
                sys.exit(1)

    size_gb = os.path.getsize(output_path) / (1024**3)
    print(f"\nDone! Output: {output_path} ({size_gb:.2f} GB)")


if __name__ == "__main__":
    main()
