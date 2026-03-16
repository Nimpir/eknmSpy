# Discord Slash Commands

All commands are registered as Discord Application Commands (slash commands) and sync automatically when the bot starts.

---

## /join

**Join a voice channel and start recording.**

The bot joins the voice channel you are currently in. A new session is created in the database and audio capture begins immediately.

**Requirements:** You must be in a voice channel.

**Response:**
```
🎙️ Started recording in #general. Session: general_2026-03-15_14-37
```

**What happens internally:**
- A DAVE MLS key exchange is initiated (OP 25–30 handshake)
- A `MultiUserSink` ring buffer is created (10 minutes per user)
- A per-session log file is opened at `data/recordings/<session>/session.log`
- The GUI updates to show the active session

**Notes:**
- If the bot is already recording, the command is rejected
- The bot undeafens itself after connecting so it can receive audio
- If the voice connection times out (>5 s), the bot disconnects and reports an error

---

## /leave

**Stop recording, save audio files, and return the session ID.**

**Response:**
```
⏹️ Recording stopped. Session ID: 7
Use /transcript to generate and retrieve the transcript.
```

**What happens internally:**
1. Recording stops and per-user ring buffer is flushed to WAV files
2. All per-user WAV files are mixed into `mixed_mono.wav` (16kHz mono)
3. The session is closed in the database (`ended_at` is set)
4. The DAVE session is detached and cleaned up
5. If no humans remain in the channel and auto-leave fires, the notify channel is `None` (silent leave)

**Auto-leave:** If all humans leave the voice channel, the bot waits 5 seconds and then calls `/leave` automatically. If someone rejoins within 5 seconds, the auto-leave is cancelled.

---

## /sessions

**List all recorded sessions for this server.**

Shows every session associated with the current Discord server, in reverse-chronological order.

**Response:**
```
Sessions for My Server (3 total)
📝 = transcript ready  🎙️ = not yet transcribed

📝 ID 7 — 2026-03-15 14:37 → 2026-03-15 16:22  |  #general  |  general_2026-03-15_14-37
🎙️ ID 6 — 2026-03-14 20:11 → 2026-03-14 21:45  |  #gaming   |  gaming_2026-03-14_20-11
📝 ID 4 — 2026-03-12 18:00 → 2026-03-12 19:30  |  #general  |  general_2026-03-12_18-00
```

**Icons:**
- `📝` — `transcript.txt` and `transcript.html` exist; use `/transcript <id>` to retrieve immediately
- `🎙️` — audio recorded but not yet transcribed; `/transcript <id>` will run the pipeline first

**Notes:**
- Only shows sessions for the **current server** — sessions from other servers are not visible
- If the list exceeds Discord's 2000-character limit, it is automatically split into multiple messages

---

## /transcript `[id]`

**Get the transcript for a session.**

| Usage | Behaviour |
|---|---|
| `/transcript` | Returns the transcript for the most recent session on this server |
| `/transcript 7` | Returns the transcript for session ID 7 |

**Response (files already exist):**
```
📝 Transcript: `general_2026-03-15_14-37` (Session ID: 7)
[transcript.txt attached]
[transcript.html attached]
```

**Response (transcription runs first):**
The response is deferred (Discord shows "thinking…") while the pipeline runs, then:
```
📝 Transcript ready: `general_2026-03-15_14-37` (Session ID: 7)
[transcript.txt attached]
[transcript.html attached]
```

**Error cases:**

| Situation | Error message |
|---|---|
| Session ID not found | `❌ Session 99 not found.` |
| Session belongs to another server | `❌ Session 7 does not belong to this server.` |
| No sessions on this server | `❌ No sessions found for this server.` |
| Audio file missing | `❌ No audio found for session 7. The recording may not have been saved yet.` |
| Pipeline crash | `❌ Transcription failed: <error detail>` |

**Notes:**
- Transcription can take several minutes for long sessions (Whisper + diarization are GPU-bound)
- The bot responds with a deferred message so Discord does not time out
- Both `.txt` and `.html` files are attached. The HTML version includes speaker colour coding and an in-page search bar

---

## /save `<minutes>`

**Send the last N minutes of live audio as a WAV file.**

Only works while recording is active. Clips from the ring buffer — all users' audio is mixed into one stereo file.

**Option:**
- `minutes` — integer, 1–10 (default: 5)

**Response:**
```
🎵 Last 5 min of audio:
[clip_14-37-00_last5min.wav attached]
```

**Notes:**
- The ring buffer holds up to 10 minutes per user
- The clip is a stereo 48kHz mix of all users active during that window
- The file is generated in memory and not saved to disk
- Peak memory during clip generation is approximately 12 MB per user-minute (48 kHz stereo int16). A 10-minute clip with 4 active users uses ~115 MB.
