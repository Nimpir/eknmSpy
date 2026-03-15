# GUI Reference

The GUI is a single `tkinter.Tk` window (`App` class in `gui.py`) with five tabs. It starts automatically when running `python main.py`.

If `DISCORD_TOKEN` is set in `.env`, the bot starts automatically 500ms after the window opens.

---

## 📁 Sessions tab

Lists all recorded sessions from the database.

| Column | Description |
|---|---|
| ID | Database session ID — used with `/transcript <id>` |
| Name | Auto-generated: `<channel>_<date>_<time>` |
| Started | Recording start time |
| Ended | Recording end time (`—` if still active) |
| Channel | Discord voice channel name |

**Actions:**
- **🔄 Refresh** — reload sessions from the database
- **📄 Open Log** — load the selected session into the Log tab
- **⚙️ Process** — send the selected session to the Process tab and pre-fill the audio path
- **Double-click** — same as ⚙️ Process

---

## ⚙️ Processing tab

Runs the full transcription pipeline on a WAV file.

**Fields:**
- **Audio file** — path to `mixed_mono.wav`; auto-filled when a session is selected in the Sessions tab, or use 📂 Browse to pick manually
- **Session ID** — database ID to store results under; auto-filled from session selection

**Progress bar** — shows current pipeline step and percentage

**Log area** — live output from the pipeline:
```
[  0%] Loading models...
[ 20%] Diarization (who speaks when)...
[ 40%] Found 2 speakers, 143 segments
[ 47%] Detecting language phases...
[ 50%] Transcribing...
[ 60%] Transcribing segment 41/143...
...
[ 92%] Fixing text with LLM...
[ 97%] Saving to database...
[ 99%] Unloading models...
[100%] Done!
✅ Done! Exported:
  data\recordings\general_2026-03-15_14-37\transcript.txt
  data\recordings\general_2026-03-15_14-37\transcript.html
```

After processing completes, the Log tab is automatically loaded with the new transcript.

---

## 📄 Log tab

Displays a colour-coded transcript for a session.

**Controls:**
- **Session ID** field + **📄 Load** button — load any session by ID
- **🌐 Open HTML in browser** — opens `transcript.html` in the default browser

**Display:** Each segment shows `[HH:MM:SS]  Speaker: text`. Each unique speaker gets a distinct colour (up to 8 colours cycle).

The Log tab is auto-loaded after processing completes.

---

## 🔍 Search tab

Full-text search across all sessions using SQLite FTS5.

**Usage:**
1. Type a query in the search box
2. Press **Enter** or click **🔍 Find**

**Results table:**

| Column | Description |
|---|---|
| Session | Session name |
| Time | Timestamp of the matching segment |
| Speaker | Speaker label |
| Text | Matching transcript text |

The result count is shown below the table. Search is case-insensitive and supports FTS5 query syntax (e.g. `"exact phrase"`, `word1 OR word2`).

---

## 🤖 Bot tab

Controls the Discord bot and shows live bot events.

**Discord Token field** — pre-filled from `DISCORD_TOKEN` in `.env`. Shown as `••••••` for security. The bot auto-starts on launch if the token is set.

**Status indicator:**
- `● Stopped` (red) — bot is not running
- `● Starting...` (yellow) — bot thread started, connecting to Discord
- `● Running` (green) — bot is connected and ready
- `● Error` (red) — bot crashed; the **▶ Start Bot** button re-enables for retry

**▶ Start Bot** — manually start the bot (only needed if auto-start failed or after a crash).

**Commands reference** — quick reminder of available slash commands.

**Bot log** — live event stream:
```
Bot is starting...
🎙️ Session started: general_2026-03-15_14-37 (ID=7)
⏹️ Session 7 finished. WAV: data\recordings\general_2026-03-15_14-37\mixed_mono.wav
```

---

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Enter` (Search tab) | Run search |
| `Double-click` (Sessions tab) | Send session to Process tab |

---

## GUI ↔ Bot callbacks

The bot thread communicates with the GUI via two module-level callables that `gui.py` sets at startup:

```python
# Set in gui.py _start_bot()
discord_bot.on_session_started = session_started   # called when /join completes
discord_bot.on_session_stopped = session_stopped   # called when /leave or auto-leave fires
```

Both callbacks are wrapped in `self.after(0, fn)` so tkinter widget updates happen on the main thread.

`on_session_stopped` pre-fills the Process tab's audio path and session ID so the user can start processing with one click.
