from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO


@dataclass
class GenericPortfolioRow:
    symbol: str
    quantity: float | None
    price: float | None
    value: float | None
    raw: dict[str, str]


class GenericPortfolioCsvParser:
    def parse_text(self, text: str) -> list[GenericPortfolioRow]:
        reader = csv.DictReader(StringIO(text))
        rows: list[GenericPortfolioRow] = []
        for row in reader:
            symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
            if not symbol:
                continue
            quantity = self._num(row.get("Quantity") or row.get("quantity"))
            price = self._num(row.get("Last Price") or row.get("price") or row.get("Price"))
            value = self._num(row.get("Current Value") or row.get("value") or row.get("Value"))
            rows.append(GenericPortfolioRow(symbol=symbol, quantity=quantity, price=price, value=value, raw=row))
        return rows

    @staticmethod
    def _num(value: str | None) -> float | None:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).replace("$", "").replace(",", "").strip())
