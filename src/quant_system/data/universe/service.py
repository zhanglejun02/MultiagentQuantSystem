"""Point-in-time universe service.

Prevents survivorship bias: membership is computed strictly from information
available at as_of (listing, delisting, ST, suspension are effective-dated).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from quant_system.data.providers.base import ReferenceDataProvider
from quant_system.domain.enums import Board, Exchange, SecurityFlag
from quant_system.domain.models import SecurityMaster


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    exchange: Exchange
    board: Board
    list_date: date
    is_st: bool
    is_suspended: bool


class UniverseService:
    def __init__(self, reference: ReferenceDataProvider) -> None:
        self._ref = reference

    def get_universe(self, trade_date: date, *, as_of: datetime) -> list[UniverseMember]:
        securities = self._ref.get_securities(as_of=as_of)
        statuses = self._ref.get_status_intervals(as_of=as_of)
        members: list[UniverseMember] = []
        for sec in securities:
            if sec.list_date > trade_date:
                continue
            if sec.delist_date is not None and sec.delist_date <= trade_date:
                continue
            flags = {
                s.flag for s in statuses if s.symbol == sec.symbol and s.covers(trade_date)
            }
            members.append(
                UniverseMember(
                    symbol=sec.symbol,
                    exchange=sec.exchange,
                    board=sec.board,
                    list_date=sec.list_date,
                    is_st=SecurityFlag.ST.value in flags,
                    is_suspended=SecurityFlag.SUSPENDED.value in flags,
                )
            )
        return sorted(members, key=lambda m: m.symbol)

    def get_security(self, symbol: str, *, as_of: datetime) -> SecurityMaster | None:
        for sec in self._ref.get_securities(as_of=as_of):
            if sec.symbol == symbol:
                return sec
        return None

    def is_suspended(self, symbol: str, trade_date: date, *, as_of: datetime) -> bool:
        for s in self._ref.get_status_intervals(as_of=as_of):
            if s.symbol == symbol and s.flag == SecurityFlag.SUSPENDED.value and s.covers(trade_date):
                return True
        return False

    def is_st(self, symbol: str, trade_date: date, *, as_of: datetime) -> bool:
        for s in self._ref.get_status_intervals(as_of=as_of):
            if s.symbol == symbol and s.flag == SecurityFlag.ST.value and s.covers(trade_date):
                return True
        return False
