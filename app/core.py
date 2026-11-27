"""核心模块：配置、日期解析、分类、数据模型、SQLite 存储（多城市版）。"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import cities as C

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "events.db"

JST = timezone(timedelta(hours=9), name="Asia/Tokyo")

WINDOW_PAST_DAYS = 30
WINDOW_FUTURE_DAYS = 400

HTTP_TIMEOUT = 30
HTTP_DELAY = 0.35
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_ACTIVE_CITY = "fukuoka"


def set_city(cid: str):
    global _ACTIVE_CITY
    _ACTIVE_CITY = cid


def current_city() -> str:
    return _ACTIVE_CITY


# --------------------------------------------------------------------------
# 日付パース
# --------------------------------------------------------------------------
FULLWIDTH = str.maketrans("０１２３４５６７８９：－〜", "0123456789:-~")
JP_DATE = re.compile(
    r"(?:(令和|平成|昭和)(\d{1,2})年)?(?:(\d{2,4})年)?"
    r"(\d{1,2})月(?:(上旬|中旬|下旬|末)|(\d{1,2})日)"
)
SLASH_DATE = re.compile(r"(?<![\d:：])(?:(\d{4})[./\-])?(\d{1,2})[./\-](\d{1,2})(?![\d:：])")
TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
RANGE_MARKS = ("～", "〜", "~", "から", "まで", "→", "－", "—", "-")
ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925}


@dataclass
class Period:
    start: datetime
    end: datetime
    all_day: bool
    note: str = ""


@dataclass
class _Token:
    y: int | None
    m: int
    d: int | None
    pos: int
    end: int
    part: str = ""


def _norm(text: str) -> str:
    return (text or "").translate(FULLWIDTH).replace("\u3000", " ")


def _eom(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _resolve_year(y, m, d, anchor: date) -> int:
    if y:
        return y
    cands = []
    for yy in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            dt = date(yy, m, d)
        except ValueError:
            continue
        cands.append((abs((dt - anchor).days), dt >= anchor - timedelta(days=180), yy))
    cands.sort(key=lambda c: (not c[1], c[0]))
    return cands[0][2] if cands else anchor.year


def find_date_tokens(text: str) -> list[_Token]:
    t = _norm(text)
    tokens: list[_Token] = []
    for m in JP_DATE.finditer(t):
        era, era_y, west_y, month, part, day = m.groups()
        year = None
        if era:
            year = ERA_BASE[era] + int(era_y)
        elif west_y:
            year = int(west_y)
            if year < 100:
                year += 2000
        tokens.append(_Token(year, int(month), int(day) if day else None, m.start(), m.end(), part or ""))
    for m in SLASH_DATE.finditer(t):
        if any(tk.pos <= m.start() <= tk.end for tk in tokens):
            continue
        y, mm, dd = m.group(1), int(m.group(2)), int(m.group(3))
        if mm > 12 or dd > 31:
            continue
        tokens.append(_Token(int(y) if y else None, mm, dd, m.start(), m.end()))
    tokens.sort(key=lambda tk: tk.pos)
    return tokens


def _token_date(tk: _Token, anchor: date, inherit_month=None, inherit_year=None) -> date:
    m = tk.m or (inherit_month or anchor.month)
    if tk.d is None:
        y = tk.y or inherit_year or _resolve_year(None, m, 15, anchor)
        day = 10 if tk.part == "上旬" else 20 if tk.part == "中旬" else _eom(y, m)
        return date(y, m, day)
    y = tk.y or inherit_year or _resolve_year(None, m, tk.d, anchor)
    try:
        return date(y, m, tk.d)
    except ValueError:
        return date(y, m, _eom(y, m))


def _is_range(text: str, a: _Token, b: _Token) -> bool:
    between = text[a.end:b.pos]
    return (not between) or any(mark in between for mark in RANGE_MARKS) or len(between) <= 6


def _add_hours(t: time, hours: int) -> time:
    total = min(t.hour * 60 + t.minute + hours * 60, 23 * 60 + 59)
    return time(total // 60, total % 60)


def _extract_times(text: str):
    t = _norm(text)
    wj = list(re.finditer(r"(午前|午後)(\d{1,2})時(?:(\d{1,2})分)?", t))
    if wj:
        def conv(m):
            h = int(m.group(2)) + (12 if m.group(1) == "午後" and int(m.group(2)) < 12 else 0)
            return time(min(h, 23), int(m.group(3) or 0))
        start = conv(wj[0])
        if len(wj) >= 2 and any(mark in t[wj[0].end():wj[1].start()] for mark in ("から", "～", "〜", "-")):
            end = conv(wj[1])
            if end > start:
                return start, end, ""
        return start, _add_hours(start, 2), ""
    for kw in ("開演", "開始", "スタート", "点灯式", "点灯"):
        idx = t.find(kw)
        if idx >= 0:
            mm = TIME_RE.search(t[idx: idx + 20])
            if mm and int(mm.group(1)) <= 23:
                start = time(int(mm.group(1)), int(mm.group(2)))
                return start, _add_hours(start, 2), ""
    times = [(int(m.group(1)), int(m.group(2)), m.start(), m.end()) for m in TIME_RE.finditer(t)]
    times = [x for x in times if 0 <= x[0] <= 24 and 0 <= x[1] <= 59]
    if not times:
        return None, None, ""
    if len(times) >= 2 and _looks_like_range(t, times[0], times[1]):
        h1, m1 = times[0][0], times[0][1]
        h2, m2v = times[1][0], times[1][1]
        start = time(min(h1, 23), m1)
        end = time(min(h2, 23), m2v) if (h2, m2v) > (h1, m1) else time(23, 59)
        note = " / ".join(f"{h:02d}:{mi:02d}" for h, mi, _, _ in times[:4]) if len(times) > 2 else ""
        return start, end, note
    h, mi = times[0][0], times[0][1]
    note = " / ".join(f"{x[0]:02d}:{x[1]:02d}" for x in times[:4]) if len(times) > 1 else ""
    start = time(min(h, 23), mi)
    return start, _add_hours(start, 2), note


def _looks_like_range(text: str, a, b) -> bool:
    between = text[a[3]:b[2]]
    return len(between) <= 4 or any(mark in between for mark in ("〜", "～", "~", "-", "－", "から", "まで"))


def parse_period(text: str, anchor: date, default_hours: int = 2) -> Period | None:
    """「2026年12月12日（土）～2027年2月23日」などを期間に変換。"""
    if not text:
        return None
    t = _norm(text)
    tokens = find_date_tokens(t)
    if not tokens:
        return None
    start_d = _token_date(tokens[0], anchor)
    end_d = start_d
    if len(tokens) >= 2 and _is_range(t, tokens[0], tokens[1]):
        end_d = _token_date(tokens[1], anchor, tokens[0].m, start_d.year)
        if end_d < start_d:
            end_d = end_d.replace(year=end_d.year + 1)
        if end_d < start_d:
            end_d = start_d
    elif len(tokens) >= 2:
        last = _token_date(tokens[-1], anchor, tokens[0].m, start_d.year)
        if last >= start_d:
            end_d = last
    start_t, end_t, note = _extract_times(t)
    if start_t is None:
        return Period(datetime.combine(start_d, time(0, 0)), datetime.combine(end_d, time(23, 59)), True)
    start = datetime.combine(start_d, start_t)
    end = datetime.combine(end_d, end_t)
    if end <= start:
        end = start + timedelta(hours=default_hours)
    return Period(start, end, False, note)


def to_jst_iso(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def fmt_date_range(start: datetime, end: datetime, all_day: bool) -> str:
    if start.date() == end.date():
        if all_day:
            return start.strftime("%m/%d")
        return f"{start.strftime('%m/%d %H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%Y/%m/%d')} – {end.strftime('%Y/%m/%d')}"


# --------------------------------------------------------------------------
# カテゴリ
# --------------------------------------------------------------------------
CATEGORIES = {
    "market": {"label": "市集/マルシェ", "zh": "市集", "color": "#f59e0b", "icon": "🧺"},
    "flea": {"label": "フリマ/蚤の市", "zh": "跳蚤市场", "color": "#a855f7", "icon": "🪑"},
    "festival": {"label": "祭り/フェス", "zh": "节庆", "color": "#ef4444", "icon": "🎊"},
    "illumination": {"label": "灯饰/ライトアップ", "zh": "灯饰", "color": "#06b6d4", "icon": "✨"},
    "exhibition": {"label": "展覧会", "zh": "展览", "color": "#3b82f6", "icon": "🖼"},
    "performance": {"label": "舞台/コンサート", "zh": "演出", "color": "#ec4899", "icon": "🎭"},
    "game": {"label": "ゲーム/謎解き", "zh": "游戏", "color": "#22c55e", "icon": "🎮"},
    "workshop": {"label": "体験/ワークショップ", "zh": "体验", "color": "#84cc16", "icon": "🔬"},
    "food": {"label": "グルメ/食", "zh": "美食", "color": "#f97316", "icon": "🍜"},
    "sports": {"label": "スポーツ", "zh": "运动", "color": "#0ea5e9", "icon": "⛸"},
    "other": {"label": "その他", "zh": "其他", "color": "#64748b", "icon": "📌"},
}

_RULES = [
    ("flea", ("フリーマーケット", "フリマ", "蚤の市", "骨董市", "ガレージセール", "青空市")),
    ("market", ("マルシェ", "朝市", "夜市", "クリスマスマーケット", "クリスマス市", "市集", "マーケット",
                "出店", "屋台村", "物産展", "直売")),
    ("illumination", ("イルミネーション", "ライトアップ", "点灯", "キャンドルナイト", "キャンドル", "光の",
                      "プロジェクションマッピング", "灯彩", "冬の光")),
    ("festival", ("祭", "まつり", "フェス", "フェスティバル", "奉納", "神幸", "山笠", "どんたく", "盆踊")),
    ("game", ("ゲーム", "eスポーツ", "e-Sports", "ボードゲーム", "カードゲーム", "謎解き", "解迷", "脱出",
              "ポケモン", "TRPG")),
    ("exhibition", ("展覧会", "企画展", "特別展", "美術展", "写真展", "絵画展", "巡回展", "常設展", "展")),
    ("performance", ("コンサート", "演奏会", "ライブ", "舞台", "ミュージカル", "オペラ", "公演", "演劇",
                     "落語", "サーカス", "バレエ", "ダンス", "リサイタル", "管弦", "交響")),
    ("workshop", ("体験", "ワークショップ", "工作", "教室", "天体観測", "観察会", "プログラミング", "科学",
                  "親子", "ものづくり", "講座")),
    ("sports", ("マラソン", "駅伝", "スケート", "スキー", "スポーツ", "ヨガ", "ランニング", "自転車")),
    ("food", ("グルメ", "フード", "食べ", "飲食", "ワイン", "ビール", "日本酒", "ラーメン", "スイーツ")),
]


def _nf(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def classify(title: str, description: str = "", default: str = "other") -> str:
    t = _nf(title)
    for code, keys in _RULES:
        if any(k in t for k in keys):
            return code
    d = _nf(description)
    for code, keys in _RULES:
        if any(k in d for k in keys):
            return code
    return default


def detect_ward(*texts) -> str:
    """按当前城市的区域关键词判定「区/エリア」。"""
    hay = _nf(" ".join(t for t in texts if t))
    for area, keys in C.areas(_ACTIVE_CITY).items():
        for k in keys:
            if k in hay:
                return area
    low = hay.lower()
    for k, area in C.venues(_ACTIVE_CITY).items():
        if k.lower() in low:
            return area
    m = re.search(r"([^\s、,，。市区町]{1,4}区)", hay)
    if m:
        name = m.group(1)
        if name in C.wards(_ACTIVE_CITY) or name in C.areas(_ACTIVE_CITY):
            return name
    return ""


def extract_venue(title: str, fallback: str = "") -> str:
    m = re.match(r"^[【\[（(]([^】\]）)]{2,24})[】\]）)]", _nf(title))
    return m.group(1) if m else fallback


def normalize_title(title: str) -> str:
    t = _nf(title).lower()
    return re.sub(r"[【】\[\]（）()「」『』<>〈〉、,，.。!！?？:：;；\-ー–—~〜\s]", "", t)


# --------------------------------------------------------------------------
# データモデル
# --------------------------------------------------------------------------
@dataclass
class Event:
    source: str
    source_key: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = True
    category: str = "other"
    tags: list = field(default_factory=list)
    venue: str = ""
    address: str = ""
    ward: str = ""
    city: str = ""
    price: str = ""
    access: str = ""
    url: str = ""
    image: str = ""
    description: str = ""
    note: str = ""

    @property
    def uid(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.source_key}".encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        key = f"{normalize_title(self.title)[:24]}|{self.start.date().isoformat()}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @property
    def display_venue(self) -> str:
        return self.venue or self.address or "会場未確認"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        d["start_at"] = to_jst_iso(self.start)
        d["end_at"] = to_jst_iso(self.end)
        d["category_label"] = CATEGORIES.get(self.category, CATEGORIES["other"])["label"]
        d["category_color"] = CATEGORIES.get(self.category, CATEGORIES["other"])["color"]
        d["display_venue"] = self.display_venue
        d["date_label"] = fmt_date_range(self.start, self.end, self.all_day)
        return d


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'other',
    start_at TEXT NOT NULL, end_at TEXT NOT NULL, all_day INTEGER NOT NULL DEFAULT 1,
    venue TEXT DEFAULT '', address TEXT DEFAULT '', ward TEXT DEFAULT '', city TEXT DEFAULT '',
    price TEXT DEFAULT '', access TEXT DEFAULT '', url TEXT DEFAULT '', image TEXT DEFAULT '',
    description TEXT DEFAULT '', note TEXT DEFAULT '', tags TEXT DEFAULT '[]', sources TEXT DEFAULT '[]',
    fingerprint TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_events_city ON events(city);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, started_at TEXT, finished_at TEXT,
    found INTEGER DEFAULT 0, saved INTEGER DEFAULT 0, ok INTEGER DEFAULT 1, error TEXT DEFAULT ''
);
"""

_FIELDS = ["title", "category", "start_at", "end_at", "all_day", "venue", "address", "ward",
           "city", "price", "access", "url", "image", "description", "note", "tags", "sources",
           "fingerprint", "first_seen", "last_seen", "active"]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["sources"] = json.loads(d.get("sources") or "[]")
    d["all_day"] = bool(d.get("all_day"))
    return d


def _merge(old: dict, new: dict) -> dict:
    merged = dict(old)
    for k in ("title", "category", "start_at", "end_at", "all_day"):
        if new.get(k):
            merged[k] = new[k]
    for k in ("venue", "address", "ward", "city", "price", "access", "url", "image", "description", "note"):
        if new.get(k) and (not merged.get(k) or len(str(new[k])) > len(str(merged[k]))):
            merged[k] = new[k]
    merged["tags"] = list(dict.fromkeys(list(old.get("tags") or []) + list(new.get("tags") or [])))
    srcs = list(old.get("sources") or [])
    for s in new.get("sources") or []:
        if not any(x.get("source") == s.get("source") and x.get("key") == s.get("key") for x in srcs):
            srcs.append(s)
    merged["sources"] = srcs
    return merged


def _update(conn, uid, m, now):
    conn.execute(
        "UPDATE events SET title=?, category=?, start_at=?, end_at=?, all_day=?, venue=?, address=?, "
        "ward=?, city=?, price=?, access=?, url=?, image=?, description=?, note=?, tags=?, sources=?, "
        "last_seen=?, active=1 WHERE uid=?",
        (m["title"], m["category"], m["start_at"], m["end_at"], m["all_day"], m["venue"], m["address"],
         m["ward"], m["city"], m["price"], m["access"], m["url"], m["image"], m["description"], m["note"],
         json.dumps(m["tags"], ensure_ascii=False), json.dumps(m["sources"], ensure_ascii=False), now, uid),
    )


def upsert(ev: Event, now: str | None = None) -> str:
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    d = ev.to_dict()
    d.pop("raw", None)
    d.pop("uid", None)
    d["tags_json"] = json.dumps(d.pop("tags") or [], ensure_ascii=False)
    d["sources_json"] = json.dumps(
        [{"source": ev.source, "key": ev.source_key, "url": ev.url}], ensure_ascii=False
    )
    d.pop("category_label", None)
    d.pop("category_color", None)
    d.pop("display_venue", None)
    d.pop("date_label", None)
    d["all_day"] = 1 if ev.all_day else 0
    d["fingerprint"] = ev.fingerprint
    d["first_seen"] = d["last_seen"] = now
    d["active"] = 1

    with connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE uid=?", (ev.uid,)).fetchone()
        if row:
            old = _row(row)
            m = _merge(old, {**d, "tags": json.loads(d["tags_json"]),
                             "sources": json.loads(d["sources_json"])})
            _update(conn, ev.uid, m, now)
            return "updated"

        cands = conn.execute(
            "SELECT * FROM events WHERE active=1 AND city=? AND substr(start_at,1,7) IN (?,?)",
            (ev.city, ev.start.strftime("%Y-%m"), ev.end.strftime("%Y-%m")),
        ).fetchall()
        best, best_score = None, 0.0
        norm_new = normalize_title(ev.title)
        ev_s, ev_e = naive(ev.start), naive(ev.end)
        for c in cands:
            score = SequenceMatcher(None, norm_new, normalize_title(c["title"])).ratio()
            if score < 0.93:
                continue
            s_old = datetime.fromisoformat(c["start_at"]).replace(tzinfo=None)
            e_old = datetime.fromisoformat(c["end_at"]).replace(tzinfo=None)
            if max(ev_s, s_old) > min(ev_e, e_old):
                continue
            if score > best_score:
                best, best_score = c, score
        if best is not None:
            old = _row(best)
            if (old.get("venue") and ev.venue and old.get("address") and ev.address
                    and SequenceMatcher(None, old["address"], ev.address).ratio() < 0.35):
                best = None
            else:
                m = _merge(old, {**d, "tags": json.loads(d["tags_json"]),
                                 "sources": json.loads(d["sources_json"])})
                _update(conn, best["uid"], m, now)
                return "merged"

        values = []
        for f in _FIELDS:
            if f == "tags":
                values.append(d["tags_json"])
            elif f == "sources":
                values.append(d["sources_json"])
            else:
                values.append(d[f])
        conn.execute(
            f"INSERT INTO events (uid,{','.join(_FIELDS)}) VALUES (?,{','.join('?' * len(_FIELDS))})",
            [ev.uid] + values,
        )
        return "inserted"


def events_between(start: datetime, end: datetime, city: str | None = None) -> list[dict]:
    s = naive(start).strftime("%Y-%m-%d")
    e = naive(end).strftime("%Y-%m-%d")
    sql = ("SELECT * FROM events WHERE active=1 AND substr(start_at,1,10) <= ? "
           "AND substr(end_at,1,10) >= ?")
    params: list = [e, s]
    if city:
        sql += " AND city=?"
        params.append(city)
    sql += " ORDER BY start_at"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def count_active(city: str | None = None) -> int:
    with connect() as conn:
        if city:
            return conn.execute("SELECT COUNT(*) FROM events WHERE active=1 AND city=?",
                                (city,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM events WHERE active=1").fetchone()[0]


def city_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT city, COUNT(*) AS n FROM events WHERE active=1 GROUP BY city").fetchall()
    return {r["city"]: r["n"] for r in rows}


def record_run(source, started, finished, found, saved, ok, error=""):
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (source, started_at, finished_at, found, saved, ok, error) VALUES (?,?,?,?,?,?,?)",
            (source, started, finished, found, saved, 1 if ok else 0, error[:500]),
        )


_SOURCE_LABELS = {
    "walkerplus": "ウォーカープラス",
    "gotokyo": "GO TOKYO（東京都公式）",
    "kyoto-cards0": "京都観光Navi（京都市公式）",
    "kobe-cards0": "Feel KOBE（神戸市公式）",
    "sapporo-cards0": "ようこそさっぽろ（札幌公式）",
    "okinawa-cards0": "おきなわ物語（沖縄観光情報）",
    "nagoya-cards0": "名古屋コンシェルジュ",
    "nagasaki-cards0": "travel nagasaki（長崎市公式）",
    "narahaku": "奈良国立博物館",
    "kanazawa21": "金沢21世紀美術館",
    "city-event": "横浜市 市民利用施設イベント",
    "akarenga": "横浜赤レンガ倉庫",
    "art-museum": "横浜美術館",
    "kanagawa-kankou": "観光かながわNOW",
    "sankeien": "三溪園",
    "yokanavi": "福岡観光ナビ",
    "konohen": "ここへん",
    "rapinc": "ラピンク",
    "crossroad": "crossroad",
    "manual": "手工整理",
    "recurring": "定期活动",
}


def source_status(city: str | None = None) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, MAX(finished_at) AS last_run, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_runs, "
            "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS fail_runs FROM runs GROUP BY source ORDER BY source"
        ).fetchall()
        counts = conn.execute("SELECT city, sources FROM events WHERE active=1").fetchall()
    per = {}
    for c in counts:
        if city and c["city"] != city:
            continue
        for s in json.loads(c["sources"] or "[]"):
            key = (c["city"], s["source"])
            per[key] = per.get(key, 0) + 1
    out = []
    for r in rows:
        dd = dict(r)
        prefix = ""
        if ":" in str(dd["source"]):
            prefix = str(dd["source"]).split(":", 1)[0]
        if city and not str(dd["source"]).startswith(city + ":"):
            continue
        dd["events"] = per.get((prefix, str(dd["source"]).split(":", 1)[-1]), 0)
        dd["short"] = _SOURCE_LABELS.get(
            str(dd["source"]).split(":", 1)[-1],
            str(dd["source"]).split(":", 1)[-1],
        )
        if not city and prefix:
            try:
                from . import cities as _C
                dd["label"] = f"{_C.get(prefix)['name']} · {dd['short']}"
            except Exception:
                dd["label"] = dd["short"]
        else:
            dd["label"] = dd["short"]
        out.append(dd)
    return out


def prune_old(days: int = 45) -> int:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        return conn.execute("UPDATE events SET active=0 WHERE end_at < ? AND active=1", (cutoff,)).rowcount


def delete_inactive() -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM events WHERE active=0").rowcount
