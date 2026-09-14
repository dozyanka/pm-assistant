# Third-party software and models

PM Assistant does **not** bundle model weights or Ollama binaries in this repository.
Users install/download them separately.

## Ollama
- Project: https://github.com/ollama/ollama
- License: MIT
- Purpose: local model runtime / HTTP API.

## Qwen3.5-9B
- Project: https://huggingface.co/Qwen/Qwen3.5-9B
- License shown by the model publisher: Apache-2.0
- Purpose: local language-model inference for analysis and structured generation.

## Qwen3-Embedding-0.6B
- Project: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- License shown by the model publisher: Apache-2.0
- Purpose: local semantic embeddings / retrieval.

## Tailscale (optional remote mode)
- Project: https://tailscale.com/
- Purpose: private device-to-device connectivity for Remote mode.
- Tailscale is not bundled with PM Assistant. Its software/service terms apply separately.

## PyInstaller / pywebview
Used only when building the optional Windows desktop packages. Their own licenses apply.
See `packaging/windows/requirements-*.txt` for build dependencies.
