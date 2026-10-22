"""Listen to staged files before they are published.

Three failure modes this exists for, all seen in practice:

- The upload is not the song. A search for a march returns a news report, a
  documentary or a let's-play that happens to share the title. Three short
  snippets are transcribed; music yields a few words (or Whisper's repetitive
  hallucinations), speech yields a steady stream of distinct words.
- An electronic remix under a classic title. Brass bands, orchestras and choirs
  put almost nothing below 60 Hz; electronic production is built on sub-bass.
- The same recording twice. Each track gets a chroma fingerprint, compared at
  every time offset against the other staged files and the whole library.

Calibrated on a collection of Soviet marches and songs - Russian, mostly brass
bands and choirs (56 files, 7 of them not songs): spoken
tracks scored 0.7-1.5 distinct words/s, plus one dialogue at 0.56 that was full of
pauses; songs scored <= 0.58, and none of those near the line were gappy. An
identical recording re-encoded and trimmed scored 0.83 similarity; the closest
pair of different recordings scored 0.28.
The one electronic remix had 61% of its energy below 60 Hz; acoustic recordings
had at most 26%, most under 5%.

The thresholds come from Russian speech and song; other languages have not been
measured, so treat a near-the-line `speech` verdict with care. The language is
per file (`settings`): a collection's own `lang`, else MLIB_CHECK_LANG, else
Whisper detects it. The sub-bass test only makes sense where electronic
production is wrong, so it runs only for collections marked `acoustic` - a
techno or hip-hop collection would otherwise be held wholesale.

Whisper is optional (`pip install -e .[check]`). Without it, only the energy
pattern is used, which catches plain talk but misses speech over a backing track.
"""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import store

SR = 16000
SPEECH_WPS = 0.6       # distinct transcribed words per second -> talking
TALKY_WPS = 0.45       # ...or this many, when the energy also has speech's gaps
PAUSE_RATIO = 0.15     # share of 50ms frames that dip, see pause_ratio()
PAUSE_ONLY = 0.2       # fallback without Whisper: gaps alone
SAME_RECORDING = 0.6   # chroma similarity -> same recording
SUB_BASS = 0.4         # share of energy in 20-60 Hz -> electronic production
VOCALS_WPS = 0.25      # distinct words/s in an *instrumental* -> someone is singing
MIN_LEN, MAX_LEN = 45, 900

CACHE = Path.home() / ".cache" / "mlib" / "fingerprints"


def decode(path, seconds=None):
    cmd = ["ffmpeg", "-v", "quiet", "-i", str(path), "-ac", "1", "-ar", str(SR)]
    if seconds:
        cmd += ["-t", str(seconds)]
    raw = subprocess.run(cmd + ["-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


# -- is it music? --------------------------------------------------------


def pause_ratio(x):
    """Share of 50ms frames well below the energy of their surrounding second."""
    f = int(0.05 * SR)
    rms = np.sqrt(np.mean(x[: len(x) // f * f].reshape(-1, f) ** 2, axis=1) + 1e-12)
    secs = [rms[i:i + 20] for i in range(0, len(rms) - 20, 20)]
    return float(np.mean([np.mean(s < 0.5 * s.mean()) for s in secs])) if secs else 0.0


def sub_bass(x):
    """Share of spectral energy in 20-60 Hz, sampled across the whole track."""
    n = 8192
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::n * 4] * np.hanning(n)
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).sum(0)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    total = power[freqs > 20].sum()
    return float(power[(freqs > 20) & (freqs < 60)].sum() / total) if total else 0.0


_model = None


def _whisper():
    global _model
    if _model is None:
        import torch
        import whisper

        _model = whisper.load_model("base", device="cuda" if torch.cuda.is_available() else "cpu")
    return _model


def have_whisper():
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def talk_rate(x, snippet=30, lang=None):
    """Distinct words per second over three snippets, plus a sample of what was heard.
    `lang` is a Whisper language code; None lets Whisper detect it."""
    import torch

    model, dur = _whisper(), len(x) / SR
    words, heard = set(), []
    for frac in (0.2, 0.5, 0.8):
        start = int(max(0, frac * dur - snippet / 2) * SR)
        r = model.transcribe(x[start:start + snippet * SR], language=lang, temperature=0.0,
                             condition_on_previous_text=False, fp16=torch.cuda.is_available())
        text = " ".join(s["text"].strip() for s in r["segments"] if s["no_speech_prob"] < 0.6)
        words.update(w.strip(".,!?;:-—«»\"'").lower() for w in text.split())
        heard.append(text)
    words.discard("")
    return len(words) / (3 * min(snippet, dur)), max(heard, key=len)[:100]


# -- is it already here? -------------------------------------------------


def fingerprint(x):
    """Pitch-class energy per ~0.5s, mean-centred so unrelated tracks score near 0."""
    n, hop = 4096, 2048
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / SR)
    keep = (freqs > 60) & (freqs < 4000)
    pitch = np.round(12 * np.log2(freqs[keep] / 440)).astype(int) % 12
    c = np.stack([power[:, keep][:, pitch == k].sum(1) for k in range(12)])
    c /= c.sum(0, keepdims=True) + 1e-12
    c = c[:, : c.shape[1] // 4 * 4].reshape(12, -1, 4).mean(2)
    c -= c.mean(1, keepdims=True)
    return (c / (np.linalg.norm(c, axis=0, keepdims=True) + 1e-9)).astype(np.float32)


def similarity(a, b, min_overlap=60):
    """Best mean frame agreement over all offsets with at least ~30s overlap."""
    best = -1.0
    for lag in range(-(b.shape[1] - min_overlap), a.shape[1] - min_overlap + 1, 2):
        lo, hi = max(0, lag), min(a.shape[1], lag + b.shape[1])
        if hi - lo >= min_overlap:
            best = max(best, float(np.mean(np.sum(a[:, lo:hi] * b[:, lo - lag:hi - lag], 0))))
    return best


def library_fingerprints(progress=print):
    """Fingerprint every library file, cached so each is downloaded only once."""
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = store.listing()
    out = {}
    for rel in paths:
        cached = CACHE / (hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".npy")
        if not cached.exists():
            progress("  fingerprinting library: " + rel)
            with tempfile.TemporaryDirectory() as tmp:
                local = store.get(rel, Path(tmp) / ("t" + Path(rel).suffix))
                np.save(cached, fingerprint(decode(local)))
        out[rel] = np.load(cached)
    # forget files that have left the library
    live = {c.name for c in (CACHE / (hashlib.sha1(r.encode("utf-8")).hexdigest() + ".npy")
                             for r in paths)}
    for stale in CACHE.glob("*.npy"):
        if stale.name not in live:
            stale.unlink()
    return out


# -- verdicts ------------------------------------------------------------


def check_files(files, read_meta, write_meta, library=True, progress=print, settings=None):
    """Judge staged files, storing {'verdict', 'detail'} in each sidecar.

    `settings(meta)` returns per-file options: {'lang': 'ru' or None (detect),
    'acoustic': True to hold electronic production}. Default: detect, not acoustic."""
    settings = settings or (lambda meta: {})
    use_whisper = have_whisper()
    if not use_whisper:
        progress("(whisper not installed - speech over music will be missed; "
                 "pip install -e .[check])")
    known = library_fingerprints(progress) if library else {}
    seen = {}
    results = []
    for path in files:
        meta = read_meta(path)
        opts = settings(meta)
        x = decode(path)
        dur = len(x) / SR
        fp = fingerprint(x)
        verdict, detail = "ok", ""
        instrumental = meta.get("version") == "instrumental" or             str(meta.get("album") or "").endswith("instrumental")
        rate = pauses = None

        bass = sub_bass(x)
        if not MIN_LEN <= dur <= MAX_LEN:
            verdict, detail = "length", "{:.0f}s does not look like one song".format(dur)
        elif opts.get("acoustic") and bass >= SUB_BASS:
            verdict, detail = "electronic", "{:.0%} of energy below 60 Hz - a remix, not a band".format(bass)
        else:
            pauses = pause_ratio(x)
            if use_whisper:
                rate, heard = talk_rate(x, lang=opts.get("lang"))
                if rate >= SPEECH_WPS or (rate >= TALKY_WPS and pauses >= PAUSE_RATIO):
                    verdict, detail = "speech", "{:.2f} words/s: {}".format(rate, heard)
                elif instrumental and rate >= VOCALS_WPS:
                    # A "march" that turned out to be sung, or another piece entirely.
                    verdict, detail = "vocals", "{:.2f} words/s in an instrumental: {}".format(rate, heard)
            elif pauses >= PAUSE_ONLY:
                verdict, detail = "speech", "pause pattern looks like talk"

        if verdict == "ok":
            for name, other in list(seen.items()) + list(known.items()):
                score = similarity(fp, other)
                if score >= SAME_RECORDING:
                    verdict, detail = "duplicate", "same recording as {} ({:.2f})".format(name, score)
                    break
        seen["staged " + path.name] = fp

        meta["check"] = {"verdict": verdict, "detail": detail,
                         "metrics": {"seconds": round(dur), "sub_bass": round(bass, 3),
                                     "words_per_s": None if rate is None else round(rate, 2),
                                     "pauses": None if pauses is None else round(pauses, 3)}}
        write_meta(path, meta)
        results.append((path, verdict, detail))
        progress("  {:9} {}{}".format(verdict, path.name, "  - " + detail if detail else ""))
    return results


def cached_verdict(meta):
    return (meta.get("check") or {}).get("verdict")
