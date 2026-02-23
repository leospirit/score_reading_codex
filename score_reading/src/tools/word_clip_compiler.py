from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import tempfile
from html import unescape
from difflib import SequenceMatcher
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, quote_plus, urlencode
from urllib.request import Request, urlopen


ProgressCallback = Callable[[float, str], None]


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class SourceCandidate:
    url: str
    title: str
    start_seconds: float | None = None
    source_type: str = "youtube"


@dataclass
class ClipResult:
    clip_path: Path
    subtitle_text: str
    source_title: str
    source_url: str


DOMAIN_QUERY_HINTS: dict[str, str] = {
    "education": "classroom lesson reading practice",
    "technology": "tech talk interview explain",
    "entertainment": "movie scene interview vlog",
    "business": "presentation meeting speech",
    "sports": "commentary interview highlight",
    "news": "news report interview",
    "phoneme_demo": (
        "english pronunciation explanation ipa phonics mouth position "
        "word stress minimal pairs common mistakes"
    ),
}

_ENCODER_CACHE_LOCK = threading.Lock()
_ENCODER_CACHE: set[str] | None = None
_SOURCE_CACHE_LOCK = threading.Lock()
_SOURCE_CACHE: dict[str, tuple[float, list[SourceCandidate]]] = {}
_SOURCE_CACHE_TTL_SECONDS = 1800
_IMPERSONATE_CHECK_LOCK = threading.Lock()
_IMPERSONATE_AVAILABLE: bool | None = None
_PO_TOKEN_LOCK = threading.Lock()
_PO_TOKEN_CACHE: tuple[float, dict[str, str]] | None = None
_PO_TOKEN_CACHE_TTL_SECONDS = 20.0
_SOURCE_POOL_LOCK = threading.Lock()
_SOURCE_POOL_PATH = Path("data/word_clips/source_pool.json")
_SEARCH_QUERY_DELAY_SECONDS = 0.18
_DOWNLOAD_ATTEMPT_DELAY_SECONDS = 0.12
_PER_SOURCE_DELAY_SECONDS = 0.05
_WHISPER_CHECK_LOCK = threading.Lock()
_WHISPER_MODEL = None
_WHISPER_AVAILABLE: bool | None = None
_ASR_KEYWORD_CACHE: dict[str, list[float]] = {}


def _emit(progress: ProgressCallback | None, value: float, message: str) -> None:
    if progress is None:
        return
    bounded = max(0.0, min(1.0, value))
    progress(bounded, message)


def _word_key(word: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (word or "").lower())


def _domain_key(domains: list[str]) -> str:
    cleaned = [item.strip().lower() for item in domains if item and item.strip()]
    if not cleaned:
        return "education"
    return "|".join(sorted(set(cleaned)))


def _load_source_pool() -> dict[str, list[dict[str, object]]]:
    with _SOURCE_POOL_LOCK:
        if not _SOURCE_POOL_PATH.exists():
            return {}
        try:
            payload = json.loads(_SOURCE_POOL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, list[dict[str, object]]] = {}
        for key, rows in payload.items():
            if not isinstance(key, str) or not isinstance(rows, list):
                continue
            cleaned_rows: list[dict[str, object]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "").strip()
                title = str(row.get("title") or "").strip()
                if not url:
                    continue
                cleaned_rows.append(
                    {
                        "url": url,
                        "title": title or "Untitled",
                        "score": int(row.get("score") or 1),
                        "last_ok": float(row.get("last_ok") or 0.0),
                    }
                )
            if cleaned_rows:
                result[key] = cleaned_rows
        return result


def _save_source_pool(data: dict[str, list[dict[str, object]]]) -> None:
    with _SOURCE_POOL_LOCK:
        _SOURCE_POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SOURCE_POOL_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _get_cached_sources(word: str, domains: list[str], *, max_count: int) -> list[SourceCandidate]:
    if max_count <= 0:
        return []
    pool = _load_source_pool()
    key = f"{_word_key(word)}::{_domain_key(domains)}"
    rows = list(pool.get(key) or [])
    rows.sort(key=lambda item: (int(item.get("score") or 0), float(item.get("last_ok") or 0.0)), reverse=True)
    out: list[SourceCandidate] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip() or "Untitled"
        if not url or url in seen:
            continue
        out.append(SourceCandidate(url=url, title=title))
        seen.add(url)
        if len(out) >= max_count:
            break
    return out


def _record_cached_sources(word: str, domains: list[str], clips: list[ClipResult]) -> None:
    if not clips:
        return
    pool = _load_source_pool()
    key = f"{_word_key(word)}::{_domain_key(domains)}"
    rows = list(pool.get(key) or [])
    by_url: dict[str, dict[str, object]] = {}
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        by_url[url] = {
            "url": url,
            "title": str(row.get("title") or "Untitled"),
            "score": int(row.get("score") or 1),
            "last_ok": float(row.get("last_ok") or 0.0),
        }
    now = time.time()
    for clip in clips:
        row = by_url.get(clip.source_url)
        if row is None:
            row = {"url": clip.source_url, "title": clip.source_title or "Untitled", "score": 1, "last_ok": now}
            by_url[clip.source_url] = row
        else:
            row["title"] = clip.source_title or str(row.get("title") or "Untitled")
            row["score"] = int(row.get("score") or 1) + 1
            row["last_ok"] = now
    saved_rows = sorted(
        by_url.values(),
        key=lambda item: (int(item.get("score") or 0), float(item.get("last_ok") or 0.0)),
        reverse=True,
    )[:120]
    pool[key] = saved_rows
    _save_source_pool(pool)


def _preferred_binary(name: str) -> str:
    if name == "ffmpeg" and Path("/usr/bin/ffmpeg").exists():
        return "/usr/bin/ffmpeg"
    if name == "ffprobe" and Path("/usr/bin/ffprobe").exists():
        return "/usr/bin/ffprobe"
    return name


def _run_command(args: list[str], *, timeout: int = 240) -> None:
    cmd_args = list(args)
    if cmd_args and cmd_args[0] in {"ffmpeg", "ffprobe"}:
        cmd_args[0] = _preferred_binary(cmd_args[0])
    result = subprocess.run(
        cmd_args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        source = stderr if stderr else stdout
        lines = source.splitlines()
        tail = "\n".join(lines[-80:]) if lines else ""
        cmd = " ".join(cmd_args)
        detail = f"Command failed ({result.returncode}): {cmd}\n{tail}".strip()
        raise RuntimeError(detail[:5000])


def _must_have_binary(name: str) -> None:
    candidate = _preferred_binary(name)
    if shutil.which(candidate):
        return
    raise RuntimeError(f"Required binary not found: {name}")


def _load_ffmpeg_encoders() -> set[str]:
    global _ENCODER_CACHE
    with _ENCODER_CACHE_LOCK:
        if _ENCODER_CACHE is not None:
            return _ENCODER_CACHE

        result = subprocess.run(
            [_preferred_binary("ffmpeg"), "-hide_banner", "-encoders"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        found: set[str] = set()
        for line in (result.stdout or "").splitlines():
            token = line.strip()
            if not token or token.startswith("--") or token.startswith("Encoders"):
                continue
            parts = token.split()
            if len(parts) >= 2 and parts[0].startswith("V"):
                found.add(parts[1])
        _ENCODER_CACHE = found
        return found


def _video_encode_args() -> list[str]:
    encoders = _load_ffmpeg_encoders()
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    # Some conda ffmpeg builds advertise libopenh264 but fail at runtime
    # with "Incorrect library version loaded", so skip it for reliability.
    return ["-c:v", "mpeg4", "-q:v", "5"]


def _supports_subtitles_filter() -> bool:
    result = subprocess.run(
        [_preferred_binary("ffmpeg"), "-hide_banner", "-filters"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    text = (result.stdout or "").lower()
    return " subtitles " in text or "\nsubtitles" in text


def _probe_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        [
            _preferred_binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    raw = (result.stdout or "").strip().splitlines()
    if not raw:
        return 0.0
    try:
        return float(raw[-1].strip())
    except Exception:
        return 0.0


def _parse_vtt_timestamp(raw: str) -> float:
    token = raw.strip().replace(",", ".")
    parts = token.split(":")
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    elif len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    else:
        raise ValueError(f"invalid timestamp: {raw}")
    return (hours * 3600) + (minutes * 60) + seconds


def _strip_vtt_markup(text: str) -> str:
    out = re.sub(r"<[^>]+>", "", text)
    out = out.replace("&nbsp;", " ").replace("&amp;", "&")
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _parse_vtt_file(path: Path) -> list[Cue]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    cues: list[Cue] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue

        left, right = [part.strip() for part in line.split("-->", 1)]
        end_token = right.split(" ", 1)[0].strip()
        try:
            start_sec = _parse_vtt_timestamp(left)
            end_sec = _parse_vtt_timestamp(end_token)
        except Exception:
            i += 1
            continue

        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        text = _strip_vtt_markup(" ".join(text_lines))
        if text:
            cues.append(Cue(start=start_sec, end=end_sec, text=text))
        i += 1
    return cues


def _srt_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000.0))
    hours = ms // 3600000
    ms -= hours * 3600000
    minutes = ms // 60000
    ms -= minutes * 60000
    sec = ms // 1000
    ms -= sec * 1000
    return f"{hours:02d}:{minutes:02d}:{sec:02d},{ms:03d}"


def _pick_sentence(cues: list[Cue], index: int) -> tuple[float, float, str]:
    cue = cues[index]
    start = cue.start
    end = cue.end
    sentence = cue.text

    cursor = index + 1
    while cursor < len(cues) and len(sentence) < 140 and not re.search(r"[.!?]$", sentence):
        sentence = f"{sentence} {cues[cursor].text}".strip()
        end = cues[cursor].end
        if re.search(r"[.!?]$", cues[cursor].text):
            break
        cursor += 1

    sentence = re.sub(r"\s+", " ", sentence).strip()
    return start, end, sentence


def _escape_subtitle_filter_path(path: Path) -> str:
    # ffmpeg subtitles filter expects escaped ":" and "'" in path strings.
    token = str(path).replace("\\", "/")
    token = token.replace(":", "\\:")
    token = token.replace("'", "\\'")
    return token


def _pick_drawtext_font() -> Path | None:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    ]
    for item in candidates:
        if item.exists():
            return item
    return None


def _title_fallback_candidate(title: str, word: str) -> bool:
    if _title_contains_keyword(title, word):
        return True
    variants = _keyword_variants(word)
    if not variants:
        return False
    tokens = re.findall(r"[a-z']+", title.lower())
    for token in tokens:
        clean = re.sub(r"[^a-z]+", "", token)
        if len(clean) < 3:
            continue
        for base in variants:
            ratio = SequenceMatcher(None, clean, base).ratio()
            if ratio >= 0.82 or (len(base) >= 6 and ratio >= 0.74):
                return True
    return False


def _subtitle_file_rank(path: Path) -> tuple[int, int]:
    name = path.name.lower()
    # Prefer original English subtitles over translated tracks.
    if name.endswith('.en-orig.vtt'):
        return (0, 0)
    if name.endswith('.en-us.vtt'):
        return (1, 0)
    if name.endswith('.en-gb.vtt'):
        return (2, 0)
    if name.endswith('.en.vtt'):
        return (3, 0)
    if '.en-' in name and name.endswith('.vtt'):
        return (4, 0)
    return (9, 0)


def _asr_keyword_window_starts(video_path: Path, word: str, *, max_count: int = 2) -> list[float]:
    key = f"{str(video_path.resolve())}::{word.lower()}"
    cached = _ASR_KEYWORD_CACHE.get(key)
    if cached is not None:
        return list(cached[:max_count])

    with _WHISPER_CHECK_LOCK:
        cached = _ASR_KEYWORD_CACHE.get(key)
        if cached is not None:
            return list(cached[:max_count])

        global _WHISPER_MODEL, _WHISPER_AVAILABLE
        if _WHISPER_AVAILABLE is False:
            _ASR_KEYWORD_CACHE[key] = []
            return []

        if _WHISPER_MODEL is None:
            try:
                import whisper  # type: ignore
                _WHISPER_MODEL = whisper.load_model("tiny.en")
                _WHISPER_AVAILABLE = True
            except Exception:
                _WHISPER_AVAILABLE = False
                _ASR_KEYWORD_CACHE[key] = []
                return []

        variants = _keyword_variants(word)
        if not variants:
            _ASR_KEYWORD_CACHE[key] = []
            return []

        patterns = [
            re.compile(rf"\b{re.escape(item)}(?:s|es|ed|ing)?\b", re.IGNORECASE)
            for item in variants
        ]

        def fuzzy_hit(text_value: str) -> bool:
            tokens = re.findall(r"[a-z']+", text_value.lower())
            for token in tokens:
                clean = re.sub(r"[^a-z]+", "", token)
                if len(clean) < 3:
                    continue
                for base in variants:
                    ratio = SequenceMatcher(None, clean, base).ratio()
                    if ratio >= 0.84 or (len(base) >= 6 and ratio >= 0.74):
                        return True
            return False

        duration = _probe_duration_seconds(video_path)
        scan_limit = 480.0
        if duration > 0:
            scan_limit = min(scan_limit, max(120.0, duration))
        chunk_starts: list[float] = []
        cursor = 0.0
        while cursor < scan_limit:
            chunk_starts.append(cursor)
            cursor += 120.0

        starts: list[float] = []
        for chunk_start in chunk_starts:
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                _run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        f"{chunk_start:.3f}",
                        "-i",
                        str(video_path),
                        "-t",
                        "120",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(tmp_path),
                    ],
                    timeout=150,
                )

                result = _WHISPER_MODEL.transcribe(str(tmp_path), language="en", task="transcribe", fp16=False)
                segments = (result or {}).get("segments") or []
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    seg_text = str(seg.get("text") or "")
                    if not seg_text.strip():
                        continue
                    hit = any(pattern.search(seg_text) for pattern in patterns)
                    if not hit:
                        hit = fuzzy_hit(seg_text)
                    if not hit:
                        continue
                    try:
                        local_start = float(seg.get("start", 0.0))
                    except Exception:
                        local_start = 0.0
                    start_sec = max(0.0, chunk_start + local_start - 0.6)
                    if starts and abs(start_sec - starts[-1]) < 1.0:
                        continue
                    starts.append(start_sec)
                    if len(starts) >= max(max_count, 2):
                        break
                if len(starts) >= max(max_count, 2):
                    break
            except Exception:
                continue
            finally:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass

        _ASR_KEYWORD_CACHE[key] = starts
        return list(starts[:max_count])


def _keyword_variants(word: str) -> list[str]:
    token = re.sub(r"[^a-zA-Z'-]+", "", (word or "").strip().lower())
    if not token:
        return []

    variants: set[str] = {token}
    if token.endswith("ies") and len(token) > 4:
        variants.add(token[:-3] + "y")
    if token.endswith("ves") and len(token) > 4:
        variants.add(token[:-3] + "f")
    if token.endswith("es") and len(token) > 4:
        variants.add(token[:-2])
    if token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    if token.endswith("ed") and len(token) > 4:
        variants.add(token[:-2])
    if token.endswith("ing") and len(token) > 5:
        variants.add(token[:-3])

    cleaned = [item for item in variants if len(item) >= 3]
    cleaned.sort(key=len, reverse=True)
    return cleaned


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _candidate_title_score(title: str, *, terms: list[str], domain_terms: list[str]) -> int:
    text = _norm_text(title)
    if not text:
        return 0
    score = 0

    for term in terms:
        if re.search(rf"\b{re.escape(term.lower())}\b", text):
            score += 8

    pronunciation_tokens = [
        "pronunciation",
        "how to pronounce",
        "american english",
        "british english",
        "phonics",
        "ipa",
        "word stress",
    ]
    for token in pronunciation_tokens:
        if token in text:
            score += 5

    for token in domain_terms:
        if token and token in text:
            score += 1

    # Prefer high-quality talk/lesson sources in fallback mode.
    quality_tokens = [
        "ted",
        "tedx",
        "ted-ed",
        "bbc learning english",
        "voa learning english",
        "english with lucy",
    ]
    for token in quality_tokens:
        if token in text:
            score += 3

    if "shorts" in text:
        score -= 2
    return score


def _source_priority_score(source: SourceCandidate, word: str) -> int:
    text = _norm_text(source.title)
    score = 0
    if "ted-ed" in text:
        score += 12
    if "tedx" in text:
        score += 10
    if re.search(r"\bted\b", text):
        score += 8
    if "shorts" in text:
        score -= 8
    if _title_fallback_candidate(source.title, word):
        score += 10
    return score


def _http_get_text(url: str, *, timeout: float = 20.0) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def _http_get_json(url: str, *, timeout: float = 20.0) -> dict[str, object]:
    payload = _http_get_text(url, timeout=timeout)
    try:
        data = json.loads(payload)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON response from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON payload from {url}")
    return data


def _http_get_binary(url: str, *, timeout: float = 20.0) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_time_to_seconds(raw: str | None) -> float | None:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        return float(token)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", token)
    if m:
        h = int(m.group(1) or 0)
        mnt = int(m.group(2) or 0)
        sec = int(m.group(3) or 0)
        total = h * 3600 + mnt * 60 + sec
        return float(total) if total > 0 else None
    return None


def _extract_youglish_youtube_pairs(html_text: str) -> list[tuple[str, float | None]]:
    text = unescape(html_text or "")
    pairs: list[tuple[str, float | None]] = []

    watch_pat = re.compile(
        r"https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{11})([^\"'\s<]*)",
        re.IGNORECASE,
    )
    for m in watch_pat.finditer(text):
        video_id = m.group(1)
        extra = m.group(2) or ""
        query = extra.lstrip("&?")
        qs = parse_qs(query) if query else {}
        start = _parse_time_to_seconds((qs.get("t") or qs.get("start") or [None])[0])
        pairs.append((video_id, start))

    short_pat = re.compile(
        r"https?://youtu\.be/([A-Za-z0-9_-]{11})(?:\?([^\"'\s<]*))?",
        re.IGNORECASE,
    )
    for m in short_pat.finditer(text):
        video_id = m.group(1)
        query = (m.group(2) or "").strip()
        qs = parse_qs(query) if query else {}
        start = _parse_time_to_seconds((qs.get("t") or qs.get("start") or [None])[0])
        pairs.append((video_id, start))

    embed_pat = re.compile(
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})(?:\?([^\"'\s<]*))?",
        re.IGNORECASE,
    )
    for m in embed_pat.finditer(text):
        video_id = m.group(1)
        query = (m.group(2) or "").strip()
        qs = parse_qs(query) if query else {}
        start = _parse_time_to_seconds((qs.get("start") or qs.get("t") or [None])[0])
        pairs.append((video_id, start))

    # Fallback for JSON-style payload fragments.
    json_id_pat = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"')
    for m in json_id_pat.finditer(text):
        video_id = m.group(1)
        window = text[m.end() : m.end() + 220]
        start = None
        m_start = re.search(r'"start(?:Seconds)?"\s*:\s*([0-9]+(?:\.[0-9]+)?)', window)
        if m_start:
            try:
                start = float(m_start.group(1))
            except Exception:
                start = None
        pairs.append((video_id, start))

    out: list[tuple[str, float | None]] = []
    seen: set[str] = set()
    for video_id, start in pairs:
        bucket = int(round(start or 0.0))
        key = f"{video_id}:{bucket}"
        if key in seen:
            continue
        seen.add(key)
        out.append((video_id, start))
    return out


def _resolve_youtube_data_api_key() -> str:
    for key in ("YOUTUBE_API_KEY", "GOOGLE_API_KEY", "GOOGLE_YOUTUBE_API_KEY"):
        token = str(os.getenv(key) or "").strip()
        if token:
            return token
    return ""


def _search_youtube_api_sources(word: str, domains: list[str], max_videos: int) -> list[SourceCandidate]:
    api_key = _resolve_youtube_data_api_key()
    if not api_key:
        return []

    terms = _keyword_variants(word)
    if not terms:
        terms = [str(word or "").strip()]
    terms = [item for item in terms if item]
    if not terms:
        return []

    hint_parts: list[str] = []
    for domain in domains:
        hint = DOMAIN_QUERY_HINTS.get(domain)
        if hint and hint not in hint_parts:
            hint_parts.append(hint)
    if not hint_parts:
        hint_parts.append(DOMAIN_QUERY_HINTS["education"])

    cache_key = (
        "ytapi::"
        + "|".join(sorted([d for d in domains if d]))
        + "::"
        + "|".join(terms[:4])
        + f"::{int(max_videos)}"
    )
    now = time.time()
    with _SOURCE_CACHE_LOCK:
        cached = _SOURCE_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _SOURCE_CACHE_TTL_SECONDS:
            return list(cached[1])

    query_specs: list[tuple[str, int]] = []
    for term in terms[:3]:
        query_specs.append((f"\"{term}\" pronunciation english", 12))
        query_specs.append((f"\"{term}\" in sentence english subtitles", 11))
        query_specs.append((f"how to pronounce {term}", 10))
        for hint in hint_parts[:2]:
            query_specs.append((f"\"{term}\" {hint}", 9))
    query_specs.append((f"{terms[0]} english pronunciation lesson", 8))

    seen_queries: set[str] = set()
    query_variants: list[tuple[str, int]] = []
    for query, weight in query_specs:
        key = query.strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        query_variants.append((query, weight))
    # Keep quota usage predictable: each call costs 100 units.
    if len(query_variants) > 3:
        query_variants = query_variants[:3]

    domain_terms: list[str] = []
    for hint in hint_parts:
        for token in re.split(r"\s+", hint.lower()):
            if len(token) >= 4 and token not in domain_terms:
                domain_terms.append(token)

    max_results = max(8, min(25, max_videos * 3))
    scored: dict[str, tuple[int, SourceCandidate]] = {}
    for query, query_weight in query_variants:
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": str(max_results),
            "q": query,
            "relevanceLanguage": "en",
            "videoEmbeddable": "true",
            "safeSearch": "none",
            "key": api_key,
        }
        url = "https://www.googleapis.com/youtube/v3/search?" + urlencode(params)
        try:
            payload = _http_get_json(url, timeout=18.0)
        except Exception:
            time.sleep(_SEARCH_QUERY_DELAY_SECONDS)
            continue

        rows = payload.get("items")
        if not isinstance(rows, list):
            time.sleep(_SEARCH_QUERY_DELAY_SECONDS)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            id_obj = row.get("id")
            snippet = row.get("snippet")
            if not isinstance(id_obj, dict) or not isinstance(snippet, dict):
                continue
            video_id = str(id_obj.get("videoId") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                continue
            title = str(snippet.get("title") or "").strip() or "Untitled"
            entry_url = f"https://www.youtube.com/watch?v={video_id}"
            base_score = _candidate_title_score(title, terms=terms, domain_terms=domain_terms)
            score = query_weight + base_score
            candidate = SourceCandidate(
                url=entry_url,
                title=title,
                source_type="youtube_api",
            )
            existing = scored.get(entry_url)
            if existing is None or score > existing[0]:
                scored[entry_url] = (score, candidate)

        if len(scored) >= max(max_videos * 8, 40):
            break
        time.sleep(_SEARCH_QUERY_DELAY_SECONDS)

    ranked = sorted(scored.values(), key=lambda item: item[0], reverse=True)
    result = [item[1] for item in ranked[: max(max_videos * 8, 40)]]
    with _SOURCE_CACHE_LOCK:
        _SOURCE_CACHE[cache_key] = (time.time(), list(result))
    return result


def _search_youglish_sources(word: str, *, max_videos: int) -> list[SourceCandidate]:
    normalized = re.sub(r"[^a-zA-Z'-]+", "", (word or "").strip())
    if not normalized:
        return []

    cache_key = f"youglish::{normalized.lower()}::{int(max_videos)}"
    now = time.time()
    with _SOURCE_CACHE_LOCK:
        cached = _SOURCE_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _SOURCE_CACHE_TTL_SECONDS:
            return list(cached[1])

    pool_size = max(max_videos * 12, 40)
    pages = [
        f"https://youglish.com/pronounce/{quote(normalized)}/english",
        f"https://youglish.com/pronounce/{quote(normalized)}/english?",
    ]
    candidates: list[SourceCandidate] = []
    seen_urls: set[str] = set()
    for page_url in pages:
        try:
            html_text = _http_get_text(page_url, timeout=20.0)
        except Exception:
            continue
        pairs = _extract_youglish_youtube_pairs(html_text)
        if not pairs:
            continue
        for idx, (video_id, start) in enumerate(pairs, start=1):
            base_url = f"https://www.youtube.com/watch?v={video_id}"
            if start is not None and start > 0:
                final_url = f"{base_url}&t={int(round(start))}s"
            else:
                final_url = base_url
            if final_url in seen_urls:
                continue
            seen_urls.add(final_url)
            candidates.append(
                SourceCandidate(
                    url=final_url,
                    title=f"YouGlish result #{idx}",
                    start_seconds=start,
                    source_type="youglish",
                )
            )
            if len(candidates) >= pool_size:
                break
        if len(candidates) >= pool_size:
            break

    with _SOURCE_CACHE_LOCK:
        _SOURCE_CACHE[cache_key] = (time.time(), list(candidates))
    return candidates


def _extract_cambridge_audio_urls(word: str, *, max_items: int = 2) -> list[tuple[str, str]]:
    normalized = re.sub(r"[^a-zA-Z'-]+", "", (word or "").strip().lower())
    if not normalized:
        return []
    page_url = f"https://dictionary.cambridge.org/dictionary/english/{quote(normalized)}"
    try:
        html_text = _http_get_text(page_url, timeout=18.0)
    except Exception:
        return []

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    mp3_pat = re.compile(r'data-src-mp3="([^"]+\.mp3[^"]*)"')
    for m in mp3_pat.finditer(html_text):
        raw_url = unescape(m.group(1).strip())
        if raw_url.startswith("//"):
            audio_url = "https:" + raw_url
        elif raw_url.startswith("/"):
            audio_url = "https://dictionary.cambridge.org" + raw_url
        else:
            audio_url = raw_url
        if not audio_url.startswith("http"):
            continue
        if audio_url in seen:
            continue
        seen.add(audio_url)
        window_left = max(0, m.start() - 180)
        window = html_text[window_left : m.start() + 120].lower()
        if "/us_pron/" in audio_url.lower() or " us dpron-i" in window:
            label = "US"
        elif "/uk_pron/" in audio_url.lower() or " uk dpron-i" in window:
            label = "UK"
        else:
            label = "EN"
        rows.append((label, audio_url))
        if len(rows) >= max_items:
            break
    return rows


def _build_cambridge_demo_clips(
    *,
    word: str,
    clips_dir: Path,
    clip_start_index: int,
    max_items: int = 1,
) -> list[ClipResult]:
    demos = _extract_cambridge_audio_urls(word, max_items=max_items)
    if not demos:
        return []
    output: list[ClipResult] = []
    font_path = _pick_drawtext_font()
    clip_index = max(1, int(clip_start_index))

    for label, audio_url in demos:
        audio_path = clips_dir / f"cambridge_{clip_index:03d}_{label.lower()}.mp3"
        try:
            audio_path.write_bytes(_http_get_binary(audio_url, timeout=18.0))
        except Exception:
            continue
        if not audio_path.exists() or audio_path.stat().st_size < 2048:
            continue

        duration = _probe_duration_seconds(audio_path)
        duration = max(1.8, min(5.2, duration if duration > 0 else 3.8))
        video_path = clips_dir / f"clip_{clip_index:03d}.mp4"

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=#0b1220:s=1280x720:d={duration:.3f}",
            "-i",
            str(audio_path),
            "-shortest",
        ]
        if font_path is not None:
            text_file = clips_dir / f"cambridge_{clip_index:03d}.txt"
            text_file.write_text(f"{word} ({label}) - Cambridge", encoding="utf-8")
            drawtext_filter = (
                f"drawtext=fontfile='{_escape_subtitle_filter_path(font_path)}':"
                f"textfile='{_escape_subtitle_filter_path(text_file)}':"
                "fontcolor=white:fontsize=46:borderw=2:bordercolor=black:"
                "x=(w-text_w)/2:y=(h-text_h)/2"
            )
            cmd.extend(["-vf", drawtext_filter])
        cmd.extend([*_video_encode_args(), "-c:a", "aac", "-movflags", "+faststart", str(video_path)])
        try:
            _run_command(cmd, timeout=180)
        except Exception:
            continue

        output.append(
            ClipResult(
                clip_path=video_path,
                subtitle_text=f"{word} ({label})",
                source_title=f"Cambridge {label}",
                source_url=audio_url,
            )
        )
        clip_index += 1
    return output


def _resolve_cookie_file() -> Path | None:
    cookie_env = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookie_candidates = []
    if cookie_env:
        cookie_candidates.append(Path(cookie_env))
    cookie_candidates.append(Path("data/yt_cookies.txt"))
    for cookie_file in cookie_candidates:
        if cookie_file.exists() and cookie_file.is_file():
            return cookie_file
    return None


def _parse_po_token_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (raw or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            key, value = "android.gvs", item
        k = key.strip().lower()
        v = value.strip()
        if not k or not v:
            continue
        out[k] = v
    return out


def _normalize_po_token_key(key: str) -> str:
    token = str(key or "").strip().lower()
    if not token:
        return ""
    aliases = {
        "android": "android.gvs",
        "web": "web.gvs",
        "mweb": "mweb.gvs",
        "ios": "ios.gvs",
        "tv": "tv.gvs",
    }
    return aliases.get(token, token)


def _resolve_po_tokens() -> dict[str, str]:
    global _PO_TOKEN_CACHE
    now = time.time()
    with _PO_TOKEN_LOCK:
        if _PO_TOKEN_CACHE and (now - _PO_TOKEN_CACHE[0]) < _PO_TOKEN_CACHE_TTL_SECONDS:
            return dict(_PO_TOKEN_CACHE[1])

        tokens: dict[str, str] = {}
        # Combined inline format:
        # YTDLP_PO_TOKENS="android.gvs=xxx,web.gvs=yyy"
        combined = os.getenv("YTDLP_PO_TOKENS", "").strip()
        if combined:
            tokens.update(_parse_po_token_map(combined))

        # Single-token shortcuts.
        single_android = os.getenv("YTDLP_PO_TOKEN_ANDROID_GVS", "").strip()
        if single_android:
            tokens["android.gvs"] = single_android
        single_web = os.getenv("YTDLP_PO_TOKEN_WEB_GVS", "").strip()
        if single_web:
            tokens["web.gvs"] = single_web
        single_mweb = os.getenv("YTDLP_PO_TOKEN_MWEB_GVS", "").strip()
        if single_mweb:
            tokens["mweb.gvs"] = single_mweb

        # File format supports JSON map or KEY=VALUE text lines.
        token_file = os.getenv("YTDLP_PO_TOKEN_FILE", "").strip()
        token_path = Path(token_file) if token_file else Path("data/yt_po_tokens.json")
        if token_path.exists() and token_path.is_file():
            file_text = token_path.read_text(encoding="utf-8", errors="ignore").strip()
            if file_text:
                loaded = False
                if file_text.startswith("{"):
                    try:
                        payload = json.loads(file_text)
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        for key, value in payload.items():
                            k = _normalize_po_token_key(str(key))
                            v = str(value or "").strip()
                            if k and v:
                                tokens[k] = v
                        loaded = True
                if not loaded:
                    for line in file_text.splitlines():
                        row = line.strip()
                        if not row or row.startswith("#"):
                            continue
                        parsed = _parse_po_token_map(row)
                        for key, value in parsed.items():
                            k = _normalize_po_token_key(key)
                            if k and value:
                                tokens[k] = value

        normalized: dict[str, str] = {}
        for key, value in tokens.items():
            k = _normalize_po_token_key(key)
            v = str(value or "").strip()
            if not k or not v:
                continue
            normalized[k] = v

        _PO_TOKEN_CACHE = (now, normalized)
        return dict(normalized)


def _po_token_args_for_client(player_client: str | None) -> list[str]:
    token_map = _resolve_po_tokens()
    if not token_map:
        return []
    if player_client:
        prefixes = [f"{player_client.lower()}."]
    else:
        # Default web profile can use any known web-ish token.
        prefixes = ["web.", "mweb.", "tv.", "android."]

    out: list[str] = []
    for key, value in token_map.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            out.append(f"{key}+{value}")

    # Fallback: if specific client token is missing, use any known token instead
    # of silently sending no po_token argument.
    if not out:
        for key, value in token_map.items():
            out.append(f"{key}+{value}")
    return out


def _has_po_tokens() -> bool:
    return bool(_resolve_po_tokens())


def _supports_impersonate() -> bool:
    global _IMPERSONATE_AVAILABLE
    with _IMPERSONATE_CHECK_LOCK:
        if _IMPERSONATE_AVAILABLE is not None:
            return _IMPERSONATE_AVAILABLE

        # yt-dlp impersonation needs both curl_cffi and a compatible
        # yt-dlp runtime option parser. Some builds have curl_cffi installed
        # but still raise AssertionError when "impersonate" is passed.
        try:
            import curl_cffi  # noqa: F401
            import yt_dlp  # type: ignore
        except Exception:
            _IMPERSONATE_AVAILABLE = False
            return _IMPERSONATE_AVAILABLE

        try:
            with yt_dlp.YoutubeDL(
                {
                    "quiet": True,
                    "noprogress": True,
                    "skip_download": True,
                    "ignoreerrors": True,
                    "noplaylist": True,
                    "impersonate": "chrome",
                }
            ):
                pass
        except Exception:
            _IMPERSONATE_AVAILABLE = False
        else:
            _IMPERSONATE_AVAILABLE = True
        return _IMPERSONATE_AVAILABLE


def _apply_yt_dlp_common_options(
    options: dict[str, object],
    *,
    use_cookies: bool = True,
    use_node_runtime: bool = True,
    player_client: str | None = None,
    use_impersonate: bool = False,
    use_po_token: bool = True,
) -> dict[str, object]:
    if use_cookies:
        cookie_file = _resolve_cookie_file()
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
    else:
        options.pop("cookiefile", None)

    options["http_headers"] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }
    if use_node_runtime:
        options["js_runtimes"] = {"node": {}}
    else:
        options.pop("js_runtimes", None)

    yt_args: dict[str, list[str]] = {}
    if player_client:
        yt_args["player_client"] = [player_client]

    if use_po_token:
        po_args = _po_token_args_for_client(player_client)
        if po_args:
            yt_args["po_token"] = po_args

    if yt_args:
        options["extractor_args"] = {"youtube": yt_args}

    if use_impersonate and _supports_impersonate():
        options["impersonate"] = "chrome"

    return options


def _extract_search_entries(
    yt_dlp_module,
    *,
    options: dict[str, object],
    query: str,
    search_size: int,
) -> list[dict[str, object]]:
    attempts = [
        f"ytsearch{search_size}:{query}",
        f"https://www.youtube.com/results?search_query={quote_plus(query)}",
    ]
    for target in attempts:
        try:
            with yt_dlp_module.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception:
            continue
        entries = info.get("entries") or []
        if entries:
            return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _search_sources(word: str, domains: list[str], max_videos: int) -> list[SourceCandidate]:
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise RuntimeError("Missing dependency: yt-dlp (pip install yt-dlp)") from exc

    terms = _keyword_variants(word)
    if not terms:
        terms = [word]
    cache_key = "|".join(sorted(domains)) + "::" + "|".join(terms[:6]) + f"::{int(max_videos)}"
    now = time.time()
    with _SOURCE_CACHE_LOCK:
        cached = _SOURCE_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _SOURCE_CACHE_TTL_SECONDS:
            return list(cached[1])

    hint_parts: list[str] = []
    for domain in domains:
        hint = DOMAIN_QUERY_HINTS.get(domain)
        if hint and hint not in hint_parts:
            hint_parts.append(hint)
    if not hint_parts:
        hint_parts.append(DOMAIN_QUERY_HINTS["education"])
    pronunciation_focus = "phoneme_demo" in domains
    # Keep a larger candidate pool so multi-domain requests do not collapse
    # into only a few sources.
    search_size = max(max_videos * max(8, len(hint_parts) * 4), max_videos + 12)
    if pronunciation_focus:
        search_size = max(search_size, max_videos * 12)

    query_specs: list[tuple[str, int]] = []
    pron_templates = [
        "{term} pronunciation american english",
        "{term} pronunciation british english",
        "how to pronounce {term}",
        "{term} word stress pronunciation",
        "{term} phonics example sentence",
    ]
    for term in terms[:5]:
        query_specs.append((f"\"{term}\" TED talk english subtitles", 13))
        query_specs.append((f"\"{term}\" TED-Ed", 13))
        query_specs.append((f"\"{term}\" TEDx talk", 12))
        for hint in hint_parts:
            query_specs.append((f"\"{term}\" english subtitles {hint}", 10))
            query_specs.append((f"{term} in sentence english subtitles {hint}", 9))
        for template in pron_templates:
            query_specs.append((template.format(term=term), 7))
        query_specs.append((f"\"{term}\" english lesson", 6))
        if pronunciation_focus:
            query_specs.extend(
                [
                    (f"{term} pronunciation explained", 11),
                    (f"{term} ipa pronunciation", 11),
                    (f"{term} mouth position pronunciation", 11),
                    (f"{term} minimal pairs pronunciation", 10),
                    (f"{term} common pronunciation mistakes", 10),
                ]
            )

    joined_terms = " ".join(terms[:3])
    query_specs.extend(
        [
            (f"\"{joined_terms}\" english subtitles {' '.join(hint_parts)}", 8),
            (f"{joined_terms} in sentence english subtitles", 8),
            (f"{joined_terms} pronunciation example", 7),
            (f"{joined_terms} english", 6),
        ]
    )

    query_variants: list[tuple[str, int]] = []
    seen_queries: set[str] = set()
    for query, weight in query_specs:
        key = query.strip().lower()
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        query_variants.append((query, weight))
    if len(query_variants) > 36:
        query_variants = query_variants[:36]

    domain_terms: list[str] = []
    for hint in hint_parts:
        for token in re.split(r"\s+", hint.lower()):
            if len(token) >= 4 and token not in domain_terms:
                domain_terms.append(token)

    base_options = {
        "quiet": True,
        "noprogress": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "playlistend": search_size,
    }
    scored: dict[str, tuple[int, SourceCandidate]] = {}
    search_profiles = [
        {"use_cookies": True, "use_node_runtime": True, "player_client": None, "use_impersonate": False},
        {"use_cookies": True, "use_node_runtime": True, "player_client": None, "use_impersonate": True},
        {"use_cookies": False, "use_node_runtime": False, "player_client": None, "use_impersonate": True},
        {"use_cookies": False, "use_node_runtime": False, "player_client": "android", "use_impersonate": False},
    ]
    profile_failures = [0 for _ in search_profiles]

    for query, query_weight in query_variants:
        entries: list[dict[str, object]] = []
        for profile_index, profile in enumerate(search_profiles):
            if profile_index == 0 and profile_failures[profile_index] >= 5:
                continue
            options = _apply_yt_dlp_common_options(dict(base_options), **profile)
            entries = _extract_search_entries(
                yt_dlp,
                options=options,
                query=query,
                search_size=search_size,
            )
            if entries:
                profile_failures[profile_index] = 0
                break
            profile_failures[profile_index] += 1
            time.sleep(_SEARCH_QUERY_DELAY_SECONDS)
        if not entries:
            time.sleep(_SEARCH_QUERY_DELAY_SECONDS)
            continue

        for entry in entries:
            if not entry:
                continue
            duration = entry.get("duration")
            if duration is not None:
                try:
                    seconds = int(duration)
                    if seconds < 8 or seconds > 900:
                        continue
                except Exception:
                    pass
            title = str(entry.get("title") or "").strip() or "Untitled"
            entry_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
            if not entry_url:
                video_id = str(entry.get("id") or "").strip()
                if video_id:
                    entry_url = f"https://www.youtube.com/watch?v={video_id}"
            if not entry_url:
                continue
            base_score = _candidate_title_score(title, terms=terms, domain_terms=domain_terms)
            score = query_weight + base_score
            existing = scored.get(entry_url)
            candidate = SourceCandidate(url=entry_url, title=title)
            if existing is None or score > existing[0]:
                scored[entry_url] = (score, candidate)

        if len(scored) >= search_size:
            break
        time.sleep(_SEARCH_QUERY_DELAY_SECONDS)

    ranked = sorted(scored.values(), key=lambda item: item[0], reverse=True)
    result = [item[1] for item in ranked[:search_size]]
    with _SOURCE_CACHE_LOCK:
        _SOURCE_CACHE[cache_key] = (time.time(), list(result))
    return result


def _download_video_and_vtt(source: SourceCandidate, target_dir: Path, slot: int) -> tuple[Path, Path | None] | None:
    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise RuntimeError("Missing dependency: yt-dlp (pip install yt-dlp)") from exc

    prefix = f"{slot:02d}"
    template = str(target_dir / f"{prefix}_%(id)s.%(ext)s")
    base_options = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 0,
        "fragment_retries": 0,
        "socket_timeout": 16,
        "format": (
            "bv*[vcodec^=avc1][height<=480]+ba[ext=m4a]/"
            "bv*[vcodec!*=av01][height<=480]+ba/"
            "b[ext=mp4][height<=480]/b[height<=480]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": template,
    }
    blocked_flag = target_dir / "_youtube_blocked.flag"
    # Always try web profiles for every source. A single blocked source should
    # not disable subtitle-capable profiles for the rest of the job.
    download_profiles: list[dict[str, object]] = [
        {
            "use_cookies": True,
            "use_node_runtime": True,
            "player_client": None,
            "use_impersonate": True,
            "with_subtitles": True,
            "format": base_options["format"],
        },
        {
            "use_cookies": False,
            "use_node_runtime": True,
            "player_client": None,
            "use_impersonate": True,
            "with_subtitles": True,
            "format": base_options["format"],
        },
        {
            "use_cookies": True,
            "use_node_runtime": False,
            "player_client": "android",
            "use_impersonate": False,
            "with_subtitles": False,
            "format": "18/b[ext=mp4][height<=480]/b[height<=480]/best",
        },
        {
            "use_cookies": False,
            "use_node_runtime": False,
            "player_client": "android",
            "use_impersonate": False,
            "with_subtitles": False,
            "format": "18/b[ext=mp4][height<=480]/b[height<=480]/best",
        },
    ]

    bot_blocked_seen = False
    for profile in download_profiles:
        options = dict(base_options)
        options["format"] = profile["format"]
        if profile["with_subtitles"]:
            options["writesubtitles"] = True
            options["writeautomaticsub"] = True
            options["subtitleslangs"] = ["en", "en-US", "en-GB", "en.*"]
            options["subtitlesformat"] = "vtt"
        options = _apply_yt_dlp_common_options(
            options,
            use_cookies=bool(profile["use_cookies"]),
            use_node_runtime=bool(profile["use_node_runtime"]),
            player_client=str(profile["player_client"]) if profile["player_client"] else None,
            use_impersonate=bool(profile["use_impersonate"]),
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([source.url])
        except Exception as exc:
            msg = str(exc).lower()
            if (
                "not a bot" in msg
                or "sign in to confirm" in msg
                or "n challenge" in msg
                or "cookies are no longer valid" in msg
            ):
                bot_blocked_seen = True
            time.sleep(_DOWNLOAD_ATTEMPT_DELAY_SECONDS)
            continue

        videos = sorted(
            [
                item
                for item in target_dir.glob(f"{prefix}_*")
                if item.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"}
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        subtitles = sorted(
            target_dir.glob(f"{prefix}_*.vtt"),
            key=lambda p: (_subtitle_file_rank(p), -p.stat().st_mtime),
        )
        if videos:
            return videos[0], (subtitles[0] if subtitles else None)
        time.sleep(_DOWNLOAD_ATTEMPT_DELAY_SECONDS)

    if bot_blocked_seen:
        blocked_flag.write_text("1", encoding="utf-8")
    return None


def _find_match_indexes(cues: list[Cue], word: str, *, max_count: int) -> list[int]:
    if not cues or max_count <= 0:
        return []

    variants = _keyword_variants(word)
    if not variants:
        return []

    patterns = [
        re.compile(rf"\b{re.escape(item)}(?:s|es|ed|ing)?\b", re.IGNORECASE)
        for item in variants
    ]
    plain_words = [re.sub(r"[^a-z]+", "", item.lower()) for item in variants]
    picked: list[int] = []
    seen_sentence_keys: set[str] = set()

    # Auto subtitles frequently misspell target words (e.g., sausage/sossage).
    # Use a light fuzzy pass on neighboring cue text to recover these cases.
    def fuzzy_match(text: str) -> bool:
        tokens = re.findall(r"[a-z']+", text.lower())
        for token in tokens:
            clean = re.sub(r"[^a-z]+", "", token)
            if len(clean) < 3:
                continue
            for base in plain_words:
                if not base:
                    continue
                ratio = SequenceMatcher(None, clean, base).ratio()
                if ratio >= 0.84 or (len(base) >= 6 and ratio >= 0.74):
                    return True
        return False

    for idx, cue in enumerate(cues):
        window_parts = [cue.text]
        if idx > 0:
            window_parts.append(cues[idx - 1].text)
        if idx + 1 < len(cues):
            window_parts.append(cues[idx + 1].text)
        window_text = " ".join(window_parts)

        matched = any(pattern.search(cue.text) or pattern.search(window_text) for pattern in patterns)
        if not matched:
            cue_norm = re.sub(r"[^a-z]+", "", window_text.lower())
            matched = any(item and item in cue_norm for item in plain_words)
        if not matched:
            matched = fuzzy_match(window_text)
        if not matched:
            continue

        if picked and cue.start - cues[picked[-1]].start < 1.2:
            continue

        _, _, sentence = _pick_sentence(cues, idx)
        sentence_key = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
        if sentence_key and sentence_key in seen_sentence_keys:
            continue
        if sentence_key:
            seen_sentence_keys.add(sentence_key)

        picked.append(idx)
        if len(picked) >= max_count:
            break

    return picked


def _title_contains_keyword(title: str, word: str) -> bool:
    variants = _keyword_variants(word)
    if not variants:
        return False
    title_norm = _norm_text(title)
    return any(re.search(rf"\b{re.escape(item)}\b", title_norm) for item in variants)


def _extract_opening_clip(
    *,
    source: SourceCandidate,
    video_path: Path,
    clip_seconds: float,
    output_dir: Path,
    clip_index: int,
    caption: str,
    start_seconds: float = 0.4,
) -> ClipResult | None:
    segment_duration = max(4.5, min(8.0, clip_seconds))
    start = max(0.0, float(start_seconds))
    final_clip = output_dir / f"clip_{clip_index:03d}.mp4"
    raw_clip = output_dir / f"clip_{clip_index:03d}_raw.mp4"
    try:
        _run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(video_path),
                "-t",
                f"{segment_duration:.3f}",
                *_video_encode_args(),
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(raw_clip),
            ],
            timeout=300,
        )
    except RuntimeError:
        return None

    caption_text = caption.strip() or source.title
    caption_txt = output_dir / f"clip_{clip_index:03d}.txt"
    caption_txt.write_text(caption_text, encoding="utf-8")
    font_path = _pick_drawtext_font()
    if font_path is None:
        shutil.copy2(raw_clip, final_clip)
    else:
        drawtext_filter = (
            f"drawtext=fontfile='{_escape_subtitle_filter_path(font_path)}':"
            f"textfile='{_escape_subtitle_filter_path(caption_txt)}':"
            "fontcolor=white:fontsize=22:borderw=2:bordercolor=black:"
            "box=1:boxcolor=black@0.45:boxborderw=10:"
            "x=(w-text_w)/2:y=h-64"
        )
        try:
            _run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw_clip),
                    "-vf",
                    drawtext_filter,
                    *_video_encode_args(),
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(final_clip),
                ],
                timeout=300,
            )
        except RuntimeError:
            shutil.copy2(raw_clip, final_clip)

    return ClipResult(
        clip_path=final_clip,
        subtitle_text=caption_text,
        source_title=source.title,
        source_url=source.url,
    )


def _extract_keyword_clip(
    *,
    word: str,
    source: SourceCandidate,
    video_path: Path,
    vtt_path: Path,
    clip_seconds: float,
    output_dir: Path,
    clip_index: int,
    cues: list[Cue] | None = None,
    match_idx: int | None = None,
) -> ClipResult | None:
    cues = cues if cues is not None else _parse_vtt_file(vtt_path)
    if not cues:
        return None

    if match_idx is None:
        matches = _find_match_indexes(cues, word, max_count=1)
        if not matches:
            return None
        match_idx = matches[0]

    sentence_start, sentence_end, sentence = _pick_sentence(cues, match_idx)
    segment_duration = max(4.5, min(8.0, clip_seconds))
    start = max(0.0, sentence_start - 0.6)
    clip_end = max(start + segment_duration, sentence_end + 0.4)
    duration = clip_end - start

    raw_clip = output_dir / f"clip_{clip_index:03d}_raw.mp4"
    sub_file = output_dir / f"clip_{clip_index:03d}.srt"
    final_clip = output_dir / f"clip_{clip_index:03d}.mp4"

    _run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            *_video_encode_args(),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(raw_clip),
        ],
        timeout=300,
    )

    subtitle_start = 0.2
    subtitle_end = max(1.2, min(duration - 0.2, duration * 0.95))
    sub_file.write_text(
        "1\n"
        f"{_srt_timestamp(subtitle_start)} --> {_srt_timestamp(subtitle_end)}\n"
        f"{sentence}\n",
        encoding="utf-8",
    )

    if _supports_subtitles_filter():
        subtitle_filter = (
            f"subtitles={_escape_subtitle_filter_path(sub_file)}:"
            "force_style='FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF&,"
            "OutlineColour=&H00000000&,BorderStyle=3,Outline=1,Shadow=0,MarginV=20'"
        )
        _run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_clip),
                "-vf",
                subtitle_filter,
                *_video_encode_args(),
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(final_clip),
            ],
            timeout=300,
        )
    else:
        caption_txt = output_dir / f"clip_{clip_index:03d}.txt"
        caption_txt.write_text(sentence, encoding="utf-8")
        font_path = _pick_drawtext_font()
        if font_path is None:
            shutil.copy2(raw_clip, final_clip)
            return ClipResult(
                clip_path=final_clip,
                subtitle_text=sentence,
                source_title=source.title,
                source_url=source.url,
            )
        drawtext_filter = (
            f"drawtext=fontfile='{_escape_subtitle_filter_path(font_path)}':"
            f"textfile='{_escape_subtitle_filter_path(caption_txt)}':"
            "fontcolor=white:fontsize=22:borderw=2:bordercolor=black:"
            "box=1:boxcolor=black@0.45:boxborderw=10:"
            "x=(w-text_w)/2:y=h-64"
        )
        try:
            _run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw_clip),
                    "-vf",
                    drawtext_filter,
                    *_video_encode_args(),
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(final_clip),
                ],
                timeout=300,
            )
        except RuntimeError:
            shutil.copy2(raw_clip, final_clip)

    return ClipResult(
        clip_path=final_clip,
        subtitle_text=sentence,
        source_title=source.title,
        source_url=source.url,
    )


def _opening_clip_starts(video_path: Path, *, clip_seconds: float, max_count: int) -> list[float]:
    if max_count <= 0:
        return []
    segment = max(4.5, min(8.0, clip_seconds))
    duration = _probe_duration_seconds(video_path)
    if duration <= 0.0:
        return [0.4]

    usable = max(0.0, duration - 0.6)
    starts: list[float] = [0.4]
    if max_count == 1 or usable <= segment + 0.8:
        return starts

    step = max(segment * 0.9, 3.2)
    cursor = min(step, max(0.4, usable - segment))
    while len(starts) < max_count and cursor + 1.5 <= usable:
        starts.append(max(0.4, cursor))
        cursor += step
    return starts


def _concat_videos(clips: list[Path], output_path: Path) -> None:
    if not clips:
        raise RuntimeError("No clips available for concatenation.")
    if len(clips) == 1:
        shutil.copy2(clips[0], output_path)
        return

    list_file = output_path.parent / "concat_list.txt"
    rows = []
    for clip in clips:
        path_token = str(clip.resolve()).replace("\\", "/").replace("'", "'\\''")
        rows.append(f"file '{path_token}'")
    list_file.write_text("\n".join(rows) + "\n", encoding="utf-8")

    _run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            *_video_encode_args(),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        timeout=300,
    )


def compile_word_clip_package(
    *,
    word: str,
    domain: str,
    domains: list[str] | None = None,
    max_videos: int,
    clip_seconds: float,
    source_mode: str = "hybrid",
    include_cambridge: bool = False,
    job_dir: Path,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    _must_have_binary("ffmpeg")

    word_clean = re.sub(r"[^a-zA-Z'-]+", "", word).strip()
    if not word_clean:
        raise RuntimeError("Word is empty after normalization.")

    max_videos = max(1, min(int(max_videos), 30))
    clip_seconds = max(4.0, min(float(clip_seconds), 8.0))

    downloads_dir = job_dir / "downloads"
    clips_dir = job_dir / "clips"
    output_dir = job_dir / "output"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_mode = str(source_mode or "hybrid").strip().lower()
    effective_source = source_mode
    if source_mode not in {"youglish", "hybrid"}:
        source_mode = "hybrid"

    selected_domains = [d for d in (domains or [domain]) if d in DOMAIN_QUERY_HINTS]
    if not selected_domains:
        selected_domains = ["education"]
    if "phoneme_demo" in selected_domains and "education" not in selected_domains:
        selected_domains.append("education")

    sources: list[SourceCandidate] = []
    fallback_mode = False
    if source_mode == "youglish":
        _emit(progress, 0.05, "Searching YouGlish clips...")
        sources = _search_youglish_sources(word_clean, max_videos=max_videos)
        selected_domains = ["youglish"]
        if not sources:
            _emit(progress, 0.06, "YouGlish unavailable, falling back to pronunciation search...")
            fallback_domains = ["phoneme_demo", "education"]
            sources = _search_sources(word_clean, fallback_domains, max(max_videos, 4))
            # Prefer keyword-related titles and avoid Shorts for better clip quality.
            sources = [item for item in sources if "/shorts/" not in item.url.lower()]
            sources.sort(key=lambda item: _source_priority_score(item, word_clean), reverse=True)
            keyword_sources = [item for item in sources if _title_fallback_candidate(item.title, word_clean)]
            ted_keyword_sources = [
                item
                for item in keyword_sources
                if any(token in _norm_text(item.title) for token in ["ted", "tedx", "ted ed", "ted-ed"])
            ]
            if ted_keyword_sources:
                seen_urls = {item.url for item in ted_keyword_sources}
                sources = ted_keyword_sources + [item for item in keyword_sources if item.url not in seen_urls]
            elif keyword_sources:
                sources = keyword_sources
            # Keep fallback scan focused and fast.
            sources = sources[: max(max_videos * 8, 16)]
            selected_domains = fallback_domains
            effective_source = "youglish_fallback"
            fallback_mode = True
    else:
        cached_sources = _get_cached_sources(
            word_clean,
            selected_domains,
            max_count=max(max_videos * 10, 30),
        )
        seen_urls: set[str] = set()
        for source in cached_sources:
            if source.url and source.url not in seen_urls:
                sources.append(source)
                seen_urls.add(source.url)

        need_network_search = len(sources) < max(max_videos * 2, 10)
        if need_network_search:
            _emit(progress, 0.05, "Searching YouTube videos...")
            searched_sources = _search_sources(word_clean, selected_domains, max_videos)
            for source in searched_sources:
                if source.url and source.url not in seen_urls:
                    sources.append(source)
                    seen_urls.add(source.url)
        else:
            _emit(progress, 0.05, f"Using cached sources ({len(sources)})...")

    if not sources:
        if source_mode == "youglish":
            raise RuntimeError(
                "No candidate clips found from YouGlish for this keyword. "
                "Fallback pronunciation search also returned no candidates."
            )
        raise RuntimeError("No candidate videos found.")

    _emit(progress, 0.08, "Preparing pronunciation demo...")
    cambridge_clips = (
        _build_cambridge_demo_clips(
            word=word_clean,
            clips_dir=clips_dir,
            clip_start_index=1,
            max_items=1,
        )
        if include_cambridge
        else []
    )

    collected_youglish: list[ClipResult] = []
    downloaded_any = False
    asr_fallback_checks = 0
    if fallback_mode:
        max_scan = min(len(sources), max(max_videos * 6, max_videos + 10, 24))
    else:
        max_scan = min(len(sources), max(max_videos * 14, max_videos + 18, 48))
    scanned = 0
    for idx, source in enumerate(sources[:max_scan], start=1):
        if len(collected_youglish) >= max_videos:
            break
        scanned += 1
        stage = 0.10 + (0.75 * (idx / max(max_scan, 1)))
        _emit(progress, stage, f"Processing source {idx}/{max_scan}...")

        bundle = _download_video_and_vtt(source, downloads_dir, idx)
        if bundle is None:
            time.sleep(_PER_SOURCE_DELAY_SECONDS)
            continue
        video_path, vtt_path = bundle
        downloaded_any = True
        cues = _parse_vtt_file(vtt_path) if vtt_path else []
        remaining = max_videos - len(collected_youglish)
        per_source_limit = min(remaining, 1 if fallback_mode else 3)
        match_indexes = _find_match_indexes(cues, word_clean, max_count=per_source_limit) if cues else []

        for match_idx in match_indexes:
            clip_index = len(cambridge_clips) + len(collected_youglish) + 1
            clip = _extract_keyword_clip(
                word=word_clean,
                source=source,
                video_path=video_path,
                vtt_path=vtt_path if vtt_path is not None else video_path,
                clip_seconds=clip_seconds,
                output_dir=clips_dir,
                clip_index=clip_index,
                cues=cues,
                match_idx=match_idx,
            )
            if clip is None:
                continue
            collected_youglish.append(clip)
            if len(collected_youglish) >= max_videos:
                break
        if len(collected_youglish) >= max_videos:
            break

        allow_asr_fallback = bool(source.start_seconds is not None) or (
            asr_fallback_checks < 4 and _title_contains_keyword(source.title, word_clean)
        )
        if not allow_asr_fallback and fallback_mode and asr_fallback_checks < 10:
            # In fallback mode, subtitles are often unavailable. Use ASR on a few
            # more candidates so keyword detection can still succeed.
            allow_asr_fallback = True
        if not allow_asr_fallback:
            time.sleep(_PER_SOURCE_DELAY_SECONDS)
            continue
        if source.start_seconds is None:
            asr_fallback_checks += 1

        remaining = max_videos - len(collected_youglish)
        starts = _asr_keyword_window_starts(
            video_path,
            word_clean,
            max_count=min(1 if fallback_mode else 3, remaining),
        )
        if starts and source.start_seconds is not None:
            starts = sorted(starts, key=lambda item: abs(float(item) - float(source.start_seconds)))
        for start_sec in starts:
            if len(collected_youglish) >= max_videos:
                break
            clip_index = len(cambridge_clips) + len(collected_youglish) + 1
            fallback_clip = _extract_opening_clip(
                source=source,
                video_path=video_path,
                clip_seconds=clip_seconds,
                output_dir=clips_dir,
                clip_index=clip_index,
                caption=f"{word_clean} | {source.title}",
                start_seconds=float(start_sec),
            )
            if fallback_clip is not None:
                collected_youglish.append(fallback_clip)
        time.sleep(_PER_SOURCE_DELAY_SECONDS)

    if not collected_youglish:
        if downloaded_any:
            if source_mode == "youglish":
                raise RuntimeError(
                    "YouGlish sources were downloaded, but no keyword pronunciation segment was confirmed. "
                    "Try increasing clip count or switching to a more common word."
                )
            raise RuntimeError(
                "No subtitle segment with the keyword was found in downloaded videos. "
                "Try selecting more domains, increasing video count, or using a more frequent target word."
            )
        if (downloads_dir / "_youtube_blocked.flag").exists():
            po_hint = (
                "PO Token is configured; verify token freshness and client mapping."
                if _has_po_tokens()
                else "Configure PO Token via data/yt_po_tokens.json (or YTDLP_PO_TOKENS) for better reliability."
            )
            cookie_file = _resolve_cookie_file()
            if cookie_file is not None:
                cookie_hint = (
                    f"Cookie file detected at {cookie_file}, but YouTube still blocked access. "
                    "Re-export cookies after logging in YouTube and retry."
                )
            else:
                cookie_hint = "Please export cookies to data/yt_cookies.txt and retry."
            raise RuntimeError(
                "YouTube requires bot verification now. "
                f"{cookie_hint} {po_hint}"
            )
        raise RuntimeError("No keyword-aligned segment was found for this keyword.")
    if len(collected_youglish) < max_videos:
        _emit(progress, 0.86, f"Only found {len(collected_youglish)}/{max_videos} keyword-aligned clips. Generating partial result...")

    collected: list[ClipResult] = list(cambridge_clips) + list(collected_youglish)

    _emit(progress, 0.88, "Merging clips...")
    final_video = output_dir / f"{word_clean.lower()}_{domain}_clips.mp4"
    _concat_videos([item.clip_path for item in collected], final_video)

    final_audio = output_dir / f"{word_clean.lower()}_{domain}_clips.mp3"
    _run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(final_video),
            "-vn",
            "-acodec",
            "mp3",
            str(final_audio),
        ],
        timeout=180,
    )

    source_manifest = output_dir / "sources.json"
    source_manifest.write_text(
        json.dumps(
            [
                {
                    "source_title": item.source_title,
                    "source_url": item.source_url,
                    "subtitle": item.subtitle_text,
                    "clip_file": item.clip_path.name,
                }
                for item in collected
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if source_mode != "youglish":
        _record_cached_sources(word_clean, selected_domains, collected_youglish)

    _emit(progress, 1.0, "Completed")
    return {
        "word": word_clean,
        "domain": selected_domains[0],
        "domains": selected_domains,
        "source": effective_source,
        "cambridge_clips": len(cambridge_clips),
        "video_path": str(final_video),
        "audio_path": str(final_audio),
        "clips_generated": len(collected),
        "videos_scanned": scanned,
        "manifest_path": str(source_manifest),
    }
