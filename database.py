from pathlib import Path
import sqlite3
from typing import Iterable, List, Dict, Any, Optional
from datetime import date, datetime

DB_PATH = Path(__file__).with_name("accurate_sales.db")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Buka koneksi ke database SQLite."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    # Supaya hasil query bisa diakses dengan nama kolom
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Buat tabel-tabel kalau belum ada."""
    cur = conn.cursor()

    # Tabel utama data penjualan
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_date      TEXT    NOT NULL,
            invoice_no        TEXT    NOT NULL,
            customer          TEXT,
            salesman          TEXT,
            item              TEXT,
            qty               REAL,
            amount            REAL,
            item_category     TEXT,
            city              TEXT,
            customer_type     TEXT
        );
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_detail(invoice_date);"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales_detail(customer);"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_salesman ON sales_detail(salesman);"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_item ON sales_detail(item);"
    )

    # Tabel kode akses (lisensi)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS access_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            customer_name TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT,  -- format YYYY-MM-DD, boleh NULL
            valid_to   TEXT   -- format YYYY-MM-DD, boleh NULL
        );
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_access_code ON access_codes(code);"
    )

    conn.commit()


def clear_sales(conn: sqlite3.Connection) -> None:
    """Kosongkan data (full reload)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM sales_detail;")
    conn.commit()


def insert_rows(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    """Insert banyak baris ke tabel sales_detail."""
    rows = list(rows)
    if not rows:
        return

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO sales_detail (
            invoice_date,
            invoice_no,
            customer,
            salesman,
            item,
            qty,
            amount,
            item_category,
            city,
            customer_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        rows,
    )
    conn.commit()


# ------------------ Helper untuk tanggal ------------------


def _parse_any_date(date_str: str):
    """
    Coba parse tanggal dalam beberapa format umum:
    - YYYY-MM-DD  (contoh: 2025-01-31)
    - DD/MM/YYYY  (contoh: 31/01/2025)
    - DD/MM/YY    (contoh: 31/01/25)
    """
    if not date_str:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def fetch_sales(
    conn: sqlite3.Connection,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Ambil data untuk dikirim ke dashboard.
    start_date & end_date format 'YYYY-MM-DD' (dari input <input type="date">).
    Tanggal di DB boleh format:
    - YYYY-MM-DD
    - DD/MM/YYYY
    - DD/MM/YY
    Filter dilakukan di Python.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            invoice_date,
            invoice_no,
            customer,
            salesman,
            item,
            qty,
            amount,
            item_category,
            city,
            customer_type
        FROM sales_detail
        """
    )
    rows = [dict(row) for row in cur.fetchall()]

    # Kalau tidak ada filter, langsung kembalikan semua
    if not start_date and not end_date:
        return rows

    start_d = None
    end_d = None
    if start_date:
        start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_d = datetime.strptime(end_date, "%Y-%m-%d").date()

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        d_val = _parse_any_date(r.get("invoice_date"))
        if not d_val:
            continue

        if start_d and d_val < start_d:
            continue
        if end_d and d_val > end_d:
            continue

        filtered.append(r)

    return filtered


# ================== FUNGSI UNTUK KODE AKSES ==================


def upsert_access_code(
    conn: sqlite3.Connection,
    code: str,
    customer_name: str,
    active: int = 1,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> None:
    """
    Tambah / update kode akses.
    Kalau code sudah ada -> update datanya.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO access_codes (code, customer_name, active, valid_from, valid_to)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            customer_name = excluded.customer_name,
            active        = excluded.active,
            valid_from    = excluded.valid_from,
            valid_to      = excluded.valid_to;
        """,
        (code, customer_name, active, valid_from, valid_to),
    )
    conn.commit()


def get_active_access_code(
    conn: sqlite3.Connection,
    code: str,
    today: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Cek apakah kode akses:
    - ada di tabel
    - active = 1
    - dan masih dalam masa berlaku (kalau valid_from / valid_to diisi)
    """
    if today is None:
        today = date.today().isoformat()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM access_codes
        WHERE
            code = ?
            AND active = 1
            AND (valid_from IS NULL OR valid_from <= ?)
            AND (valid_to   IS NULL OR valid_to   >= ?)
        """,
        (code, today, today),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(row)