"""采集器集合（横浜版）：只抓横浜本地来源。

统一接口：Collector.collect(anchor: date) -> list[Event]
数据源（全部为横浜本地，与福冈项目无重叠）：
  1. walkerplus      ウォーカープラス 神奈川県页（仅收横浜市）
  2. city-event      横浜市公式「市民利用施設等イベント情報」
  3. akarenga        横浜赤レンガ倉庫
  4. art-museum      横浜美術館
  5. kanagawa-kankou 観光かながわNOW（神奈川県観光，仅收横浜）
  6. sankeien        三溪園
"""

from __future__ import annotations

import json
import re
import time as _time
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .core import (HTTP_DELAY, HTTP_TIMEOUT, USER_AGENT, Period,
                   WINDOW_FUTURE_DAYS, WINDOW_PAST_DAYS, Event, classify, detect_ward,
                   normalize_title, parse_period)

CITY = "横浜市"
DATE_RE = re.compile(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日")
NOISE = re.compile(
    r"(?:20\d{2}年\s*)?\d{1,2}月\s*\d{1,2}日\s*(?:[（(][^）)]{0,8}[）)])?"
    r"|20\d{2}\s*[./]\s*\d{1,2}\s*[./]\s*\d{1,2}(?:\s*[（(][^）)]{0,8}[）)])?"
    r"|\d{1,2}\s*\.\s*\d{1,2}(?:\s*[（(][^）)]{0,8}[）)])?"
    r"|\d{1,2}:\d{2}(?:\s*[〜～~-]\s*\d{1,2}:\d{2})?"
    r"|[〜～~]\s*|\s*[（(][^）)]{0,6}[）)]"
)
LABELS = ("その他", "キャンペーン", "イベント", "ステージ", "ショー", "ワークショップ", "グルメ",
          "ショッピング", "New", "NEW", "お知らせ", "セール", "特別展", "企画展", "コレクション展", "常設展")
TITLE_TAIL = ("が開催されます！", "が開催されます", "が開催されました！", "が開催されました",
              "を開催します！", "を開催します", "【終了しました】", "終了しました", "イベント", "キャンペーン")


def strip_dates(text: str) -> str:
    """日付・曜日・時刻・範囲記号を取り除いたタイトル片を作る。"""
    t = NOISE.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" 　-–—/、,，.。・|｜")
    for lab in LABELS:
        if t.startswith(lab + " "):
            t = t[len(lab):].strip()
    for tail in TITLE_TAIL:
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
    return t.strip(" 　-–—/、,，.。・|｜")


class Collector:
    name = "base"
    label = ""
    homepage = ""
    enabled = True

    def __init__(self):
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

    @staticmethod
    def clean_title(raw: str, remove: list[str] | None = None) -> str:
        t = " ".join((raw or "").split())
        for r in remove or []:
            t = t.replace(r, " ")
        for lab in LABELS:
            if t.startswith(lab + " "):
                t = t[len(lab):].strip()
        return " ".join(t.split())


def _in_window(period, start, end) -> bool:
    return period is not None and period.end.date() >= start and period.start.date() <= end


# ----------------------------------------------------------------------------
# 1) ウォーカープラス 神奈川県（横浜市のみ採用）
# ----------------------------------------------------------------------------

class WalkerplusKanagawa(Collector):
    name = "walkerplus"
    label = "ウォーカープラス 神奈川県（横浜市のみ）"
    homepage = "https://www.walkerplus.com/event_list/ar0314/"
    MAX_PAGES = 20

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
                if "横浜市" not in addr and "横浜" not in venue:
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
                    category=classify(name, desc),
                    venue=venue[:40], address=addr[:80],
                    ward=detect_ward(addr, venue, name), city=CITY,
                    url=e.get("url") or "", description=desc,
                ))
        return out


# ----------------------------------------------------------------------------
# 2) 横浜市公式 市民利用施設等イベント情報
# ----------------------------------------------------------------------------

class YokohamaCityEvent(Collector):
    name = "city-event"
    label = "横浜市公式 市民利用施設イベント"
    homepage = "https://cgi.city.yokohama.lg.jp/common/event2/event_list.html"
    PER_PAGE = 20
    MAX_PAGES = 30

    def collect(self, anchor):
        start, end = self.window(anchor)
        out = []
        for page in range(self.MAX_PAGES):
            params = {
                "txt": "", "canparticipate": "", "railway_id": "",
                "term_after": (anchor - timedelta(days=7)).isoformat(),
                "term_before": (anchor + timedelta(days=180)).isoformat(),
                "term_is_application": "0", "selectiongroups": "",
                "num": str(self.PER_PAGE), "pos": str(page * self.PER_PAGE),
                "ie": "UTF-8",
            }
            try:
                resp = self.session.get(self.homepage, params=params, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
            except Exception as exc:
                self.errors.append(f"page {page}: {exc}")
                break
            if resp.encoding in (None, "ISO-8859-1"):
                resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            items = soup.select("ul.result > li")
            if not items:
                break
            for li in items:
                a = li.select_one("a.event")
                if not a:
                    continue
                title = self.text(a)
                raw_date = self.text(li.select_one("p.date"))
                if not title or not raw_date:
                    continue
                period = None
                m = re.search(
                    r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日[^0-9]{0,6}?から\s*"
                    r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", raw_date)
                if m:
                    y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
                    period = Period(datetime(y1, m1, d1), datetime(y2, m2, d2, 23, 59), True)
                else:
                    m2 = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", raw_date)
                    if m2:
                        y, mo, d = (int(x) for x in m2.groups())
                        t = re.search(r"(\d{1,2})[:：](\d{2})\s*[〜～~-]\s*(\d{1,2})[:：](\d{2})", raw_date)
                        if t:
                            period = Period(datetime(y, mo, d, int(t.group(1)), int(t.group(2))),
                                            datetime(y, mo, d, int(t.group(3)), int(t.group(4))), False)
                        else:
                            period = Period(datetime(y, mo, d), datetime(y, mo, d), True)
                if period is None or not _in_window(period, start, end):
                    continue
                venue = ""
                place = li.select_one("p.place")
                if place:
                    venue = re.sub(r"^場所:\s*", "", self.text(place)).split("(")[0].strip()
                href = urljoin(self.homepage, a.get("href", ""))
                out.append(Event(
                    source=self.name, source_key=href[-30:] or title,
                    title=title[:90],
                    start=period.start, end=period.end,
                    all_day=period.start.time() == time(0, 0) and period.end.time() == time(0, 0),
                    category=classify(title), venue=venue[:40],
                    address="横浜市", ward=detect_ward(venue, title), city=CITY, url=href,
                ))
        return out


# ----------------------------------------------------------------------------
# 3) 横浜赤レンガ倉庫
# ----------------------------------------------------------------------------

class Akarenga(Collector):
    name = "akarenga"
    label = "横浜赤レンガ倉庫"
    homepage = "https://www.yokohama-akarenga.jp/event/"
    MAX_PAGES = 1

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.homepage if page == 1 else f"{self.homepage}page/{page}/"
            try:
                soup = self.soup(url)
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                break
            cards = soup.select("li.l-card_item")
            if not cards:
                break
            for c in cards:
                title = self.text(c.select_one(".l-card_title"))
                raw_date = c.get("data-event-date") or self.text(c.select_one(".l-card_date"))
                if not title or not raw_date:
                    continue
                period = parse_period(raw_date, anchor)
                if not _in_window(period, start, end):
                    continue
                key = f"{normalize_title(title)}|{period.start.date()}"
                if key in seen:
                    continue
                seen.add(key)
                desc = self.text(c.select_one(".l-card_description"))
                venue = "横浜赤レンガ倉庫"
                tags = [self.text(t) for t in c.select(".l-card_tags li")]
                link = c.select_one("a[href]")
                out.append(Event(
                    source=self.name, source_key=key, title=title[:90],
                    start=period.start, end=period.end, all_day=True,
                    category=classify(title, desc + " " + " ".join(tags)),
                    venue=venue, address="横浜市中区新港1-1 赤レンガ倉庫",
                    ward="赤レンガ・新港", city=CITY,
                    url=urljoin(url, link.get("href")) if link else url,
                    description=desc[:200],
                ))
        return out


# ----------------------------------------------------------------------------
# 4) 横浜美術館
# ----------------------------------------------------------------------------

class YokohamaArtMuseum(Collector):
    name = "art-museum"
    label = "横浜美術館"
    homepage = "https://yokohama.art.museum/exhibition/"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for box in soup.select(".exhibitionMain__set"):
            a = box.select_one("h3 a")
            date_txt = self.text(box.select_one(".dateTxt"))
            if not a or not date_txt:
                continue
            title = self.text(a)
            period = parse_period(date_txt, anchor)
            if not _in_window(period, start, end):
                continue
            out.append(Event(
                source=self.name, source_key=f"{normalize_title(title)}|{period.start.date()}",
                title=title[:90], start=period.start, end=period.end, all_day=True,
                category="exhibition",
                venue="横浜美術館", address="横浜市西区みなとみらい3-4-1",
                ward="西区", city=CITY, url=urljoin(self.homepage, a.get("href", "")),
            ))
        return out


# ----------------------------------------------------------------------------
# 5) 観光かながわNOW（神奈川県観光サイト，横浜のみ採用）
# ----------------------------------------------------------------------------

class KanagawaKankou(Collector):
    name = "kanagawa-kankou"
    label = "観光かながわNOW（横浜のみ）"
    homepage = "https://www.kanagawa-kankou.or.jp/event/"
    MAX_PAGES = 8

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.homepage if page == 1 else f"{self.homepage}?page={page}"
            try:
                soup = self.soup(url)
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                break
            tiles = soup.select(".post-tile-block")
            if not tiles:
                break
            for t in tiles:
                a = t.select_one("a[href*='/event/']")
                img = t.select_one("img[alt]")
                title = self.text(t.select_one(".post-title")) or (img.get("alt") if img else "")
                date_txt = self.text(t.select_one(".post-date"))
                blob = self.text(t)
                if not title or not date_txt:
                    continue
                if "横浜" not in blob and "よこはま" not in blob:
                    continue
                period = parse_period(date_txt, anchor)
                if not _in_window(period, start, end):
                    continue
                key = f"{normalize_title(title)}|{period.start.date()}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    source=self.name, source_key=key, title=title[:90],
                    start=period.start, end=period.end, all_day=True,
                    category=classify(title, blob), venue="",
                    address="横浜市", ward=detect_ward(blob), city=CITY,
                    url=urljoin(self.homepage, a.get("href", "")) if a else url,
                ))
        return out


# ----------------------------------------------------------------------------
# 6) 三溪園
# ----------------------------------------------------------------------------

class Sankeien(Collector):
    name = "sankeien"
    label = "横浜 三溪園"
    homepage = "https://www.sankeien.or.jp/event/"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for li in soup.select("li.eventBox__lst__item"):
            a = li.select_one("a[href]")
            title = self.text(li.select_one(".eventBox__lst__item__tit"))
            sub = self.text(li.select_one(".subtit"))
            if not title or not sub:
                continue
            period = parse_period(sub, anchor)
            if not _in_window(period, start, end):
                continue
            out.append(Event(
                source=self.name, source_key=f"{normalize_title(title)}|{period.start.date()}",
                title=title[:90], start=period.start, end=period.end, all_day=True,
                category=classify(title, sub),
                venue="三溪園", address="横浜市中区本牧三之谷58-1",
                ward="中区", city=CITY,
                url=urljoin(self.homepage, a.get("href", "")) if a else self.homepage,
            ))
        return out


ALL = [WalkerplusKanagawa, YokohamaCityEvent, Akarenga, YokohamaArtMuseum, KanagawaKankou, Sankeien]
