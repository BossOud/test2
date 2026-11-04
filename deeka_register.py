# -*- coding: utf-8 -*-
"""
โปรแกรมทะเบียนการจัดทำฎีกา (Python + Tkinter + SQLite)
- ไม่ต้องติดตั้งไลบรารีเพิ่ม (ใช้ของที่มากับ Python)
- ข้อมูลเก็บในไฟล์ deeka.db (ไฟล์เดียว) ตั้งค่าได้ใน config.ini
- วันที่แสดง/กรอกเป็น พ.ศ. รูปแบบ วว/ดด/ปปปป
"""
import configparser
import csv
import logging
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_DIR = Path(getattr(sys, "frozen", False) and sys.executable or __file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"

DEFAULT_CONFIG = """[files]
database = deeka.db
log = deeka.log
backup_dir = backup
export_dir = export
; สำรองอัตโนมัติทุกครั้งที่ปิดโปรแกรม (ถ้ามีการแก้ไขข้อมูล) เก็บไฟล์ล่าสุดไว้กี่ไฟล์
backup_keep = 30

[retry]
; ถ้าไฟล์ฐานข้อมูลถูกล็อก (เช่น เปิดซ้อนหลายโปรแกรม) ให้ลองใหม่กี่ครั้ง / รอกี่วินาที
times = 3
wait_seconds = 1

[lists]
; รายการให้เลือกในช่องแผนงาน / ประเภทรายจ่าย (คั่นด้วย , ) พิมพ์เพิ่มเองในโปรแกรมได้
plans = แผนงานบริหารทั่วไป,แผนงานการศึกษา,แผนงานสาธารณสุข,แผนงานเคหะและชุมชน,แผนงานสร้างความเข้มแข็งของชุมชน
expense_types = งบบุคลากร,งบดำเนินงาน,งบลงทุน,งบเงินอุดหนุน,งบรายจ่ายอื่น
"""


# ---------------------------------------------------------------- config / log
def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG, encoding="utf-8")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def setup_logging(cfg) -> None:
    logging.basicConfig(
        filename=BASE_DIR / cfg["files"]["log"],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- date / money
def parse_thai_date(text: str):
    """'04/11/2569' (พ.ศ.) หรือ '04/11/2026' (ค.ศ.) -> date ; ค่าว่าง -> None ; ผิด -> ValueError"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        d, m, y = (int(p) for p in text.replace("-", "/").split("/"))
        if y < 2400:          # ผู้ใช้กรอก ค.ศ. มา
            y += 543
        return date(y - 543, m, d)
    except (ValueError, TypeError):
        raise ValueError(f"วันที่ '{text}' ไม่ถูกต้อง (ใช้รูปแบบ วว/ดด/ปปปป เช่น 04/11/2569)")


def fmt_thai_date(iso: str) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"


def parse_amount(text: str) -> int:
    """'12,345.50' -> 1234550 (สตางค์) เก็บเป็นจำนวนเต็มเพื่อกันทศนิยมเพี้ยน"""
    try:
        v = Decimal((text or "").replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("จำนวนเงินต้องเป็นตัวเลข เช่น 12,345.50")
    if v <= 0:
        raise ValueError("จำนวนเงินต้องมากกว่า 0")
    if v.as_tuple().exponent < -2:
        raise ValueError("จำนวนเงินมีทศนิยมได้ไม่เกิน 2 ตำแหน่ง")
    return int(v * 100)


def fmt_amount(satang: int) -> str:
    return f"{satang / 100:,.2f}"


# ---------------------------------------------------------------- validation
def validate(form: dict) -> dict:
    """ตรวจฟอร์ม -> คืน dict พร้อมบันทึก หรือ raise ValueError(รายการข้อผิดพลาด)"""
    errors, out = [], {}

    def need(key, label):
        v = (form.get(key) or "").strip()
        if not v:
            errors.append(f"กรุณากรอก{label}")
        return v

    out["no"] = need("no", "ที่")
    out["subject"] = need("subject", "ฎีกาเรื่อง")
    out["plan"] = need("plan", "แผนงาน")
    out["expense_type"] = need("expense_type", "ประเภทรายจ่าย")
    out["note"] = (form.get("note") or "").strip()

    dates = {}
    for key, label, required in (
        ("created", "วันที่จัดทำ", True),
        ("handover", "วันที่ผู้ส่งมอบ", False),
        ("receive", "วันที่ผู้รับมอบ", False),
    ):
        try:
            d = parse_thai_date(form.get(key))
            if d is None and required:
                errors.append(f"กรุณากรอก{label}")
            dates[key] = d
        except ValueError as e:
            errors.append(f"{label}: {e}")
    try:
        out["amount"] = parse_amount(form.get("amount"))
    except ValueError as e:
        errors.append(str(e))

    c, h, r = dates.get("created"), dates.get("handover"), dates.get("receive")
    if c and h and h < c:
        errors.append("วันที่ส่งมอบต้องไม่ก่อนวันที่จัดทำ")
    if h and r and r < h:
        errors.append("วันที่รับมอบต้องไม่ก่อนวันที่ส่งมอบ")

    # ผู้ส่งมอบ / ผู้รับมอบ และวันที่ ไม่บังคับกรอก (เว้นว่างได้ทั้งหมด)
    out["sender"] = (form.get("sender") or "").strip()
    out["receiver"] = (form.get("receiver") or "").strip()

    if errors:
        raise ValueError("\n".join(errors))
    for key in dates:
        out[key] = dates[key].isoformat() if dates[key] else ""
    return out


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS deeka (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    no            TEXT NOT NULL UNIQUE,     -- ที่
    created       TEXT NOT NULL,            -- วันที่จัดทำ (ISO ค.ศ.)
    subject       TEXT NOT NULL,            -- ฎีกาเรื่อง
    plan          TEXT NOT NULL,            -- แผนงาน
    expense_type  TEXT NOT NULL,            -- ประเภทรายจ่าย
    amount        INTEGER NOT NULL,         -- จำนวนเงิน (สตางค์)
    sender        TEXT DEFAULT '',          -- ผู้ส่งมอบ
    handover      TEXT DEFAULT '',          -- วันที่ส่งมอบ
    receiver      TEXT DEFAULT '',          -- ผู้รับมอบ
    receive       TEXT DEFAULT '',          -- วันที่รับมอบ
    note          TEXT DEFAULT '',          -- หมายเหตุ
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
COLS = ("no", "created", "subject", "plan", "expense_type", "amount",
        "sender", "handover", "receiver", "receive", "note")


class Database:
    def __init__(self, path: Path, retry_times=3, retry_wait=1.0):
        self.retry_times, self.retry_wait = retry_times, retry_wait
        self.path = path
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self._run(lambda: (self.conn.executescript(SCHEMA), self.conn.commit()))

    def _run(self, fn):
        """รันคำสั่ง และลองใหม่เมื่อไฟล์ถูกล็อก"""
        for attempt in range(1, self.retry_times + 1):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                logging.warning("DB error (ครั้งที่ %s/%s): %s", attempt, self.retry_times, e)
                if attempt == self.retry_times:
                    raise
                time.sleep(self.retry_wait)

    def next_no(self) -> str:
        nums = [int(r["no"]) for r in self.conn.execute("SELECT no FROM deeka") if r["no"].isdigit()]
        return str(max(nums, default=0) + 1)

    def add(self, rec: dict) -> int:
        def go():
            cur = self.conn.execute(
                f"INSERT INTO deeka ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                [rec[c] for c in COLS])
            self.conn.commit()
            return cur.lastrowid
        try:
            rid = self._run(go)
        except sqlite3.IntegrityError:
            raise ValueError(f"เลขที่ '{rec['no']}' มีอยู่แล้ว")
        logging.info("เพิ่ม ที่=%s เรื่อง=%s", rec["no"], rec["subject"])
        return rid

    def update(self, rid: int, rec: dict) -> None:
        def go():
            sets = ",".join(f"{c}=?" for c in COLS)
            self.conn.execute(f"UPDATE deeka SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                              [rec[c] for c in COLS] + [rid])
            self.conn.commit()
        try:
            self._run(go)
        except sqlite3.IntegrityError:
            raise ValueError(f"เลขที่ '{rec['no']}' มีอยู่แล้ว")
        logging.info("แก้ไข id=%s ที่=%s", rid, rec["no"])

    def delete(self, rid: int) -> None:
        self._run(lambda: (self.conn.execute("DELETE FROM deeka WHERE id=?", (rid,)), self.conn.commit()))
        logging.info("ลบ id=%s", rid)

    def get(self, rid: int):
        return self.conn.execute("SELECT * FROM deeka WHERE id=?", (rid,)).fetchone()

    def search(self, keyword: str = ""):
        k = f"%{keyword.strip()}%"
        return self.conn.execute(
            "SELECT * FROM deeka WHERE no LIKE ? OR subject LIKE ? OR plan LIKE ? "
            "OR expense_type LIKE ? OR note LIKE ? OR sender LIKE ? OR receiver LIKE ? "
            "ORDER BY CAST(no AS INTEGER), no", (k,) * 7).fetchall()

    def backup(self, folder: Path, keep: int = 0) -> Path:
        """สำรองฐานข้อมูล; keep>0 = เก็บไฟล์สำรองล่าสุดไว้กี่ไฟล์ (ที่เก่ากว่าลบทิ้ง)"""
        folder.mkdir(exist_ok=True)
        dest = folder / f"deeka_{datetime.now():%Y%m%d_%H%M%S}.db"
        self.conn.commit()
        shutil.copy2(self.path, dest)
        logging.info("สำรองข้อมูล -> %s", dest)
        if keep > 0:
            for old in sorted(folder.glob("deeka_*.db"))[:-keep]:
                try:
                    old.unlink()
                    logging.info("ลบไฟล์สำรองเก่า %s", old.name)
                except OSError:
                    logging.warning("ลบไฟล์สำรองเก่าไม่ได้: %s", old)
        return dest



# ---------------------------------------------------------------- สถานะ / ตัวกรอง
ST_WAIT, ST_SENT, ST_DONE = "รอส่งมอบ", "ส่งมอบแล้ว", "รับมอบแล้ว"
STATUSES = [ST_WAIT, ST_SENT, ST_DONE]
ALL = "ทั้งหมด"
THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]


def status_of(r) -> str:
    """สถานะคำนวณจากวันที่: มีวันที่รับมอบ = รับมอบแล้ว, มีวันที่ส่งมอบ = ส่งมอบแล้ว, ไม่มีเลย = รอส่งมอบ"""
    if r["receive"]:
        return ST_DONE
    if r["handover"]:
        return ST_SENT
    return ST_WAIT


def fiscal_year(iso: str) -> int:
    """ปีงบประมาณ (พ.ศ.) เริ่ม 1 ต.ค. เช่น 15/10/2568 -> 2569"""
    d = date.fromisoformat(iso)
    return d.year + 543 + (1 if d.month >= 10 else 0)


def parse_filters(raw: dict) -> dict:
    """แปลงค่าจากช่องตัวกรอง (ข้อความ) -> dict พร้อมใช้ ; ค่า 'ทั้งหมด'/ว่าง = ไม่กรอง"""
    f = {k: ("" if raw.get(k, "") in (ALL, None) else str(raw.get(k, "")).strip())
         for k in ("fy", "plan", "expense_type", "status")}
    try:
        d1, d2 = parse_thai_date(raw.get("date_from")), parse_thai_date(raw.get("date_to"))
    except ValueError as e:
        raise ValueError(f"ตัวกรองวันที่: {e}")
    if d1 and d2 and d2 < d1:
        raise ValueError("ตัวกรองวันที่: 'ถึงวันที่' ต้องไม่ก่อน 'ตั้งแต่วันที่'")
    f["date_from"] = d1.isoformat() if d1 else ""
    f["date_to"] = d2.isoformat() if d2 else ""
    if f["fy"] and not f["fy"].isdigit():
        raise ValueError("ปีงบประมาณต้องเป็นตัวเลข เช่น 2569")
    return f


def filter_rows(rows, f: dict):
    out = []
    for r in rows:
        if f.get("date_from") and r["created"] < f["date_from"]:
            continue
        if f.get("date_to") and r["created"] > f["date_to"]:
            continue
        if f.get("fy") and fiscal_year(r["created"]) != int(f["fy"]):
            continue
        if f.get("plan") and r["plan"] != f["plan"]:
            continue
        if f.get("expense_type") and r["expense_type"] != f["expense_type"]:
            continue
        if f.get("status") and status_of(r) != f["status"]:
            continue
        out.append(r)
    return out


def describe_filters(f: dict, keyword: str = "") -> str:
    parts = []
    if keyword.strip():
        parts.append(f"คำค้น \"{keyword.strip()}\"")
    if f.get("fy"):
        parts.append(f"ปีงบประมาณ {f['fy']}")
    if f.get("date_from") or f.get("date_to"):
        parts.append(f"วันที่จัดทำ {fmt_thai_date(f.get('date_from')) or '...'} ถึง "
                     f"{fmt_thai_date(f.get('date_to')) or '...'}")
    for key, label in (("plan", "แผนงาน"), ("expense_type", "ประเภท"), ("status", "สถานะ")):
        if f.get(key):
            parts.append(f"{label}: {f[key]}")
    return " | ".join(parts) if parts else "ทั้งหมด (ไม่มีตัวกรอง)"


def export_csv(rows, dest: Path) -> int:
    """ส่งออกเป็น CSV (UTF-8 มี BOM เพื่อให้ Excel อ่านภาษาไทยได้)"""
    head = ["ที่", "วันที่จัดทำ", "ฎีกาเรื่อง", "แผนงาน", "ประเภทรายจ่าย", "จำนวนเงิน",
            "ผู้ส่งมอบ", "วันที่ส่งมอบ", "ผู้รับมอบ", "วันที่รับมอบ", "สถานะ", "หมายเหตุ"]
    with open(dest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in rows:
            w.writerow([r["no"], fmt_thai_date(r["created"]), r["subject"], r["plan"],
                        r["expense_type"], f"{r['amount'] / 100:.2f}", r["sender"],
                        fmt_thai_date(r["handover"]), r["receiver"], fmt_thai_date(r["receive"]),
                        status_of(r), r["note"]])
    logging.info("ส่งออก %s รายการ -> %s", len(rows), dest)
    return len(rows)


# ---------------------------------------------------------------- รายงาน (HTML สำหรับพิมพ์)
_CSS = """
body { font-family: "TH Sarabun New","Sarabun","Tahoma",sans-serif; font-size: 13pt; }
h1 { text-align:center; font-size: 18pt; margin: 0 0 4px; }
h2 { font-size: 14pt; margin: 14px 0 4px; }
.meta { text-align:center; margin-bottom: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #000; padding: 3px 5px; font-size: 11pt; vertical-align: top; }
th { background: #eee; } thead { display: table-header-group; } tr { page-break-inside: avoid; }
.c { text-align:center; } .r { text-align:right; white-space: nowrap; }
tfoot td { font-weight: bold; }
"""


def _page(title, subtitle, rows_count, inner, landscape=True):
    from html import escape as e
    size = "A4 landscape" if landscape else "A4 portrait"
    return f"""<!doctype html><html lang="th"><head><meta charset="utf-8">
<title>{e(title)}</title><style>@page {{ size: {size}; margin: 10mm; }}{_CSS}</style>
</head><body onload="window.print()">
<h1>{e(title)}</h1>
<div class="meta">ตัวกรอง: {e(subtitle)}<br>พิมพ์เมื่อ {fmt_thai_date(date.today().isoformat())}
 &nbsp;|&nbsp; จำนวน {rows_count} รายการ</div>
{inner}</body></html>"""


def build_print_html(rows, title="ทะเบียนการจัดทำฎีกา", subtitle="ทั้งหมด (ไม่มีตัวกรอง)") -> str:
    """ทะเบียน: A4 แนวนอน"""
    from html import escape as e
    total = sum(r["amount"] for r in rows)
    body = "".join(
        "<tr>" + "".join(f"<td{c}>{e(str(v))}</td>" for v, c in (
            (r["no"], ' class="c"'), (fmt_thai_date(r["created"]), ' class="c"'),
            (r["subject"], ""), (r["plan"], ""), (r["expense_type"], ""),
            (fmt_amount(r["amount"]), ' class="r"'),
            (r["sender"], ""), (fmt_thai_date(r["handover"]), ' class="c"'),
            (r["receiver"], ""), (fmt_thai_date(r["receive"]), ' class="c"'),
            (status_of(r), ' class="c"'), (r["note"], ""))) + "</tr>" for r in rows)
    inner = f"""<table><thead><tr><th>ที่</th><th>วันที่จัดทำ</th><th>ฎีกาเรื่อง</th><th>แผนงาน</th>
<th>ประเภทรายจ่าย</th><th>จำนวนเงิน</th><th>ผู้ส่งมอบ</th><th>วันที่ส่งมอบ</th>
<th>ผู้รับมอบ</th><th>วันที่รับมอบ</th><th>สถานะ</th><th>หมายเหตุ</th></tr></thead>
<tbody>{body}</tbody>
<tfoot><tr><td colspan="5" class="r">รวมทั้งสิ้น</td><td class="r">{fmt_amount(total)}</td><td colspan="6"></td></tr></tfoot>
</table>"""
    return _page(title, subtitle, len(rows), inner, landscape=True)


def group_totals(rows, keyfn):
    """รวมจำนวนรายการ/ยอดเงิน ตามกลุ่ม -> [(กลุ่ม, จำนวน, ยอดเงิน)] เรียงตามกลุ่ม"""
    acc = {}
    for r in rows:
        k = keyfn(r)
        c, t = acc.get(k, (0, 0))
        acc[k] = (c + 1, t + r["amount"])
    return [(k, c, t) for k, (c, t) in sorted(acc.items())]


def summary_sections(rows):
    """ส่วนสรุปทั้งหมด -> [(หัวข้อ, [(ชื่อกลุ่ม, จำนวน, ยอดเงิน)])]"""
    order = {s: i for i, s in enumerate(STATUSES)}
    by_status = [(STATUSES[k], c, t) for k, c, t in group_totals(rows, lambda r: order[status_of(r)])]

    def month_label(ym):
        y, m = ym.split("-")
        return f"{THAI_MONTHS[int(m)]} {int(y) + 543}"

    return [
        ("สรุปตามสถานะ", by_status),
        ("สรุปตามแผนงาน", group_totals(rows, lambda r: r["plan"])),
        ("สรุปตามประเภทรายจ่าย", group_totals(rows, lambda r: r["expense_type"])),
        ("สรุปตามเดือนที่จัดทำ", [(month_label(k), c, t)
                                   for k, c, t in group_totals(rows, lambda r: r["created"][:7])]),
    ]


def build_summary_html(rows, subtitle="ทั้งหมด (ไม่มีตัวกรอง)") -> str:
    """รายงานสรุปยอด: A4 แนวตั้ง"""
    from html import escape as e
    parts = []
    for title, data in summary_sections(rows):
        body = "".join(f'<tr><td>{e(str(k))}</td><td class="c">{c}</td><td class="r">{fmt_amount(t)}</td></tr>'
                       for k, c, t in data)
        parts.append(f"<h2>{e(title)}</h2><table><thead><tr><th>รายการ</th><th>จำนวนฎีกา</th>"
                     f"<th>จำนวนเงิน (บาท)</th></tr></thead><tbody>{body}</tbody></table>")
    total = sum(r["amount"] for r in rows)
    parts.append(f'<h2>รวมทั้งสิ้น</h2><table><tr><td>ฎีกาทั้งหมด {len(rows)} ฉบับ</td>'
                 f'<td class="r">{fmt_amount(total)} บาท</td></tr></table>')
    return _page("รายงานสรุปทะเบียนการจัดทำฎีกา", subtitle, len(rows), "".join(parts), landscape=False)


# ---------------------------------------------------------------- GUI
def run_gui():
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()
    setup_logging(cfg)
    db = Database(BASE_DIR / cfg["files"]["database"],
                  cfg.getint("retry", "times"), cfg.getfloat("retry", "wait_seconds"))
    plans = [s.strip() for s in cfg["lists"]["plans"].split(",") if s.strip()]
    types = [s.strip() for s in cfg["lists"]["expense_types"].split(",") if s.strip()]
    backup_dir = BASE_DIR / cfg["files"]["backup_dir"]
    backup_keep = cfg.getint("files", "backup_keep", fallback=30)
    dirty = {"v": False}   # มีการแก้ไขข้อมูลตั้งแต่สำรองครั้งล่าสุดหรือไม่

    root = tk.Tk()
    root.title("ทะเบียนการจัดทำฎีกา")
    root.geometry("1280x760")

    # ----- ฟอร์ม
    form = ttk.LabelFrame(root, text=" ข้อมูลฎีกา ", padding=10)
    form.pack(fill="x", padx=10, pady=(10, 5))
    vars_ = {k: tk.StringVar() for k in
             ("no", "created", "subject", "plan", "expense_type", "amount",
              "sender", "handover", "receiver", "receive", "note")}

    def field(label, key, row, col, width=22, values=None):
        ttk.Label(form, text=label).grid(row=row, column=col * 2, sticky="e", padx=(8, 4), pady=4)
        w = (ttk.Combobox(form, textvariable=vars_[key], values=values, width=width - 2)
             if values is not None else ttk.Entry(form, textvariable=vars_[key], width=width))
        w.grid(row=row, column=col * 2 + 1, sticky="w", pady=4)

    field("ที่ *", "no", 0, 0, 10)
    field("วันที่จัดทำ * (วว/ดด/ปปปป)", "created", 0, 1, 14)
    field("ฎีกาเรื่อง *", "subject", 0, 2, 40)
    field("แผนงาน *", "plan", 1, 0, 24, plans)
    field("ประเภทรายจ่าย *", "expense_type", 1, 1, 24, types)
    field("จำนวนเงิน (บาท) *", "amount", 1, 2, 18)
    field("ผู้ส่งมอบ", "sender", 2, 0, 24)
    field("วันที่ส่งมอบ", "handover", 2, 1, 14)
    field("ผู้รับมอบ", "receiver", 2, 2, 24)
    field("วันที่รับมอบ", "receive", 3, 0, 14)
    field("หมายเหตุ", "note", 3, 1, 40)

    # ----- ตัวกรอง
    flt = {k: tk.StringVar(value=ALL if k in ("fy", "plan", "expense_type", "status") else "")
           for k in ("fy", "plan", "expense_type", "status", "date_from", "date_to")}
    ff = ttk.LabelFrame(root, text=" ตัวกรอง ", padding=6)
    ff.pack(fill="x", padx=10, pady=(0, 5))
    combos = {}

    def fcombo(label, key, width):
        ttk.Label(ff, text=label).pack(side="left", padx=(8, 3))
        c = ttk.Combobox(ff, textvariable=flt[key], width=width, state="readonly")
        c.pack(side="left")
        c.bind("<<ComboboxSelected>>", lambda _: reload())
        combos[key] = c

    def fentry(label, key):
        ttk.Label(ff, text=label).pack(side="left", padx=(8, 3))
        en = ttk.Entry(ff, textvariable=flt[key], width=11)
        en.pack(side="left")
        en.bind("<Return>", lambda _: reload())

    fcombo("ปีงบประมาณ", "fy", 8)
    fentry("วันที่จัดทำ ตั้งแต่", "date_from")
    fentry("ถึง", "date_to")
    fcombo("แผนงาน", "plan", 18)
    fcombo("ประเภท", "expense_type", 14)
    fcombo("สถานะ", "status", 11)

    def clear_filters():
        for k, v in flt.items():
            v.set(ALL if k in ("fy", "plan", "expense_type", "status") else "")
        reload()

    ttk.Button(ff, text="กรอง", command=lambda: reload()).pack(side="left", padx=(10, 2))
    ttk.Button(ff, text="ล้างตัวกรอง", command=clear_filters).pack(side="left", padx=2)

    def refresh_filter_lists():
        rows = db.search("")
        combos["fy"]["values"] = [ALL] + [str(y) for y in sorted({fiscal_year(r["created"]) for r in rows}, reverse=True)]
        combos["plan"]["values"] = [ALL] + sorted(set(plans) | {r["plan"] for r in rows})
        combos["expense_type"]["values"] = [ALL] + sorted(set(types) | {r["expense_type"] for r in rows})
        combos["status"]["values"] = [ALL] + STATUSES

    state = {"id": None}   # id ของรายการที่กำลังแก้ไข (None = เพิ่มใหม่)
    status = tk.StringVar()
    search_var = tk.StringVar()

    # ----- ตาราง
    heads = [("no", "ที่", 50), ("created", "วันที่จัดทำ", 90), ("subject", "ฎีกาเรื่อง", 240),
             ("plan", "แผนงาน", 140), ("expense_type", "ประเภทรายจ่าย", 105),
             ("amount", "จำนวนเงิน", 95), ("sender", "ผู้ส่งมอบ", 85),
             ("handover", "ส่งมอบ", 80), ("receiver", "ผู้รับมอบ", 85),
             ("receive", "รับมอบ", 80), ("status", "สถานะ", 85), ("note", "หมายเหตุ", 130)]
    tree = ttk.Treeview(root, columns=[h[0] for h in heads], show="headings", selectmode="browse")
    for key, text, w in heads:
        tree.heading(key, text=text)
        tree.column(key, width=w, anchor="e" if key == "amount" else "w")
    tag_of = {ST_WAIT: "wait", ST_SENT: "sent", ST_DONE: "done"}
    tree.tag_configure("wait", background="#ffe9c2")   # ส้มอ่อน = ค้าง
    tree.tag_configure("sent", background="#dbe9ff")   # ฟ้าอ่อน = ส่งแล้วรอรับ
    tree.tag_configure("done", background="#dff3df")   # เขียวอ่อน = เสร็จ
    sb = ttk.Scrollbar(root, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)

    def current_rows():
        """รายการตามคำค้น + ตัวกรอง ; raise ValueError ถ้าตัวกรองผิดรูปแบบ"""
        f = parse_filters({k: v.get() for k, v in flt.items()})
        return filter_rows(db.search(search_var.get()), f), f

    def reload(_=None):
        try:
            rows, _f = current_rows()
        except ValueError as e:
            messagebox.showwarning("ตัวกรองไม่ถูกต้อง", str(e))
            return []
        refresh_filter_lists()
        tree.delete(*tree.get_children())
        counts, total = {s: 0 for s in STATUSES}, 0
        for r in rows:
            st = status_of(r)
            counts[st] += 1
            total += r["amount"]
            tree.insert("", "end", iid=str(r["id"]), tags=(tag_of[st],), values=(
                r["no"], fmt_thai_date(r["created"]), r["subject"], r["plan"], r["expense_type"],
                fmt_amount(r["amount"]), r["sender"], fmt_thai_date(r["handover"]),
                r["receiver"], fmt_thai_date(r["receive"]), st, r["note"]))
        status.set(f"จำนวน {len(rows)} รายการ   รวมเงิน {fmt_amount(total)} บาท   |   "
                   + "   ".join(f"{s} {counts[s]}" for s in STATUSES))
        return rows

    def clear():
        state["id"] = None
        for v in vars_.values():
            v.set("")
        vars_["no"].set(db.next_no())
        vars_["created"].set(fmt_thai_date(date.today().isoformat()))
        tree.selection_remove(tree.selection())

    def on_select(_=None):
        sel = tree.selection()
        if not sel:
            return
        r = db.get(int(sel[0]))
        state["id"] = r["id"]
        for k in vars_:
            val = r[k]
            if k in ("created", "handover", "receive"):
                val = fmt_thai_date(val)
            elif k == "amount":
                val = f"{val / 100:.2f}"
            vars_[k].set(val)

    def save():
        try:
            rec = validate({k: v.get() for k, v in vars_.items()})
            if state["id"] is None:
                db.add(rec)
                msg = "เพิ่มรายการแล้ว"
            else:
                db.update(state["id"], rec)
                msg = "แก้ไขรายการแล้ว"
        except ValueError as e:
            messagebox.showwarning("ข้อมูลไม่ถูกต้อง", str(e))
            return
        except sqlite3.Error as e:
            logging.exception("บันทึกไม่สำเร็จ")
            messagebox.showerror("บันทึกไม่สำเร็จ", f"ฐานข้อมูลมีปัญหา: {e}\nดูรายละเอียดในไฟล์ log")
            return
        dirty["v"] = True
        reload()
        clear()
        status.set(status.get() + f"   | {msg}")

    def delete():
        if state["id"] is None:
            messagebox.showinfo("ลบ", "กรุณาเลือกรายการในตารางก่อน")
            return
        if messagebox.askyesno("ยืนยัน", "ลบรายการนี้ถาวรหรือไม่?"):
            db.delete(state["id"])
            dirty["v"] = True
            reload()
            clear()

    def do_export():
        try:
            rows, _f = current_rows()
        except ValueError as e:
            messagebox.showwarning("ตัวกรองไม่ถูกต้อง", str(e))
            return
        folder = BASE_DIR / cfg["files"]["export_dir"]
        folder.mkdir(exist_ok=True)
        dest = filedialog.asksaveasfilename(
            initialdir=folder, initialfile=f"deeka_{datetime.now():%Y%m%d}.csv",
            defaultextension=".csv", filetypes=[("CSV (เปิดด้วย Excel)", "*.csv")])
        if dest:
            n = export_csv(rows, Path(dest))
            messagebox.showinfo("ส่งออก", f"ส่งออก {n} รายการ (ตามตัวกรองที่เลือก) เรียบร้อย\n{dest}")

    def open_report(builder, filename):
        try:
            rows, f = current_rows()
        except ValueError as e:
            messagebox.showwarning("ตัวกรองไม่ถูกต้อง", str(e))
            return
        if not rows:
            messagebox.showinfo("พิมพ์", "ไม่มีรายการให้พิมพ์")
            return
        folder = BASE_DIR / cfg["files"]["export_dir"]
        folder.mkdir(exist_ok=True)
        dest = folder / filename
        dest.write_text(builder(rows, describe_filters(f, search_var.get())), encoding="utf-8")
        logging.info("สร้างรายงาน %s (%s รายการ)", filename, len(rows))
        webbrowser.open(dest.as_uri())   # เปิดเบราว์เซอร์ + หน้าต่างพิมพ์ขึ้นเอง

    def do_backup():
        dest = db.backup(backup_dir, backup_keep)
        dirty["v"] = False
        messagebox.showinfo("สำรองข้อมูล", f"สำรองข้อมูลแล้ว\n{dest}")

    def on_close():
        """สำรองอัตโนมัติตอนปิดโปรแกรม (เฉพาะเมื่อมีการแก้ไขข้อมูล)"""
        if dirty["v"]:
            try:
                db.backup(backup_dir, backup_keep)
            except Exception:
                logging.exception("สำรองอัตโนมัติไม่สำเร็จ")
        try:
            db.conn.close()
        finally:
            root.destroy()

    # ----- ปุ่ม + ค้นหา
    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=10)
    ttk.Button(bar, text="บันทึก", command=save).pack(side="left", padx=2)
    ttk.Button(bar, text="ล้างฟอร์ม / เพิ่มใหม่", command=clear).pack(side="left", padx=2)
    ttk.Button(bar, text="ลบรายการ", command=delete).pack(side="left", padx=2)
    ttk.Button(bar, text="พิมพ์ทะเบียน",
               command=lambda: open_report(build_print_html, "print_deeka.html")).pack(side="left", padx=(20, 2))
    ttk.Button(bar, text="รายงานสรุป",
               command=lambda: open_report(build_summary_html, "summary_deeka.html")).pack(side="left", padx=2)
    ttk.Button(bar, text="ส่งออก CSV", command=do_export).pack(side="left", padx=2)
    ttk.Button(bar, text="สำรองข้อมูล", command=do_backup).pack(side="left", padx=2)
    ttk.Button(bar, text="ค้นหา", command=lambda: reload()).pack(side="right", padx=2)
    e = ttk.Entry(bar, textvariable=search_var, width=28)
    e.pack(side="right")
    e.bind("<Return>", lambda _: reload())
    ttk.Label(bar, text="ค้นหา:").pack(side="right", padx=4)

    ttk.Label(root, textvariable=status, anchor="w").pack(side="bottom", fill="x", padx=10, pady=4)
    tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
    sb.pack(side="left", fill="y", pady=8, padx=(0, 10))
    tree.bind("<<TreeviewSelect>>", on_select)
    root.protocol("WM_DELETE_WINDOW", on_close)

    reload()
    clear()
    root.mainloop()


if __name__ == "__main__":
    run_gui()