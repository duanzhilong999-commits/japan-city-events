"""収集パイプライン（多城市）。"""

from __future__ import annotations

import traceback
from datetime import date, datetime

from . import cities as C
from . import collectors
from .core import count_active, init_db, prune_old, record_run, set_city, upsert


def run(sources=None, anchor: date | None = None, verbose: bool = True,
        city_ids: list[str] | None = None) -> dict:
    anchor = anchor or date.today()
    init_db()
    prune_old()
    summary = {"events": 0, "sources": {}, "by_city": {}}
    targets = city_ids or C.all_ids()
    for cid in targets:
        set_city(cid)
        insts = collectors.sources_for(cid)
        for c in insts:
            started = datetime.now()
            found = saved = 0
            ok, error = True, ""
            label = f"{cid}:{c.name}"
            try:
                events = c.collect(anchor)
                found = len(events)
                for ev in events:
                    ev.city = cid
                    try:
                        upsert(ev)
                        saved += 1
                    except Exception as exc:
                        c.errors.append(f"save: {exc}")
            except Exception as exc:
                ok, error = False, str(exc)
                if verbose:
                    traceback.print_exc()
            finished = datetime.now()
            record_run(label, started.isoformat(timespec="seconds"),
                       finished.isoformat(timespec="seconds"), found, saved, ok, error)
            secs = round((finished - started).total_seconds(), 1)
            summary["sources"][label] = {"found": found, "saved": saved, "ok": ok,
                                        "seconds": secs, "errors": c.errors[:5]}
            if verbose:
                print(f"[{label}] found={found} saved={saved} ok={ok} {secs}s")
                for e in c.errors[:5]:
                    print("   !", e)
        summary["by_city"][cid] = count_active(cid)
    summary["events"] = count_active()
    return summary


if __name__ == "__main__":
    print(run())
