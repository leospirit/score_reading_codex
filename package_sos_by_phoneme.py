#!/usr/bin/env python3
"""
Package local Sounds of Speech assets by phoneme folder.

Input default:
  data/sos_assets_english_full/phonemes

Output default:
  data/sos_assets_english_full/packs/
    - <folder>.zip
    - sos_all_phonemes.zip
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def zip_folder(folder: Path, out_zip: Path) -> int:
    files = [p for p in folder.rglob("*") if p.is_file()]
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in files:
            zf.write(fp, arcname=str(fp.relative_to(folder)))
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package SoS assets by phoneme")
    parser.add_argument("--root", type=str, default="data/sos_assets_english_full", help="SoS root path")
    args = parser.parse_args()

    root = Path(args.root)
    phonemes_root = root / "phonemes"
    packs_root = root / "packs"
    if not phonemes_root.exists() or not phonemes_root.is_dir():
        print(f"[ERROR] Missing phonemes directory: {phonemes_root}")
        return 2

    manifest: dict[str, dict[str, int | str]] = {}
    folders = sorted([p for p in phonemes_root.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
    total_files = 0
    for folder in folders:
        out_zip = packs_root / f"{folder.name}.zip"
        count = zip_folder(folder, out_zip)
        total_files += count
        manifest[folder.name] = {
            "zip": str(out_zip.relative_to(root)).replace("\\", "/"),
            "file_count": count,
        }
        print(f"[PACKED] {folder.name}: files={count} -> {out_zip}")

    all_zip = packs_root / "sos_all_phonemes.zip"
    with zipfile.ZipFile(all_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for folder in folders:
            for fp in folder.rglob("*"):
                if fp.is_file():
                    zf.write(fp, arcname=str(fp.relative_to(root)))

    manifest_path = packs_root / "pack_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "root": str(root),
                "folder_count": len(folders),
                "total_files": total_files,
                "all_zip": str(all_zip.relative_to(root)).replace("\\", "/"),
                "packs": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[DONE] folders={len(folders)} total_files={total_files}")
    print(f"[DONE] all_zip={all_zip}")
    print(f"[DONE] manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

