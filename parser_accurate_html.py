from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Optional
import re

from bs4 import BeautifulSoup

# Mapping nama bulan Indo/Eng ke angka
MONTH_MAP = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MEI": 5,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "AGU": 8,
    "SEP": 9,
    "OCT": 10,
    "OKT": 10,
    "NOV": 11,
    "DEC": 12,
    "DES": 12,
}


@dataclass
class SalesRow:
    invoice_date: str
    invoice_no: str
    customer: str
    salesman: str
    item: str
    qty: Optional[float]
    amount: Optional[float]
    item_category: Optional[str]
    city: Optional[str]
    customer_type: Optional[str]

    def to_tuple(self) -> Tuple:
        return (
            self.invoice_date,
            self.invoice_no,
            self.customer,
            self.salesman,
            self.item,
            self.qty,
            self.amount,
            self.item_category,
            self.city,
            self.customer_type,
        )


def _parse_date(text: str) -> str:
    """
    Contoh: '01 Des 2025' -> '2025-12-01'
    Kalau gagal, balikin text aslinya.
    """
    text = text.strip()
    if not text:
        return text
    parts = text.split()
    if len(parts) != 3:
        return text
    day_str, mon_str, year_str = parts
    mon_key = mon_str[:3].upper()
    month = MONTH_MAP.get(mon_key)
    if month is None:
        return text
    try:
        day = int(day_str)
        year = int(year_str)
        dt = datetime(year, month, day)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return text


def _parse_number(text: str) -> Optional[float]:
    """
    Ambil angka dari text: '20,000' -> 20000, '350.000,00' -> 350000 (kurang lebih).
    Kalau kosong / '-' -> None.
    """
    text = text.strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9-]", "", text)
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def parse_html_content(html: str) -> List[SalesRow]:
    """
    Parse isi HTML Accurate (string) jadi list SalesRow.

    Berdasarkan sample:
    - kolom  1 : Date
    - kolom  5 : No. Faktur
    - kolom  9 : Customer
    - kolom 13 : Salesman
    - kolom 17 : Item
    - kolom 21 : Qty
    - kolom 25 : Amount
    - kolom 29 : Item Category
    - kolom 33 : City
    - kolom 37 : Customer Type
    """
    soup = BeautifulSoup(html, "html.parser")
    records: List[SalesRow] = []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        # baris data punya banyak kolom (sekitar 41 kolom)
        if len(tds) < 38:
            continue

        date_text = tds[1].get_text(strip=True)

        # skip baris header / kosong
        if not date_text or date_text == "Date":
            continue

        invoice_no = tds[5].get_text(strip=True)
        customer = tds[9].get_text(strip=True)
        salesman = tds[13].get_text(strip=True)
        item = tds[17].get_text(strip=True)
        qty_text = tds[21].get_text(strip=True)
        amount_text = tds[25].get_text(strip=True)
        item_category = tds[29].get_text(strip=True) if len(tds) > 29 else ""
        city = tds[33].get_text(strip=True) if len(tds) > 33 else ""
        customer_type = tds[37].get_text(strip=True) if len(tds) > 37 else ""

        row = SalesRow(
            invoice_date=_parse_date(date_text),
            invoice_no=invoice_no,
            customer=customer,
            salesman=salesman,
            item=item,
            qty=_parse_number(qty_text),
            amount=_parse_number(amount_text),
            item_category=item_category or None,
            city=city or None,
            customer_type=customer_type or None,
        )
        records.append(row)

    return records