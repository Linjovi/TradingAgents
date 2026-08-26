"""Parallel batch analysis: pick tickers from ticker_names.json, then the usual CLI setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import questionary
from rich.console import Console

from cli.models import AnalystType
from cli.utils import detect_asset_type, normalize_ticker_symbol
from tradingagents.reporting import TICKER_NAME_MAP_FILENAME, report_directory_component

console = Console()
_print_lock = threading.Lock()


def list_ticker_entries(path: Path | None = None) -> list[tuple[str, str]]:
    """Return ``(ticker, short_name)`` rows from ticker_names.json, skipping comment keys."""
    if path is None:
        repo_root = Path(__file__).resolve().parents[1]
        candidates = [
            Path.cwd() / "reports" / TICKER_NAME_MAP_FILENAME,
            repo_root / "reports" / TICKER_NAME_MAP_FILENAME,
        ]
    else:
        candidates = [path]

    raw: dict = {}
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            raw = payload
            break

    entries: list[tuple[str, str]] = []
    for key, name in raw.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if not isinstance(name, str):
            continue
        cleaned = name.strip()
        if not cleaned:
            continue
        entries.append((key.strip(), cleaned))
    return entries


def parse_ticker_choice(raw: str, listed: list[tuple[str, str]]) -> list[str]:
    """Parse a ticker pick: ``all``, 1-based indexes, codes, or short names."""
    if not listed:
        raise ValueError("No tickers are listed.")
    text = (raw or "").strip()
    if not text:
        raise ValueError("Select at least one ticker.")

    lowered = text.lower()
    if lowered in {"all", "a", "*"}:
        return [ticker for ticker, _ in listed]

    tokens = [token for token in text.replace(",", " ").split() if token]
    matched: set[str] = set()
    by_code = {ticker.upper(): ticker for ticker, _ in listed}
    by_name = {name: ticker for ticker, name in listed}

    for token in tokens:
        if token.isdigit():
            index = int(token)
            if index < 1 or index > len(listed):
                raise ValueError(f"Index {index} is out of range (1-{len(listed)}).")
            matched.add(listed[index - 1][0])
            continue
        code = by_code.get(token.upper())
        if code:
            matched.add(code)
            continue
        name_hit = by_name.get(token)
        if name_hit:
            matched.add(name_hit)
            continue
        raise ValueError(f"Unknown ticker selection: {token}")

    if not matched:
        raise ValueError("Select at least one ticker.")
    return [ticker for ticker, _ in listed if ticker in matched]


def serialize_selections(selections: dict) -> dict:
    payload = dict(selections)
    payload["analysts"] = [
        analyst.value if isinstance(analyst, AnalystType) else str(analyst)
        for analyst in selections["analysts"]
    ]
    return payload


def deserialize_selections(payload: dict) -> dict:
    selections = dict(payload)
    selections["analysts"] = [AnalystType(value) for value in payload["analysts"]]
    return selections


def selections_for_ticker(base: dict, ticker: str) -> dict:
    selected = dict(base)
    selected["ticker"] = normalize_ticker_symbol(ticker)
    selected["asset_type"] = detect_asset_type(selected["ticker"]).value
    return selected


def default_report_save_path(ticker: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / "reports" / report_directory_component(ticker) / stamp


def prompt_tickers(listed: list[tuple[str, str]]) -> list[str]:
    if not listed:
        console.print("[red]No tickers found in reports/ticker_names.json. Exiting...[/red]")
        raise SystemExit(1)

    choices = questionary.checkbox(
        "Select tickers to analyze:",
        choices=[
            questionary.Choice(f"{ticker}  {name}", value=ticker)
            for ticker, name in listed
        ],
        instruction=(
            "\n- Press Space to select/unselect"
            "\n- Press 'a' to select/unselect all"
            "\n- Press Enter when done"
        ),
        validate=lambda selected: bool(selected) or "Select at least one ticker.",
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print("\n[red]No tickers selected. Exiting...[/red]")
        raise SystemExit(1)
    return choices


def _spawn_worker(
    ticker: str,
    payload: dict,
    checkpoint: bool | None,
) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump({**payload, "_checkpoint": checkpoint}, handle, ensure_ascii=False)
        payload_path = handle.name

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from cli.batch import run_worker_payload; "
                f"run_worker_payload(Path({payload_path!r}))",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            with _print_lock:
                console.print(f"[dim]{ticker}[/dim] {line.rstrip()}")
        return ticker, proc.wait()
    finally:
        Path(payload_path).unlink(missing_ok=True)


def run_parallel(
    tickers: list[str],
    shared: dict,
    checkpoint: bool | None = None,
) -> int:
    jobs: dict[str, dict] = {}
    for ticker in tickers:
        jobs[ticker] = serialize_selections(selections_for_ticker(shared, ticker))

    console.print(
        f"\n[bold]Running {len(tickers)} analyses in parallel.[/bold]"
        " Reports save to the default path when each job finishes.\n"
    )

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        futures = {
            pool.submit(_spawn_worker, ticker, payload, checkpoint): ticker
            for ticker, payload in jobs.items()
        }
        for future in as_completed(futures):
            ticker, code = future.result()
            if code == 0:
                console.print(f"[green]✓ {ticker} finished[/green]")
            else:
                failures.append(ticker)
                console.print(f"[red]✗ {ticker} failed (exit {code})[/red]")

    if failures:
        console.print(f"\n[red]Failed: {', '.join(failures)}[/red]")
        return 1
    console.print("\n[green]All batch analyses finished.[/green]")
    return 0


def run_batch(
    checkpoint: bool | None = None,
    clear_checkpoints: bool = False,
) -> None:
    from cli.main import get_user_selections

    if clear_checkpoints:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.checkpointer import clear_all_checkpoints

        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")

    listed = list_ticker_entries()
    tickers = prompt_tickers(listed)
    console.print(f"[green]Selected tickers:[/green] {', '.join(tickers)}")

    shared = get_user_selections(ticker=tickers[0])
    code = run_parallel(tickers, shared, checkpoint=checkpoint)
    raise SystemExit(code)


def run_worker_payload(payload_path: Path, checkpoint: bool | None = None) -> None:
    from cli.main import _find_vendor_error, run_analysis

    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    if checkpoint is None:
        checkpoint = payload.pop("_checkpoint", None)
    else:
        payload.pop("_checkpoint", None)
    selections = deserialize_selections(payload)
    try:
        run_analysis(
            checkpoint=checkpoint,
            selections=selections,
            live=False,
            auto_save=True,
            prompt_display=False,
        )
    except Exception as exc:
        vendor_error = _find_vendor_error(exc)
        if vendor_error:
            console.print(f"[red]Market data unavailable: {vendor_error}[/red]")
            raise SystemExit(1) from None
        raise


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run TradingAgents on multiple tickers from reports/ticker_names.json"
    )
    parser.add_argument("--checkpoint", dest="checkpoint", action="store_true")
    parser.add_argument("--no-checkpoint", dest="checkpoint", action="store_false")
    parser.add_argument(
        "--clear-checkpoints",
        action="store_true",
        help="Delete saved checkpoints before the batch starts.",
    )
    parser.set_defaults(checkpoint=None)
    args = parser.parse_args(argv)
    run_batch(
        checkpoint=args.checkpoint,
        clear_checkpoints=args.clear_checkpoints,
    )


if __name__ == "__main__":
    main()
