"""Frozen full SAFE HTML response, not a hand-reconstructed historical vintage."""

from __future__ import annotations

import base64
import gzip
from datetime import UTC, date, datetime

from quant_system.artifacts.envelope import hash_bytes
from quant_system.macro.safe import parse_safe_observations

SOURCE_URL = "https://www.safe.gov.cn/safe/2026/0206/27116.html"
RESPONSE_SHA256 = "1fa0dded1b810ca521872d21eed3b80435a0d9a0ec9605e076a8d29fc83827c1"
FROZEN_RESPONSE = (
    "H4sIAAAAAAACE+09bXPbRnqf41+BMHP3oVOC+4JXSuLFkezU0945YzmXtnEmA5KQhJokWBD0y3Vuxo3tk52effHkYsdxrolzdpNp"
    "El1yySWK3/JjKlLSJ/+F7i5AcgGCNCivKDEmPRbBZ1+e193n2QeLxezzC0fnj//LK4ekFb9akQoHZuk3/bKtMvmq2r5Fivx61v73"
    "pnNqLjPv1ny75mePn63bGakU/JrL+PYZP0ebzkilFctr2P5c01/KGpmkPv45++rB7LxbrVu+U6zw3Rw5NHeo2qxYvn3kEIQZKVc4"
    "IElBBzWras9lFh3f/hW54tq0bj1srX3XunO9/fXq5trtzXd+1/r63PaNz9qXvt98eG3z85sZKamXBbdqOTWun9OnT8sNa8mWl91T"
    "cqmW3OrIwrxb5rEXq7oC6Af2N/BXnMa8W2lWeTSb9z/aWrvdfu+r9pW1TLxFidUuO416xTpbi/G59n77+g+ttz5r3Vnd+tuFjXuf"
    "9jU/6PlOqWIfd/zK8JaPH1xCAGmPH1zu6+OVZnGByJ9rTmtmIcgCva9yaA2LbtMrPVEnfa0X7EbJc+q+4/ICat/4mIpy1qdcFGZz"
    "wfcBabbi1E5Knl2ZyzRWXM8vNX3JKdGmPrHFuYxTtZbt3JlsAFvx7KW5TI4qNOfb1Tq1Kc9uMEJzWEdFWF4qW0UEFGjAollUlkwF"
    "W4ZtIs3ScqyvBmstk/4yUtUuOxZBXPJsu8bscjYXDpGiWz5bOPDcbNk5VXj68ZIrRJThuctHyly7Q2dKdkVeXLFtP1b1Zbtme5bv"
    "elzt115ZlI4uLTklWzp0nNZ/Ppt93VmSln1bqjZcyXyjMHuGDPdZN7/glppV0ohgrNvEjOwGhR5s+kTUhWbD9mZz3Z+kYN6ziUDL"
    "BQRgaBvHgZkHWh7rtGKnmNT8J6vhh+3qxRIt5CBh+aJ1ivWFuL4QzCtmp3pQgf5KpnO+2fDdanLZPy4eJYBys+S/1HQq5V/bnlT2"
    "82UinobvObXlDEGsoiyEsiEjGSJoqBRRfzPa2ZH5X8dbLywYh6F++NA8UBcUBb+kQ800D2kHDajqGB7WaGekGaN+MJ05pofnX7fJ"
    "6F96I5stzDb8s9T+icqkF+vEHA889x9Vy1t2ankg66pTk4CMAPvifs0ceI4oNktN0/ay3foYdYuWXNcfUEQ04vyG2I5VyVKEWavi"
    "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"
    "XvGrlcL/A5fkQh4RwgAA"
)


def test_macro_safe_full_official_response_quantity_valuation_and_provenance() -> None:
    body = gzip.decompress(base64.b64decode(FROZEN_RESPONSE))
    assert len(body) == 49681
    assert hash_bytes(body) == RESPONSE_SHA256
    fetched_at = datetime(2026, 10, 17, 8, tzinfo=UTC)
    observations = parse_safe_observations(
        body.decode("utf-8"),
        source_url=SOURCE_URL,
        ingested_at=fetched_at,
        year=2026,
        raw_content_hash=RESPONSE_SHA256,
    )
    quantity = [row for row in observations if row.series_id == "CN_GOLD_RESERVES"]
    valuation = [row for row in observations if row.series_id == "CN_GOLD_RESERVES_USD"]
    assert len(quantity) == len(valuation) == 9
    assert quantity[0].value == 7419
    assert quantity[-1].value == 7747
    assert quantity[-1].observation_date == date(2026, 9, 30)
    assert valuation[-1].value == 3235.17
    assert all(
        row.source_url == SOURCE_URL and row.raw_content_hash == RESPONSE_SHA256 for row in observations
    )
    assert all(row.available_at == fetched_at and row.published_at is None for row in observations)
