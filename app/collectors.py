"""各城市采集器（多城市版）。

每个 Collector 实例自带 name / label / homepage；
`source_key` 用「标题归一化 + 开始日」确保跨源不撞车。
"""

from __future__ import annotations

import json
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import cities as C
from . import fukuoka, yokohama
from .core import (HTTP_DELAY, HTTP_TIMEOUT, USER_AGENT, WINDOW_FUTURE_DAYS,
                   WINDOW_PAST_DAYS, Event, classify, detect_ward, normalize_title, parse_period)

DATE_RE = re.compile(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日")
NOISE = re.compile(
    r"(?:20\d{2}年\s*)?\d{1,2}月\s*\d{1,2}日\s*(?:[（(][^）)]{0,8}[）)])?"
    r"|20\d{2}\s*[./]\s*\d{1,2}\s*[./]\s*\d{1,2}(?:\s*[（(][^）)]{0,8}[）)])?"
    r"|\d{1,2}:\d{2}(?:\s*[〜～~-]\s*\d{1,2}:\d{2})?"
    r"|[〜～~]\s*|\s*[（(][^）)]{0,6}[）)]"
)
LABELS = ("その他", "キャンペーン", "イベント", "ステージ", "ショー", "ワークショップ", "グルメ",
          "ショッピング", "New", "NEW", "お知らせ", "セール", "特別展", "企画展", "コレクション展", "常設展")
TITLE_TAIL = ("が開催されます！", "が開催されます", "を開催します！", "を開催します",
              "【終了しました】", "終了しました", "イベント", "キャンペーン")


def strip_dates(text: str) -> str:
    t = NOISE.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" 　-–—/、,，.。・|｜")
    for lab in LABELS:
        if t.startswith(lab + " "):
            t = t[len(lab):].strip()
    for tail in TITLE_TAIL:
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
    return t.strip(" 　-–—/、,，.。・|｜")


def _cut_at_date(text: str) -> str:
    m = DATE_RE.search(text or "")
    head = text[: m.start()] if m else (text or "")
    return strip_dates(head) or strip_dates(text or "")


class Collector:
    name = "base"
    label = ""
    homepage = ""
    enabled = True

    def __init__(self, city_id: str = ""):
        self.city_id = city_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
        self._last = 0.0
        self.errors: list[str] = []

    def get(self, url, **kwargs) -> requests.Response:
        wait = HTTP_DELAY - (_time.time() - self._last)
        if wait > 0:
            _time.sleep(wait)
        resp = self.session.get(url, timeout=HTTP_TIMEOUT, **kwargs)
        self._last = _time.time()
        resp.raise_for_status()
        if resp.encoding in (None, "ISO-8859-1"):
            m = re.search(rb'charset\s*=\s*["\']?([\w\-]+)', resp.content[:3000], re.I)
            if m:
                resp.encoding = m.group(1).decode("ascii", "ignore")
            else:
                resp.encoding = resp.apparent_encoding
        return resp

    def soup(self, url, **kwargs) -> BeautifulSoup:
        return BeautifulSoup(self.get(url, **kwargs).text, "lxml")

    def window(self, anchor: date):
        return anchor - timedelta(days=WINDOW_PAST_DAYS), anchor + timedelta(days=WINDOW_FUTURE_DAYS)

    def collect(self, anchor: date) -> list[Event]:
        raise NotImplementedError

    @staticmethod
    def text(node) -> str:
        if node is None:
            return ""
        return " ".join(node.get_text(" ", strip=True).split())


def _in_window(period, start, end) -> bool:
    return period is not None and period.end.date() >= start and period.start.date() <= end


# ----------------------------------------------------------------------------
# ウォーカープラス（都道府県単位 → 市区町村で絞る）
# ----------------------------------------------------------------------------

class Walkerplus(Collector):
    name = "walkerplus"

    def __init__(self, city_id: str, area: str, filters: list[str]):
        super().__init__(city_id)
        self.area = area
        self.filters = filters
        self.homepage = f"https://www.walkerplus.com/event_list/{area}/"
        self.label = f"ウォーカープラス {C.get(city_id)['pref']}（{C.get(city_id)['ja']}のみ）"
        self.MAX_PAGES = 20

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.homepage if page == 1 else f"{self.homepage}{page}.html"
            try:
                html = self.get(url).text
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                break
            events = []
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
                try:
                    data = json.loads(block)
                except Exception:
                    continue
                items = data if isinstance(data, list) else [data]
                events += [x for x in items if isinstance(x, dict) and x.get("@type") == "Event"]
            if not events:
                break
            for e in events:
                name = (e.get("name") or "").strip()
                if not name or name in seen:
                    continue
                loc = e.get("location") or {}
                addr = loc.get("address") if isinstance(loc, dict) else ""
                if isinstance(addr, dict):
                    addr = "".join(str(addr.get(k, "")) for k in ("addressRegion", "addressLocality", "streetAddress"))
                addr = str(addr or "")
                venue = (loc.get("name") or "") if isinstance(loc, dict) else ""
                if not any(f in addr for f in self.filters) and not any(f in venue for f in self.filters):
                    continue
                sd, ed = e.get("startDate"), e.get("endDate")
                if not sd:
                    continue
                try:
                    p_start = datetime.fromisoformat(str(sd)[:10])
                    p_end = datetime.fromisoformat(str(ed or sd)[:10])
                except Exception:
                    continue
                if p_end.date() < start or p_start.date() > end:
                    continue
                seen.add(name)
                desc = str(e.get("description") or "")[:300]
                out.append(Event(
                    source=self.name, source_key=f"{normalize_title(name)}|{str(sd)[:10]}",
                    title=name[:90], start=p_start, end=p_end, all_day=True,
                    category=classify(name, desc), venue=venue[:40], address=addr[:80],
                    ward=detect_ward(addr, venue, name), city=self.city_id,
                    url=e.get("url") or "", description=desc,
                ))
        return out


# ----------------------------------------------------------------------------
# 通用卡片列表（选择器驱动）
# ----------------------------------------------------------------------------

@dataclass
class CardSpec:
    url: str
    label: str
    item: str
    title: str
    date: str
    venue: str = ""
    link: str = "a[href]"
    link_attr: str = "href"
    pages: int = 1
    page_tpl: str = ""          # e.g. "{url}?page={n}" / "{url}page/{n}/"
    default_venue: str = ""
    default_address: str = ""
    must_contain: str = ""
    cut_title_at_date: bool = True


class CardList(Collector):
    name = "cards"

    def __init__(self, city_id: str, spec: CardSpec, name: str):
        super().__init__(city_id)
        self.spec = spec
        self.name = name
        self.label = spec.label
        self.homepage = spec.url

    def _page_url(self, n: int) -> str:
        if n == 1 or not self.spec.page_tpl:
            return self.spec.url
        return self.spec.page_tpl.format(url=self.spec.url, n=n)

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        spec = self.spec
        for n in range(1, spec.pages + 1):
            url = self._page_url(n)
            try:
                soup = self.soup(url)
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                break
            items = soup.select(spec.item)
            if not items:
                break
            for it in items:
                raw_title = self.text(it.select_one(spec.title))
                date_txt = self.text(it.select_one(spec.date)) if spec.date else ""
                blob = self.text(it)
                if spec.must_contain and spec.must_contain not in blob:
                    continue
                if not raw_title and not date_txt:
                    continue
                period = parse_period(date_txt or raw_title, anchor)
                if period is None or not _in_window(period, start, end):
                    continue
                title = _cut_at_date(raw_title) if spec.cut_title_at_date else raw_title
                title = title or _cut_at_date(blob)[:90]
                if not title:
                    continue
                venue = self.text(it.select_one(spec.venue)) if spec.venue else ""
                venue = re.sub(r"^(会場|エリア)\s*[:：]\s*", "", venue).strip() or spec.default_venue
                a = it.select_one(spec.link)
                href = a.get(spec.link_attr) if a else ""
                key = f"{normalize_title(title)}|{period.start.date()}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    source=self.name, source_key=key, title=title[:90],
                    start=period.start, end=period.end, all_day=period.all_day,
                    category=classify(title, blob), venue=venue[:40],
                    address=spec.default_address or C.get(self.city_id)["ja"],
                    ward=detect_ward(venue, blob) or "", city=self.city_id,
                    url=urljoin(url, href) if href else url,
                    description="" if len(blob) > 400 else blob[:200],
                ))
        return out


# ----------------------------------------------------------------------------
# 东京都官方观光 GO TOKYO（展覧会列表）
# ----------------------------------------------------------------------------

class TokyoGotokyo(Collector):
    name = "gotokyo"
    label = "GO TOKYO 东京都官方观光（展覧会）"
    homepage = "https://www.gotokyo.org/jp/event/index.html"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for box in soup.select(".wrap_exhibition"):
            ttl = box.select_one(".exhibition_ttl .ttl")
            cnt = box.select_one(".exhibition_cnt")
            if not ttl or not cnt:
                continue
            title = self.text(ttl)
            ps = [self.text(p) for p in cnt.find_all("p")]
            date_txt = next((p for p in ps if DATE_RE.search(p)), "")
            venue = self.text(cnt.select_one(".facility"))
            period = parse_period(date_txt, anchor)
            if not title or not _in_window(period, start, end):
                continue
            out.append(Event(
                source=self.name, source_key=f"{normalize_title(title)}|{period.start.date()}",
                title=title[:90], start=period.start, end=period.end, all_day=True,
                category="exhibition", venue=venue[:40],
                address="東京都", ward=detect_ward(venue, title), city=self.city_id,
                url=ttl.get("data-link") or self.homepage,
                price=next((p for p in ps if "料金" in p or "円" in p), "")[:40],
                description=(ps[3] if len(ps) > 3 else "")[:200],
            ))
        return out


# ----------------------------------------------------------------------------
# 奈良国立博物館（公式・開催中/企画展）
# ----------------------------------------------------------------------------

class NaraNarahaku(Collector):
    name = "narahaku"
    label = "奈良国立博物館（公式）"
    homepage = "https://www.narahaku.go.jp/"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for box in soup.select(".slide-item"):
            title = self.text(box.select_one(".c-title"))
            sub = self.text(box.select_one(".sub-title"))
            date_txt = self.text(box.select_one(".c-date"))
            a = box.select_one("a[href]")
            if not title or not date_txt:
                continue
            period = parse_period(date_txt, anchor)
            if period is None:
                continue
            if date_txt.rstrip().endswith(("～", "〜", "~")):
                period.end = period.start + timedelta(days=180)
            if not _in_window(period, start, end):
                continue
            title = f"{title} {sub}".strip()
            out.append(Event(
                source=self.name, source_key=f"{normalize_title(title)}|{period.start.date()}",
                title=title[:90], start=period.start, end=period.end, all_day=True,
                category="exhibition", venue="奈良国立博物館",
                address="奈良市登大路町50", ward="奈良公園・東大寺", city=self.city_id,
                url=urljoin(self.homepage, a.get("href")) if a else self.homepage,
                price="有料" if "有料" in self.text(box) else "",
            ))
        return out


# ----------------------------------------------------------------------------
# 金沢21世紀美術館（公式・展覧会）
# ----------------------------------------------------------------------------

class Kanazawa21(Collector):
    name = "kanazawa21"
    label = "金沢21世紀美術館（公式）"
    homepage = "https://www.kanazawa21.jp/exhibition/"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for art in soup.select("article.panel"):
            title = self.text(art.select_one(".panel__ttl"))
            date_txt = self.text(art.select_one(".panel__date"))
            period = parse_period(date_txt, anchor)
            if not title or not _in_window(period, start, end):
                continue
            a = art.select_one("a[href]")
            out.append(Event(
                source=self.name, source_key=f"{normalize_title(title)}|{period.start.date()}",
                title=title[:90], start=period.start, end=period.end, all_day=True,
                category="exhibition", venue="金沢21世紀美術館",
                address="金沢市広坂1-2-1", ward="21世紀美術館・広坂", city=self.city_id,
                url=urljoin(self.homepage, a.get("href")) if a else self.homepage,
                price="有料" if "有料" in self.text(art) else "",
            ))
        return out


# ----------------------------------------------------------------------------
# 城市 → 采集器
# ----------------------------------------------------------------------------

def _cards(city_id: str) -> list[Collector]:
    kw = C.get(city_id)
    specs = {
        "kyoto": [CardSpec(
            url="https://ja.kyoto.travel/event/", label="京都市公式 京都観光Navi",
            item="li.item", title="h3.tit", date="p.time", link="h3.tit a",
            default_venue="京都市内", default_address="京都市")],
        "kobe": [CardSpec(
            url="https://www.feel-kobe.jp/event/", label="神戸公式観光サイト Feel KOBE",
            item=".c-card", title=".c-text", date=".c-text_caption.date",
            venue=".c-areaItem", link="a[href]",
            default_venue="神戸市内", default_address="神戸市")],
        "sapporo": [CardSpec(
            url="https://www.sapporo.travel/event/", label="札幌観光公式 ようこそさっぽろ",
            item="tr.table-list_row", title="td:nth-of-type(1)", date="td:nth-of-type(2)",
            venue="td:nth-of-type(3)", link="td:nth-of-type(1) a",
            default_venue="札幌市内", default_address="札幌市")],
        "okinawa": [CardSpec(
            url="https://www.okinawastory.jp/event/", label="沖縄観光情報 おきなわ物語",
            item=".os-p-list-cmn__unit", title=".os-c-list-cmn__title",
            date=".os-c-list-cmn__lead", venue=".os-c-list-cmn__disc",
            link=".os-c-list-cmn__canvas-link", default_venue="那覇市内",
            default_address="那覇市", must_contain="")],
        "nagoya": [CardSpec(
            url="https://www.nagoya-info.jp/event/", label="名古屋市観光情報 名古屋コンシェルジュ",
            item=".event-slider-item", title="h4.event-slider-name",
            date="dl.box-spot-now dd:nth-of-type(1)", venue="dl.box-spot-now dd:nth-of-type(2)",
            link="a[href]", default_venue="名古屋市内", default_address="名古屋市")],
        "nagasaki": [CardSpec(
            url="https://www.at-nagasaki.jp/event/", label="長崎市公式観光 travel nagasaki",
            item="li.o-digest--tile__item", title="h2.o-digest--tile__title",
            date="dl.o-digest--list__date", venue=".o-digest--list-location",
            link="a.o-digest--tile__anchor", default_venue="長崎市内", default_address="長崎市")],
        "osaka": [],  # 大阪官网列表为 JS 渲染，未接入；以 walkerplus 为主
        "nara": [],
        "tokyo": [],
        "kanazawa": [],
    }.get(city_id, [])
    return [CardList(city_id, s, name=f"{city_id}-cards{i}") for i, s in enumerate(specs)]


def sources_for(city_id: str) -> list[Collector]:
    kw = C.get(city_id)
    if city_id == "fukuoka":
        return [cls() for cls in fukuoka.ALL]
    if city_id == "yokohama":
        return [cls() for cls in yokohama.ALL]
    out: list[Collector] = [Walkerplus(city_id, kw["walker"], kw["filters"])]
    out += _cards(city_id)
    if city_id == "tokyo":
        out.append(TokyoGotokyo(city_id))
    if city_id == "nara":
        out.append(NaraNarahaku(city_id))
    if city_id == "kanazawa":
        out.append(Kanazawa21(city_id))
    return out
