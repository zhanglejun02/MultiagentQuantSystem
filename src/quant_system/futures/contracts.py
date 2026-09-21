"""Versioned futures contract catalog loaded from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from quant_system.futures.domain import FuturesContract


class FuturesContractCatalog:
    def __init__(self, contracts: list[FuturesContract], *, version: str) -> None:
        self.version = version
        self._contracts = {contract.symbol: contract for contract in contracts}
        if len(self._contracts) != len(contracts):
            raise ValueError("duplicate futures contract symbol")

    @classmethod
    def from_yaml(cls, path: str | Path) -> FuturesContractCatalog:
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get("contracts"), list):
            raise ValueError("invalid futures contracts configuration")
        contracts = [FuturesContract.model_validate(item) for item in raw["contracts"]]
        return cls(contracts, version=str(raw.get("version", "unknown")))

    def get(self, symbol: str) -> FuturesContract:
        try:
            return self._contracts[symbol]
        except KeyError as exc:
            raise KeyError(f"unknown futures contract {symbol}") from exc

    def all(self) -> list[FuturesContract]:
        return [self._contracts[symbol] for symbol in sorted(self._contracts)]