# Setup Guide

## System requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 / Ubuntu 20.04 | Windows 11 |
| Python | 3.11 | 3.11 |
| GPU VRAM | 6 GB | 12 GB (RTX 4070 Ti) |
| RAM | 16 GB | 32 GB |
| Disk | 10 GB free | 20 GB free |
| CUDA | 12.x | 12.6 |

> **Python 3.11 only.** PyTorch does not support 3.12+ as of this writing. Use `py -3.11` on Windows if you have multiple versions installed.

---

## Step 1 — FFmpeg

FFmpeg is required by pydub for audio mixing.

**Windows:**
1. Download a build from https://ffmpeg.org/download.html (e.g. the gyan.dev full build)
2. Extract and add the `bin/` folder to your system `PATH`
3. Verify: `ffmpeg -version`

**Ubuntu:**
```bash
sudo apt install ffmpeg
```

---

## Step 2 — CUDA Toolkit

Install CUDA 12.x from https://developer.nvidia.com/cuda-downloads.

Verify:
```bash
nvcc --version
nvidia-smi
```

---

## Step 3 — Clone and create virtualenv

```bash
git clone <repo-url>
cd discord_transcriber

python -m venv venv

# Windows
venv\Scripts\activate

# Linux
source venv/bin/activate
```

---

## Step 4 — Install PyTorch (CUDA build)

This **must** be done before installing other packages:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
```

Verify CUDA is available:
```python
python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
# Expected: True  12.6
```

If it prints `False`, you have a CPU-only build. Re-install with the `--force-reinstall` flag:
```bash
pip install torch torchaudio --force-reinstall --index-url https://download.pytorch.org/whl/cu126
```

---

## Step 5 — Install remaining dependencies

```bash
pip install -r requirements.txt
```

`davey` (a C extension for DAVE MLS decryption) is included in `requirements.txt`. If a pre-built wheel is available for your platform it installs automatically. If not, build from source:

```bash
pip install davey
# or, if installing from the davey source repo:
pip install .
```

Verify the install:
```bash
python -c "from davey import DaveSession; print('davey ok')"
```

Without `davey` the bot will connect but record silence — no error is printed, the audio file simply contains no data.

---

## Step 6 — ollama (optional, for LLM post-processing)

ollama runs a local LLM to fix transcription errors. If not installed, the step is silently skipped.

1. Download from https://ollama.com/download
2. Pull the default model:
   ```bash
   ollama pull qwen2.5:7b
   ```
3. ollama must be running in the background when the app is used (`ollama serve`)

To use a different model, set `LLM_MODEL` in `.env`:
```env
LLM_MODEL=llama3.2:3b    # lighter, faster
LLM_MODEL=qwen2.5:14b    # heavier, better quality
```

---

## Step 7 — Discord bot token

1. Go to https://discord.com/developers/applications
2. **New Application** → give it a name
3. **Bot** tab → **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable:
   - `Server Members Intent`
   - `Voice States` (auto-enabled)
5. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Attach Files`
6. Open the generated URL in a browser to invite the bot to your server

---

## Step 8 — Hugging Face token

pyannote models are gated — you must accept their terms before downloading.

1. Register at https://huggingface.co
2. **Settings → Access Tokens → New token** (role: read)
3. Accept the model terms at both URLs while logged in:
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0

---

## Step 9 — .env file

```bash
cp .env.example .env
```

Edit `.env`:
```env
DISCORD_TOKEN=your_discord_bot_token
HF_TOKEN=your_huggingface_token
LLM_MODEL=qwen2.5:7b        # optional
```

---

## Step 10 — First run

```bash
venv\Scripts\activate
python main.py
```

On first run, the app downloads ~4 GB of models in the background when processing is triggered:
- Whisper large-v3 → `~/.cache/whisper/` (~3 GB)
- pyannote/speaker-diarization-3.1 → `~/.cache/huggingface/` (~1 GB)

This only happens once. Subsequent runs load from cache.

---

## Troubleshooting

### `torch.cuda.is_available()` returns False
Re-install PyTorch with the correct CUDA index URL and `--force-reinstall`.

### `GatedRepoError` from pyannote
You haven't accepted the model license on Hugging Face. Visit both model pages while logged in and click **Agree**.

### `Could not load libtorchcodec`
Harmless warning — suppressed automatically. The app uses scipy instead of torchcodec for audio loading.

### Bot connects but no audio is recorded
DAVE MLS handshake may have failed. Check `data/logs/` for `dave_handler` DEBUG entries. The handshake requires OP 25 → 26 → 27 → 28 → 29 to complete. If it stalls, reconnect.

### `OpusError` in logs
DAVE decryption is returning garbage — usually a timing issue during the MLS transition. The frame is dropped and recording continues. If this is frequent, the DAVE session may need to reinitialise (re-join the channel).

### `ValueError: Audio file is empty (0 samples)`
The ring buffer captured no audio. Causes:
- DAVE decryption failed throughout the session
- The bot was muted/deafened
- The session was too short (< 1 second)
