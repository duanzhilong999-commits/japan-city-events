"""采集器集合：通用抓取 + 各站点解析 + 长浜市场等定期规则。

统一接口：Collector.collect(anchor: date) -> list[Event]
"""

from __future__ import annotations

import json
import re
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .core import (CATEGORIES, CACHE_DIR, HTTP_DELAY, HTTP_TIMEOUT, USER_AGENT, Period,
                   WINDOW_FUTURE_DAYS, WINDOW_PAST_DAYS, Event, classify, detect_ward,
                   normalize_title, parse_period, to_jst_iso)

CITY = "福岡市"
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


def venue_from_address(address: str) -> str:
    """「福岡県福岡市中央区天神1 天神中央公園」→「天神1 天神中央公園」。"""
    a = re.sub(r"^〒?\s*\d{3}-?\d{4}\s*", "", (address or "").strip())
    a = re.sub(r"^(福岡県)?福岡市[\u4e00-\u9faf]+?区", "", a)
    return a.strip()[:40]


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
# 1) よかなび（福岡市公式観光サイト）
# ----------------------------------------------------------------------------
class Yokanavi(Collector):
    name = "yokanavi"
    label = "よかなび（福岡市公式観光）"
    homepage = "https://yokanavi.com/events"

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, 7):
            q = (f"https://yokanavi.com/events?q%5Bevent_terms_end_date_gteq%5D={start.isoformat()}"
                 f"&q%5Bevent_terms_start_date_lteq%5D={end.isoformat()}&page={page}")
            soup = self.soup(q)
            cards = soup.select("div.card-list__item")
            if not cards:
                break
            for card in cards:
                link = card.select_one("a.card-title")
                if not link:
                    continue
                href = urljoin("https://yokanavi.com", link.get("href", ""))
                if href in seen:
                    continue
                seen.add(href)
                try:
                    ev = self._detail(href, self.text(link),
                                      self.text(card.select_one(".card-description")),
                                      self.text(card.select_one(".card-period")), anchor)
                    if ev:
                        out.append(ev)
                except Exception as exc:
                    self.errors.append(f"{self.text(link)[:16]}: {exc}")
        return out

    def _detail(self, url, title, desc, period_txt, anchor):
        soup = self.soup(url)
        full_title = self.text(soup.select_one("h1")) or title
        holding = self.text(soup.select_one(".article-contents-header__holding_date"))
        info = {}
        for item in soup.select(".basic-info-list-item"):
            lab = self.text(item.select_one(".basic-info-list-item__label"))
            val = self.text(item.select_one(".basic-info-list-item__contents"))
            if lab:
                info[lab] = val
        period = parse_period(" ".join(x for x in (holding, info.get("開催期間", ""), period_txt) if x), anchor)
        if not period:
            return None
        venue = info.get("開催場所", "")
        address = ""
        m = re.search(r"[（(]([^）)]*福岡市[^）)]*)[）)]", venue)
        if m:
            address = m.group(1)
            venue = venue[: m.start()].strip() or venue
        if not address:
            m = re.search(r"福岡市[\u4e00-\u9faf]+区[^）) 」]*", venue)
            if m:
                address = m.group(0)
                venue = venue[: m.start()].strip("（( ")
        ward = detect_ward(venue, address, full_title)
        if not ("福岡市" in address or "福岡市" in venue or ward):
            return None
        body = self.text(soup.select_one("article"))[:400]
        img = soup.select_one("meta[property='og:image']")
        return Event(
            source=self.name, source_key=url.rstrip("/").split("/")[-1],
            title=full_title, start=period.start, end=period.end, all_day=period.all_day,
            category=classify(full_title, f"{desc} {body}"),
            tags=re.findall(r"#(\S+)", body)[:6],
            venue=venue, address=address or venue, ward=ward, city=CITY,
            price=info.get("料金", "") or info.get("参加費", ""),
            access=info.get("アクセス", ""), url=url,
            image=img.get("content", "") if img else "",
            description=desc or info.get("イベント詳細", ""), note=period.note,
        )


# ----------------------------------------------------------------------------
# 2) クロスロードふくおか（福岡県公式観光）
# ----------------------------------------------------------------------------
class Crossroad(Collector):
    name = "crossroad"
    label = "クロスロードふくおか（福岡県官方）"
    homepage = "https://www.crossroadfukuoka.jp/event"
    BASE = "https://www.crossroadfukuoka.jp"
    CACHE = CACHE_DIR / "crossroad"

    def __init__(self):
        super().__init__()
        self.CACHE.mkdir(parents=True, exist_ok=True)

    def collect(self, anchor):
        start, end = self.window(anchor)
        cards = []
        for page in range(1, 51):
            soup = self.soup(f"{self.BASE}/event?st=acs&vw=tile&page={page}")
            items = soup.select("li.o-digest--tile__item")
            if not items:
                break
            for it in items:
                a = it.select_one("a.o-digest--tile__anchor")
                if not a:
                    continue
                if "福岡" not in self.text(it.select_one(".o-digest--list-location__label")):
                    continue
                period = parse_period(self.text(it.select_one(".o-digest--list__date dd")), anchor)
                if not _in_window(period, start, end):
                    continue
                cards.append({
                    "url": urljoin(self.BASE, a.get("href", "")),
                    "title": self.text(it.select_one("h2.o-digest--tile__title")),
                    "desc": self.text(it.select_one(".o-digest--tile__description")),
                    "period": period,
                })
            if len(items) < 40:
                break

        out = []
        for c in cards:
            try:
                ev = self._detail(c)
                if ev:
                    out.append(ev)
            except Exception as exc:
                self.errors.append(f"{c['title'][:16]}: {exc}")
        return out

    def _info(self, url):
        key = url.rstrip("/").split("/")[-1]
        f = self.CACHE / f"{key}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        soup = self.soup(url)
        info = {}
        for tr in soup.select("table.o-detail-contents__table tr"):
            th, td = tr.find("th"), tr.find("td")
            if th and td:
                info[self.text(th)] = self.text(td)
        img = soup.select_one("meta[property='og:image']")
        data = {"title": self.text(soup.select_one("h1")),
                "desc": self.text(soup.select_one(".o-detail-contents__description")),
                "info": info,
                "image": img.get("content", "") if img else "",
                "body": self.text(soup.body)[:1200] if soup.body else ""}
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def _detail(self, card):
        data = self._info(card["url"])
        info = data.get("info", {})
        address = info.get("住所", "")
        if "福岡市" not in address:
            return None
        period = parse_period(info.get("開催日", ""), card["period"].start.date()) or card["period"]
        title = data.get("title") or card["title"]
        desc = card["desc"] or data.get("desc", "")
        hours = info.get("営業時間", "")
        extra_note = ""
        if hours:
            if period.all_day and period.start.date() == period.end.date():
                hp = parse_period(f"{period.start.strftime('%Y年%m月%d日')} {hours}", period.start.date())
                if hp and not hp.all_day:
                    period = hp
            else:
                extra_note = f"時間 {hours}"[:60]
        return Event(
            source=self.name, source_key=card["url"].rstrip("/").split("/")[-1],
            title=title, start=period.start, end=period.end, all_day=period.all_day,
            category=classify(title, f"{desc} {data.get('desc', '')} {data.get('body', '')}"),
            venue=info.get("名称", "") or "",
            address=address, ward=detect_ward(address, title, info.get("アクセス情報", "")),
            city=CITY, price=info.get("料金", ""), access=info.get("アクセス情報", ""),
            url=card["url"], image=data.get("image", ""), description=desc,
            note=" ".join(x for x in (period.note, extra_note) if x)[:80],
        )


# ----------------------------------------------------------------------------
# 3) 福岡市美術館
# ----------------------------------------------------------------------------
class ArtMuseum(Collector):
    name = "fukuoka-art-museum"
    label = "福岡市美術館"
    homepage = "https://www.fukuoka-art-museum.jp/exhibition/"
    VENUE = "福岡市美術館"
    ADDRESS = "福岡市中央区大濠公園1-6"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for a in soup.select("a.c-article-list__item"):
            title = self.text(a.select_one("h2.c-article-list__title"))
            date_txt = self.text(a.select_one("p.c-article-list__date"))
            period = parse_period(date_txt, anchor)
            if not title or not _in_window(period, start, end):
                continue
            out.append(Event(
                source=self.name, source_key=urljoin(self.homepage, a.get("href", "")),
                title=title, start=period.start, end=period.end, all_day=period.all_day,
                category="exhibition", venue=self.VENUE, address=self.ADDRESS, ward="中央区",
                city=CITY, url=urljoin(self.homepage, a.get("href", "")),
                note=date_txt,
            ))
        return out


# ----------------------------------------------------------------------------
# 4) 福岡市博物館（特別展：館内リンク＋外部特設サイト）
# ----------------------------------------------------------------------------
class CityMuseum(Collector):
    name = "fukuoka-city-museum"
    label = "福岡市博物館"
    homepage = "https://museum.city.fukuoka.jp/"
    VENUE = "福岡市博物館"
    ADDRESS = "福岡市早良区百道浜2-1-1"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.find_all("a"):
            title = self.text(a)
            href = urljoin(self.homepage, a.get("href", ""))
            if not title or len(title) > 60 or "展" not in title:
                continue
            if href in seen or href.rstrip("/") == self.homepage.rstrip("/"):
                continue
            seen.add(href)
            try:
                ev = self._from_page(href, title, anchor, start, end)
                if ev:
                    out.append(ev)
            except Exception as exc:
                self.errors.append(f"{title[:14]}: {exc}")
        return out

    def _from_page(self, url, title, anchor, start, end):
        soup = self.soup(url)
        text = self.text(soup)
        m = re.search(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日\s*[（(][^）)]*[）)]?\s*[〜～~]\s*"
                      r"(?:20\d{2}年\s*)?\d{1,2}月\s*\d{1,2}日", text)
        if not m:
            return None
        period = parse_period(m.group(0), anchor)
        if not _in_window(period, start, end):
            return None
        return Event(
            source=self.name, source_key=url, title=title[:60],
            start=period.start, end=period.end, all_day=period.all_day,
            category="exhibition", venue=self.VENUE, address=self.ADDRESS, ward="早良区",
            city=CITY, url=url, note=m.group(0),
        )


# ----------------------------------------------------------------------------
# 5) キャナルシティ博多
# ----------------------------------------------------------------------------
class CanalCity(Collector):
    name = "canalcity"
    label = "キャナルシティ博多"
    homepage = "https://canalcity.co.jp/event/"
    VENUE = "キャナルシティ博多"
    ADDRESS = "福岡市博多区住吉1-2"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.select("a[href*='/event/detail/']"):
            date_txt = self.text(a.select_one(".c-post__date"))
            raw = self.text(a.select_one(".c-post__body")) or self.text(a)
            period = parse_period(date_txt, anchor)
            if not _in_window(period, start, end):
                continue
            url = urljoin(self.homepage, a.get("href", ""))
            if url in seen:
                continue
            seen.add(url)
            title = self.clean_title(strip_dates(raw), [date_txt])
            if len(title) < 3:
                continue
            out.append(Event(
                source=self.name, source_key=url.split("/detail/")[-1].split("?")[0],
                title=title, start=period.start, end=period.end, all_day=period.all_day,
                category=classify(title + " " + raw), venue=self.VENUE, address=self.ADDRESS,
                ward="博多区", city=CITY, url=url,
            ))
        return out


# ----------------------------------------------------------------------------
# 6) 福岡タワー
# ----------------------------------------------------------------------------
class FukuokaTower(Collector):
    name = "fukuoka-tower"
    label = "福岡タワー"
    homepage = "https://www.fukuokatower.co.jp/"
    VENUE = "福岡タワー"
    ADDRESS = "福岡市早良区百道浜2-3-26"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.select("a.hover-img-larger"):
            raw = self.text(a)
            dm = re.search(r"20\d{2}年\s*\d{1,2}月\s*\d{1,2}日(?:[（(][^）)]{0,10}[）)])?"
                           r"\s*(?:[〜～~]\s*(?:20\d{2}年\s*)?\d{1,2}月\s*\d{1,2}日(?:[（(][^）)]{0,10}[）)])?)?", raw)
            period = parse_period(dm.group(0), anchor) if dm else None
            if not _in_window(period, start, end):
                continue
            title = self.clean_title(strip_dates(raw), [dm.group(0)])
            if len(title) < 3 or title in seen:
                continue
            seen.add(title)
            url = urljoin(self.homepage, a.get("href", "")) if a.get("href") else self.homepage
            out.append(Event(
                source=self.name, source_key=url + "#" + str(abs(hash(title))),
                title=title, start=period.start, end=period.end, all_day=period.all_day,
                category=classify(title + " " + raw), venue=self.VENUE, address=self.ADDRESS,
                ward="早良区", city=CITY, url=url,
            ))
        return out


# ----------------------------------------------------------------------------
# 7) アクロス福岡
# ----------------------------------------------------------------------------
class Acros(Collector):
    name = "acros"
    label = "アクロス福岡"
    homepage = "https://www.acros.or.jp/"
    VENUE = "アクロス福岡"
    ADDRESS = "福岡市中央区天神1-1-1"
    EXCLUDE = ("九州芸文館", "科学館", "久留米", "北九州")

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.find_all("a"):
            raw = self.text(a)
            if not raw or not DATE_RE.search(raw) or len(raw) > 120:
                continue
            if any(x in raw for x in self.EXCLUDE):
                continue
            dm = re.match(r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日[^0-9]{0,12}?"
                          r"(?:[〜～~]\s*(?:20\d{2}年\s*)?\d{1,2}月\s*\d{1,2}日[^0-9]{0,8}?)?)"
                          r"(?:\s*\d{1,2}:\d{2})?\s*(.*)", raw)
            if not dm:
                continue
            period = parse_period(raw[: dm.end(1) + 6], anchor)
            if not _in_window(period, start, end):
                continue
            title = strip_dates(dm.group(2)) or strip_dates(raw)
            if len(title) < 3 or title in seen:
                continue
            seen.add(title)
            url = urljoin(self.homepage, a.get("href", ""))
            out.append(Event(
                source=self.name, source_key=url + "#" + str(abs(hash(title))),
                title=title, start=period.start, end=period.end, all_day=period.all_day,
                category=classify(title), venue=self.VENUE, address=self.ADDRESS,
                ward="中央区", city=CITY, url=url, note=period.note,
            ))
        return out


# ----------------------------------------------------------------------------
# 8) 博多座
# ----------------------------------------------------------------------------
class Hakataza(Collector):
    name = "hakataza"
    label = "博多座"
    homepage = "https://www.hakataza.co.jp/"
    VENUE = "博多座"
    ADDRESS = "福岡市博多区下川端町4-1"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for slide in soup.select("div.main_slider__slide"):
            img = slide.select_one("a.main_slider__img img")
            title = (img.get("alt", "") if img else "").strip("『』「」 ")
            a = slide.select_one("a.main_slider__img")
            date_txt = self.text(slide.select_one("p.main_slider__date"))
            period = parse_period(date_txt, anchor)
            if not title or not _in_window(period, start, end):
                continue
            url = urljoin(self.homepage, a.get("href", "")) if a else self.homepage
            out.append(Event(
                source=self.name, source_key=url,
                title=title, start=period.start, end=period.end, all_day=period.all_day,
                category="performance", venue=self.VENUE, address=self.ADDRESS, ward="博多区",
                city=CITY, url=url, note=date_txt,
            ))
        return out


# ----------------------------------------------------------------------------
# 9) 福マルシェ（福岡市内10会場の都市型マルシェ）
# ----------------------------------------------------------------------------
class FukuMarche(Collector):
    name = "fuku-marche"
    label = "福マルシェ"
    homepage = "https://fuku-marche.com/"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out = []
        for li in soup.select("li.p-home-schedule__item"):
            try:
                ev = self._item(li, anchor, start, end)
                if ev:
                    out.append(ev)
            except Exception as exc:
                self.errors.append(f"item: {exc}")
        return out

    def _item(self, li, anchor, start, end):
        year_txt = self.text(li.select_one(".p-home-schedule__year"))
        month_txt = re.sub(r"[^0-9]", "", self.text(li.select_one(".p-home-schedule__month")))
        days = [d for d in (self.text(x) for x in li.select(".p-home-schedule__date")) if d.isdigit()]
        if not (year_txt.isdigit() and month_txt and days):
            return None
        y, m = int(year_txt), int(month_txt)
        d1, d2 = int(days[0]), int(days[-1])
        if d2 < d1:
            d2, m2 = d1, m + 1 if m < 12 else 1
        else:
            m2 = m
        start_d = datetime.combine(date(y, m, d1), time(0, 0))
        end_d = datetime.combine(date(y, m2, d2), time(23, 59))
        if end_d.date() < start or start_d.date() > end:
            return None
        tm = self.text(li.select_one(".p-home-schedule__time"))
        t1 = re.match(r"(\d{1,2}):(\d{2})", tm)
        if t1:
            start_d = start_d.replace(hour=int(t1.group(1)), minute=int(t1.group(2)))
            end_d = end_d.replace(hour=23, minute=59)
            all_day = False
        else:
            all_day = True
        venue = self.text(li.select_one(".p-home-schedule__venueName")) or "福マルシェ会場"
        title = self.text(li.select_one(".p-home-schedule__title")) or "福マルシェ"
        address = self.text(li.select_one(".p-home-schedule__address")).replace("住所 …", "").strip()
        link = li.select_one("a.p-home-schedule__link")
        url = urljoin(self.homepage, link.get("href")) if link else self.homepage
        return Event(
            source=self.name, source_key=f"{y}{m:02d}{d1:02d}-{venue}-{title[:10]}",
            title=f"福マルシェ {venue}" if title in ("", "福マルシェ") else f"福マルシェ {venue}：{title}",
            start=start_d, end=end_d, all_day=all_day, category="market",
            venue=venue, address=address, ward=detect_ward(address, venue), city=CITY,
            url=url, note=tm,
        )


# ----------------------------------------------------------------------------
# 10) 定期イベント（公式に日程公开済みのもの）
# ----------------------------------------------------------------------------
class Recurring(Collector):
    """長浜鮮魚市場「市民感謝デー」など、公式日程が確定している定例イベント。"""

    name = "recurring"
    label = "定期イベント（長浜市場など）"
    homepage = "https://www.city.fukuoka.lg.jp/nosui/sengyo/life/sengyosijou.html"

    NAGAHAMA_2026 = [
        date(2026, 4, 11), date(2026, 5, 9), date(2026, 6, 13),
        date(2026, 10, 10), date(2026, 11, 7), date(2026, 12, 12),
    ]
    # 2027 年度（令和9）は未発表のため、同時期の第2土曜で仮置きしない（明示データのみ採用）

    def collect(self, anchor):
        start, end = self.window(anchor)
        out = []
        for d in self.NAGAHAMA_2026:
            if not (start <= d <= end):
                continue
            out.append(Event(
                source=self.name, source_key=f"nagahama-{d.isoformat()}",
                title="長浜鮮魚市場 市民感謝デー（一般開放）",
                start=datetime.combine(d, time(9, 0)),
                end=datetime.combine(d, time(12, 0)),
                all_day=False, category="market",
                venue="長浜鮮魚市場", address="福岡市中央区長浜3-11-3", ward="中央区", city=CITY,
                price="入場無料", url=self.homepage,
                description="鮮魚販売は11:00まで、本マグロ解体ショーは9:30開始。7〜9月と翌年1〜3月は休止。",
                note="9:00–12:00",
                tags=["市場開放日", "市民感謝デー"],
            ))
        return out


# ----------------------------------------------------------------------------
# 10) 福岡市商店街イベント（市公式ページ）
# ----------------------------------------------------------------------------
class Shotengai(Collector):
    name = "shotengai"
    label = "福岡市商店街イベント"
    homepage = "https://www.city.fukuoka.lg.jp/keizai/c-syogyo/business/shotengai2024.html"

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for block in soup.select("div.thumb-inrLM"):
            txt = self.text(block)
            if "日時" not in txt:
                continue
            paras = [self.text(p) for p in block.find_all("p")]
            title = ""
            for p in paras:
                if p and not p.startswith(("日時", "場所", "＞＞", "※")) and len(p) > 3:
                    title = p.strip("◇◆■□●○*＊ ")
                    break
            if not title:
                continue
            dt_m = re.search(r"日時[：:]\s*(.+)", txt)
            place_m = re.search(r"場所[：:]\s*(.+?)(?:\s*＞＞|$)", txt)
            if not dt_m:
                continue
            period = parse_period(dt_m.group(1), anchor)
            if not _in_window(period, start, end):
                continue
            place = place_m.group(1).strip() if place_m else ""
            if "終了" in title or "終了" in place:
                continue
            title = strip_dates(title)
            key = f"{title[:20]}-{period.start.date()}"
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                source=self.name, source_key=key,
                title=title[:80], start=period.start, end=period.end, all_day=period.all_day,
                category=classify(title, txt), venue=place, address=place,
                ward=detect_ward(place, title), city=CITY, url=self.homepage, note=period.note,
            ))
        return out


# ----------------------------------------------------------------------------
# 11) 福岡県イベント一覧（rapinc event-inbox）
# ----------------------------------------------------------------------------
class Rapinc(Collector):
    name = "rapinc"
    label = "福岡県イベント一覧（rapinc）"
    homepage = "https://rapinc.co.jp/event-inbox/area/fukuoka/"
    LIST = "https://rapinc.co.jp/event-inbox/events/?pref=fukuoka&page={page}"

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, 31):
            try:
                soup = self.soup(self.LIST.format(page=page))
            except Exception:
                break
            cards = soup.select("li.event-card")
            if not cards:
                break
            for card in cards:
                a = card.select_one("h2 a")
                if not a:
                    continue
                url = a.get("href", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                try:
                    ev = self._card(card, a, url, anchor, start, end)
                    if ev:
                        out.append(ev)
                except Exception as exc:
                    self.errors.append(f"{self.text(a)[:14]}: {exc}")
        return out

    def _card(self, card, a, url, anchor, start, end):
        title = self.text(a)
        genre = self.text(card.select_one("span.genre"))
        dd = {}
        for dt in card.select("dl dt"):
            nxt = dt.find_next_sibling("dd")
            if nxt is not None:
                dd[self.text(dt)] = self.text(nxt)
        period = parse_period(dd.get("日程", ""), anchor)
        if not _in_window(period, start, end):
            return None
        venue_block = dd.get("会場", "")
        address = ""
        m = re.search(r"福岡[県]?福岡市[^ ]*", venue_block)
        if m:
            address = m.group(0)
        if not address:
            m = re.search(r"福岡市[^ ]*", venue_block)
            address = m.group(0) if m else ""
        if not address:
            return None
        venue = venue_block.replace(address, "").strip() or title
        desc = self.text(card.select_one("p"))
        return Event(
            source=self.name, source_key=url.rstrip("/").split("/")[-1],
            title=title, start=period.start, end=period.end, all_day=period.all_day,
            category=classify(title, f"{genre} {desc}"), tags=[genre] if genre else [],
            venue=venue[:40], address=address, ward=detect_ward(address, venue, title), city=CITY,
            price=dd.get("料金", ""), url=url, description=desc,
            note=dd.get("日程", "")[:60],
        )


# ----------------------------------------------------------------------------
# 12) ウォーカープラス 福岡市イベント（JSON-LD，最稳定）
# ----------------------------------------------------------------------------
class Walkerplus(Collector):
    name = "walkerplus"
    label = "ウォーカープラス 福岡市"
    homepage = "https://www.walkerplus.com/event_list/ar1040130/fukuoka/"
    MAX_PAGES = 8

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.homepage if page == 1 else f"{self.homepage}{page}.html"
            try:
                html = self.get(url).text
            except Exception:
                break
            events = []
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
                try:
                    data = json.loads(block)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("@type") == "Event":
                    events.append(data)
                elif isinstance(data, list):
                    events += [x for x in data if isinstance(x, dict) and x.get("@type") == "Event"]
                elif isinstance(data, dict) and data.get("@graph"):
                    events += [x for x in data["@graph"] if x.get("@type") == "Event"]
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
                if "福岡市" not in addr and "福岡市" not in (loc.get("name") or ""):
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
                venue = loc.get("name") or ""
                desc = str(e.get("description") or "")[:300]
                out.append(Event(
                    source=self.name,
                    source_key=f"{normalize_title(name)}|{str(sd)[:10]}",
                    title=name[:90], start=p_start, end=p_end, all_day=True,
                    category=classify(name, desc),
                    venue=venue[:40], address=addr[:80],
                    ward=detect_ward(addr, venue, name), city=CITY,
                    url=e.get("url") or "", description=desc,
                ))
        return out


# ----------------------------------------------------------------------------
# 13) クリスマスアドベント公式（福岡 各会場 + サーカス）
# ----------------------------------------------------------------------------
class ChristmasAdvent(Collector):
    name = "christmas-advent"
    label = "クリスマスアドベント公式（福岡）"
    homepage = "https://christmas-advent.jp/"
    VENUES = [
        ("https://christmas-advent.jp/fukuoka-hakata/", "JR博多駅前広場", "博多区"),
        ("https://christmas-advent.jp/fukuoka-tenjin/", "福岡市役所西側ふれあい広場", "中央区"),
        ("https://christmas-advent.jp/fukuoka-chuo-koen/", "天神中央公園", "中央区"),
        ("https://christmas-advent.jp/fukuoka-kihinkan/", "貴賓館前（西中洲）", "中央区"),
        ("https://christmas-advent.jp/fukuoka-daimyo/", "福岡大名ガーデンシティ", "中央区"),
        ("https://christmas-advent.jp/fukuoka-passage/", "パサージュ広場", "博多区"),
    ]
    NEWS = "https://christmas-advent.jp/news/reverie2026/"
    PERIOD_RE = re.compile(
        r"(?:20\d{2}年)?\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:[（(][^）)]{0,8}[）)])?\s*[〜～~\-–]\s*"
        r"(?:(20\d{2})年\s*)?(?:(\d{1,2})月\s*)?(\d{1,2})日"
    )
    HOURS_RE = re.compile(r"営業時間\s*(?:平日[:：]?\s*)?(\d{1,2}[:：]\d{2})\s*[-〜～~]\s*(\d{1,2}[:：]\d{2})")

    def _period(self, txt, anchor):
        i = txt.find("開催期間")
        seg = txt[i: i + 160] if i >= 0 else txt
        m = self.PERIOD_RE.search(seg)
        if not m:
            return None
        m1, d1, y2, m2, d2 = m.groups()
        y1 = anchor.year
        if m2 and int(m2) < int(m1):  # 12月 → 1月
            pass
        start = date(y1, int(m1), int(d1))
        end_year = int(y2) if y2 else (y1 + 1 if (m2 and int(m2) < int(m1)) else y1)
        end = date(end_year, int(m2) if m2 else int(m1), int(d2))
        if end.year == y1 and end < start:
            start = start.replace(year=start.year - 1)
        return Period(start=datetime.combine(start, time(0, 0)),
                      end=datetime.combine(end, time(23, 59)), all_day=True,
                      note="")

    def collect(self, anchor):
        start, end = self.window(anchor)
        out = []
        for url, venue, ward in self.VENUES:
            try:
                soup = self.soup(url)
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                continue
            txt = self.text(soup)
            period = self._period(txt, anchor)
            if period is None or not _in_window(period, start, end):
                continue
            hours = self.HOURS_RE.search(txt)
            note = f"{hours.group(1)}–{hours.group(2)}" if hours else ""
            out.append(Event(
                source=self.name, source_key=url.rstrip("/").split("/")[-1],
                title=f"クリスマスアドベント（{venue}）", start=period.start, end=period.end,
                all_day=True, category="market", tags=["クリスマスマーケット"],
                venue=venue, address=f"福岡市{ward}", ward=ward, city=CITY,
                price="入場無料", url=url, description=f"クリスマスマーケット／光のフォレスト 会場: {venue}",
                note=note,
            ))
        # クリスマスサーカス REVERIE
        try:
            soup = self.soup(self.NEWS)
            txt = self.text(soup)
            period = self._period(txt, anchor)
            if period and _in_window(period, start, end):
                out.append(Event(
                    source=self.name, source_key="reverie",
                    title="クリスマスサーカス REVERIE", start=period.start, end=period.end,
                    all_day=True, category="performance", tags=["サーカス", "無料"],
                    venue="天神中央公園 特設ステージ", address="福岡市中央区天神1", ward="中央区",
                    city=CITY, price="観覧無料", url=self.NEWS,
                    description="空中演技・ジャグリング・バトントワリング。1日2回 18:15/20:25（各約30分）、金土休演。",
                ))
        except Exception as exc:
            self.errors.append(f"reverie: {exc}")
        return out


# ----------------------------------------------------------------------------
# 14) 福岡市科学館（開催中・開催予定）
# ----------------------------------------------------------------------------
class ScienceMuseum(Collector):
    name = "science-museum"
    label = "福岡市科学館"
    homepage = "https://www.fukuokacity-kagakukan.jp/activity/inSession.html"
    CATS = ("スペシャル企画", "セミナー・講座", "フィールドワーク", "天体観測会", "サイエンスショー",
            "テーブルサイエンス", "科学実験プログラム", "ものづくりプログラム", "クラブ活動", "イベント")
    PERIOD_RE = re.compile(
        r"(20\d{2})\.(\d{1,2})\.(\d{1,2})\s*(?:[〜～~\-]\s*(?:(20\d{2})\.)?(?:(\d{1,2})\.)?([\d]{1,2}))?")

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.select("a[href]"):
            href = urljoin(self.homepage, a.get("href", ""))
            if "/activity/20" not in href or href in seen:
                continue
            raw = self.text(a)
            m = self.PERIOD_RE.search(raw)
            if not m:
                continue
            y1, m1, d1, y2, m2, d2 = m.groups()
            try:
                p_start = datetime(int(y1), int(m1), int(d1))
                p_end = datetime(int(y2 or y1), int(m2 or m1), int(d2 or d1))
            except ValueError:
                continue
            if p_end.date() < start or p_start.date() > end:
                continue
            head = raw[: m.start()].strip()
            for cat in self.CATS:
                if head.endswith(cat):
                    head = head[: -len(cat)].strip()
            title = re.sub(r"^［[^］]*］\s*", "", head)
            title = re.sub(r"^[・~～\-]\s*", "", title)
            title = re.sub(r"^\d{1,2}/\d{1,2}\s*(?:[（(][^）)]{1,6}[）)])?\s*", "", title).strip()
            title = re.sub(r"[（(][^）)]{0,6}[）)]\s*$", "", title).strip()
            if not title:
                continue
            seen.add(href)
            venue = "六本松421 屋上" if "天体観測" in title else "福岡市科学館"
            out.append(Event(
                source=self.name, source_key=href.rstrip("/").split("/")[-1].split(".")[0],
                title=title[:80], start=p_start, end=p_end, all_day=True,
                category=classify(title, title),
                tags=["科学館"], venue=venue, address="福岡市中央区六本松4-2-1",
                ward="中央区", city=CITY,
                price="", url=href, description=raw[:200],
            ))
        return out


# ----------------------------------------------------------------------------
# 15) 福岡市内的跳蚤市场（Fukuoka City Market，含每週日定期市集）
# ----------------------------------------------------------------------------
class FukuokaCityMarket(Collector):
    name = "fukuoka-city-market"
    label = "Fukuoka City Market（フリマ）"
    homepage = "https://fukuokacitymarket.com/events/"
    FULL_DATE = re.compile(
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:[（(][^）)]{0,10}[）)])?"
        r"(?:\s*[〜～~\-–]\s*(?:(20\d{2})年)?(?:(\d{1,2})月)?(\d{1,2})日(?:[（(][^）)]{0,10}[）)])?)?")
    SHORT = re.compile(r"\d{1,2}/\d{1,2}(?:\s*[〜～~・,、]\s*\d{1,2}/\d{1,2})*")
    WEEKLY = re.compile(r"毎週[月火水木金土日]曜?日?")

    def collect(self, anchor):
        start, end = self.window(anchor)
        soup = self.soup(self.homepage)
        out, seen = [], set()
        for a in soup.select("a[href*='/events/']"):
            href = urljoin(self.homepage, a.get("href", ""))
            if href.rstrip("/") == self.homepage.rstrip("/"):
                continue
            txt = " ".join(a.get_text(" ", strip=True).split())
            m = self.FULL_DATE.search(txt)
            if not m:
                continue
            y1, m1, d1, y2, m2, d2 = m.groups()
            p_start = date(int(y1), int(m1), int(d1))
            if y2 or m2 or d2:
                p_end = date(int(y2 or y1), int(m2 or m1), int(d2 or d1))
            else:
                p_end = p_start
            if p_end < p_start:
                p_end = date(p_end.year + 1, p_end.month, p_end.day)
            venue_m = re.search(r"会場[:：]\s*(.+)$", txt)
            venue = venue_m.group(1).strip() if venue_m else ""
            body = txt[: venue_m.start()] if venue_m else txt
            body = self.FULL_DATE.sub(" ", body)
            body = self.SHORT.sub(" ", body)
            body = re.sub(r"募集中|出店者募集|終了|券売|ロゴ|開催|５?週連続|※[^ ]*|毎週[月火水木金土日]曜?日?|[〜～~\-–]", " ", body)
            title = " ".join(body.split())
            title = re.sub(r"^出店者募集\s*", "", title).strip()
            title = re.sub(r"^.*！\s*", "", title)
            title = re.sub(r"\d{1,2}[:：]\d{2}", " ", title)
            title = re.sub(r"[（(](?:予定|月・祝)[）)]", " ", title)
            title = title.replace("｜", " ").strip(" ・-–—/、,，.。")
            title = " ".join(title.split())
            if len(title) < 4:
                continue
            weekly = bool(self.WEEKLY.search(txt))
            dates = []
            if weekly:
                cur = p_start
                while cur <= p_end:
                    if cur.weekday() == 6:
                        dates.append(cur)
                    cur += timedelta(days=1)
                if not dates:
                    dates = [p_start]
            else:
                dates = [p_start]
            for d in dates:
                if d < start or d > end:
                    continue
                key = f"{href.rstrip('/').split('/')[-1]}-{d}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    source=self.name, source_key=key, title=title[:80],
                    start=datetime.combine(d, time(10, 0)),
                    end=datetime.combine(d, time(17, 0)), all_day=False,
                    category="market", tags=["フリマ", "蚤の市"],
                    venue=venue, address="福岡市", ward=detect_ward(venue, title), city=CITY,
                    price="入場無料", url=href, description=txt[:160],
                ))
        return out


# ----------------------------------------------------------------------------
# 16) 福岡アジア美術館
# ----------------------------------------------------------------------------
class AsiaMuseum(Collector):
    name = "asia-museum"
    label = "福岡アジア美術館"
    homepage = "https://faam.city.fukuoka.lg.jp/exhibition/"
    LISTS = ("https://faam.city.fukuoka.lg.jp/exhibition/",
             "https://faam.city.fukuoka.lg.jp/exhibition/schedule/",
             "https://faam.city.fukuoka.lg.jp/event/",
             "https://faam.city.fukuoka.lg.jp/event/schedule/")
    PERIOD_RE = re.compile(
        r"(20\d{2})\.(\d{1,2})\.(\d{1,2})\s*(?:[（(][^）)]{0,4}[）)])?"
        r"(?:\s*[〜～~]\s*(20\d{2})\.(\d{1,2})\.(\d{1,2}))?")

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for url in self.LISTS:
            try:
                soup = self.soup(url)
            except Exception as exc:
                self.errors.append(f"{url}: {exc}")
                continue
            for a in soup.select("a[href*='/exhibition/'], a[href*='/event/']"):
                href = urljoin(url, a.get("href", ""))
                if not re.search(r"/(?:exhibition|event)/\d+", href) or href in seen:
                    continue
                raw = self.text(a)
                m = self.PERIOD_RE.search(raw)
                if not m:
                    continue
                y1, m1, d1, y2, m2, d2 = m.groups()
                try:
                    p_start = datetime(int(y1), int(m1), int(d1))
                    p_end = datetime(int(y2 or y1), int(m2 or m1), int(d2 or d1), 23, 59)
                except ValueError:
                    continue
                if p_end.date() < start or p_start.date() > end:
                    continue
                title = raw[: m.start()].strip()
                if not title:
                    continue
                seen.add(href)
                out.append(Event(
                    source=self.name, source_key=href.rstrip("/").split("/")[-1],
                    title=title[:90], start=p_start, end=p_end, all_day=True,
                    category="exhibition", tags=["展覧会"],
                    venue="福岡アジア美術館", address="福岡市博多区下川端町3-1 リバレインセンタービル7F",
                    ward="博多区", city=CITY, url=href,
                ))
        return out


# ----------------------------------------------------------------------------
# 17) このへん。（福岡市の地域メディア，日程完備）
# ----------------------------------------------------------------------------
class Konohen(Collector):
    name = "konohen"
    label = "このへん。（福岡市地域メディア）"
    homepage = "https://konohen.media/event/"
    MAX_PAGES = 12
    DATE_RE = re.compile(
        r"^(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?\s*(?:[-–〜～]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?)?")

    def collect(self, anchor):
        start, end = self.window(anchor)
        out, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            url = self.homepage if page == 1 else f"{self.homepage}page/{page}/"
            try:
                soup = self.soup(url)
            except Exception:
                break
            items = soup.select("a[href*='/event/']")
            got = 0
            for a in items:
                href = urljoin(url, a.get("href", ""))
                if not re.search(r"/event/[^/]+", href) or href.rstrip("/") == self.homepage.rstrip("/"):
                    continue
                raw = self.text(a)
                m = self.DATE_RE.match(raw)
                if not m:
                    continue
                y1, m1, d1, h1, mi1, y2, m2, d2, h2, mi2 = m.groups()
                try:
                    p_start = datetime(int(y1), int(m1), int(d1), int(h1 or 0), int(mi1 or 0))
                    p_end = datetime(int(y2 or y1), int(m2 or m1), int(d2 or d1),
                                     int(h2 or 23), int(mi2 or 59))
                except ValueError:
                    continue
                if p_end.date() < start or p_start.date() > end:
                    continue
                rest = raw[m.end():].strip()
                title = re.split(r"\s+(?:scheduled|announced|その他|※)\s*/", rest)[0]
                title = re.sub(r"\s*/\s*$", "", title).strip()
                area_m = re.search(r"/\s*([^/]+?)\s*$", rest)
                area = area_m.group(1).strip() if area_m else ""
                if href in seen or len(title) < 3:
                    continue
                seen.add(href)
                got += 1
                out.append(Event(
                    source=self.name, source_key=href.rstrip("/").split("/")[-1][:60],
                    title=title[:90], start=p_start, end=p_end, all_day=(not h1),
                    category=classify(title), tags=["地域メディア"],
                    venue=area, address=f"福岡市{area}" if area else "福岡市",
                    ward=detect_ward(area, title), city=CITY, url=href,
                ))
            if got == 0:
                break
        return out


class Manual(Collector):
    """手工补录兜底：官方只在 SNS/海报上公布、没有结构化网页的活动。

    数据放在 data/manual_events.json，格式：
    [{"title": "...", "start": "2026-12-03", "end": "2026-12-08",
      "venue": "...", "address": "...", "category": "展览", "price": "", "url": "", "note": ""}]
    """

    name = "manual"
    label = "手工补录（SNS/海报活动）"
    homepage = ""

    PATH = Path(__file__).resolve().parent.parent / "data" / "manual_events.json"

    def collect(self, anchor):
        if not self.PATH.exists():
            return []
        start, end = self.window(anchor)
        data = json.loads(self.PATH.read_text(encoding="utf-8"))
        out = []
        for i, row in enumerate(data):
            try:
                p_start = datetime.strptime(row["start"], "%Y-%m-%d")
                p_end = datetime.strptime(row.get("end") or row["start"], "%Y-%m-%d")
                if p_end < p_start:
                    p_end = p_start
            except Exception:
                continue
            if p_end.date() < start or p_start.date() > end:
                continue
            title = (row.get("title") or "").strip()
            if not title:
                continue
            venue = (row.get("venue") or "").strip()
            addr = (row.get("address") or "福岡市").strip()
            out.append(Event(
                source=self.name, source_key=f"manual-{i}-{p_start.date()}",
                title=title[:90], start=p_start,
                end=p_end.replace(hour=23, minute=59), all_day=True,
                category=row.get("category") or classify(title, ""),
                tags=row.get("tags") or ["手工补录"],
                venue=venue[:40], address=addr[:80],
                ward=detect_ward(addr, venue, title), city=CITY,
                price=row.get("price") or "",
                url=row.get("url") or "", description=row.get("note") or "",
            ))
        return out


ALL = [Yokanavi, Crossroad, Walkerplus, ChristmasAdvent, ScienceMuseum, FukuokaCityMarket,
       AsiaMuseum, Konohen, ArtMuseum, CityMuseum, CanalCity, FukuokaTower, Acros, Hakataza,
       FukuMarche, Shotengai, Rapinc, Recurring, Manual]
