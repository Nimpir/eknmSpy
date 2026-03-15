# Architecture

## Component overview

```
┌─────────────────────────────────────────────────────────────────┐
│  main.py  — entry point                                         │
│  Loads .env → configures logging → launches gui.py             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │         gui.py             │
              │   tkinter App (main thread)│
              │  5 tabs: Sessions / Process│
              │  Log / Search / Bot        │
              └──┬──────────────────┬──────┘
                 │                  │
    ┌────────────▼───┐   ┌──────────▼────────────┐
    │    bot.py      │   │     processor.py       │
    │  Discord bot   │   │  diarization + STT     │
    │  (daemon thread│   │  (daemon thread,       │
    │   asyncio loop)│   │   started from GUI)    │
    └──┬─────────────┘   └──────────┬─────────────┘
       │                            │
  ┌────▼────────────┐          ┌────▼────────────┐
  │ dave_handler.py │          │     db.py        │
  │  DAVE MLS key  │          │  SQLite + FTS5   │
  │  exchange &     │          │  sessions +      │
  │  decryption    │          │  segments tables │
  └─────────────────┘          └─────────────────┘
```

## Threading model

The application runs three concurrent execution contexts:

| Context | Type | Responsibilities |
|---|---|---|
| Main thread | tkinter mainloop | All UI rendering and event handling |
| Bot thread | `threading.Thread` (daemon) + `asyncio` event loop | Discord gateway, voice receive, slash commands |
| Processing thread | `threading.Thread` (daemon) | pyannote + Whisper + ollama (CPU/GPU bound) |

**Thread safety rules:**
- All tkinter widget writes must go through `self.after(0, fn)` — never called directly from bot or processing threads
- The bot thread communicates with the GUI via `on_session_started` / `on_session_stopped` callbacks, both wrapped in `self.after()`
- The processing thread updates the GUI via the `progress_cb` closure which also uses `self.after()`

## Data flow

```
Discord UDP audio frames
        │
        ▼
VoiceWebSocket.poll_event()         ← patched in bot.py
        │  binary WebSocket frames
        ▼
DaveHandler.handle_binary()         ← dave_handler.py
        │  MLS key exchange (OP 25-30)
        ▼
DecodeManager.run()                 ← patched in bot.py
        │  DAVE decrypt → Opus decode → PCM
        ▼
MultiUserSink.write()               ← bot.py
        │  48kHz stereo int16 PCM chunks
        ▼
RingBuffer (per user, 10 min)
        │
   /leave or auto-leave
        │
        ▼
save_all_users()  →  Username_ID.wav  (48kHz stereo)
mix_to_mono_wav() →  mixed_mono.wav   (16kHz mono, Whisper input)
        │
   /transcript or GUI "Start Processing"
        │
        ▼
process_session()                   ← processor.py
  ├─ scipy.io.wavfile.read()        load audio once, reuse for both steps
  ├─ pyannote Pipeline()            speaker diarization
  ├─ _map_speakers_to_discord()     energy-based SPEAKER_XX → Discord name
  ├─ _smooth_language_phases()      per-segment language → stable phases
  ├─ whisper_model.transcribe()     STT per segment with forced language
  ├─ fix_segments_with_llm()        ollama post-processing
  └─ insert_segments()              write to SQLite
        │
  export_txt() + export_html()
        │
  (if via Discord) post files to channel
```

## File map

| File | Lines | Responsibility |
|---|---|---|
| `main.py` | ~85 | Entry point: `.env` loader, logging config, stderr redirect |
| `bot.py` | ~720 | Discord bot: voice capture, ring buffer, DAVE patches, slash commands |
| `dave_handler.py` | ~300 | DAVE MLS state machine: OP 25–30 handshake, per-frame decryption |
| `processor.py` | ~540 | Full transcription pipeline: diarization → STT → LLM → DB → export |
| `gui.py` | ~475 | 5-tab tkinter GUI, all bot↔GUI callbacks |
| `db.py` | ~130 | SQLite schema, CRUD helpers, FTS5 search |

## Database schema

```sql
sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,        -- e.g. "general_2026-03-15_14-37"
    started_at  TEXT NOT NULL,        -- ISO 8601
    ended_at    TEXT,
    guild       TEXT,                 -- Discord server name
    channel     TEXT                  -- voice channel name
)

segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id),
    speaker     TEXT NOT NULL,        -- "Username(123456789)"
    start_sec   REAL NOT NULL,
    end_sec     REAL NOT NULL,
    text        TEXT NOT NULL
)

segments_fts   -- FTS5 virtual table over segments(text, speaker)
               -- auto-populated by AFTER INSERT trigger
```

## Audio constants

| Constant | Value | Reason |
|---|---|---|
| `SAMPLE_RATE` | 48 000 Hz | Discord native |
| `CHANNELS` | 2 (stereo) | Discord native |
| `SAMPLE_WIDTH` | 2 bytes (int16) | Discord native |
| `RING_BUFFER_SEC` | 600 s (10 min) | Rolling capture window |
| Whisper input | 16 000 Hz mono | Whisper requirement |
