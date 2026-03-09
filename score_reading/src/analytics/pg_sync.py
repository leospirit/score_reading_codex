"""PostgreSQL analytics sync helpers (phase 1).

This module is intentionally side-car only: it reads report JSON files and
upserts normalized rows to analytics tables without changing scoring behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ReportFile:
    json_path: Path
    report_url: str
    file_size: int
    mtime_ns: int


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _as_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_script_text(text: str) -> str:
    compact = " ".join((text or "").split())
    return compact.lower()


def _safe_iso_timestamp(value: str | None) -> str | None:
    raw = _safe_text(value)
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone().isoformat()


def extract_class_id_from_report_url(report_url: str) -> str:
    """Extract class id from report URL.

    Expected report URL shape: /reports/{class_id}/.../xxx.html
    """
    parts = [p for p in _safe_text(report_url).split("/") if p]
    if len(parts) >= 2 and parts[0] == "reports":
        class_id = _safe_text(parts[1])
        return class_id or "unknown"
    return "unknown"


def build_article_identity(meta: Mapping[str, Any], script_text: str) -> tuple[str, str | None, str]:
    task_id = _safe_text(meta.get("task_id"))
    normalized_script = _normalize_script_text(script_text)
    article_hash = hashlib.sha256(normalized_script.encode("utf-8")).hexdigest()
    if task_id:
        article_id = f"task:{task_id}"
    else:
        article_id = f"hash:{article_hash[:16]}"
    return article_id, (task_id or None), article_hash


def _count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return 0


def parse_report_json_record(json_path: Path, report_url: str | None = None) -> dict[str, Any]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}

    submission_id = _safe_text(meta.get("submission_id")) or json_path.stem
    student_name = _safe_text(meta.get("student_name"))
    student_id = _safe_text(meta.get("student_id")) or student_name or submission_id.split("_")[0]
    class_id = extract_class_id_from_report_url(report_url or "")

    script_text = _safe_text(payload.get("script_text"))
    article_id, task_id, article_hash = build_article_identity(meta, script_text)

    return {
        "submission_id": submission_id,
        "student_id": student_id,
        "student_name": student_name or student_id,
        "class_id": class_id,
        "article_id": article_id,
        "task_id": task_id,
        "article_hash": article_hash,
        "article_title": task_id or article_id,
        "submitted_at": _safe_iso_timestamp(_safe_text(meta.get("timestamp"))) or datetime.now().isoformat(),
        "overall_score": _as_float(scores.get("overall_100")),
        "pron_score": _as_float(scores.get("pronunciation_100")),
        "fluency_score": _as_float(scores.get("fluency_100")),
        "intonation_score": _as_float(scores.get("intonation_100")),
        "completeness_score": _as_float(scores.get("completeness_100")),
        "missing_word_count": _count_items(analysis.get("missing_words")),
        "weak_phoneme_count": _count_items(analysis.get("weak_phonemes")),
        "source_json_path": str(json_path),
    }


def _iter_report_files(report_root: Path) -> Iterable[ReportFile]:
    if not report_root.exists():
        return []
    files: list[ReportFile] = []
    for json_path in report_root.glob("**/*.json"):
        # Most report JSON files use web_*.json naming; skip unrelated control files.
        if not json_path.stem.startswith("web_"):
            continue
        rel = json_path.relative_to(report_root)
        report_rel = rel.with_suffix(".html")
        report_url = "/reports/" + report_rel.as_posix()
        stat = json_path.stat()
        files.append(
            ReportFile(
                json_path=json_path,
                report_url=report_url,
                file_size=_as_int(stat.st_size),
                mtime_ns=_as_int(stat.st_mtime_ns),
            )
        )
    files.sort(key=lambda r: str(r.json_path))
    return files


def _load_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "psycopg is required for PG sync. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg


def _ensure_schema(conn: Any, schema_sql_path: Path) -> None:
    sql_text = schema_sql_path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)


def _load_file_state(conn: Any) -> dict[str, tuple[int, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT file_path, file_size, mtime_ns
            FROM analytics_file_ingest_state
            """
        )
        rows = cur.fetchall()
    return {str(row[0]): (_as_int(row[1]), _as_int(row[2])) for row in rows}


def _upsert_dimension_rows(conn: Any, record: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_dim_class (class_id, class_name)
            VALUES (%s, %s)
            ON CONFLICT (class_id)
            DO UPDATE SET
              class_name = EXCLUDED.class_name,
              updated_at = NOW()
            """,
            (record["class_id"], record["class_id"]),
        )
        cur.execute(
            """
            INSERT INTO analytics_dim_student (student_id, student_name, class_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (student_id)
            DO UPDATE SET
              student_name = EXCLUDED.student_name,
              class_id = EXCLUDED.class_id,
              updated_at = NOW()
            """,
            (record["student_id"], record["student_name"], record["class_id"]),
        )
        cur.execute(
            """
            INSERT INTO analytics_dim_article (article_id, task_id, article_hash, title)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (article_id)
            DO UPDATE SET
              task_id = EXCLUDED.task_id,
              article_hash = EXCLUDED.article_hash,
              title = EXCLUDED.title,
              updated_at = NOW()
            """,
            (
                record["article_id"],
                record["task_id"],
                record["article_hash"],
                record["article_title"],
            ),
        )


def _upsert_attempt_row(conn: Any, record: Mapping[str, Any]) -> bool:
    """Returns True if inserted, False if updated."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_fact_attempt (
                submission_id,
                student_id,
                class_id,
                article_id,
                submitted_at,
                overall_score,
                pron_score,
                fluency_score,
                intonation_score,
                completeness_score,
                missing_word_count,
                weak_phoneme_count,
                source_json_path
            )
            VALUES (
                %s, %s, %s, %s, %s::timestamptz,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (submission_id)
            DO UPDATE SET
                student_id = EXCLUDED.student_id,
                class_id = EXCLUDED.class_id,
                article_id = EXCLUDED.article_id,
                submitted_at = EXCLUDED.submitted_at,
                overall_score = EXCLUDED.overall_score,
                pron_score = EXCLUDED.pron_score,
                fluency_score = EXCLUDED.fluency_score,
                intonation_score = EXCLUDED.intonation_score,
                completeness_score = EXCLUDED.completeness_score,
                missing_word_count = EXCLUDED.missing_word_count,
                weak_phoneme_count = EXCLUDED.weak_phoneme_count,
                source_json_path = EXCLUDED.source_json_path,
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            (
                record["submission_id"],
                record["student_id"],
                record["class_id"],
                record["article_id"],
                record["submitted_at"],
                record["overall_score"],
                record["pron_score"],
                record["fluency_score"],
                record["intonation_score"],
                record["completeness_score"],
                record["missing_word_count"],
                record["weak_phoneme_count"],
                record["source_json_path"],
            ),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _upsert_file_state(conn: Any, file_item: ReportFile, status: str, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_file_ingest_state (
                file_path, file_size, mtime_ns, last_status, last_error
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (file_path)
            DO UPDATE SET
                file_size = EXCLUDED.file_size,
                mtime_ns = EXCLUDED.mtime_ns,
                last_status = EXCLUDED.last_status,
                last_error = EXCLUDED.last_error,
                updated_at = NOW()
            """,
            (str(file_item.json_path), file_item.file_size, file_item.mtime_ns, status, error),
        )


def _insert_job_log(
    conn: Any,
    *,
    status: str,
    processed: int,
    inserted: int,
    updated: int,
    skipped: int,
    errors: int,
    message: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_job_run (
                status, processed_count, inserted_count, updated_count,
                skipped_count, error_count, message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (status, processed, inserted, updated, skipped, errors, message),
        )


def sync_reports_to_pg(
    *,
    dsn: str,
    report_root: Path,
    schema_sql_path: Path,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    files = list(_iter_report_files(report_root))
    if limit and limit > 0:
        files = files[:limit]

    summary = {
        "total_files": len(files),
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        for item in files:
            try:
                parse_report_json_record(item.json_path, item.report_url)
                summary["processed"] += 1
            except Exception:
                summary["errors"] += 1
        return summary

    psycopg = _load_psycopg()

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        _ensure_schema(conn, schema_sql_path)
        conn.commit()

        state_map = _load_file_state(conn)

        for item in files:
            previous = state_map.get(str(item.json_path))
            if previous and previous[0] == item.file_size and previous[1] == item.mtime_ns:
                summary["skipped"] += 1
                continue

            try:
                record = parse_report_json_record(item.json_path, item.report_url)
                _upsert_dimension_rows(conn, record)
                inserted = _upsert_attempt_row(conn, record)
                _upsert_file_state(conn, item, status="ok", error=None)
                conn.commit()

                summary["processed"] += 1
                if inserted:
                    summary["inserted"] += 1
                else:
                    summary["updated"] += 1
            except Exception as exc:
                conn.rollback()
                try:
                    _upsert_file_state(conn, item, status="error", error=str(exc)[:2000])
                    conn.commit()
                except Exception:
                    conn.rollback()
                summary["errors"] += 1

        _insert_job_log(
            conn,
            status="ok" if summary["errors"] == 0 else "partial",
            processed=summary["processed"],
            inserted=summary["inserted"],
            updated=summary["updated"],
            skipped=summary["skipped"],
            errors=summary["errors"],
            message="phase1 sync run",
        )
        conn.commit()

    return summary


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync report JSON data into PostgreSQL analytics tables.")
    parser.add_argument("--dsn", default=_safe_text(os.getenv("PG_ANALYTICS_DSN") or os.getenv("DATABASE_URL")))
    parser.add_argument("--report-root", default="data/out")
    parser.add_argument("--schema-sql", default="analytics_pg/schema.sql")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.dsn and not args.dry_run:
        parser.error("--dsn is required (or set PG_ANALYTICS_DSN / DATABASE_URL)")

    repo_root = Path(__file__).resolve().parents[3]
    report_root = (repo_root / args.report_root).resolve()
    schema_sql = (repo_root / args.schema_sql).resolve()

    if not report_root.exists():
        raise SystemExit(f"Report root not found: {report_root}")
    if not args.dry_run and not schema_sql.exists():
        raise SystemExit(f"Schema SQL not found: {schema_sql}")

    summary = sync_reports_to_pg(
        dsn=args.dsn,
        report_root=report_root,
        schema_sql_path=schema_sql,
        limit=args.limit if args.limit > 0 else None,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
