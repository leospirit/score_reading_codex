import os
import sys
import logging
import json
import csv
import threading
import re
import time
import uuid
import gzip
import hmac
import shutil
import tempfile
from collections import Counter
from typing import Any, Dict, Optional
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
import requests
try:
    from qcloud_cos import CosConfig, CosS3Client
except Exception:  # pragma: no cover - optional dependency
    CosConfig = None  # type: ignore[assignment]
    CosS3Client = None  # type: ignore[assignment]

# Fix import path to prioritize backend src (inside score_reading) over frontend src
# This is crucial because both have a 'src' folder
sys.path.insert(0, str(Path(__file__).parent / "score_reading"))

from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Form, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.analysis.llm_advisor import get_llm_advisor
from src.config import config, load_config
from src.feedback_optimization import (
    apply_feedback_optimization_result,
    begin_feedback_optimization,
    build_feedback_optimization_state,
    freeze_feedback_optimization,
    hydrate_scoring_result_for_feedback,
    mark_feedback_optimization_pending,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

# Init Config
load_config()

app = FastAPI(title="Score Reading API")
ADMIN_API_TOKEN = str(os.getenv("ADMIN_API_TOKEN", "") or "").strip()
ADMIN_TOKEN_HEADER = "x-admin-token"

PLAYBOOK_PATH = Path(__file__).parent / "score_reading" / "advice" / "western_pronunciation_playbook.md"
PLAYBOOK_LOCK = threading.Lock()
FEEDBACK_OPTIMIZATION_LOCK = threading.Lock()
FEEDBACK_OPTIMIZATION_INFLIGHT: set[str] = set()

def _load_cors_origins() -> list[str]:
    """
    Load allowed CORS origins from env.
    - `CORS_ALLOW_ORIGINS` accepts comma-separated origins.
    - Fallback keeps common local development origins only.
    """
    raw = str(os.getenv("CORS_ALLOW_ORIGINS", "") or "").strip()
    if raw:
        items = [part.strip() for part in raw.split(",") if str(part).strip()]
        if items:
            return items
    return [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


_cors_origins = _load_cors_origins()
_cors_allow_credentials = "*" not in _cors_origins
if not _cors_allow_credentials:
    logger.warning("CORS_ALLOW_ORIGINS contains '*'; disabling credentials for CORS safety.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AzureConfig(BaseModel):
    api_key: Optional[str] = None
    region: Optional[str] = None

class GeminiConfig(BaseModel):
    api_key: Optional[str] = None
    model: Optional[str] = None
    alignment_source: Optional[str] = None

class LLMConfig(BaseModel):
    provider: str
    base_url: Optional[str] = None
    model: str
    api_key: Optional[str] = None

class ConfigUpdate(BaseModel):
    llm: Optional[LLMConfig] = None
    azure: Optional[AzureConfig] = None
    gemini: Optional[GeminiConfig] = None

class GradeThresholdConfig(BaseModel):
    c_min: Optional[int] = None
    b_min: Optional[int] = None
    a_min: Optional[int] = None
    a_plus_min: Optional[int] = None

class ReportDisplayConfig(BaseModel):
    score_view_mode: Optional[str] = None
    grade_thresholds: Optional[GradeThresholdConfig] = None

class FeedbackOverrideRequest(BaseModel):
    integrated_feedback_text: str
    updated_by: Optional[str] = None


class FeedbackFreezeBatchRequest(BaseModel):
    submission_ids: Optional[list[str]] = None
    reason: Optional[str] = None

class TeacherPhraseCreateRequest(BaseModel):
    text: str
    category: Optional[str] = None

class TeacherPhraseUseRequest(BaseModel):
    phrase_id: str

class ScriptReferenceRequest(BaseModel):
    text: str
    wait: bool = False
    timeout_sec: float = 0.0


class PlaybookUpdateRequest(BaseModel):
    text: str


class PlaybookIdeaRequest(BaseModel):
    idea: str
    ai_refine: bool = False


class WordClipJobRequest(BaseModel):
    word: str
    domain: str = "education"
    domains: Optional[list[str]] = None
    video_count: int = 4
    clip_seconds: float = 5.0
    source: str = "youglish"
    include_cambridge: bool = True


class AnalyticsPgSyncRequest(BaseModel):
    dry_run: bool = False
    limit: Optional[int] = None

# --- Async Job System ---
import asyncio
from enum import Enum
from typing import Optional

class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class Job(BaseModel):
    id: str
    status: JobStatus
    submission_id: str
    student_id: str
    task_id: str
    filename: str
    timestamp: float
    mode: Optional[str] = "auto"  # Added persistence for mode
    result_url: Optional[str] = None
    error: Optional[str] = None
    
# Global State
JOBS: Dict[str, Job] = {}
JOB_QUEUE: asyncio.Queue = asyncio.Queue()
JOBS_FILE = Path("data/jobs.json")
JOBS_FILE_LOCK = threading.Lock()
REPORTS_DIR = Path("data/out") # Ensure this is defined for worker usage or import it
WORK_TMP_DIR = Path("data/work")
WORK_TMP_CLEANUP_LOCK = threading.Lock()
LAST_WORK_TMP_CLEANUP_TS = 0.0

# Word clip extraction jobs (YouTube keyword montage)
WORD_CLIP_JOBS: Dict[str, Dict[str, Any]] = {}
WORD_CLIP_LOCK = threading.Lock()
WORD_CLIP_WORKER_SEMAPHORE = threading.Semaphore(1)
WORD_CLIP_PO_TOKEN_PATH = Path("data/yt_po_tokens.json")
TEACHER_PHRASE_BANK_PATH = Path("data/teacher_phrase_bank.json")
TEACHER_PHRASE_BANK_LOCK = threading.Lock()
TEACHER_PHRASE_CATEGORIES = {"praise", "issue", "advice", "encourage"}
SOS_LIBRARY_ROOT = Path(str(os.getenv("SOS_LIBRARY_ROOT", "data/sos_assets_english_full") or "data/sos_assets_english_full"))
SOS_LIBRARY_LOCK = threading.Lock()
SOS_LIBRARY_CACHE: dict[str, Any] | None = None
SOS_LIBRARY_CACHE_TS = 0.0
SOS_LIBRARY_CACHE_TTL_SECONDS = 12.0
PRON_FOCUS_CACHE_LOCK = threading.Lock()
PRON_FOCUS_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
PRON_FOCUS_CACHE_TTL_SECONDS = 45.0
COS_SIGN_CLIENT_LOCK = threading.Lock()
COS_SIGN_CLIENT: Any = None
COS_SIGN_CLIENT_KEY = ""


def _read_float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        logger.warning("Invalid %s=%r; fallback to %s", name, raw, default)
        return float(default)


def _read_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        logger.warning("Invalid %s=%r; fallback to %s", name, raw, default)
        return int(default)


def _read_bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s=%r; fallback to %s", name, raw, default)
    return bool(default)


WORK_TMP_RETENTION_HOURS = max(0.0, _read_float_env("WORK_TMP_RETENTION_HOURS", 24.0))
WORK_TMP_CLEANUP_INTERVAL_SECONDS = max(0.0, _read_float_env("WORK_TMP_CLEANUP_INTERVAL_SECONDS", 1800.0))
UPLOAD_MAX_MB = max(1.0, _read_float_env("UPLOAD_MAX_MB", 30.0))
UPLOAD_MAX_BYTES = int(UPLOAD_MAX_MB * 1024 * 1024)
UPLOAD_STORAGE_WARN_GB = max(1.0, _read_float_env("UPLOAD_STORAGE_WARN_GB", 20.0))
UPLOAD_STORAGE_WARN_BYTES = int(UPLOAD_STORAGE_WARN_GB * 1024 * 1024 * 1024)
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024
ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".oga",
    ".mpga",
    ".mpeg",
    ".webm",
    ".wma",
    ".mp4",
}
COS_SIGN_ENABLED = _read_bool_env("COS_SIGN_ENABLED", True)
COS_SIGN_EXPIRE_SECONDS = max(60, _read_int_env("COS_SIGN_EXPIRE_SECONDS", 7 * 24 * 3600))
COS_SIGN_REGION = str(os.getenv("COS_SIGN_REGION", os.getenv("COS_REGION", "ap-beijing")) or "ap-beijing").strip()
COS_SIGN_BUCKET = str(os.getenv("COS_SIGN_BUCKET", os.getenv("COS_BUCKET", "")) or "").strip()
COS_SIGN_KEY_PREFIX = str(os.getenv("COS_SIGN_KEY_PREFIX", "sos/phonemes") or "sos/phonemes").strip().strip("/")
COS_SIGN_SECRET_ID = str(
    os.getenv("COS_SIGN_SECRET_ID", os.getenv("COS_SECRET_ID", os.getenv("TENCENT_SECRET_ID", ""))) or ""
).strip()
COS_SIGN_SECRET_KEY = str(
    os.getenv("COS_SIGN_SECRET_KEY", os.getenv("COS_SECRET_KEY", os.getenv("TENCENT_SECRET_KEY", ""))) or ""
).strip()


def _normalize_po_token_key(value: str) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return "web.gvs"
    aliases = {
        "web": "web.gvs",
        "android": "android.gvs",
        "mweb": "mweb.gvs",
        "ios": "ios.gvs",
        "tv": "tv.gvs",
    }
    return aliases.get(token, token if "." in token else f"{token}.gvs")


def _extract_po_tokens_from_har(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    entries = (((payload or {}).get("log") or {}).get("entries") or [])
    if not isinstance(entries, list):
        return []

    def add_token(raw: str) -> None:
        item = str(raw or "").strip()
        if not item:
            return
        decoded = unquote(item)
        if decoded in seen:
            return
        seen.add(decoded)
        out.append(decoded)

    po_pattern = re.compile(r'"poToken"\s*:\s*"([^"]+)"', re.IGNORECASE)
    pot_pattern = re.compile(r'"pot"\s*:\s*"([^"]+)"', re.IGNORECASE)

    def decode_payload_text(raw_text: str) -> str:
        text = str(raw_text or "")
        if not text:
            return ""
        try:
            raw_bytes = text.encode("latin1", errors="ignore")
        except Exception:
            return text
        if len(raw_bytes) >= 2 and raw_bytes[0] == 0x1F and raw_bytes[1] == 0x8B:
            try:
                return gzip.decompress(raw_bytes).decode("utf-8", errors="ignore")
            except Exception:
                return text
        return text

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request_obj = entry.get("request") or {}
        if not isinstance(request_obj, dict):
            request_obj = {}
        url = str(request_obj.get("url") or "").strip()
        if url:
            try:
                query = parse_qs(urlparse(url).query or "")
            except Exception:
                query = {}
            for key in ("pot", "poToken"):
                for val in query.get(key, []) or []:
                    add_token(str(val))

        post_obj = request_obj.get("postData") or {}
        if isinstance(post_obj, dict):
            text = decode_payload_text(str(post_obj.get("text") or ""))
            if text:
                for match in po_pattern.findall(text):
                    add_token(match)
                for match in pot_pattern.findall(text):
                    add_token(match)

        response_obj = entry.get("response") or {}
        if isinstance(response_obj, dict):
            content_obj = response_obj.get("content") or {}
            if isinstance(content_obj, dict):
                text = decode_payload_text(str(content_obj.get("text") or ""))
                if text:
                    for match in po_pattern.findall(text):
                        add_token(match)
                    for match in pot_pattern.findall(text):
                        add_token(match)

    return out


def _load_saved_po_tokens() -> dict[str, str]:
    if not WORD_CLIP_PO_TOKEN_PATH.exists():
        return {}
    try:
        payload = json.loads(WORD_CLIP_PO_TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in payload.items():
        k = _normalize_po_token_key(str(key))
        v = str(value or "").strip()
        if k and v:
            out[k] = v
    return out


def _mask_token(value: str) -> str:
    token = str(value or "")
    if len(token) <= 12:
        return token
    return token[:8] + "..." + token[-6:]


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON atomically to avoid partial jobs.json corruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _as_non_negative_int(raw: Any, default: int = 0) -> int:
    try:
        value = int(float(raw))
    except Exception:
        value = int(default)
    return value if value >= 0 else int(default)


def _normalize_teacher_phrase_category(raw: Any) -> str:
    category = str(raw or "").strip().lower()
    return category if category in TEACHER_PHRASE_CATEGORIES else "praise"


def _normalize_teacher_phrase_text(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    return text


def _default_teacher_phrase_items(now_ts: int) -> list[Dict[str, Any]]:
    defaults = [
        {"category": "praise", "text": "这次朗读语气自然，整体节奏比较稳定。"},
        {"category": "praise", "text": "你对课文内容很熟悉，句子衔接比较流畅。"},
        {"category": "issue", "text": "关键问题是个别词尾收音不够清楚。"},
        {"category": "advice", "text": "建议：目标词先慢读3遍，再放回原句连读3遍。"},
        {"category": "encourage", "text": "继续保持这个状态，下次会更稳。"},
    ]
    out: list[Dict[str, Any]] = []
    for idx, row in enumerate(defaults, start=1):
        category = _normalize_teacher_phrase_category(row.get("category"))
        out.append(
            {
                "id": f"default_{category}_{idx:02d}",
                "text": str(row.get("text") or "").strip(),
                "category": category,
                "use_count": 0,
                "created_at": now_ts,
                "updated_at": now_ts,
                "last_used_at": 0,
                "builtin": True,
            }
        )
    return out


def _normalize_teacher_phrase_item(raw: Any, now_ts: int) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    text = _normalize_teacher_phrase_text(raw.get("text"))
    if not text:
        return None
    phrase_id = str(raw.get("id") or "").strip() or f"ph_{uuid.uuid4().hex[:12]}"
    created_at = _as_non_negative_int(raw.get("created_at"), now_ts)
    updated_at = _as_non_negative_int(raw.get("updated_at"), created_at)
    return {
        "id": phrase_id[:64],
        "text": text[:220],
        "category": _normalize_teacher_phrase_category(raw.get("category")),
        "use_count": _as_non_negative_int(raw.get("use_count"), 0),
        "created_at": created_at,
        "updated_at": updated_at,
        "last_used_at": _as_non_negative_int(raw.get("last_used_at"), 0),
        "builtin": bool(raw.get("builtin", False)),
    }


def _read_teacher_phrase_bank_unlocked() -> Dict[str, Any]:
    now_ts = int(time.time())
    raw: Dict[str, Any] = {}
    if TEACHER_PHRASE_BANK_PATH.exists():
        try:
            parsed = json.loads(TEACHER_PHRASE_BANK_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
        except Exception:
            raw = {}

    normalized_items: list[Dict[str, Any]] = []
    seen_texts: set[str] = set()
    items_raw = raw.get("items") if isinstance(raw, dict) else None
    if isinstance(items_raw, list):
        for row in items_raw:
            item = _normalize_teacher_phrase_item(row, now_ts)
            if not item:
                continue
            key = item["text"].lower()
            if key in seen_texts:
                continue
            seen_texts.add(key)
            normalized_items.append(item)

    if not normalized_items:
        normalized_items = _default_teacher_phrase_items(now_ts)

    bank = {
        "version": 1,
        "updated_at": _as_non_negative_int(raw.get("updated_at"), now_ts),
        "items": normalized_items,
    }

    if (not TEACHER_PHRASE_BANK_PATH.exists()) or raw != bank:
        _write_json_atomic(TEACHER_PHRASE_BANK_PATH, bank)

    return bank


def _sorted_teacher_phrase_items(items: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda row: (
            -_as_non_negative_int(row.get("use_count"), 0),
            -_as_non_negative_int(row.get("last_used_at"), 0),
            -_as_non_negative_int(row.get("updated_at"), 0),
            str(row.get("text", "")).lower(),
        ),
    )


def _delete_teacher_phrase_from_bank(phrase_id: str) -> Dict[str, Any]:
    phrase_id = str(phrase_id or "").strip()
    if not phrase_id:
        raise HTTPException(status_code=400, detail="phrase_id is required")

    now_ts = int(time.time())
    with TEACHER_PHRASE_BANK_LOCK:
        bank = _read_teacher_phrase_bank_unlocked()
        items = list(bank.get("items") or [])
        kept: list[Dict[str, Any]] = []
        deleted: Optional[Dict[str, Any]] = None

        for row in items:
            item = _normalize_teacher_phrase_item(row, now_ts)
            if not item:
                continue
            if item.get("id") != phrase_id:
                kept.append(item)
                continue
            if bool(item.get("builtin")):
                raise HTTPException(status_code=400, detail="builtin phrase cannot be deleted")
            deleted = item

        if not deleted:
            raise HTTPException(status_code=404, detail="phrase_id not found")

        bank["items"] = kept
        bank["updated_at"] = now_ts
        _write_json_atomic(TEACHER_PHRASE_BANK_PATH, bank)
        return deleted

def save_jobs():
    """Persist jobs to disk"""
    try:
        with JOBS_FILE_LOCK:
            data = {k: v.dict() for k, v in JOBS.items()}
            _write_json_atomic(JOBS_FILE, data)
    except Exception as e:
        logger.error(f"Failed to save jobs: {e}")

def load_jobs():
    """Load jobs from disk"""
    global JOBS
    if not JOBS_FILE.exists():
        return
    
    try:
        with open(JOBS_FILE, "r") as f:
            data = json.load(f)
            for k, v in data.items():
                try:
                    # Restore Job object
                    job = Job(**v)
                    # If job was PROCESSING when server died, re-queue it on startup.
                    if job.status == JobStatus.PROCESSING:
                        job.status = JobStatus.QUEUED
                        job.error = "Recovered after server restart; job re-queued."
                    JOBS[k] = job
                except Exception as e:
                    logger.warning(f"Skipping invalid job record {k}: {e}")
        logger.info(f"Loaded {len(JOBS)} jobs from disk")
    except Exception as e:
        logger.error(f"Failed to load jobs: {e}")


def cleanup_work_tmp_dirs(
    older_than_hours: float = 24.0,
    limit: int = 200,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Clean stale temporary folders under data/work/tmp*.
    Used to avoid indefinite disk growth from interrupted jobs.
    """
    root = WORK_TMP_DIR
    retention_hours = max(0.0, float(older_than_hours))
    max_delete = max(1, int(limit))

    if not root.exists():
        return {
            "status": "ok",
            "root_exists": False,
            "matched_count": 0,
            "deleted_count": 0,
            "error_count": 0,
            "dry_run": bool(dry_run),
        }

    cutoff_ts = time.time() - retention_hours * 3600.0
    candidates: list[tuple[float, Path]] = []
    for item in root.iterdir():
        if not item.is_dir() or not item.name.startswith("tmp"):
            continue
        try:
            mtime = float(item.stat().st_mtime)
        except Exception:
            continue
        if mtime <= cutoff_ts:
            candidates.append((mtime, item))

    candidates.sort(key=lambda pair: pair[0])
    targets = candidates[:max_delete]

    deleted_names: list[str] = []
    errors: list[str] = []
    for _, item in targets:
        if dry_run:
            deleted_names.append(item.name)
            continue
        try:
            shutil.rmtree(item)
            deleted_names.append(item.name)
        except Exception as exc:
            errors.append(f"{item.name}: {exc}")

    return {
        "status": "ok",
        "root_exists": True,
        "older_than_hours": retention_hours,
        "limit": max_delete,
        "matched_count": len(candidates),
        "deleted_count": 0 if dry_run else len(deleted_names),
        "error_count": len(errors),
        "sample_ids": deleted_names[:20],
        "dry_run": bool(dry_run),
    }


def maybe_cleanup_work_tmp_dirs(force: bool = False) -> None:
    global LAST_WORK_TMP_CLEANUP_TS
    now = time.time()

    with WORK_TMP_CLEANUP_LOCK:
        if (
            not force
            and WORK_TMP_CLEANUP_INTERVAL_SECONDS > 0.0
            and (now - LAST_WORK_TMP_CLEANUP_TS) < WORK_TMP_CLEANUP_INTERVAL_SECONDS
        ):
            return
        LAST_WORK_TMP_CLEANUP_TS = now

    try:
        cleanup = cleanup_work_tmp_dirs(
            older_than_hours=WORK_TMP_RETENTION_HOURS,
            limit=300,
            dry_run=False,
        )
        if int(cleanup.get("deleted_count", 0)) > 0 or int(cleanup.get("error_count", 0)) > 0:
            logger.info(
                "Work tmp cleanup: deleted=%s error=%s matched=%s retention_h=%s",
                cleanup.get("deleted_count", 0),
                cleanup.get("error_count", 0),
                cleanup.get("matched_count", 0),
                cleanup.get("older_than_hours", WORK_TMP_RETENTION_HOURS),
            )
    except Exception as exc:
        logger.warning("Work tmp cleanup skipped: %s", exc)


def _scan_tree_file_bytes(root: Path, allowed_suffixes: Optional[set[str]] = None) -> tuple[int, int]:
    if not root.exists():
        return 0, 0

    total_bytes = 0
    file_count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if allowed_suffixes is not None:
                            suffix = Path(entry.name).suffix.lower()
                            if suffix not in allowed_suffixes:
                                continue
                        file_count += 1
                        total_bytes += int(entry.stat(follow_symlinks=False).st_size)
                    except Exception:
                        continue
        except Exception:
            continue
    return total_bytes, file_count


def _scan_upload_audio_metrics(root: Path, report_submission_ids: Optional[set[str]] = None) -> Dict[str, int]:
    metrics: Dict[str, int] = {
        "uploads_bytes": 0,
        "uploads_file_count": 0,
        "linked_audio_file_count": 0,
        "orphan_audio_file_count": 0,
    }
    if not root.exists():
        return metrics

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        suffix = Path(entry.name).suffix.lower()
                        if suffix not in ALLOWED_AUDIO_EXTENSIONS:
                            continue
                        metrics["uploads_file_count"] += 1
                        metrics["uploads_bytes"] += int(entry.stat(follow_symlinks=False).st_size)

                        if report_submission_ids is not None:
                            submission_id = Path(entry.name).stem
                            if submission_id in report_submission_ids:
                                metrics["linked_audio_file_count"] += 1
                            else:
                                metrics["orphan_audio_file_count"] += 1
                    except Exception:
                        continue
        except Exception:
            continue
    return metrics


def _delete_upload_artifacts_for_submission(submission_id: str) -> int:
    upload_root = Path("data/uploads")
    if not upload_root.exists():
        return 0

    deleted = 0
    for up in upload_root.glob(f"**/{submission_id}.*"):
        if not up.is_file():
            continue
        try:
            up.unlink()
            deleted += 1
        except Exception as e:
            logger.warning(f"Failed to delete upload artifact {up}: {e}")
    return deleted


def _count_upload_artifacts_for_submission(submission_id: str) -> int:
    upload_root = Path("data/uploads")
    if not upload_root.exists():
        return 0
    count = 0
    for up in upload_root.glob(f"**/{submission_id}.*"):
        if up.is_file():
            count += 1
    return count


def get_active_processing_jobs() -> int:
    return sum(1 for job in JOBS.values() if job.status == JobStatus.PROCESSING)


def require_admin_token_if_configured(request: Request) -> None:
    """
    Optional admin protection:
    - If ADMIN_API_TOKEN is not configured, requests are allowed (backward-compatible).
    - If configured, caller must provide matching `x-admin-token` header.
    """
    if not ADMIN_API_TOKEN:
        return

    provided = str(request.headers.get(ADMIN_TOKEN_HEADER, "") or "").strip()
    if not provided:
        raise HTTPException(
            status_code=401,
            detail=f"Missing admin token header: {ADMIN_TOKEN_HEADER}",
        )
    if not hmac.compare_digest(provided, ADMIN_API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token")

async def worker():
    """Background worker to process jobs from the queue"""
    logger.info("Worker started")
    while True:
        try:
            job_id, file_path, text, mode, metadata = await JOB_QUEUE.get()
            
            # Update status to PROCESSING
            if job_id in JOBS:
                JOBS[job_id].status = JobStatus.PROCESSING
                save_jobs() # Save state
                logger.info(f"Processing job {job_id} ({metadata['submission_id']})")
                
            try:
                # Run Pipeline (Blocking CPU task, run in threadpool)
                from src.pipeline.runner import run_scoring_pipeline
                from fastapi.concurrency import run_in_threadpool
                
                # Execute pipeline in threadpool to not block async loop
                result, json_path, html_path = await run_in_threadpool(
                    run_scoring_pipeline,
                    mp3_path=file_path,
                    text=text,
                    output_dir=REPORTS_DIR,
                    student_id=metadata['student_id'],
                    task_id=metadata['task_id'],
                    submission_id=metadata['submission_id'],
                    engine_mode=metadata['engine_mode']
                )
                
                # Success
                if job_id in JOBS:
                    JOBS[job_id].status = JobStatus.COMPLETED
                    # Construct simplified report URL
                    rel_path = html_path.relative_to(REPORTS_DIR)
                    JOBS[job_id].result_url = f"/reports/{rel_path}"
                    save_jobs() # Save state
                    _invalidate_report_scan_cache()
                    logger.info(f"Job {job_id} completed")
                    
            except Exception as e:
                logger.exception("Job %s failed", job_id)
                if job_id in JOBS:
                    JOBS[job_id].status = JobStatus.FAILED
                    JOBS[job_id].error = str(e)
                    save_jobs() # Save state
            
            finally:
                JOB_QUEUE.task_done()
                
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            break
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    # Load concurrency config
    load_config()
    num_workers = int(config.get("concurrency.default_jobs", 4) or 4)
    align_source = str(config.get("engines.gemini.alignment_source", "whisper") or "whisper").strip().lower()
    if align_source == "gop" and num_workers > 1:
        logger.warning("GOP alignment is memory-heavy; capping workers to 1 for stability.")
        num_workers = 1
    logger.info(f"Starting {num_workers} background workers...")
    
    # Load persistence
    load_jobs()
    maybe_cleanup_work_tmp_dirs(force=True)
    
    # Restoring Queued Jobs
    from src.models import EngineMode
    count_restored = 0
    
    for job_id, job in JOBS.items():
        if job.status == JobStatus.QUEUED:
            # Reconstruct paths
            # Assuming standard path structure: data/uploads/YYYYMMDD/{submission_id}.mp3
            # We can try to find the file.
            # Since timestamp is float, we might not have exact date str easily unless we stored it.
            # BUT, we can search for the file in data/uploads/**/*.mp3 with matching submission_id
            
            found_mp3 = None
            found_txt = "" # Default empty
            
            upload_base = Path("data/uploads")
            if upload_base.exists():
                # Fast search: iterate dates folders?
                # Or just glob
                candidates = list(upload_base.glob(f"**/{job.submission_id}.mp3"))
                if candidates:
                    found_mp3 = candidates[0]
                    # Check for .txt sidecar
                    txt_path = found_mp3.with_suffix(".txt")
                    if txt_path.exists():
                        try:
                            found_txt = txt_path.read_text(encoding="utf-8")
                        except:
                            pass
            
            if found_mp3:
                # Re-queue
                try:
                    target_mode = EngineMode(job.mode.lower()) if job.mode else EngineMode.AUTO
                except:
                    target_mode = EngineMode.AUTO

                metadata = {
                    "student_id": job.student_id,
                    "task_id": job.task_id,
                    "submission_id": job.submission_id,
                    "engine_mode": target_mode
                }
                
                await JOB_QUEUE.put((job_id, found_mp3, found_txt, job.mode or "auto", metadata))
                count_restored += 1
                logger.info(f"Restored queued job {job_id} to execution queue.")
            else:
                logger.warning(f"Could not restore job {job_id}: File not found.")
                # Mark as failed?
                job.status = JobStatus.FAILED
                job.error = "File lost during restart"
                
    if count_restored > 0:
        logger.info(f"Restored {count_restored} jobs from persistence.")
        save_jobs() # Save any failed updates

    for i in range(num_workers):
        asyncio.create_task(worker())

DEFAULT_GRADE_THRESHOLDS = {
    "c_min": 61,
    "b_min": 71,
    "a_min": 81,
    "a_plus_min": 86,
}

def _clamp_int(value: Any, min_v: int, max_v: int) -> int:
    try:
        n = int(round(float(value)))
    except Exception:
        return min_v
    return max(min_v, min(max_v, n))

def _normalize_grade_thresholds(raw: Optional[Dict[str, Any]]) -> Dict[str, int]:
    src = raw or {}
    c_min = _clamp_int(src.get("c_min", DEFAULT_GRADE_THRESHOLDS["c_min"]), 1, 97)
    b_min = _clamp_int(src.get("b_min", DEFAULT_GRADE_THRESHOLDS["b_min"]), c_min + 1, 98)
    a_min = _clamp_int(src.get("a_min", DEFAULT_GRADE_THRESHOLDS["a_min"]), b_min + 1, 99)
    a_plus_min = _clamp_int(src.get("a_plus_min", DEFAULT_GRADE_THRESHOLDS["a_plus_min"]), a_min + 1, 100)
    return {
        "c_min": c_min,
        "b_min": b_min,
        "a_min": a_min,
        "a_plus_min": a_plus_min,
    }

def _normalize_score_view_mode(raw: Any) -> str:
    return "grade" if str(raw or "").strip().lower() == "grade" else "score"

def _get_report_display_config() -> Dict[str, Any]:
    report_conf = config.get("report", {}) or {}
    display_conf = {}
    if isinstance(report_conf, dict):
        display_conf = report_conf.get("display", {}) or {}
    mode = _normalize_score_view_mode(display_conf.get("score_view_mode"))
    thresholds = _normalize_grade_thresholds(display_conf.get("grade_thresholds"))
    return {
        "score_view_mode": mode,
        "grade_thresholds": thresholds,
    }

@app.get("/api/config")
def get_config():
    """Get current config (masking API key)"""
    # Reload to get latest
    load_config()
    
    llm_conf = config.get("llm", {})
    azure_conf = config.get("engines.azure", {})
    gemini_conf = config.get("engines.gemini", {})
    llm_key = llm_conf.get("api_key") or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY")
    azure_key = azure_conf.get("api_key") or os.getenv("AZURE_API_KEY")
    gemini_key = gemini_conf.get("api_key") or os.getenv("GEMINI_API_KEY")
    
    def mask_key(key):
        if not key: return ""
        if len(key) > 6: return f"{key[:3]}...{key[-3:]}"
        return "***"
        
    return {
        "llm": {
            "provider": llm_conf.get("provider", "openai"),
            "base_url": llm_conf.get("base_url", ""),
            "model": llm_conf.get("model", "gpt-4o"),
            "api_key_masked": mask_key(llm_key or ""),
            "has_key": bool(llm_key)
        },
        "azure": {
            "region": azure_conf.get("region", "eastus"),
            "api_key_masked": mask_key(azure_key or ""),
            "has_key": bool(azure_key)
        },
        "gemini": {
            "model": gemini_conf.get("model", "gemini-3-flash-preview"),
            "alignment_source": gemini_conf.get("alignment_source", "whisper"),
            "api_key_masked": mask_key(gemini_key or ""),
            "has_key": bool(gemini_key)
        },
        "upload": {
            "max_mb": float(UPLOAD_MAX_MB),
        },
        "report_display": _get_report_display_config(),
    }

@app.get("/api/report-display")
def get_report_display():
    """Get report score/grade display preferences (shared across UI pages)."""
    load_config()
    return {"status": "ok", "report_display": _get_report_display_config()}

@app.post("/api/report-display")
def update_report_display(data: ReportDisplayConfig):
    payload = data.dict(exclude_unset=True)
    if not payload:
        return {"status": "ok", "message": "No changes detected", "report_display": _get_report_display_config()}

    current = _get_report_display_config()
    next_mode = current.get("score_view_mode", "score")
    next_thresholds = current.get("grade_thresholds", DEFAULT_GRADE_THRESHOLDS)

    if "score_view_mode" in payload:
        next_mode = _normalize_score_view_mode(payload.get("score_view_mode"))
    if "grade_thresholds" in payload and isinstance(payload.get("grade_thresholds"), dict):
        next_thresholds = _normalize_grade_thresholds(payload.get("grade_thresholds"))

    updates = {
        "report": {
            "display": {
                "score_view_mode": next_mode,
                "grade_thresholds": next_thresholds,
            }
        }
    }
    try:
        config.save_user_config(updates)
        return {"status": "ok", "message": "Report display updated", "report_display": _get_report_display_config()}
    except Exception as e:
        logger.error(f"Failed to save report display config: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config", dependencies=[Depends(require_admin_token_if_configured)])
def update_config(data: ConfigUpdate):
    updates = data.dict(exclude_unset=True)
    logger.info(f"Received config update: {updates.keys()}")

    save_updates = {}

    # 1. LLM
    if "llm" in updates:
        llm_update = updates["llm"]
        clean_llm = {k: v for k, v in llm_update.items() if k != "api_key"}
        if llm_update.get("api_key") and "***" not in llm_update["api_key"]:
            clean_llm["api_key"] = llm_update["api_key"]
        save_updates["llm"] = clean_llm

    # 2. Azure
    if "azure" in updates:
        az_update = updates["azure"]
        clean_az = {k: v for k, v in az_update.items() if k != "api_key"}
        if az_update.get("api_key") and "***" not in az_update["api_key"]:
            clean_az["api_key"] = az_update["api_key"]
        save_updates["engines"] = save_updates.get("engines", {})
        save_updates["engines"]["azure"] = clean_az

    # 3. Gemini
    if "gemini" in updates:
        gm_update = updates["gemini"]
        clean_gm = {k: v for k, v in gm_update.items() if k != "api_key"}
        if "alignment_source" in clean_gm:
            src = str(clean_gm.get("alignment_source", "")).strip().lower()
            if src not in {"whisper", "gop"}:
                clean_gm.pop("alignment_source", None)
            else:
                clean_gm["alignment_source"] = src
        if gm_update.get("api_key") and "***" not in gm_update["api_key"]:
            clean_gm["api_key"] = gm_update["api_key"]
        save_updates["engines"] = save_updates.get("engines", {})
        save_updates["engines"]["gemini"] = clean_gm
        
    if save_updates:
        try:
            config.save_user_config(save_updates)
            return {"status": "ok", "message": "Configuration saved"}
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    return {"status": "ok", "message": "No changes detected"}


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


@app.get("/api/storage/uploads-usage")
def uploads_storage_usage():
    upload_root = Path("data/uploads")
    report_submission_ids: set[str] = set()
    try:
        cached_reports = _get_cached_reports_snapshot()
        for row in cached_reports:
            submission_id = str((row or {}).get("id") or "").strip()
            if submission_id:
                report_submission_ids.add(submission_id)
    except Exception:
        report_submission_ids = set()

    metrics = _scan_upload_audio_metrics(upload_root, report_submission_ids)
    uploads_bytes = int(metrics.get("uploads_bytes", 0))
    file_count = int(metrics.get("uploads_file_count", 0))
    linked_audio_file_count = int(metrics.get("linked_audio_file_count", 0))
    orphan_audio_file_count = int(metrics.get("orphan_audio_file_count", 0))
    warn_bytes = int(UPLOAD_STORAGE_WARN_BYTES)
    over_warn = uploads_bytes >= warn_bytes

    disk_total = 0
    disk_used = 0
    disk_free = 0
    try:
        probe = upload_root if upload_root.exists() else Path("data")
        usage = shutil.disk_usage(probe)
        disk_total = int(usage.total)
        disk_used = int(usage.used)
        disk_free = int(usage.free)
    except Exception:
        pass

    return {
        "status": "ok",
        "uploads_root": str(upload_root),
        "uploads_bytes": int(uploads_bytes),
        "uploads_gb": round(float(uploads_bytes) / (1024.0 ** 3), 3),
        "uploads_file_count": int(file_count),
        "report_submission_count": int(len(report_submission_ids)),
        "linked_audio_file_count": int(linked_audio_file_count),
        "orphan_audio_file_count": int(orphan_audio_file_count),
        "warn_bytes": int(warn_bytes),
        "warn_gb": float(UPLOAD_STORAGE_WARN_GB),
        "over_warn": bool(over_warn),
        "disk_total_bytes": int(disk_total),
        "disk_used_bytes": int(disk_used),
        "disk_free_bytes": int(disk_free),
    }


@app.get("/api/diagnostics/summary")
def diagnostics_summary(
    failed_limit: int = Query(default=10, ge=1, le=100),
):
    """Read-only diagnostics summary for quick operational checks."""
    reports_count = 0
    latest_report_timestamp = 0.0
    try:
        reports = _get_cached_reports_snapshot()
        reports_count = len(reports)
        if reports:
            latest_report_timestamp = float(reports[0].get("timestamp") or 0.0)
    except Exception:
        reports = []

    upload_root = Path("data/uploads")
    report_submission_ids: set[str] = set()
    for row in reports:
        submission_id = str((row or {}).get("id") or "").strip()
        if submission_id:
            report_submission_ids.add(submission_id)

    metrics = _scan_upload_audio_metrics(upload_root, report_submission_ids)
    uploads_bytes = int(metrics.get("uploads_bytes", 0))
    uploads_file_count = int(metrics.get("uploads_file_count", 0))
    linked_audio_file_count = int(metrics.get("linked_audio_file_count", 0))
    orphan_audio_file_count = int(metrics.get("orphan_audio_file_count", 0))
    warn_bytes = int(UPLOAD_STORAGE_WARN_BYTES)

    def _job_status_text(job: Job) -> str:
        raw = job.status
        return raw.value if isinstance(raw, JobStatus) else str(raw).strip().lower()

    all_jobs = list(JOBS.values())
    status_counts = Counter(_job_status_text(job) for job in all_jobs)
    failed_jobs = [job for job in all_jobs if _job_status_text(job) == JobStatus.FAILED.value]
    failed_jobs.sort(key=lambda x: float(x.timestamp or 0.0), reverse=True)

    failed_recent = [
        {
            "job_id": str(job.id),
            "submission_id": str(job.submission_id),
            "timestamp": float(job.timestamp or 0.0),
            "error": str(job.error or "").strip(),
        }
        for job in failed_jobs[: int(failed_limit)]
    ]

    disk_total = 0
    disk_used = 0
    disk_free = 0
    try:
        probe = upload_root if upload_root.exists() else Path("data")
        usage = shutil.disk_usage(probe)
        disk_total = int(usage.total)
        disk_used = int(usage.used)
        disk_free = int(usage.free)
    except Exception:
        pass

    return {
        "status": "ok",
        "generated_at": float(time.time()),
        "reports": {
            "count": int(reports_count),
            "latest_timestamp": float(latest_report_timestamp),
        },
        "uploads": {
            "bytes": int(uploads_bytes),
            "gb": round(float(uploads_bytes) / (1024.0 ** 3), 3),
            "file_count": int(uploads_file_count),
            "linked_file_count": int(linked_audio_file_count),
            "orphan_file_count": int(orphan_audio_file_count),
            "warn_bytes": int(warn_bytes),
            "warn_gb": float(UPLOAD_STORAGE_WARN_GB),
            "over_warn": bool(uploads_bytes >= warn_bytes),
        },
        "jobs": {
            "total": int(len(all_jobs)),
            "queued": int(status_counts.get(JobStatus.QUEUED.value, 0)),
            "processing": int(status_counts.get(JobStatus.PROCESSING.value, 0)),
            "completed": int(status_counts.get(JobStatus.COMPLETED.value, 0)),
            "failed": int(status_counts.get(JobStatus.FAILED.value, 0)),
            "active": int(status_counts.get(JobStatus.QUEUED.value, 0) + status_counts.get(JobStatus.PROCESSING.value, 0)),
        },
        "failed_recent": failed_recent,
        "disk": {
            "total_bytes": int(disk_total),
            "used_bytes": int(disk_used),
            "free_bytes": int(disk_free),
        },
    }


@app.post("/api/restart", dependencies=[Depends(require_admin_token_if_configured)])
def restart_backend():
    """
    Trigger self-restart. Container will come back automatically via docker restart policy.
    """
    active_processing = get_active_processing_jobs()
    if active_processing > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot restart now: {active_processing} job(s) are still processing."
        )

    def _exit_process():
        logger.warning("Backend restart requested via /api/restart")
        os._exit(0)

    timer = threading.Timer(0.8, _exit_process)
    timer.daemon = True
    timer.start()
    return {"status": "restarting", "message": "Backend restart initiated"}


@app.post("/api/analytics/pg-sync", dependencies=[Depends(require_admin_token_if_configured)])
async def trigger_analytics_pg_sync(payload: AnalyticsPgSyncRequest):
    """
    Trigger sidecar PG analytics sync.
    - Does not affect scoring/report generation pipeline.
    - Reads report JSON from data/out and upserts analytics tables.
    """
    dry_run = bool(payload.dry_run)
    limit = int(payload.limit) if payload.limit is not None else None
    if limit is not None and limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be > 0")

    dsn = str(os.getenv("PG_ANALYTICS_DSN", "") or os.getenv("DATABASE_URL", "")).strip()
    if not dry_run and not dsn:
        raise HTTPException(status_code=400, detail="PG_ANALYTICS_DSN is not configured")

    schema_sql = (Path(__file__).parent / "analytics_pg" / "schema.sql").resolve()
    if not schema_sql.exists():
        raise HTTPException(status_code=500, detail=f"Schema SQL not found: {schema_sql}")

    from fastapi.concurrency import run_in_threadpool

    try:
        from src.analytics.pg_sync import sync_reports_to_pg
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load PG sync module: {exc}") from exc

    try:
        summary = await run_in_threadpool(
            sync_reports_to_pg,
            dsn=dsn,
            report_root=REPORTS_DIR,
            schema_sql_path=schema_sql,
            limit=limit,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("PG analytics sync failed")
        raise HTTPException(status_code=500, detail=f"PG analytics sync failed: {exc}") from exc

    return {
        "status": "ok",
        "dry_run": dry_run,
        "limit": limit,
        "summary": summary,
    }

# Serve Reports (e.g. data/out/demo/demo_report.html -> /reports/demo/demo_report.html)
REPORTS_DIR = Path("data/out")
if not REPORTS_DIR.exists():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=REPORTS_DIR, html=True), name="reports")

REPORT_SCAN_CACHE_TTL_SECONDS = max(
    0.5,
    float(os.getenv("REPORT_SCAN_CACHE_TTL_SECONDS", "3") or "3"),
)
REPORT_SCAN_CACHE_LOCK = threading.Lock()
REPORT_SCAN_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "items": [],
}


def _invalidate_report_scan_cache() -> None:
    with REPORT_SCAN_CACHE_LOCK:
        REPORT_SCAN_CACHE["expires_at"] = 0.0
        REPORT_SCAN_CACHE["items"] = []


def _scan_reports_from_disk() -> list[Dict[str, Any]]:
    reports: list[Dict[str, Any]] = []
    if not REPORTS_DIR.exists():
        return reports

    for report_file in REPORTS_DIR.glob("**/*.html"):
        if report_file.name == "index.html":
            continue

        submission_id = report_file.stem
        json_path = report_file.parent / f"{submission_id}.json"
        try:
            rel_path = report_file.relative_to(REPORTS_DIR)
            url = f"/reports/{rel_path}"
        except Exception:
            continue

        report_data: Dict[str, Any] = {
            "id": submission_id,
            "url": url,
            "timestamp": os.path.getmtime(report_file),
            "student_name": submission_id.split("_")[0],
            "display_name": submission_id,
            "original_filename": None,
            "score": None,
        }

        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    report_data["score"] = data.get("scores", {}).get("overall_100")
                    meta = data.get("meta", {})
                    if meta.get("student_id"):
                        report_data["student_name"] = meta["student_id"]
                        report_data["display_name"] = meta["student_id"]
            except Exception:
                pass

        reports.append(report_data)

    reports.sort(key=lambda x: x["timestamp"], reverse=True)
    return reports


def _get_cached_reports_snapshot() -> list[Dict[str, Any]]:
    now_ts = time.time()
    with REPORT_SCAN_CACHE_LOCK:
        expires_at = float(REPORT_SCAN_CACHE.get("expires_at") or 0.0)
        cached_items = REPORT_SCAN_CACHE.get("items")
        if isinstance(cached_items, list) and now_ts < expires_at:
            return [dict(row) for row in cached_items]

    fresh = _scan_reports_from_disk()
    with REPORT_SCAN_CACHE_LOCK:
        REPORT_SCAN_CACHE["items"] = fresh
        REPORT_SCAN_CACHE["expires_at"] = time.time() + REPORT_SCAN_CACHE_TTL_SECONDS
        return [dict(row) for row in fresh]

@app.delete("/api/reports/{submission_id}")
async def delete_report(submission_id: str):
    """
    Delete a report and its associated files.
    """
    # Defensive check against directory traversal
    if ".." in submission_id or "/" in submission_id or "\\" in submission_id:
         raise HTTPException(status_code=400, detail="Invalid submission ID")

    # Find the report directory
    # Structure: data/out/{task}/{student}/{sub_id}/...
    found = False
    
    # Brute force search for simplicity given the varying depth/structure or just use glob
    # We know the ID is unique enough
    # Try to find the folder ending in submission_id
    
    target_dir = None
    
    # Search in data/out
    for path in REPORTS_DIR.glob(f"**/{submission_id}"):
        if path.is_dir():
             target_dir = path
             break
             
    if not target_dir:
        # It might be a flat structure or just a file in some legacy cases, but we standardized on folders
        # Let's try to look for the JSON file to locate it
        for path in REPORTS_DIR.glob(f"**/{submission_id}.json"):
             target_dir = path.parent
             break
             
    if target_dir and target_dir.exists():
         import shutil
         try:
             shutil.rmtree(target_dir)
             found = True
             logger.info(f"Deleted report dir: {target_dir}")
         except Exception as e:
             logger.error(f"Failed to delete {target_dir}: {e}")
             raise HTTPException(status_code=500, detail=f"Failed to delete report files: {e}")
    else:
         # Check if it was just a loose HTML file (unlikely in new structure but possible)
         pass

    # Also remove source upload artifacts by submission_id (audio + sidecars)
    upload_deleted = _delete_upload_artifacts_for_submission(submission_id)
    if upload_deleted > 0:
        found = True
    
    if not found:
         # Try to check if it matches a JOB id (for Pending/Failed jobs that have no directory)
         pass

    # CRITICAL: Also remove from JOBS persistence
    job_found = False
    
    # Check by key (Job ID)
    if submission_id in JOBS:
        del JOBS[submission_id]
        job_found = True
    else:
        # Check by submission_id value (if key is UUID not sub_id)
        # JOBS keys are job_id (UUID), but submission_id is passed here?
        # Actually server.py list_reports returns 'id' as 'submission_id'. 
        # But JOBS keys are UUIDs. 
        # We need to find the job with this submission_id.
        keys_to_delete = []
        for k, v in JOBS.items():
            if v.submission_id == submission_id:
                keys_to_delete.append(k)
        
        for k in keys_to_delete:
            del JOBS[k]
            job_found = True
            
    if job_found:
        save_jobs()
        logger.info(f"Removed job record associated with {submission_id}")

    if not found and not job_found:
         raise HTTPException(status_code=404, detail="Report/Job not found")
    if found:
        _invalidate_report_scan_cache()
         
    return {
        "status": "success",
        "message": f"Report {submission_id} deleted",
        "upload_deleted_count": upload_deleted,
    }

class BatchDeleteRequest(BaseModel):
    ids: list[str]

@app.post("/api/reports/batch-delete")
async def batch_delete_reports(request: BatchDeleteRequest):
    """批量删除报告"""
    deleted_count = 0
    errors = []
    
    # Reload JOBS just in case
    global JOBS
    
    ids_to_remove_from_jobs = []

    # Helper to find dir
    def find_dir(sid):
        for path in REPORTS_DIR.glob(f"**/{sid}"):
            if path.is_dir():
                return path
        return None

    upload_deleted_total = 0

    for sub_id in request.ids:
        # 1. Delete Directory (Search recursively)
        report_dir = find_dir(sub_id)
        
        if report_dir and report_dir.exists():
            import shutil
            try:
                shutil.rmtree(report_dir)
                deleted_count += 1
            except Exception as e:
                errors.append(f"Failed to delete dir {sub_id}: {e}")
        else:
            # Maybe it doesn't exist on disk (just job record), that's fine
            pass
        
        # 2. Delete source upload artifacts (audio + sidecars)
        upload_deleted_total += _delete_upload_artifacts_for_submission(sub_id)

        # 3. Mark for Job Deletion
        # Check by key
        if sub_id in JOBS:
            ids_to_remove_from_jobs.append(sub_id)
        else:
            # Check by submission_id value
            for k, v in JOBS.items():
                if v.submission_id == sub_id:
                    ids_to_remove_from_jobs.append(k)
                    break
    
    # Remove from JOBS
    for job_id in ids_to_remove_from_jobs:
        if job_id in JOBS:
            del JOBS[job_id]
            
    if ids_to_remove_from_jobs:
        save_jobs()
    if deleted_count > 0:
        _invalidate_report_scan_cache()
        
    return {
        "status": "success", 
        "deleted_count": deleted_count, 
        "upload_deleted_count": upload_deleted_total,
        "job_removed_count": len(ids_to_remove_from_jobs),
        "errors": errors
    }

@app.get("/api/reports")
async def list_reports(
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=100),
    search: str = Query(default=""),
    status: str = Query(default="all"),
    date_range: str = Query(default="all"),
):
    """
    List all generated reports in the output directory.
    """
    reports = _get_cached_reports_snapshot()

    # Build a quick lookup so UI can display the original uploaded filename.
    job_by_submission_id: Dict[str, Job] = {}
    for j in JOBS.values():
        prev = job_by_submission_id.get(j.submission_id)
        if not prev or j.timestamp > prev.timestamp:
            job_by_submission_id[j.submission_id] = j
    for row in reports:
        sid = str(row.get("id") or "")
        if not sid:
            continue
        job = job_by_submission_id.get(sid)
        if job and job.filename:
            row["original_filename"] = job.filename
            row["display_name"] = Path(job.filename).stem

    # Backward-compatible mode:
    # if no filtering/pagination params are provided, return plain list as before.
    search_text = str(search or "").strip().lower()
    status_key = str(status or "all").strip().lower()
    date_range_key = str(date_range or "all").strip().lower()
    use_extended_response = (
        page is not None
        or page_size is not None
        or bool(search_text)
        or status_key not in ("", "all")
        or date_range_key not in ("", "all")
    )
    if not use_extended_response:
        return reports

    filtered = reports
    if date_range_key in ("today", "7d", "last7d", "last_7_days"):
        now_local = time.localtime()
        cutoff_7d = time.time() - 7 * 24 * 3600

        def _report_ts(row: Dict[str, Any]) -> float:
            try:
                return float(row.get("timestamp") or 0.0)
            except Exception:
                return 0.0

        if date_range_key == "today":
            def _is_today(row: Dict[str, Any]) -> bool:
                ts = _report_ts(row)
                if ts <= 0:
                    return False
                d = time.localtime(ts)
                return d.tm_year == now_local.tm_year and d.tm_yday == now_local.tm_yday
            filtered = [row for row in filtered if _is_today(row)]
        else:
            filtered = [row for row in filtered if _report_ts(row) >= cutoff_7d]

    if search_text:
        def _matches(row: Dict[str, Any]) -> bool:
            return (
                search_text in str(row.get("id", "")).lower()
                or search_text in str(row.get("student_name", "")).lower()
                or search_text in str(row.get("display_name", "")).lower()
                or search_text in str(row.get("original_filename", "")).lower()
            )
        filtered = [row for row in filtered if _matches(row)]

    if status_key in ("scored", "with_score"):
        filtered = [row for row in filtered if row.get("score") is not None]
    elif status_key in ("unscored", "without_score"):
        filtered = [row for row in filtered if row.get("score") is None]

    total = len(filtered)
    page_size_val = int(page_size or 8)
    total_pages = max(1, (total + page_size_val - 1) // page_size_val)
    page_val = int(page or 1)
    page_val = min(max(1, page_val), total_pages)

    start = (page_val - 1) * page_size_val
    end = start + page_size_val
    items = filtered[start:end]

    return {
        "items": items,
        "total": total,
        "page": page_val,
        "page_size": page_size_val,
        "total_pages": total_pages,
        "has_prev": page_val > 1,
        "has_next": page_val < total_pages,
    }


def _validate_submission_id(submission_id: str) -> None:
    if ".." in submission_id or "/" in submission_id or "\\" in submission_id:
        raise HTTPException(status_code=400, detail="Invalid submission ID")


def _find_report_json_path(submission_id: str) -> Optional[Path]:
    for path in REPORTS_DIR.glob(f"**/{submission_id}.json"):
        if path.exists():
            return path
    return None


def _read_report_payload(json_path: Path) -> dict[str, Any]:
    current = json.loads(json_path.read_text(encoding="utf-8"))
    return current if isinstance(current, dict) else {}


def _freeze_report_feedback(json_path: Path, *, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _read_report_payload(json_path)
    state = freeze_feedback_optimization(current, reason=reason)
    _write_json_atomic(json_path, current)
    return current, state


def _feedback_error_message(advisor_feedback: dict[str, Any] | None) -> str:
    if not isinstance(advisor_feedback, dict):
        return ""
    errors = advisor_feedback.get("_advisor_errors")
    if isinstance(errors, list):
        joined = " | ".join(str(item or "").strip() for item in errors if str(item or "").strip())
        if joined:
            return joined[:1000]
    return ""


def _should_schedule_feedback_optimization(payload: dict[str, Any]) -> bool:
    state = build_feedback_optimization_state(payload)
    if state.get("status") != "pending":
        return False
    if str(state.get("current_provider") or "") != "azure_fallback":
        return False
    if int(state.get("updated_at", 0) or 0) > 0:
        return False
    return bool(str(state.get("current_text") or "").strip())


def _run_feedback_optimization(submission_id: str, json_path_str: str) -> None:
    json_path = Path(json_path_str)
    try:
        current = _read_report_payload(json_path)
        state = begin_feedback_optimization(current)
        if state.get("status") != "optimizing":
            return
        expected_version = int(state.get("version", 0) or 0)
        _write_json_atomic(json_path, current)

        advisor = get_llm_advisor()
        result = hydrate_scoring_result_for_feedback(current)
        _, advisor_feedback = advisor.generate_feedback(result)

        latest = _read_report_payload(json_path)
        provider = (
            str((advisor_feedback or {}).get("_advisor_provider") or (advisor_feedback or {}).get("provider") or "")
            .strip()
            .lower()
        )
        overall_comment = str((advisor_feedback or {}).get("overall_comment", "") or "").strip()
        if not advisor_feedback or provider == "azure_fallback" or not overall_comment:
            mark_feedback_optimization_pending(latest, error=_feedback_error_message(advisor_feedback))
            _write_json_atomic(json_path, latest)
            return

        updated = apply_feedback_optimization_result(
            latest,
            advisor_feedback,
            expected_version=expected_version,
        )
        _write_json_atomic(json_path, updated)
    except Exception as exc:
        try:
            latest = _read_report_payload(json_path)
            mark_feedback_optimization_pending(latest, error=str(exc))
            _write_json_atomic(json_path, latest)
        except Exception:
            logger.warning("Failed to persist feedback optimization error for %s", submission_id, exc_info=True)
        logger.warning("Feedback optimization failed for %s: %s", submission_id, exc)
    finally:
        with FEEDBACK_OPTIMIZATION_LOCK:
            FEEDBACK_OPTIMIZATION_INFLIGHT.discard(submission_id)


def _schedule_feedback_optimization(submission_id: str, json_path: Path) -> bool:
    with FEEDBACK_OPTIMIZATION_LOCK:
        if submission_id in FEEDBACK_OPTIMIZATION_INFLIGHT:
            return False
        FEEDBACK_OPTIMIZATION_INFLIGHT.add(submission_id)

    worker_thread = threading.Thread(
        target=_run_feedback_optimization,
        args=(submission_id, str(json_path)),
        daemon=True,
    )
    worker_thread.start()
    return True


@app.get("/api/reports/{submission_id}/data")
async def get_report_data(submission_id: str):
    """
    获取报告的完整 JSON 数据，用于报告生成器
    """
    _validate_submission_id(submission_id)
    
    # 查找 JSON 文件
    json_path = _find_report_json_path(submission_id)
    
    if not json_path or not json_path.exists():
        raise HTTPException(status_code=404, detail="Report data not found")
    
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Backward compatibility: enrich legacy/sparse reports with dense phoneme alignment.
        try:
            alignment_data = data.get("alignment", {}) if isinstance(data, dict) else {}
            words_data = alignment_data.get("words", []) if isinstance(alignment_data, dict) else []
            phonemes_data = alignment_data.get("phonemes", []) if isinstance(alignment_data, dict) else []

            def _normalize_token(text: Any) -> str:
                return "".join(ch for ch in str(text or "").lower() if ("a" <= ch <= "z") or ch == "'")

            should_enrich = False
            if isinstance(words_data, list) and words_data:
                if not isinstance(phonemes_data, list) or len(phonemes_data) == 0:
                    should_enrich = True
                else:
                    word_tokens = [
                        _normalize_token((w or {}).get("word", ""))
                        for w in words_data
                        if isinstance(w, dict)
                    ]
                    word_tokens = [t for t in word_tokens if t]
                    phoneme_tokens = {
                        _normalize_token((p or {}).get("in_word", ""))
                        for p in phonemes_data
                        if isinstance(p, dict)
                    }
                    phoneme_tokens.discard("")
                    covered = len({t for t in word_tokens if t in phoneme_tokens})
                    coverage_ratio = (covered / max(1, len(set(word_tokens)))) if word_tokens else 0.0
                    # If only a few words have phonemes (common in sparse cloud outputs), top up data.
                    should_enrich = coverage_ratio < 0.55

            if should_enrich:
                from src.models import Alignment, PhonemeAlignment, PhonemeTag, WordAlignment, WordTag
                from src.pipeline.phoneme_fallback import ensure_dense_phoneme_alignment

                def _to_word_tag(raw: Any) -> WordTag:
                    tag = str(raw or "ok").strip().lower()
                    if tag == "weak":
                        return WordTag.WEAK
                    if tag == "poor":
                        return WordTag.POOR
                    if tag == "missing":
                        return WordTag.MISSING
                    return WordTag.OK

                def _to_phoneme_tag(raw: Any) -> PhonemeTag:
                    tag = str(raw or "ok").strip().lower()
                    if tag == "poor":
                        return PhonemeTag.POOR
                    if tag == "weak":
                        return PhonemeTag.WEAK
                    return PhonemeTag.OK

                words = []
                for row in words_data:
                    if not isinstance(row, dict):
                        continue
                    words.append(
                        WordAlignment(
                            word=str(row.get("word", "") or ""),
                            start=float(row.get("start", 0.0) or 0.0),
                            end=float(row.get("end", 0.0) or 0.0),
                            score=float(row.get("score", 0.0) or 0.0),
                            tag=_to_word_tag(row.get("tag")),
                        )
                    )

                phonemes = []
                if isinstance(phonemes_data, list):
                    for p in phonemes_data:
                        if not isinstance(p, dict):
                            continue
                        symbol = str(p.get("phoneme", "") or "").strip()
                        if not symbol:
                            continue
                        phonemes.append(
                            PhonemeAlignment(
                                phoneme=symbol,
                                start=float(p.get("start", 0.0) or 0.0),
                                end=float(p.get("end", 0.0) or 0.0),
                                tag=_to_phoneme_tag(p.get("tag")),
                                score=float(p.get("score", 0.0) or 0.0),
                                in_word=str(p.get("in_word", "") or ""),
                            )
                        )

                alignment = Alignment(words=words, phonemes=phonemes)
                ensure_dense_phoneme_alignment(alignment)
                alignment_data["phonemes"] = [
                    {
                        "phoneme": p.phoneme,
                        "start": round(float(p.start), 3),
                        "end": round(float(p.end), 3),
                        "tag": p.tag.value,
                        "score": round(float(p.score), 1),
                        "in_word": p.in_word,
                    }
                    for p in alignment.phonemes
                ]
                data["alignment"] = alignment_data
        except Exception as enrich_err:
            logger.warning(f"Phoneme enrichment skipped: {enrich_err}")

        build_feedback_optimization_state(data)
        if _should_schedule_feedback_optimization(data):
            _schedule_feedback_optimization(submission_id, json_path)

        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read report: {e}")


@app.post("/api/reports/{submission_id}/freeze-feedback")
async def freeze_report_feedback(submission_id: str):
    _validate_submission_id(submission_id)
    json_path = _find_report_json_path(submission_id)
    if not json_path or not json_path.exists():
        raise HTTPException(status_code=404, detail="Report data not found")

    try:
        current, state = _freeze_report_feedback(json_path, reason="single_export")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to freeze feedback: {exc}") from exc

    return {
        "status": "ok",
        "submission_id": submission_id,
        "feedback_optimization": state,
        "feedback_override": current.get("feedback_override"),
    }


@app.post("/api/reports/freeze-feedback-batch")
async def freeze_report_feedback_batch(payload: FeedbackFreezeBatchRequest):
    submission_ids = [
        str(item or "").strip()
        for item in (payload.submission_ids or [])
        if str(item or "").strip()
    ]
    reason = str(payload.reason or "").strip() or "batch_export"
    targets: list[tuple[str, Path]] = []

    if submission_ids:
        for submission_id in submission_ids:
            _validate_submission_id(submission_id)
            json_path = _find_report_json_path(submission_id)
            if json_path and json_path.exists():
                targets.append((submission_id, json_path))
    else:
        targets = [(path.stem, path) for path in REPORTS_DIR.glob("**/*.json") if path.exists()]

    frozen_ids: list[str] = []
    for submission_id, json_path in targets:
        try:
            _freeze_report_feedback(json_path, reason=reason)
            frozen_ids.append(submission_id)
        except Exception:
            logger.warning("Failed to freeze feedback for %s during batch export", submission_id, exc_info=True)

    return {
        "status": "ok",
        "reason": reason,
        "frozen_count": len(frozen_ids),
        "submission_ids": frozen_ids,
    }


@app.get("/api/reports/{submission_id}/phoneme-videos")
async def get_report_phoneme_videos(submission_id: str, top_n: int = 3, per_phoneme: int = 2):
    """
    根据报告中的 weak_phonemes 自动返回针对性发音视频链接。
    - 优先返回 COS 签名链接（若配置了 COS 签名参数）
    - 同时保留本地 /api/sos/assets 链接作为回退
    """
    _validate_submission_id(submission_id)
    json_path = _find_report_json_path(submission_id)
    if not json_path or not json_path.exists():
        raise HTTPException(status_code=404, detail="Report data not found")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read report JSON: {exc}") from exc

    analysis_obj = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    raw_weak = (analysis_obj.get("weak_phonemes") or [])
    raw_focus_words = (analysis_obj.get("weak_words") or [])
    focus_words: list[str] = []
    seen_focus: set[str] = set()
    for item in raw_focus_words if isinstance(raw_focus_words, list) else []:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen_focus:
            continue
        seen_focus.add(key)
        focus_words.append(text)
        if len(focus_words) >= 12:
            break

    weak_phonemes: list[str] = []
    seen_tokens: set[str] = set()
    for item in raw_weak if isinstance(raw_weak, list) else []:
        token = str(item or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen_tokens:
            continue
        seen_tokens.add(key)
        weak_phonemes.append(token)
        if len(weak_phonemes) >= max(1, min(int(top_n or 3), 12)):
            break

    if not weak_phonemes:
        return {
            "submission_id": submission_id,
            "available": False,
            "detail": "No weak_phonemes in report",
            "focus_words": focus_words,
            "weak_phonemes": [],
            "items": [],
        }

    with SOS_LIBRARY_LOCK:
        index = _sos_load_index()
    if not bool(index.get("available")):
        return {
            "submission_id": submission_id,
            "available": False,
            "detail": str(index.get("detail") or "SoS library unavailable"),
            "focus_words": focus_words,
            "weak_phonemes": weak_phonemes,
            "items": [],
        }

    clipped_per = max(1, min(int(per_phoneme or 2), 4))
    items: list[dict[str, Any]] = []
    for phoneme in weak_phonemes:
        matches = _sos_search_matches(index, phoneme, max(3, clipped_per))
        if not matches:
            items.append(
                {
                    "phoneme": phoneme,
                    "matched": False,
                    "matches": [],
                }
            )
            continue

        best = matches[0]
        videos = _pick_sos_video_rows(best, clipped_per)
        items.append(
            {
                "phoneme": phoneme,
                "matched": True,
                "folder": str(best.get("folder") or ""),
                "display": str(best.get("display") or ""),
                "matched_by": str(best.get("matched_by") or ""),
                "matches": videos,
            }
        )

    return {
        "submission_id": submission_id,
        "available": True,
        "cos_signed_enabled": bool(_get_cos_sign_client() is not None),
        "focus_words": focus_words,
        "weak_phonemes": weak_phonemes,
        "items": items,
    }


@app.get("/api/reports/{submission_id}/phoneme-video-message")
async def get_report_phoneme_video_message(submission_id: str, top_n: int = 3, per_phoneme: int = 2, max_links: int = 3):
    """
    生成可直接发送给家长的文本消息（含针对性发音视频链接）。
    """
    payload = await get_report_phoneme_videos(submission_id=submission_id, top_n=top_n, per_phoneme=per_phoneme)
    items = payload.get("items") or []
    links = _pick_parent_push_links(items if isinstance(items, list) else [], max_links=max_links)
    focus_words = payload.get("focus_words") or []
    if not isinstance(focus_words, list):
        focus_words = []

    json_path = _find_report_json_path(submission_id)
    student_name = ""
    if json_path and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                student_name = str(data.get("student_name") or data.get("student") or "").strip()
        except Exception:
            student_name = ""

    weak_phonemes = payload.get("weak_phonemes") or []
    if not isinstance(weak_phonemes, list):
        weak_phonemes = []

    message_text = _build_parent_push_message(
        student_name=student_name,
        focus_words=[str(x or "").strip() for x in focus_words if str(x or "").strip()],
        weak_phonemes=[str(x or "") for x in weak_phonemes],
        links=links,
    )

    return {
        "submission_id": submission_id,
        "student_name": student_name,
        "focus_words": focus_words,
        "weak_phonemes": weak_phonemes,
        "link_count": len(links),
        "links": links,
        "message_text": message_text,
        "source": payload,
    }


@app.post("/api/reports/{submission_id}/feedback-override")
async def update_report_feedback_override(submission_id: str, payload: FeedbackOverrideRequest):
    _validate_submission_id(submission_id)
    json_path = _find_report_json_path(submission_id)
    if not json_path or not json_path.exists():
        raise HTTPException(status_code=404, detail="Report data not found")

    text = str(payload.integrated_feedback_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="integrated_feedback_text is required")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="integrated_feedback_text too long (max 4000 chars)")

    updated_by = str(payload.updated_by or "").strip()
    override: Dict[str, Any] = {
        "integrated_feedback_text": text,
        "updated_at": int(time.time()),
    }
    if updated_by:
        override["updated_by"] = updated_by[:60]

    try:
        current = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
        current["feedback_override"] = override
        _write_json_atomic(json_path, current)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update feedback override: {e}")

    return {
        "status": "ok",
        "submission_id": submission_id,
        "feedback_override": override,
    }


@app.delete("/api/reports/{submission_id}/feedback-override")
async def clear_report_feedback_override(submission_id: str):
    _validate_submission_id(submission_id)
    json_path = _find_report_json_path(submission_id)
    if not json_path or not json_path.exists():
        raise HTTPException(status_code=404, detail="Report data not found")

    try:
        current = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
        had_override = bool(current.get("feedback_override"))
        current.pop("feedback_override", None)
        _write_json_atomic(json_path, current)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear feedback override: {e}")

    return {
        "status": "ok",
        "submission_id": submission_id,
        "cleared": had_override,
    }


@app.get("/api/teacher-phrases")
def get_teacher_phrases():
    try:
        with TEACHER_PHRASE_BANK_LOCK:
            bank = _read_teacher_phrase_bank_unlocked()
            items = _sorted_teacher_phrase_items(list(bank.get("items") or []))
        return {
            "status": "ok",
            "updated_at": _as_non_negative_int(bank.get("updated_at"), 0),
            "items": items,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to read teacher phrase bank: {e}")
        raise HTTPException(status_code=500, detail="Failed to read teacher phrase bank")


@app.post("/api/teacher-phrases")
def add_teacher_phrase(payload: TeacherPhraseCreateRequest):
    text = _normalize_teacher_phrase_text(payload.text)
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 220:
        raise HTTPException(status_code=400, detail="text too long (max 220 chars)")
    category = _normalize_teacher_phrase_category(payload.category)
    now_ts = int(time.time())

    try:
        with TEACHER_PHRASE_BANK_LOCK:
            bank = _read_teacher_phrase_bank_unlocked()
            items = list(bank.get("items") or [])
            text_key = text.lower()
            for idx, row in enumerate(items):
                existing = _normalize_teacher_phrase_item(row, now_ts)
                if not existing:
                    continue
                if existing["text"].lower() == text_key:
                    if existing.get("category") != category:
                        existing["category"] = category
                        existing["updated_at"] = now_ts
                        items[idx] = existing
                        bank["items"] = items
                        bank["updated_at"] = now_ts
                        _write_json_atomic(TEACHER_PHRASE_BANK_PATH, bank)
                    return {"status": "ok", "created": False, "item": existing}

            new_item: Dict[str, Any] = {
                "id": f"ph_{uuid.uuid4().hex[:12]}",
                "text": text,
                "category": category,
                "use_count": 0,
                "created_at": now_ts,
                "updated_at": now_ts,
                "last_used_at": 0,
                "builtin": False,
            }
            items.append(new_item)
            bank["items"] = items
            bank["updated_at"] = now_ts
            _write_json_atomic(TEACHER_PHRASE_BANK_PATH, bank)
            return {"status": "ok", "created": True, "item": new_item}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add teacher phrase: {e}")
        raise HTTPException(status_code=500, detail="Failed to add teacher phrase")


@app.post("/api/teacher-phrases/use")
def mark_teacher_phrase_used(payload: TeacherPhraseUseRequest):
    phrase_id = str(payload.phrase_id or "").strip()
    if not phrase_id:
        raise HTTPException(status_code=400, detail="phrase_id is required")
    now_ts = int(time.time())

    try:
        with TEACHER_PHRASE_BANK_LOCK:
            bank = _read_teacher_phrase_bank_unlocked()
            items = list(bank.get("items") or [])
            for idx, row in enumerate(items):
                item = _normalize_teacher_phrase_item(row, now_ts)
                if not item or item.get("id") != phrase_id:
                    continue
                item["use_count"] = _as_non_negative_int(item.get("use_count"), 0) + 1
                item["last_used_at"] = now_ts
                item["updated_at"] = now_ts
                items[idx] = item
                bank["items"] = items
                bank["updated_at"] = now_ts
                _write_json_atomic(TEACHER_PHRASE_BANK_PATH, bank)
                return {"status": "ok", "item": item}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to mark teacher phrase usage: {e}")
        raise HTTPException(status_code=500, detail="Failed to update teacher phrase usage")

    raise HTTPException(status_code=404, detail="phrase_id not found")


@app.delete("/api/teacher-phrases/{phrase_id}")
def delete_teacher_phrase(phrase_id: str):
    try:
        deleted = _delete_teacher_phrase_from_bank(phrase_id)
        return {"status": "ok", "deleted": True, "item": deleted}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete teacher phrase: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete teacher phrase")


_WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _normalize_word_tokens(raw: Any) -> list[str]:
    text = str(raw or "").strip().lower()
    if not text:
        return []
    return [token for token in _WORD_TOKEN_PATTERN.findall(text) if len(token) >= 2]


def _extract_report_student_name(payload: dict[str, Any], report_path: Path) -> str:
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    if isinstance(meta, dict):
        for key in ("student_id", "student_name"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
    # data/out/{task}/{student}/{submission}/{submission}.json
    if len(report_path.parts) >= 3:
        return str(report_path.parent.parent.name or "").strip()
    return ""


def _canonical_upload_name(filename: str) -> str:
    name = str(filename or "").strip()
    if not name:
        return ""
    stem = Path(name).stem.strip().lower()
    if not stem:
        return ""
    # Normalize duplicate suffix styles from OS/browser uploads:
    # foo_01, foo-2, foo (3)
    stem = re.sub(r"(?:[_\-\s]0*\d{1,3}|\(\d{1,3}\))$", "", stem).strip()
    return stem


def _submission_filename_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for job in JOBS.values():
        submission = str(getattr(job, "submission_id", "") or "").strip()
        filename = str(getattr(job, "filename", "") or "").strip()
        if submission and filename:
            lookup[submission] = filename
    return lookup


def _collect_report_focus_words(payload: dict[str, Any]) -> set[str]:
    words: set[str] = set()
    if not isinstance(payload, dict):
        return words

    advisor_feedback = payload.get("advisor_feedback") if isinstance(payload.get("advisor_feedback"), dict) else {}
    engine_raw = payload.get("engine_raw") if isinstance(payload.get("engine_raw"), dict) else {}
    integrated_feedback = (
        engine_raw.get("integrated_feedback") if isinstance(engine_raw.get("integrated_feedback"), dict) else {}
    )

    for top_errors in (advisor_feedback.get("top_errors"), integrated_feedback.get("top_errors")):
        if not isinstance(top_errors, list):
            continue
        for item in top_errors:
            if not isinstance(item, dict):
                continue
            raw_candidates: list[Any] = []
            raw_words = item.get("words")
            if isinstance(raw_words, list):
                raw_candidates.extend(raw_words)
            elif isinstance(raw_words, str):
                raw_candidates.append(raw_words)
            for field in ("word", "target_word", "target", "token"):
                if item.get(field):
                    raw_candidates.append(item.get(field))
            for raw in raw_candidates:
                words.update(_normalize_word_tokens(raw))

    return words


@app.get("/api/pronunciation/focus-words")
async def get_pronunciation_focus_words(
    student: Optional[str] = None,
    min_count: int = 2,
    limit: int = 12,
    recent_reports: int = 400,
):
    """
    Aggregate frequently mispronounced words from recent report JSON files.
    """
    student_filter = str(student or "").strip()
    min_count = max(1, min(int(min_count or 2), 20))
    limit = max(1, min(int(limit or 12), 50))
    recent_reports = max(20, min(int(recent_reports or 400), 3000))

    candidates = [p for p in REPORTS_DIR.glob("**/*.json") if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    report_files = candidates[:recent_reports]

    head_signature = ""
    if report_files:
        newest = report_files[0]
        try:
            head_signature = f"{newest.as_posix()}:{newest.stat().st_mtime_ns}:{len(report_files)}"
        except Exception:
            head_signature = f"{newest.as_posix()}:{len(report_files)}"
    cache_key = "|".join(
        [
            student_filter.lower(),
            str(min_count),
            str(limit),
            str(recent_reports),
            head_signature,
        ]
    )
    now = time.time()
    with PRON_FOCUS_CACHE_LOCK:
        cached = PRON_FOCUS_CACHE.get(cache_key)
        if cached and (now - cached[0]) <= PRON_FOCUS_CACHE_TTL_SECONDS:
            return cached[1]

    word_counter: Counter[str] = Counter()
    student_counter: dict[str, set[str]] = {}
    last_seen_ts: dict[str, float] = {}
    scanned_reports = 0
    matched_reports = 0
    deduplicated_reports = 0
    duplicate_reports_skipped = 0
    student_filter_lower = student_filter.lower()
    submission_to_filename = _submission_filename_lookup()
    seen_source_keys: set[str] = set()

    for report_file in report_files:
        try:
            payload = json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        scanned_reports += 1

        report_student = _extract_report_student_name(payload, report_file)
        if student_filter_lower and student_filter_lower not in report_student.lower():
            continue

        meta = payload.get("meta") if isinstance(payload, dict) else {}
        submission_id = str((meta or {}).get("submission_id") or "").strip() if isinstance(meta, dict) else ""
        original_filename = submission_to_filename.get(submission_id, "")
        canonical_name = _canonical_upload_name(original_filename)
        if canonical_name:
            source_key = f"{report_student.lower()}|{canonical_name}"
            if source_key in seen_source_keys:
                duplicate_reports_skipped += 1
                continue
            seen_source_keys.add(source_key)
            deduplicated_reports += 1

        report_words = _collect_report_focus_words(payload)
        if not report_words:
            continue

        matched_reports += 1
        mtime = report_file.stat().st_mtime
        for word_token in report_words:
            word_counter[word_token] += 1
            if report_student:
                student_counter.setdefault(word_token, set()).add(report_student)
            last_seen_ts[word_token] = max(last_seen_ts.get(word_token, 0.0), mtime)

    rows: list[dict[str, Any]] = []
    for token, count in word_counter.items():
        if count < min_count:
            continue
        rows.append(
            {
                "word": token,
                "count": int(count),  # appears in how many reports
                "student_count": len(student_counter.get(token, set())),
                "last_seen_ts": int(last_seen_ts.get(token, 0.0)),
            }
        )

    rows.sort(key=lambda item: (-item["count"], -item["last_seen_ts"], item["word"]))
    payload = {
        "student_filter": student_filter,
        "min_count": min_count,
        "limit": limit,
        "recent_reports": recent_reports,
        "scanned_reports": scanned_reports,
        "matched_reports": matched_reports,
        "deduplicated_reports": deduplicated_reports,
        "duplicate_reports_skipped": duplicate_reports_skipped,
        "words": rows[:limit],
    }

    with PRON_FOCUS_CACHE_LOCK:
        PRON_FOCUS_CACHE[cache_key] = (time.time(), payload)
        # Keep cache size bounded.
        if len(PRON_FOCUS_CACHE) > 24:
            oldest_keys = sorted(PRON_FOCUS_CACHE.items(), key=lambda item: item[1][0])[:6]
            for key, _ in oldest_keys:
                PRON_FOCUS_CACHE.pop(key, None)

    return payload


def _clear_playbook_cache() -> None:
    try:
        from src.advice import playbook as playbook_module

        loader = getattr(playbook_module, "_load_runtime_rows", None)
        if loader and hasattr(loader, "cache_clear"):
            loader.cache_clear()
    except Exception as e:
        logger.warning(f"Failed to clear playbook cache: {e}")


def _parse_playbook_table_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower() == "## runtime lookup table":
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 6:
            continue
        if cols[0].lower() == "key" or set(cols[0]) == {"-"}:
            continue
        rows.append(
            {
                "key": cols[0],
                "triggers": cols[1],
                "focus_words": cols[2],
                "technique": cols[3],
                "drill_20s": cols[4],
                "mnemonic": cols[5],
            }
        )
    return rows


def _split_csv_items(raw: str) -> list[str]:
    parts = re.split(r"[,\s/|]+", raw or "")
    out: list[str] = []
    for item in parts:
        token = item.strip().lower()
        if token and token not in out:
            out.append(token)
    return out


def _sanitize_cell(text: str) -> str:
    clean = (text or "").replace("|", "/").replace("\n", " ").replace("\r", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:180]


def _slugify_key(seed: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", seed or "")
    if not tokens:
        return "CUSTOM_RULE"
    return "_".join(tokens[:4]).upper()[:48]


def _normalize_playbook_idea(idea: str) -> dict[str, str]:
    text = re.sub(r"\s+", " ", (idea or "").strip())
    if not text:
        raise HTTPException(status_code=400, detail="idea is required")

    mapping: dict[str, str] = {}
    for seg in re.split(r"[;\n]+", text):
        part = seg.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
        else:
            continue
        key = k.strip().lower()
        value = v.strip()
        if not value:
            continue
        if key in {"key", "id"}:
            mapping["key"] = value
        elif key in {"triggers", "trigger", "keywords"}:
            mapping["triggers"] = value
        elif key in {"focus", "focus_words", "words"}:
            mapping["focus_words"] = value
        elif key in {"technique", "method"}:
            mapping["technique"] = value
        elif key in {"drill", "practice", "steps"}:
            mapping["drill_20s"] = value
        elif key in {"mnemonic", "memory", "hook"}:
            mapping["mnemonic"] = value

    words = re.findall(r"[A-Za-z]+(?:_[A-Za-z]+)?", text)
    lower_words: list[str] = []
    for w in words:
        token = w.lower()
        if token not in lower_words:
            lower_words.append(token)

    focus_words = mapping.get("focus_words", "")
    if not focus_words:
        focus_words = ",".join(lower_words[:8]) if lower_words else "custom_word"

    triggers = mapping.get("triggers", "")
    if not triggers:
        base = lower_words[:8] if lower_words else ["custom", "teacher_note"]
        triggers = ",".join(base)

    technique = mapping.get("technique", "")
    if not technique:
        low = text.lower()
        if "stress" in low:
            technique = "Stress-Beat Custom Drill"
        elif "pause" in low:
            technique = "Thought-Group Pause Drill"
        elif "link" in low:
            technique = "Connected Speech Custom Drill"
        else:
            technique = "Teacher Custom Pronunciation Drill"

    drill = mapping.get("drill_20s", "")
    if not drill:
        drill = text[:160]

    mnemonic = mapping.get("mnemonic", "")
    if not mnemonic:
        mnemonic = "Teacher custom memory hook."

    key_seed = mapping.get("key", "") or f"{technique}_{focus_words}"
    return {
        "key": _sanitize_cell(_slugify_key(key_seed)),
        "triggers": _sanitize_cell(",".join(_split_csv_items(triggers))),
        "focus_words": _sanitize_cell(",".join(_split_csv_items(focus_words))),
        "technique": _sanitize_cell(technique),
        "drill_20s": _sanitize_cell(drill),
        "mnemonic": _sanitize_cell(mnemonic),
    }


def _get_gemini_playbook_keys() -> list[str]:
    raw = config.get("engines.gemini.api_key") or os.getenv("GEMINI_API_KEY") or ""
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    if isinstance(raw, str):
        return [k.strip() for k in raw.split(",") if k.strip()]
    return []


def _extract_gemini_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else {}
        parts = content.get("parts") if isinstance(content, dict) else []
        for part in parts or []:
            value = part.get("text") if isinstance(part, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _safe_parse_json_block(text: str) -> dict[str, Any]:
    clean = (text or "").strip()
    if clean.startswith("```json"):
        clean = clean[7:]
    if clean.startswith("```"):
        clean = clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()
    return json.loads(clean) if clean else {}


def _ai_refine_playbook_row(idea: str, base_row: dict[str, str]) -> tuple[dict[str, str], str]:
    keys = _get_gemini_playbook_keys()
    if not keys:
        return base_row, "Gemini key not configured; used rule-based normalize."

    model = str(config.get("engines.gemini.model", "gemini-3-flash-preview") or "gemini-3-flash-preview").strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = f"""
Normalize this teacher idea into ONE compact JSON object for pronunciation playbook row.
Return JSON only, no markdown.
Required keys: key,triggers,focus_words,technique,drill_20s,mnemonic
Constraints:
- key: UPPER_SNAKE_CASE, short.
- triggers/focus_words: comma separated lowercase tokens.
- technique/drill_20s/mnemonic: concise actionable text.
- keep each value <= 180 chars.

Teacher idea:
{idea}

Rule-based draft:
{json.dumps(base_row, ensure_ascii=False)}
"""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }

    errors: list[str] = []
    for key in keys[:3]:
        try:
            resp = requests.post(
                endpoint,
                params={"key": key},
                json=payload,
                timeout=(4, 15),
            )
            if resp.status_code >= 400:
                errors.append(f"HTTP {resp.status_code}")
                continue
            data = resp.json()
            value = _extract_gemini_text(data)
            if not value:
                errors.append("empty_text")
                continue
            parsed = _safe_parse_json_block(value)
            if not isinstance(parsed, dict):
                errors.append("bad_json")
                continue
            refined = {
                "key": _sanitize_cell(_slugify_key(str(parsed.get("key", base_row["key"])))),
                "triggers": _sanitize_cell(",".join(_split_csv_items(str(parsed.get("triggers", base_row["triggers"]))))),
                "focus_words": _sanitize_cell(",".join(_split_csv_items(str(parsed.get("focus_words", base_row["focus_words"]))))),
                "technique": _sanitize_cell(str(parsed.get("technique", base_row["technique"]))),
                "drill_20s": _sanitize_cell(str(parsed.get("drill_20s", base_row["drill_20s"]))),
                "mnemonic": _sanitize_cell(str(parsed.get("mnemonic", base_row["mnemonic"]))),
            }
            if not refined["triggers"]:
                refined["triggers"] = base_row["triggers"]
            if not refined["focus_words"]:
                refined["focus_words"] = base_row["focus_words"]
            if not refined["technique"]:
                refined["technique"] = base_row["technique"]
            if not refined["drill_20s"]:
                refined["drill_20s"] = base_row["drill_20s"]
            if not refined["mnemonic"]:
                refined["mnemonic"] = base_row["mnemonic"]
            return refined, ""
        except Exception as e:
            errors.append(str(e))
            continue

    return base_row, f"Gemini refine failed; used rule-based normalize ({'; '.join(errors[:2])})"


def _append_playbook_row(md_text: str, row: dict[str, str]) -> tuple[str, dict[str, str]]:
    lines = md_text.splitlines()
    start_idx = -1
    for i, line in enumerate(lines):
        if line.strip().lower() == "## runtime lookup table":
            start_idx = i
            break
    if start_idx < 0:
        raise HTTPException(status_code=500, detail="Runtime Lookup Table not found in playbook")

    table_start = -1
    for i in range(start_idx + 1, len(lines)):
        if lines[i].strip().startswith("| key |"):
            table_start = i
            break
    if table_start < 0:
        raise HTTPException(status_code=500, detail="Playbook table header not found")

    table_end = table_start
    for i in range(table_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("|"):
            table_end = i
            continue
        if stripped.startswith("## "):
            break
        if stripped == "":
            table_end = i - 1
            break

    existing_keys = {
        item["key"].upper()
        for item in _parse_playbook_table_rows(md_text)
        if item.get("key")
    }

    base_key = row["key"].upper()
    final_key = base_key
    suffix = 1
    while final_key in existing_keys:
        suffix += 1
        final_key = f"{base_key}_{suffix}"

    saved = dict(row)
    saved["key"] = final_key
    new_row = (
        f"| {saved['key']} | {saved['triggers']} | {saved['focus_words']} | "
        f"{saved['technique']} | {saved['drill_20s']} | {saved['mnemonic']} |"
    )

    lines.insert(table_end + 1, new_row)
    return "\n".join(lines) + "\n", saved


@app.get("/api/playbook")
def get_playbook():
    try:
        if not PLAYBOOK_PATH.exists():
            raise HTTPException(status_code=404, detail="Playbook file not found")
        text = PLAYBOOK_PATH.read_text(encoding="utf-8", errors="ignore")
        rows = _parse_playbook_table_rows(text)
        return {
            "path": str(PLAYBOOK_PATH),
            "entry_count": len(rows),
            "updated_at": PLAYBOOK_PATH.stat().st_mtime,
            "text": text,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load playbook: {e}")


@app.put("/api/playbook", dependencies=[Depends(require_admin_token_if_configured)])
def update_playbook(payload: PlaybookUpdateRequest):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if "| key | triggers | focus_words | technique | drill_20s | mnemonic |" not in text:
        raise HTTPException(status_code=400, detail="Playbook text missing required Runtime Lookup table header")

    try:
        with PLAYBOOK_LOCK:
            PLAYBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
            PLAYBOOK_PATH.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        _clear_playbook_cache()
        return {"status": "ok", "entry_count": len(_parse_playbook_table_rows(text))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update playbook: {e}")


@app.post("/api/playbook/ideas", dependencies=[Depends(require_admin_token_if_configured)])
def append_playbook_idea(payload: PlaybookIdeaRequest):
    row = _normalize_playbook_idea(payload.idea)
    refine_warning = ""
    source = "rule"
    if payload.ai_refine:
        row, refine_warning = _ai_refine_playbook_row(payload.idea, row)
        if not refine_warning:
            source = "gemini"
    try:
        with PLAYBOOK_LOCK:
            if not PLAYBOOK_PATH.exists():
                raise HTTPException(status_code=404, detail="Playbook file not found")
            current = PLAYBOOK_PATH.read_text(encoding="utf-8", errors="ignore")
            updated_text, saved = _append_playbook_row(current, row)
            PLAYBOOK_PATH.write_text(updated_text, encoding="utf-8")
        _clear_playbook_cache()
        return {"status": "ok", "entry": saved, "source": source, "warning": refine_warning}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to append playbook idea: {e}")


@app.post("/api/script-reference")
async def prepare_script_reference(payload: ScriptReferenceRequest):
    """
    Prebuild script pronunciation reference cache without uploading audio.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    try:
        from src.pipeline.script_reference import (
            build_local_script_reference,
            ensure_script_reference_async,
            load_script_reference,
            render_preheat_text,
            script_reference_hash,
            wait_for_script_reference,
        )

        script_hash = script_reference_hash(text)
        cached = load_script_reference(text)
        cached_is_modern = bool(
            cached
            and int((cached or {}).get("version", 0) or 0) >= 5
            and isinstance((cached or {}).get("pronunciation_rules"), list)
            and isinstance((cached or {}).get("pause_rules"), list)
            and isinstance((cached or {}).get("pace_norm"), dict)
        )
        ensure_script_reference_async(text)
        fallback_data = build_local_script_reference(text, script_hash=script_hash)
        ready_data = cached if cached_is_modern else fallback_data

        if payload.wait and cached_is_modern and not ready_data:
            timeout_sec = max(0.0, min(60.0, float(payload.timeout_sec or 0.0)))
            if timeout_sec > 0:
                ready_data = wait_for_script_reference(text, timeout_sec=timeout_sec)

        return {
            "status": "ready" if ready_data else "scheduled",
            "ready": bool(ready_data),
            "script_hash": script_hash,
            "cache_path": f"data/script_references/{script_hash}.json",
            "version": (ready_data or {}).get("version", 0),
            "model": (ready_data or {}).get("model", ""),
            "unique_word_count": (ready_data or {}).get("unique_word_count", 0),
            "pronunciation_rule_count": len((ready_data or {}).get("pronunciation_rules", []) or []),
            "pause_rule_count": len((ready_data or {}).get("pause_rules", []) or []),
            "pace_norm_ready": bool((ready_data or {}).get("pace_norm")),
            "preheat_text": render_preheat_text(text, ready_data) if ready_data else "",
            "enhancing": not cached_is_modern,
        }
    except Exception as e:
        logger.error(f"Failed to prebuild script reference: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to prebuild script reference: {e}")


WORD_CLIP_ALLOWED_DOMAINS = {
    "education",
    "technology",
    "entertainment",
    "business",
    "sports",
    "news",
    "phoneme_demo",
}


def _word_clip_update(job_id: str, **fields: Any) -> None:
    with WORD_CLIP_LOCK:
        item = WORD_CLIP_JOBS.get(job_id)
        if not item:
            return
        item.update(fields)
        item["updated_at"] = time.time()


def _run_word_clip_job(job_id: str, payload: WordClipJobRequest) -> None:
    try:
        from src.tools.word_clip_compiler import compile_word_clip_package

        job_root = Path("data/word_clips") / job_id
        job_root.mkdir(parents=True, exist_ok=True)
        _word_clip_update(job_id, status="queued", message="Queued (waiting for worker)...", progress=0.0)

        with WORD_CLIP_WORKER_SEMAPHORE:
            _word_clip_update(job_id, status="processing", message="Starting pipeline...", progress=0.02)

            def progress_cb(value: float, message: str) -> None:
                _word_clip_update(job_id, progress=round(value * 100.0, 1), message=message)

            result = compile_word_clip_package(
                word=payload.word,
                domain=payload.domain,
                domains=list(payload.domains or []),
                max_videos=payload.video_count,
                clip_seconds=payload.clip_seconds,
                source_mode=payload.source,
                include_cambridge=bool(payload.include_cambridge),
                job_dir=job_root,
                progress=progress_cb,
            )

        _word_clip_update(
            job_id,
            status="completed",
            progress=100.0,
            message="Completed",
            clips_generated=int(result.get("clips_generated", 0) or 0),
            videos_scanned=int(result.get("videos_scanned", 0) or 0),
            video_path=str(result.get("video_path") or ""),
            audio_path=str(result.get("audio_path") or ""),
            manifest_path=str(result.get("manifest_path") or ""),
        )
    except Exception as exc:
        logger.exception("Word clip job failed: %s", exc)
        _word_clip_update(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
        )


def _extract_youtube_video_id(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return (parsed.path or "").strip("/").split("/")[0][:11]
    if "youtube.com" in host:
        qs = parse_qs(parsed.query or "")
        value = (qs.get("v") or [""])[0]
        if value:
            return value[:11]
        parts = [segment for segment in (parsed.path or "").split("/") if segment]
        if len(parts) >= 2 and parts[0] in {"embed", "shorts"}:
            return parts[1][:11]
    return ""


def _build_youtube_embed_url(video_id: str, start_seconds: float | int | None = None) -> str:
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", str(video_id or ""))[:11]
    if not clean_id:
        return ""
    start = int(round(float(start_seconds or 0)))
    # Use youtube.com embed so browser login/session cookies can be reused.
    base = f"https://www.youtube.com/embed/{clean_id}?rel=0&modestbranding=1&playsinline=1"
    if start > 0:
        return f"{base}&start={start}"
    return base


def _normalize_clip_query(raw_word: str) -> str:
    text = str(raw_word or "").strip()
    text = re.sub(r"[^a-zA-Z0-9' -]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _extract_sos_rel_from_asset_url(asset_url: str) -> str:
    token = str(asset_url or "").strip()
    marker = "/api/sos/assets/"
    idx = token.find(marker)
    if idx < 0:
        return ""
    return _sos_safe_relative_path(token[idx + len(marker) :])


def _rel_to_cos_object_key(rel_path: str) -> str:
    rel = _sos_safe_relative_path(rel_path)
    if not rel:
        return ""
    if rel.startswith("phonemes/"):
        rel = rel[len("phonemes/") :]
    prefix = COS_SIGN_KEY_PREFIX.strip("/")
    if prefix:
        return f"{prefix}/{rel}"
    return rel


def _get_cos_sign_client() -> Any:
    global COS_SIGN_CLIENT, COS_SIGN_CLIENT_KEY
    if not COS_SIGN_ENABLED:
        return None
    if not COS_SIGN_BUCKET or not COS_SIGN_REGION or not COS_SIGN_SECRET_ID or not COS_SIGN_SECRET_KEY:
        return None
    if CosConfig is None or CosS3Client is None:
        return None

    cache_key = "|".join([COS_SIGN_REGION, COS_SIGN_BUCKET, COS_SIGN_SECRET_ID, COS_SIGN_SECRET_KEY])
    with COS_SIGN_CLIENT_LOCK:
        if COS_SIGN_CLIENT is not None and COS_SIGN_CLIENT_KEY == cache_key:
            return COS_SIGN_CLIENT
        try:
            cfg = CosConfig(
                Region=COS_SIGN_REGION,
                SecretId=COS_SIGN_SECRET_ID,
                SecretKey=COS_SIGN_SECRET_KEY,
                Scheme="https",
            )
            COS_SIGN_CLIENT = CosS3Client(cfg)
            COS_SIGN_CLIENT_KEY = cache_key
            return COS_SIGN_CLIENT
        except Exception as exc:
            logger.warning("COS sign client init failed: %s", exc)
            COS_SIGN_CLIENT = None
            COS_SIGN_CLIENT_KEY = ""
            return None


def _build_signed_cos_url_from_rel(rel_path: str, expire_seconds: Optional[int] = None) -> str:
    client = _get_cos_sign_client()
    if client is None:
        return ""
    key = _rel_to_cos_object_key(rel_path)
    if not key:
        return ""
    ttl = max(60, int(expire_seconds or COS_SIGN_EXPIRE_SECONDS))
    try:
        return str(
            client.get_presigned_download_url(
                Bucket=COS_SIGN_BUCKET,
                Key=key,
                Expired=ttl,
                SignHost=True,
            )
            or ""
        ).strip()
    except Exception as exc:
        logger.warning("COS signed URL generation failed for %s: %s", key, exc)
        return ""


SOS_PHONEME_FOLDER_MAP: dict[str, list[str]] = {
    "p": ["p-sound"],
    "b": ["b-sound"],
    "t": ["t-sound"],
    "d": ["d-sound"],
    "k": ["k-sound"],
    "g": ["g-sound"],
    "f": ["f-sound"],
    "v": ["v-sound"],
    "s": ["s-sound"],
    "z": ["z-sound"],
    "h": ["h-sound"],
    "m": ["m-sound"],
    "n": ["n-sound"],
    "ng": ["ng-sound"],
    "l": ["l-sound"],
    "r": ["r-sound"],
    "th": ["theta-sound"],
    "dh": ["eth-sound"],
    "sh": ["sch-sound"],
    "zh": ["zh-sound"],
    "ch": ["ch-sound"],
    "j": ["y-sound"],
    "jh": ["dzh-sound"],
    "w": ["w-sound"],
    "y": ["y-sound"],
    "theta": ["theta-sound"],
    "eth": ["eth-sound"],
    "dzh": ["dzh-sound"],
    "dj": ["dzh-sound"],
    "ae": ["ae-sound"],
    "a": ["short-a-sound", "long-a-sound"],
    "e": ["short-e-sound", "long-e-sound"],
    "i": ["short-i-sound", "long-e-sound", "i-sound"],
    "o": ["short-o-sound", "long-o-sound"],
    "u": ["short-u-sound", "long-u-sound", "long-ue-sound"],
    "schwa": ["schwa-sound"],
    "er": ["er-sound"],
    "ai": ["ai-sound"],
    "au": ["au-sound"],
    "oi": ["oi-sound"],
    "glottal": ["glottal-stop"],
}


def _normalize_sos_phoneme_token(raw: str) -> str:
    token = str(raw or "").strip().lower()
    token = token.replace("/", "").replace(" ", "")
    substitutions = {
        "tʃ": "ch",
        "dʒ": "jh",
        "θ": "th",
        "ð": "dh",
        "ʃ": "sh",
        "ʒ": "zh",
        "ŋ": "ng",
        "æ": "ae",
        "ə": "schwa",
        "ɚ": "er",
        "ɝ": "er",
        "ɑ": "a",
        "ɒ": "o",
        "ɔ": "o",
        "ʌ": "u",
        "ʊ": "u",
        "ɪ": "i",
        "ː": "",
    }
    for src, dst in substitutions.items():
        token = token.replace(src, dst)
    token = re.sub(r"[^a-z0-9-]+", "", token)
    return token


def _sos_safe_relative_path(raw: str) -> str:
    token = unquote(str(raw or "")).strip().replace("\\", "/")
    token = token.lstrip("/")
    if not token or ".." in token.split("/"):
        return ""
    return token


def _sos_load_index(force: bool = False) -> dict[str, Any]:
    global SOS_LIBRARY_CACHE, SOS_LIBRARY_CACHE_TS
    now = time.time()
    if not force and SOS_LIBRARY_CACHE and (now - SOS_LIBRARY_CACHE_TS) < SOS_LIBRARY_CACHE_TTL_SECONDS:
        return SOS_LIBRARY_CACHE

    root = SOS_LIBRARY_ROOT
    manifest = root / "manifest.csv"
    if not root.exists() or not manifest.exists():
        payload = {
            "available": False,
            "detail": f"SoS library not found at {root}",
            "root": str(root),
            "folder_count": 0,
            "file_count": 0,
            "folders": [],
        }
        SOS_LIBRARY_CACHE = payload
        SOS_LIBRARY_CACHE_TS = now
        return payload

    folder_rows: dict[str, dict[str, Any]] = {}
    file_count = 0
    with manifest.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = str(row.get("status") or "").strip().lower()
            if not status.startswith("ok"):
                continue
            folder = str(row.get("folder") or "").strip()
            kind = str(row.get("kind") or "").strip()
            rel = _sos_safe_relative_path(str(row.get("saved_path") or ""))
            if not folder or not kind or not rel:
                continue
            fp = root / rel
            if not fp.exists() or not fp.is_file():
                continue
            row_obj = folder_rows.setdefault(
                folder,
                {
                    "folder": folder,
                    "label": folder.replace("-sound", "").replace("-", " "),
                    "kinds": {},
                },
            )
            row_obj["kinds"][kind] = {
                "rel_path": rel,
                "url": f"/api/sos/assets/{quote(rel, safe='/')}",
            }
            file_count += 1

    folders = sorted(folder_rows.values(), key=lambda item: str(item.get("folder") or ""))
    payload = {
        "available": len(folders) > 0,
        "root": str(root),
        "folder_count": len(folders),
        "file_count": file_count,
        "folders": folders,
    }
    SOS_LIBRARY_CACHE = payload
    SOS_LIBRARY_CACHE_TS = now
    return payload


def _sos_pick_preview_urls(kinds: dict[str, Any]) -> tuple[str, str, list[str]]:
    animation_url = str(((kinds.get("animation") or {}).get("url")) or "")
    sound_url = str(((kinds.get("sound") or {}).get("url")) or "")
    samples: list[str] = []
    for key in ("word1", "word2", "word3", "word4", "word"):
        url = str(((kinds.get(key) or {}).get("url")) or "")
        if url and url not in samples:
            samples.append(url)
    return animation_url, sound_url, samples


def _sos_search_matches(index: dict[str, Any], raw_token: str, limit: int) -> list[dict[str, Any]]:
    folders = list(index.get("folders") or [])
    token = _normalize_sos_phoneme_token(raw_token)
    if not token:
        out = []
        for row in folders[: max(1, min(limit, 12))]:
            kinds = dict(row.get("kinds") or {})
            animation_url, sound_url, samples = _sos_pick_preview_urls(kinds)
            out.append(
                {
                    "folder": row.get("folder"),
                    "display": row.get("label") or row.get("folder"),
                    "matched_by": "default",
                    "animation_url": animation_url,
                    "sound_url": sound_url,
                    "sample_urls": samples,
                }
            )
        return out

    alias_targets = SOS_PHONEME_FOLDER_MAP.get(token, [])
    alias_set = set(alias_targets)
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in folders:
        folder = str(row.get("folder") or "")
        if not folder:
            continue
        base = folder.replace("-sound", "")
        compact = re.sub(r"[^a-z0-9]+", "", folder.lower())
        base_compact = re.sub(r"[^a-z0-9]+", "", base.lower())

        score = -1
        matched_by = ""
        if folder in alias_set:
            score = 100
            matched_by = "alias"
        elif token == base_compact:
            score = 90
            matched_by = "exact"
        elif token in base_compact:
            score = 80
            matched_by = "contains"
        elif token in compact:
            score = 72
            matched_by = "folder"
        elif any(part.startswith(token) for part in folder.lower().split("-")):
            score = 64
            matched_by = "prefix"
        elif token and len(token) >= 2 and any(token in part for part in folder.lower().split("-")):
            score = 58
            matched_by = "partial"
        if score < 0:
            continue

        kinds = dict(row.get("kinds") or {})
        animation_url, sound_url, samples = _sos_pick_preview_urls(kinds)
        scored.append(
            (
                score,
                {
                    "folder": folder,
                    "display": row.get("label") or folder,
                    "matched_by": matched_by,
                    "animation_url": animation_url,
                    "sound_url": sound_url,
                    "sample_urls": samples,
                },
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[: max(1, min(limit, 24))]]


def _pick_sos_video_rows(match: dict[str, Any], per_phoneme: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    animation_url = str(match.get("animation_url") or "").strip()
    sound_url = str(match.get("sound_url") or "").strip()
    if animation_url:
        candidates.append(("animation", animation_url))
    if sound_url:
        candidates.append(("sound", sound_url))
    for url in list(match.get("sample_urls") or []):
        token = str(url or "").strip()
        if token:
            candidates.append(("word", token))

    for kind, local_url in candidates:
        rel = _extract_sos_rel_from_asset_url(local_url)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        signed_url = _build_signed_cos_url_from_rel(rel)
        rows.append(
            {
                "kind": kind,
                "local_url": local_url,
                "asset_rel_path": rel,
                "signed_url": signed_url,
            }
        )
        if len(rows) >= max(1, per_phoneme):
            break
    return rows


def _pick_parent_push_links(items: list[dict[str, Any]], max_links: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    limit = max(1, min(int(max_links or 3), 8))
    for row in items:
        phoneme = str(row.get("phoneme") or "").strip() or "Key Sound"
        matches = row.get("matches") or []
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            url = str(match.get("signed_url") or match.get("local_url") or "").strip()
            if not url:
                continue
            kind = str(match.get("kind") or "video").strip()
            out.append(
                {
                    "phoneme": phoneme,
                    "kind": kind,
                    "url": url,
                }
            )
            break
        if len(out) >= limit:
            break
    return out


def _build_parent_push_message(
    *,
    student_name: str,
    focus_words: list[str],
    weak_phonemes: list[str],
    links: list[dict[str, str]],
) -> str:
    _ = student_name
    _ = focus_words  # Keep signature stable for callers.

    phoneme_text = "、".join(
        f"/{str(x or '').strip()}/" for x in weak_phonemes if str(x or "").strip()
    ) or "（本次无明显弱读音素）"

    video_parts: list[str] = []
    for link in links:
        phoneme = str(link.get("phoneme") or "Key Sound").strip()
        url = str(link.get("url") or "").strip()
        if not url:
            continue
        video_parts.append(f"{phoneme}: {url}")
    video_text = "；".join(video_parts) if video_parts else "（暂无可用视频链接）"

    lines = [
        "家长您好！以下是孩子发音时需要注意的：",
        f"弱读音素：{phoneme_text}",
        f"对应发音示范视频：{video_text}",
    ]
    return "\\n".join(lines)
def _resolve_precise_keyword_starts(
    *,
    word: str,
    source_rows: list[Any],
    max_items: int,
) -> tuple[dict[str, tuple[float, str]], str]:
    try:
        from src.tools.word_clip_compiler import (  # type: ignore
            _asr_keyword_window_starts,
            _download_video_and_vtt,
            _find_match_indexes,
            _parse_vtt_file,
        )
    except Exception as exc:
        return {}, f"precise locator unavailable: {str(exc)[:180]}"

    resolved: dict[str, tuple[float, str]] = {}
    warning = ""
    if not source_rows or max_items <= 0:
        return resolved, warning

    max_scan = max(4, min(10, max_items + 2))
    with tempfile.TemporaryDirectory(prefix="wordclip_precise_") as tmpdir:
        target_dir = Path(tmpdir)
        slot = 0
        for row in source_rows:
            if len(resolved) >= max_items:
                break
            if slot >= max_scan:
                break
            raw_url = str(getattr(row, "url", "") or "").strip()
            if not raw_url:
                continue
            video_id = _extract_youtube_video_id(raw_url)
            if not video_id or video_id in resolved:
                continue

            slot += 1
            try:
                bundle = _download_video_and_vtt(row, target_dir, slot)
            except Exception as exc:
                text = str(exc).strip()
                if text:
                    warning = (warning + f" | {text[:120]}").strip(" |")
                continue
            if not bundle:
                continue

            video_path, vtt_path = bundle
            start_seconds: float | None = None
            timing_source = ""
            if vtt_path and vtt_path.exists():
                try:
                    cues = _parse_vtt_file(vtt_path)
                    matches = _find_match_indexes(cues, word, max_count=1)
                    if matches:
                        cue = cues[matches[0]]
                        start_seconds = max(0.0, float(cue.start))
                        timing_source = "subtitle"
                except Exception:
                    start_seconds = None
                    timing_source = ""
            if start_seconds is None:
                try:
                    starts = _asr_keyword_window_starts(video_path, word, max_count=1)
                except Exception:
                    starts = []
                if starts:
                    start_seconds = max(0.0, float(starts[0]))
                    timing_source = "asr"

            if start_seconds is not None:
                resolved[video_id] = (start_seconds, timing_source or "heuristic")
    return resolved, warning


def _parse_youglish_snapshot(markdown_text: str, *, max_nearby: int = 12) -> dict[str, Any]:
    text = str(markdown_text or "")
    count = 0
    count_match = re.search(r"\|\s*(\d+)\s+pronunciations?\s+of\b", text, re.IGNORECASE)
    if count_match:
        try:
            count = int(count_match.group(1))
        except Exception:
            count = 0

    example_sentence = ""
    lines = [line.strip() for line in text.splitlines()]
    heading_idx = -1
    for idx, line in enumerate(lines):
        if line.lower().startswith("how to pronounce ") and " out of " in line.lower():
            heading_idx = idx
            break
    if heading_idx >= 0:
        banned_prefixes = (
            "![",
            "[[",
            "*",
            "speed:",
            "arrow_",
            "close",
            "definition:",
            "nearby words:",
            "phonetic:",
        )
        for line in lines[heading_idx + 1 : heading_idx + 80]:
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith(banned_prefixes):
                continue
            if line in {"•", "••", "•••", "×", "U"}:
                continue
            if not re.search(r"[a-zA-Z]", line):
                continue
            word_count = len(re.findall(r"[A-Za-z']+", line))
            if word_count < 5:
                continue
            example_sentence = line
            break

    nearby_words: list[dict[str, str]] = []
    seen_words: set[str] = set()
    nearby_start = text.lower().find("nearby words:")
    scan_block = text[nearby_start:] if nearby_start >= 0 else text
    for match in re.finditer(r"\*\s+\[([^\]]+)\]\((https?://youglish\.com/pronounce/[^)]+)\)", scan_block):
        token = str(match.group(1) or "").strip()
        link = str(match.group(2) or "").strip()
        key = token.lower()
        if not token or not link or key in seen_words:
            continue
        seen_words.add(key)
        nearby_words.append({"word": token, "url": link})
        if len(nearby_words) >= max_nearby:
            break

    return {
        "count": count,
        "example_sentence": example_sentence,
        "nearby_words": nearby_words,
    }


@app.get("/api/word-clips/youglish-snapshot")
async def get_youglish_snapshot(word: str, limit: int = 12):
    token = _normalize_clip_query(word)
    if not token:
        raise HTTPException(status_code=400, detail="word is required")

    clipped_limit = max(4, min(int(limit or 12), 24))
    source_url = f"https://youglish.com/pronounce/{quote(token)}/english"
    proxy_url = f"https://r.jina.ai/http://youglish.com/pronounce/{quote(token)}/english"

    warning = ""
    try:
        response = requests.get(
            proxy_url,
            timeout=22,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = _parse_youglish_snapshot(response.text, max_nearby=clipped_limit)
        available = bool(payload["count"] or payload["example_sentence"] or payload["nearby_words"])
    except Exception as exc:
        payload = {"count": 0, "example_sentence": "", "nearby_words": []}
        available = False
        warning = str(exc)[:220]

    return {
        "word": token,
        "available": available,
        "source": "youglish_snapshot",
        "source_url": source_url,
        "count": int(payload["count"]),
        "example_sentence": str(payload["example_sentence"] or ""),
        "nearby_words": payload["nearby_words"],
        "warning": warning,
    }


@app.get("/api/word-clips/online-sources")
async def get_word_clip_online_sources(word: str, limit: int = 8, accent: str = "all", precise: bool = False):
    token = _normalize_clip_query(word)
    if not token:
        raise HTTPException(status_code=400, detail="word is required")

    clipped_limit = max(2, min(int(limit or 8), 24))
    accent_token = str(accent or "all").strip().lower()
    if accent_token not in {"all", "us", "uk", "aus", "ca"}:
        accent_token = "all"

    try:
        from src.tools.word_clip_compiler import (  # type: ignore
            _search_sources,
            _search_youglish_sources,
            _search_youtube_api_sources,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Word clip module unavailable: {exc}") from exc

    source_rows: list[Any] = []
    source_name = "youglish"
    warning = ""
    try:
        source_rows = _search_youglish_sources(token, max_videos=clipped_limit)
    except Exception as exc:
        warning = str(exc)[:240]
        source_rows = []

    if not source_rows:
        source_name = "fallback_youtube"
        try:
            source_rows = _search_youtube_api_sources(token, ["phoneme_demo", "education"], clipped_limit)
            if source_rows:
                source_name = "fallback_youtube_api"
        except Exception as exc:
            warning = (warning + f" | {str(exc)[:180]}").strip(" |")
            source_rows = []
    if not source_rows:
        source_name = "fallback_youtube"
        try:
            source_rows = _search_sources(token, ["phoneme_demo", "education"], clipped_limit)
        except Exception as exc:
            detail = f"Online source search failed: {exc}"
            raise HTTPException(status_code=502, detail=detail[:500]) from exc

    precise_map: dict[str, tuple[float, str]] = {}
    if precise:
        precise_map, precise_warning = _resolve_precise_keyword_starts(
            word=token,
            source_rows=source_rows,
            max_items=clipped_limit,
        )
        if precise_warning:
            warning = (warning + f" | {precise_warning}").strip(" |")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in source_rows:
        raw_url = str(getattr(row, "url", "") or "").strip()
        if not raw_url:
            continue
        vid = _extract_youtube_video_id(raw_url)
        if not vid:
            continue
        default_start = float(getattr(row, "start_seconds", 0.0) or 0.0)
        precise_entry = precise_map.get(vid)
        start = float(precise_entry[0]) if precise_entry else default_start
        bucket = int(round(start))
        dedupe_key = f"{vid}:{bucket}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        embed_url = _build_youtube_embed_url(vid, start)
        if not embed_url:
            continue
        items.append(
            {
                "video_id": vid,
                "title": str(getattr(row, "title", "") or "Untitled clip"),
                "source_url": raw_url,
                "embed_url": embed_url,
                "start_seconds": bucket,
                "source_type": str(getattr(row, "source_type", "") or source_name),
                "timing_source": precise_entry[1] if precise_entry else ("youglish_hint" if default_start > 0 else "none"),
                "has_precise_timing": bool(precise_entry),
            }
        )
        if len(items) >= clipped_limit:
            break

    return {
        "word": token,
        "source": source_name,
        "accent": accent_token,
        "count": len(items),
        "warning": warning,
        "items": items,
    }


@app.get("/api/sos/status")
async def get_sos_status():
    with SOS_LIBRARY_LOCK:
        payload = _sos_load_index()
    return {
        "available": bool(payload.get("available")),
        "root": str(payload.get("root") or ""),
        "folder_count": int(payload.get("folder_count") or 0),
        "file_count": int(payload.get("file_count") or 0),
        "detail": str(payload.get("detail") or ""),
    }


@app.get("/api/sos/search")
async def search_sos_assets(phoneme: str = "", q: str = "", limit: int = 8):
    query_token = str(phoneme or q or "").strip()
    clipped_limit = max(1, min(int(limit or 8), 24))
    with SOS_LIBRARY_LOCK:
        payload = _sos_load_index()
    if not bool(payload.get("available")):
        return {
            "available": False,
            "query": query_token,
            "count": 0,
            "matches": [],
            "detail": str(payload.get("detail") or "SoS library unavailable"),
        }
    matches = _sos_search_matches(payload, query_token, clipped_limit)
    return {
        "available": True,
        "query": query_token,
        "count": len(matches),
        "matches": matches,
    }


@app.get("/api/sos/assets/{asset_path:path}")
async def get_sos_asset(asset_path: str):
    rel = _sos_safe_relative_path(asset_path)
    if not rel:
        raise HTTPException(status_code=400, detail="Invalid asset path")
    root = SOS_LIBRARY_ROOT.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid asset path") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    media_type = "video/mp4" if target.suffix.lower() == ".mp4" else None
    return FileResponse(path=target, filename=target.name, media_type=media_type)


@app.post("/api/word-clips/po-token/extract-har")
async def extract_word_clip_po_token_from_har(
    har_file: UploadFile = File(...),
    token_key: str = Form("web.gvs"),
    merge_existing: bool = Form(True),
):
    name = str(har_file.filename or "").strip().lower()
    if not name.endswith(".har") and not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Please upload a .har file")

    raw = await har_file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(raw) > 220 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="HAR file is too large (max 220MB)")

    try:
        payload = json.loads(raw.decode("utf-8-sig", errors="ignore"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid HAR JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid HAR structure")

    tokens = _extract_po_tokens_from_har(payload)
    if not tokens:
        raise HTTPException(
            status_code=400,
            detail="No poToken/pot found in HAR. Please capture a playing YouTube video and retry.",
        )

    selected = max(tokens, key=len)
    key = _normalize_po_token_key(token_key)

    current: dict[str, str] = _load_saved_po_tokens() if merge_existing else {}

    current[key] = selected
    WORD_CLIP_PO_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORD_CLIP_PO_TOKEN_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    preview = selected[:12] + "..." + selected[-8:] if len(selected) > 24 else selected
    return {
        "status": "ok",
        "token_key": key,
        "tokens_found": len(tokens),
        "selected_token_preview": preview,
        "saved_path": str(WORD_CLIP_PO_TOKEN_PATH),
        "saved_keys": sorted(current.keys()),
    }


@app.get("/api/word-clips/po-token/status")
async def get_word_clip_po_token_status():
    tokens = _load_saved_po_tokens()
    updated_at = None
    if WORD_CLIP_PO_TOKEN_PATH.exists():
        try:
            updated_at = WORD_CLIP_PO_TOKEN_PATH.stat().st_mtime
        except Exception:
            updated_at = None
    return {
        "status": "ok",
        "exists": bool(tokens),
        "saved_path": str(WORD_CLIP_PO_TOKEN_PATH),
        "updated_at": updated_at,
        "keys": sorted(tokens.keys()),
        "token_previews": {key: _mask_token(value) for key, value in tokens.items()},
    }


@app.get("/api/word-clips/po-token/health-check")
async def check_word_clip_po_token_health(token_key: str = "web.gvs"):
    key = _normalize_po_token_key(token_key)
    tokens = _load_saved_po_tokens()
    if key not in tokens:
        raise HTTPException(
            status_code=400,
            detail=f"Token key '{key}' not found. Upload HAR first.",
        )

    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Missing yt-dlp: {exc}") from exc

    try:
        from src.tools.word_clip_compiler import _apply_yt_dlp_common_options  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Word clip module unavailable: {exc}") from exc

    player_client = "android" if key.startswith("android.") else None
    test_urls = [
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=M7lc1UVf-VE",
    ]
    probes = [
        ("po_only", False),
        ("po_plus_cookies", True),
    ]
    probe_results: list[dict[str, Any]] = []
    for probe_name, use_cookies in probes:
        options: dict[str, Any] = {
            "quiet": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 18,
        }
        options = _apply_yt_dlp_common_options(
            options,
            use_cookies=use_cookies,
            use_node_runtime=True,
            player_client=player_client,
            use_impersonate=False,
            use_po_token=True,
        )

        success_payload: dict[str, Any] | None = None
        last_error = ""
        for test_url in test_urls:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                formats = (info or {}).get("formats") or []
                playable = [
                    item
                    for item in formats
                    if isinstance(item, dict)
                    and item.get("ext") != "mhtml"
                    and (item.get("vcodec") != "none" or item.get("acodec") != "none")
                ]
                success_payload = {
                    "probe": probe_name,
                    "ok": True,
                    "playable_formats": len(playable),
                    "total_formats": len(formats),
                    "video_id": str((info or {}).get("id") or ""),
                    "title": str((info or {}).get("title") or ""),
                    "test_url": test_url,
                }
                if playable:
                    probe_results.append(success_payload)
                    return {
                        "status": "ok",
                        "token_key": key,
                        "token_preview": _mask_token(tokens.get(key, "")),
                        "probe": probe_name,
                        "video_id": str((info or {}).get("id") or ""),
                        "title": str((info or {}).get("title") or ""),
                        "playable_formats": len(playable),
                        "total_formats": len(formats),
                        "hint": "Token is usable.",
                        "probe_results": probe_results,
                    }
            except Exception as exc:
                last_error = str(exc).strip() or repr(exc)

        if success_payload is not None:
            probe_results.append(success_payload)
        else:
            probe_results.append({"probe": probe_name, "ok": False, "error": last_error[:500]})

    if probe_results:
        best = max(
            [item for item in probe_results if item.get("ok")] or [{}],
            key=lambda item: int(item.get("playable_formats") or 0),
        )
        if best and int(best.get("playable_formats") or 0) == 0 and best.get("ok"):
            return {
                "status": "degraded",
                "token_key": key,
                "token_preview": _mask_token(tokens.get(key, "")),
                "probe": str(best.get("probe") or ""),
                "video_id": str(best.get("video_id") or ""),
                "title": str(best.get("title") or ""),
                "playable_formats": 0,
                "total_formats": int(best.get("total_formats") or 0),
                "hint": "Token loaded, but no playable formats. Refresh HAR token.",
                "probe_results": probe_results,
            }
        msg = str((probe_results[-1] or {}).get("error") or "").strip() or "Unknown upstream error"
        low = msg.lower()
        if "not a bot" in low or "sign in to confirm" in low:
            hint = "Token appears expired/invalid. Re-export HAR and extract again."
        elif "po token" in low:
            hint = "PO Token mismatch. Try updating both web.gvs and android.gvs."
        else:
            hint = "Health check failed due to upstream/network condition."
        return {
            "status": "failed",
            "token_key": key,
            "token_preview": _mask_token(tokens.get(key, "")),
            "error": msg[:1200],
            "hint": hint,
            "probe_results": probe_results,
        }
    return {
        "status": "failed",
        "token_key": key,
        "token_preview": _mask_token(tokens.get(key, "")),
        "error": "No probe result",
        "hint": "Health check failed due to upstream/network condition.",
    }


@app.post("/api/word-clips/jobs")
async def create_word_clip_job(payload: WordClipJobRequest):
    word = (payload.word or "").strip()
    if not word:
        raise HTTPException(status_code=400, detail="word is required")

    source_mode = str(payload.source or "youglish").strip().lower()
    if source_mode not in {"youglish", "hybrid"}:
        raise HTTPException(status_code=400, detail="source must be one of: youglish, hybrid")

    raw_domains = payload.domains if isinstance(payload.domains, list) and payload.domains else [payload.domain]
    clean_domains: list[str] = []
    if source_mode == "youglish":
        clean_domains = ["phoneme_demo"]
        primary_domain = "youglish"
    else:
        for item in raw_domains:
            token = str(item or "").strip().lower()
            if not token:
                continue
            if token not in WORD_CLIP_ALLOWED_DOMAINS:
                raise HTTPException(
                    status_code=400,
                    detail=f"domain must be one of: {', '.join(sorted(WORD_CLIP_ALLOWED_DOMAINS))}",
                )
            if token not in clean_domains:
                clean_domains.append(token)
        if not clean_domains:
            clean_domains = ["education"]
        primary_domain = clean_domains[0]

    video_count = max(1, min(int(payload.video_count or 4), 20))
    clip_seconds = max(4.0, min(float(payload.clip_seconds or 5.0), 8.0))
    include_cambridge = bool(payload.include_cambridge)

    job_id = uuid.uuid4().hex
    now = time.time()
    with WORD_CLIP_LOCK:
        WORD_CLIP_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "word": word,
            "domain": primary_domain,
            "domains": clean_domains,
            "source": source_mode,
            "include_cambridge": include_cambridge,
            "video_count": video_count,
            "clip_seconds": clip_seconds,
            "progress": 0.0,
            "message": "Queued",
            "error": "",
            "clips_generated": 0,
            "videos_scanned": 0,
            "video_path": "",
            "audio_path": "",
            "manifest_path": "",
            "created_at": now,
            "updated_at": now,
        }

    normalized_payload = WordClipJobRequest(
        word=word,
        domain=primary_domain,
        domains=clean_domains,
        video_count=video_count,
        clip_seconds=clip_seconds,
        source=source_mode,
        include_cambridge=include_cambridge,
    )
    worker_thread = threading.Thread(target=_run_word_clip_job, args=(job_id, normalized_payload), daemon=True)
    worker_thread.start()
    return {"status": "queued", "job_id": job_id}


@app.get("/api/word-clips/jobs/{job_id}")
async def get_word_clip_job(job_id: str):
    with WORD_CLIP_LOCK:
        job = dict(WORD_CLIP_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="Word clip job not found")

    if job.get("video_path"):
        job["video_download_url"] = f"/api/word-clips/jobs/{job_id}/download/video"
    if job.get("audio_path"):
        job["audio_download_url"] = f"/api/word-clips/jobs/{job_id}/download/audio"
    return job


@app.get("/api/word-clips/jobs/{job_id}/download/{asset}")
async def download_word_clip_asset(job_id: str, asset: str):
    if asset not in {"video", "audio"}:
        raise HTTPException(status_code=400, detail="asset must be video or audio")

    with WORD_CLIP_LOCK:
        job = dict(WORD_CLIP_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="Word clip job not found")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed yet")

    path_key = "video_path" if asset == "video" else "audio_path"
    target_path = Path(str(job.get(path_key) or ""))
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail=f"{asset} output not found")

    media_type = "video/mp4" if asset == "video" else "audio/mpeg"
    return FileResponse(path=target_path, filename=target_path.name, media_type=media_type)


@app.post("/api/upload")
async def upload_audio(
    file: UploadFile = File(...),
    text: str = Form(""),
    mode: str = Form("auto"),
):
    """
    Async Upload: Saves file and queues job. Returns Job ID immediately.
    """
    import time
    from datetime import datetime
    import hashlib
    import uuid
    import json # For serialization in save_jobs
    from src.models import EngineMode
    
    # Opportunistic cleanup to keep temp workspace bounded over time.
    maybe_cleanup_work_tmp_dirs(force=False)

    raw_filename = str(file.filename or "").strip()
    filename_suffix = Path(raw_filename).suffix.lower()
    content_type = str(file.content_type or "").strip().lower()
    if not (content_type.startswith("audio/") or filename_suffix in ALLOWED_AUDIO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported file type. Please upload audio files "
                "(mp3/wav/m4a/aac/flac/ogg/opus)."
            ),
        )

    # Generate IDs
    job_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_hash = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    submission_id = f"web_{timestamp}_{random_hash}"
    
    # Save Upload
    date_str = datetime.now().strftime("%Y%m%d")
    upload_dir = Path("data/uploads") / date_str
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = upload_dir / f"{submission_id}.mp3"
    
    try:
        size_bytes = 0
        with open(file_path, "wb") as buffer:
            while True:
                chunk = file.file.read(UPLOAD_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                next_size = size_bytes + len(chunk)
                if next_size > UPLOAD_MAX_BYTES:
                    max_mb_text = f"{UPLOAD_MAX_MB:g}"
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (>{max_mb_text}MB). Maximum allowed is {max_mb_text}MB.",
                    )
                buffer.write(chunk)
                size_bytes = next_size
        if size_bytes <= 0:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(status_code=400, detail="Empty file is not allowed.")
        logger.info(f"File saved to {file_path}")
        
        # Save Text Sidecar for persistence
        if text:
            txt_path = upload_dir / f"{submission_id}.txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            # Prebuild script pronunciation reference asynchronously.
            try:
                from src.pipeline.script_reference import ensure_script_reference_async
                ensure_script_reference_async(text)
            except Exception as ref_err:
                logger.warning(f"Script reference prebuild skipped: {ref_err}")
                
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    # Parse metadata
    import re
    fname_stem = Path(file.filename).stem
    parts = fname_stem.split('_', 1)
    
    if len(parts) == 2:
        raw_student = parts[0]
        raw_task = parts[1]
    else:
        raw_student = fname_stem
        raw_task = "upload"
        
    def safe_meta(s):
        # Allow alphanumeric, chinese, dashes, underscores
        return re.sub(r'[^\w\-\u4e00-\u9fff]', '_', s)
        
    student_id = safe_meta(raw_student)
    task_id = safe_meta(raw_task)
    
    logger.info(f"Upload: filename='{file.filename}' -> raw_student='{raw_student}' -> student_id='{student_id}'")
    
    try:
        target_mode = EngineMode(mode.lower())
    except ValueError:
        target_mode = EngineMode.AUTO
        
    # Handling auto mode text clearing is done in the frontend mostly now, 
    # but strictly if we want to ignore passed text for AUTO, we can unless we want to keep it reference.
    # The previous fix in server.py was:
    if target_mode == EngineMode.AUTO:
        # text = ""  <-- Disabling this to allow sidecar saving if needed, 
        # but the pipeline ignores it anyway if mode is FREE_SPEAKING?
        # Actually pipeline checks engine_mode.
        pass

    # Create Job Entry
    job = Job(
        id=job_id,
        status=JobStatus.QUEUED,
        submission_id=submission_id,
        student_id=student_id,
        task_id=task_id,
        filename=file.filename,
        timestamp=time.time(),
        mode=str(target_mode.value)
    )
    JOBS[job_id] = job
    save_jobs() # Save state
    
    # Enqueue
    metadata = {
        "student_id": student_id,
        "task_id": task_id,
        "submission_id": submission_id,
        "engine_mode": target_mode
    }
    
    await JOB_QUEUE.put((job_id, file_path, text, mode, metadata))
    logger.info(f"Job {job_id} queued for {submission_id}")
    
    return {
        "status": "queued",
        "job_id": job_id,
        "submission_id": submission_id,
        "queue_position": JOB_QUEUE.qsize()
    }

@app.get("/api/jobs/stats")
async def get_job_stats():
    """
    Lightweight job counters for dashboard/polling UIs.
    """
    counts: Dict[str, int] = {
        JobStatus.QUEUED.value: 0,
        JobStatus.PROCESSING.value: 0,
        JobStatus.COMPLETED.value: 0,
        JobStatus.FAILED.value: 0,
    }
    for job in JOBS.values():
        raw = job.status
        key = raw.value if isinstance(raw, JobStatus) else str(raw).strip().lower()
        if key in counts:
            counts[key] += 1

    total = int(sum(counts.values()))
    return {
        "status": "ok",
        "total": total,
        "queued": int(counts[JobStatus.QUEUED.value]),
        "processing": int(counts[JobStatus.PROCESSING.value]),
        "completed": int(counts[JobStatus.COMPLETED.value]),
        "failed": int(counts[JobStatus.FAILED.value]),
        "active": int(counts[JobStatus.QUEUED.value] + counts[JobStatus.PROCESSING.value]),
        "timestamp": time.time(),
    }


@app.get("/api/jobs/overview")
async def get_jobs_overview(
    active_limit: int = Query(default=200, ge=1, le=2000),
    failed_limit: int = Query(default=500, ge=1, le=5000),
):
    """
    Aggregated jobs payload for polling UIs:
    counters + active jobs + failed jobs in one request.
    """
    counts: Dict[str, int] = {
        JobStatus.QUEUED.value: 0,
        JobStatus.PROCESSING.value: 0,
        JobStatus.COMPLETED.value: 0,
        JobStatus.FAILED.value: 0,
    }
    active_jobs: list[Job] = []
    failed_jobs: list[Job] = []

    for job in JOBS.values():
        raw = job.status
        key = raw.value if isinstance(raw, JobStatus) else str(raw).strip().lower()
        if key in counts:
            counts[key] += 1
        if key in (JobStatus.QUEUED.value, JobStatus.PROCESSING.value):
            active_jobs.append(job)
        elif key == JobStatus.FAILED.value:
            failed_jobs.append(job)

    active_jobs.sort(key=lambda x: x.timestamp, reverse=True)
    failed_jobs.sort(key=lambda x: x.timestamp, reverse=True)
    total = int(sum(counts.values()))

    return {
        "status": "ok",
        "stats": {
            "total": total,
            "queued": int(counts[JobStatus.QUEUED.value]),
            "processing": int(counts[JobStatus.PROCESSING.value]),
            "completed": int(counts[JobStatus.COMPLETED.value]),
            "failed": int(counts[JobStatus.FAILED.value]),
            "active": int(counts[JobStatus.QUEUED.value] + counts[JobStatus.PROCESSING.value]),
        },
        "active_jobs": active_jobs[: int(active_limit)],
        "failed_jobs": failed_jobs[: int(failed_limit)],
        "timestamp": time.time(),
    }


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return JOBS[job_id]

@app.get("/api/jobs")
async def list_jobs(
    status: str = Query(default="all"),
    limit: Optional[int] = Query(default=None, ge=1, le=2000),
):
    """
    List jobs in memory, optionally filtered by status.
    Backward-compatible: default still returns all jobs.
    """
    status_key = str(status or "all").strip().lower()
    status_filter: Optional[set[str]] = None

    alias_filters: Dict[str, set[str]] = {
        "active": {JobStatus.QUEUED.value, JobStatus.PROCESSING.value},
        "terminal": {JobStatus.COMPLETED.value, JobStatus.FAILED.value},
        "failed": {JobStatus.FAILED.value},
        "queued": {JobStatus.QUEUED.value},
        "processing": {JobStatus.PROCESSING.value},
        "completed": {JobStatus.COMPLETED.value},
    }
    if status_key not in ("", "all"):
        if status_key in alias_filters:
            status_filter = alias_filters[status_key]
        else:
            allowed = {s.value for s in JobStatus}
            csv_items = {part.strip().lower() for part in status_key.split(",") if part.strip()}
            selected = {part for part in csv_items if part in allowed}
            # If caller passes unknown statuses, keep compatibility by falling back to all.
            status_filter = selected if selected else None

    all_jobs = list(JOBS.values())
    if status_filter:
        def _job_status_text(job: Job) -> str:
            raw = job.status
            return raw.value if isinstance(raw, JobStatus) else str(raw).strip().lower()
        all_jobs = [job for job in all_jobs if _job_status_text(job) in status_filter]

    all_jobs.sort(key=lambda x: x.timestamp, reverse=True)
    if limit is not None:
        all_jobs = all_jobs[: int(limit)]
    return all_jobs


@app.post("/api/jobs/cleanup")
async def cleanup_jobs(
    status: str = Query(default="failed"),
    older_than_hours: float = Query(default=24.0, ge=0.0),
    limit: int = Query(default=500, ge=1, le=5000),
    delete_uploads: bool = Query(default=False),
    dry_run: bool = Query(default=False),
):
    """
    Delete old job records from in-memory JOBS and persist to jobs.json.
    Default behavior: remove up to 500 failed jobs older than 24 hours.
    """
    status_key = str(status or "failed").strip().lower()
    alias_filters: Dict[str, set[str]] = {
        "active": {JobStatus.QUEUED.value, JobStatus.PROCESSING.value},
        "terminal": {JobStatus.COMPLETED.value, JobStatus.FAILED.value},
        "failed": {JobStatus.FAILED.value},
        "queued": {JobStatus.QUEUED.value},
        "processing": {JobStatus.PROCESSING.value},
        "completed": {JobStatus.COMPLETED.value},
    }

    allowed = {s.value for s in JobStatus}
    if status_key in alias_filters:
        status_filter = alias_filters[status_key]
    else:
        csv_items = {part.strip().lower() for part in status_key.split(",") if part.strip()}
        status_filter = {part for part in csv_items if part in allowed}
        if not status_filter:
            status_filter = {JobStatus.FAILED.value}

    cutoff_ts = time.time() - float(older_than_hours) * 3600.0

    def _job_status_text(job: Job) -> str:
        raw = job.status
        return raw.value if isinstance(raw, JobStatus) else str(raw).strip().lower()

    matched = [
        (job_id, job)
        for job_id, job in JOBS.items()
        if _job_status_text(job) in status_filter and float(job.timestamp or 0.0) <= cutoff_ts
    ]
    # Delete oldest first for predictable cleanup.
    matched.sort(key=lambda pair: float((pair[1].timestamp or 0.0)))
    to_delete = matched[: int(limit)]
    delete_ids = [job_id for job_id, _ in to_delete]
    target_delete_count = len(delete_ids)
    estimated_upload_delete_count = 0
    upload_deleted_count = 0

    if delete_uploads and to_delete:
        seen_submission_ids: set[str] = set()
        for _, job in to_delete:
            sid = str(job.submission_id or "").strip()
            if not sid or sid in seen_submission_ids:
                continue
            seen_submission_ids.add(sid)
            try:
                estimated_upload_delete_count += _count_upload_artifacts_for_submission(sid)
            except Exception:
                continue

    if delete_ids and not dry_run:
        for job_id, job in to_delete:
            if delete_uploads:
                try:
                    upload_deleted_count += _delete_upload_artifacts_for_submission(str(job.submission_id or "").strip())
                except Exception:
                    pass
            JOBS.pop(job_id, None)
        save_jobs()

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "delete_uploads": bool(delete_uploads),
        "matched_count": len(matched),
        "target_delete_count": int(target_delete_count),
        "deleted_count": 0 if dry_run else int(target_delete_count),
        "upload_delete_estimate_count": int(estimated_upload_delete_count),
        "upload_deleted_count": int(estimated_upload_delete_count) if dry_run else int(upload_deleted_count),
        "sample_ids": delete_ids[:20],
        "status_filter": sorted(status_filter),
        "older_than_hours": float(older_than_hours),
        "limit": int(limit),
        "remaining_jobs": len(JOBS),
    }

@app.post("/api/jobs/{job_id}/rescore")
async def rescore_job(job_id: str):
    """
    Duplicate an existing job/file and re-queue it for scoring.
    """
    import shutil
    import time
    from datetime import datetime
    import hashlib
    import uuid
    from src.models import EngineMode
    
    # 1. Validate Original Job
    # We might need to look up by submission_id if job_id is not in memory (cleaned up)
    # But for now assuming it's in JOBS or we can find it via reports list?
    # Actually list_reports has ID. Let's support both job_id and submission_id lookups
    
    target_job = JOBS.get(job_id)
    if not target_job:
        # Try to find by submission_id
        for k, v in JOBS.items():
            if v.submission_id == job_id:
                target_job = v
                break
    
    original_file_path = None
    original_text = ""
    original_filename = "unknown.mp3"
    upload_root = Path("data/uploads")
    recovered_engine_used = ""

    def _load_report_script_snapshot(submission_id: str) -> tuple[str, bool, str]:
        if not submission_id:
            return "", True, ""
        try:
            candidates = list(REPORTS_DIR.glob(f"**/{submission_id}/{submission_id}.json"))
        except Exception:
            candidates = []
        for report_json in candidates:
            try:
                payload = json.loads(report_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            script = str(payload.get("script_text") or "").strip()
            meta = payload.get("meta") if isinstance(payload, dict) else {}
            is_auto = bool((meta or {}).get("is_auto_transcribed", True))
            engine_used = str((meta or {}).get("engine_used") or "").strip().lower()
            if script and not is_auto:
                return script, is_auto, engine_used
        # Fallback: if only auto-transcribed reports are available, don't force script.
        return "", True, ""

    def find_submission_audio(submission_id: str) -> Optional[Path]:
        if not submission_id:
            return None
        if not upload_root.exists():
            return None
        for path in upload_root.glob(f"**/{submission_id}.*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
                continue
            return path
        return None
    
    if target_job:
        # Known job, try to find its file in data/uploads/...
        # We don't verify if file exists yet, constructed path logic needs to be robust
        # Job doesn't store full path, we need to reconstruct or search?
        # Aah, `worker` gets `file_path`, but `Job` model only has `filename`. 
        # But wait, `upload_audio` stores file in `data/uploads/{date_str}/{submission_id}.mp3`
        # We don't know date_str easily from Job unless we check timestamp or search.
        
        # Strategy: Search for the file in data/uploads
        original_file_path = find_submission_audio(target_job.submission_id)
        if original_file_path is not None:
            original_filename = target_job.filename
            txt_path = original_file_path.with_suffix(".txt")
            if txt_path.exists():
                try:
                    original_text = txt_path.read_text(encoding="utf-8")
                except Exception:
                    original_text = ""
            if not original_text.strip():
                recovered_text, _recovered_auto, recovered_engine = _load_report_script_snapshot(
                    target_job.submission_id
                )
                if recovered_text:
                    original_text = recovered_text
                if recovered_engine:
                    recovered_engine_used = recovered_engine
             
    else:
        # Job might be gone (restarted server), but report exists?
        # If passed ID is a submission_id (from report list UI)
        submission_id = job_id
        # Find audio file only
        original_file_path = find_submission_audio(submission_id)
        if original_file_path is not None:
            original_filename = f"{submission_id}{original_file_path.suffix}"  # Fallback
            txt_path = original_file_path.with_suffix(".txt")
            if txt_path.exists():
                try:
                    original_text = txt_path.read_text(encoding="utf-8")
                except Exception:
                    original_text = ""
            if not original_text.strip():
                recovered_text, _recovered_auto, recovered_engine = _load_report_script_snapshot(submission_id)
                if recovered_text:
                    original_text = recovered_text
                if recovered_engine:
                    recovered_engine_used = recovered_engine
            
    if not original_file_path or not original_file_path.exists():
        raise HTTPException(status_code=404, detail="Original audio file not found. Cannot rescore.")

    # 2. Create New File with Suffix
    # Parse original filename to append _new01
    # Check if already has _newXX
    import re
    
    # Logic: 
    # Logic 1: original filename (from user upload) -> modify stem -> new filename
    # Logic 2: submission_id (system id) -> create new system id
    
    # We want the "filename" metadata to reflect the change so the user sees it in the Report Title?
    # Currently report uses `submission_id` for title mostly, or metadata.
    
    old_stem = Path(original_filename).stem
    # 2. Create New File with Suffix
    # Parse original filename to append _vXX
    import re
    
    old_stem = Path(original_filename).stem
    # Match _v(\d+) or _new(\d+) to be safe, but let's standardize on _v
    match = re.search(r'(_v|_new)(\d+)$', old_stem)
    
    version_label = ""
    if match:
        ver = int(match.group(2))
        new_ver = ver + 1
        # Replace existing suffix with new version
        base_stem = old_stem[:match.start()]
        version_label = f"v{new_ver}"
        new_stem = f"{base_stem}_{version_label}"
    else:
        new_ver = 1
        version_label = f"v{new_ver}"
        new_stem = f"{old_stem}_{version_label}"
        
    new_filename = f"{new_stem}{Path(original_filename).suffix}"
    
    # Generate new system IDs
    new_job_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_hash = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    new_submission_id = f"web_{timestamp}_{random_hash}"
    
    # Copy file
    date_str = datetime.now().strftime("%Y%m%d")
    upload_dir = Path("data/uploads") / date_str
    upload_dir.mkdir(parents=True, exist_ok=True)
    new_file_path = upload_dir / f"{new_submission_id}{original_file_path.suffix}"
    
    try:
        shutil.copy2(original_file_path, new_file_path)
        logger.info(f"Rescore: Copied {original_file_path} to {new_file_path}")
        if original_text.strip():
            new_txt_path = new_file_path.with_suffix(".txt")
            new_txt_path.write_text(original_text, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to copy file: {e}")
        
    # 3. Create New Job
    # We want the student_id to be distinct for the UI
    # Re-use metadata logic but force the version into the student_id
    
    parts = new_stem.split('_', 1)
    if len(parts) == 2:
        raw_student = parts[0]
        raw_task = parts[1]
    else:
        raw_student = new_stem
        raw_task = "rescore"
        
    def safe_meta(s):
        return re.sub(r'[^\w\-]', '_', s)
    
    # Logic: if raw_task contains the version, student_id stays same
    # But user wants to see difference in the list.
    # List displays 'student_name' which comes from 'student_id'.
    # So we MUST append version to student_id.
    base_student_id = safe_meta(raw_student)
    
    # Check if student_id already ends with _v\d+
    if re.search(r'_v\d+$', base_student_id):
         # Strip it
         base_student_id = re.sub(r'_v\d+$', '', base_student_id)
         
    student_id = f"{base_student_id}_{version_label}"
    task_id = safe_meta(raw_task)
    
    job = Job(
        id=new_job_id,
        status=JobStatus.QUEUED,
        submission_id=new_submission_id,
        student_id=student_id,
        task_id=task_id,
        filename=new_filename,
        timestamp=time.time(),
        mode=target_job.mode if target_job else "auto" # Preserve mode
    )
    
    JOBS[new_job_id] = job
    save_jobs()
    
    try:
        target_mode = EngineMode(str(target_job.mode or "auto").lower()) if target_job else EngineMode.AUTO
    except Exception:
        target_mode = EngineMode.AUTO
    if target_mode == EngineMode.AUTO and recovered_engine_used:
        try:
            target_mode = EngineMode(recovered_engine_used)
        except Exception:
            pass

    metadata = {
        "student_id": student_id,
        "task_id": task_id,
        "submission_id": new_submission_id,
        "engine_mode": target_mode
    }
    
    await JOB_QUEUE.put((new_job_id, new_file_path, original_text, target_mode.value, metadata))
    logger.info(f"Rescore job {new_job_id} queued for {new_submission_id} (derived from {job_id})")
    
    return {
        "status": "queued",
        "job_id": new_job_id,
        "submission_id": new_submission_id,
        "new_filename": new_filename
    }

