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
    upsert_access_code,      # <-- TAMBAHAN
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
        <title>Masukkan Kode Akses</title>
        <link
          href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
          rel="stylesheet"
        >
    </head>
    <body class="bg-light">
        <div class="container d-flex justify-content-center align-items-center" style="min-height:100vh;">
            <div class="card shadow-sm" style="max-width:420px; width:100%;">
                <div class="card-body">
                    <h4 class="card-title mb-3 text-center">Masukkan Kode Akses</h4>
                    <p class="text-muted small text-center">
                        Kode akses didapat dari vendor aplikasi dashboard Accurate.
                    </p>
                    <form action="/access" method="post">
                        <div class="mb-3">
                            <label for="code" class="form-label">Kode Akses</label>
                            <input type="text" class="form-control" id="code" name="code" placeholder="Contoh: ABC-2025" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100">Masuk</button>
                    </form>
                    {msg_html}
                </div>
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
    Ambil list transaksi → kembalikan data ringkasan untuk dashboard:
    - total penjualan
    - jumlah customer
    - top customer
    - berbagai Top 10
    - total bulan ini vs bulan lalu
    - Top 10 item & salesman bulan ini vs bulan lalu
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
        <title>Power BI Accurate - Monitoring Penjualan</title>
        <!-- Bootstrap 5 CDN -->
        <link
          href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
          rel="stylesheet"
        >
        <!-- Chart.js CDN -->
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

        <style>
            body {{
                background-color: #f5f7fb;
                font-family: Arial, sans-serif;
            }}
            .navbar-custom {{
                background-color: #0d6efd;
                color: white;
                padding: 12px 24px;
                font-size: 20px;
                font-weight: 600;
            }}
            .card-metric {{
                border-radius: 10px;
                color: white;
                padding: 16px;
                height: 100%;
            }}
            .card-metric-title {{
                font-size: 14px;
                opacity: 0.9;
            }}
            .card-metric-value {{
                font-size: 26px;
                font-weight: 700;
                margin-top: 8px;
            }}
            .card-metric-sub {{
                font-size: 13px;
                margin-top: 4px;
            }}
            .content-wrapper {{
                padding: 20px 30px 40px 30px;
            }}
            .card-box {{
                background-color: white;
                border-radius: 10px;
                padding: 14px 18px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                height: 260px;
            }}
            .upload-box {{
                background-color: white;
                border-radius: 10px;
                padding: 16px 18px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            }}
            .chart-container {{
                position: relative;
                width: 100%;
                height: 190px;
            }}
        </style>
    </head>
    <body>
        <div class="navbar-custom">
            Power BI Accurate - Monitoring Penjualan
        </div>

        <div class="content-wrapper">

            <!-- FILTER PERIODE -->
            <div class="mb-3">
                <div class="row g-3 align-items-end">
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
                    <div class="card-metric" style="background-color:#0d6efd;">
                        <div class="card-metric-title">Total Penjualan (Semua Data Terfilter)</div>
                        <div class="card-metric-value" id="metricTotal">Rp 0</div>
                        <div class="card-metric-sub" id="metricTotalMoM">
                            Kenaikan/Penurunan vs Bulan Lalu: -
                        </div>
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="card-metric" style="background-color:#198754;">
                        <div class="card-metric-title">Jumlah Customer</div>
                        <div class="card-metric-value" id="metricCustomer">0</div>
                    </div>
                </div>
                <div class="col-12 col-md-4">
                    <div class="card-metric" style="background-color:#ffc107; color:#333;">
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

            <!-- ROW 3 CHARTS -->
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
                        <div class="fw-semibold mb-1">Top 10 Penjualan per Barang / Kategori</div>
                        <div class="chart-container">
                            <canvas id="chartItemCategory"></canvas>
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
            let chartItemCategory = null;
            let chartTotalMoM = null;
            let chartItemMoM = null;
            let chartSalesmanMoM = null;

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
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            y: {{
                                beginAtZero: true
                            }}
                        }}
                    }}
                }});
            }}

            function createOrUpdateBarChartMulti(oldChart, canvasId, labels, datasets) {{
                const ctx = document.getElementById(canvasId).getContext("2d");
                if (oldChart) {{
                    oldChart.destroy();
                }}
                return new Chart(ctx, {{
                    type: "bar",
                    data: {{
                        labels: labels,
                        datasets: datasets
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {{
                            y: {{
                                beginAtZero: true
                            }}
                        }}
                    }}
                }});
            }}

            function createOrUpdatePieChart(oldChart, canvasId, labels, values, title) {{
                const ctx = document.getElementById(canvasId).getContext("2d");
                if (oldChart) {{
                    oldChart.destroy();
                }}
                return new Chart(ctx, {{
                    type: "pie",
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: title,
                            data: values,
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
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

                // Barang/Kategori (gabung)
                const topItem = data.top10_item || [];
                const topCat = data.top10_category || [];
                const combinedLabels = [];
                const combinedValues = [];
                for (const it of topItem) {{
                    combinedLabels.push(it.label);
                    combinedValues.push(it.amount);
                }}
                for (const ct of topCat) {{
                    combinedLabels.push(ct.label + " (Kategori)");
                    combinedValues.push(ct.amount);
                }}

                chartItemCategory = createOrUpdateBarChart(
                    chartItemCategory,
                    "chartItemCategory",
                    combinedLabels,
                    combinedValues,
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
    Fungsi ini otomatis dipanggil saat app start
    (baik di lokal maupun di Railway).
    Di sini kita:
    - pastikan tabel sudah ada (init_db)
    - buat / update kode akses default (DEMO-1234, ABC-2025)
    """
    conn = get_connection()
    init_db(conn)

    # Kode demo tanpa masa berlaku (bisa kamu ubah nanti)
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

    try:
        contents = await file.read()
        html = contents.decode("utf-8", errors="ignore")

        records = parse_html_content(html)

        conn = get_connection()
        init_db(conn)

        if clear_before == "1":
            clear_sales(conn)

        insert_rows(conn, [r.to_tuple() for r in records])
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

    conn = get_connection()
    data = fetch_sales(conn, start_date=start_date, end_date=end_date)
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

    conn = get_connection()
    rows = fetch_sales(conn, start_date=start_date, end_date=end_date)
    conn.close()

    data = build_dashboard_data(rows)
    return JSONResponse(content=data)