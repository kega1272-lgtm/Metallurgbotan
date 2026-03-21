import requests
import time
import random
import asyncio
import logging
import re
import json
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─────────────────────────── НАСТРОЙКИ ───────────────────────────

TOKEN = "ТВОЙ_ТОКЕН"

CHECK_INTERVAL = 25
IDLE_INTERVAL = 90
REQUEST_TIMEOUT = 15
SCORE_CONFIRM_THRESHOLD = 2   # матч подтверждается минимум 2 источниками

METALLURG_TG_CHANNEL_ID = -1001126797283
METALLURG_TG_USERNAME = "metallurgmgn"

# ─────────────────────────── КОМАНДЫ КХЛ ─────────────────────────

KHL_TEAMS_DATA: List[Dict[str, Any]] = [
    {"name": "Металлург Мг",      "aliases": ["металлург", "магнитка", "металлург мг",
                                               "металлург магнитогорск", "магнитогорский металлург",
                                               "metallurg", "mmg"],
                                   "city": "Магнитогорск", "is_mg": True},
    {"name": "Локомотив",         "aliases": ["локомотив", "локо", "lokomotiv"],
                                   "city": "Ярославль"},
    {"name": "СКА",               "aliases": ["ска", "ska"],
                                   "city": "Санкт-Петербург"},
    {"name": "Ак Барс",           "aliases": ["ак барс", "акбарс", "ak bars"],
                                   "city": "Казань"},
    {"name": "Трактор",           "aliases": ["трактор", "traktor"],
                                   "city": "Челябинск"},
    {"name": "Салават Юлаев",     "aliases": ["салават юлаев", "салават", "salavat yulaev", "salavat"],
                                   "city": "Уфа"},
    {"name": "Авангард",          "aliases": ["авангард", "avangard"],
                                   "city": "Омск"},
    {"name": "ЦСКА",              "aliases": ["цска", "cska"],
                                   "city": "Москва"},
    {"name": "Динамо Минск",      "aliases": ["динамо минск", "динамо мн", "dinamo minsk"],
                                   "city": "Минск"},
    {"name": "Динамо Москва",     "aliases": ["динамо москва", "динамо м", "динамо мск",
                                               "dinamo moscow", "dinamo moscow"],
                                   "city": "Москва"},
    {"name": "Лада",              "aliases": ["лада", "lada"],
                                   "city": "Тольятти"},
    {"name": "Торпедо",           "aliases": ["торпедо", "torpedo"],
                                   "city": "Нижний Новгород"},
    {"name": "Адмирал",           "aliases": ["адмирал", "admiral"],
                                   "city": "Владивосток"},
    {"name": "Северсталь",        "aliases": ["северсталь", "severstal"],
                                   "city": "Череповец"},
    {"name": "Сибирь",            "aliases": ["сибирь", "sibir"],
                                   "city": "Новосибирск"},
    {"name": "Амур",              "aliases": ["амур", "amur"],
                                   "city": "Хабаровск"},
    {"name": "Барыс",             "aliases": ["барыс", "barys"],
                                   "city": "Астана"},
    {"name": "Автомобилист",      "aliases": ["автомобилист", "авто", "avtomobilist"],
                                   "city": "Екатеринбург"},
    {"name": "Нефтехимик",        "aliases": ["нефтехимик", "neftekhimik"],
                                   "city": "Нижнекамск"},
    {"name": "Спартак",           "aliases": ["спартак", "spartak"],
                                   "city": "Москва"},
    {"name": "Шанхай Дрэгонс",   "aliases": ["шанхай дрэгонс", "шанхай драгонс", "шанхай",
                                               "куньлунь", "куньлунь ред стар",
                                               "shanghai dragons", "kunlun red star",
                                               "kunlun", "shanghai"],
                                   "city": "Шанхай"},
]

# Быстрый lookup
ALL_TEAM_ALIASES: Dict[str, str] = {}          # alias → canonical name
MG_ALIASES: List[str] = []

for _td in KHL_TEAMS_DATA:
    for _a in _td["aliases"]:
        ALL_TEAM_ALIASES[_a.lower()] = _td["name"]
    if _td.get("is_mg"):
        MG_ALIASES = [a.lower() for a in _td["aliases"]]

# ─────────────────────────── ЛОГИРОВАНИЕ ─────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────── БОТ ─────────────────────────────────

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ─────────────────────────── FSM ─────────────────────────────────

class ChannelSetup(StatesGroup):
    waiting_id = State()

# ─────────────────────────── УТИЛИТЫ КОМАНД ──────────────────────

def is_mg(name: str) -> bool:
    if not name:
        return False
    low = name.lower().strip()
    return any(a in low for a in MG_ALIASES)


def canonical_team(raw: str) -> Optional[str]:
    """Возвращает каноническое имя команды или None."""
    if not raw:
        return None
    low = raw.lower().strip()
    # Точное совпадение
    if low in ALL_TEAM_ALIASES:
        return ALL_TEAM_ALIASES[low]
    # Содержит
    for alias, canon in ALL_TEAM_ALIASES.items():
        if alias in low or low in alias:
            return canon
    return None


def find_opponent_in_text(text: str) -> Optional[str]:
    """Ищет НЕ-Металлург команду КХЛ."""
    if not text:
        return None
    low = text.lower()
    # Сортируем по длине alias desc — чтобы «Динамо Москва» нашлось раньше «Динамо»
    sorted_aliases = sorted(ALL_TEAM_ALIASES.keys(), key=len, reverse=True)
    for alias in sorted_aliases:
        if alias in low:
            canon = ALL_TEAM_ALIASES[alias]
            if not is_mg(canon):
                return canon
    return None


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────── МОДЕЛИ ──────────────────────────────

@dataclass
class MatchCandidate:
    """Кандидат на матч, собранный из одного источника."""
    home: str
    away: str
    score: str                    # "3:1" или ""
    is_live: bool
    period: int
    source: str                   # id источника
    confidence: float = 0.5       # 0..1
    timestamp: float = field(default_factory=time.time)

    @property
    def has_mg(self) -> bool:
        return is_mg(self.home) or is_mg(self.away)

    @property
    def opponent(self) -> str:
        if is_mg(self.home):
            return self.away
        if is_mg(self.away):
            return self.home
        return ""


@dataclass
class Penalty:
    team: str
    player: str
    minutes: int
    reason: str
    period: int
    game_time: str
    start_ts: float
    active: bool = True

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.minutes * 60

    def remaining(self) -> int:
        return max(0, int(self.end_ts - time.time()))

    def remaining_str(self) -> str:
        r = self.remaining()
        if r <= 0:
            return "завершён"
        m, s = divmod(r, 60)
        return f"{m}:{s:02d}"


@dataclass
class PenaltyTracker:
    home: List[Penalty] = field(default_factory=list)
    away: List[Penalty] = field(default_factory=list)
    seen: set = field(default_factory=set)

    def add(self, team, player, minutes, reason, period, gtime, is_home) -> Penalty:
        p = Penalty(team=team, player=player, minutes=minutes,
                    reason=reason, period=period, game_time=gtime,
                    start_ts=time.time())
        (self.home if is_home else self.away).append(p)
        return p

    def _expire(self):
        now = time.time()
        for p in self.home + self.away:
            if p.active and now >= p.end_ts:
                p.active = False

    def active_home(self):
        self._expire(); return [p for p in self.home if p.active]
    def active_away(self):
        self._expire(); return [p for p in self.away if p.active]
    def home_on_ice(self): return max(3, 5 - len(self.active_home()))
    def away_on_ice(self): return max(3, 5 - len(self.active_away()))
    def strength(self): return f"{self.home_on_ice()} на {self.away_on_ice()}"
    def pp_home(self): return self.home_on_ice() > self.away_on_ice()
    def pp_away(self): return self.away_on_ice() > self.home_on_ice()
    def equal(self): return self.home_on_ice() == self.away_on_ice()

    def cancel_minor(self, home_scored):
        for p in (self.away if home_scored else self.home):
            if p.active and p.minutes == 2:
                p.active = False
                break

    def all_sorted(self):
        c = self.home + self.away
        c.sort(key=lambda x: x.start_ts)
        return c

    def clear(self):
        self.home.clear(); self.away.clear(); self.seen.clear()


@dataclass
class BotState:
    channel_id: Optional[int] = None
    is_live: bool = False
    score: str = ""
    period: int = 0
    home_team: str = ""
    away_team: str = ""
    notified_start: bool = False
    notified_end: bool = False
    penalties: PenaltyTracker = field(default_factory=PenaltyTracker)
    pp_goals_h: int = 0
    pp_goals_a: int = 0
    sh_goals_h: int = 0
    sh_goals_a: int = 0
    seen_posts: set = field(default_factory=set)
    last_forwarded_post_id: int = 0
    forwarded_post_ids: set = field(default_factory=set)
    # Верификация матча
    confirmed_opponent: str = ""
    confirmation_count: int = 0
    # ТВ-ИН трансляция
    tv_in_stream_url: str = ""
    tv_in_watching: bool = False

    def reset_match(self):
        self.is_live = False
        self.score = ""
        self.period = 0
        self.home_team = ""
        self.away_team = ""
        self.notified_start = False
        self.notified_end = False
        self.penalties.clear()
        self.pp_goals_h = 0
        self.pp_goals_a = 0
        self.sh_goals_h = 0
        self.sh_goals_a = 0
        self.seen_posts.clear()
        self.confirmed_opponent = ""
        self.confirmation_count = 0
        self.tv_in_stream_url = ""
        self.tv_in_watching = False


S = BotState()

# ─────────────────────────── ТЕКСТЫ ──────────────────────────────

GOAL_MG = ["🥅🔥 МЕТАЛЛУРГ ЗАБИВАЕТ!", "⚡ ГОООЛ МЕТАЛЛУРГА!", "🚨 МАГНИТКА ЗАБИВАЕТ!"]
GOAL_MG_PP = ["🥅🔥⚡ ГОЛ В БОЛЬШИНСТВЕ!", "💪🚨 БОЛЬШИНСТВО РЕАЛИЗОВАНО!"]
GOAL_MG_SH = ["🥅😱 ГОЛ В МЕНЬШИНСТВЕ!", "🔥🛡 НЕВЕРОЯТНЫЙ ГОЛ В МЕНЬШИНСТВЕ!"]
CONCEDE = ["😤 Пропустили...", "😔 Гол в наши ворота..."]
CONCEDE_PP = ["😤 Соперник реализовал большинство..."]
CONCEDE_SH = ["😱 Соперник забил в меньшинстве!"]
START = ["🟢 Матч начался!", "🏒 Погнали, Магнитка!", "🏟 Шайба вброшена!"]
END = ["🏁 Матч завершён!", "🔔 Финальная сирена!"]
PEN = ["🟡 Удаление!", "⚠️ Штраф!"]
PERIODS = {1: "1️⃣ Первый период", 2: "2️⃣ Второй период",
           3: "3️⃣ Третий период", 4: "⏱ Овертайм!", 5: "🎯 Буллиты!"}

# ─────────────────────────── HTTP ────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def fetch_page(url: str, headers: Optional[Dict] = None) -> Optional[str]:
    try:
        h = headers or HEADERS
        resp = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception as e:
        logger.debug("fetch %s: %s", url, e)
        return None


def fetch_json(url: str, headers: Optional[Dict] = None) -> Optional[Any]:
    try:
        h = headers or HEADERS
        resp = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# ██  ВАЛИДАЦИЯ МАТЧА (борьба с ложными срабатываниями)
# ═══════════════════════════════════════════════════════════════════

def validate_match_date(text: str) -> bool:
    """Проверяет, относится ли текст к сегодняшнему дню."""
    now = datetime.now()
    today_patterns = [
        now.strftime("%d.%m.%Y"),
        now.strftime("%d.%m.%y"),
        now.strftime("%Y-%m-%d"),
        now.strftime("%d %B"),
        "сегодня", "today",
    ]
    low = text.lower()
    # Если есть явная дата прошлого/будущего — плохо
    date_m = re.search(r'(\d{1,2})[\.\/](\d{1,2})[\.\/](\d{2,4})', text)
    if date_m:
        try:
            d, m = int(date_m.group(1)), int(date_m.group(2))
            if d != now.day or m != now.month:
                return False
        except ValueError:
            pass
    return True


def is_today_match(candidate: MatchCandidate) -> bool:
    """Быстрая проверка что кандидат — сегодняшний матч."""
    if candidate.is_live:
        return True
    if candidate.score and candidate.score != "0:0":
        # Со счётом, но не live — может быть результат прошлого матча
        return candidate.confidence >= 0.6
    return True


class MatchVerifier:
    """
    Верифицирует матч: несколько источников должны согласовываться
    по сопернику и статусу.
    """

    def __init__(self):
        self.candidates: List[MatchCandidate] = []
        self._last_verified: Optional[MatchCandidate] = None

    def add(self, c: MatchCandidate):
        if c.has_mg:
            self.candidates.append(c)

    def clear(self):
        self.candidates.clear()
        self._last_verified = None

    def best(self) -> Optional[MatchCandidate]:
        """Возвращает лучший верифицированный кандидат или None."""
        if not self.candidates:
            return None

        # Группируем по сопернику
        opponent_votes: Dict[str, List[MatchCandidate]] = {}
        for c in self.candidates:
            opp = canonical_team(c.opponent) or c.opponent
            if not opp:
                continue
            opponent_votes.setdefault(opp, []).append(c)

        if not opponent_votes:
            return None

        # Лучший соперник — тот, за которого больше голосов
        best_opp = max(opponent_votes, key=lambda k: (
            # live кандидаты ценнее
            sum(1 for c in opponent_votes[k] if c.is_live) * 10 +
            # кол-во источников
            len(opponent_votes[k]) +
            # сумма confidence
            sum(c.confidence for c in opponent_votes[k])
        ))

        group = opponent_votes[best_opp]

        # Если только 1 источник и матч не live — не доверяем (может быть архив)
        if len(group) < SCORE_CONFIRM_THRESHOLD:
            # Но если live — можно доверять
            live = [c for c in group if c.is_live]
            if not live:
                logger.info("⚠️ Матч vs %s: только %d источник(ов), не подтверждён",
                           best_opp, len(group))
                # Всё равно вернём, но с пометкой
                best = max(group, key=lambda c: c.confidence)
                best.confidence = min(best.confidence, 0.3)
                return best

        # Берём кандидата с наибольшей confidence
        best = max(group, key=lambda c: (c.is_live, c.confidence, bool(c.score)))

        # Обновляем confidence на основе подтверждения
        best.confidence = min(1.0, 0.3 + 0.2 * len(group))

        self._last_verified = best
        return best


verifier = MatchVerifier()

# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: metallurg.ru (глубокий)
# ═══════════════════════════════════════════════════════════════════

def parse_metallurg_site() -> List[MatchCandidate]:
    """Глубокий парсинг metallurg.ru — возвращает СПИСОК кандидатов."""
    candidates = []

    # 1. Главная
    html = fetch_page("https://metallurg.ru/")
    if html:
        candidates.extend(_deep_extract_candidates(html, "metallurg_site"))

    # 2. Подстраницы
    for path in ["/matches/", "/schedule/", "/calendar/", "/team/matches/"]:
        html = fetch_page(f"https://metallurg.ru{path}")
        if html:
            candidates.extend(_deep_extract_candidates(html, "metallurg_site"))

    # 3. API
    for url in ["https://metallurg.ru/api/matches/",
                "https://metallurg.ru/api/v1/matches/",
                "https://metallurg.ru/local/api/matches.php"]:
        data = fetch_json(url)
        if data:
            candidates.extend(_json_to_candidates(data, "metallurg_api"))

    logger.info("metallurg.ru: %d кандидатов", len(candidates))
    return candidates


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: khl.ru (глубокий)
# ═══════════════════════════════════════════════════════════════════

def parse_khl() -> List[MatchCandidate]:
    """Глубокий парсинг khl.ru."""
    candidates = []
    td = today_str()

    # 1. Главная
    for base in ["https://www.khl.ru/", "https://khl.ru/"]:
        html = fetch_page(base)
        if html:
            candidates.extend(_deep_extract_candidates(html, "khl"))
            break

    # 2. Календарь
    for url in [f"https://www.khl.ru/calendar/{td}/",
                "https://www.khl.ru/calendar/",
                f"https://www.khl.ru/calendar/?date={td}"]:
        html = fetch_page(url)
        if html:
            candidates.extend(_deep_extract_candidates(html, "khl"))

    # 3. API
    for url in [f"https://khl.api.webcaster.pro/api/khl_mobile/events_v2.json?q[start_at_from_date]={td}",
                "https://www.khl.ru/api/events/today/"]:
        data = fetch_json(url)
        if data:
            candidates.extend(_json_to_candidates(data, "khl_api"))

    logger.info("khl.ru: %d кандидатов", len(candidates))
    return candidates


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: ТВ-ИН (трансляция + парсинг КХЛ)
# ═══════════════════════════════════════════════════════════════════

def parse_tv_in() -> Dict[str, Any]:
    """
    Парсит tv-in.ru:
    1. Ищет онлайн-трансляцию КХЛ
    2. Если трансляция есть — парсит данные (счёт, команды)
    3. Ищет iframe/embed с видеопотоком
    """
    result = {
        "has_stream": False,
        "stream_url": "",
        "khl_found": False,
        "match_text": "",
        "candidates": [],
    }

    # Основная страница трансляций
    html = fetch_page("https://tv-in.ru/translyacyya-on-line.html")
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    # ── Ищем упоминание КХЛ / хоккея / Металлурга ──
    khl_keywords = ["кхл", "khl", "хоккей", "hockey"] + MG_ALIASES
    khl_lines = []
    for i, line in enumerate(lines):
        low = line.lower()
        if any(kw in low for kw in khl_keywords):
            window = lines[max(0, i-3):i+4]
            khl_lines.extend(window)
            result["khl_found"] = True

    if khl_lines:
        result["match_text"] = " ".join(khl_lines)

        # Ищем счёт
        combined = " ".join(khl_lines)
        score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', combined)
        opponent = find_opponent_in_text(combined)

        if score_m and (opponent or any(a in combined.lower() for a in MG_ALIASES)):
            c = MatchCandidate(
                home="Металлург Мг",
                away=opponent or "Соперник",
                score=f"{score_m.group(1)}:{score_m.group(2)}",
                is_live=True,
                period=_extract_period_text(combined),
                source="tv_in",
                confidence=0.7,
            )
            result["candidates"].append(c)

    # ── Ищем iframe / embed (видеопоток) ──
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if src:
            result["has_stream"] = True
            result["stream_url"] = src
            break

    for embed in soup.find_all("embed"):
        src = embed.get("src", "")
        if src:
            result["has_stream"] = True
            result["stream_url"] = src
            break

    # ── Ищем video / source теги ──
    for video in soup.find_all("video"):
        for source in video.find_all("source"):
            src = source.get("src", "")
            if src:
                result["has_stream"] = True
                result["stream_url"] = src
                break

    # ── Ищем ссылки на трансляцию в JavaScript ──
    for script in soup.find_all("script"):
        js = script.string or ""
        if not js:
            continue
        # Ищем URL потока
        stream_patterns = [
            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
            r'(https?://[^\s"\']+/live[^\s"\']*)',
            r'(https?://[^\s"\']+/stream[^\s"\']*)',
            r'src\s*[:=]\s*["\']?(https?://[^\s"\']+)',
            r'file\s*[:=]\s*["\']?(https?://[^\s"\']+)',
            r'source\s*[:=]\s*["\']?(https?://[^\s"\']+)',
        ]
        for pat in stream_patterns:
            m = re.search(pat, js)
            if m:
                url = m.group(1)
                if any(ext in url.lower() for ext in ['.m3u8', '/live', '/stream', '.mp4', '.flv']):
                    result["has_stream"] = True
                    result["stream_url"] = url

        # Ищем данные о матче в JS
        if any(kw in js.lower() for kw in khl_keywords):
            cands = _extract_candidates_from_js(js, "tv_in")
            result["candidates"].extend(cands)

    # ── Парсим страницу программы передач на наличие КХЛ ──
    schedule_urls = [
        "https://tv-in.ru/programma-peredach.html",
        "https://tv-in.ru/schedule.html",
        "https://tv-in.ru/raspisanie.html",
    ]
    for url in schedule_urls:
        sched_html = fetch_page(url)
        if not sched_html:
            continue
        sched_soup = BeautifulSoup(sched_html, "html.parser")
        sched_text = sched_soup.get_text(separator="\n")
        for line in sched_text.split("\n"):
            low = line.strip().lower()
            if any(kw in low for kw in khl_keywords):
                result["khl_found"] = True
                result["match_text"] += " | " + line.strip()

    logger.info("ТВ-ИН: stream=%s, khl=%s, candidates=%d",
               result["has_stream"], result["khl_found"], len(result["candidates"]))
    return result


def parse_tv_in_stream_page(stream_url: str) -> List[MatchCandidate]:
    """
    Парсит страницу, на которую ведёт iframe трансляции,
    ищет данные о матче (оверлеи со счётом и т.п.)
    """
    candidates = []
    if not stream_url:
        return candidates

    html = fetch_page(stream_url)
    if not html:
        return candidates

    soup = BeautifulSoup(html, "html.parser")

    # Ищем оверлеи со счётом
    for tag in soup.find_all(["div", "span", "p"]):
        text = tag.get_text(separator=" ", strip=True)
        if any(a in text.lower() for a in MG_ALIASES):
            score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
            if score_m:
                opponent = find_opponent_in_text(text)
                candidates.append(MatchCandidate(
                    home="Металлург Мг",
                    away=opponent or "Соперник",
                    score=f"{score_m.group(1)}:{score_m.group(2)}",
                    is_live=True,
                    period=_extract_period_text(text),
                    source="tv_in_stream",
                    confidence=0.8,
                ))

    # Ищем в JS
    for script in soup.find_all("script"):
        js = script.string or ""
        if js:
            candidates.extend(_extract_candidates_from_js(js, "tv_in_stream"))

    return candidates


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: ОТВ
# ═══════════════════════════════════════════════════════════════════

def parse_otv() -> Dict[str, Any]:
    """Парсит otv.ru — канал Челябинска и Челяб. области."""
    result = {"found": False, "text": "", "candidates": []}

    for url in ["https://www.otv.ru/", "https://www.otv.ru/sport/",
                "https://www.otv.ru/online/"]:
        html = fetch_page(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        for line in text.split("\n"):
            low = line.strip().lower()
            if any(a in low for a in MG_ALIASES):
                result["found"] = True
                result["text"] += line.strip() + " | "

        candidates = _deep_extract_candidates(html, "otv")
        result["candidates"].extend(candidates)

    return result


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: Telegram
# ═══════════════════════════════════════════════════════════════════

def parse_telegram(channel: str = "metallurgmgn") -> Tuple[List[Dict], List[MatchCandidate]]:
    """Возвращает (посты, кандидаты_матча)."""
    html = fetch_page(f"https://t.me/s/{channel}")
    if not html:
        return [], []

    soup = BeautifulSoup(html, "html.parser")
    posts = []
    candidates = []

    for msg in soup.select(".tgme_widget_message")[-20:]:
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ", strip=True)
        if not text:
            continue

        post: Dict[str, Any] = {"text": text, "source": "metallurg_telegram"}

        msg_link = msg.get("data-post", "")
        if msg_link:
            try:
                post["post_id"] = int(msg_link.split("/")[-1])
            except (ValueError, IndexError):
                pass

        time_el = msg.select_one("time")
        if time_el:
            post["dt"] = time_el.get("datetime", "")
            # Проверяем что пост сегодняшний
            dt_str = post["dt"]
            if dt_str and today_str() not in dt_str:
                # Может быть вчерашний — пропускаем для матча, но оставляем для пересылки
                pass

        posts.append(post)

        # Извлекаем кандидатов
        if any(a in text.lower() for a in MG_ALIASES):
            score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
            opponent = find_opponent_in_text(text)
            if score_m and opponent:
                # Проверяем дату поста
                is_today = True
                if post.get("dt") and today_str() not in post["dt"]:
                    is_today = False

                candidates.append(MatchCandidate(
                    home="Металлург Мг",
                    away=opponent,
                    score=f"{score_m.group(1)}:{score_m.group(2)}",
                    is_live=is_today and _check_if_live_text(text),
                    period=_extract_period_text(text),
                    source="metallurg_telegram",
                    confidence=0.6 if is_today else 0.2,
                ))

    logger.info("Telegram @%s: %d постов, %d кандидатов", channel, len(posts), len(candidates))
    return posts, candidates


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСЕР: VK
# ═══════════════════════════════════════════════════════════════════

def parse_vk(group: str = "hcmetallurg") -> Tuple[List[Dict], List[MatchCandidate]]:
    html = fetch_page(f"https://m.vk.com/{group}", headers=MOBILE_HEADERS)
    if not html:
        return [], []

    soup = BeautifulSoup(html, "html.parser")
    posts = []
    candidates = []

    for item in soup.select(".wall_item, .post, .wi_body")[:15]:
        text_el = item.select_one(".wall_post_text, .pi_text, .wpt")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ", strip=True)
        if not text:
            continue
        posts.append({"text": text, "source": "metallurg_vk"})

        if any(a in text.lower() for a in MG_ALIASES):
            score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
            opponent = find_opponent_in_text(text)
            if score_m and opponent:
                candidates.append(MatchCandidate(
                    home="Металлург Мг",
                    away=opponent,
                    score=f"{score_m.group(1)}:{score_m.group(2)}",
                    is_live=_check_if_live_text(text),
                    period=_extract_period_text(text),
                    source="metallurg_vk",
                    confidence=0.5,
                ))

    logger.info("VK %s: %d постов, %d кандидатов", group, len(posts), len(candidates))
    return posts, candidates


# ═══════════════════════════════════════════════════════════════════
# ██  ОБЩИЕ УТИЛИТЫ ПАРСИНГА
# ═══════════════════════════════════════════════════════════════════

def _deep_extract_candidates(html: str, source: str) -> List[MatchCandidate]:
    """Глубоко парсит HTML, возвращает всех кандидатов."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    # A) JSON в <script>
    for script in soup.find_all("script"):
        js = script.string or ""
        if js:
            candidates.extend(_extract_candidates_from_js(js, source))

    # B) CSS классы
    match_css = [
        "match", "game", "score", "widget", "live", "result",
        "board", "current", "today", "header-match", "main-match",
        "next-match", "match-widget", "scoreboard", "match-info",
        "match-result", "match-score", "game-info",
    ]
    for cls in match_css:
        for tag in soup.find_all(class_=re.compile(cls, re.I)):
            c = _tag_to_candidate(tag, source)
            if c:
                candidates.append(c)

    # C) data-атрибуты
    for tag in soup.find_all(True):
        for attr_name, attr_val in tag.attrs.items():
            if isinstance(attr_val, str) and any(kw in attr_val.lower()
                    for kw in ["match", "game", "score", "матч"]):
                c = _tag_to_candidate(tag, source)
                if c:
                    candidates.append(c)
                break

    # D) Полнотекстовый
    body_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if not any(a in line.lower() for a in MG_ALIASES):
            continue
        window = " ".join(lines[max(0, i-4):i+5])
        c = _text_to_candidate(window, source)
        if c:
            # Проверяем дату
            if validate_match_date(window):
                candidates.append(c)
            else:
                c.confidence = 0.1
                candidates.append(c)

    # Фильтруем: только с Металлургом
    candidates = [c for c in candidates if c.has_mg]

    # Дедупликация
    seen = set()
    unique = []
    for c in candidates:
        key = (c.opponent, c.score)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


def _extract_candidates_from_js(js: str, source: str) -> List[MatchCandidate]:
    """Извлекает кандидатов из JavaScript."""
    candidates = []

    # Ищем JSON
    json_patterns = [
        r'(?:var|let|const)\s+\w+\s*=\s*(\{[^;]{10,3000}\})\s*;',
        r'(?:var|let|const)\s+\w+\s*=\s*(\[[^\]]{10,5000}\])\s*;',
        r'data\s*[:=]\s*(\{.+?\})\s*[,;]',
    ]
    for pat in json_patterns:
        for m in re.finditer(pat, js, re.DOTALL):
            try:
                data = json.loads(m.group(1))
                candidates.extend(_json_to_candidates(data, source))
            except (json.JSONDecodeError, ValueError):
                pass

    # Прямой поиск счёта
    if any(a in js.lower() for a in MG_ALIASES):
        score_m = re.search(r'["\']?score["\']?\s*[:=]\s*["\']?(\d+)\s*[:\-]\s*(\d+)', js)
        if score_m:
            opp = find_opponent_in_text(js)
            if opp:
                candidates.append(MatchCandidate(
                    home="Металлург Мг", away=opp,
                    score=f"{score_m.group(1)}:{score_m.group(2)}",
                    is_live="live" in js.lower(),
                    period=0, source=source, confidence=0.5,
                ))

    return candidates


def _json_to_candidates(data: Any, source: str) -> List[MatchCandidate]:
    """JSON → список кандидатов."""
    candidates = []

    if isinstance(data, dict):
        data_str = json.dumps(data, ensure_ascii=False).lower()
        if not any(a in data_str for a in MG_ALIASES):
            return []

        for key in ["matches", "games", "data", "items", "result",
                     "events", "schedule"]:
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    c = _dict_to_candidate(item, source)
                    if c:
                        candidates.append(c)

        c = _dict_to_candidate(data, source)
        if c:
            candidates.append(c)

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                c = _dict_to_candidate(item, source)
                if c:
                    candidates.append(c)

    return candidates


def _dict_to_candidate(m: dict, source: str) -> Optional[MatchCandidate]:
    """Словарь → кандидат."""
    if not isinstance(m, dict):
        return None

    home = ""
    away = ""

    for k in ["home", "team_a", "home_team", "team_home", "team1"]:
        if k in m:
            v = m[k]
            home = v.get("name", v.get("title", str(v))) if isinstance(v, dict) else str(v)
            break
    for k in ["away", "team_b", "away_team", "team_away", "team2"]:
        if k in m:
            v = m[k]
            away = v.get("name", v.get("title", str(v))) if isinstance(v, dict) else str(v)
            break

    if not home and not away:
        return None

    # Нужен Металлург
    if not is_mg(home) and not is_mg(away):
        return None

    score = ""
    if "score" in m:
        score = str(m["score"])
    else:
        sa = m.get("score_a", m.get("home_score", m.get("score_home", "")))
        sb = m.get("score_b", m.get("away_score", m.get("score_away", "")))
        if sa != "" and sb != "":
            score = f"{sa}:{sb}"

    status = str(m.get("status", m.get("state", ""))).lower()
    is_live = status in ("live", "playing", "in_progress", "active", "started")

    period = 0
    for pk in ["period", "current_period", "game_period"]:
        if pk in m:
            try:
                period = int(m[pk])
            except (ValueError, TypeError):
                pass
            break

    # Проверяем дату
    date_str = str(m.get("date", m.get("game_date", m.get("datetime", ""))))
    confidence = 0.6
    if date_str and today_str() in date_str:
        confidence = 0.8
    elif date_str and today_str() not in date_str and not is_live:
        confidence = 0.1  # Не сегодня

    return MatchCandidate(
        home=str(home), away=str(away), score=score,
        is_live=is_live, period=period, source=source,
        confidence=confidence,
    )


def _tag_to_candidate(tag, source: str) -> Optional[MatchCandidate]:
    """HTML-тег → кандидат."""
    text = tag.get_text(separator=" ", strip=True)
    return _text_to_candidate(text, source)


def _text_to_candidate(text: str, source: str) -> Optional[MatchCandidate]:
    """Текст → кандидат."""
    if not text or len(text) < 5 or len(text) > 2000:
        return None
    if not any(a in text.lower() for a in MG_ALIASES):
        return None

    score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    opponent = find_opponent_in_text(text)

    if not opponent and not score_m:
        return None

    score = f"{score_m.group(1)}:{score_m.group(2)}" if score_m else ""
    is_live = _check_if_live_text(text)
    period = _extract_period_text(text)

    # Определяем дома/в гостях
    if opponent:
        mg_pos = min((text.lower().find(a) for a in MG_ALIASES if a in text.lower()), default=999)
        opp_pos = text.lower().find(opponent.lower())
        if mg_pos < opp_pos:
            home, away = "Металлург Мг", opponent
        else:
            home, away = opponent, "Металлург Мг"
    else:
        home, away = "Металлург Мг", "Соперник"

    confidence = 0.4
    if opponent:
        confidence += 0.2
    if score:
        confidence += 0.1
    if is_live:
        confidence += 0.2
    if validate_match_date(text):
        confidence += 0.1

    return MatchCandidate(
        home=home, away=away, score=score,
        is_live=is_live, period=period, source=source,
        confidence=min(1.0, confidence),
    )


def _check_if_live_text(text: str) -> bool:
    keywords = ["live", "онлайн", "идёт", "идет", "прямой",
                "сейчас", "текущий", "в эфире", "трансляция",
                "online", "playing", "in progress"]
    return any(kw in text.lower() for kw in keywords)


def _extract_period_text(text: str) -> int:
    m = re.search(r'(\d)\s*[-\s]?\s*(?:период|пер|per)', text.lower())
    if m:
        return int(m.group(1))
    if any(kw in text.lower() for kw in ["овертайм", "overtime", "от "]):
        return 4
    if any(kw in text.lower() for kw in ["буллит", "shootout"]):
        return 5
    m = re.search(r'(\d)\s*п(?:ер)?\.', text.lower())
    if m:
        return int(m.group(1))
    return 0


# ═══════════════════════════════════════════════════════════════════
# ██  СБОРЩИК ДАННЫХ
# ═══════════════════════════════════════════════════════════════════

class Collector:
    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.ok: Dict[str, bool] = {}
        self.tg_posts: List[Dict] = []
        self.vk_posts: List[Dict] = []
        self.tv_in_data: Dict[str, Any] = {}
        self.otv_data: Dict[str, Any] = {}
        self.names = {
            "metallurg_site": "🌐 Metallurg.ru",
            "metallurg_api": "🌐 Metallurg.ru API",
            "khl": "🏒 KHL.ru",
            "khl_api": "🏒 KHL.ru API",
            "metallurg_telegram": "📱 Telegram @metallurgmgn",
            "metallurg_vk": "📘 VKontakte",
            "tv_in": "📺 ТВ-ИН (Магнитогорск)",
            "tv_in_stream": "📺 ТВ-ИН стрим",
            "otv": "📺 ОТВ (Челябинск)",
        }

    async def check_all(self) -> None:
        loop = asyncio.get_running_loop()
        self.ok = {k: False for k in ["metallurg_site", "khl", "metallurg_telegram",
                                       "metallurg_vk", "tv_in", "otv"]}
        verifier.clear()

        # ── metallurg.ru ──
        try:
            cands = await asyncio.wait_for(
                loop.run_in_executor(None, parse_metallurg_site),
                timeout=REQUEST_TIMEOUT + 10)
            if cands:
                self.ok["metallurg_site"] = True
                for c in cands:
                    verifier.add(c)
        except Exception as e:
            logger.error("❌ metallurg.ru: %s", e)

        # ── khl.ru ──
        try:
            cands = await asyncio.wait_for(
                loop.run_in_executor(None, parse_khl),
                timeout=REQUEST_TIMEOUT + 10)
            if cands:
                self.ok["khl"] = True
                for c in cands:
                    verifier.add(c)
        except Exception as e:
            logger.error("❌ khl.ru: %s", e)

        # ── Telegram ──
        try:
            posts, cands = await asyncio.wait_for(
                loop.run_in_executor(None, parse_telegram),
                timeout=REQUEST_TIMEOUT + 5)
            self.tg_posts = posts
            if posts:
                self.ok["metallurg_telegram"] = True
            for c in cands:
                verifier.add(c)
        except Exception as e:
            logger.error("❌ Telegram: %s", e)

        # ── VK ──
        try:
            posts, cands = await asyncio.wait_for(
                loop.run_in_executor(None, parse_vk),
                timeout=REQUEST_TIMEOUT + 5)
            self.vk_posts = posts
            if posts:
                self.ok["metallurg_vk"] = True
            for c in cands:
                verifier.add(c)
        except Exception as e:
            logger.error("❌ VK: %s", e)

        # ── ТВ-ИН (с парсингом трансляции КХЛ) ──
        try:
            tv_data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_tv_in),
                timeout=REQUEST_TIMEOUT + 5)
            self.tv_in_data = tv_data
            if tv_data.get("khl_found") or tv_data.get("has_stream"):
                self.ok["tv_in"] = True
            for c in tv_data.get("candidates", []):
                verifier.add(c)

            # Если есть стрим — парсим его страницу
            stream_url = tv_data.get("stream_url", "")
            if stream_url and stream_url != S.tv_in_stream_url:
                S.tv_in_stream_url = stream_url
                logger.info("📺 ТВ-ИН стрим: %s", stream_url)

            if S.tv_in_stream_url:
                stream_cands = await asyncio.wait_for(
                    loop.run_in_executor(None, parse_tv_in_stream_page, S.tv_in_stream_url),
                    timeout=REQUEST_TIMEOUT + 5)
                for c in stream_cands:
                    verifier.add(c)
                if stream_cands:
                    S.tv_in_watching = True

        except Exception as e:
            logger.error("❌ ТВ-ИН: %s", e)

        # ── ОТВ ──
        try:
            otv_data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_otv),
                timeout=REQUEST_TIMEOUT + 5)
            self.otv_data = otv_data
            if otv_data.get("found"):
                self.ok["otv"] = True
            for c in otv_data.get("candidates", []):
                verifier.add(c)
        except Exception as e:
            logger.error("❌ ОТВ: %s", e)

        # Логируем итог
        active = sum(1 for v in self.ok.values() if v)
        total = len(self.ok)
        n_cands = len(verifier.candidates)
        logger.info("📡 Источники: %d/%d, кандидатов: %d", active, total, n_cands)

    def get_match(self) -> Optional[MatchCandidate]:
        """Верифицированный матч."""
        return verifier.best()

    def get_social_posts(self) -> List[Dict[str, str]]:
        return self.tg_posts + self.vk_posts


collector = Collector()

# ─────────────────────────── ПЕРЕСЫЛКА ПОСТОВ ────────────────────

async def forward_metallurg_posts():
    if not S.channel_id:
        return

    posts = collector.tg_posts
    if not posts:
        return

    new_posts = []
    for post in posts:
        post_id = post.get("post_id", 0)
        text = post.get("text", "")
        if not post_id or not text:
            continue
        if post_id in S.forwarded_post_ids:
            continue
        if post_id <= S.last_forwarded_post_id:
            continue
        new_posts.append(post)

    if S.last_forwarded_post_id == 0 and posts:
        max_id = max((p.get("post_id", 0) for p in posts), default=0)
        S.last_forwarded_post_id = max_id
        S.forwarded_post_ids = {p.get("post_id", 0) for p in posts if p.get("post_id")}
        logger.info("Инициализация пересылки: последний ID = %d", max_id)
        return

    for post in new_posts:
        post_id = post.get("post_id", 0)
        text = post.get("text", "")

        S.forwarded_post_ids.add(post_id)
        if post_id > S.last_forwarded_post_id:
            S.last_forwarded_post_id = post_id

        msg_text = (
            f"📢 <b>ХК Металлург Мг</b>\n\n"
            f"{text}\n\n"
            f"<a href='https://t.me/metallurgmgn/{post_id}'>📎 Оригинал</a>"
        )

        try:
            await bot.send_message(S.channel_id, msg_text, parse_mode="HTML",
                                   disable_web_page_preview=True)
            logger.info("📢 Переслан пост #%d", post_id)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error("Ошибка пересылки #%d: %s", post_id, e)


# ─────────────────────────── ОТПРАВКА ────────────────────────────

async def send(text: str, source: str = ""):
    if not S.channel_id:
        logger.warning("Канал не установлен")
        return
    if source and source in collector.names:
        text += f"\n\n<i>📡 {collector.names[source]}</i>"
    try:
        await bot.send_message(S.channel_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка отправки: %s", e)

# ─────────────────────────── ФОРМАТИРОВАНИЕ ──────────────────────

def fmt_active_pen(home: str, away: str) -> str:
    tr = S.penalties
    ha, aa = tr.active_home(), tr.active_away()
    if not ha and not aa:
        return ""
    lines = []
    if ha:
        lines.append(f"\n🟡 Штрафы <b>{home}</b>:")
        for p in ha:
            lines.append(f"   • {p.player} — {p.minutes} мин ({p.reason}) [ост. {p.remaining_str()}]")
    if aa:
        lines.append(f"\n🟡 Штрафы <b>{away}</b>:")
        for p in aa:
            lines.append(f"   • {p.player} — {p.minutes} мин ({p.reason}) [ост. {p.remaining_str()}]")
    lines.append(f"\n👥 На льду: <b>{tr.strength()}</b>")
    return "\n".join(lines)


def fmt_all_pen(home: str, away: str) -> str:
    tr = S.penalties
    all_p = tr.all_sorted()
    if not all_p:
        return "🟢 Штрафов в матче пока нет."
    lines = ["📋 <b>Все штрафы матча:</b>\n"]
    for p in all_p:
        icon = "⏳" if p.active else "✅"
        lines.append(f"{icon} {p.team} | {p.player} — {p.minutes} мин ({p.reason}) "
                     f"[{p.period}-й пер., {p.game_time}]")
    th = sum(p.minutes for p in tr.home)
    ta = sum(p.minutes for p in tr.away)
    lines.append(f"\nИтого: {home} — {th} мин, {away} — {ta} мин")
    return "\n".join(lines)


# ─────────────────────────── КОМАНДЫ ─────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "🏒 <b>Бот ХК «Металлург» Магнитогорск</b>\n\n"
        "📡 <b>Источники (с верификацией!):</b>\n"
        "• 🌐 metallurg.ru — глубокий парсинг\n"
        "• 🏒 khl.ru — глубокий парсинг\n"
        "• 📱 Telegram @metallurgmgn\n"
        "• 📘 VK hcmetallurg\n"
        "• 📺 ТВ-ИН — парсинг трансляции КХЛ\n"
        "• 📺 ОТВ — канал Челябинска\n\n"
        "🛡 <b>Защита от ложных матчей:</b> данные\n"
        "подтверждаются несколькими источниками.\n\n"
        "📢 Посты @metallurgmgn пересылаются автоматически!\n\n"
        "<b>Команды:</b>\n"
        "/setchannel — привязать канал\n"
        "/status — статус бота\n"
        "/score — текущий счёт\n"
        "/penalties — штрафы\n"
        "/sources — источники\n"
        "/tv — ТВ-ИН / ОТВ / трансляции\n"
        "/teams — команды КХЛ\n"
        "/forcecheck — проверить сейчас\n"
        "/debug — отладочная информация\n"
        "/stop — остановить",
        parse_mode="HTML")


@dp.message(Command("setchannel"))
async def cmd_setchannel(msg: Message, state: FSMContext):
    parts = msg.text.split(maxsplit=1)
    if len(parts) >= 2:
        await _do_set_channel(msg, parts[1].strip())
        return
    await msg.answer(
        "📺 <b>Установка канала</b>\n\n"
        "1️⃣ Добавьте бота в канал\n"
        "2️⃣ Администратор с правом отправки\n"
        "3️⃣ Узнайте ID через @userinfobot\n"
        "4️⃣ Отправьте ID сюда\n\n"
        "💡 Пример: <code>-1001234567890</code>",
        parse_mode="HTML")
    await state.set_state(ChannelSetup.waiting_id)


@dp.message(ChannelSetup.waiting_id)
async def on_channel_id(msg: Message, state: FSMContext):
    await _do_set_channel(msg, msg.text.strip())
    await state.clear()


async def _do_set_channel(msg: Message, raw: str):
    try:
        cid = int(raw)
    except ValueError:
        await msg.answer("❌ Числовой ID.\nПример: <code>-1001234567890</code>", parse_mode="HTML")
        return
    if not str(cid).startswith("-100"):
        await msg.answer("⚠️ ID начинается с <code>-100</code>", parse_mode="HTML")
        return
    try:
        chat = await bot.get_chat(cid)
    except Exception as e:
        await msg.answer(f"❌ Канал не найден.\n<code>{e}</code>", parse_mode="HTML")
        return
    if chat.type != "channel":
        await msg.answer(f"⚠️ Тип: {chat.type}, нужен channel.", parse_mode="HTML")
        return
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(cid, me.id)
    except Exception as e:
        await msg.answer(f"❌ Проверка прав: <code>{e}</code>", parse_mode="HTML")
        return
    if member.status not in ("administrator", "creator"):
        await msg.answer("⚠️ Бот не администратор канала.", parse_mode="HTML")
        return
    try:
        test = await bot.send_message(cid, "✅ Бот подключён!", parse_mode="HTML")
        await asyncio.sleep(3)
        try:
            await bot.delete_message(cid, test.message_id)
        except Exception:
            pass
    except Exception as e:
        await msg.answer(f"❌ Отправка: <code>{e}</code>", parse_mode="HTML")
        return
    S.channel_id = cid
    await msg.answer(
        f"✅ <b>Канал установлен!</b>\n"
        f"📺 <b>{chat.title}</b>\n"
        f"🆔 <code>{cid}</code>",
        parse_mode="HTML")
    logger.info("Канал: %s (%s)", chat.title, cid)


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    ch = "не установлен"
    if S.channel_id:
        try:
            chat = await bot.get_chat(S.channel_id)
            ch = chat.title
        except Exception:
            ch = str(S.channel_id)

    active = sum(1 for v in collector.ok.values() if v)
    total = len(collector.ok)
    tr = S.penalties

    match = verifier.best()
    conf = f"{match.confidence:.0%}" if match else "—"

    lines = [
        "🏒 <b>Статус</b>\n",
        f"📺 Канал: <b>{ch}</b>",
        f"🔴 Матч: <b>{'идёт' if S.is_live else 'нет'}</b>",
        f"🏠 {S.home_team or '—'} vs 🏃 {S.away_team or '—'}",
        f"📊 Счёт: <b>{S.score or '—'}</b>",
        f"🛡 Уверенность: <b>{conf}</b>",
        f"👥 На льду: <b>{tr.strength()}</b>",
        f"📡 Источники: {active}/{total}",
        f"📺 ТВ-ИН стрим: {'🟢' if S.tv_in_watching else '⚪'}",
        f"📢 Переслано: {len(S.forwarded_post_ids)}",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(msg: Message):
    wait_msg = await msg.answer("🔄 Проверяю источники (с верификацией)...")

    await collector.check_all()
    match = collector.get_match()

    if not match:
        await wait_msg.edit_text(
            "⚠️ Матч Металлурга не найден.\n\n"
            "Проверены все источники, матч не подтверждён.\n"
            "Попробуйте /forcecheck или /debug")
        return

    home, away = match.home, match.away
    score = match.score or "—"
    src_name = collector.names.get(match.source, match.source)

    live_str = "🔴 LIVE" if match.is_live else "⚪ Не начался" if not match.score else "🏁 Завершён"
    period_str = PERIODS.get(match.period, "") if match.period else ""
    conf = f"{match.confidence:.0%}"

    pen_block = fmt_active_pen(home, away)

    text = (
        f"🏒 <b>{home}</b>  {score}  <b>{away}</b>\n\n"
        f"{live_str}"
        f"{f'  |  {period_str}' if period_str else ''}\n"
        f"🛡 Уверенность: <b>{conf}</b>\n"
        f"👥 На льду: <b>{S.penalties.strength()}</b>"
        f"{pen_block}\n\n"
        f"📡 {src_name}"
    )
    await wait_msg.edit_text(text, parse_mode="HTML")


@dp.message(Command("penalties"))
async def cmd_penalties(msg: Message):
    home = S.home_team or "Хозяева"
    away = S.away_team or "Гости"
    await msg.answer(fmt_all_pen(home, away), parse_mode="HTML")


@dp.message(Command("sources"))
async def cmd_sources(msg: Message):
    lines = ["📡 <b>Источники:</b>\n"]
    urls = {
        "metallurg_site": "metallurg.ru",
        "khl": "khl.ru",
        "metallurg_telegram": "t.me/metallurgmgn",
        "metallurg_vk": "vk.com/hcmetallurg",
        "tv_in": "tv-in.ru",
        "otv": "otv.ru",
    }
    for sid in collector.ok:
        name = collector.names.get(sid, sid)
        ok = "✅" if collector.ok.get(sid) else "❌"
        url = urls.get(sid, "")
        lines.append(f"{ok} <b>{name}</b>\n   └ {url}")

    lines.append(f"\n📊 Активных: {sum(1 for v in collector.ok.values() if v)}/{len(collector.ok)}")
    lines.append(f"🔢 Кандидатов: {len(verifier.candidates)}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("tv"))
async def cmd_tv(msg: Message):
    lines = [
        "📺 <b>Трансляции:</b>\n",
        "🏒 <b>ТВ-ИН</b> — канал Магнитогорска",
        "   └ <a href='https://tv-in.ru/translyacyya-on-line.html'>Онлайн</a>",
    ]

    if S.tv_in_stream_url:
        lines.append(f"   └ 🔴 Стрим: <code>{S.tv_in_stream_url[:80]}</code>")
    if S.tv_in_watching:
        lines.append("   └ 👁 Бот парсит трансляцию!")

    tv_in = collector.tv_in_data
    if tv_in.get("khl_found"):
        lines.append(f"   └ 🏒 КХЛ найдена: {tv_in.get('match_text', '')[:100]}")

    lines.extend([
        "",
        "📺 <b>ОТВ</b> — канал Челябинска",
        "   └ <a href='https://www.otv.ru/online/'>Онлайн</a>",
    ])

    otv = collector.otv_data
    if otv.get("found"):
        lines.append(f"   └ 🏒 Металлург: {otv.get('text', '')[:100]}")

    await msg.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("teams"))
async def cmd_teams(msg: Message):
    lines = ["🏒 <b>Команды КХЛ:</b>\n"]
    for td in KHL_TEAMS_DATA:
        icon = "⭐" if td.get("is_mg") else "🏒"
        aliases = ", ".join(td["aliases"][:3])
        lines.append(f"{icon} <b>{td['name']}</b> ({td['city']})\n   Алиасы: <i>{aliases}</i>")
    lines.append(f"\n💡 Всего алиасов: {len(ALL_TEAM_ALIASES)}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("debug"))
async def cmd_debug(msg: Message):
    """Подробная отладочная информация."""
    wait_msg = await msg.answer("🔧 Собираю отладочные данные...")

    await collector.check_all()

    lines = ["🔧 <b>DEBUG</b>\n"]

    # Все кандидаты
    lines.append(f"📊 Кандидатов: {len(verifier.candidates)}\n")
    for i, c in enumerate(verifier.candidates, 1):
        lines.append(
            f"  {i}. {c.home} vs {c.away}\n"
            f"     Счёт: {c.score or '—'} | Live: {c.is_live} | "
            f"Per: {c.period} | Conf: {c.confidence:.0%}\n"
            f"     Источник: {c.source}"
        )

    # Верифицированный результат
    best = verifier.best()
    if best:
        lines.append(f"\n✅ ВЕРИФИЦИРОВАН: {best.home} {best.score} {best.away}")
        lines.append(f"   Confidence: {best.confidence:.0%}, Source: {best.source}")
    else:
        lines.append("\n❌ Матч не верифицирован")

    # ТВ-ИН
    tv = collector.tv_in_data
    if tv:
        lines.append(f"\n📺 ТВ-ИН:")
        lines.append(f"   Stream: {tv.get('has_stream')}")
        lines.append(f"   URL: {tv.get('stream_url', '—')[:60]}")
        lines.append(f"   KHL: {tv.get('khl_found')}")
        lines.append(f"   Text: {tv.get('match_text', '—')[:80]}")

    # Обрезаем если слишком длинное
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрезано)"

    await wait_msg.edit_text(text, parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_forcecheck(msg: Message):
    wait_msg = await msg.answer("🔄 Глубокая проверка + верификация...")

    await collector.check_all()

    lines = ["📊 <b>Проверка:</b>\n"]

    for sid, ok in collector.ok.items():
        icon = "✅" if ok else "❌"
        name = collector.names.get(sid, sid)
        lines.append(f"{icon} {name}")

    lines.append(f"\n🔢 Кандидатов: {len(verifier.candidates)}")

    best = collector.get_match()
    if best:
        opp = best.opponent
        lines.append(f"\n🏒 <b>Матч:</b> {best.home} {best.score or '—'} {best.away}")
        lines.append(f"🛡 Уверенность: <b>{best.confidence:.0%}</b>")
        lines.append(f"🔴 Live: {best.is_live}")
        if best.confidence < 0.5:
            lines.append("⚠️ <i>Низкая уверенность — возможно ложное срабатывание!</i>")
    else:
        lines.append("\n⚠️ Матч не определён")

    # ТВ-ИН
    if collector.tv_in_data.get("has_stream"):
        lines.append(f"\n📺 ТВ-ИН: стрим обнаружен")
    if collector.tv_in_data.get("khl_found"):
        lines.append(f"🏒 ТВ-ИН: КХЛ в программе!")

    await wait_msg.edit_text("\n".join(lines), parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    S.reset_match()
    verifier.clear()
    await msg.answer("🛑 Остановлено, данные сброшены.")


# ─────────────────────────── ОБРАБОТКА ГОЛОВ ─────────────────────

async def on_score_change(home: str, away: str, old: str, new: str, source: str):
    try:
        oa, ob = map(int, old.split(":"))
        na, nb = map(int, new.split(":"))
    except ValueError:
        return

    home_scored = na > oa
    scorer = home if home_scored else away
    mg_scored = is_mg(scorer)

    tr = S.penalties
    goal_type = "eq"
    gt_text = ""

    if home_scored:
        if tr.pp_home():
            goal_type = "pp"; S.pp_goals_h += 1; gt_text = "💪 <b>Гол в БОЛЬШИНСТВЕ!</b>"
        elif tr.pp_away():
            goal_type = "sh"; S.sh_goals_h += 1; gt_text = "🛡 <b>Гол в МЕНЬШИНСТВЕ!</b>"
    else:
        if tr.pp_away():
            goal_type = "pp"; S.pp_goals_a += 1; gt_text = "💪 <b>Гол в БОЛЬШИНСТВЕ!</b>"
        elif tr.pp_home():
            goal_type = "sh"; S.sh_goals_a += 1; gt_text = "🛡 <b>Гол в МЕНЬШИНСТВЕ!</b>"

    if mg_scored:
        texts = GOAL_MG_PP if goal_type == "pp" else GOAL_MG_SH if goal_type == "sh" else GOAL_MG
    else:
        texts = CONCEDE_PP if goal_type == "pp" else CONCEDE_SH if goal_type == "sh" else CONCEDE

    if goal_type == "pp":
        tr.cancel_minor(home_scored)

    parts = [random.choice(texts), ""]
    if gt_text:
        parts.append(gt_text)
    parts.append(f"🏒 {home} <b>{new}</b> {away}")
    parts.append(f"⚽ Забил: <b>{scorer}</b>")
    if not tr.equal():
        parts.append(f"👥 На льду: <b>{tr.strength()}</b>")
    pen = fmt_active_pen(home, away)
    if pen:
        parts.append(pen)

    await send("\n".join(parts), source)


# ─────────────────────────── WATCHER ─────────────────────────────

async def watcher():
    logger.info("🏒 Watcher запущен (верификация + ТВ-ИН)")

    while True:
        try:
            await collector.check_all()

            # Пересылка постов
            await forward_metallurg_posts()

            # Верифицированный матч
            match = collector.get_match()

            if not match or match.confidence < 0.4:
                # Нет подтверждённого матча
                if S.is_live:
                    logger.info("⚠️ Матч пропал из источников")
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            home, away = match.home, match.away
            score = match.score or ""
            is_live = match.is_live
            period = match.period
            source = match.source

            S.home_team = home
            S.away_team = away

            # ── Начало матча ──
            if is_live and not S.is_live:
                S.is_live = True
                if not S.notified_start:
                    opp = match.opponent
                    stream_info = ""
                    if S.tv_in_stream_url:
                        stream_info = ("\n\n📺 <a href='https://tv-in.ru/"
                                       "translyacyya-on-line.html'>Смотреть на ТВ-ИН</a>")
                    await send(
                        f"{random.choice(START)}\n\n"
                        f"🏒 <b>{home}</b> vs <b>{away}</b>\n"
                        f"🛡 Подтверждено ({match.confidence:.0%})"
                        f"{stream_info}",
                        source)
                    S.notified_start = True

            # ── Период ──
            if period > 0 and period != S.period:
                pt = PERIODS.get(period, f"▶️ Период {period}")
                await send(f"{pt}\n🏒 {home} <b>{score}</b> {away}", source)
                S.period = period

            # ── Счёт ──
            if score and score != S.score and S.score:
                await on_score_change(home, away, S.score, score, source)

            # ── Конец матча ──
            if not is_live and S.is_live and score and score != "0:0":
                if not S.notified_end:
                    tr = S.penalties
                    th = sum(p.minutes for p in tr.home)
                    ta = sum(p.minutes for p in tr.away)
                    pen_str = ""
                    if th or ta:
                        pen_str = (f"\n\n🟡 Штрафы:\n"
                                   f"   {home} — {th} мин\n"
                                   f"   {away} — {ta} мин")
                    await send(
                        f"{random.choice(END)}\n\n"
                        f"🏒 <b>{home}</b> {score} <b>{away}</b>{pen_str}",
                        source)
                    S.notified_end = True
                    await asyncio.sleep(300)
                    S.reset_match()
                    verifier.clear()
                    continue

            if score:
                S.score = score

            # ── Штрафы из постов ──
            for post in collector.get_social_posts():
                text = post.get("text", "")
                src = post.get("source", "")
                pid = hash(text[:80])
                if pid in S.seen_posts:
                    continue
                if not any(a in text.lower() for a in MG_ALIASES):
                    continue
                S.seen_posts.add(pid)

                pen_patterns = [
                    r'удал[её]н\w*\s+(.+?)\s*[\(\[]\s*(\d+)\s*мин',
                    r'штраф\w*\s+(.+?)\s*[\(\[]\s*(\d+)\s*мин',
                    r'(\w+\s+\w+)\s+удал[её]н\w*\s+на\s+(\d+)\s*мин',
                ]
                for pen_pat in pen_patterns:
                    pen_m = re.search(pen_pat, text.lower())
                    if pen_m:
                        player = pen_m.group(1).strip().title()
                        mins = int(pen_m.group(2))
                        pen_id = f"{player}_{mins}_{len(S.penalties.all_sorted())}"
                        if pen_id not in S.penalties.seen:
                            S.penalties.seen.add(pen_id)
                            h = S.home_team or "Металлург Мг"
                            a = S.away_team or "Соперник"
                            is_home_pen = is_mg(h)
                            team = h if is_home_pen else a
                            S.penalties.add(team, player, mins, "нарушение",
                                           S.period or 1, "??:??", is_home_pen)
                            emoji = "😤" if is_mg(team) else "😏"
                            tr = S.penalties
                            await send(
                                f"{random.choice(PEN)} {emoji}\n\n"
                                f"🏒 {team} — <b>{player}</b>\n"
                                f"⏱ {mins} мин\n"
                                f"👥 На льду: <b>{tr.strength()}</b>",
                                src)
                        break

                score_from_post = _extract_score(text)
                if score_from_post and S.score and score_from_post != S.score:
                    h = S.home_team or "Металлург Мг"
                    a = S.away_team or "Соперник"
                    await on_score_change(h, a, S.score, score_from_post, src)
                    S.score = score_from_post

        except Exception as e:
            logger.exception("Watcher error: %s", e)

        interval = CHECK_INTERVAL if S.is_live else IDLE_INTERVAL
        await asyncio.sleep(interval)


def _extract_score(text: str) -> Optional[str]:
    m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    return f"{m.group(1)}:{m.group(2)}" if m else None


# ─────────────────────────── MAIN ────────────────────────────────

async def main():
    me = await bot.get_me()
    logger.info("🏒 Бот @%s запущен", me.username)
    logger.info("📡 Источники: metallurg.ru, khl.ru, TG, VK, ТВ-ИН, ОТВ")
    logger.info("🏒 Команд КХЛ: %d, алиасов: %d", len(KHL_TEAMS_DATA), len(ALL_TEAM_ALIASES))
    logger.info("🛡 Верификация: порог = %d источника", SCORE_CONFIRM_THRESHOLD)

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
