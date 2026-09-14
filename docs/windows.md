# Windows Desktop build

## Build
From the repository root on Windows:

```bat
scripts\windows\build_desktop.cmd
```

The script creates an isolated `.build-venv`, runs the full regression suite and builds the application with PyInstaller.
Output is written under `release\PM-Assistant-Windows-v1.0.0`.

## Runtime AI
Install Ollama separately and download the required models:

```bat
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b
scripts\windows\start_ollama.cmd
```

Model weights and Ollama binaries are intentionally not stored in Git.
