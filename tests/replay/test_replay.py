from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers import make_engine_with

from quant_system.backtest.event_store import EventStore
from quant_system.backtest.replay import LedgerReplayService, ReplayService
from quant_system.backtest.request import BacktestRequest
from quant_system.domain.exceptions import ReplayMismatchError
from quant_system.strategies import resolve_strategy


@pytest.fixture()
def backtest_result(provider, rulebook, store, registry):
    engine = make_engine_with(
        provider, rulebook, store, registry, resolve_strategy("momentum_topk", {})
    )
    return engine.run(BacktestRequest(
        strategy_id="momentum_topk", dataset_snapshot_id="",
        start_date=date(2024, 1, 1), end_date=date(2024, 3, 29),
        initial_cash=Decimal("1000000"),
    ))


def test_event_log_replays_to_identical_account_state(backtest_result):
    events_path = str(Path(backtest_result.run_dir) / "events.jsonl")
    checked = LedgerReplayService().replay_and_verify(
        events_path, Decimal("1000000"),
        expected_final_hash=backtest_result.final_event_hash,
    )
    assert checked > 0  # every AccountEvent checkpoint matched


def test_tampered_event_log_detected(backtest_result, tmp_path):
    src = Path(backtest_result.run_dir) / "events.jsonl"
    lines = src.read_text().splitlines()
    tampered = []
    changed = False
    for line in lines:
        row = json.loads(line)
        if not changed and row["event_type"] in ("FILL", "PARTIAL_FILL"):
            row["quantity"] = int(row["quantity"]) + 100  # falsify a fill
            changed = True
        tampered.append(json.dumps(row))
    assert changed, "expected at least one fill to tamper with"
    dst = tmp_path / "tampered.jsonl"
    dst.write_text("\n".join(tampered) + "\n")
    with pytest.raises(ReplayMismatchError):
        ReplayService().replay_and_verify(str(dst), Decimal("1000000"))


@pytest.mark.parametrize("mutation", ["delete", "reorder"])
def test_event_log_sequence_mutation_detected(backtest_result, tmp_path, mutation):
    src = Path(backtest_result.run_dir) / "events.jsonl"
    lines = src.read_text().splitlines()
    if mutation == "delete":
        del lines[len(lines) // 2]
    else:
        middle = len(lines) // 2
        lines[middle], lines[middle + 1] = lines[middle + 1], lines[middle]
    path = tmp_path / f"{mutation}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayMismatchError):
        LedgerReplayService().replay_and_verify(
            str(path), Decimal("1000000"),
            expected_final_hash=backtest_result.final_event_hash,
        )


def test_consistent_tamper_and_rehashed_chain_fails_artifact_root(backtest_result, tmp_path):
    src = Path(backtest_result.run_dir) / "events.jsonl"
    rows = [json.loads(line) for line in src.read_text().splitlines()]
    fill = next(row for row in rows if row["event_type"] in {"FILL", "PARTIAL_FILL"})
    fill["quantity"] += 100
    account = next(
        row for row in rows
        if row["event_type"] == "ACCOUNT" and row["seq"] > fill["seq"]
    )
    account["positions"][fill["symbol"]]["total"] += 100

    previous = ""
    rewritten = []
    for row in rows:
        row["previous_event_hash"] = previous
        row["event_hash"] = ""
        event = EventStore.load_event(row)
        row["event_hash"] = EventStore._hash(event)
        previous = row["event_hash"]
        rewritten.append(json.dumps(row))
    path = tmp_path / "consistent-rehash.jsonl"
    path.write_text("\n".join(rewritten) + "\n")

    with pytest.raises(ReplayMismatchError, match="final hash"):
        LedgerReplayService().replay_and_verify(
            str(path), Decimal("1000000"),
            expected_final_hash=backtest_result.final_event_hash,
        )
