from pathlib import Path

import src.analytics.pg_sync as pg_sync
from src.analytics.pg_sync import (
    build_article_identity,
    extract_class_id_from_report_url,
    parse_report_json_record,
    sync_reports_to_pg,
)


def test_extract_class_id_from_report_url() -> None:
    assert extract_class_id_from_report_url('/reports/01/student/web_1.html') == '01'
    assert extract_class_id_from_report_url('/reports/upload/a/b.html') == 'upload'
    assert extract_class_id_from_report_url('/invalid/path') == 'unknown'


def test_build_article_identity_prefers_task_id() -> None:
    article_id, task_id, article_hash = build_article_identity(
        {'task_id': '01'},
        'Hello world.',
    )
    assert article_id == 'task:01'
    assert task_id == '01'
    assert len(article_hash) == 64


def test_parse_report_json_record_extracts_scores(tmp_path: Path) -> None:
    payload = {
        'meta': {
            'task_id': '01',
            'student_id': 'S001',
            'student_name': 'Tom',
            'submission_id': 'web_abc',
            'timestamp': '2026-03-05T08:00:00',
        },
        'scores': {
            'overall_100': 88.5,
            'pronunciation_100': 90,
            'fluency_100': 80,
            'intonation_100': 75,
            'completeness_100': 100,
        },
        'analysis': {
            'missing_words': ['winter', 'vacation'],
            'weak_phonemes': ['/r/', '/n/'],
        },
        'script_text': 'Lily, today is the last day of school.',
    }

    p = tmp_path / 'web_abc.json'
    p.write_text(__import__('json').dumps(payload), encoding='utf-8')

    record = parse_report_json_record(p, report_url='/reports/01/student/web_abc.html')

    assert record['submission_id'] == 'web_abc'
    assert record['class_id'] == '01'
    assert record['student_id'] == 'S001'
    assert record['overall_score'] == 88.5
    assert record['missing_word_count'] == 2
    assert record['weak_phoneme_count'] == 2


def test_sync_reports_to_pg_dry_run_does_not_require_psycopg(tmp_path: Path, monkeypatch) -> None:
    report_root = tmp_path / 'out' / '01' / 'student_a' / 'web_abc'
    report_root.mkdir(parents=True, exist_ok=True)
    json_path = report_root / 'web_abc.json'
    json_path.write_text(
        __import__('json').dumps(
            {
                'meta': {'submission_id': 'web_abc', 'student_id': 'S001'},
                'scores': {'overall_100': 88},
                'analysis': {},
                'script_text': 'hello world',
            }
        ),
        encoding='utf-8',
    )
    schema_sql = tmp_path / 'schema.sql'
    schema_sql.write_text('SELECT 1;', encoding='utf-8')

    def _boom() -> None:
        raise AssertionError('psycopg should not load in dry-run')

    monkeypatch.setattr(pg_sync, '_load_psycopg', _boom)

    summary = sync_reports_to_pg(
        dsn='',
        report_root=tmp_path / 'out',
        schema_sql_path=schema_sql,
        dry_run=True,
    )
    assert summary['processed'] == 1
    assert summary['errors'] == 0
