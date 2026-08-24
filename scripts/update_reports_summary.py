"""Rebuild ``reports/汇总.md`` from every saved complete report.

Usage:
    python scripts/update_reports_summary.py
    python scripts/update_reports_summary.py /path/to/reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tradingagents.report_summary import collect_report_entries, update_reports_summary  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild reports/汇总.md from saved reports.")
    parser.add_argument(
        "reports_root",
        nargs="?",
        default="reports",
        help="Reports directory to scan (default: ./reports)",
    )
    args = parser.parse_args(argv)

    reports_root = Path(args.reports_root).expanduser().resolve()
    if not reports_root.is_dir():
        print(f"Reports directory not found: {reports_root}", file=sys.stderr)
        return 1

    entries = collect_report_entries(reports_root)
    summary_path = update_reports_summary(reports_root)
    instruments = {entry["instrument"] for entry in entries}
    print(
        f"Updated {summary_path} "
        f"({len(instruments)} instruments, {len(entries)} reports)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
