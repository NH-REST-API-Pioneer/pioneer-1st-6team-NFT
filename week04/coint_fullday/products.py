"""품목 코드 → 거래소 (../results/data/products_all.csv)."""
from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

PRODUCTS = Path(__file__).resolve().parent.parent / "results" / "data" / "products_all.csv"


@cache
def _table() -> dict[str, str]:
    with PRODUCTS.open(encoding="utf-8") as f:
        return {r["prd_cd"]: r["exch_cd"] for r in csv.DictReader(f)}


def exch_of(prd: str) -> str:
    return _table()[prd]
