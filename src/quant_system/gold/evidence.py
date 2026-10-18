"""Locally computed evidence, definitions and advice constraints for gold research.

Pure fact builders; no LLM calls and no persistence.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_system.agents.deliberation import EvidenceItem, EvidencePackage
from quant_system.gold.models import Quote
from quant_system.gold.schemas import AdviceStance, GoldOrchestratorDecision
from quant_system.research.runtime.models import EvidenceRef

ADVICE_DEFINITIONS = {
    "gold_return_126": "Au99.99收盘价相对126个观测前的收益，约半年，不是未来收益。",
    "gold_return_252": "Au99.99收盘价相对252个观测前的收益，约一年。",
    "gold_ma120_gap": "当前现货收盘价/最近120个收盘均值-1，非均线斜率。",
    "gold_ma250_gap": "当前现货收盘价/最近250个收盘均值-1，非均线斜率。",
    "gold_drawdown_252": "当前现货收盘价/最近252个观测最高收盘价-1，仅为距滚动高点回撤，不是区间最大回撤。",
    "diagnostic_excess_return": "策略诊断收益减同初始仓位持有收益，非真实样本外超额。",
    "double_cost_return": "20bp单边成本诊断收益，以初始总资产为分母。",
    "independent_oos": "固定false，未完成独立样本外，不能称已验证。",
}
MACRO_DEFINITIONS = {
    "real_yield": "美国实际利率序列。",
    "nominal_yield_10y": "美国10年期国债名义收益率，单位百分数。",
    "broad_dollar_index": "名义广义贸易加权美元指数，2006年1月=100，不是ICE DXY。",
    "usd_cny_ny_noon": "纽约午间人民币/1美元买入汇率，不是人民银行中间价。",
    "china_gold_reserves_quantity": "中国官方黄金储备存量，万金衡盎司，不代表全球央行需求。",
    "china_gold_reserves_value": "中国黄金储备美元估值，亿美元，变化不等于购金数量。",
    "dollar_index": "美元指数序列。",
    "inflation": "通胀与预期通胀序列。",
    "fed_expectations": "美联储政策利率预期。",
    "etf_flows": "黄金ETF资金流。",
    "futures_positioning": "黄金期货持仓结构。",
    "central_bank_demand": "央行黄金需求。",
    "news_events": "具有可用时间戳的新闻与事件。",
}

MacroEvidenceReader = Callable[[str, datetime], dict[str, Any]]


class GoldMacroEvidence(BaseModel):
    """Reader result: snapshot_id, canonical macro items, and FACT evidence references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    items: dict[str, EvidenceItem]
    evidence: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def facts_only(self) -> GoldMacroEvidence:
        if self.items.keys() - MACRO_DEFINITIONS.keys():
            raise ValueError("unknown gold macro evidence categories")
        if any(item.category != "macro" for item in self.items.values()):
            raise ValueError("macro reader may return macro facts only")
        if any(reference.kind != "FACT" for reference in self.evidence):
            raise ValueError("macro reader cannot supply agent opinions")
        if self.snapshot_id not in {reference.artifact_id for reference in self.evidence}:
            raise ValueError("macro reader must reference its requested snapshot")
        return self


def quote_facts(quote: Quote) -> dict[str, Any]:
    prices = [float(point.close) for point in quote.points]
    return {
        "name": quote.name,
        "symbol": quote.symbol,
        "kind": quote.kind,
        "unit": quote.unit,
        "as_of": quote.points[-1].date.isoformat(),
        "close": prices[-1],
        "observations": len(prices),
        "source": quote.source,
        "source_url": quote.source_url,
        "fetched_at": quote.fetched_at.isoformat(),
        "price_returns": {
            str(window): prices[-1] / prices[-window - 1] - 1 if len(prices) > window else None
            for window in (1, 5, 20, 60, 126, 252)
        },
    }


def diagnostic_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in evaluation.items() if key not in {"diagnostic", "double_cost"}},
        **{
            key: {
                name: value
                for name, value in evaluation.get(key, {}).items()
                if name not in {"curve", "trades"}
            }
            for key in ("diagnostic", "double_cost")
        },
    }


def advice_evidence(quote: Quote, evaluation: dict[str, Any]) -> dict[str, float | bool | None]:
    prices = [float(point.close) for point in quote.points]
    diagnostic = evaluation["diagnostic"]
    total_return = diagnostic.get("total_return")
    benchmark = diagnostic.get("buy_hold_same_cap_return")
    return {
        "gold_return_126": prices[-1] / prices[-127] - 1 if len(prices) >= 127 else None,
        "gold_return_252": prices[-1] / prices[-253] - 1 if len(prices) >= 253 else None,
        "gold_ma120_gap": prices[-1] / statistics.mean(prices[-120:]) - 1 if len(prices) >= 120 else None,
        "gold_ma250_gap": prices[-1] / statistics.mean(prices[-250:]) - 1 if len(prices) >= 250 else None,
        "gold_drawdown_252": prices[-1] / max(prices[-252:]) - 1 if len(prices) >= 252 else None,
        "diagnostic_excess_return": total_return - benchmark
        if total_return is not None and benchmark is not None
        else None,
        "double_cost_return": evaluation["double_cost"].get("total_return"),
        "independent_oos": False,
    }


def allowed_advice_stances(evidence: dict[str, float | bool | None]) -> list[AdviceStance]:
    trend_keys = ("gold_return_126", "gold_return_252", "gold_ma120_gap", "gold_ma250_gap")
    trend = [evidence[key] for key in trend_keys]
    if any(value is None for value in trend) or evidence["gold_drawdown_252"] is None:
        return ["WAIT"]
    if (
        all(value is not None and value > 0 for value in trend)
        and float(evidence["gold_drawdown_252"] or 0) > -0.15
    ):
        return ["WAIT", "GRADUAL_ALLOCATION"]
    return ["WAIT", "DEFENSIVE"]


def evidence_package(context: dict[str, Any], evaluation: dict[str, Any]) -> EvidencePackage:
    facts = context["facts"]
    values = facts["investment_advice_evidence"]
    items = {
        key: EvidenceItem(
            category="price" if key.startswith("gold_") else "diagnostic",
            value=value,
            definition=ADVICE_DEFINITIONS[key],
            source="Au99.99" if key.startswith("gold_") else "local-diagnostic",
        )
        for key, value in values.items()
    }
    for key, definition in MACRO_DEFINITIONS.items():
        items[key] = EvidenceItem.model_validate(
            facts.get("macro_items", {}).get(
                key, {"category": "macro", "value": None, "definition": definition, "source": "not-collected"}
            )
        )
    for key in ("close", "fast_average", "slow_average", "annualized_volatility", "trailing_reference"):
        items[f"selected_{key}"] = EvidenceItem(
            category="price",
            value=evaluation.get(key),
            definition=f"所选研究对象的本地指标：{key}，价格单位见selected_unit。",
            source=facts["selected_instrument"]["symbol"],
        )
    items["selected_unit"] = EvidenceItem(
        category="constraint", value=evaluation["unit"], definition="价格单位。", source="instrument"
    )
    return EvidencePackage(
        snapshot_id=context["snapshot_id"],
        as_of=context["as_of"],
        items=items,
        limitations=[
            *evaluation["limitations"],
            "宏观覆盖不全时仅允许WAIT；置信度是模型自评，不是已校准胜率。",
        ],
    )


def validate_advice(decision: GoldOrchestratorDecision, evidence: dict[str, Any]) -> None:
    advice = decision.investment_advice
    available = evidence["analysis_context"]["facts"]["investment_advice_evidence"]
    if advice.stance not in evidence["investment_advice_constraints"]["allowed_stances"]:
        raise ValueError("investment advice stance conflicts with local long-term evidence constraints")
    keys = [reason.evidence_key for reason in advice.reasons]
    if len(keys) != len(set(keys)) or any(available.get(key) is None for key in keys):
        raise ValueError("investment advice must cite distinct, available local evidence keys")
    if not any(key.startswith("gold_") for key in keys):
        raise ValueError("investment advice must cite at least one medium/long-term gold price fact")
    for reason in advice.reasons:
        if reason.evidence_key == "gold_drawdown_252" and any(
            term in reason.implication.lower() for term in ("最大回撤", "maximum drawdown", "max drawdown")
        ):
            raise ValueError(
                "gold_drawdown_252 is current distance from the rolling high, not maximum drawdown"
            )
