import csv
import io
from datetime import date as date_cls

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..config import SITES
from ..database import get_db
from ..security import CurrentAdmin
from ..services import employee_public, month_stats, site_filter, working_days_elapsed
from ..utils import parse_date, today_str

router = APIRouter(prefix="/api/reports", tags=["reports"])


async def _muster(month: str, site: str | None) -> dict:
    db = get_db()
    emps = [e async for e in db.employees.find({"is_active": True} | site_filter(site)).sort("emp_code", 1)]
    stats = await month_stats([e["_id"] for e in emps], month)
    today = {r["employee_id"]: r["status"] async for r in db.attendance.find({"date": today_str()})}
    elapsed = working_days_elapsed(month)
    rows = []
    for e in emps:
        s = stats.get(e["_id"], {})
        dw = s.get("days_worked", 0)
        rows.append(employee_public(e) | {
            "days_worked": dw,
            "present": s.get("present", 0),
            "half_day": s.get("half_day", 0),
            "absent": s.get("absent", 0),
            "late": s.get("late", 0),
            "ot_hours": s.get("ot_hours", 0),
            "gross_wages": round(dw * e.get("daily_rate", 0)),
            "today_status": today.get(e["_id"]),
        })
    totals = {
        "employees": len(rows),
        "days_worked": sum(r["days_worked"] for r in rows),
        "ot_hours": sum(r["ot_hours"] for r in rows),
        "gross_wages": sum(r["gross_wages"] for r in rows),
    }
    return {"month": month, "elapsed_days": elapsed, "rows": rows, "totals": totals}


@router.get("/muster")
async def muster(_: CurrentAdmin, month: str | None = None, site: str | None = None):
    return await _muster(month or today_str()[:7], site)


@router.get("/muster.csv")
async def muster_csv(_: CurrentAdmin, month: str | None = None, site: str | None = None):
    data = await _muster(month or today_str()[:7], site)
    buf = io.StringIO()
    buf.write("﻿")  # Excel-friendly UTF-8 BOM
    w = csv.writer(buf)
    w.writerow(["Employee ID", "PP Number", "Name", "Trade", "Site", "Present", "Half-Day", "Absent",
                "Days Worked", f"Of {data['elapsed_days']} Days", "OT Hours", "Late", "Daily Rate",
                "Gross Wages", "UAN", "ESIC", "PF/ESI"])
    for r in data["rows"]:
        w.writerow([r["emp_code"], r["pp_number"], r["name"], r["trade"], r["site"], r["present"], r["half_day"],
                    r["absent"], r["days_worked"], data["elapsed_days"], r["ot_hours"], r["late"], r["daily_rate"],
                    r["gross_wages"], r["uan"] or "", r["esic"] or "", r["pf_status"]])
    fname = f"PERFECT_ENGINERING_Muster_Roll_{data['month']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/daily")
async def daily_report(admin: CurrentAdmin, date: str | None = None):
    db = get_db()
    day = parse_date(date).isoformat() if date else today_str()
    emps = [e async for e in db.employees.find({"is_active": True}).sort("emp_code", 1)]
    recs = {r["employee_id"]: r async for r in db.attendance.find({"date": day})}

    def counts(lst):
        st = [recs[e["_id"]]["status"] if e["_id"] in recs else None for e in lst]
        return st.count("P"), st.count("HD"), st.count("A"), st.count(None)

    p, hd, a, um = counts(emps)
    pretty = date_cls.fromisoformat(day).strftime("%A, %B %d, %Y")
    lines = [
        "=" * 56,
        "PERFECT ENGINERING — DAILY SITE ATTENDANCE REPORT",
        f"Date: {pretty} | Shift A (08:00 – 17:00)",
        f"Approved By: {admin['name']} — Administrator",
        "=" * 56,
        "",
        "1. OVERALL MANPOWER SUMMARY",
        f"   • Total Active Workforce : {len(emps)}",
        f"   • Present (P)            : {p}",
        f"   • Half-Day (HD)          : {hd}",
        f"   • Absent (A)             : {a}",
        f"   • Not Marked             : {um}",
        "",
        "2. SITE-WISE BREAKDOWN",
    ]
    for i, s in enumerate(SITES):
        sp, shd, sa, sum_ = counts([e for e in emps if e["site"] == s])
        total = sum(1 for e in emps if e["site"] == s)
        lines += [f"   [{chr(65 + i)}] {s}",
                  f"       Present: {sp} | Half-Day: {shd} | Absent: {sa} | Not Marked: {sum_} (Total: {total})", ""]
    lines.append("3. ABSENTEE / HALF-DAY / LATE REMARKS")
    remarks = []
    for e in emps:
        r = recs.get(e["_id"])
        if not r:
            remarks.append(f"   • {e['emp_code']} | {e['name']} ({e['trade']}) — [NOT MARKED] @ {e['site']}")
        elif r["status"] != "P" or r.get("late"):
            tag = r["status"] + (" • LATE " + r["check_in"] if r.get("late") else "")
            remarks.append(f"   • {e['emp_code']} | {e['name']} ({e['trade']}) — [{tag}] @ {e['site']}")
    lines += remarks or ["   • None — full attendance"]
    lines += ["", "Generated via PERFECT ENGINERING Digital Attendance Portal"]
    return {"date": day, "text": "\n".join(lines)}
