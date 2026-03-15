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

from db import create_session, close_session

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
        super().write(data, user)           # WaveSink accumulates audio_data
        uid = user.id
        pcm = data.data if hasattr(data, "data") else data
        with self._lock:
            if uid not in self._ring:
                self._ring[uid]  = RingBuffer(
                    RING_BUFFER_SEC, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH
                )
                self._names[uid] = user.display_name
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
            names = dict(self._names)
        for uid, audio in self.audio_data.items():
            wav_bytes = audio.file.getvalue()
            if not wav_bytes:
                continue
            safe_name = names.get(uid, str(uid)).replace(" ", "_")
            wav_path  = self.session_dir / f"{safe_name}_{uid}.wav"
            with open(wav_path, "wb") as f:
                f.write(wav_bytes)
            paths[uid] = wav_path
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
    wavs = list(session_dir.glob("*.wav"))
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
    global _active_sink, _voice_client, _session_id, _session_dir

    if not ctx.author.voice:
        await ctx.respond("❌ You must be in a voice channel first.", ephemeral=True)
        return

    channel = ctx.author.voice.channel

    if _voice_client and _voice_client.is_connected():
        await ctx.respond("⚠️ Already recording.", ephemeral=True)
        return

    await ctx.defer()

    _voice_client = await channel.connect()
    await channel.guild.change_voice_state(channel=channel, self_deaf=False)

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

    await ctx.followup.send(f"🎙️ Started recording in **{channel.name}**. Session: `{session_name}`")

    if on_session_started:
        on_session_started(_session_id, session_name)


async def _on_recording_done(sink: MultiUserSink, channel, *args):
    pass  # handled in /leave


@bot.slash_command(name="leave", description="Stop recording and leave the channel")
async def leave(ctx: discord.ApplicationContext):
    global _active_sink, _voice_client, _session_id, _session_dir, _session_log_handler

    if not _voice_client or not _voice_client.is_connected():
        await ctx.respond("❌ Not recording.", ephemeral=True)
        return

    await ctx.defer()

    _voice_client.stop_recording()
    await _voice_client.disconnect()

    # save per-user wavs
    user_wavs = _active_sink.save_all_users()
    speaker_map = _active_sink.get_speaker_map()

    # mix to mono for whisper
    mixed_path = _session_dir / "mixed_mono.wav"
    await asyncio.get_event_loop().run_in_executor(
        None, mix_to_mono_wav, _session_dir, mixed_path
    )

    close_session(_session_id)

    await ctx.followup.send(
        f"⏹️ Recording stopped. Files saved:\n"
        + "\n".join(f"• {p.name}" for p in user_wavs.values())
        + f"\n\nUse the GUI to transcribe (`{mixed_path}`)"
    )

    if on_session_stopped:
        on_session_stopped(_session_id, str(mixed_path), speaker_map)

    log.info("Session ended: id=%s", _session_id)
    if _session_log_handler:
        _detach_session_log(_session_log_handler)

    _voice_client        = None
    _active_sink         = None
    _session_log_handler = None


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


def run_bot(token: str = None):
    bot.run(token or DISCORD_TOKEN)
