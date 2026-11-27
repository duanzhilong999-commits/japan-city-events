"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta

import requests

from . import cities as C
from .core import (BASE_DIR, CATEGORIES, connect, count_active, city_counts, events_between,
                   init_db, source_status)


def cmd_collect(args):
    from .pipeline import run
    ids = None if args.city in ("", "all") else [args.city]
    anchor = date.fromisoformat(args.anchor) if args.anchor else date.today()
    s = run(anchor=anchor, verbose=True, city_ids=ids)
    print("\n各城市入库：", json.dumps(s["by_city"], ensure_ascii=False))


def cmd_serve(args):
    import uvicorn
    uvicorn.run("app.web:app", host=args.host, port=args.port, reload=args.reload)


def cmd_list(args):
    init_db()
    s = datetime.fromisoformat(args.start)
    e = datetime.fromisoformat(args.end) if args.end else s + timedelta(days=31)
    cid = None if args.city in ("", "all") else args.city
    evs = events_between(s, e, cid)
    for ev in evs[: args.limit]:
        print(f"{ev['start_at'][:16]}  [{C.display(ev['city'])}] {ev['title'][:52]}  @ {ev['display_venue'] if 'display_venue' in ev else ev['venue']}")
    print(f"\n共 {len(evs)} 个")


def cmd_stats(args):
    init_db()
    print(f"有效活动：{count_active()}")
    print("各城市：", json.dumps(city_counts(), ensure_ascii=False))
    print(f"{'source':<28}{'events':>7}  last_run             ok/fail")
    for s in source_status():
        print(f"{s['source']:<28}{s['events']:>7}  {str(s['last_run'] or '-'):<20} {s['ok_runs']}/{s['fail_runs']}")


def cmd_ics(args):
    from .ics import build_ics
    init_db()
    s = datetime.now() - timedelta(days=args.days_before)
    e = datetime.now() + timedelta(days=args.days_after)
    cid = None if args.city == "all" else args.city
    body = build_ics(events_between(s, e, cid),
                     cal_name=(f"{C.display(cid)}活动日历" if cid else "日本热门城市活动日历"))
    out = BASE_DIR / (args.out or (f"{cid or 'japan'}-events.ics"))
    out.write_text(body, encoding="utf-8", newline="")
    print(f"已导出 {out}")


def cmd_static(args):
    from .web import page_context, TEMPLATES
    init_db()
    css = (BASE_DIR / "web" / "static" / "style.css").read_text(encoding="utf-8")
    outdir = BASE_DIR / "docs" / args.month
    outdir.mkdir(parents=True, exist_ok=True)
    ids = C.all_ids() if args.city == "all" else [args.city]
    total = 0
    cards = []
    for cid in ids:
        ctx = page_context(month=args.month, city=cid)
        html = TEMPLATES.get_template("index.html").render(**ctx)
        html = html.replace('<link rel="stylesheet" href="/static/style.css">', f"<style>\n{css}\n</style>")
        html = html.replace("fetch('/refresh?city=", "Promise.resolve({json:async()=>({message:'静态页面不支持抓取',ok:false})});//")
        (outdir / f"{cid}.html").write_text(html, encoding="utf-8")
        total += ctx["total"]
        cards.append((cid, ctx["city_name"], ctx["city_ja"], ctx["total"], ctx["all_total"], ctx["active"]))
        print(f"{cid:<10} 本月 {ctx['total']:>4} 个（含跨月 {ctx['all_total']}）")

    items = "\n".join(
        f'<li><a href="{cid}.html"><b>{name}</b> <span>{ja}</span></a>'
        f'<em>本月 {t} 个 · 库内 {a} 个</em></li>' for cid, name, ja, t, _x, a in cards)
    (outdir / "index.html").write_text(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{args.month} 日本热门城市活动日历</title>
<style>body{{font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;background:#f6f8fb;margin:0;padding:32px;color:#0f172a}}
h1{{font-size:26px;margin:0 0 6px}}p.sub{{color:#64748b;margin:0 0 24px}}
ul{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
li{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px}}
a{{text-decoration:none;color:#0f172a;display:block}}a span{{color:#94a3b8;font-size:12px;margin-left:6px}}
em{{display:block;color:#64748b;font-style:normal;font-size:12px;margin-top:6px}}</style></head><body>
<h1>{args.month.replace('-', '年')}月 · 日本热门城市活动日历</h1>
<p class="sub">共 {len(cards)} 个城市 · 本月合计 {total} 个活动 · 数据来自各城市官方/本地媒体站点自动采集</p>
<ul>{items}</ul></body></html>""", encoding="utf-8")
    print(f"\n已导出 {len(cards)} 个城市页面 → {outdir}")


def cmd_verify(args):
    init_db()
    s = datetime.fromisoformat(args.start) if args.start else datetime.now()
    evs = events_between(s, s + timedelta(days=args.days))
    evs = [e for e in evs if e.get("url")]
    sample = random.sample(evs, min(args.limit, len(evs)))
    ok = 0
    for ev in sample:
        try:
            r = requests.get(ev["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            status = r.status_code
            good = status < 400
        except Exception as exc:
            status, good = str(exc)[:40], False
        ok += good
        print(f"{'OK ' if good else 'BAD'} {status} [{C.display(ev['city'])}] {ev['title'][:40]} {ev['url'][:80]}")
    print(f"\n{ok}/{len(sample)} 链接可回源")


def main(argv=None):
    p = argparse.ArgumentParser(prog="japan-city-events")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="抓取所有城市并入库")
    c.add_argument("--city", default="all")
    c.add_argument("--anchor", help="以哪天为“今天”解析日期")
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("serve", help="启动网页")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    l = sub.add_parser("list", help="列出某段时间的活动")
    l.add_argument("--city", default="all")
    l.add_argument("--start", default=datetime.now().strftime("%Y-%m-%d"))
    l.add_argument("--end", default="")
    l.add_argument("--limit", type=int, default=60)
    l.set_defaults(func=cmd_list)

    st = sub.add_parser("stats", help="各来源状态")
    st.set_defaults(func=cmd_stats)

    ic = sub.add_parser("ics", help="导出 .ics")
    ic.add_argument("--city", default="all")
    ic.add_argument("--out", default="")
    ic.add_argument("--days-before", type=int, default=7)
    ic.add_argument("--days-after", type=int, default=180)
    ic.set_defaults(func=cmd_ics)

    sx = sub.add_parser("static", help="导出单文件月历（每城市一个 HTML + 索引）")
    sx.add_argument("--month", default="2026-12")
    sx.add_argument("--city", default="all")
    sx.set_defaults(func=cmd_static)

    vf = sub.add_parser("verify", help="抽样回源站确认链接可打开")
    vf.add_argument("--start", default="")
    vf.add_argument("--days", type=int, default=45)
    vf.add_argument("--limit", type=int, default=15)
    vf.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
