# Discord Transcriber — Project Guide for Claude Code

## What this project does
Captures voice from a Discord channel, transcribes it with speaker labels, and stores everything in a local SQLite database. A tkinter GUI lets the user process recordings and search transcripts.

## Stack
- **discord.py 2.7.x** (voice-receive fork) — bot that joins voice channels and records audio
- **pyannote/speaker-diarization-3.1** — identifies who is speaking and when
- **openai-whisper large-v3** — speech-to-text (supports mixed Russian/English)
- **SQLite + FTS5** — stores sessions and segments, full-text search
- **tkinter** — desktop GUI (dark theme)
- **pydub / numpy** — audio mixing and ring buffer

## File map
| File | Responsibility |
|---|---|
| `main.py` | Entry point. Loads `.env`, then launches `gui.py` |
| `bot.py` | Discord bot. Records voice, maintains ring buffer, exposes `/join` `/leave` `/save N` slash commands |
| `processor.py` | Post-session pipeline: pyannote diarization → Whisper STT → merge segments → export `.txt` and `.html` |
| `db.py` | All SQLite logic: sessions table, segments table, FTS5 virtual table, CRUD helpers |
| `gui.py` | 5-tab tkinter app: Sessions / Process / Log / Search / Bot |

## Key data flow
```
Discord voice → MultiUserSink (bot.py)
  → per-user WAV files  saved to data/recordings/<session>/
  → mixed_mono.wav      (mono 16kHz, input for Whisper)
  → process_session()   (processor.py)
      → pyannote diarization  (who spoke when)
      → whisper.transcribe()  per segment
      → insert_segments()     (db.py)
      → export_txt / export_html
```

## Environment variables (`.env`)
```
DISCORD_TOKEN=   # Discord bot token
HF_TOKEN=        # HuggingFace token (needed for pyannote model download)
```

## Database schema
```sql
sessions  (id, name, started_at, ended_at, guild, channel)
segments  (id, session_id, speaker, start_sec, end_sec, text)
segments_fts  -- FTS5 virtual table over segments(text, speaker)
```

## Audio constants (bot.py)
```python
SAMPLE_RATE     = 48000   # Discord native
CHANNELS        = 2       # stereo
SAMPLE_WIDTH    = 2       # 16-bit PCM
RING_BUFFER_SEC = 600     # 10 min rolling buffer per user
```

## GUI callbacks between bot.py and gui.py
`bot.py` exposes two module-level callables that `gui.py` sets at runtime:
```python
bot.on_session_started(session_id, session_name)
bot.on_session_stopped(session_id, mixed_wav_path, speaker_map)
```
These update the GUI when a Discord session starts/ends.

## Common tasks

### Add a new slash command
In `bot.py`, add a new `@bot.tree.command(...)` decorated async function. The bot syncs commands on startup via `await bot.tree.sync()`.

### Change Whisper model
In `processor.py`, change `WHISPER_MODEL = "large-v3"` to `"medium"` or `"small"` for faster/lighter processing.

### Add a new GUI tab
In `gui.py`, add a `ttk.Frame` to the notebook in `_build_ui()`, then add a `_build_<name>_tab()` method following the existing pattern.

### Export to a new format
Add an `export_<format>(segments, path)` function in `processor.py` and call it from `gui.py` after processing.

### Run processing from CLI (no GUI)
```python
from processor import process_session, export_txt, export_html
from db import init_db
init_db()
segments = process_session("data/recordings/session/mixed_mono.wav", session_id=1)
export_txt(segments, "out.txt")
export_html(segments, "out.html", session_name="My Session")
```

## Known issues / limitations
- `discord.sinks.WaveSink` (discord.py 2.7.x) — do NOT use `discord.AudioSink` (old API, will raise AttributeError)
- Python 3.13 is NOT supported by PyTorch — use Python 3.11
- PyTorch must be installed before other packages: `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121`
- First run downloads ~4 GB of models (Whisper large-v3 + pyannote)
- pyannote requires accepting model terms at huggingface.co/pyannote/speaker-diarization-3.1

## Output files per session
```
data/recordings/<session_name>/
├── user_<id>.wav       # per-user raw audio (48kHz stereo)
├── mixed_mono.wav      # merged mono 16kHz (Whisper input)
├── transcript.txt      # plain text log
└── transcript.html     # interactive log with speaker colours and search
```
