from __future__ import annotations

import math
import statistics
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.gold.models import Quote


class StrategyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fast_window: int = Field(default=20, ge=5, le=30)
    slow_window: int = Field(default=60, ge=40, le=120)
    volatility_target: float = Field(default=0.12, ge=0.05, le=0.20)
    trailing_stop: float = Field(default=0.08, ge=0.03, le=0.15)

    @model_validator(mode="after")
    def ordered_windows(self) -> StrategyParameters:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast window must be shorter than slow window")
        return self


def _signal(prices: list[float], parameters: StrategyParameters, cap: float) -> dict[str, Any]:
    slow = statistics.mean(prices[-parameters.slow_window:])
    fast = statistics.mean(prices[-parameters.fast_window:])
    returns = [math.log(current / previous) for previous, current in zip(prices[-21:-1], prices[-20:], strict=True)]
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None
    peak = max(prices[-parameters.slow_window:])
    stop = peak * (1 - parameters.trailing_stop)
    trend = prices[-1] > slow and fast > slow and prices[-1] > stop
    weight = min(cap, cap * parameters.volatility_target / volatility) if volatility else cap
    return {"signal": "ENTER" if trend else "EXIT_OR_WAIT", "close": prices[-1],
            "fast_average": fast, "slow_average": slow, "trailing_reference": stop,
            "annualized_volatility": volatility, "target_weight": weight if trend else 0.0}


def _diagnose(quote: Quote, parameters: StrategyParameters, cap: float, cost: float) -> dict[str, Any]:
    points = quote.points
    prices = [float(point.close) for point in points]
    cash, quantity, peak_nav, drawdown = 1.0, 0.0, 1.0, 0.0
    fees = 0.0
    curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    missing_open = any(point.open is None for point in points[parameters.slow_window:])
    if missing_open:
        return {"status": "UNAVAILABLE", "reason": "Missing execution open prices", "trades": []}
    for index in range(parameters.slow_window, len(points)):
        point = points[index]
        open_price = float(point.open or point.close)
        previous = _signal(prices[:index], parameters, cap)
        current_nav = cash + quantity * open_price
        target = previous["target_weight"]
        exit_position = target == 0 and quantity > 0
        if index == parameters.slow_window or (index - parameters.slow_window) % 5 == 0 or exit_position:
            target_quantity = current_nav * target / open_price
            delta = target_quantity - quantity
            if delta > 0:
                delta = min(delta, cash / (open_price * (1 + cost)))
            if abs(delta) > 1e-12:
                fee = abs(delta) * open_price * cost
                cash -= delta * open_price + fee
                quantity += delta
                fees += fee
                trades.append({"signal_date": points[index - 1].date.isoformat(),
                               "execution_date": point.date.isoformat(), "side": "BUY" if delta > 0 else "SELL"})
        nav = cash + quantity * prices[index]
        peak_nav = max(peak_nav, nav)
        drawdown = min(drawdown, nav / peak_nav - 1)
        curve.append({"date": point.date.isoformat(), "nav": nav})
    anchor = float(points[parameters.slow_window].open or points[parameters.slow_window].close)
    return {"status": "DIAGNOSTIC_ONLY", "execution": "NEXT_OPEN", "total_return": curve[-1]["nav"] - 1,
            "max_drawdown": drawdown, "trade_count": len(trades), "trades": trades,
            "cost_fraction_of_initial": fees, "one_way_cost_bps": cost * 10000,
            "buy_hold_same_cap_return": cap * (prices[-1] / anchor - 1), "curve": curve,
            "start_date": points[parameters.slow_window].date.isoformat(), "end_date": points[-1].date.isoformat()}


def evaluate_gold_strategy(quote: Quote, parameters: StrategyParameters, *, max_weight: float) -> dict[str, Any]:
    if not 0 <= max_weight <= 0.30 or not math.isfinite(max_weight):
        raise ValueError("gold research cap must be between zero and 30% of total assets")
    prices = [float(point.close) for point in quote.points]
    spot = quote.kind == "spot"
    rules = {
        "entry": f"收盘价 > MA{parameters.slow_window}，MA{parameters.fast_window} > MA{parameters.slow_window}，且高于滚动止损参考线",
        "exit": f"任一趋势条件失效，或收盘跌破最近{parameters.slow_window}个观测最高收盘价的{1-parameters.trailing_stop:.0%}",
        "sizing": f"总资产黄金上限{max_weight:.1%}，按20日年化波动率与{parameters.volatility_target:.1%}目标缩放；不是建议配置比例",
        "execution": ("收盘后决策，以次一观测开盘价计算无杠杆金价参考组合；非银行/金条成交模拟。"
                      if spot else "收盘后决策，下一交易日开盘诊断；")
                     + "每5个观测检查调仓，退出优先。仅研究，不自动下单。",
        "invalidation": ("数据缺失/陈旧、渠道价差或保管回购费用变化时暂停；"
                         if spot else "数据缺失/陈旧、发生分红或拆并份额、流动性或规则变化时暂停；")
                        + "公开历史非PIT认证，需独立样本外与真实费用验证。",
    }
    base: dict[str, Any] = {"symbol": quote.symbol, "instrument_kind": quote.kind, "unit": quote.unit,
                            "parameters": parameters.model_dump(), "rules": rules,
                            "deployment_eligible": False, "research_only": True, "max_weight": max_weight,
                            "limitations": [
                                "全样本研究诊断，无留出集，不是事件驱动权威回测，也不是收益预测。",
                                "收益以初始总资产为分母，非黄金满仓收益；买入持有按相同初始仓位持有，不扣交易费用。",
                                ("Au99.99为上金所现货参考，非银行积存金或金条成交价；连续克数、零现金利息，未计渠道价差、保管、提取与回购费用。"
                                 if spot else "未复权ETF价格，分红/份额变更可能失真；连续份额、零现金利息，不含实际最小交易单位与流动性约束。"),
                                "单边10bp/20bp为统一假设，不代表真实券商费用。",
                            ]}
    if len(prices) <= parameters.slow_window:
        unavailable = {"status": "UNAVAILABLE", "trades": [], "reason": "Insufficient history"}
        return {**base, "signal": "INSUFFICIENT_DATA", "target_weight": 0.0,
                "diagnostic": unavailable, "double_cost": {**unavailable, "trades": []}}
    result = _signal(prices, parameters, max_weight)
    return {**base, **result, "as_of": quote.points[-1].date.isoformat(),
            "diagnostic": _diagnose(quote, parameters, max_weight, 0.001),
            "double_cost": _diagnose(quote, parameters, max_weight, 0.002)}