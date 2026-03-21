import requests
import time
import random
import asyncio
import logging
import re
import json
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════

TOKEN = "СЮДА_ВСТАВЬ_ТОКЕН"

CHECK_INTERVAL = 30
IDLE_INTERVAL = 120
HTTP_TIMEOUT = 12

METALLURG_TG = "metallurgmgn"

# ══════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mmg")

# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ КХЛ  (alias → каноническое имя)
# ══════════════════════════════════════════════════════════════

_TEAMS_RAW: List[Tuple[str, List[str], bool]] = [
    ("Металлург Мг", ["металлург", "магнитка", "металлург мг",
                      "металлург магнитогорск", "metallurg", "mmg"], True),
    ("Локомотив",    ["локомотив", "локо", "lokomotiv"], False),
    ("СКА",          ["ска", "ska"], False),
    ("Ак Барс",      ["ак барс", "ак-барс", "ak bars"], False),
    ("Трактор",      ["трактор", "traktor"], False),
    ("Салават Юлаев", ["салават юлаев", "салават", "salavat"], False),
    ("Авангард",     ["авангард", "avangard"], False),
    ("ЦСКА",         ["цска", "cska"], False),
    ("Динамо Минск", ["динамо минск", "динамо мн", "dinamo minsk"], False),
    ("Динамо Москва", ["динамо москва", "динамо м", "dinamo moscow"], False),
    ("Лада",         ["лада", "lada"], False),
    ("Торпедо",      ["торпедо", "torpedo"], False),
    ("Адмирал",      ["адмирал", "admiral"], False),
    ("Северсталь",   ["северсталь", "severstal"], False),
    ("Сибирь",       ["сибирь", "sibir"], False),
    ("Амур",         ["амур", "amur"], False),
    ("Барыс",        ["барыс", "barys"], False),
    ("Автомобилист", ["автомобилист", "авто", "avtomobilist"], False),
    ("Нефтехимик",   ["нефтехимик", "neftekhimik"], False),
    ("Спартак",      ["спартак", "spartak"], False),
    ("Шанхай Дрэгонс", ["шанхай дрэгонс", "шанхай драгонс", "шанхай",
                         "куньлунь", "куньлунь ред стар",
                         "shanghai dragons", "kunlun"], False),
]

ALIAS_MAP: Dict[str, str] = {}
MG_ALIASES: List[str] = []
for _name, _aliases, _is_mg in _TEAMS_RAW:
    for _a in _aliases:
        ALIAS_MAP[_a] = _name
    if _is_mg:
        MG_ALIASES = list(_aliases)

# Сортировка длинные → короткие (чтобы "Динамо Минск" нашёлся раньше "Динамо")
_OPP_SORTED = sorted(
    [(a, n) for a, n in ALIAS_MAP.items() if n != "Металлург Мг"],
    key=lambda x: len(x[0]),
    reverse=True,
)


def is_mg(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(a in low for a in MG_ALIASES)


def find_opponent(text: str) -> Optional[str]:
    low = text.lower()
    for alias, canon in _OPP_SORTED:
        if alias in low:
            return canon
    return None


# ══════════════════════════════════════════════════════════════
#  СЧЁТ — надёжное извлечение (НЕ время суток!)
# ══════════════════════════════════════════════════════════════

def _is_hockey_score(a: int, b: int) -> bool:
    """Хоккейный счёт: 0‑15 каждый."""
    return 0 <= a <= 15 and 0 <= b <= 15


def extract_score(text: str) -> Optional[str]:
    """
    Возвращает хоккейный счёт вида '3:1' или None.
    Фильтрует время суток (19:30), даты, таймеры.
    """
    for m in re.finditer(r'(\d{1,2})\s*[:\-–]\s*(\d{1,2})', text):
        a, b = int(m.group(1)), int(m.group(2))

        # — не хоккейный диапазон
        if not _is_hockey_score(a, b):
            continue

        # — скорее всего время суток (>=13 : XX  или  XX : >=60 бывает редко)
        if a >= 13 and b <= 59:
            continue                       # 19:30  21:00  и т.п.

        # — контекст: если рядом «час / мск / время / начало» → время
        start = max(0, m.start() - 20)
        end = min(len(text), m.end() + 15)
        ctx = text[start:end].lower()
        time_words = ["час", "мск", "мест", "время", "time",
                      "начал", "старт", "pm", "am"]
        if any(w in ctx for w in time_words):
            continue

        # — если после числа «:ХХ» (секунды) — это таймер 19:10:34
        after = text[m.end():m.end() + 4]
        if re.match(r'^:\d{2}', after):
            continue

        return f"{a}:{b}"

    return None


# ══════════════════════════════════════════════════════════════
#  МОДЕЛИ
# ══════════════════════════════════════════════════════════════

@dataclass
class MatchInfo:
    home: str = ""
    away: str = ""
    score: str = ""          # "3:1" или ""
    is_live: bool = False
    period: int = 0
    source: str = ""
    confidence: float = 0.0

    @property
    def has_mg(self) -> bool:
        return is_mg(self.home) or is_mg(self.away)

    @property
    def opponent(self) -> str:
        if is_mg(self.home):
            return self.away
        return self.home


@dataclass
class Penalty:
    team: str
    player: str
    minutes: int
    reason: str
    period: int
    start_ts: float
    active: bool = True

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.minutes * 60

    def remaining(self) -> int:
        return max(0, int(self.end_ts - time.time()))

    def remaining_fmt(self) -> str:
        r = self.remaining()
        if r <= 0:
            return "✅"
        mins, s = divmod(r, 60)
        return f"{mins}:{s:02d}"


@dataclass
class PenTracker:
    home: List[Penalty] = field(default_factory=list)
    away: List[Penalty] = field(default_factory=list)
    seen: set = field(default_factory=set)

    def _expire(self):
        now = time.time()
        for p in self.home + self.away:
            if p.active and now >= p.end_ts:
                p.active = False

    def active_home(self):
        self._expire()
        return [p for p in self.home if p.active]

    def active_away(self):
        self._expire()
        return [p for p in self.away if p.active]

    def h_ice(self):
        return max(3, 5 - len(self.active_home()))

    def a_ice(self):
        return max(3, 5 - len(self.active_away()))

    def strength(self):
        return f"{self.h_ice()} на {self.a_ice()}"

    def pp_home(self):
        return self.h_ice() > self.a_ice()

    def pp_away(self):
        return self.a_ice() > self.h_ice()

    def cancel_minor(self, home_scored: bool):
        lst = self.away if home_scored else self.home
        for p in lst:
            if p.active and p.minutes == 2:
                p.active = False
                break

    def clear(self):
        self.home.clear()
        self.away.clear()
        self.seen.clear()


@dataclass
class State:
    channel_id: Optional[int] = None
    is_live: bool = False
    score: str = ""
    period: int = 0
    home: str = ""
    away: str = ""
    notified_start: bool = False
    notified_end: bool = False
    pens: PenTracker = field(default_factory=PenTracker)
    seen_posts: set = field(default_factory=set)
    last_fwd: int = 0
    fwd_ids: set = field(default_factory=set)

    def reset(self):
        self.is_live = False
        self.score = ""
        self.period = 0
        self.home = ""
        self.away = ""
        self.notified_start = False
        self.notified_end = False
        self.pens.clear()
        self.seen_posts.clear()


ST = State()

# ══════════════════════════════════════════════════════════════
#  ФРАЗЫ
# ══════════════════════════════════════════════════════════════

_GOAL_MG    = ["🥅🔥 МЕТАЛЛУРГ ЗАБИВАЕТ!", "⚡ ГОООЛ!", "🚨 МАГНИТКА!"]
_GOAL_MG_PP = ["🥅⚡ ГОЛ В БОЛЬШИНСТВЕ!"]
_GOAL_MG_SH = ["🥅😱 ГОЛ В МЕНЬШИНСТВЕ!"]
_CONCEDE    = ["😤 Пропустили…", "😔 Гол в наши ворота…"]
_CONCEDE_PP = ["😤 Соперник реализовал большинство…"]
_CONCEDE_SH = ["😱 Соперник забил в меньшинстве!"]
_START      = ["🟢 Матч начался!", "🏒 Погнали, Магнитка!"]
_END        = ["🏁 Матч завершён!", "🔔 Финальная сирена!"]
_PEN_MSG    = ["🟡 Удаление!", "⚠️ Штраф!"]
_PER_NAMES  = {1: "1️⃣ 1-й период", 2: "2️⃣ 2-й период",
               3: "3️⃣ 3-й период", 4: "⏱ Овертайм", 5: "🎯 Буллиты"}

SRC_LABEL = {
    "metallurg": "🌐 metallurg.ru",
    "khl":       "🏒 khl.ru",
    "tg":        "📱 Telegram",
    "vk":        "📘 VK",
    "tv_in":     "📺 ТВ-ИН",
    "otv":       "📺 ОТВ",
}

# ══════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0 Safari/537.36")
_HDR = {"User-Agent": _UA, "Accept-Language": "ru-RU,ru;q=0.9"}


def _get(url: str, hdr: Optional[Dict] = None) -> Optional[str]:
    try:
        r = requests.get(url, headers=hdr or _HDR,
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  УТИЛИТА: текст → MatchInfo
# ══════════════════════════════════════════════════════════════

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _text_is_today(text: str) -> bool:
    now = datetime.now()
    for m in re.finditer(r'(\d{1,2})[\.\/](\d{1,2})[\.\/](\d{2,4})', text):
        try:
            d, mo = int(m.group(1)), int(m.group(2))
            if d != now.day or mo != now.month:
                return False
        except ValueError:
            pass
    return True


def _live_check(text: str) -> bool:
    kw = ["live", "идёт", "идет", "онлайн", "прямой",
          "сейчас", "в эфире", "online"]
    low = text.lower()
    return any(w in low for w in kw)


def _period_check(text: str) -> int:
    low = text.lower()
    m = re.search(r'(\d)\s*[-\s]?(?:период|пер)', low)
    if m:
        return int(m.group(1))
    if "овертайм" in low or "overtime" in low:
        return 4
    if "буллит" in low or "shootout" in low:
        return 5
    return 0


def text_to_match(text: str, source: str) -> Optional[MatchInfo]:
    """
    Строгое извлечение матча Металлурга из произвольного текста.
    """
    if not text:
        return None
    low = text.lower()
    if not any(a in low for a in MG_ALIASES):
        return None
    if not _text_is_today(text):
        return None

    opp = find_opponent(text)
    if not opp:
        return None

    score = extract_score(text)
    live = _live_check(text)
    period = _period_check(text)

    # порядок команд
    mg_pos = min((low.find(a) for a in MG_ALIASES if a in low), default=9999)
    opp_pos = low.find(opp.lower())
    if mg_pos <= opp_pos:
        home, away = "Металлург Мг", opp
    else:
        home, away = opp, "Металлург Мг"

    conf = 0.3
    if score:
        conf += 0.3
    if live:
        conf += 0.2
    conf += 0.1   # за наличие соперника

    return MatchInfo(home=home, away=away, score=score or "",
                     is_live=live, period=period, source=source,
                     confidence=min(1.0, conf))


# ══════════════════════════════════════════════════════════════
#  ПАРСЕРЫ
# ══════════════════════════════════════════════════════════════

def _html_find_matches(html: str, source: str) -> List[MatchInfo]:
    """Ищет матчи Металлурга в HTML по текстовым блокам."""
    soup = BeautifulSoup(html, "html.parser")
    out: List[MatchInfo] = []
    seen: set = set()

    for tag in soup.find_all(
            ["div", "section", "article", "a", "span",
             "td", "li", "p", "h1", "h2", "h3", "h4"]):
        text = tag.get_text(separator=" ", strip=True)
        if not text or len(text) < 5 or len(text) > 600:
            continue
        mi = text_to_match(text, source)
        if mi:
            key = (mi.opponent, mi.score)
            if key not in seen:
                seen.add(key)
                out.append(mi)
    return out


# ─── metallurg.ru ────────────────────────────────────────────

def parse_metallurg() -> List[MatchInfo]:
    res: List[MatchInfo] = []
    for path in ["/", "/matches/", "/schedule/", "/calendar/"]:
        html = _get(f"https://metallurg.ru{path}")
        if html:
            res.extend(_html_find_matches(html, "metallurg"))
    return res


# ─── khl.ru ──────────────────────────────────────────────────

def parse_khl() -> List[MatchInfo]:
    res: List[MatchInfo] = []
    td = _today()
    for url in [
        "https://www.khl.ru/",
        f"https://www.khl.ru/calendar/{td}/",
        "https://www.khl.ru/calendar/",
    ]:
        html = _get(url)
        if html:
            res.extend(_html_find_matches(html, "khl"))
    return res


# ─── Telegram ────────────────────────────────────────────────

def parse_tg() -> Tuple[List[Dict[str, Any]], List[MatchInfo]]:
    html = _get(f"https://t.me/s/{METALLURG_TG}")
    if not html:
        return [], []

    soup = BeautifulSoup(html, "html.parser")
    posts: List[Dict[str, Any]] = []
    matches: List[MatchInfo] = []

    for msg in soup.select(".tgme_widget_message")[-20:]:
        tel = msg.select_one(".tgme_widget_message_text")
        if not tel:
            continue
        text = tel.get_text(separator=" ", strip=True)
        if not text:
            continue

        post: Dict[str, Any] = {"text": text, "source": "tg"}
        link = msg.get("data-post", "")
        if link:
            try:
                post["post_id"] = int(link.split("/")[-1])
            except (ValueError, IndexError):
                pass
        te = msg.select_one("time")
        if te:
            post["dt"] = te.get("datetime", "")
        posts.append(post)

        mi = text_to_match(text, "tg")
        if mi:
            if post.get("dt") and _today() not in post.get("dt", ""):
                mi.confidence = 0.05
            matches.append(mi)

    return posts, matches


# ─── VK ──────────────────────────────────────────────────────

def parse_vk() -> Tuple[List[Dict[str, Any]], List[MatchInfo]]:
    mobile = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 "
                       "like Mac OS X) AppleWebKit/605.1.15"),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    html = _get("https://m.vk.com/hcmetallurg", hdr=mobile)
    if not html:
        return [], []

    soup = BeautifulSoup(html, "html.parser")
    posts: List[Dict[str, Any]] = []
    matches: List[MatchInfo] = []

    for item in soup.select(".wall_item, .post, .wi_body")[:15]:
        tel = item.select_one(".wall_post_text, .pi_text, .wpt")
        if not tel:
            continue
        text = tel.get_text(separator=" ", strip=True)
        if not text:
            continue
        posts.append({"text": text, "source": "vk"})
        mi = text_to_match(text, "vk")
        if mi:
            matches.append(mi)

    return posts, matches


# ─── ТВ-ИН ───────────────────────────────────────────────────

def parse_tv_in() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "stream_url": "", "khl_found": False,
        "text": "", "matches": [],
    }
    html = _get("https://tv-in.ru/translyacyya-on-line.html")
    if not html:
        return out

    soup = BeautifulSoup(html, "html.parser")
    full = soup.get_text(separator="\n")
    lines = [l.strip() for l in full.split("\n") if l.strip()]

    khl_kw = ["кхл", "khl", "хоккей"] + MG_ALIASES
    for i, line in enumerate(lines):
        if any(k in line.lower() for k in khl_kw):
            out["khl_found"] = True
            window = " ".join(lines[max(0, i - 3):i + 4])
            out["text"] += window + " | "
            mi = text_to_match(window, "tv_in")
            if mi:
                mi.is_live = True
                mi.confidence = max(mi.confidence, 0.7)
                out["matches"].append(mi)

    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src", "")
        if src:
            out["stream_url"] = src
            break

    for script in soup.find_all("script"):
        js = script.string or ""
        m = re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', js)
        if m:
            out["stream_url"] = m.group(1)

    return out


# ─── ОТВ ─────────────────────────────────────────────────────

def parse_otv() -> List[MatchInfo]:
    res: List[MatchInfo] = []
    for url in ["https://www.otv.ru/", "https://www.otv.ru/sport/"]:
        html = _get(url)
        if html:
            res.extend(_html_find_matches(html, "otv"))
    return res


# ══════════════════════════════════════════════════════════════
#  ВЕРИФИКАТОР — выбирает лучший матч
# ══════════════════════════════════════════════════════════════

def pick_best(candidates: List[MatchInfo]) -> Optional[MatchInfo]:
    good = [c for c in candidates if c.has_mg and c.confidence >= 0.15]
    if not good:
        return None

    groups: Dict[str, List[MatchInfo]] = {}
    for c in good:
        groups.setdefault(c.opponent or "?", []).append(c)

    def score_group(g: List[MatchInfo]) -> float:
        return (sum(1 for c in g if c.is_live) * 100
                + len(g) * 10
                + sum(c.confidence for c in g) * 5
                + sum(1 for c in g if c.score) * 20)

    best_opp = max(groups, key=lambda k: score_group(groups[k]))
    group = groups[best_opp]
    best = max(group, key=lambda c: (c.is_live, c.confidence, bool(c.score)))
    n_src = len({c.source for c in group})
    best.confidence = min(1.0, best.confidence + n_src * 0.15)

    if best.confidence < 0.3:
        return None
    return best


# ══════════════════════════════════════════════════════════════
#  КОЛЛЕКТОР
# ══════════════════════════════════════════════════════════════

class Collector:
    def __init__(self):
        self.ok: Dict[str, bool] = {}
        self.tg_posts: List[Dict] = []
        self.vk_posts: List[Dict] = []
        self.tv_in_data: Dict[str, Any] = {}
        self.candidates: List[MatchInfo] = []

    async def run(self) -> Optional[MatchInfo]:
        loop = asyncio.get_running_loop()
        self.ok = {}
        self.candidates = []

        # metallurg.ru
        try:
            ms = await asyncio.wait_for(
                loop.run_in_executor(None, parse_metallurg), 20)
            self.candidates.extend(ms)
            self.ok["metallurg"] = bool(ms)
        except Exception:
            self.ok["metallurg"] = False

        # khl.ru
        try:
            ms = await asyncio.wait_for(
                loop.run_in_executor(None, parse_khl), 20)
            self.candidates.extend(ms)
            self.ok["khl"] = bool(ms)
        except Exception:
            self.ok["khl"] = False

        # Telegram
        try:
            posts, ms = await asyncio.wait_for(
                loop.run_in_executor(None, parse_tg), 15)
            self.tg_posts = posts
            self.candidates.extend(ms)
            self.ok["tg"] = bool(posts)
        except Exception:
            self.ok["tg"] = False

        # VK
        try:
            posts, ms = await asyncio.wait_for(
                loop.run_in_executor(None, parse_vk), 15)
            self.vk_posts = posts
            self.candidates.extend(ms)
            self.ok["vk"] = bool(posts)
        except Exception:
            self.ok["vk"] = False

        # ТВ-ИН
        try:
            td = await asyncio.wait_for(
                loop.run_in_executor(None, parse_tv_in), 15)
            self.tv_in_data = td
            self.candidates.extend(td.get("matches", []))
            self.ok["tv_in"] = td.get("khl_found", False)
        except Exception:
            self.ok["tv_in"] = False

        # ОТВ
        try:
            ms = await asyncio.wait_for(
                loop.run_in_executor(None, parse_otv), 15)
            self.candidates.extend(ms)
            self.ok["otv"] = bool(ms)
        except Exception:
            self.ok["otv"] = False

        n = sum(1 for v in self.ok.values() if v)
        log.info("Источники %d/%d  кандидатов %d",
                 n, len(self.ok), len(self.candidates))

        return pick_best(self.candidates)


coll = Collector()

# ══════════════════════════════════════════════════════════════
#  БОТ + DISPATCHER
# ══════════════════════════════════════════════════════════════

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class ChSetup(StatesGroup):
    waiting = State()


# ──── отправка ───────────────────────────────────────────────

async def send(text: str, source: str = ""):
    if not ST.channel_id:
        return
    if source:
        text += f"\n\n<i>📡 {SRC_LABEL.get(source, source)}</i>"
    try:
        await bot.send_message(ST.channel_id, text, parse_mode="HTML")
    except Exception as e:
        log.error("send: %s", e)


# ──── пересылка постов ───────────────────────────────────────

async def forward_posts():
    if not ST.channel_id or not coll.tg_posts:
        return

    if ST.last_fwd == 0:
        mx = max((p.get("post_id", 0) for p in coll.tg_posts), default=0)
        ST.last_fwd = mx
        ST.fwd_ids = {p.get("post_id", 0) for p in coll.tg_posts if p.get("post_id")}
        log.info("FWD init: last=%d", mx)
        return

    for post in coll.tg_posts:
        pid = post.get("post_id", 0)
        if not pid or pid <= ST.last_fwd or pid in ST.fwd_ids:
            continue
        ST.fwd_ids.add(pid)
        ST.last_fwd = max(ST.last_fwd, pid)
        text = post["text"]
        msg = (f"📢 <b>ХК Металлург Мг</b>\n\n{text}\n\n"
               f"<a href='https://t.me/{METALLURG_TG}/{pid}'>📎 Оригинал</a>")
        try:
            await bot.send_message(ST.channel_id, msg, parse_mode="HTML",
                                   disable_web_page_preview=True)
            await asyncio.sleep(1)
        except Exception as e:
            log.error("fwd #%d: %s", pid, e)


# ──── форматирование ─────────────────────────────────────────

def fmt_pens_active() -> str:
    tr = ST.pens
    ha, aa = tr.active_home(), tr.active_away()
    if not ha and not aa:
        return ""
    lines = []
    if ha:
        lines.append(f"\n🟡 <b>{ST.home}</b>:")
        for p in ha:
            lines.append(f"  • {p.player} {p.minutes}м ({p.reason}) [{p.remaining_fmt()}]")
    if aa:
        lines.append(f"\n🟡 <b>{ST.away}</b>:")
        for p in aa:
            lines.append(f"  • {p.player} {p.minutes}м ({p.reason}) [{p.remaining_fmt()}]")
    lines.append(f"\n👥 <b>{tr.strength()}</b>")
    return "\n".join(lines)


# ──── голы ────────────────────────────────────────────────────

async def on_goal(old: str, new: str, source: str):
    try:
        oa, ob = map(int, old.split(":"))
        na, nb = map(int, new.split(":"))
    except ValueError:
        return
    if not _is_hockey_score(na, nb):
        return

    home_scored = na > oa
    scorer = ST.home if home_scored else ST.away
    mg = is_mg(scorer)
    tr = ST.pens
    gt = ""

    if home_scored:
        if tr.pp_home():
            gt = "pp"
        elif tr.pp_away():
            gt = "sh"
    else:
        if tr.pp_away():
            gt = "pp"
        elif tr.pp_home():
            gt = "sh"

    if mg:
        phrases = (_GOAL_MG_PP if gt == "pp"
                   else _GOAL_MG_SH if gt == "sh"
                   else _GOAL_MG)
    else:
        phrases = (_CONCEDE_PP if gt == "pp"
                   else _CONCEDE_SH if gt == "sh"
                   else _CONCEDE)

    if gt == "pp":
        tr.cancel_minor(home_scored)

    parts = [random.choice(phrases), "",
             f"🏒 {ST.home} <b>{new}</b> {ST.away}"]
    pen = fmt_pens_active()
    if pen:
        parts.append(pen)
    await send("\n".join(parts), source)


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ══════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "🏒 <b>Металлург Мг — бот</b>\n\n"
        "📡 metallurg.ru · khl.ru · TG · VK · ТВ-ИН · ОТВ\n"
        "🛡 Верификация матча\n"
        "📢 Пересылка @metallurgmgn\n\n"
        "/setchannel · /status · /score\n"
        "/penalties · /sources · /tv\n"
        "/teams · /forcecheck · /debug · /stop",
        parse_mode="HTML")


@dp.message(Command("setchannel"))
async def cmd_setch(msg: Message, state: FSMContext):
    parts = msg.text.split(maxsplit=1)
    if len(parts) >= 2:
        await _set_ch(msg, parts[1].strip())
        return
    await msg.answer("Отправьте ID канала (напр. <code>-1001234567890</code>)",
                     parse_mode="HTML")
    await state.set_state(ChSetup.waiting)


@dp.message(ChSetup.waiting)
async def on_ch(msg: Message, state: FSMContext):
    await _set_ch(msg, msg.text.strip())
    await state.clear()


async def _set_ch(msg: Message, raw: str):
    try:
        cid = int(raw)
    except ValueError:
        await msg.answer("❌ Числовой ID")
        return
    try:
        chat = await bot.get_chat(cid)
    except Exception as e:
        await msg.answer(f"❌ {e}")
        return
    ST.channel_id = cid
    await msg.answer(f"✅ <b>{chat.title}</b>", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    ch = "—"
    if ST.channel_id:
        try:
            ch = (await bot.get_chat(ST.channel_id)).title
        except Exception:
            ch = str(ST.channel_id)
    a = sum(1 for v in coll.ok.values() if v)
    await msg.answer(
        f"📺 {ch}\n"
        f"🔴 {'Матч идёт' if ST.is_live else 'Нет матча'}\n"
        f"🏠 {ST.home or '—'} vs {ST.away or '—'}\n"
        f"📊 <b>{ST.score or '—'}</b>\n"
        f"📡 {a}/{len(coll.ok) or 1}\n"
        f"📢 Постов: {len(ST.fwd_ids)}",
        parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(msg: Message):
    w = await msg.answer("🔄 …")
    best = await coll.run()
    if not best:
        await w.edit_text("⚠️ Матч не найден")
        return
    live = "🔴 LIVE" if best.is_live else "⚪"
    per = _PER_NAMES.get(best.period, "")
    await w.edit_text(
        f"🏒 <b>{best.home}</b>  {best.score or '—'}  <b>{best.away}</b>\n\n"
        f"{live}  {per}\n"
        f"🛡 {best.confidence:.0%}\n"
        f"📡 {SRC_LABEL.get(best.source, best.source)}",
        parse_mode="HTML")


@dp.message(Command("penalties"))
async def cmd_pen(msg: Message):
    tr = ST.pens
    ap = tr.home + tr.away
    if not ap:
        await msg.answer("🟢 Штрафов нет")
        return
    lines = ["📋 <b>Штрафы:</b>\n"]
    for p in sorted(ap, key=lambda x: x.start_ts):
        ico = "⏳" if p.active else "✅"
        lines.append(f"{ico} {p.team} — {p.player} {p.minutes}м ({p.reason})")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("sources"))
async def cmd_src(msg: Message):
    lines = ["📡 <b>Источники:</b>\n"]
    for sid, ok in coll.ok.items():
        lines.append(f"{'✅' if ok else '❌'} {SRC_LABEL.get(sid, sid)}")
    lines.append(f"\nКандидатов: {len(coll.candidates)}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("tv"))
async def cmd_tv(msg: Message):
    lines = [
        "📺 <b>Трансляции</b>\n",
        "🏒 <a href='https://tv-in.ru/translyacyya-on-line.html'>ТВ-ИН</a> (Магнитогорск)",
        "📺 <a href='https://www.otv.ru/online/'>ОТВ</a> (Челябинск)",
    ]
    td = coll.tv_in_data
    if td.get("khl_found"):
        lines.append(f"\n🏒 КХЛ на ТВ-ИН: {td.get('text', '')[:100]}")
    if td.get("stream_url"):
        lines.append("🔴 Стрим обнаружен")
    await msg.answer("\n".join(lines), parse_mode="HTML",
                     disable_web_page_preview=True)


@dp.message(Command("teams"))
async def cmd_teams(msg: Message):
    lines = ["🏒 <b>Команды КХЛ:</b>\n"]
    for name, aliases, mg in _TEAMS_RAW:
        ico = "⭐" if mg else "🏒"
        lines.append(f"{ico} {name}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_force(msg: Message):
    w = await msg.answer("🔄 …")
    best = await coll.run()
    lines = ["📊 <b>Проверка:</b>\n"]
    for sid, ok in coll.ok.items():
        lines.append(f"{'✅' if ok else '❌'} {SRC_LABEL.get(sid, sid)}")
    lines.append(f"\nКандидатов: {len(coll.candidates)}")
    if best:
        lines.append(f"\n🏒 {best.home} {best.score or '—'} {best.away}")
        lines.append(f"🛡 {best.confidence:.0%}")
    else:
        lines.append("\n⚠️ Не определён")
    await w.edit_text("\n".join(lines), parse_mode="HTML")


@dp.message(Command("debug"))
async def cmd_debug(msg: Message):
    w = await msg.answer("🔧 …")
    best = await coll.run()
    lines = [f"🔧 <b>DEBUG</b> — {len(coll.candidates)} кандидатов\n"]
    for i, c in enumerate(coll.candidates[:20], 1):
        lines.append(
            f"{i}. {c.home} vs {c.away} | "
            f"{'⚽' + c.score if c.score else '—'} | "
            f"{'🔴' if c.is_live else '⚪'} | "
            f"{c.confidence:.0%} | {c.source}")
    if best:
        lines.append(f"\n✅ {best.home} {best.score or '—'} {best.away} "
                     f"({best.confidence:.0%})")
    else:
        lines.append("\n❌ Нет")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"
    await w.edit_text(text, parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    ST.reset()
    await msg.answer("🛑 Сброс")


# ══════════════════════════════════════════════════════════════
#  WATCHER
# ══════════════════════════════════════════════════════════════

async def watcher():
    log.info("Watcher started")
    while True:
        try:
            best = await coll.run()
            await forward_posts()

            if not best or best.confidence < 0.3:
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            ST.home = best.home
            ST.away = best.away

            # начало
            if best.is_live and not ST.is_live:
                ST.is_live = True
                if not ST.notified_start:
                    await send(
                        f"{random.choice(_START)}\n\n"
                        f"🏒 <b>{best.home}</b> vs <b>{best.away}</b>\n"
                        f"🛡 {best.confidence:.0%}",
                        best.source)
                    ST.notified_start = True

            # период
            if best.period and best.period != ST.period:
                pn = _PER_NAMES.get(best.period, f"Период {best.period}")
                await send(f"{pn}\n🏒 {best.home} <b>{best.score or '—'}</b> {best.away}",
                          best.source)
                ST.period = best.period

            # счёт
            if best.score and ST.score and best.score != ST.score:
                await on_goal(ST.score, best.score, best.source)

            # конец
            if not best.is_live and ST.is_live and best.score:
                if not ST.notified_end:
                    await send(
                        f"{random.choice(_END)}\n\n"
                        f"🏒 <b>{best.home}</b> {best.score} <b>{best.away}</b>",
                        best.source)
                    ST.notified_end = True
                    await asyncio.sleep(300)
                    ST.reset()
                    continue

            if best.score:
                ST.score = best.score

        except Exception as e:
            log.exception("watcher: %s", e)

        await asyncio.sleep(CHECK_INTERVAL if ST.is_live else IDLE_INTERVAL)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    me = await bot.get_me()
    log.info("Bot @%s started  |  %d teams  |  %d aliases",
             me.username, len(_TEAMS_RAW), len(ALIAS_MAP))

    task = asyncio.create_task(watcher())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
