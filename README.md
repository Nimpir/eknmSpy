# Discord Transcriber

Records voice from a Discord channel, transcribes it with speaker labels, and stores everything in a local SQLite database. A tkinter GUI lets you process recordings and search transcripts. Transcript files can be retrieved directly from Discord via slash commands.

**Stack:** py-cord 2.7.x · pyannote 3.1 · Whisper large-v3 · ollama (local LLM) · SQLite FTS5 · tkinter

---

## Requirements

- Windows 10/11 (or Linux)
- Python **3.11** (3.12+ not supported by PyTorch)
- NVIDIA GPU with 6+ GB VRAM (tested on RTX 4070 Ti — 12 GB)
- CUDA Toolkit 12.x
- FFmpeg in PATH
- [ollama](https://ollama.com/) running locally (optional — used for transcript post-processing)

---

## Installation

### 1. FFmpeg

Download from https://ffmpeg.org/download.html and add to PATH.

```bash
ffmpeg -version   # verify
```

### 2. Python dependencies

```bash
git clone <repo>
cd discord_transcriber

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux

# PyTorch with CUDA — must be installed BEFORE other packages
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126

# Remaining dependencies
pip install -r requirements.txt
```

### 3. Tokens

**Discord Bot Token:**
1. Go to https://discord.com/developers/applications
2. Create application → Bot tab → Reset Token → copy
3. Privileged Gateway Intents → enable `Server Members Intent` and `Voice States`
4. Invite via OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Attach Files`

**Hugging Face Token** (required for pyannote models):
1. Register at https://huggingface.co
2. Settings → Access Tokens → New token (read)
3. Accept model terms at:
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0

### 4. .env

```bash
cp .env.example .env
```

```env
DISCORD_TOKEN=your_bot_token_here
HF_TOKEN=your_huggingface_token_here
LLM_MODEL=qwen2.5:7b          # optional — any model available in ollama
```

---

## Running

```bash
venv\Scripts\activate
python main.py
```

The GUI opens. If `DISCORD_TOKEN` is set in `.env`, the bot starts automatically.

---

## Discord slash commands

| Command | Description |
|---|---|
| `/join` | Join your current voice channel and start recording |
| `/leave` | Stop recording, save audio files, return the session ID |
| `/sessions` | List all sessions recorded on this server (ID, date, channel, transcript status) |
| `/transcript` | Get the transcript for the latest session |
| `/transcript <id>` | Get the transcript for a specific session ID |
| `/save <N>` | Send the last N minutes of live audio (1–10 min) |

**Transcript command behaviour:**
- Only returns sessions belonging to the current server
- If transcript files already exist they are posted immediately
- If not, the transcription pipeline runs first (diarization + Whisper + LLM fix), then posts
- Wrong ID or session from another server returns an error

---

## GUI

| Tab | Purpose |
|---|---|
| 📁 Sessions | List all recorded sessions; double-click to open in Process tab |
| ⚙️ Processing | Run diarization + transcription manually on any WAV file |
| 📄 Log | View transcript in the app; open HTML version in browser |
| 🔍 Search | Full-text search across all sessions (SQLite FTS5) |
| 🤖 Bot | Bot status and log; token field for manual start |

---

## Data flow

```
Discord voice → MultiUserSink (ring buffer, 10 min per user)
  └─ /leave ──→ per-user WAV files  (data/recordings/<session>/)
               mixed_mono.wav       (mono 16kHz, Whisper input)

/transcript or GUI "Start Processing":
  mixed_mono.wav
    → pyannote diarization     (who speaks when)
    → energy-based mapping     (SPEAKER_00 → Discord name)
    → language phase detection (RU/EN smoothing across segments)
    → Whisper large-v3 STT     (per segment, forced language)
    → ollama LLM fix           (correct transcription errors)
    → SQLite insert
    → transcript.txt + transcript.html
```

---

## Project structure

```
discord_transcriber/
├── main.py           entry point — loads .env, starts GUI
├── gui.py            5-tab tkinter UI
├── bot.py            Discord bot — recording, ring buffer, slash commands
├── dave_handler.py   DAVE MLS key exchange and audio decryption
├── processor.py      pyannote + Whisper + LLM pipeline
├── db.py             SQLite helpers (sessions, segments, FTS5)
├── .env.example
├── requirements.txt
└── data/
    ├── transcripts.db
    ├── logs/
    └── recordings/
        └── <session_name>/
            ├── Username_123456789.wav   per-user raw audio (48kHz stereo)
            ├── mixed_mono.wav           merged mono 16kHz
            ├── session.log              per-session debug log
            ├── transcript.txt
            └── transcript.html          interactive, colour-coded by speaker
```

---

## Audio constants

| Constant | Value |
|---|---|
| Recording sample rate | 48 000 Hz (Discord native) |
| Channels | 2 (stereo) |
| Bit depth | 16-bit PCM |
| Ring buffer | 10 minutes per user |
| Whisper input | 16 000 Hz mono |

---

## First run

The first run downloads ~4 GB of models:
- Whisper large-v3 (~3 GB, cached in `~/.cache/whisper`)
- pyannote/speaker-diarization-3.1 + segmentation-3.0 (~1 GB, cached in `~/.cache/huggingface`)

After the first run, no internet is required (except for the Discord connection).

---

## Known limitations

- Voice receive requires a **self-hosted bot** — third-party OAuth bots cannot receive audio
- Python 3.13 is not supported; use **Python 3.11**
- PyTorch must be installed before other packages (CUDA build)
- Transcription quality degrades with heavy echo or poor microphones
- ollama must be running locally for LLM post-processing; if unavailable the step is skipped silently
