from datetime import date, timedelta

from database import get_connection, init_db, upsert_access_code


def main():
    conn = get_connection()
    init_db(conn)

    # Contoh: bikin 2 kode akses

    # 1) Kode DEMO, tanpa masa kadaluarsa (valid_from/to = None)
    upsert_access_code(
        conn,
        code="DEMO-1234",
        customer_name="Demo Customer",
        active=1,
        valid_from=None,
        valid_to=None,
    )

    # 2) Kode untuk PT Contoh Sukses, berlaku 1 tahun dari hari ini
    today = date.today()
    one_year = today + timedelta(days=365)
    upsert_access_code(
        conn,
        code="ABC-2025",
        customer_name="PT Contoh Sukses",
        active=1,
        valid_from=today.isoformat(),
        valid_to=one_year.isoformat(),
    )

    conn.close()
    print("Kode akses berhasil dibuat / diperbarui.")


if __name__ == "__main__":
    main()