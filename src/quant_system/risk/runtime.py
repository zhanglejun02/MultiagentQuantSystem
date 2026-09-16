"""Runtime safety: KillSwitch, circuit breaker and trading runtime state.

Agent code has no import path that can disable these; only explicit human
operations (or fail-closed automation) change runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_system.domain.enums import TradingRuntimeState
from quant_system.domain.types import utc_now


class KillSwitch:
    def __init__(self) -> None:
        self._engaged = False
        self._reason = ""
        self._at: datetime | None = None

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str:
        return self._reason

    def engage(self, reason: str) -> None:
        self._engaged = True
        self._reason = reason
        self._at = utc_now()

    def disengage(self, operator: str) -> None:
        """Requires an explicit human operator identity."""
        if not operator:
            raise ValueError("disengage requires an operator identity")
        self._engaged = False
        self._reason = f"disengaged by {operator}"
        self._at = utc_now()


@dataclass
class CircuitBreakerConfig:
    max_daily_loss_pct: Decimal = Decimal("0.05")
    max_drawdown_pct: Decimal = Decimal("0.20")


class TradingRuntime:
    def __init__(self, kill_switch: KillSwitch, config: CircuitBreakerConfig | None = None) -> None:
        self.kill_switch = kill_switch
        self.config = config or CircuitBreakerConfig()
        self._state = TradingRuntimeState.RUNNING
        self._safe_mode_reason = ""

    @property
    def state(self) -> TradingRuntimeState:
        if self.kill_switch.engaged:
            return TradingRuntimeState.KILLED
        return self._state

    @property
    def safe_mode_reason(self) -> str:
        return self._safe_mode_reason

    def enter_safe_mode(self, reason: str) -> None:
        self._state = TradingRuntimeState.SAFE_MODE
        self._safe_mode_reason = reason

    def exit_safe_mode(self, operator: str) -> None:
        if not operator:
            raise ValueError("exit_safe_mode requires an operator identity")
        self._state = TradingRuntimeState.RUNNING
        self._safe_mode_reason = ""

    def check_circuit_breakers(
        self,
        *,
        daily_pnl_pct: Decimal,
        drawdown_pct: Decimal,
        broker_connected: bool = True,
        market_data_connected: bool = True,
        reconciliation_ok: bool = True,
    ) -> None:
        if daily_pnl_pct < -self.config.max_daily_loss_pct:
            self.kill_switch.engage(f"daily loss {daily_pnl_pct} breached limit")
        if drawdown_pct > self.config.max_drawdown_pct:
            self.kill_switch.engage(f"drawdown {drawdown_pct} breached limit")
        if not broker_connected:
            self.enter_safe_mode("broker disconnected")
        if not market_data_connected:
            self.enter_safe_mode("market data disconnected")
        if not reconciliation_ok:
            self.enter_safe_mode("state reconciliation failure")

    def can_trade(self) -> bool:
        return self.state == TradingRuntimeState.RUNNING
