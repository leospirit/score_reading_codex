from pathlib import Path
import sys

# Ensure backend python package path wins (same convention as server.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "score_reading"))

from src.analytics.pg_sync import cli_main


if __name__ == "__main__":
    raise SystemExit(cli_main())
