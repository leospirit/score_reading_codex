#!/usr/bin/env python3
"""
Download Sounds of Speech English resources to local folder.

Usage:
  python download_sos_resources.py
  python download_sos_resources.py --out "C:\\Users\\Lenovo\\Downloads\\sos_assets_english_full"
  python download_sos_resources.py --out "C:\\Users\\Lenovo\\Downloads\\sos_assets_english_full" --resume --workers 10 --timeout 8
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


BASE_PAGE = "https://soundsofspeech.uiowa.edu/english"
BASE_ROOT = "https://soundsofspeech.uiowa.edu"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
FINAL_SKIP_PREFIXES = ("ok", "non_mp4", "http:403", "http:404", "http:410")


def _http_get_text(url: str, timeout: float) -> str:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _discover_main_js_url(timeout: float) -> str:
    html = _http_get_text(BASE_PAGE, timeout)
    match = re.search(r'src=["\']([^"\']*main\.[^"\']+\.js)["\']', html, re.IGNORECASE)
    if not match:
        raise RuntimeError("Cannot locate main.*.js")
    return urllib.parse.urljoin(BASE_PAGE, match.group(1))


def _extract_folder_names(main_js_text: str) -> list[str]:
    return sorted(set(re.findall(r'folderName:"([^"]+)"', main_js_text)))


def _build_rows(folders: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for folder in folders:
        base = f"{BASE_ROOT}/assets/phonemes/{folder}"
        rows.append({"folder": folder, "kind": "animation", "url": f"{base}/animation/{folder}.mp4"})
        rows.append({"folder": folder, "kind": "sound", "url": f"{base}/examples/sound.mp4"})
        rows.append({"folder": folder, "kind": "word", "url": f"{base}/examples/word.mp4"})
        for i in range(1, 5):
            rows.append({"folder": folder, "kind": f"word{i}", "url": f"{base}/examples/word{i}.mp4"})
    return rows


def _download(url: str, timeout: float) -> tuple[str, bytes, str]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = getattr(resp, "status", 200)
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            data = resp.read()
            if status != 200:
                return f"http:{status}", b"", ctype
            if "video/mp4" not in ctype:
                return "non_mp4", b"", ctype
            if not data:
                return "empty", b"", ctype
            return "ok", data, ctype
    except urllib.error.HTTPError as exc:
        return f"http:{exc.code}", b"", ""
    except urllib.error.URLError:
        return "url_error", b"", ""
    except Exception:
        return "error", b"", ""


def _load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            out[url] = {
                "status": str(row.get("status") or "").strip(),
                "bytes": str(row.get("bytes") or "").strip(),
                "content_type": str(row.get("content_type") or "").strip(),
                "saved_path": str(row.get("saved_path") or "").strip(),
            }
    return out


def _is_final_status(status: str) -> bool:
    token = str(status or "").strip().lower()
    return any(token.startswith(prefix) for prefix in FINAL_SKIP_PREFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Sounds of Speech resources")
    parser.add_argument("--out", type=str, default="", help="Output directory")
    parser.add_argument("--timeout", type=float, default=8.0, help="Request timeout seconds")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers")
    parser.add_argument("--resume", action="store_true", help="Resume from existing manifest.csv")
    args = parser.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = Path(args.out) if args.out else Path("data/sos_assets") / stamp
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.csv"

    try:
        main_js_url = _discover_main_js_url(args.timeout)
        main_js_text = _http_get_text(main_js_url, args.timeout)
        folders = [f for f in _extract_folder_names(main_js_text) if f.strip()]
    except Exception as exc:
        print(f"[ERROR] Discovery failed: {exc}")
        return 2

    rows = _build_rows(folders)
    total = len(rows)
    previous = _load_manifest(manifest_path) if args.resume else {}

    merged: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    downloaded_ok = 0

    for row in rows:
        prev = previous.get(row["url"])
        if prev and _is_final_status(prev.get("status", "")):
            status = prev.get("status", "")
            saved_path = prev.get("saved_path", "")
            # Ensure file still exists for successful entries; otherwise retry.
            if status.startswith("ok") and saved_path and not (out_root / saved_path).exists():
                pending.append(row)
                merged.append(
                    {
                        "folder": row["folder"],
                        "kind": row["kind"],
                        "url": row["url"],
                        "status": "retry_missing_file",
                        "bytes": "0",
                        "content_type": "",
                        "saved_path": "",
                    }
                )
                continue
            merged.append(
                {
                    "folder": row["folder"],
                    "kind": row["kind"],
                    "url": row["url"],
                    "status": status,
                    "bytes": prev.get("bytes", ""),
                    "content_type": prev.get("content_type", ""),
                    "saved_path": saved_path,
                }
            )
            if status.startswith("ok"):
                downloaded_ok += 1
            continue
        pending.append(row)
        merged.append(
            {
                "folder": row["folder"],
                "kind": row["kind"],
                "url": row["url"],
                "status": "pending",
                "bytes": "0",
                "content_type": "",
                "saved_path": "",
            }
        )

    print(f"[INFO] total={total} resume={args.resume} already_ok={downloaded_ok} pending={len(pending)}")

    index_by_url = {row["url"]: i for i, row in enumerate(merged)}
    workers = max(1, min(int(args.workers or 8), 24))
    completed = 0
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_download, row["url"], float(args.timeout)): row for row in pending}
            for fut in as_completed(futures):
                row = futures[fut]
                status, data, ctype = fut.result()
                saved_path = ""
                if status == "ok":
                    rel = Path("phonemes") / row["folder"] / row["kind"] / Path(row["url"]).name
                    target = out_root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    saved_path = str(rel).replace("\\", "/")
                    downloaded_ok += 1
                idx = index_by_url[row["url"]]
                merged[idx] = {
                    "folder": row["folder"],
                    "kind": row["kind"],
                    "url": row["url"],
                    "status": status,
                    "bytes": str(len(data)),
                    "content_type": ctype,
                    "saved_path": saved_path,
                }
                completed += 1
                if completed % 40 == 0 or completed == len(pending):
                    print(f"[INFO] downloaded {completed}/{len(pending)} in this run (ok_total={downloaded_ok})")

    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["folder", "kind", "url", "status", "bytes", "content_type", "saved_path"],
        )
        writer.writeheader()
        writer.writerows(merged)

    print(f"[DONE] ok_total={downloaded_ok}/{total} out={out_root.resolve()}")
    print(f"[DONE] manifest={manifest_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
