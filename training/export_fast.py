"""
Kéo dữ liệu thật từ Pi về laptop cho bộ nhận diện nguồn bụi.

Chụp DB trên Pi bằng sqlite3 backup (an toàn khi dịch vụ đang ghi), chép về
training/data/pi_aiot.db rồi xuất CSV: fast_readings.csv, raw_readings.csv,
readings.csv, source_labels.csv (ts là giây epoch).

Chạy trên laptop: .venv/Scripts/python -m training.export_fast --host admin@172.29.23.41
"""
import argparse
import sqlite3
import subprocess
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
REMOTE_DB = "/home/admin/iot_data/aiot.db"
REMOTE_TMP = "/tmp/aiot_export.db"
TABLES = ("fast_readings", "raw_readings", "readings", "source_labels")

BACKUP_PY = (
    "import sqlite3;"
    f"s=sqlite3.connect('file:{REMOTE_DB}?mode=ro',uri=True);"
    f"d=sqlite3.connect('{REMOTE_TMP}');s.backup(d);d.close();s.close()"
)


def pull(host: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", host, f'python3 -c "{BACKUP_PY}"'], check=True)
    subprocess.run(["scp", "-q", f"{host}:{REMOTE_TMP}", str(out)], check=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"rm -f {REMOTE_TMP}"], check=False)
    return out


def export_csv(db: Path, out_dir: Path = DATA) -> dict:
    conn = sqlite3.connect(db)
    counts = {}
    for t in TABLES:
        try:
            df = pd.read_sql_query(f"SELECT * FROM {t} ORDER BY 1", conn)
        except Exception:
            continue                    # DB cũ chưa có bảng này
        df.to_csv(out_dir / f"{t}.csv", index=False)
        counts[t] = len(df)
    conn.close()
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--host", default="admin@pi.local")
    p.add_argument("--db", help="dùng file DB có sẵn thay vì kéo từ Pi")
    a = p.parse_args(argv)
    db = Path(a.db) if a.db else pull(a.host, DATA / "pi_aiot.db")
    for t, n in export_csv(db).items():
        print(f"{t}: {n} dòng -> {DATA / (t + '.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
