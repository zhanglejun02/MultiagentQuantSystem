"""SAFE official reserve-assets discovery and merged HTML table parsing."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from quant_system.macro.models import SERIES, MacroObservation

SAFE_INDEX_URL = "https://www.safe.gov.cn/safe/whcb/index.html"
_GOLD = "\u9ec4\u91d1"
_OUNCES = "\u4e07\u76ce\u53f8"
_USD = "\u4ebf\u7f8e\u5143"


def validate_safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.safe.gov.cn", "safe.gov.cn"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path.startswith("/safe/")
        or not parsed.path.endswith(".html")
        or parsed.query
        or parsed.fragment
        or "/../" in parsed.path
        or "%" in parsed.path
    ):
        raise ValueError("only allowlisted official SAFE HTML pages are permitted")
    return url


@dataclass
class _Cell:
    text: str
    rowspan: int
    colspan: int


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.tables: list[list[list[_Cell]]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._table_depth = 0
        self._table: list[list[_Cell]] = []
        self._row: list[_Cell] = []
        self._cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a":
            self._href = attributes.get("href")
            self._link_text = []
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table = []
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            rowspan = int(attributes.get("rowspan") or "1")
            colspan = int(attributes.get("colspan") or "1")
            if not 1 <= rowspan <= 128 or not 1 <= colspan <= 128:
                raise ValueError("SAFE table span exceeds parser bounds")
            self._cell = _Cell("", rowspan, colspan)
        elif tag == "br" and self._cell is not None:
            self._cell.text += " "

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._link_text.append(data)
        if self._table_depth == 1 and self._cell is not None:
            self._cell.text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._link_text)))
            self._href = None
        if self._table_depth == 1:
            if tag in {"td", "th"} and self._cell is not None:
                self._row.append(self._cell)
                self._cell = None
            elif tag == "tr":
                self._table.append(self._row)
            elif tag == "table":
                self.tables.append(self._table)
        if tag == "table":
            self._table_depth -= 1


def _document(html: str) -> _Document:
    document = _Document()
    document.feed(html)
    document.close()
    return document


def discover_safe_year_pages(html: str, *, source_url: str = SAFE_INDEX_URL) -> dict[int, str]:
    pages: dict[int, str] = {}
    for href, title in _document(html).links:
        if (
            "\u5b98\u65b9\u50a8\u5907\u8d44\u4ea7" not in title
            and "official reserve assets" not in title.lower()
        ):
            continue
        years = re.findall(r"(?:19|20)\d{2}", title)
        if len(years) != 1:
            continue
        try:
            pages.setdefault(int(years[0]), validate_safe_url(urljoin(source_url, href)))
        except ValueError:
            continue
    return pages


def discover_safe_html_page(html: str, *, source_url: str) -> str | None:
    for href, title in _document(html).links:
        if title.strip().lower() in {"html", "html\u683c\u5f0f"}:
            return validate_safe_url(urljoin(source_url, href))
    return None


def _expand(rows: list[list[_Cell]]) -> list[list[str]]:
    cells: dict[tuple[int, int], str] = {}
    width = 0
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row:
            while (row_index, column) in cells:
                column += 1
            if row_index + cell.rowspan > 512 or column + cell.colspan > 128:
                raise ValueError("SAFE table exceeds parser bounds")
            for covered_row in range(row_index, row_index + cell.rowspan):
                for covered_column in range(column, column + cell.colspan):
                    if (covered_row, covered_column) in cells:
                        raise ValueError("overlapping SAFE table spans")
                    cells[covered_row, covered_column] = " ".join(cell.text.split())
            column += cell.colspan
            width = max(width, column)
    height = max((row for row, _ in cells), default=-1) + 1
    return [[cells.get((row, column), "") for column in range(width)] for row in range(height)]


def _month(text: str, year: int | None) -> tuple[int, int] | None:
    matched = re.fullmatch(r"(?:(\d{4})[./\-\u5e74])?(\d{1,2})(?:\u6708)?", text.strip())
    if matched is None or not 1 <= int(matched[2]) <= 12:
        return None
    actual_year = int(matched[1]) if matched[1] else year
    return (actual_year, int(matched[2])) if actual_year is not None else None


def _has_ounces(text: str) -> bool:
    normalized = re.sub(r"[\s,]", "", text).lower()
    return _OUNCES in normalized or "10000troyounces" in normalized or "10000fineounces" in normalized


def _number(text: str) -> float | None:
    normalized = text.replace(_OUNCES, "")
    normalized = re.sub(r"[\s,]", "", normalized)
    if normalized.lower() in {"", ".", "-", "--", "...", "n/a"}:
        return None
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized) is None:
        raise ValueError("SAFE gold cell is not an unambiguous number")
    return float(normalized)


def parse_safe_observations(
    html: str,
    *,
    source_url: str,
    ingested_at: datetime,
    year: int | None = None,
    raw_content_hash: str = "",
) -> list[MacroObservation]:
    validate_safe_url(source_url)
    observations: dict[tuple[str, date], MacroObservation] = {}
    for table in _document(html).tables:
        grid = _expand(table)
        header: tuple[int, dict[int, tuple[int, int]]] | None = None
        for row_index, row in enumerate(grid):
            months = {
                column: month_key for column, text in enumerate(row) if (month_key := _month(text, year))
            }
            if months:
                header = row_index, months
                break
        if header is None:
            continue
        header_index, month_columns = header
        first_month_column = min(month_columns)
        gold_index = next(
            (
                row_index
                for row_index, row in enumerate(grid)
                if row_index > header_index
                and any(
                    _GOLD in text or re.search(r"\bgold\b", text, re.I) for text in row[:first_month_column]
                )
            ),
            None,
        )
        if gold_index is None:
            continue
        for row_index in range(gold_index, min(gold_index + 3, len(grid))):
            row = grid[row_index]
            is_quantity = any(_has_ounces(text) for text in row)
            if row_index != gold_index and not is_quantity:
                continue
            for column, (column_year, month) in month_columns.items():
                if year is not None and column_year != year:
                    raise ValueError("SAFE table year does not match the discovered publication")
                if is_quantity:
                    series_id = "CN_GOLD_RESERVES"
                else:
                    headings = [
                        re.sub(r"\s", "", grid[index][column]).lower()
                        for index in range(header_index + 1, gold_index)
                    ]
                    currency = next(
                        (
                            text
                            for text in reversed(headings)
                            if "usd" in text or "sdr" in text or _USD in text
                        ),
                        "",
                    )
                    if ("usd" not in currency and _USD not in currency) or "sdr" in currency:
                        continue
                    if _USD not in currency and "100millionusd" not in currency:
                        raise ValueError("SAFE dollar valuation unit is not hundred million USD")
                    series_id = "CN_GOLD_RESERVES_USD"
                value = _number(row[column])
                if value is None:
                    continue
                observed = date(column_year, month, calendar.monthrange(column_year, month)[1])
                observation = MacroObservation(
                    series_id=series_id,
                    observation_date=observed,
                    period=f"{column_year:04d}-{month:02d}",
                    value=value,
                    unit=SERIES[series_id].unit,
                    source="SAFE",
                    source_url=source_url,
                    ingested_at=ingested_at,
                    available_at=ingested_at,
                    vintage={"kind": "current_annual_table_not_historical_pit"},
                    raw_content_hash=raw_content_hash,
                )
                key = series_id, observed
                if key in observations and observations[key].value != value:
                    raise ValueError("conflicting duplicate SAFE month values")
                observations[key] = observation
    if not observations:
        raise ValueError("SAFE official gold table or explicit units not found")
    return [observations[key] for key in sorted(observations)]
