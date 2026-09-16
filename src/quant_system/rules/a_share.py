"""Config-driven A-share rulebook loaded from configs/markets/cn_a.yaml."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml

from quant_system.domain.enums import Board, Exchange, OrderType, SecurityType
from quant_system.domain.exceptions import MarketRuleViolationError
from quant_system.domain.types import CN_TZ, require_aware, to_decimal
from quant_system.rules.rulebook import FeeSchedule, TradingRules


class ConfigAShareRuleBook:
    def __init__(self, config_path: str) -> None:
        raw = yaml.safe_load(Path(config_path).read_text())
        self.rulebook_version: str = raw["rulebook_version"]
        self._t_plus_sell: int = int(raw["t_plus_sell"])
        self._sessions: tuple[tuple[str, str], ...] = tuple(
            (a, b) for a, b in raw["sessions"]
        )
        self._boards: dict[str, dict] = raw["boards"]
        self._source_metadata: dict[str, str] = raw["source_metadata"]
        self._rule_versions: list[dict] = raw["rule_versions"]
        self._fees: list[FeeSchedule] = sorted(
            (
                FeeSchedule(
                    effective_from=date.fromisoformat(str(f["effective_from"])),
                    commission_rate=to_decimal(f["commission_rate"]),
                    min_commission=to_decimal(f["min_commission"]),
                    stamp_tax_sell_rate=to_decimal(f["stamp_tax_sell_rate"]),
                    transfer_fee_rate=to_decimal(f["transfer_fee_rate"]),
                )
                for f in raw["fees"]
            ),
            key=lambda f: f.effective_from,
        )

    def _fee_schedule_at(self, d: date) -> FeeSchedule:
        candidates = [f for f in self._fees if f.effective_from <= d]
        if not candidates:
            raise MarketRuleViolationError(f"no fee schedule effective at {d}")
        return candidates[-1]

    def get_rules(
        self,
        exchange: Exchange,
        board: Board,
        security_type: SecurityType,
        symbol: str,
        timestamp: datetime,
        *,
        is_st: bool = False,
        is_suspended: bool = False,
        list_date: date | None = None,
        listed_trading_days: int | None = None,
    ) -> TradingRules:
        require_aware(timestamp, "timestamp")
        local_date = timestamp.astimezone(CN_TZ).date()
        try:
            board_cfg = self._boards[board.value]
        except KeyError as exc:
            raise MarketRuleViolationError(f"no rules configured for board {board}") from exc

        status = "ST" if is_st else "NORMAL"
        version = self._version_at(exchange, board, security_type, status, local_date)
        no_limit_days = int(board_cfg.get("ipo_no_limit_days", 0))
        if list_date is not None and listed_trading_days is None:
            raise MarketRuleViolationError("listed_trading_days required for IPO rule evaluation")
        limit_ratio = (
            None
            if listed_trading_days is not None and listed_trading_days <= no_limit_days
            else to_decimal(version["price_limit"])
        )

        return TradingRules(
            rulebook_version=self.rulebook_version,
            rule_version=version["rule_version"],
            source_metadata={**self._source_metadata, "rule_source": version["source"]},
            exchange=exchange,
            board=board,
            symbol=symbol,
            tick_size=to_decimal(board_cfg["tick_size"]),
            lot_size=int(board_cfg["lot_size"]),
            price_limit_ratio=limit_ratio,
            t_plus_sell=self._t_plus_sell,
            is_suspended=is_suspended,
            is_st=is_st,
            sessions=self._sessions,
            allowed_order_types=tuple(OrderType(t) for t in board_cfg["order_types"]),
            fee_schedule=self._fee_schedule_at(local_date),
        )

    def _version_at(
        self, exchange: Exchange, board: Board, security_type: SecurityType,
        status: str, local_date: date,
    ) -> dict:
        matches = []
        for version in self._rule_versions:
            exchanges = version["exchange"]
            statuses = version["security_status"]
            effective_to = version.get("effective_to")
            if (
                exchange.value in exchanges
                and version["board"] == board.value
                and version["security_type"] == security_type.value
                and status in ([statuses] if isinstance(statuses, str) else statuses)
                and date.fromisoformat(str(version["effective_from"])) <= local_date
                and (effective_to is None or local_date <= date.fromisoformat(str(effective_to)))
            ):
                matches.append(version)
        if len(matches) != 1:
            raise MarketRuleViolationError(
                f"UNSUPPORTED rules for {exchange}/{board}/{security_type}/{status} at {local_date}"
            )
        return matches[0]
