"""PIT-safe price-band reference-price calculation."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from quant_system.domain.models import CorporateActionRecord


class ReferencePriceService(Protocol):
    def calculate(
        self,
        previous_close: Decimal,
        actions: Sequence[CorporateActionRecord],
        *,
        rule_version: str,
        security_status: str,
        available_reference_price: Decimal | None = None,
    ) -> Decimal: ...


class CorporateActionReferencePriceService:
    def calculate(
        self,
        previous_close: Decimal,
        actions: Sequence[CorporateActionRecord],
        *,
        rule_version: str,
        security_status: str,
        available_reference_price: Decimal | None = None,
    ) -> Decimal:
        if not rule_version or not security_status:
            raise ValueError("rule version and security status are required")
        if available_reference_price is not None:
            if available_reference_price <= 0:
                raise ValueError("official reference price must be positive")
            return available_reference_price
        cash = sum((action.cash_per_share for action in actions), Decimal("0"))
        ratio = Decimal("1")
        for action in actions:
            if action.kind in {"STOCK_SPLIT", "BONUS_SHARE", "SPLIT"}:
                ratio *= action.split_ratio
        if ratio <= 0 or previous_close < cash:
            raise ValueError("invalid corporate-action reference-price inputs")
        return (previous_close - cash) / ratio