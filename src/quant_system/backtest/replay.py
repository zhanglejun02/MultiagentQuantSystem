"""Replay a backtest from its persisted event log and verify that the
account state evolution matches the recorded AccountEvents exactly."""

from __future__ import annotations

from decimal import Decimal

from quant_system.backtest.account.ledger import AccountLedger, CorporateAction
from quant_system.backtest.event_store import EventStore
from quant_system.backtest.events import (
    AccountEvent,
    CorporateActionEvent,
    FillEvent,
    MarketEvent,
    PartialFillEvent,
)
from quant_system.domain.exceptions import ReplayMismatchError
from quant_system.domain.models import FillRecord
from quant_system.domain.types import to_decimal


class LedgerReplayService:
    """Deterministic replay: fills + day boundaries are sufficient to rebuild
    the ledger; every AccountEvent must match the rebuilt state."""

    def replay_and_verify(
        self, events_path: str, initial_cash: Decimal, *, expected_final_hash: str | None = None
    ) -> int:
        try:
            events = EventStore.load_jsonl(
                events_path, expected_final_hash=expected_final_hash
            )
        except (ValueError, KeyError) as exc:
            raise ReplayMismatchError(str(exc)) from exc
        ledger = AccountLedger("replay", initial_cash)
        checked = 0
        for event in events:
            if isinstance(event, MarketEvent) and event.kind == "SESSION_OPEN":
                ledger.start_of_day(event.trade_date)
            elif isinstance(event, CorporateActionEvent):
                ledger.apply_corporate_action(
                    CorporateAction(
                        symbol=event.symbol, kind=event.kind, ex_date=event.trade_date,
                        cash_per_share=event.cash_per_share, split_ratio=event.split_ratio,
                    )
                )
            elif isinstance(event, (FillEvent, PartialFillEvent)):
                fill = FillRecord(
                    fill_id=event.fill_id, order_id=event.order_id, symbol=event.symbol,
                    side=event.side, quantity=event.quantity, price=event.price,
                    commission=event.commission, stamp_tax=event.stamp_tax,
                    transfer_fee=event.transfer_fee, trade_date=event.trade_date,
                    filled_at=event.ts,
                )
                ledger.apply_fill(fill)
            elif isinstance(event, AccountEvent):
                prices = {
                    symbol: to_decimal(p["last_price"]) for symbol, p in event.positions.items()
                }
                ledger.mark_to_market(prices)
                self._compare(event, ledger)
                checked += 1
        return checked

    @staticmethod
    def _compare(event: AccountEvent, ledger: AccountLedger) -> None:
        mismatches: list[str] = []
        for field, expected, actual in (
            ("cash", event.cash, ledger.cash),
            ("frozen_cash", event.frozen_cash, ledger.frozen_cash),
            ("realized_pnl", event.realized_pnl, ledger.realized_pnl),
            ("unrealized_pnl", event.unrealized_pnl, ledger.unrealized_pnl),
            ("fees", event.fees, ledger.fees_paid),
            ("taxes", event.taxes, ledger.taxes_paid),
        ):
            if expected != actual:
                mismatches.append(f"{field} {actual} != recorded {expected}")
        if event.nav != ledger.nav:
            mismatches.append(f"nav {ledger.nav} != recorded {event.nav}")
        recorded_positions = {
            symbol: {
                key: int(position[key])
                for key in ("total", "available", "frozen_for_sell", "pending_settle")
            }
            for symbol, position in event.positions.items()
        }
        actual_snapshot = ledger.snapshot()["positions"]
        actual_positions = {
            symbol: {
                key: int(position[key])
                for key in ("total", "available", "frozen_for_sell", "pending_settle")
            }
            for symbol, position in actual_snapshot.items()
        }
        if recorded_positions != actual_positions:
            mismatches.append(f"positions {actual_positions} != recorded {recorded_positions}")
        if mismatches:
            raise ReplayMismatchError(
                f"replay diverged at {event.trade_date}: " + "; ".join(mismatches)
            )


ReplayService = LedgerReplayService
