# Transcription Pipeline

The pipeline lives in `processor.py` and is triggered either by the GUI's **▶ Start Processing** button or the Discord `/transcript` command.

Entry point: `process_session(audio_path, session_id, progress_cb=None)`

---

## Steps

### 1. Load models (0%)

```python
whisper_model = whisper.load_model("large-v3", device=DEVICE)
diarizer      = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=HF_TOKEN)
diarizer.to(torch.device(DEVICE))
```

Both models are loaded fresh each run and explicitly unloaded afterwards to free VRAM.

**DEVICE** is `"cuda"` if a CUDA GPU is available, otherwise `"cpu"`. On Ampere+ GPUs (RTX 30xx/40xx), TF32 is enabled for better throughput.

Model cache locations:
- Whisper → `~/.cache/whisper/`
- pyannote → `~/.cache/huggingface/hub/`

---

### 2. Diarization — who speaks when (20%)

```python
sample_rate, samples = scipy.io.wavfile.read(audio_path)
waveform = torch.from_numpy(samples).float()
# normalise to [-1, 1], add batch dim if needed
diarization = diarizer({"waveform": waveform, "sample_rate": sample_rate},
                       num_speakers=num_speakers)
turns = [(turn.start, turn.end, speaker)
         for turn, _, speaker in diarization.speaker_diarization.itertracks(yield_label=True)]
```

**`num_speakers` constraint:** The pipeline counts non-empty per-user WAV files in the session directory. Users who joined but never spoke produce near-empty files (≤ 44 bytes = WAV header only) and are excluded. Passing the exact speaker count to pyannote significantly improves diarization accuracy for small groups.

Example: 2 users for 30 min then 2 more join → 4 non-empty WAVs → `num_speakers=4`.

---

### 3. Speaker → Discord name mapping (40%)

pyannote labels speakers as `SPEAKER_00`, `SPEAKER_01`, etc. These are mapped to Discord display names using audio energy correlation.

**Algorithm:**
1. Load each per-user WAV file (`Username_ID.wav`) with scipy
2. For every `(speaker_label, user)` pair, accumulate RMS energy across all turns where that speaker is active
3. Greedy assignment: the speaker label with the highest total energy for a given user wins; each user is assigned at most once

```python
for start, end, speaker in turns:
    for uid, (name, mono, sr) in user_audio.items():
        chunk = mono[int(start*sr) : int(end*sr)]
        votes[speaker][uid] += sqrt(mean(chunk ** 2))   # RMS energy
```

The result is a map like `{"SPEAKER_00": "Alice(290431177272066048)"}`. Speaker labels in all subsequent steps use the `Name(ID)` format.

---

### 4. Language phase detection (47%)

Whisper supports forced language selection per segment. Rather than auto-detecting language each time (which flips on single foreign words like "ok" or "coffee"), the pipeline:

1. For each turn > 300ms, runs `whisper_model.detect_language(mel)` to get language probabilities
2. Feeds the raw per-segment detections into `_smooth_language_phases()` which commits a language switch only after **5 consecutive segments** in the new language

```
raw:      ru ru ru en ru ru en en en en en en
smoothed: ru ru ru ru ru ru ru en en en en en
                              ^--- switch committed after 5× en
```

Turns shorter than 300ms are tagged `None` and inherit the previous language (forward-fill).

---

### 5. Transcription — Whisper STT (50–95%)

Each speaker turn is transcribed individually with the smoothed language forced:

```python
result = whisper_model.transcribe(
    chunk,
    language=smoothed_langs[i],   # e.g. "ru" or "en"
    task="transcribe",
    fp16=(DEVICE == "cuda"),
    verbose=False,
)
```

Key details:
- Audio sliced from the same numpy array loaded in step 2 (no second disk read)
- `n_mels=whisper_model.dims.n_mels` — required for `large-v3` which uses 128 mel bins (default is 80)
- Segments shorter than 300ms or with empty transcription are skipped
- Progress is reported every 10 segments

---

### 6. LLM post-processing (92%)

The full transcript is sent to a local ollama model (`qwen2.5:7b` by default) to fix transcription errors — wrong words, missing punctuation, misheard proper nouns.

**Prompt structure:**
```
system: You are a transcription correction tool. [injection-resistant instructions]

user:   Fix transcription errors in this voice chat recording.
        <transcript>
        [0] Alice(123) (00:00:05): transcribed text here
        [1] Bob(456)   (00:00:12): more text
        </transcript>
        Return only valid JSON like: ["corrected text 0", "corrected text 1", ...]
```

**Security:** User speech content could contain prompt injection attempts. Three defences:
1. `_sanitize_text()` — strips XML/HTML tags, collapses excess whitespace, truncates to 1000 chars
2. `_looks_like_injection()` — regex for "ignore previous instructions", role-change phrases, etc. — flagged segments are replaced with `[inaudible]`
3. `<transcript>` XML delimiters + system message instruction that content inside is data, not commands

**Output validation:**
- Must be a JSON array
- Length must match the number of input segments
- Each item must be a string
- Items suspiciously longer than `3× original + 200 chars` are rejected (kept as original)

If ollama is not running or the request fails, the step is silently skipped and original Whisper output is used.

---

### 7. Save to database (97%)

```python
insert_segments(session_id, segments)
```

Each segment is inserted as a row in `segments` with `(session_id, speaker, start_sec, end_sec, text)`. The FTS5 virtual table is populated automatically via an `AFTER INSERT` trigger.

---

### 8. Unload models (99%)

```python
del whisper_model, diarizer
if DEVICE == "cuda":
    torch.cuda.empty_cache()

# Ask ollama to unload its model immediately
ollama.chat(model=LLM_MODEL, messages=[...], keep_alive=0)
```

Freeing VRAM after processing allows the GPU to be used for other tasks. On an RTX 4070 Ti (12 GB), Whisper large-v3 + pyannote together occupy ~7–8 GB.

---

## Export formats

### transcript.txt

Plain text, one line per segment:
```
[00:00:05]  Alice(290431177272066048): Привет, как дела?
[00:00:08]  Bob(123456789): Хорошо, спасибо!
```

### transcript.html

Self-contained HTML file with:
- Dark theme matching the GUI
- Speaker badges with unique colours (up to 8 speakers)
- In-page text search (client-side JS, no server needed)
- All user content HTML-escaped to prevent XSS

---

## Performance

Benchmarks on RTX 4070 Ti (12 GB VRAM), 4.5 hours of audio, 2 speakers:

| Step | Time |
|---|---|
| Model loading | ~60 s |
| Diarization | ~3 min |
| Language detection | ~1 min |
| Whisper transcription | ~8 min |
| LLM fix (qwen2.5:7b) | ~2 min |
| **Total** | **~15 min** |

On CPU only, expect 10–20× longer for the Whisper and diarization steps.

---

## Running from CLI (no GUI)

```python
from processor import process_session, export_txt, export_html
from db import init_db

init_db()
segments = process_session("data/recordings/my_session/mixed_mono.wav", session_id=1)
export_txt(segments, "out.txt")
export_html(segments, "out.html", session_name="My Session")
```
