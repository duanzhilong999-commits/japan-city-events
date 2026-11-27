"""iCalendar (RFC 5545) 导出：给 iPhone / Google 日历订阅用。"""

from __future__ import annotations

from datetime import datetime, timedelta

PRODID = "-//Japan City Event Calendar//JA//"
TZID = "Asia/Tokyo"

VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "X-LIC-LOCATION:Asia/Tokyo",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0900",
    "TZOFFSETTO:+0900",
    "TZNAME:JST",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def _esc(text: str) -> str:
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """按 75 字节折行（UTF-8 安全），续行以单个空格开头。"""
    out, cur, cur_len, first = [], "", 0, True
    for ch in line:
        b = len(ch.encode("utf-8"))
        limit = 75 if first else 74
        if cur_len + b > limit and cur:
            out.append(cur if first else " " + cur)
            first = False
            cur, cur_len = "", 0
        cur += ch
        cur_len += b
    if cur or not out:
        out.append(cur if first else " " + cur)
    return out


def _dt(d: datetime) -> str:
    return d.strftime("%Y%m%dT%H%M%S")


def build_ics(events: list[dict], cal_name: str = "日本热门城市活动日历",
              now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(cal_name)}",
        "X-WR-TIMEZONE:Asia/Tokyo",
        "X-WR-CALDESC:日本主要城市のイベント・市集・展示・祭り自動収集カレンダー",
    ]
    lines += VTIMEZONE

    for ev in events:
        start = datetime.fromisoformat(ev["start_at"])
        end = datetime.fromisoformat(ev["end_at"])
        all_day = bool(ev.get("all_day"))
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev['uid']}@japan-city-events")
        lines.append(f"DTSTAMP:{_dt(now)}")
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(end + timedelta(days=1)).strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART;TZID={TZID}:{_dt(start)}")
            lines.append(f"DTEND;TZID={TZID}:{_dt(end)}")
        lines.append(f"SUMMARY:{_esc(ev['title'])}")
        loc = ev.get("venue") or ev.get("address") or ""
        if loc:
            lines.append(f"LOCATION:{_esc(loc)}")
        desc_parts = []
        if ev.get("date_label"):
            desc_parts.append(f"開催日: {ev['date_label']}")
        if ev.get("price"):
            desc_parts.append(f"料金: {ev['price']}")
        if ev.get("description"):
            desc_parts.append(str(ev["description"])[:400])
        if ev.get("url"):
            desc_parts.append(f"詳細: {ev['url']}")
        if desc_parts:
            lines.append(f"DESCRIPTION:{_esc(chr(10).join(desc_parts))}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        if ev.get("category_label"):
            lines.append(f"CATEGORIES:{_esc(ev['category_label'])}")
        lines.append("TRANSP:OPAQUE")
        lines.append("BEGIN:VALARM")
        lines.append("ACTION:DISPLAY")
        lines.append(f"DESCRIPTION:{_esc(ev['title'])}")
        lines.append("TRIGGER:-PT2H" if all_day else "TRIGGER:-PT1H")
        lines.append("END:VALARM")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    folded = []
    for ln in lines:
        folded.extend(_fold(ln))
    return "\r\n".join(folded) + "\r\n"
