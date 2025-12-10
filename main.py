from typing import Optional, List, Dict
from collections import defaultdict
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from database import (
    get_connection,
    init_db,
    clear_sales,
    insert_rows,
    fetch_sales,
    get_active_access_code,  # cek kode akses ke DB
    upsert_access_code,      # untuk auto-bikin kode akses
)
from parser_accurate_html import parse_html_content

app = FastAPI(title="Accurate Sales Dashboard")

ACCESS_COOKIE_NAME = "access_code"


# ================== FUNGSI KODE AKSES ==================


def is_authorized(request: Request) -> bool:
    """Cek apakah user punya kode akses valid (di cookie & masih aktif di DB)."""
    code = request.cookies.get(ACCESS_COOKIE_NAME)
    if not code:
        return False

    conn = get_connection()
    init_db(conn)  # jaga-jaga kalau tabel belum ada
    record = get_active_access_code(conn, code)
    conn.close()

    return record is not None


def render_access_page(message: str = "", is_error: bool = False) -> str:
    msg_html = ""
    if message:
        color = "danger" if is_error else "success"
        msg_html = f'<div class="alert alert-{color} mt-3" role="alert">{message}</div>'

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Masukkan Kode Akses</title>
        <link
          href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
          rel="stylesheet"
        >
        <style>
            body {{
                margin: 0;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: radial-gradient(circle at top, #0f172a 0, #020617 55%);
                color: #e5e7eb;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }}
            .login-card {{
                max-width: 420px;
                width: 100%;
                background: rgba(15,23,42,0.9);
                border-radius: 18px;
                padding: 28px 24px 22px;
                box-shadow: 0 20px 45px rgba(0,0,0,0.7);
                border: 1px solid rgba(148,163,184,0.4);
                backdrop-filter: blur(16px);
            }}
            .login-title {{
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 4px;
                text-align: center;
            }}
            .login-sub {{
                font-size: 13px;
                color: #9ca3af;
                text-align: center;
                margin-bottom: 18px;
            }}
            label {{
                font-size: 13px;
                color: #cbd5f5;
            }}
            input.form-control {{
                background-color: #020617;
                border-radius: 10px;
                border: 1px solid #1e293b;
                color: #e5e7eb;
                font-size: 14px;
            }}
            input.form-control:focus {{
                border-color: #6366f1;
                box-shadow: 0 0 0 1px rgba(99,102,241,0.6);
            }}
            .btn-primary {{
                background: linear-gradient(135deg, #6366f1, #22c55e);
                border: none;
                border-radius: 999px;
                font-weight: 600;
            }}
            .btn-primary:hover {{
                opacity: 0.9;
            }}
            .footer-note {{
                margin-top: 16px;
                font-size: 11px;
                color: #64748b;
                text-align: center;
            }}
        </style>
    </head>
    <body>
        <div class="login-card">
            <div class="login-title">Masukkan Kode Akses</div>
            <div class="login-sub">
                Kode akses didapat dari vendor aplikasi dashboard Accurate.
            </div>
            <form action="/access" method="post">
                <div class="mb-3">
                    <label for="code" class="form-label">Kode Akses</label>
                    <input type="text" class="form-control" id="code" name="code"
                           placeholder="Contoh: ABC-2025" required>
                </div>
                <button type="submit" class="btn btn-primary w-100">Masuk ke Dashboard</button>
            </form>
            {msg_html}
            <div class="footer-note">
                ACA Cloud • ACIS Indonesia
            </div>
        </div>
    </body>
    </html>
    """


@app.get("/access", response_class=HTMLResponse)
def access_page():
    return render_access_page()


@app.post("/access", response_class=HTMLResponse)
async def access_submit(code: str = Form(...)):
    code = code.strip()

    conn = get_connection()
    init_db(conn)
    record = get_active_access_code(conn, code)
    conn.close()

    if record:
        resp = RedirectResponse(url="/", status_code=302)
        # httponly supaya tidak bisa diubah dari JS/browser
        resp.set_cookie(ACCESS_COOKIE_NAME, code, httponly=True)
        return resp
    else:
        return render_access_page("Kode akses salah atau sudah tidak aktif.", is_error=True)


# ================== LOGIKA AGREGASI DASHBOARD (BACKEND) ==================


def _aggregate_top_n(
    rows: List[Dict],
    key: str,
    n: int = 10,
) -> List[Dict]:
    """Total amount per dimensi (customer, salesman, dll) lalu ambil Top N."""
    totals = defaultdict(float)
    for r in rows:
        label = r.get(key)
        if not label:
            continue
        amount = r.get("amount") or 0
        totals[label] += float(amount)

    sorted_items = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:n]
    return [{"label": k, "amount": v} for k, v in sorted_items]


def _get_year_month(date_str: str):
    """
    Baca bulan & tahun dari berbagai format:
    - YYYY-MM-DD
    - DD/MM/YYYY
    - DD/MM/YY
    """
    if not date_str:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.year, dt.month
        except ValueError:
            continue
    return None


def _get_prev_year_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _build_monthly_top(
    rows: List[Dict],
    dim_key: str,
    base_year: int,
    base_month: int,
) -> List[Dict]:
    """
    Hitung Top 10 per dimensi (item/salesman) untuk:
    - bulan ini (base_year, base_month)
    - bulan lalu
    """
    if base_year is None or base_month is None:
        return []

    prev_year, prev_month = _get_prev_year_month(base_year, base_month)

    cur_totals = defaultdict(float)
    prev_totals = defaultdict(float)

    for r in rows:
        d = r.get("invoice_date")
        ym = _get_year_month(d) if d else None
        if ym is None:
            continue
        y, m = ym
        amount = float(r.get("amount") or 0)
        key = r.get(dim_key)
        if not key:
            continue

        if y == base_year and m == base_month:
            cur_totals[key] += amount
        elif y == prev_year and m == prev_month:
            prev_totals[key] += amount

    labels = set(cur_totals.keys()) | set(prev_totals.keys())
    data_list = []
    for label in labels:
        cur_val = cur_totals.get(label, 0.0)
        prev_val = prev_totals.get(label, 0.0)
        data_list.append(
            {
                "label": label,
                "current": cur_val,
                "previous": prev_val,
            }
        )

    data_list.sort(key=lambda x: x["current"], reverse=True)
    return data_list[:10]


def build_dashboard_data(
    rows: List[Dict],
) -> Dict:
    """
    Ambil list transaksi → kembalikan data ringkasan untuk dashboard.
    """
    total_sales = 0.0
    customers_set = set()
    all_months = []

    for r in rows:
        amt = r.get("amount") or 0
        total_sales += float(amt)
        customer = r.get("customer")
        if customer:
            customers_set.add(customer)
        d = r.get("invoice_date")
        ym = _get_year_month(d) if d else None
        if ym:
            all_months.append(ym)

    # Top 10 dimensi
    top10_customer = _aggregate_top_n(rows, "customer")
    top10_customer_type = _aggregate_top_n(rows, "customer_type")
    top10_city = _aggregate_top_n(rows, "city")
    top10_salesman = _aggregate_top_n(rows, "salesman")
    top10_item = _aggregate_top_n(rows, "item")
    top10_category = _aggregate_top_n(rows, "item_category")

    # Top customer (nama saja)
    top_customer_name = top10_customer[0]["label"] if top10_customer else "-"

    # ===== BULAN INI vs BULAN LALU (berdasarkan bulan terakhir di data) =====
    month_current_total = None
    month_prev_total = None
    month_diff = None
    month_diff_pct = None
    item_mom_top10: List[Dict] = []
    salesman_mom_top10: List[Dict] = []

    if all_months:
        # Ambil bulan paling akhir dari data sebagai "bulan ini"
        base_year, base_month = max(all_months)

        prev_year, prev_month = _get_prev_year_month(base_year, base_month)

        cur_total = 0.0
        prev_total = 0.0
        for r in rows:
            d = r.get("invoice_date")
            ym = _get_year_month(d) if d else None
            if ym is None:
                continue
            y, m = ym
            amt = float(r.get("amount") or 0)
            if y == base_year and m == base_month:
                cur_total += amt
            elif y == prev_year and m == prev_month:
                prev_total += amt

        month_current_total = cur_total
        month_prev_total = prev_total
        month_diff = cur_total - prev_total
        if prev_total != 0:
            month_diff_pct = month_diff / prev_total
        else:
            month_diff_pct = None

        # Top 10 per barang (bulan ini vs bulan lalu)
        item_mom_top10 = _build_monthly_top(rows, "item", base_year, base_month)
        # Top 10 per salesman (bulan ini vs bulan lalu)
        salesman_mom_top10 = _build_monthly_top(rows, "salesman", base_year, base_month)

    return {
        "total_sales": total_sales,
        "customer_count": len(customers_set),
        "top_customer": top_customer_name,
        "top10_customer": top10_customer,
        "top10_customer_type": top10_customer_type,
        "top10_city": top10_city,
        "top10_salesman": top10_salesman,
        "top10_item": top10_item,
        "top10_category": top10_category,
        "total_month_current": month_current_total,
        "total_month_prev": month_prev_total,
        "total_month_diff": month_diff,
        "total_month_diff_pct": month_diff_pct,
        "item_mom_top10": item_mom_top10,
        "salesman_mom_top10": salesman_mom_top10,
    }


# ================== HTML DASHBOARD ==================


def render_dashboard(
    status_message: str = "",
    status_level: str = "info",  # "success" | "error" | "info"
) -> str:
    if status_level == "success":
        status_class = "text-success"
    elif status_level == "error":
        status_class = "text-danger"
    else:
        status_class = "text-muted"

    status_html = (
        f'<div class="{status_class}" style="margin-top:4px;">{status_message}</div>'
        if status_message
        else ""
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Power BI Accurate - Monitoring Penjualan</title>
        <!-- Bootstrap 5 CDN -->
        <link
          href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
          rel="stylesheet"
        >
        <!-- Chart.js CDN -->
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

        <style>
            * {{
                box-sizing: border-box;
            }}
            body {{
                margin: 0;
                background: radial-gradient(circle at top, #0f172a 0, #020617 55%);
                color: #e5e7eb;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }}
            .navbar-custom {{
                position: sticky;
                top: 0;
                z-index: 20;
                backdrop-filter: blur(18px);
                background: linear-gradient(135deg, rgba(15,23,42,0.96), rgba(15,23,42,0.9));
                border-bottom: 1px solid rgba(148,163,184,0.25);
            }}
            .navbar-inner {{
                max-width: 1200px;
                margin: 0 auto;
                padding: 14px 20px;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }}
            .navbar-kicker {{
                font-size: 11px;
                color: #38bdf8;
                text-transform: uppercase;
                letter-spacing: 0.14em;
            }}
            .navbar-title {{
                font-size: 20px;
                font-weight: 600;
            }}
            .navbar-pill {{
                padding: 6px 14px;
                border-radius: 999px;
                background: linear-gradient(135deg, #22c55e, #16a34a);
                color: #022c22;
                font-size: 12px;
                font-weight: 600;
            }}

            .content-wrapper {{
                max-width: 1200px;
                margin: 0 auto;
                padding: 22px 20px 40px 20px;
            }}

            .upload-box {{
                background: rgba(15,23,42,0.9);
                border-radius: 18px;
                padding: 16px 18px;
                box-shadow: 0 16px 40px rgba(0,0,0,0.7);
                border: 1px solid rgba(148,163,184,0.35);
                margin-bottom: 24px;
            }}

            .upload-box .form-label {{
                font-size: 13px;
                color: #cbd5f5;
            }}
            .upload-box input[type="file"] {{
                background-color: #020617;
                border-radius: 10px;
                border: 1px solid #1e293b;
                color: #e5e7eb;
                font-size: 13px;
            }}
            .upload-box input[type="file"]:focus {{
                border-color: #6366f1;
                box-shadow: 0 0 0 1px rgba(99,102,241,0.6);
            }}
            .upload-box small {{
                color: #64748b;
            }}

            .form-check-label {{
                color: #cbd5f5;
                font-size: 13px;
            }}

            .btn-success {{
                background: linear-gradient(135deg, #22c55e, #16a34a);
                border: none;
                border-radius: 999px;
                font-weight: 600;
            }}
            .btn-success:hover {{
                opacity: 0.92;
            }}

            .filters-row label.form-label {{
                font-size: 13px;
                color: #cbd5f5;
            }}
            .filters-row input[type="date"] {{
                background-color: #020617;
                border-radius: 10px;
                border: 1px solid #1e293b;
                color: #e5e7eb;
                font-size: 13px;
            }}
            .filters-row input[type="date"]:focus {{
                border-color: #38bdf8;
                box-shadow: 0 0 0 1px rgba(56,189,248,0.6);
            }}
            #btnFilter {{
                background: linear-gradient(135deg, #6366f1, #3b82f6);
                border-radius: 12px;
                border: none;
                font-size: 13px;
                font-weight: 600;
            }}

            .card-metric {{
                border-radius: 18px;
                padding: 18px 18px 14px;
                height: 100%;
                color: #e5e7eb;
                box-shadow: 0 16px 40px rgba(0,0,0,0.7);
                border: 1px solid rgba(15,23,42,0.8);
            }}
            .cm-blue {{
                background: linear-gradient(135deg, #1d4ed8, #2563eb);
            }}
            .cm-green {{
                background: linear-gradient(135deg, #15803d, #22c55e);
            }}
            .cm-yellow {{
                background: linear-gradient(135deg, #facc15, #f97316);
                color: #111827;
            }}
            .card-metric-title {{
                font-size: 12px;
                opacity: 0.9;
            }}
            .card-metric-value {{
                font-size: 24px;
                font-weight: 700;
                margin-top: 6px;
            }}
            .card-metric-sub {{
                font-size: 12px;
                margin-top: 6px;
                color: #e5e7eb;
            }}

            .card-box {{
                background: rgba(15,23,42,0.9);
                border-radius: 18px;
                padding: 14px 18px 14px;
                box-shadow: 0 16px 40px rgba(0,0,0,0.7);
                border: 1px solid rgba(30,64,175,0.6);
                height: 260px;
            }}
            .card-box .fw-semibold {{
                font-size: 13px;
                color: #cbd5f5;
            }}
            .card-box:hover {{
                border-color: #38bdf8;
            }}

            .chart-container {{
                position: relative;
                width: 100%;
                height: 190px;
                margin-top: 4px;
            }}

            small.text-muted {{
                color: #64748b !important;
            }}

            @media (max-width: 768px) {{
                .navbar-inner {{
                    padding: 10px 14px;
                }}
                .navbar-title {{
                    font-size: 18px;
                }}
                .content-wrapper {{
                    padding: 16px 12px 26px 12px;
                }}
                .card-box {{
                    height: 240px;
                }}
                .card-metric-value {{
                    font-size: 20px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="navbar-custom">
            <div class="navbar-inner">
                <div>
                    <div class="navbar-kicker">Preview Dashboard</div>
                    <div class="navbar-title">Monitoring Penjualan</div>
                </div>
                <div class="navbar-pill">Live &amp; Online</div>
            </div>
        </div>

        <div class="content-wrapper">

            <!-- FILTER PERIODE -->
            <div class="mb-3">
                <div class="row g-3 align-items-end filters-row">
                    <div class="col-auto">
                        <label class="form-label mb-0"><strong>Periode :</strong></label>
                    </div>
                    <div class="col-12 col-md-3">
                        <label class="form-label">Periode Mulai</label>
                        <input type="date" class="form-control" id="start_date" />
                    </div>
                    <div class="col-12 col-md-3">
                        <label class="form-label">Periode Selesai</label>
                        <input type="date" class="form-control" id="end_date" />
                    </div>
                    <div class="col-12 col-md-3">
                        <button id="btnFilter" class="btn btn-primary w-100">
                            Terapkan Filter
                        </button>
                    </div>
                </div>
                <small class="text-muted">
                    Filter ini akan menerapkan periode ke semua kartu dan grafik di bawah.
                </small>
            </div>

            <!-- UPLOAD BOX -->
            <div class="upload-box mb-4">
                <form action="/upload" method="post" enctype="multipart/form-data" class="row g-3 align-items-center">
                    <div class="col-12 col-md-6">
                        <label for="file" class="form-label">Upload Laporan Accurate (HTML)</label>
                        <input type="file" class="form-control" id="file" name="file" accept=".html,.htm" required />
                        <small class="text-muted">
                            Gunakan laporan Accurate (misal "Rincian Penjualan per Barang"), lalu simpan sebagai HTML dan upload di sini.
                        </small>
                        {status_html}
                    </div>
                    <div class="col-12 col-md-4">
                        <label class="form-label d-block">Opsi</label>
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" value="1" id="clear_before" name="clear_before" checked>
                            <label class="form-check-label" for="clear_before">
                                Hapus data lama sebelum import
                            </label>
                        </div>
                    </div>
                    <div class="col-12 col-md-2 text-md-end">
                        <label class="form-label d-none d-md-block">&nbsp;</label>
                        <button type="submit" class="btn btn-success w-100">
                            Upload &amp; Proses
                        </button>
                    </div>
                </form>
            </div>

            <!-- METRIC CARDS -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-4">
                    <div class="card-metric cm-blue">
                        <div class="card-metric-title">Total Penjualan (Semua Data Terfilter)</div>
                        <div class="card-metric-value" id="metricTotal">Rp 0</div>
                        <div class="card-metric-sub" id="metricTotalMoM">
                            Kenaikan/Penurunan vs Bulan Lalu: -
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="card-metric cm-green">
                        <div class="card-metric-title">Jumlah Customer</div>
                        <div class="card-metric-value" id="metricCustomer">0</div>
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="card-metric cm-yellow">
                        <div class="card-metric-title">Top Customer</div>
                        <div class="card-metric-value" id="metricTopCustomer">-</div>
                    </div>
                </div>
            </div>

            <!-- ROW 1 CHARTS -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Customer</div>
                        <div class="chart-container">
                            <canvas id="chartCustomer"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Komposisi Penjualan (Top 10 Customer)</div>
                        <div class="chart-container">
                            <canvas id="chartComposition"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ROW 2 CHARTS -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Customer Type</div>
                        <div class="chart-container">
                            <canvas id="chartCustomerType"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Kota</div>
                        <div class="chart-container">
                            <canvas id="chartCity"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ROW 3 CHARTS: SALESMAN + BARANG -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Salesman</div>
                        <div class="chart-container">
                            <canvas id="chartSalesman"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Barang</div>
                        <div class="chart-container">
                            <canvas id="chartItem"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ROW 3B: KATEGORI BARANG -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Kategori Barang</div>
                        <div class="chart-container">
                            <canvas id="chartCategory"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ROW 4: ANALISA MOM TOTAL + BARANG -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Analisa Kenaikan/Penurunan Total Penjualan (Bulan Ini vs Bulan Lalu)</div>
                        <div class="chart-container">
                            <canvas id="chartTotalMoM"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Penjualan per Barang (Top 10) - Bulan Ini vs Bulan Lalu</div>
                        <div class="chart-container">
                            <canvas id="chartItemMoM"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ROW 5: SALESMAN MOM -->
            <div class="row g-3 mb-3">
                <div class="col-12 col-md-6">
                    <div class="card-box">
                        <div class="fw-semibold mb-1">Penjualan per Salesman (Top 10) - Bulan Ini vs Bulan Lalu</div>
                        <div class="chart-container">
                            <canvas id="chartSalesmanMoM"></canvas>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            // ======= GLOBAL VAR UNTUK CHART =======
            let chartCustomer = null;
            let chartComposition = null;
            let chartCustomerType = null;
            let chartCity = null;
            let chartSalesman = null;
            let chartItem = null;
            let chartCategory = null;
            let chartTotalMoM = null;
            let chartItemMoM = null;
            let chartSalesmanMoM = null;

            const PALETTE = [
                "rgba(56,189,248,0.85)",
                "rgba(96,165,250,0.85)",
                "rgba(52,211,153,0.85)",
                "rgba(250,204,21,0.85)",
                "rgba(248,113,113,0.85)",
                "rgba(167,139,250,0.85)",
                "rgba(251,146,60,0.85)",
                "rgba(45,212,191,0.85)",
                "rgba(244,114,182,0.85)",
                "rgba(129,140,248,0.85)"
            ];

            function formatRupiah(value) {{
                if (!value) return "0";
                return Number(value).toLocaleString('id-ID');
            }}

            function updateMetricCards(data) {{
                const total = data.total_sales || 0;
                const custCount = data.customer_count || 0;
                const topCustomer = data.top_customer || "-";

                document.getElementById("metricTotal").innerText = "Rp " + formatRupiah(total);
                document.getElementById("metricCustomer").innerText = custCount;
                document.getElementById("metricTopCustomer").innerText = topCustomer;

                const cur = data.total_month_current;
                const prev = data.total_month_prev;
                const diff = data.total_month_diff;
                const pct = data.total_month_diff_pct;

                let text = "Belum ada data bulan ini / bulan lalu";
                if (cur !== null && prev !== null) {{
                    const sign = (diff || 0) >= 0 ? "+" : "";
                    const pctText = (pct === null || pct === undefined)
                        ? ""
                        : " (" + (pct * 100).toFixed(1) + "%)";
                    text = sign + "Rp " + formatRupiah(diff || 0) + pctText;
                }}
                document.getElementById("metricTotalMoM").innerText =
                    "Kenaikan/Penurunan vs Bulan Lalu: " + text;
            }}

            function buildLabelsAndValues(list) {{
                const labels = [];
                const values = [];
                for (const item of list || []) {{
                    labels.push(item.label);
                    values.push(item.amount);
                }}
                return {{ labels, values }};
            }}

            function baseChartOptions() {{
                return {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{
                            labels: {{
                                color: "#e5e7eb",
                                font: {{ size: 11 }}
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            ticks: {{ color: "#e5e7eb", maxRotation: 45, minRotation: 0 }},
                            grid: {{ color: "rgba(148,163,184,0.25)" }}
                        }},
                        y: {{
                            beginAtZero: true,
                            ticks: {{ color: "#e5e7eb" }},
                            grid: {{ color: "rgba(148,163,184,0.18)" }}
                        }}
                    }}
                }};
            }}

            function createOrUpdateBarChart(oldChart, canvasId, labels, values, title) {{
                const ctx = document.getElementById(canvasId).getContext("2d");
                if (oldChart) {{
                    oldChart.destroy();
                }}
                return new Chart(ctx, {{
                    type: "bar",
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: title,
                            data: values,
                            backgroundColor: "rgba(56,189,248,0.9)",
                            borderRadius: 6,
                        }}]
                    }},
                    options: baseChartOptions()
                }});
            }}

            function createOrUpdateBarChartMulti(oldChart, canvasId, labels, datasets) {{
                const ctx = document.getElementById(canvasId).getContext("2d");
                if (oldChart) {{
                    oldChart.destroy();
                }}
                const ds = datasets.map((d, idx) => ({{
                    ...d,
                    backgroundColor: idx === 0
                        ? "rgba(56,189,248,0.9)"
                        : "rgba(96,165,250,0.9)",
                    borderRadius: 5,
                }}));
                return new Chart(ctx, {{
                    type: "bar",
                    data: {{
                        labels: labels,
                        datasets: ds
                    }},
                    options: baseChartOptions()
                }});
            }}

            function createOrUpdatePieChart(oldChart, canvasId, labels, values, title) {{
                const ctx = document.getElementById(canvasId).getContext("2d");
                if (oldChart) {{
                    oldChart.destroy();
                }}
                const colors = labels.map((_, i) => PALETTE[i % PALETTE.length]);
                return new Chart(ctx, {{
                    type: "pie",
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: title,
                            data: values,
                            backgroundColor: colors,
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                labels: {{
                                    color: "#e5e7eb",
                                    font: {{ size: 11 }}
                                }}
                            }}
                        }}
                    }}
                }});
            }}

            async function loadDashboard() {{
                const start = document.getElementById("start_date").value;
                const end = document.getElementById("end_date").value;

                let url = "/dashboard-data";
                const params = [];
                if (start) params.push("start_date=" + encodeURIComponent(start));
                if (end) params.push("end_date=" + encodeURIComponent(end));
                if (params.length > 0) {{
                    url += "?" + params.join("&");
                }}

                const resp = await fetch(url);
                if (!resp.ok) {{
                    alert("Gagal mengambil data dashboard (mungkin belum ada data / kode akses belum benar).");
                    return;
                }}
                const data = await resp.json();

                // Update kartu
                updateMetricCards(data);

                // Top 10 Customer (bar)
                let lv = buildLabelsAndValues(data.top10_customer);
                chartCustomer = createOrUpdateBarChart(
                    chartCustomer,
                    "chartCustomer",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // Komposisi Top 10 Customer (pie)
                chartComposition = createOrUpdatePieChart(
                    chartComposition,
                    "chartComposition",
                    lv.labels,
                    lv.values,
                    "Komposisi Penjualan"
                );

                // Customer Type
                lv = buildLabelsAndValues(data.top10_customer_type);
                chartCustomerType = createOrUpdateBarChart(
                    chartCustomerType,
                    "chartCustomerType",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // Kota
                lv = buildLabelsAndValues(data.top10_city);
                chartCity = createOrUpdateBarChart(
                    chartCity,
                    "chartCity",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // Salesman
                lv = buildLabelsAndValues(data.top10_salesman);
                chartSalesman = createOrUpdateBarChart(
                    chartSalesman,
                    "chartSalesman",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // Barang (Top 10)
                lv = buildLabelsAndValues(data.top10_item);
                chartItem = createOrUpdateBarChart(
                    chartItem,
                    "chartItem",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // Kategori Barang (Top 10)
                lv = buildLabelsAndValues(data.top10_category);
                chartCategory = createOrUpdateBarChart(
                    chartCategory,
                    "chartCategory",
                    lv.labels,
                    lv.values,
                    "Total Penjualan"
                );

                // ===== Analisa Total Penjualan Bulan Ini vs Bulan Lalu =====
                const totalCurr = data.total_month_current || 0;
                const totalPrev = data.total_month_prev || 0;
                chartTotalMoM = createOrUpdateBarChart(
                    chartTotalMoM,
                    "chartTotalMoM",
                    ["Bulan Lalu", "Bulan Ini"],
                    [totalPrev, totalCurr],
                    "Total Penjualan"
                );

                // ===== Penjualan per Barang Bulan Ini vs Bulan Lalu (Top 10) =====
                const itemMoM = data.item_mom_top10 || [];
                const itemLabels = itemMoM.map(x => x.label);
                const itemCur = itemMoM.map(x => x.current || 0);
                const itemPrev = itemMoM.map(x => x.previous || 0);
                chartItemMoM = createOrUpdateBarChartMulti(
                    chartItemMoM,
                    "chartItemMoM",
                    itemLabels,
                    [
                        {{
                            label: "Bulan Ini",
                            data: itemCur,
                        }},
                        {{
                            label: "Bulan Lalu",
                            data: itemPrev,
                        }}
                    ]
                );

                // ===== Penjualan per Salesman Bulan Ini vs Bulan Lalu (Top 10) =====
                const smMoM = data.salesman_mom_top10 || [];
                const smLabels = smMoM.map(x => x.label);
                const smCur = smMoM.map(x => x.current || 0);
                const smPrev = smMoM.map(x => x.previous || 0);
                chartSalesmanMoM = createOrUpdateBarChartMulti(
                    chartSalesmanMoM,
                    "chartSalesmanMoM",
                    smLabels,
                    [
                        {{
                            label: "Bulan Ini",
                            data: smCur,
                        }},
                        {{
                            label: "Bulan Lalu",
                            data: smPrev,
                        }}
                    ]
                );
            }}

            document.addEventListener("DOMContentLoaded", function() {{
                // pertama kali load dashboard tanpa filter
                loadDashboard();

                // tombol filter
                document.getElementById("btnFilter").addEventListener("click", function() {{
                    loadDashboard();
                }});
            }});
        </script>
    </body>
    </html>
    """


# ================== EVENT STARTUP ==================


@app.on_event("startup")
def startup_event():
    """
    Otomatis jalan saat app start (lokal maupun Railway).
    - pastikan tabel ada
    - buat / update kode akses default
    """
    conn = get_connection()
    init_db(conn)

    # Kode demo – nanti bisa kamu ganti pola & masa berlakunya
    upsert_access_code(
        conn,
        code="DEMO-1234",
        customer_name="Demo Customer",
        active=1,
        valid_from=None,
        valid_to=None,
    )

    upsert_access_code(
        conn,
        code="ABC-2025",
        customer_name="Customer Contoh",
        active=1,
        valid_from=None,
        valid_to=None,
    )

    conn.close()


# ================== ROUTES UI ==================


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_authorized(request):
        return render_access_page()
    return render_dashboard()


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    clear_before: Optional[str] = Form("1"),
):
    if not is_authorized(request):
        return render_access_page("Silakan masukkan kode akses terlebih dahulu.", is_error=True)

    # Ambil kode akses dari cookie (pasti ada kalau sudah authorized)
    access_code = request.cookies.get(ACCESS_COOKIE_NAME)
    if not access_code:
        return render_access_page("Sesi akses tidak valid. Silakan login ulang.", is_error=True)

    try:
        contents = await file.read()
        html = contents.decode("utf-8", errors="ignore")

        records = parse_html_content(html)

        conn = get_connection()
        init_db(conn)

        # Hapus data lama hanya untuk access_code ini
        if clear_before == "1":
            clear_sales(conn, access_code)

        # Insert data baru untuk access_code ini
        insert_rows(conn, [r.to_tuple() for r in records], access_code)
        conn.close()

        msg = f"Berhasil import {len(records)} baris transaksi dari file: {file.filename}"
        return render_dashboard(status_message=msg, status_level="success")

    except Exception as e:
        print("UPLOAD ERROR:", e)
        msg = "Gagal upload: Internal Server Error"
        return render_dashboard(status_message=msg, status_level="error")


# ================== API UNTUK DASHBOARD ==================


@app.get("/sales")
def get_sales(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    if not is_authorized(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    access_code = request.cookies.get(ACCESS_COOKIE_NAME)
    if not access_code:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    conn = get_connection()
    data = fetch_sales(conn, access_code=access_code, start_date=start_date, end_date=end_date)
    conn.close()
    return JSONResponse(content=data)


@app.get("/dashboard-data")
def get_dashboard_data(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    if not is_authorized(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    access_code = request.cookies.get(ACCESS_COOKIE_NAME)
    if not access_code:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    conn = get_connection()
    rows = fetch_sales(conn, access_code=access_code, start_date=start_date, end_date=end_date)
    conn.close()

    data = build_dashboard_data(rows)
    return JSONResponse(content=data)