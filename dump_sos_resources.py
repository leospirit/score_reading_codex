#!/usr/bin/env python3
"""
Dump direct resource URLs from Sounds of Speech (UIowa English page).

Usage examples:
  python dump_sos_resources.py
  python dump_sos_resources.py --probe --timeout 15
  python dump_sos_resources.py --output D:\score_reading_fresh\data\out\sos_resources.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable


BASE_PAGE = "https://soundsofspeech.uiowa.edu/english"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _http_get_text(url: str, timeout: float = 30.0) -> str:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _discover_main_js_url(timeout: float) -> str:
    html = _http_get_text(BASE_PAGE, timeout=timeout)
    match = re.search(r'src=["\']([^"\']*main\.[^"\']+\.js)["\']', html, re.IGNORECASE)
    if not match:
        raise RuntimeError("Cannot locate main.*.js from page")
    return urllib.parse.urljoin(BASE_PAGE, match.group(1))


def _extract_folder_names(main_js_text: str) -> list[str]:
    folders = sorted(set(re.findall(r'folderName:"([^"]+)"', main_js_text)))
    return [item for item in folders if item.strip()]


def _build_candidate_rows(base_root: str, folders: Iterable[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for folder in folders:
        base = f"{base_root}/assets/phonemes/{folder}"
        rows.append({"folder": folder, "kind": "animation", "url": f"{base}/animation/{folder}.mp4"})
        rows.append({"folder": folder, "kind": "sound", "url": f"{base}/examples/sound.mp4"})
        rows.append({"folder": folder, "kind": "word", "url": f"{base}/examples/word.mp4"})
        for i in range(1, 5):
            rows.append({"folder": folder, "kind": f"word{i}", "url": f"{base}/examples/word{i}.mp4"})
    return rows


def _probe_url(url: str, timeout: float) -> tuple[str, str]:
    """
    Return (status, content_length).
    status:
      - "ok:<code>"
      - "http_error:<code>"
      - "url_error"
      - "error"
    """
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = getattr(resp, "status", 200)
            length = resp.headers.get("Content-Length", "")
            return f"ok:{code}", str(length)
    except urllib.error.HTTPError as exc:
        return f"http_error:{exc.code}", ""
    except urllib.error.URLError:
        return "url_error", ""
    except Exception:
        return "error", ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump Sounds of Speech direct resource links")
    parser.add_argument("--output", type=str, default="", help="Output CSV path")
    parser.add_argument("--probe", action="store_true", help="Probe each URL with small range request")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    try:
        main_js_url = _discover_main_js_url(timeout=args.timeout)
        main_js_text = _http_get_text(main_js_url, timeout=args.timeout)
        folders = _extract_folder_names(main_js_text)
        if not folders:
            raise RuntimeError("No folder names extracted from main JS")
    except Exception as exc:
        print(f"[ERROR] Failed to discover folders: {exc}")
        return 2

    base_root = "https://soundsofspeech.uiowa.edu"
    rows = _build_candidate_rows(base_root, folders)

    if args.probe:
        print(f"[INFO] Probing {len(rows)} URLs ...")
        for idx, row in enumerate(rows, start=1):
            status, content_length = _probe_url(row["url"], timeout=args.timeout)
            row["status"] = status
            row["content_length"] = content_length
            if idx % 50 == 0:
                print(f"[INFO] Probed {idx}/{len(rows)}")
    else:
        for row in rows:
            row["status"] = "not_probed"
            row["content_length"] = ""

    if args.output:
        out_path = Path(args.output)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("data/out") / f"sos_resources_{stamp}.csv"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["folder", "kind", "url", "status", "content_length"])
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    ok_count = sum(1 for r in rows if str(r.get("status", "")).startswith("ok:"))
    print(f"[DONE] folders={len(folders)} urls={total} ok={ok_count} output={out_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

