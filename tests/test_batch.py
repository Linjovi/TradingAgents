"""Batch ticker listing, selection parsing, and auto-save path."""

from __future__ import annotations

from datetime import datetime

import pytest

from cli.batch import (
    default_report_save_path,
    deserialize_selections,
    list_ticker_entries,
    parse_ticker_choice,
    run_parallel,
    selections_for_ticker,
    serialize_selections,
)
from cli.models import AnalystType


@pytest.mark.unit
def test_list_ticker_entries_skips_comment_keys(tmp_path):
    path = tmp_path / "ticker_names.json"
    path.write_text(
        '{"_comment": "ignore me", "002475.SZ": "立讯精密", "601138.SS": "工业富联"}',
        encoding="utf-8",
    )

    entries = list_ticker_entries(path)

    assert entries == [
        ("002475.SZ", "立讯精密"),
        ("601138.SS", "工业富联"),
    ]


@pytest.mark.unit
def test_parse_ticker_choice_by_index_and_code():
    listed = [("002475.SZ", "立讯精密"), ("601138.SS", "工业富联"), ("002837.SZ", "英维克")]

    assert parse_ticker_choice("1,3", listed) == ["002475.SZ", "002837.SZ"]
    assert parse_ticker_choice("all", listed) == [t for t, _ in listed]
    assert parse_ticker_choice("601138.SS 立讯精密", listed) == ["002475.SZ", "601138.SS"]


@pytest.mark.unit
def test_parse_ticker_choice_rejects_empty():
    listed = [("002475.SZ", "立讯精密")]
    with pytest.raises(ValueError):
        parse_ticker_choice("", listed)
    with pytest.raises(ValueError):
        parse_ticker_choice("99", listed)


@pytest.mark.unit
def test_serialize_roundtrip_preserves_analysts():
    selections = {
        "ticker": "002475.SZ",
        "asset_type": "stock",
        "analysis_date": "2026-08-25",
        "analysts": [AnalystType.MARKET, AnalystType.NEWS],
        "research_depth": 5,
        "llm_provider": "mimo",
        "backend_url": "https://example.com",
        "shallow_thinker": "fast",
        "deep_thinker": "slow",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "Chinese",
    }

    restored = deserialize_selections(serialize_selections(selections))

    assert restored["analysts"] == [AnalystType.MARKET, AnalystType.NEWS]
    assert restored["research_depth"] == 5
    assert restored["output_language"] == "Chinese"


@pytest.mark.unit
def test_selections_for_ticker_overrides_symbol():
    base = {
        "ticker": "002475.SZ",
        "asset_type": "stock",
        "analysts": [AnalystType.MARKET],
    }

    next_sel = selections_for_ticker(base, "601138.SS")

    assert next_sel["ticker"] == "601138.SS"
    assert next_sel["analysts"] == [AnalystType.MARKET]
    assert base["ticker"] == "002475.SZ"


@pytest.mark.unit
def test_default_report_save_path_uses_local_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "cli.batch.report_directory_component",
        lambda ticker: "立讯精密",
    )
    path = default_report_save_path("002475.SZ", now=datetime(2026, 8, 25, 15, 6, 41))

    assert path == tmp_path / "reports" / "立讯精密" / "20260825_150641"


@pytest.mark.unit
def test_worker_payload_auto_saves_and_strips_checkpoint(tmp_path, monkeypatch):
    import json

    from cli.batch import run_worker_payload

    payload = {
        "ticker": "002475.SZ",
        "asset_type": "stock",
        "analysis_date": "2026-08-25",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "mimo",
        "backend_url": "https://example.com",
        "shallow_thinker": "fast",
        "deep_thinker": "slow",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "Chinese",
        "_checkpoint": False,
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict = {}

    def fake_run_analysis(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("cli.main.run_analysis", fake_run_analysis)
    run_worker_payload(path)

    assert captured["checkpoint"] is False
    assert captured["auto_save"] is True
    assert captured["live"] is False
    assert captured["prompt_display"] is False
    assert captured["selections"]["ticker"] == "002475.SZ"
    assert "_checkpoint" not in captured["selections"]


def _minimal_batch_selections() -> dict:
    return {
        "ticker": "002475.SZ",
        "asset_type": "stock",
        "analysis_date": "2026-08-25",
        "analysts": [AnalystType.MARKET],
        "research_depth": 1,
        "llm_provider": "mimo",
        "backend_url": "https://example.com",
        "shallow_thinker": "fast",
        "deep_thinker": "slow",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "Chinese",
    }


@pytest.mark.unit
def test_run_parallel_staggers_worker_starts(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("cli.batch.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "cli.batch._spawn_worker",
        lambda ticker, payload, checkpoint: (ticker, 0),
    )

    code = run_parallel(
        ["002475.SZ", "601138.SS", "002837.SZ"],
        _minimal_batch_selections(),
        stagger_seconds=30,
    )

    assert code == 0
    assert sleeps == [30, 30]


@pytest.mark.unit
def test_run_parallel_skips_stagger_when_delay_is_zero(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("cli.batch.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "cli.batch._spawn_worker",
        lambda ticker, payload, checkpoint: (ticker, 0),
    )

    code = run_parallel(
        ["002475.SZ", "601138.SS"],
        _minimal_batch_selections(),
        stagger_seconds=0,
    )

    assert code == 0
    assert sleeps == []
