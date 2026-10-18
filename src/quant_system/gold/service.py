from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from quant_system.artifacts.envelope import ArtifactEnvelope, hash_bytes
from quant_system.artifacts.registry import ArtifactRegistry
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.types import utc_now
from quant_system.gold.ingestion import fetch_gold
from quant_system.gold.models import Allocation, Quote, allocation_summary

GoldFetcher = Callable[[date, datetime], tuple[list[Quote], dict[str, Any], list[str]]]


def completed_eod_date(now: datetime) -> date:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.date() if local.hour >= 18 else local.date() - timedelta(days=1)


SOURCES = [
    {
        "name": "上海黄金交易所",
        "url": "https://www.sge.com.cn/sjzx/mrhq",
        "description": "Au99.99 人民币现货历史行情",
    },
    {
        "name": "腾讯证券行情",
        "url": "https://gu.qq.com/",
        "description": "黄金 ETF 未复权日线；公开网页接口，无稳定性保证",
    },
    {
        "name": "上海金基准价",
        "url": "https://www.sge.com.cn/sjzx/jzj",
        "description": "SHAU 早盘与午盘基准价（官方查询）",
    },
    {
        "name": "上海期货交易所",
        "url": "https://www.shfe.com.cn/reports/",
        "description": "AU 期货行情、持仓和规则公告",
    },
    {
        "name": "官方储备资产",
        "url": "https://www.safe.gov.cn/safe/whcb/index.html",
        "description": "月度黄金储备数量与价值（官方查询）",
    },
    {
        "name": "人民币汇率中间价",
        "url": "https://www.chinamoney.com.cn/chinese/bkccpr/",
        "description": "汇率背景；非可成交实时汇率",
    },
    {
        "name": "上交所黄金ETF",
        "url": "https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=518880",
        "description": "行情、基金份额、申赎清单与公告（官方查询）",
    },
]


class GoldSnapshot(BaseModel):
    schema_version: str = "1.0"
    fetched_at: datetime
    quotes: list[Quote] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    retained_symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_symbols(self) -> GoldSnapshot:
        if len({quote.symbol for quote in self.quotes}) != len(self.quotes):
            raise ValueError("duplicate snapshot symbols")
        if not any(quote.symbol == "Au99.99" and quote.kind == "spot" for quote in self.quotes):
            raise ValueError("Au99.99 spot benchmark is required")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must include timezone")
        return self


class GoldService:
    def __init__(self, store: LocalArtifactStore, registry: ArtifactRegistry) -> None:
        self.store = store
        self.registry = registry

    def _latest(self, kind: ArtifactType) -> ArtifactEnvelope | None:
        artifacts = self.registry.find_by_type(kind)
        return max(artifacts, key=lambda item: (item.created_at, item.artifact_id)) if artifacts else None

    def _read(self, artifact: ArtifactEnvelope) -> Any:
        content = self.store.get_bytes(artifact.uri)
        if hash_bytes(content) != artifact.content_hash:
            raise ValueError("gold artifact content hash mismatch")
        return self.store.get_json(artifact.uri)

    def load_snapshot(self, artifact_id: str) -> GoldSnapshot:
        artifact = self.registry.get(artifact_id)
        if artifact.artifact_type != ArtifactType.GOLD_SNAPSHOT:
            raise ValueError("not a gold snapshot")
        return GoldSnapshot.model_validate(self._read(artifact))

    def refresh(
        self,
        *,
        as_of: date | None = None,
        fetched_at: datetime | None = None,
        fetcher: GoldFetcher = fetch_gold,
    ) -> tuple[ArtifactEnvelope, GoldSnapshot]:
        now = fetched_at or utc_now()
        if now.tzinfo is None:
            raise ValueError("fetched_at must include timezone")
        cutoff = completed_eod_date(now)
        as_of = as_of or cutoff
        if as_of > cutoff:
            raise ValueError("as-of exceeds the completed EOD cutoff (18:00 Asia/Shanghai)")
        quotes, raw, warnings = fetcher(as_of, now)
        if any(quote.points[-1].date > as_of or quote.fetched_at != now for quote in quotes):
            raise ValueError("source quotation is outside this fetch or EOD cutoff")
        previous = self._latest(ArtifactType.GOLD_SNAPSHOT)
        by_symbol = {quote.symbol: quote for quote in quotes}
        retained_symbols = []
        if previous:
            for old_quote in self.load_snapshot(previous.artifact_id).quotes:
                if old_quote.points[-1].date > as_of:
                    continue
                new_quote = by_symbol.get(old_quote.symbol)
                if new_quote is None or new_quote.points[-1].date < old_quote.points[-1].date:
                    by_symbol[old_quote.symbol] = old_quote
                    retained_symbols.append(old_quote.symbol)
                    warnings.append(
                        f"{old_quote.symbol}: retained prior quotation dated {old_quote.points[-1].date}"
                    )
        snapshot = GoldSnapshot(
            fetched_at=now, quotes=list(by_symbol.values()), warnings=warnings,
            retained_symbols=retained_symbols,
        )
        artifact = self.publish(
            snapshot, raw, previous_snapshot_id=previous.artifact_id if previous else None
        )
        return artifact, snapshot

    def publish(
        self, snapshot: GoldSnapshot, raw: dict[str, Any], *, previous_snapshot_id: str | None = None
    ) -> ArtifactEnvelope:
        raw_uri, raw_hash = self.store.put_content_addressed_json("gold/raw", raw)
        source = self.registry.register(
            ArtifactEnvelope(
                artifact_type=ArtifactType.DATA_SNAPSHOT,
                producer="gold-public-data",
                uri=raw_uri,
                content_hash=raw_hash,
                status=ArtifactStatus.VERIFIED,
                metadata={"scope": "gold", "fetched_at": snapshot.fetched_at.isoformat()},
            )
        )
        payload = snapshot.model_dump(mode="json")
        uri, digest = self.store.put_content_addressed_json("gold/snapshots", payload)
        return self.registry.register(
            ArtifactEnvelope(
                artifact_type=ArtifactType.GOLD_SNAPSHOT,
                producer="gold-public-data",
                uri=uri,
                content_hash=digest,
                data_snapshot_id=source.artifact_id,
                parent_artifact_ids=tuple(
                    sorted({source.artifact_id, *([previous_snapshot_id] if previous_snapshot_id else [])})
                ),
                status=ArtifactStatus.VERIFIED,
                metadata={"symbols": [quote.symbol for quote in snapshot.quotes]},
            )
        )

    def save_allocation(self, record: Allocation) -> None:
        previous = self._latest(ArtifactType.GOLD_ALLOCATION)
        uri, digest = self.store.put_content_addressed_json(
            "gold/allocations", record.model_dump(mode="json")
        )
        if previous is not None and previous.content_hash == digest:
            return
        self.registry.register(
            ArtifactEnvelope(
                artifact_type=ArtifactType.GOLD_ALLOCATION,
                producer="manual-gold-allocation",
                uri=uri,
                content_hash=digest,
                parent_artifact_ids=(previous.artifact_id,) if previous else (),
                metadata={"account_type": "manual", "broker_synced": False},
            )
        )

    def dashboard(self) -> dict[str, Any]:
        artifact = self._latest(ArtifactType.GOLD_SNAPSHOT)
        snapshot = GoldSnapshot.model_validate(self._read(artifact)) if artifact else None
        allocation_artifact = self._latest(ArtifactType.GOLD_ALLOCATION)
        allocation = (
            Allocation.model_validate(self._read(allocation_artifact))
            if allocation_artifact
            else Allocation()
        )
        today = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        prices = {quote.symbol: quote.points[-1].close for quote in snapshot.quotes} if snapshot else {}
        return {
            "snapshot": {
                **snapshot.model_dump(mode="json"),
                "artifact_id": artifact.artifact_id,
                "quotes": [
                    {**quote.model_dump(mode="json"), "stale": quote.is_stale(today)}
                    for quote in snapshot.quotes
                ],
            }
            if snapshot and artifact
            else None,
            "allocation": {
                "artifact_id": allocation_artifact.artifact_id if allocation_artifact else None,
                "updated_at": allocation_artifact.created_at.isoformat() if allocation_artifact else None,
                "record": allocation.model_dump(mode="json"),
            },
            "summary": allocation_summary(allocation, prices),
            "sources": SOURCES,
            "futures": {"trade_date": None, "contracts": []},
        }
