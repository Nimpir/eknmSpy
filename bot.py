"""
bot.py — Discord bot: voice capture, ring buffer, per-user audio mixing
"""

import os
import asyncio
import threading
import collections
import wave
import struct
import time
import numpy as np
from datetime import datetime
from pathlib import Path

import logging
import logging.handlers
import re

from unidecode import unidecode
import discord
import discord.sinks
import discord.gateway
from discord.ext import commands
from pydub import AudioSegment

from db import create_session, close_session, get_session, get_last_session_for_guild, get_conn

# ── Compatibility patch ────────────────────────────────────────────────────────
# Discord voice gateway (v8) requires DAVE E2E encryption fields in IDENTIFY.
# py-cord 2.7.1 omits them → server closes immediately with code 4017.
# max_dave_protocol_version=1 declares DAVE v1 support (required by Discord);
# video/streams are also expected by the current voice gateway spec.
async def _patched_identify(self):
    state = self._connection
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "video": False,
            "streams": [],
            "max_dave_protocol_version": 1,
        },
    }
    await self.send_as_json(payload)

discord.gateway.DiscordVoiceWebSocket.identify = _patched_identify

# ── DAVE MLS integration ───────────────────────────────────────────────────────
# py-cord 2.7.1 declares DAVE v1 support but never implements the MLS key
# exchange.  We patch:
#   1. DiscordVoiceWebSocket.received_message — handle DAVE opcodes 25-30
#   2. DecodeManager.run — DAVE-decrypt each audio frame before Opus decoding
# The actual MLS state machine lives in dave_handler.DaveHandler.
import discord.opus as _opus
import discord.voice_client as _vc
from dave_handler import DaveHandler

_dave_log = logging.getLogger("dave_handler")


# ── 1. Voice WebSocket — forward DAVE opcodes to the handler ─────────────────
# Guard against double-patching on importlib.reload(): recover the real original
# by following the _orig chain if the method was already patched by us.

def _unwrap(fn):
    """Follow _orig chain until we reach an unpatched function."""
    while hasattr(fn, "_orig"):
        fn = fn._orig
    return fn

_orig_received_message = _unwrap(
    discord.gateway.DiscordVoiceWebSocket.received_message
)

# ── 0. SELECT_PROTOCOL — announce DAVE v1 support ────────────────────────────
# Discord only sends DAVE binary frames if SELECT_PROTOCOL includes
# dave_protocol_version: 1.  py-cord 2.7.1 omits this field.
_orig_select_protocol = _unwrap(discord.gateway.DiscordVoiceWebSocket.select_protocol)

async def _patched_select_protocol(self, ip, port, mode):
    payload = {
        "op": self.SELECT_PROTOCOL,
        "d": {
            "protocol": "udp",
            "data": {"address": ip, "port": port, "mode": mode},
            "dave_protocol_version": 1,
        },
    }
    _dave_log.debug("SELECT_PROTOCOL dave_protocol_version=1 ip=%s port=%s mode=%s", ip, port, mode)
    await self.send_as_json(payload)

_patched_select_protocol._orig = _orig_select_protocol
discord.gateway.DiscordVoiceWebSocket.select_protocol = _patched_select_protocol


# ── 1a. received_message — auto-attach handler + JSON DAVE opcodes ───────────
async def _patched_received_message(self, msg):
    global _pending_dave_handler
    op = msg.get("op")
    _dave_log.debug("ws msg op=%s", op)
    if op == 4:  # SESSION_DESCRIPTION — shows DAVE negotiation result
        _dave_log.debug("SESSION_DESCRIPTION d=%s", msg.get("d"))
    await _orig_received_message(self, msg)
    handler: DaveHandler | None = getattr(self, "_dave", None)
    # Auto-attach the pending handler the first time any TEXT message arrives
    if handler is None and _pending_dave_handler is not None:
        _pending_dave_handler.attach(self)
        handler = _pending_dave_handler
        _pending_dave_handler = None
    # Log content of high opcodes we don't fully know yet
    if op in (18, 20) or (op is not None and op >= 17 and op not in (18, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30)):
        _dave_log.debug("unrecognised high op=%s d=%s", op, msg.get("d"))
    # Handle JSON DAVE opcodes — op 20 observed in the wild as DAVE_PREPARE_EPOCH
    # (Discord appears to use 20 instead of the documented 24 in current production)
    if handler and op in (20, 21, 22, 24):
        await handler.handle_json(self, msg)

_patched_received_message._orig = _orig_received_message
discord.gateway.DiscordVoiceWebSocket.received_message = _patched_received_message


# ── 1b. poll_event — intercept binary DAVE frames ────────────────────────────
# py-cord's poll_event only handles TEXT frames; binary frames are silently
# dropped.  Discord sends all DAVE MLS messages as binary WebSocket frames.
import aiohttp as _aiohttp
from discord.errors import ConnectionClosed as _ConnectionClosed
from discord import utils as _discord_utils

_orig_poll_event = _unwrap(discord.gateway.DiscordVoiceWebSocket.poll_event)

async def _patched_poll_event(self):
    try:
        msg = await asyncio.wait_for(self.ws.receive(), timeout=30.0)
    except asyncio.TimeoutError:
        _dave_log.debug("poll_event timeout — no WS message in 30s")
        return
    if msg.type is _aiohttp.WSMsgType.TEXT:
        await self.received_message(_discord_utils._from_json(msg.data))
    elif msg.type is _aiohttp.WSMsgType.BINARY:
        _dave_log.debug("binary frame %d bytes", len(msg.data))
        # Auto-attach pending handler on first binary frame too
        global _pending_dave_handler
        handler: DaveHandler | None = getattr(self, "_dave", None)
        if handler is None and _pending_dave_handler is not None:
            _pending_dave_handler.attach(self)
            handler = _pending_dave_handler
            _pending_dave_handler = None
        if handler:
            await handler.handle_binary(self, msg.data)
    elif msg.type is _aiohttp.WSMsgType.ERROR:
        _dave_log.debug("Voice WS error: %s", msg)
        raise _ConnectionClosed(self.ws, shard_id=None) from msg.data
    elif msg.type in (
        _aiohttp.WSMsgType.CLOSED,
        _aiohttp.WSMsgType.CLOSE,
        _aiohttp.WSMsgType.CLOSING,
    ):
        _dave_log.debug("Voice WS closed: %s", msg)
        raise _ConnectionClosed(self.ws, shard_id=None, code=self._close_code)

_patched_poll_event._orig = _orig_poll_event
discord.gateway.DiscordVoiceWebSocket.poll_event = _patched_poll_event

# Pending handler set just before channel.connect() so it can be auto-attached
# the moment the first DAVE opcode arrives (which happens during connect()).
_pending_dave_handler: DaveHandler | None = None


# ── 2. DecodeManager — DAVE-decrypt before Opus decode ───────────────────────

def _patched_decode_manager_run(self):
    import time
    from discord.opus import OpusError

    while not self._end_thread.is_set():
        try:
            data = self.decode_queue.pop(0)
        except IndexError:
            time.sleep(0.001)
            continue

        try:
            if data.decrypted_data is None:
                _dave_log.debug("RTP decrypt returned None for ssrc=%s — dropping", data.ssrc)
                continue

            raw = data.decrypted_data
            _dave_log.debug("decode item ssrc=%s decrypted_len=%d", data.ssrc, len(raw))

            # Attempt DAVE decryption if the handler is ready
            dave: DaveHandler | None = getattr(getattr(self.client, "ws", None), "_dave", None)
            user_id = self.client.ws.ssrc_map.get(data.ssrc, {}).get("user_id")

            opus_bytes = raw   # default: pass raw through
            if dave and user_id is not None:
                if dave.ready and not dave.can_passthrough(user_id):
                    decrypted = dave.decrypt(user_id, raw)
                    if decrypted is not None:
                        opus_bytes = decrypted
                    else:
                        _dave_log.debug("DAVE decrypt returned None for ssrc=%s, skipping", data.ssrc)
                        continue
                elif dave.ready and dave.can_passthrough(user_id):
                    # Try DAVE first, fall back to raw
                    decrypted = dave.decrypt(user_id, raw)
                    if decrypted is not None:
                        opus_bytes = decrypted

            try:
                data.decoded_data = self.get_decoder(data.ssrc).decode(opus_bytes)
            except OpusError:
                _dave_log.debug("OpusError ssrc=%s len=%d first4=%s",
                                data.ssrc, len(opus_bytes),
                                opus_bytes[:4].hex() if opus_bytes else "")
                continue

        except Exception:
            _dave_log.exception("Unexpected error in DecodeManager")
            continue

        self.client.recv_decoded_audio(data)

_patched_decode_manager_run._orig = _unwrap(_opus.DecodeManager.run)
_opus.DecodeManager.run = _patched_decode_manager_run
# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN", "")
RECORDINGS_DIR  = Path("data/recordings")
RING_BUFFER_SEC = 600   # 10 minutes ring buffer
SAMPLE_RATE     = 48000
CHANNELS        = 2
SAMPLE_WIDTH    = 2     # 16-bit

RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Ring buffer ───────────────────────────────────────────────────────────────

class RingBuffer:
    """Stores last N seconds of raw PCM audio per user."""
    def __init__(self, max_seconds: int, sr: int, channels: int, width: int):
        self.max_frames  = max_seconds * sr
        self.sr          = sr
        self.channels    = channels
        self.width       = width
        self._buf: collections.deque[bytes] = collections.deque()
        self._frames     = 0
        self._lock       = threading.Lock()

    def push(self, pcm: bytes):
        frame_size   = self.channels * self.width
        num_frames   = len(pcm) // frame_size
        with self._lock:
            self._buf.append(pcm)
            self._frames += num_frames
            while self._frames > self.max_frames and self._buf:
                old = self._buf.popleft()
                self._frames -= len(old) // frame_size

    def get_last_n_seconds(self, n: int) -> bytes:
        target_frames = n * self.sr
        frame_size    = self.channels * self.width
        with self._lock:
            chunks = list(self._buf)
        # take from the end
        result = []
        collected = 0
        for chunk in reversed(chunks):
            result.append(chunk)
            collected += len(chunk) // frame_size
            if collected >= target_frames:
                break
        result.reverse()
        raw = b"".join(result)
        # trim to exact n seconds
        max_bytes = target_frames * frame_size
        return raw[-max_bytes:] if len(raw) > max_bytes else raw


# ── Audio sink ────────────────────────────────────────────────────────────────

class MultiUserSink(discord.sinks.WaveSink):
    """Captures audio from each user separately."""

    def __init__(self, session_dir: Path):
        super().__init__()
        self.session_dir  = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._ring:  dict[int, RingBuffer] = {}
        self._names: dict[int, str] = {}
        self._lock   = threading.Lock()
        self.start_time = time.time()

    def write(self, data, user):
        # py-cord passes a User object during recording but an int user_id
        # from _process_audio_packet — handle both forms
        uid  = user if isinstance(user, int) else user.id
        if isinstance(user, int):
            member = next(
                (g.get_member(uid) for g in bot.guilds if g.get_member(uid)),
                None
            )
            name = member.display_name if member else str(uid)
        else:
            name = user.display_name
        pcm  = data.data if hasattr(data, "data") else data
        with self._lock:
            if uid not in self._ring:
                log.debug("First audio packet from user %s (%s), pcm_len=%d", uid, name, len(pcm))
                self._ring[uid]  = RingBuffer(
                    RING_BUFFER_SEC, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH
                )
                self._names[uid] = name
            self._ring[uid].push(pcm)

    def get_ring_clip(self, n_seconds: int) -> bytes:
        """Mix all users' ring buffers into one stereo PCM clip."""
        with self._lock:
            clips = {uid: rb.get_last_n_seconds(n_seconds)
                     for uid, rb in self._ring.items()}
        if not clips:
            return b""
        # normalise lengths, mix by summing int16 samples
        max_len = max(len(c) for c in clips.values())
        arrays  = []
        for pcm in clips.values():
            padded = pcm + b"\x00" * (max_len - len(pcm))
            arr    = np.frombuffer(padded, dtype=np.int16).astype(np.float32)
            arrays.append(arr)
        mixed = np.sum(arrays, axis=0)
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return mixed.tobytes()

    def save_all_users(self) -> dict[int, Path]:
        """Write per-user WAV files, return {user_id: path}."""
        paths = {}
        with self._lock:
            names    = dict(self._names)
            ring_pcm = {uid: rb.get_last_n_seconds(RING_BUFFER_SEC)
                        for uid, rb in self._ring.items()}
        log.debug("save_all_users: ring has %d user(s): %s", len(ring_pcm), list(ring_pcm.keys()))
        for uid, pcm in ring_pcm.items():
            if not pcm:
                log.debug("save_all_users: user %s has empty ring buffer, skipping", uid)
                continue
            safe_name = names.get(uid, str(uid)).replace(" ", "_")
            wav_path  = self.session_dir / f"{safe_name}_{uid}.wav"
            _write_wav(wav_path, pcm)
            paths[uid] = wav_path
            log.info("save_all_users: wrote %s (%d bytes PCM)", wav_path.name, len(pcm))
        return paths

    def get_speaker_map(self) -> dict[int, str]:
        with self._lock:
            return dict(self._names)


def _write_wav(path: Path, pcm: bytes):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def mix_to_mono_wav(session_dir: Path, out_path: Path):
    """Merge all per-user WAV files into one mono 16kHz WAV for Whisper."""
    wavs = [p for p in session_dir.glob("*.wav") if p.name != "mixed_mono.wav"]
    if not wavs:
        return
    combined = None
    for p in wavs:
        seg = AudioSegment.from_wav(str(p)).set_channels(1).set_frame_rate(16000)
        combined = seg if combined is None else combined.overlay(seg)
    combined.export(str(out_path), format="wav")


# ── Bot ───────────────────────────────────────────────────────────────────────

intents         = discord.Intents.default()
intents.voice_states = True
intents.guilds  = True
bot             = commands.Bot(command_prefix="!", intents=intents)

_active_sink:         MultiUserSink | None = None
_voice_client:        discord.VoiceClient | None = None
_session_id:          int | None = None
_session_dir:         Path | None = None
_session_log_handler: logging.Handler | None = None
_dave_handler:        DaveHandler | None = None

# callback so GUI can react to bot events
on_session_started = None   # callable(session_id, session_name)
on_session_stopped = None   # callable(session_id, mixed_wav_path, speaker_map)


def _attach_session_log(session_dir: Path) -> logging.Handler:
    """Create a rotating log file inside the session folder and attach it to the root logger."""
    log_path = session_dir / "session.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,   # 5 MB per file
        backupCount=10,              # session.log → session.log.1 … session.log.10
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    return handler


def _detach_session_log(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    try:
        await bot.sync_commands()
    except Exception as e:
        log.exception("Command sync failed: %s", e)


@bot.slash_command(name="join", description="Join a voice channel and start recording")
async def join(ctx: discord.ApplicationContext):
    global _active_sink, _voice_client, _session_id, _session_dir, _session_log_handler, _dave_handler, _pending_dave_handler

    if not ctx.author.voice:
        await ctx.respond("❌ You must be in a voice channel first.", ephemeral=True)
        return

    channel = ctx.author.voice.channel

    if _voice_client and _voice_client.is_connected():
        await ctx.respond("⚠️ Already recording.", ephemeral=True)
        return

    await ctx.defer()

    # Create DAVE handler BEFORE connecting — OP 25 arrives during connect()
    # and must not be missed. _patched_received_message will auto-attach it.
    _pending_dave_handler = DaveHandler(
        user_id=bot.user.id,
        channel_id=channel.id,
    )
    _dave_handler = _pending_dave_handler   # keep reference for cleanup

    _voice_client = await channel.connect()


    # Wait up to 5 s for the voice WebSocket handshake to fully complete
    for _ in range(50):
        if _voice_client.is_connected():
            break
        await asyncio.sleep(0.1)
    else:
        log.error("Voice connection timed out for channel %s (guild %s)", channel.name, ctx.guild.name)
        await _voice_client.disconnect(force=True)
        _voice_client = None
        await ctx.followup.send("❌ Failed to establish voice connection.")
        return

    # Undeafen after connection is fully established so the bot can receive audio
    await channel.guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
    await asyncio.sleep(0.5)   # let the gateway process the state change

    me = channel.guild.me
    vs = me.voice
    log.debug("Voice state after undeafen: self_deaf=%s self_mute=%s channel=%s",
              vs.self_deaf if vs else "N/A",
              vs.self_mute if vs else "N/A",
              vs.channel.name if vs and vs.channel else "N/A")

    log.info("DAVE handler ready=%s status=%s",
             _dave_handler.ready, _dave_handler.status)

    safe_channel = re.sub(r"[^\w-]", "_", unidecode(channel.name))
    session_name = f"{safe_channel}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    _session_dir = RECORDINGS_DIR / session_name
    _session_dir.mkdir(parents=True, exist_ok=True)
    _session_id  = create_session(
        session_name,
        guild=ctx.guild.name,
        channel=channel.name
    )

    _session_log_handler = _attach_session_log(_session_dir)
    log.info("Session started: %s (id=%s)", session_name, _session_id)

    _active_sink = MultiUserSink(_session_dir)
    _voice_client.start_recording(_active_sink, _on_recording_done, ctx.channel)
    log.debug("Recording started on sink %s", _active_sink)

    await ctx.followup.send(f"🎙️ Started recording in **{channel.name}**. Session: `{session_name}`")

    if on_session_started:
        on_session_started(_session_id, session_name)


async def _on_recording_done(sink: MultiUserSink, channel, *args):
    pass  # handled in /leave


async def _do_leave(notify_channel=None, interaction=None):
    """Stop recording, save files, fire callbacks. Shared by /leave and auto-leave."""
    global _active_sink, _voice_client, _session_id, _session_dir, _session_log_handler, _dave_handler

    if _voice_client is None or _active_sink is None:
        log.warning("_do_leave called but not recording — ignoring")
        return

    _voice_client.stop_recording()
    await _voice_client.disconnect()

    user_wavs   = _active_sink.save_all_users()
    speaker_map = _active_sink.get_speaker_map()

    mixed_path = _session_dir / "mixed_mono.wav"
    await asyncio.get_running_loop().run_in_executor(
        None, mix_to_mono_wav, _session_dir, mixed_path
    )

    sid = _session_id
    close_session(sid)

    msg = (
        f"⏹️ Recording stopped. Session ID: **{sid}**\n"
        f"Use `/transcript` to generate and retrieve the transcript."
    )
    if interaction is not None:
        await interaction.followup.send(msg)
    elif notify_channel:
        await notify_channel.send(msg)

    if on_session_stopped:
        on_session_stopped(sid, str(mixed_path), speaker_map)

    log.info("Session ended: id=%s", sid)
    if _session_log_handler:
        _detach_session_log(_session_log_handler)
    if _dave_handler:
        _dave_handler.detach()

    _voice_client        = None
    _active_sink         = None
    _session_log_handler = None
    _dave_handler        = None


_auto_leave_task: asyncio.Task | None = None


@bot.event
async def on_voice_state_update(member, before, after):
    global _auto_leave_task

    if not _voice_client or not _voice_client.is_connected():
        return

    channel = _voice_client.channel
    if channel is None:
        return

    # Count non-bot members still in the channel (excluding the bot itself)
    human_members = [m for m in channel.members if not m.bot]
    if human_members:
        # Someone is still here — cancel any pending auto-leave
        if _auto_leave_task and not _auto_leave_task.done():
            _auto_leave_task.cancel()
            _auto_leave_task = None
            log.info("Auto-leave cancelled — humans still present")
        return

    # Bot is alone — schedule auto-leave in 5 s (avoid double-scheduling)
    if _auto_leave_task and not _auto_leave_task.done():
        return

    log.info("Bot is alone in channel — auto-leaving in 5 s")

    async def _delayed_leave():
        await asyncio.sleep(5)
        if _voice_client and _voice_client.is_connected():
            log.info("Auto-leave triggered")
            await _do_leave(notify_channel=None)

    _auto_leave_task = asyncio.create_task(_delayed_leave())


@bot.slash_command(name="leave", description="Stop recording and leave the channel")
async def leave(ctx: discord.ApplicationContext):
    global _auto_leave_task

    if not _voice_client or not _voice_client.is_connected():
        await ctx.respond("❌ Not recording.", ephemeral=True)
        return

    if _auto_leave_task and not _auto_leave_task.done():
        _auto_leave_task.cancel()
        _auto_leave_task = None

    await ctx.defer()
    await _do_leave(interaction=ctx)


@bot.slash_command(name="save", description="Save the last N minutes of audio (1-10)")
@discord.option("minutes", description="How many minutes to save (1-10)", min_value=1, max_value=10)
async def save_clip(ctx: discord.ApplicationContext, minutes: int = 5):
    if not _active_sink:
        await ctx.respond("❌ Recording is not active.", ephemeral=True)
        return

    minutes = max(1, min(10, minutes))
    await ctx.defer()

    pcm  = _active_sink.get_ring_clip(minutes * 60)
    if not pcm:
        await ctx.followup.send("❌ Buffer is empty.")
        return

    wav  = pcm_to_wav_bytes(pcm)
    ts   = datetime.now().strftime("%H-%M-%S")
    name = f"clip_{ts}_last{minutes}min.wav"

    await ctx.followup.send(
        f"🎵 Last **{minutes} min** of audio:",
        file=discord.File(fp=__import__("io").BytesIO(wav), filename=name)
    )


@bot.slash_command(name="sessions", description="List all recorded sessions for this server")
async def list_sessions(ctx: discord.ApplicationContext):
    guild_name = ctx.guild.name
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, name, started_at, ended_at, channel
               FROM sessions WHERE guild=? ORDER BY started_at DESC""",
            (guild_name,)
        ).fetchall()

    if not rows:
        await ctx.respond("❌ No sessions found for this server.", ephemeral=True)
        return

    lines = []
    for r in rows:
        date  = r["started_at"][:16].replace("T", " ") if r["started_at"] else "?"
        ended = r["ended_at"][:16].replace("T", " ")   if r["ended_at"]   else "ongoing"
        has_transcript = (RECORDINGS_DIR / r["name"] / "transcript.txt").exists()
        flag  = "📝" if has_transcript else "🎙️"
        lines.append(f"{flag} **ID {r['id']}** — {date} → {ended}  |  #{r['channel'] or '?'}  |  `{r['name']}`")

    header  = f"**Sessions for {guild_name}** ({len(rows)} total)\n📝 = transcript ready  🎙️ = not yet transcribed\n\n"
    body    = "\n".join(lines)
    message = header + body

    # Discord message limit is 2000 chars — paginate if needed
    if len(message) <= 2000:
        await ctx.respond(message)
    else:
        await ctx.defer()
        chunks, current = [], header
        for line in lines:
            if len(current) + len(line) + 1 > 2000:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        await ctx.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await ctx.followup.send(chunk)


@bot.slash_command(name="transcript", description="Get the transcript for a session")
@discord.option("id", description="Session ID (omit for the latest session)", required=False, default=None)
async def get_transcript(ctx: discord.ApplicationContext, id: int = None):
    guild_name = ctx.guild.name

    # ── Resolve session ────────────────────────────────────────────────────────
    if id is not None:
        row = get_session(id)
        if row is None:
            await ctx.respond(f"❌ Session **{id}** not found.", ephemeral=True)
            return
        if row["guild"] != guild_name:
            await ctx.respond(
                f"❌ Session **{id}** does not belong to this server.", ephemeral=True
            )
            return
    else:
        row = get_last_session_for_guild(guild_name)
        if row is None:
            await ctx.respond("❌ No sessions found for this server.", ephemeral=True)
            return

    session_id   = row["id"]
    session_name = row["name"]
    session_dir  = RECORDINGS_DIR / session_name
    txt_path     = session_dir / "transcript.txt"
    html_path    = session_dir / "transcript.html"

    # ── Return existing files if available ────────────────────────────────────
    if txt_path.exists() and html_path.exists():
        files = [
            discord.File(str(txt_path),  filename=txt_path.name),
            discord.File(str(html_path), filename=html_path.name),
        ]
        await ctx.respond(
            f"📝 **Transcript:** `{session_name}` (Session ID: {session_id})",
            files=files,
        )
        return

    # ── No transcript yet — run pipeline ──────────────────────────────────────
    mixed_wav = session_dir / "mixed_mono.wav"
    if not mixed_wav.exists():
        await ctx.respond(
            f"❌ No audio found for session **{session_id}**. "
            "The recording may not have been saved yet.",
            ephemeral=True,
        )
        return

    await ctx.defer()
    log.info("Transcript command: running pipeline for session %s", session_id)

    def _run_pipeline():
        from processor import process_session, export_txt, export_html
        segs = process_session(str(mixed_wav), session_id)
        export_txt(segs, str(txt_path))
        export_html(segs, str(html_path), session_name=session_name)

    try:
        await asyncio.get_running_loop().run_in_executor(None, _run_pipeline)
    except Exception as e:
        log.exception("Transcript pipeline failed for session %s", session_id)
        await ctx.followup.send(f"❌ Transcription failed: {e}")
        return

    files = [
        discord.File(str(p), filename=p.name)
        for p in (txt_path, html_path)
        if p.exists()
    ]
    await ctx.followup.send(
        f"📝 **Transcript ready:** `{session_name}` (Session ID: {session_id})",
        files=files,
    )


def run_bot(token: str = None):
    bot.run(token or DISCORD_TOKEN)
