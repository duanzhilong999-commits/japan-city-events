"""Web 界面：多城市月历 + 列表 + ICS 订阅。"""

from __future__ import annotations

import calendar
import threading
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cities as C
from .core import (BASE_DIR, CATEGORIES, city_counts, count_active, events_between, init_db,
                   source_status, to_jst_iso)
from .ics import build_ics

WEB_DIR = BASE_DIR / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))
TEMPLATES.env.filters["jp_date"] = lambda d: f"{d.month}月{d.day}日"
TEMPLATES.env.filters["wday"] = lambda d: "月火水木金土日"[d.weekday()]

app = FastAPI(title="日本热门城市活动日历", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "last": None, "message": ""}
DEFAULT_CITY = "fukuoka"


def parse_month(s: str | None) -> tuple[int, int]:
    today = date.today()
    if not s:
        return today.year, today.month
    try:
        y, m = s.split("-")[:2]
        return int(y), int(m)
    except Exception:
        return today.year, today.month


def month_bounds(y: int, m: int) -> tuple[datetime, datetime, date, date]:
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    return (datetime(y, m, 1), datetime(y, m, last.day, 23, 59, 59), first, last)


def month_grid(y: int, m: int) -> list[list[date]]:
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    rows, cur = [], start
    while cur <= end:
        rows.append([cur + timedelta(days=i) for i in range(7)])
        cur += timedelta(days=7)
    return rows


def event_dates(ev: dict) -> tuple[date, date]:
    return datetime.fromisoformat(ev["start_at"]).date(), datetime.fromisoformat(ev["end_at"]).date()


def _is_free(ev: dict) -> bool:
    p = (ev.get("price") or "") + " " + (ev.get("description") or "")
    return any(k in p for k in ("無料", "入場無料", "0円", "フリー"))


def enrich(ev: dict) -> dict:
    cat = CATEGORIES.get(ev.get("category"), CATEGORIES["other"])
    ev["category_color"] = cat["color"]
    ev["category_label"] = cat["label"]
    ev["category_zh"] = cat["zh"]
    ev["category_icon"] = cat["icon"]
    ev["display_venue"] = ev.get("venue") or ev.get("address") or ev.get("ward") or ""
    ev["city_name"] = C.display(ev.get("city", ""))
    try:
        s, e = event_dates(ev)
        ev["date_label"] = s.strftime("%m/%d") if s == e else f"{s.strftime('%m/%d')}–{e.strftime('%m/%d')}"
    except Exception:
        ev["date_label"] = ""
    return ev


def apply_filters(events: list[dict], cats: list[str], area: str, q: str,
                  free: bool, weekend: bool) -> list[dict]:
    out = []
    for ev in events:
        if cats and ev["category"] not in cats:
            continue
        if area and ev.get("ward") != area:
            continue
        if free and not _is_free(ev):
            continue
        if weekend:
            s, e = event_dates(ev)
            if not any((s + timedelta(days=i)).weekday() >= 5 for i in range((e - s).days + 1)):
                continue
        if q:
            hay = " ".join(str(ev.get(k, "")) for k in ("title", "venue", "address", "description", "tags"))
            if q.lower() not in hay.lower():
                continue
        out.append(ev)
    return out


def page_context(month: str | None = None, city: str | None = None, cat: str | None = None,
                 area: str = "", q: str = "", free: int = 0, weekend: int = 0) -> dict:
    """构建月历页所需的全部数据（网页和静态导出共用）。"""
    cid = city if city in C.CITIES else DEFAULT_CITY
    y, m = parse_month(month)
    start, end, first, last = month_bounds(y, m)
    all_events = events_between(start, end, cid)
    cats = [c for c in (cat or "").split(",") if c]
    events = [enrich(dict(ev)) for ev in apply_filters(all_events, cats, area, q.strip(), bool(free), bool(weekend))]

    by_day: dict[str, list[dict]] = {}
    for ev in events:
        s, e = event_dates(ev)
        cur = max(s, first)
        stop = min(e, last)
        while cur <= stop:
            by_day.setdefault(cur.isoformat(), []).append(ev)
            cur += timedelta(days=1)

    today = date.today()
    grid = []
    for row in month_grid(y, m):
        cells = []
        for d in row:
            key = d.isoformat()
            items = by_day.get(key, [])
            items = sorted(items, key=lambda x: (x["start_at"][:10] != key, x["start_at"]))
            starting = [x for x in items if x["start_at"][:10] == key]
            ongoing = [x for x in items if x["start_at"][:10] != key]
            cells.append({
                "date": d, "day": d.day, "in_month": d.month == m,
                "today": d == today, "weekend": d.weekday() >= 5,
                "events": starting, "ongoing": ongoing,
            })
        grid.append(cells)

    order = sorted(events, key=lambda x: (x["start_at"][:10] < first.isoformat(), x["start_at"], x["title"]))
    counts = city_counts()
    tabs = [{"id": i, "name": C.get(i)["name"], "ja": C.get(i)["ja"], "n": counts.get(i, 0)} for i in C.all_ids()]
    present = sorted({e.get("ward") for e in all_events if e.get("ward")})
    all_areas = list(C.areas(cid).keys())
    area_list = [a for a in all_areas if a in present] + [a for a in present if a not in all_areas]

    return {
        "year": y, "month": m, "month_str": f"{y}-{m:02d}",
        "grid": grid, "events": order, "total": len(order),
        "all_total": len(all_events), "categories": CATEGORIES, "cats": cats,
        "city": cid, "city_name": C.get(cid)["name"], "city_ja": C.get(cid)["ja"],
        "city_pref": C.get(cid)["pref"], "tabs": tabs, "city_counts": counts,
        "ward": area, "wards": area_list, "q": q, "free": free, "weekend": weekend,
        "prev_month": (first - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (last + timedelta(days=1)).strftime("%Y-%m"),
        "today_str": f"{today.year}-{today.month:02d}",
        "sources": source_status(cid), "active": count_active(cid),
        "refresh": _refresh_state,
    }


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, month: str | None = None, city: str | None = None, cat: str | None = None,
          area: str = "", q: str = "", free: int = 0, weekend: int = 0, ward: str = ""):
    return TEMPLATES.TemplateResponse(
        request=request, name="index.html",
        context=page_context(month, city, cat, area or ward, q, free, weekend))


@app.get("/api/events")
def api_events(start: str | None = None, end: str | None = None, city: str | None = None,
               cat: str | None = None, area: str = "", q: str = "", free: int = 0, weekend: int = 0):
    s = datetime.fromisoformat(start) if start else datetime.now()
    e = datetime.fromisoformat(end) if end else s + timedelta(days=60)
    evs = events_between(s, e, city)
    evs = apply_filters(evs, [c for c in (cat or "").split(",") if c], area, q, bool(free), bool(weekend))
    return JSONResponse({"count": len(evs), "events": evs})


@app.get("/calendar.ics")
def calendar_ics(city: str = "", days_before: int = 7, days_after: int = 180):
    now = datetime.now()
    s = now - timedelta(days=days_before)
    e = now + timedelta(days=days_after)
    cid = city if city in C.CITIES else None
    evs = events_between(s, e, cid)
    name = f"{C.display(cid)}活动日历" if cid else "日本热门城市活动日历"
    body = build_ics(evs, cal_name=name)
    fname = f"{cid or 'japan'}-events.ics"
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{fname}"',
                             "Cache-Control": "no-cache"})


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, city: str = ""):
    cid = city if city in C.CITIES else ""
    return TEMPLATES.TemplateResponse(request=request, name="sources.html", context={
        "sources": source_status(cid or None), "active": count_active(cid or None),
        "refresh": _refresh_state, "tabs": [{"id": i, "name": C.get(i)["name"]} for i in C.all_ids()],
        "city": cid, "city_counts": city_counts(),
    })


@app.post("/refresh")
def refresh(anchor: str | None = None, city: str = ""):
    if _refresh_lock.locked():
        return JSONResponse({"ok": False, "message": "刷新正在进行中"}, status_code=409)

    def job():
        from .pipeline import run
        with _refresh_lock:
            _refresh_state.update(running=True, message="正在抓取…")
            try:
                a = date.fromisoformat(anchor) if anchor else date.today()
                ids = [city] if city in C.CITIES else None
                summary = run(anchor=a, verbose=False, city_ids=ids)
                _refresh_state.update(running=False, last=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                      message=f"完成：库内 {summary['events']} 个活动")
            except Exception as exc:  # noqa: BLE001
                _refresh_state.update(running=False, message=f"失败: {exc}")

    threading.Thread(target=job, daemon=True).start()
    return JSONResponse({"ok": True, "message": "已开始后台抓取，全部城市约 5–8 分钟"})


@app.get("/health")
def health():
    return {"ok": True, "events": count_active(), "cities": city_counts(),
            "time": to_jst_iso(datetime.now())}
