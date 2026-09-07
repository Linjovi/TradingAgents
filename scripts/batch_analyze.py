#!/usr/bin/env python3
"""Pick tickers from reports/ticker_names.json and run analyses in parallel.

Each finished job writes the report to the default path with no confirmation.
Worker processes start 30 seconds apart (override with BATCH_STAGGER_SECONDS).

Usage:
    python scripts/batch_analyze.py
"""

from cli.batch import main

if __name__ == "__main__":
    main()
