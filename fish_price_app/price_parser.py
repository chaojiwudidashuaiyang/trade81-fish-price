"""Read the two New Stone / Trade81 workbook layouts used by the prototype."""
from __future__ import annotations

from datetime import date, datetime
import re
import unicodedata
from typing import Any

from openpyxl import load_workbook


ALIASES = {
    "縞鯵": "シマアジ",
    "油甘魚": "ハマチ",
    "油金魚": "ハマチ",
    "油金": "ハマチ",
    "蜜柑鯛": "みかん鯛",
    "蜜甘鯛": "みかん鯛",
    # Trade81 order lists use both Japanese and Chinese names for farmed yellowtail.
    "養殖油甘魚": "ハマチ",
    "养殖油甘鱼": "ハマチ",
    "養殖はまち": "ハマチ",
    "養殖ハマチ": "ハマチ",
}


def _used_cells(ws) -> dict[tuple[int, int], Any]:
    # Some supplied workbooks have formatting extending to XFD. Only visit
    # cells that actually contain content.
    return {(c.row, c.column): c.value for c in ws._cells.values() if c.value is not None}


def _date_text(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    if value is None:
        return ""
    match = re.search(r"(20\d{2})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})", str(value))
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).replace(",", "").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def canonical_product_name(value: Any) -> str:
    """Standardize known aliases while retaining size, grade, and origin."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"（[^）]*）|\([^)]*\)", "", text)
    text = re.sub(r"[×x＊*]\s*\d+(?:\.\d+)?\s*(?:尾|匹|個|枚|本|箱|p|パック)?", "", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:尾|匹|個|枚|本|箱)\s*$", "", text)
    for old, new in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(old, new)
    # A description may list two synonymous names side by side, such as
    # "養殖ハマチ 養殖油甘魚". Collapse the duplicate after alias replacement.
    text = re.sub(r"(ハマチ)(?:\s*\1)+", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ・,，;；")


def product_key(value: Any) -> str:
    """Conservative matching key; retain size/specification and remove spaces."""
    return re.sub(r"[\s・,，;；]+", "", canonical_product_name(value)).lower()


def _sheet_with_header(wb, row: int, expected: set[str]):
    for ws in wb.worksheets:
        cells = _used_cells(ws)
        row_values = {str(v).strip() for (r, _c), v in cells.items() if r == row}
        if expected.issubset(row_values):
            return ws, cells
    raise ValueError(f"找不到包含这些表头的工作表：{'、'.join(sorted(expected))}")


def read_price_template(file) -> dict[str, Any]:
    wb = load_workbook(file, data_only=True, read_only=False)
    ws, cells = _sheet_with_header(wb, 7, {"餐廳名稱", "產品名稱", "數量", "單位", "成本+5%", "总数"})
    report_date = _date_text(cells.get((5, 2)))
    factor = _number(cells.get((5, 10))) or 1.05
    rows = []
    for r in range(8, max((row for row, _col in cells), default=7) + 1):
        product = cells.get((r, 5))
        if not isinstance(product, str) or not product.strip():
            continue
        product = product.strip()
        if product.startswith("■"):
            continue
        quote = _number(cells.get((r, 9)))
        rows.append({
            "product": product,
            "canonical_name": canonical_product_name(product),
            "key": product_key(product),
            "fishmonger_quote_jpy": quote,
            "source_row": r,
        })
    return {"date": report_date, "sheet": ws.title, "factor": factor, "rows": rows}


def group_price_rows(sheet: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[float]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sheet["rows"]:
        if row["key"] and row["fishmonger_quote_jpy"] is not None:
            grouped.setdefault(row["key"], []).append(row)
    unique: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, list[float]] = {}
    for key, rows in grouped.items():
        prices = sorted({r["fishmonger_quote_jpy"] for r in rows})
        if len(prices) > 1:
            conflicts[key] = prices
        else:
            unique[key] = {**rows[0], "fishmonger_quote_jpy": prices[0]}
    return unique, conflicts
