"""
processor.py — pyannote diarization + Whisper transcription pipeline
Run after a session: merges per-user .wav files into a single transcript.
"""

import logging
import os
import json
import torch
import whisper
import numpy as np
from pathlib import Path
from datetime import timedelta
from pyannote.audio import Pipeline

from db import insert_segments

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

WHISPER_MODEL   = "large-v3"          # best quality; fits in 12 GB VRAM
DEVICE          = "cuda" if torch.cuda.is_available() else "cpu"
HF_TOKEN        = os.getenv("HF_TOKEN", "")   # needed for pyannote models


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_time(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_whisper():
    log.info("Loading Whisper model %s on %s", WHISPER_MODEL, DEVICE)
    return whisper.load_model(WHISPER_MODEL, device=DEVICE)


def load_diarizer():
    log.info("Loading pyannote speaker-diarization-3.1")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=HF_TOKEN
    )
    pipeline.to(torch.device(DEVICE))
    return pipeline


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_session(audio_path: str, session_id: int,
                    progress_cb=None) -> list[dict]:
    """
    audio_path  — path to merged .wav file (mono, 16kHz recommended)
    session_id  — DB session id to store results
    progress_cb — optional callable(step: str, pct: int)

    Returns list of segment dicts.
    """
    def _progress(step, pct):
        if progress_cb:
            progress_cb(step, pct)
        log.debug("[%3d%%] %s", pct, step)

    _progress("Loading models...", 0)
    whisper_model = load_whisper()
    diarizer      = load_diarizer()

    # ── Step 1: diarization ───────────────────────────────────────────────────
    _progress("Diarization (who speaks when)...", 20)
    diarization = diarizer(audio_path)

    # Build speaker turn list: [(start, end, speaker_label), ...]
    turns = [
        (turn.start, turn.end, speaker)
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
    _progress(f"Found {len(set(t[2] for t in turns))} speakers, {len(turns)} segments", 40)

    # ── Step 2: load audio for whisper ────────────────────────────────────────
    _progress("Loading audio for transcription...", 45)
    audio = whisper.load_audio(audio_path)
    sr    = 16000  # whisper always uses 16kHz internally

    # ── Step 3: transcribe each turn ─────────────────────────────────────────
    _progress("Transcribing...", 50)
    segments = []
    total = len(turns)

    for i, (start, end, speaker) in enumerate(turns):
        pct = 50 + int((i / total) * 45)
        if i % 10 == 0:
            _progress(f"Transcribing segment {i+1}/{total}...", pct)

        # slice audio for this turn
        s = int(start * sr)
        e = int(end   * sr)
        chunk = audio[s:e]

        if len(chunk) < sr * 0.3:   # skip < 300ms (noise/breath)
            continue

        # pad/trim to fit whisper's 30s window if needed
        chunk_padded = whisper.pad_or_trim(chunk)
        mel = whisper.log_mel_spectrogram(chunk_padded).to(DEVICE)

        result = whisper_model.transcribe(
            chunk,
            language=None,        # auto-detect (handles rus+eng mix)
            task="transcribe",
            fp16=(DEVICE == "cuda"),
            verbose=False
        )
        text = result["text"].strip()

        if not text:
            continue

        segments.append({
            "speaker": speaker,
            "start":   round(start, 2),
            "end":     round(end,   2),
            "text":    text
        })

    # ── Step 4: save to DB ────────────────────────────────────────────────────
    _progress("Saving to database...", 95)
    insert_segments(session_id, segments)

    _progress("Done!", 100)
    return segments


# ── Export ────────────────────────────────────────────────────────────────────

def export_txt(segments: list[dict], path: str):
    lines = []
    for s in segments:
        lines.append(f"[{fmt_time(s['start'])}]  {s['speaker']}: {s['text']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def export_html(segments: list[dict], path: str, session_name: str = ""):
    # assign consistent colors per speaker
    speakers = sorted(set(s["speaker"] for s in segments))
    palette  = ["#4F8EF7", "#E06C75", "#56B6C2", "#E5C07B",
                "#98C379", "#C678DD", "#61AFEF", "#D19A66"]
    color_map = {sp: palette[i % len(palette)] for i, sp in enumerate(speakers)}

    rows = ""
    for s in segments:
        color = color_map[s["speaker"]]
        rows += f"""
        <tr>
          <td class="time">{fmt_time(s['start'])}</td>
          <td class="speaker" style="color:{color}">{s['speaker']}</td>
          <td class="text">{s['text']}</td>
        </tr>"""

    speaker_legend = "".join(
        f'<span class="badge" style="background:{color_map[sp]}">{sp}</span>'
        for sp in speakers
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{session_name}</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background:#1e1e2e; color:#cdd6f4; margin:0; padding:20px; }}
  h1   {{ color:#cba6f7; }}
  .badges {{ margin-bottom:16px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px;
            color:#1e1e2e; font-weight:600; margin-right:6px; font-size:.85em; }}
  .search-bar {{ margin-bottom:16px; }}
  #search {{ width:400px; padding:8px 12px; border-radius:8px; border:1px solid #45475a;
             background:#313244; color:#cdd6f4; font-size:1em; }}
  table  {{ width:100%; border-collapse:collapse; }}
  tr:hover {{ background:#313244; }}
  td     {{ padding:6px 10px; vertical-align:top; border-bottom:1px solid #313244; }}
  .time  {{ color:#6c7086; font-size:.85em; white-space:nowrap; width:80px; }}
  .speaker {{ font-weight:700; width:120px; }}
  .text  {{ line-height:1.5; }}
  .hidden {{ display:none; }}
</style>
</head>
<body>
<h1>📝 {session_name}</h1>
<div class="badges">{speaker_legend}</div>
<div class="search-bar">
  <input id="search" type="text" placeholder="Search text..." oninput="filterRows(this.value)">
</div>
<table id="log">
  <thead><tr><th>Time</th><th>Speaker</th><th>Text</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<script>
function filterRows(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('#log tbody tr').forEach(row => {{
    row.classList.toggle('hidden', q && !row.cells[2].textContent.toLowerCase().includes(q));
  }});
}}
</script>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
