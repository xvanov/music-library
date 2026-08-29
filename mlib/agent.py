"""One-shot LLM assist for picking real songs out of search results.

Token budget is the whole design here. Searching and filtering are deterministic
and free; the model is handed a pre-trimmed shortlist and asked for exactly one
JSON answer. A typical run costs on the order of a thousand tokens, which keeps
it viable on a $20 ChatGPT plan.

The backend is any CLI that reads a prompt and prints text (Codex by default),
so this never needs an API key of its own.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

# Titles that are almost never a single track. Cheaper to reject here than to
# spend tokens asking a model about them.
REJECT = re.compile(
    r"\b(full album|album completo|mix|megamix|dj set|compilation|greatest hits|"
    r"best of|non[- ]?stop|playlist|mixtape|live (set|concert|at|in)|"
    r"reaction|review|interview|documentary|tutorial|lesson|karaoke|"
    r"\d+\s*(hours?|hrs?)|top \d+)\b",
    re.I,
)

MIN_SECONDS = 60
MAX_SECONDS = 600

PROMPT = """You are picking real individual songs out of search results.

Request: {request}

Candidates (index|duration|title|channel):
{candidates}

Rules:
- Keep only single songs matching the request. Reject albums, mixes, sets, covers
  unless asked, and anything that is not music.
- artist: the performing artist, NOT the channel name, unless they are the same.
- album: the real album if you know it, otherwise "Singles".
- title: the song title alone, no featured-artist suffixes, no "(Official Video)".
- Keep at most {limit}. If nothing fits, return an empty list.

Reply with ONLY this JSON, no prose, no code fences:
{{"picks":[{{"i":1,"artist":"...","album":"...","title":"..."}}]}}"""


class AgentError(RuntimeError):
    pass


def default_backend():
    """The CLI used to answer the one prompt. Override with MLIB_LLM_CMD."""
    override = os.environ.get("MLIB_LLM_CMD")
    if override:
        return override
    for candidate in ("codex", "claude"):
        if shutil.which(candidate):
            return candidate
    return ""


def prefilter(candidates, want=15):
    """Drop the obvious non-songs before spending any tokens."""
    kept = []
    for c in candidates:
        duration = c.get("duration") or 0
        if duration and not (MIN_SECONDS <= duration <= MAX_SECONDS):
            continue
        if REJECT.search(c.get("raw_title") or ""):
            continue
        kept.append(c)
        if len(kept) >= want:
            break
    return kept


def _render(candidates):
    lines = []
    for i, c in enumerate(candidates, 1):
        seconds = int(c.get("duration") or 0)
        lines.append(
            "{}|{}:{:02d}|{}|{}".format(
                i, seconds // 60, seconds % 60, c.get("raw_title") or "", c.get("channel") or ""
            )
        )
    return "\n".join(lines)


SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "artist": {"type": "string"},
                    "album": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["i", "artist", "album", "title"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


def _resolve(exe):
    """Absolute path to the executable.

    On Windows an npm-installed CLI is a .CMD shim, which subprocess cannot exec
    by bare name, so always run the resolved path.
    """
    found = shutil.which(exe)
    if not found:
        raise AgentError(
            "{} not found. Install it, or set MLIB_LLM_CMD to another CLI.".format(exe)
        )
    return found


def _run_backend(cmd, prompt, timeout=180):
    """Invoke the CLI non-interactively and return its final message."""
    parts = cmd.split()
    exe = parts[0]
    parts[0] = _resolve(exe)

    if exe != "codex":
        argv = parts + (["-p"] if exe == "claude" else [])
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode != 0:
            raise AgentError("{} failed: {}".format(exe, (proc.stderr or proc.stdout)[-400:]))
        return proc.stdout

    # Codex bills its own harness overhead (repo files, AGENTS.md, tool defs) on
    # every call, so run it from an empty scratch directory rather than the repo.
    # --output-schema pins the response shape; --output-last-message keeps the
    # session banner and token accounting out of what we parse.
    with tempfile.TemporaryDirectory(prefix="mlib-codex-") as workdir:
        schema_path = os.path.join(workdir, "schema.json")
        answer_path = os.path.join(workdir, "answer.json")
        with open(schema_path, "w", encoding="utf-8") as fh:
            json.dump(SCHEMA, fh)

        argv = parts + [
            "exec",
            "--skip-git-repo-check",
            "--cd", workdir,
            "--output-schema", schema_path,
            "--output-last-message", answer_path,
            "-",
        ]
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if os.path.exists(answer_path):
            with open(answer_path, encoding="utf-8") as fh:
                answer = fh.read().strip()
            if answer:
                return answer
        if proc.returncode != 0:
            raise AgentError("codex failed: {}".format((proc.stderr or proc.stdout)[-400:]))
        return proc.stdout


def _extract_json(text):
    """Pull the JSON object out of whatever framing the CLI wrapped it in."""
    text = re.sub(r"```(?:json)?", "", text)
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : i + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(parsed, dict) and "picks" in parsed:
                    return parsed
                start = None
    raise AgentError("no JSON object with a 'picks' key in the model output")


def choose(request, candidates, limit=5, backend=None, timeout=180):
    """Return [(candidate, {artist, album, title}), ...] chosen by the model."""
    shortlist = prefilter(candidates)
    if not shortlist:
        return []

    backend = backend or default_backend()
    if not backend:
        raise AgentError("no LLM CLI found; install codex or set MLIB_LLM_CMD")

    prompt = PROMPT.format(request=request, candidates=_render(shortlist), limit=limit)
    data = _extract_json(_run_backend(backend, prompt, timeout=timeout))

    chosen = []
    for pick in data.get("picks", [])[:limit]:
        try:
            idx = int(pick["i"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= idx < len(shortlist):
            continue
        chosen.append(
            (
                shortlist[idx],
                {
                    "artist": (pick.get("artist") or "").strip(),
                    "album": (pick.get("album") or "Singles").strip() or "Singles",
                    "title": (pick.get("title") or "").strip(),
                },
            )
        )
    return chosen


def estimate_tokens(request, candidates, limit=5):
    """Rough size of the single prompt, for reporting before spending anything."""
    shortlist = prefilter(candidates)
    prompt = PROMPT.format(request=request, candidates=_render(shortlist), limit=limit)
    return len(prompt) // 4, len(shortlist)
