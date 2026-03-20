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

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─────────────────────────── НАСТРОЙКИ ───────────────────────────

TOKEN = "ТВОЙ_ТОКЕН"

CHECK_INTERVAL = 30
IDLE_INTERVAL = 120
REQUEST_TIMEOUT = 15

# Канал Металлурга для пересылки постов
METALLURG_TG_CHANNEL_ID = -1001126797283
METALLURG_TG_USERNAME = "metallurgmgn"

# ─────────────────────────── КОМАНДЫ КХЛ ─────────────────────────

KHL_TEAMS = [
    "Металлург", "Металлург Мг", "Магнитка",
    "Локомотив", "СКА", "Ак Барс", "Трактор",
    "Салават Юлаев", "Авангард", "ЦСКА",
    "Динамо Минск", "Динамо Мн",
    "Динамо Москва", "Динамо М",
    "Лада", "Торпедо", "Адмирал", "Сочи",
    "Северсталь", "Сибирь", "Амур", "Барыс",
    "Автомобилист", "Нефтехимик", "Спартак",
    "Куньлунь Ред Стар", "Шанхай Драгонс",
    # Англ. варианты
    "Metallurg", "Lokomotiv", "SKA", "Ak Bars", "Traktor",
    "Salavat Yulaev", "Avangard", "CSKA",
    "Dinamo Minsk", "Dinamo Moscow",
    "Lada", "Torpedo", "Admiral", "Sochi",
    "Severstal", "Sibir", "Amur", "Barys",
    "Avtomobilist", "Neftekhimik", "Spartak",
    "Kunlun Red Star", "Shanghai Dragons",
]

KHL_TEAMS_LOWER = [t.lower() for t in KHL_TEAMS]

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

# ─────────────────────────── МОДЕЛИ ──────────────────────────────

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
        self._expire()
        return [p for p in self.home if p.active]

    def active_away(self):
        self._expire()
        return [p for p in self.away if p.active]

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
        self.home.clear()
        self.away.clear()
        self.seen.clear()


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
    # Для пересылки постов из TG канала Металлурга
    last_forwarded_post_id: int = 0
    forwarded_post_ids: set = field(default_factory=set)

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


S = BotState()

# ─────────────────────────── ТЕКСТЫ ──────────────────────────────

MG_ALIASES = [
    "металлург", "магнитка", "metallurg", "mmg", "мг",
    "металлург мг", "металлург магнитогорск",
    "metallurg mg", "metallurg magnitogorsk",
    "магнитогорский металлург",
]

def is_mg(name: str) -> bool:
    if not name:
        return False
    low = name.lower().strip()
    return any(a in low for a in MG_ALIASES)


def find_opponent_in_text(text: str) -> Optional[str]:
    """Ищет название команды-соперника КХЛ в тексте."""
    low = text.lower()
    for team in KHL_TEAMS:
        tl = team.lower()
        if tl in low and not any(a in tl for a in MG_ALIASES):
            return team
    return None


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

# ─────────────────────────── ПАРСЕРЫ ─────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def fetch_page(url: str, headers: Optional[Dict] = None) -> Optional[str]:
    """Загружает страницу, возвращает HTML или None."""
    try:
        h = headers or HEADERS
        resp = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception as e:
        logger.error("fetch %s: %s", url, e)
        return None


def fetch_json(url: str, headers: Optional[Dict] = None) -> Optional[Any]:
    """Загружает JSON."""
    try:
        h = headers or HEADERS
        resp = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("fetch_json %s: %s", url, e)
        return None


# ═══════════════════════════════════════════════════════════════════
# ██  ГЛУБОКИЙ ПАРСИНГ metallurg.ru
# ═══════════════════════════════════════════════════════════════════

def parse_metallurg_site() -> Optional[Dict[str, Any]]:
    """
    Глубокий парсинг metallurg.ru — множественные подходы:
    1. Главная — виджет матча
    2. Все подстраницы с расписанием / матчами
    3. Поиск iframe/script c данными
    4. API-эндпоинты
    """
    # ── 1. Главная ──
    result = _parse_metallurg_main_deep()
    if result and result.get("score"):
        logger.info("metallurg.ru [main]: %s", result)
        return result

    # ── 2. Подстраницы ──
    sub_paths = [
        "/matches/", "/schedule/", "/games/", "/calendar/",
        "/team/matches/", "/hockey/matches/",
        "/match/", "/game/",
        "/results/", "/media/",
    ]
    for path in sub_paths:
        r = _parse_metallurg_page(f"https://metallurg.ru{path}")
        if r and r.get("score"):
            logger.info("metallurg.ru [%s]: %s", path, r)
            return r

    # ── 3. API ──
    r = _try_metallurg_api()
    if r and r.get("score"):
        logger.info("metallurg.ru [api]: %s", r)
        return r

    # ── 4. Без счёта (ближайший матч) ──
    if result:
        return result

    # ── 5. Ещё раз главная, но ищем хоть что-то ──
    r = _parse_metallurg_brute()
    if r:
        logger.info("metallurg.ru [brute]: %s", r)
        return r

    return None


def _parse_metallurg_main_deep() -> Optional[Dict[str, Any]]:
    """Глубокий парсинг главной metallurg.ru."""
    html = fetch_page("https://metallurg.ru/")
    if not html:
        return None
    return _deep_parse_html(html, "metallurg_site")


def _parse_metallurg_page(url: str) -> Optional[Dict[str, Any]]:
    """Парсит конкретную подстраницу."""
    html = fetch_page(url)
    if not html:
        return None
    return _deep_parse_html(html, "metallurg_site")


def _deep_parse_html(html: str, source: str) -> Optional[Dict[str, Any]]:
    """
    Полный глубокий парсинг HTML — ищет матч Металлурга:
    - по классам CSS
    - по data-атрибутам
    - по тексту
    - по script-тегам (JSON внутри)
    - по iframe
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── A) Ищем JSON в <script> тегах ──
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        result = _extract_match_from_js(text, source)
        if result:
            return result

    # ── B) Ищем по data-атрибутам ──
    for tag in soup.find_all(True):
        attrs = tag.attrs
        for attr_name, attr_val in attrs.items():
            if not isinstance(attr_val, str):
                continue
            if any(a in attr_val.lower() for a in ["match", "game", "score", "матч", "счёт", "счет"]):
                text = tag.get_text(separator=" ", strip=True)
                result = _try_extract_match(text, source)
                if result:
                    return result

    # ── C) Ищем по CSS классам ──
    match_classes = [
        "match", "game", "score", "widget", "live",
        "result", "board", "current", "today",
        "header-match", "main-match", "next-match",
        "match-widget", "game-widget", "scoreboard",
        "match-info", "match-result", "match-score",
        "game-info", "game-result", "game-score",
    ]
    for cls in match_classes:
        for tag in soup.find_all(class_=re.compile(cls, re.I)):
            text = tag.get_text(separator=" ", strip=True)
            result = _try_extract_match(text, source)
            if result:
                return result

    # ── D) Ищем по id ──
    match_ids = [
        "match", "game", "score", "widget", "live",
        "scoreboard", "result", "current-match",
    ]
    for mid in match_ids:
        tag = soup.find(id=re.compile(mid, re.I))
        if tag:
            text = tag.get_text(separator=" ", strip=True)
            result = _try_extract_match(text, source)
            if result:
                return result

    # ── E) Полнотекстовый поиск ──
    body_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    result = _find_score_in_lines(lines, source)
    if result:
        return result

    # ── F) Ищем любое упоминание Металлурга ──
    for tag in soup.find_all(["div", "section", "article", "a", "span", "p", "li", "td", "tr", "table"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) < 3 or len(text) > 1000:
            continue
        if any(a in text.lower() for a in MG_ALIASES):
            result = _try_extract_match(text, source)
            if result:
                return result

    # ── G) Поиск ссылок на матч ──
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        if any(kw in href for kw in ["match", "game", "матч"]):
            text = a_tag.get_text(separator=" ", strip=True)
            result = _try_extract_match(text, source)
            if result:
                return result

    return None


def _extract_match_from_js(js_text: str, source: str) -> Optional[Dict[str, Any]]:
    """Извлекает данные матча из JavaScript кода."""
    # Ищем JSON объекты в JS
    patterns = [
        r'(?:var|let|const)\s+\w+\s*=\s*(\{[^;]{10,2000}\})\s*;',
        r'(?:var|let|const)\s+\w+\s*=\s*(\[[^\]]{10,5000}\])\s*;',
        r'JSON\.parse\s*\(\s*[\'"](.+?)[\'"]\s*\)',
        r'data\s*[:=]\s*(\{.+?\})\s*[,;]',
        r'match\w*\s*[:=]\s*(\{.+?\})\s*[,;]',
        r'game\w*\s*[:=]\s*(\{.+?\})\s*[,;]',
    ]
    for pat in patterns:
        for m in re.finditer(pat, js_text, re.DOTALL):
            raw = m.group(1)
            try:
                data = json.loads(raw)
                result = _parse_json_data(data, source)
                if result:
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

    # Ищем прямые упоминания счёта в JS
    if any(a in js_text.lower() for a in MG_ALIASES):
        score_m = re.search(r'["\']?score["\']?\s*[:=]\s*["\']?(\d+)\s*[:\-]\s*(\d+)', js_text)
        if score_m:
            return {
                "home": "Металлург",
                "away": find_opponent_in_text(js_text) or "Соперник",
                "score": f"{score_m.group(1)}:{score_m.group(2)}",
                "is_live": "live" in js_text.lower(),
                "period": 0,
                "source": source,
            }

    return None


def _parse_json_data(data: Any, source: str) -> Optional[Dict[str, Any]]:
    """Пытается распарсить JSON-данные о матче."""
    if isinstance(data, dict):
        # Проверяем, содержит ли Металлург
        data_str = json.dumps(data, ensure_ascii=False).lower()
        if not any(a in data_str for a in MG_ALIASES):
            return None

        # Ищем в разных ключах
        for key in ["matches", "games", "data", "items", "result", "events", "schedule"]:
            if key in data and isinstance(data[key], list):
                result = _find_match_in_list(data[key], source)
                if result:
                    return result

        # Сам объект — матч?
        result = _try_parse_single_match(data, source)
        if result:
            return result

    elif isinstance(data, list):
        return _find_match_in_list(data, source)

    return None


def _find_match_in_list(matches: list, source: str) -> Optional[Dict[str, Any]]:
    """Ищет матч Металлурга в списке."""
    today = datetime.now().strftime("%Y-%m-%d")
    best = None

    for m in matches:
        if not isinstance(m, dict):
            continue

        m_str = json.dumps(m, ensure_ascii=False).lower()
        if not any(a in m_str for a in MG_ALIASES):
            continue

        result = _try_parse_single_match(m, source)
        if not result:
            continue

        # Live матч — сразу возвращаем
        if result.get("is_live"):
            return result

        # Сегодняшний
        date_str = str(m.get("date", m.get("game_date", m.get("datetime", ""))))
        if today in date_str:
            best = result

        if not best:
            best = result

    return best


def _try_parse_single_match(m: dict, source: str) -> Optional[Dict[str, Any]]:
    """Парсит один матч из JSON."""
    home = ""
    away = ""

    # Пробуем разные ключи для команд
    home_keys = ["home", "team_a", "home_team", "team_home", "team1", "teamA"]
    away_keys = ["away", "team_b", "away_team", "team_away", "team2", "teamB"]

    for k in home_keys:
        if k in m:
            v = m[k]
            home = v.get("name", v.get("title", str(v))) if isinstance(v, dict) else str(v)
            break

    for k in away_keys:
        if k in m:
            v = m[k]
            away = v.get("name", v.get("title", str(v))) if isinstance(v, dict) else str(v)
            break

    if not home and not away:
        return None

    # Счёт
    score = ""
    if "score" in m:
        score = str(m["score"])
    else:
        sa = m.get("score_a", m.get("home_score", m.get("score_home", "")))
        sb = m.get("score_b", m.get("away_score", m.get("score_away", "")))
        if sa != "" and sb != "":
            score = f"{sa}:{sb}"

    # Статус
    status = str(m.get("status", m.get("state", m.get("game_status", "")))).lower()
    is_live = status in ("live", "playing", "in_progress", "active", "started", "ongoing")

    period = 0
    for pk in ["period", "current_period", "game_period"]:
        if pk in m:
            try:
                period = int(m[pk])
            except (ValueError, TypeError):
                pass
            break

    return {
        "home": str(home),
        "away": str(away),
        "score": str(score),
        "is_live": is_live,
        "period": period,
        "source": source,
    }


def _try_extract_match(text: str, source: str) -> Optional[Dict[str, Any]]:
    """Пытается извлечь матч из текстового блока."""
    if len(text) < 5 or len(text) > 1000:
        return None
    if not any(a in text.lower() for a in MG_ALIASES):
        return None

    score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    teams = _extract_teams_from_text(text)

    if score_m and teams:
        is_live = _check_if_live_text(text)
        period = _extract_period_text(text)
        return {
            "home": teams[0],
            "away": teams[1],
            "score": f"{score_m.group(1)}:{score_m.group(2)}",
            "is_live": is_live,
            "period": period,
            "source": source,
        }

    if teams:
        return {
            "home": teams[0],
            "away": teams[1],
            "score": f"{score_m.group(1)}:{score_m.group(2)}" if score_m else "",
            "is_live": _check_if_live_text(text),
            "period": _extract_period_text(text),
            "source": source,
        }

    return None


def _try_metallurg_api() -> Optional[Dict[str, Any]]:
    """Пробуем разные API-эндпоинты metallurg.ru."""
    api_urls = [
        "https://metallurg.ru/api/matches/",
        "https://metallurg.ru/api/v1/matches/",
        "https://metallurg.ru/api/v2/matches/",
        "https://metallurg.ru/local/api/matches.php",
        "https://metallurg.ru/api/schedule/",
        "https://metallurg.ru/api/games/",
        "https://metallurg.ru/bitrix/services/main/ajax.php",
        "https://metallurg.ru/ajax/matches/",
        "https://metallurg.ru/ajax/schedule/",
    ]
    for url in api_urls:
        data = fetch_json(url)
        if data:
            logger.info("metallurg API hit: %s → %s", url, str(data)[:200])
            result = _parse_json_data(data, "metallurg_api")
            if result:
                return result
    return None


def _parse_metallurg_brute() -> Optional[Dict[str, Any]]:
    """Брутфорс: ищем любое упоминание матча на всех страницах."""
    html = fetch_page("https://metallurg.ru/")
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Собираем все внутренние ссылки
    internal_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/") and not href.startswith("//"):
            internal_links.add(href)
        elif "metallurg.ru" in href:
            internal_links.add(href)

    # Фильтруем полезные
    useful_keywords = ["match", "game", "schedule", "calendar", "live",
                       "матч", "расписание", "календарь", "результат"]
    for link in internal_links:
        if any(kw in link.lower() for kw in useful_keywords):
            url = link if link.startswith("http") else f"https://metallurg.ru{link}"
            page_html = fetch_page(url)
            if page_html:
                result = _deep_parse_html(page_html, "metallurg_site")
                if result and result.get("score"):
                    return result

    return None


# ═══════════════════════════════════════════════════════════════════
# ██  ГЛУБОКИЙ ПАРСИНГ khl.ru
# ═══════════════════════════════════════════════════════════════════

def parse_khl() -> Optional[Dict[str, Any]]:
    """
    Глубокий парсинг khl.ru:
    1. Главная — текущие матчи
    2. Расписание
    3. API
    4. Страница Металлурга
    """
    # ── 1. Главная khl.ru ──
    result = _parse_khl_main()
    if result and result.get("score"):
        return result

    # ── 2. Расписание ──
    result = _parse_khl_schedule()
    if result:
        return result

    # ── 3. API ──
    result = _parse_khl_api()
    if result:
        return result

    # ── 4. Страница Металлурга ──
    result = _parse_khl_team_page()
    if result:
        return result

    return None


def _parse_khl_main() -> Optional[Dict[str, Any]]:
    """Парсинг главной khl.ru."""
    html = fetch_page("https://www.khl.ru/")
    if not html:
        html = fetch_page("https://khl.ru/")
    if not html:
        return None
    return _deep_parse_html(html, "khl")


def _parse_khl_schedule() -> Optional[Dict[str, Any]]:
    """Парсинг расписания khl.ru."""
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")

    urls = [
        f"https://www.khl.ru/calendar/{date_str}/",
        "https://www.khl.ru/calendar/",
        "https://www.khl.ru/schedule/",
        f"https://www.khl.ru/calendar/{today.strftime('%Y-%m-%d')}/",
        "https://www.khl.ru/games/",
    ]
    for url in urls:
        html = fetch_page(url)
        if not html:
            continue
        result = _deep_parse_html(html, "khl")
        if result:
            return result
    return None


def _parse_khl_api() -> Optional[Dict[str, Any]]:
    """Пробуем API khl.ru."""
    today = datetime.now().strftime("%Y-%m-%d")

    api_urls = [
        f"https://khl.api.webcaster.pro/api/khl_mobile/events_v2.json?q[start_at_from_date]={today}",
        f"https://www.khl.ru/api/events/?date={today}",
        "https://www.khl.ru/api/events/today/",
        f"https://khl-scores.qstage.io/api/v1/scores?date={today}",
    ]

    for url in api_urls:
        data = fetch_json(url)
        if data:
            logger.info("khl API hit: %s → %s", url, str(data)[:200])
            result = _parse_json_data(data, "khl_api")
            if result:
                return result
    return None


def _parse_khl_team_page() -> Optional[Dict[str, Any]]:
    """Парсинг страницы Металлурга на khl.ru."""
    # Металлург Мг ID на khl.ru
    urls = [
        "https://www.khl.ru/clubs/metallurg_mg/",
        "https://www.khl.ru/clubs/metallurg-mg/",
        "https://www.khl.ru/teams/metallurg_mg/",
    ]
    for url in urls:
        html = fetch_page(url)
        if not html:
            continue
        result = _deep_parse_html(html, "khl")
        if result:
            return result
    return None


# ═══════════════════════════════════════════════════════════════════
# ██  ТВ-ИН и ОТВ
# ═══════════════════════════════════════════════════════════════════

def parse_tv_in() -> Optional[Dict[str, str]]:
    """
    Парсит tv-in.ru — официальный канал Магнитогорска.
    Ищет информацию о трансляции матча.
    """
    html = fetch_page("https://tv-in.ru/translyacyya-on-line.html")
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        low = line.lower()
        if any(a in low for a in MG_ALIASES) or "хоккей" in low or "кхл" in low:
            window = " ".join(lines[max(0, i - 3):i + 4])
            return {
                "text": window,
                "source": "tv_in",
                "url": "https://tv-in.ru/translyacyya-on-line.html",
            }

    # Ищем iframe с трансляцией
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if src:
            return {
                "text": f"Онлайн-трансляция: {src}",
                "source": "tv_in",
                "url": src,
            }

    return None


def parse_otv() -> Optional[Dict[str, str]]:
    """
    Парсит ОТВ (otv.ru) — официальный канал Челябинска и Челябинской области.
    Ищет информацию о матчах/трансляциях.
    """
    urls = [
        "https://www.otv.ru/",
        "https://www.otv.ru/sport/",
        "https://www.otv.ru/online/",
    ]

    for url in urls:
        html = fetch_page(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        for i, line in enumerate(lines):
            low = line.lower()
            if any(a in low for a in MG_ALIASES) or "металлург" in low:
                window = " ".join(lines[max(0, i - 3):i + 4])
                return {
                    "text": window,
                    "source": "otv",
                    "url": url,
                }

    return None


# ═══════════════════════════════════════════════════════════════════
# ██  Telegram и VK парсеры
# ═══════════════════════════════════════════════════════════════════

def parse_telegram(channel: str = "metallurgmgn") -> List[Dict[str, str]]:
    """Парсит публичный канал через t.me/s/"""
    html = fetch_page(f"https://t.me/s/{channel}")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.select(".tgme_widget_message")[-20:]:
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ", strip=True)
        if text:
            post = {"text": text, "source": "metallurg_telegram"}

            # Извлекаем ID поста
            msg_link = msg.get("data-post", "")
            if msg_link:
                try:
                    post_id = int(msg_link.split("/")[-1])
                    post["post_id"] = post_id
                except (ValueError, IndexError):
                    pass

            time_el = msg.select_one("time")
            if time_el:
                post["dt"] = time_el.get("datetime", "")
            posts.append(post)

    logger.info("Telegram @%s: %d постов", channel, len(posts))
    return posts


def parse_vk(group: str = "hcmetallurg") -> List[Dict[str, str]]:
    """Парсит публичную группу VK через мобильную версию."""
    vk_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    html = fetch_page(f"https://m.vk.com/{group}", headers=vk_headers)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for item in soup.select(".wall_item, .post, .wi_body")[:15]:
        text_el = item.select_one(".wall_post_text, .pi_text, .wpt")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ", strip=True)
        if text:
            posts.append({"text": text, "source": "metallurg_vk"})

    logger.info("VK %s: %d постов", group, len(posts))
    return posts


# ═══════════════════════════════════════════════════════════════════
# ██  УТИЛИТЫ ПАРСИНГА
# ═══════════════════════════════════════════════════════════════════

def _find_score_in_lines(lines: List[str], source: str) -> Optional[Dict[str, Any]]:
    """Ищет счёт матча Металлурга в строках текста."""
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(a in low for a in MG_ALIASES):
            continue

        # Ищем счёт в этой строке и соседних
        window = " ".join(lines[max(0, i - 5):i + 6])
        score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', window)
        if not score_m:
            continue

        teams = _extract_teams_from_text(window)
        if teams:
            is_live = _check_if_live_text(window)
            period = _extract_period_text(window)
            return {
                "home": teams[0],
                "away": teams[1],
                "score": f"{score_m.group(1)}:{score_m.group(2)}",
                "is_live": is_live,
                "period": period,
                "source": source,
            }

    # Второй проход: ищем без строгой привязки
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(a in low for a in MG_ALIASES):
            continue
        # Просто нашли Металлург — вернём без счёта
        window = " ".join(lines[max(0, i - 5):i + 6])
        teams = _extract_teams_from_text(window)
        opponent = find_opponent_in_text(window)
        if teams:
            return {
                "home": teams[0],
                "away": teams[1],
                "score": "",
                "is_live": _check_if_live_text(window),
                "period": 0,
                "source": source,
            }
        elif opponent:
            return {
                "home": "Металлург",
                "away": opponent,
                "score": "",
                "is_live": False,
                "period": 0,
                "source": source,
            }

    return None


def _extract_teams_from_text(text: str) -> Optional[List[str]]:
    """Извлекает два названия команд из текста."""
    # Паттерн: "Команда1 — Команда2"
    separators = [r'—', r'–', r'-', r'vs\.?', r'против']
    for sep in separators:
        m = re.search(
            rf'([А-ЯЁа-яёA-Za-z\s\.\-]+?)\s*{sep}\s*([А-ЯЁа-яёA-Za-z\s\.\-]+)',
            text
        )
        if m:
            t1 = m.group(1).strip()
            t2 = m.group(2).strip()
            # Очищаем
            t1 = re.sub(r'^\d+\s*', '', t1).strip()
            t2 = re.sub(r'\s*\d+$', '', t2).strip()
            if 2 < len(t1) < 50 and 2 < len(t2) < 50:
                # Проверяем, что хотя бы одна — КХЛ команда
                if (any(a in t1.lower() for a in MG_ALIASES) or
                    any(a in t2.lower() for a in MG_ALIASES) or
                    t1.lower() in KHL_TEAMS_LOWER or
                    t2.lower() in KHL_TEAMS_LOWER):
                    return [t1, t2]

    # Ищем Металлург + соперника из списка КХЛ
    opponent = find_opponent_in_text(text)
    if opponent and any(a in text.lower() for a in MG_ALIASES):
        # Определяем порядок
        mg_pos = min((text.lower().find(a) for a in MG_ALIASES if a in text.lower()), default=999)
        opp_pos = text.lower().find(opponent.lower())
        if mg_pos < opp_pos:
            return ["Металлург", opponent]
        else:
            return [opponent, "Металлург"]

    # Fallback: Металлург + любое слово с заглавной
    mg_m = re.search(r'(металлург\w*)', text, re.IGNORECASE)
    if mg_m:
        without_mg = re.sub(r'металлург\w*', '', text, flags=re.IGNORECASE).strip()
        team_m = re.search(r'([А-ЯЁA-Z][а-яёa-z]{2,}(?:\s+[А-ЯЁA-Z]?[а-яёa-z]+)*)', without_mg)
        if team_m:
            other = team_m.group(1).strip()
            if 2 < len(other) < 40:
                return ["Металлург", other]

    return None


def _check_if_live_text(text: str) -> bool:
    """Проверяет, идёт ли матч, по тексту."""
    keywords = [
        "live", "онлайн", "идёт", "идет", "прямой",
        "сейчас", "текущий", "в эфире", "трансляция",
        "online", "playing", "in progress",
    ]
    text_low = text.lower()
    return any(kw in text_low for kw in keywords)


def _extract_period_text(text: str) -> int:
    """Извлекает номер периода из текста."""
    m = re.search(r'(\d)\s*[-\s]?\s*(?:период|пер|per)', text.lower())
    if m:
        return int(m.group(1))
    if "от" in text.lower() or "овертайм" in text.lower() or "overtime" in text.lower():
        return 4
    if "булл" in text.lower() or "shootout" in text.lower():
        return 5
    # "1П", "2П", "3П"
    m = re.search(r'(\d)\s*п(?:ер)?', text.lower())
    if m:
        return int(m.group(1))
    return 0


def _extract_score(text: str) -> Optional[str]:
    m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    return f"{m.group(1)}:{m.group(2)}" if m else None


# ═══════════════════════════════════════════════════════════════════
# ██  СБОРЩИК ДАННЫХ
# ═══════════════════════════════════════════════════════════════════

class Collector:
    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.ok: Dict[str, bool] = {
            "metallurg_site": False,
            "khl": False,
            "metallurg_telegram": False,
            "metallurg_vk": False,
            "tv_in": False,
            "otv": False,
        }
        self.names = {
            "metallurg_site": "🌐 Metallurg.ru",
            "metallurg_api": "🌐 Metallurg.ru API",
            "khl": "🏒 KHL.ru",
            "khl_api": "🏒 KHL.ru API",
            "metallurg_telegram": "📱 Telegram @metallurgmgn",
            "metallurg_vk": "📘 VKontakte",
            "tv_in": "📺 ТВ-ИН (Магнитогорск)",
            "otv": "📺 ОТВ (Челябинск)",
        }

    async def check_all(self) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        self.results = {}

        # ── Сайт metallurg.ru ──
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_metallurg_site),
                timeout=REQUEST_TIMEOUT + 10)
            if data:
                self.results["metallurg_site"] = data
                self.ok["metallurg_site"] = True
                logger.info("✅ metallurg.ru: %s %s %s",
                           data.get("home"), data.get("score"), data.get("away"))
            else:
                self.ok["metallurg_site"] = False
                logger.info("❌ metallurg.ru: данных нет")
        except Exception as e:
            self.ok["metallurg_site"] = False
            logger.error("❌ metallurg.ru: %s", e)

        # ── KHL.ru ──
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_khl),
                timeout=REQUEST_TIMEOUT + 10)
            if data:
                self.results["khl"] = data
                self.ok["khl"] = True
                logger.info("✅ khl.ru: %s %s %s",
                           data.get("home"), data.get("score"), data.get("away"))
            else:
                self.ok["khl"] = False
                logger.info("❌ khl.ru: данных нет")
        except Exception as e:
            self.ok["khl"] = False
            logger.error("❌ khl.ru: %s", e)

        # ── Telegram ──
        try:
            posts = await asyncio.wait_for(
                loop.run_in_executor(None, parse_telegram),
                timeout=REQUEST_TIMEOUT + 5)
            if posts:
                self.results["metallurg_telegram"] = posts
                self.ok["metallurg_telegram"] = True
            else:
                self.ok["metallurg_telegram"] = False
        except Exception as e:
            self.ok["metallurg_telegram"] = False
            logger.error("❌ Telegram: %s", e)

        # ── VK ──
        try:
            posts = await asyncio.wait_for(
                loop.run_in_executor(None, parse_vk),
                timeout=REQUEST_TIMEOUT + 5)
            if posts:
                self.results["metallurg_vk"] = posts
                self.ok["metallurg_vk"] = True
            else:
                self.ok["metallurg_vk"] = False
        except Exception as e:
            self.ok["metallurg_vk"] = False
            logger.error("❌ VK: %s", e)

        # ── ТВ-ИН ──
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_tv_in),
                timeout=REQUEST_TIMEOUT + 5)
            if data:
                self.results["tv_in"] = data
                self.ok["tv_in"] = True
                logger.info("✅ ТВ-ИН: %s", data.get("text", "")[:100])
            else:
                self.ok["tv_in"] = False
        except Exception as e:
            self.ok["tv_in"] = False
            logger.error("❌ ТВ-ИН: %s", e)

        # ── ОТВ ──
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_otv),
                timeout=REQUEST_TIMEOUT + 5)
            if data:
                self.results["otv"] = data
                self.ok["otv"] = True
                logger.info("✅ ОТВ: %s", data.get("text", "")[:100])
            else:
                self.ok["otv"] = False
        except Exception as e:
            self.ok["otv"] = False
            logger.error("❌ ОТВ: %s", e)

        return self.results

    def get_match(self) -> Optional[Dict]:
        """Лучшие данные о матче — приоритет: live > с счётом > без."""
        candidates = []

        # Сайт metallurg
        site = self.results.get("metallurg_site")
        if site and isinstance(site, dict):
            candidates.append(("metallurg_site", site))

        # KHL
        khl = self.results.get("khl")
        if khl and isinstance(khl, dict):
            candidates.append(("khl", khl))

        # Из постов
        for key in ("metallurg_telegram", "metallurg_vk"):
            posts = self.results.get(key, [])
            if not isinstance(posts, list):
                continue
            for post in reversed(posts):
                text = post.get("text", "")
                score = _extract_score(text)
                if score and any(a in text.lower() for a in MG_ALIASES):
                    teams = _extract_teams_from_text(text)
                    candidate = {
                        "home": teams[0] if teams else "Металлург",
                        "away": teams[1] if teams else find_opponent_in_text(text) or "Соперник",
                        "score": score,
                        "is_live": True,
                        "period": 0,
                        "source": key,
                    }
                    candidates.append((key, candidate))
                    break  # Берём только последний пост с счётом

        if not candidates:
            return None

        # Приоритет: live с счётом > не-live с счётом > без счёта
        live_with_score = [(k, c) for k, c in candidates if c.get("is_live") and c.get("score")]
        if live_with_score:
            return live_with_score[0][1]

        with_score = [(k, c) for k, c in candidates if c.get("score")]
        if with_score:
            return with_score[0][1]

        return candidates[0][1]

    def get_social_posts(self) -> List[Dict[str, str]]:
        all_posts = []
        for key in ("metallurg_telegram", "metallurg_vk"):
            posts = self.results.get(key, [])
            if isinstance(posts, list):
                all_posts.extend(posts)
        return all_posts


collector = Collector()

# ─────────────────────────── ПЕРЕСЫЛКА ПОСТОВ ────────────────────

async def forward_metallurg_posts():
    """
    Пересылает новые посты из канала @metallurgmgn в установленный канал.
    Парсит через t.me/s/ и отправляет текст новых постов.
    """
    if not S.channel_id:
        return

    posts = collector.results.get("metallurg_telegram", [])
    if not isinstance(posts, list) or not posts:
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

    # Обновляем last_forwarded_post_id при первом запуске
    if S.last_forwarded_post_id == 0 and posts:
        max_id = max((p.get("post_id", 0) for p in posts), default=0)
        S.last_forwarded_post_id = max_id
        S.forwarded_post_ids = {p.get("post_id", 0) for p in posts if p.get("post_id")}
        logger.info("Инициализация пересылки: последний ID = %d", max_id)
        return

    # Пересылаем новые
    for post in new_posts:
        post_id = post.get("post_id", 0)
        text = post.get("text", "")

        S.forwarded_post_ids.add(post_id)
        if post_id > S.last_forwarded_post_id:
            S.last_forwarded_post_id = post_id

        # Форматируем пост
        msg_text = (
            f"📢 <b>ХК Металлург Мг</b>\n\n"
            f"{text}\n\n"
            f"<a href='https://t.me/metallurgmgn/{post_id}'>📎 Оригинал</a>"
        )

        try:
            await bot.send_message(S.channel_id, msg_text, parse_mode="HTML",
                                   disable_web_page_preview=True)
            logger.info("📢 Переслан пост #%d", post_id)
            await asyncio.sleep(1)  # Не спамим
        except Exception as e:
            logger.error("Ошибка пересылки поста #%d: %s", post_id, e)


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
        "📡 <b>Источники данных:</b>\n"
        "• 🌐 metallurg.ru (глубокий парсинг)\n"
        "• 🏒 khl.ru (глубокий парсинг)\n"
        "• 📱 Telegram @metallurgmgn\n"
        "• 📘 VK hcmetallurg\n"
        "• 📺 ТВ-ИН (Магнитогорск)\n"
        "• 📺 ОТВ (Челябинск)\n\n"
        "📢 Бот автоматически пересылает посты из TG-канала Металлурга!\n\n"
        "<b>Команды:</b>\n"
        "/setchannel — привязать канал\n"
        "/status — статус бота\n"
        "/score — текущий счёт\n"
        "/penalties — штрафы\n"
        "/sources — источники\n"
        "/tv — ссылки на трансляции\n"
        "/teams — команды КХЛ\n"
        "/forcecheck — проверить сейчас\n"
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
        "2️⃣ Сделайте его администратором (право отправки сообщений)\n"
        "3️⃣ Узнайте ID канала через @userinfobot или @getmyid_bot\n"
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
        await msg.answer("❌ Введите числовой ID канала.\nПример: <code>-1001234567890</code>",
                         parse_mode="HTML")
        return

    if not str(cid).startswith("-100"):
        await msg.answer("⚠️ ID канала начинается с <code>-100</code>.\n"
                         "Пример: <code>-1001234567890</code>",
                         parse_mode="HTML")
        return

    try:
        chat = await bot.get_chat(cid)
    except Exception as e:
        await msg.answer(f"❌ Канал не найден.\n<code>{e}</code>", parse_mode="HTML")
        return

    if chat.type != "channel":
        await msg.answer(f"⚠️ Это не канал (тип: {chat.type}). Нужен именно канал.", parse_mode="HTML")
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(cid, me.id)
    except Exception as e:
        await msg.answer(f"❌ Не удалось проверить права бота.\n<code>{e}</code>", parse_mode="HTML")
        return

    if member.status not in ("administrator", "creator"):
        await msg.answer("⚠️ Бот не администратор канала.\n"
                         "Добавьте бота как админа с правом отправки сообщений.", parse_mode="HTML")
        return

    if getattr(member, "can_post_messages", None) is False:
        await msg.answer("⚠️ У бота нет права отправлять сообщения.\n"
                         "Включите в настройках администратора.", parse_mode="HTML")
        return

    try:
        test = await bot.send_message(cid, "✅ Бот подключён к каналу!", parse_mode="HTML")
        await asyncio.sleep(3)
        try:
            await bot.delete_message(cid, test.message_id)
        except Exception:
            pass
    except Exception as e:
        await msg.answer(f"❌ Не удалось отправить сообщение.\n<code>{e}</code>", parse_mode="HTML")
        return

    S.channel_id = cid
    await msg.answer(
        f"✅ <b>Канал установлен!</b>\n\n"
        f"📺 <b>{chat.title}</b>\n"
        f"🆔 <code>{cid}</code>\n\n"
        f"📢 Посты из @metallurgmgn будут пересылаться автоматически.",
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

    lines = [
        "🏒 <b>Статус</b>\n",
        f"📺 Канал: <b>{ch}</b>",
        f"🔴 Матч: <b>{'идёт' if S.is_live else 'нет'}</b>",
        f"🏠 {S.home_team or '—'} vs 🏃 {S.away_team or '—'}",
        f"📊 Счёт: <b>{S.score or '—'}</b>",
        f"👥 На льду: <b>{tr.strength()}</b>",
        f"📡 Источники: {active}/{total}",
        f"📢 Переслано постов: {len(S.forwarded_post_ids)}",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(msg: Message):
    wait_msg = await msg.answer("🔄 Проверяю все источники...")

    await collector.check_all()
    data = collector.get_match()

    if not data:
        await wait_msg.edit_text(
            "⚠️ Матч Металлурга не найден.\n\n"
            "Возможно, сейчас нет игры или источники недоступны.\n\n"
            "🔍 Проверены:\n"
            "• metallurg.ru (глубокий парсинг)\n"
            "• khl.ru (глубокий парсинг)\n"
            "• Telegram @metallurgmgn\n"
            "• VK hcmetallurg\n\n"
            "Попробуйте /forcecheck")
        return

    home = data.get("home", "?")
    away = data.get("away", "?")
    score = data.get("score", "—")
    source = data.get("source", "")
    src_name = collector.names.get(source, source)
    is_live = data.get("is_live", False)
    period = data.get("period", 0)

    live_str = "🔴 LIVE" if is_live else "⚪ Не начался" if not score else "🏁 Завершён"
    period_str = PERIODS.get(period, "") if period else ""

    pen_block = fmt_active_pen(home, away)

    text = (
        f"🏒 <b>{home}</b>  {score or '—'}  <b>{away}</b>\n\n"
        f"{live_str}"
        f"{f'  |  {period_str}' if period_str else ''}\n"
        f"👥 На льду: <b>{S.penalties.strength()}</b>"
        f"{pen_block}\n\n"
        f"📡 {src_name}"
    )
    await wait_msg.edit_text(text, parse_mode="HTML")


@dp.message(Command("penalties"))
async def cmd_penalties(msg: Message):
    home = S.home_team or "Хозяева"
    away = S.away_team or "Гости"
    text = fmt_all_pen(home, away)
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("sources"))
async def cmd_sources(msg: Message):
    lines = ["📡 <b>Источники данных:</b>\n"]
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

    lines.append(f"\n📊 Активных: "
                 f"{sum(1 for v in collector.ok.values() if v)}/{len(collector.ok)}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("tv"))
async def cmd_tv(msg: Message):
    """Ссылки на трансляции."""
    lines = [
        "📺 <b>Трансляции и ТВ:</b>\n",
        "🏒 <b>ТВ-ИН</b> — официальный канал Магнитогорска",
        "   └ <a href='https://tv-in.ru/translyacyya-on-line.html'>Онлайн-трансляция</a>\n",
        "📺 <b>ОТВ</b> — канал Челябинска и Челябинской области",
        "   └ <a href='https://www.otv.ru/online/'>Онлайн</a>\n",
    ]

    # Проверяем, есть ли данные от ТВ-ИН
    tv_in = collector.results.get("tv_in")
    if tv_in:
        lines.append(f"🔴 <b>ТВ-ИН сейчас:</b> {tv_in.get('text', '—')[:200]}")

    otv = collector.results.get("otv")
    if otv:
        lines.append(f"🔴 <b>ОТВ сейчас:</b> {otv.get('text', '—')[:200]}")

    await msg.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("teams"))
async def cmd_teams(msg: Message):
    """Список команд КХЛ."""
    teams_list = [
        "Металлург Мг", "Ак Барс", "Авангард", "Автомобилист",
        "Адмирал", "Амур", "Барыс", "Динамо Минск",
        "Динамо Москва", "Лада", "Локомотив", "Нефтехимик",
        "Салават Юлаев", "Северсталь", "Сибирь", "СКА",
        "Сочи", "Спартак", "Торпедо", "Трактор",
        "ЦСКА", "Куньлунь Ред Стар / Шанхай Драгонс",
    ]
    lines = ["🏒 <b>Команды КХЛ (сезон):</b>\n"]
    for i, team in enumerate(teams_list, 1):
        icon = "⭐" if "Металлург" in team else "🏒"
        lines.append(f"{icon} {i}. {team}")

    lines.append("\n💡 Бот ищет соперника Металлурга среди всех этих команд.")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_forcecheck(msg: Message):
    wait_msg = await msg.answer("🔄 Глубокая проверка всех источников...")

    results = await collector.check_all()

    lines = ["📊 <b>Результаты глубокой проверки:</b>\n"]

    # Сайт metallurg.ru
    site = results.get("metallurg_site")
    if site:
        lines.append(f"✅ <b>metallurg.ru:</b> {site.get('home', '?')} "
                     f"{site.get('score', '—')} {site.get('away', '?')}")
        lines.append(f"   Live: {site.get('is_live')}, Период: {site.get('period')}, "
                     f"Источник: {site.get('source', '—')}")
    else:
        lines.append("❌ <b>metallurg.ru:</b> данных нет")

    # KHL.ru
    khl = results.get("khl")
    if khl:
        lines.append(f"✅ <b>khl.ru:</b> {khl.get('home', '?')} "
                     f"{khl.get('score', '—')} {khl.get('away', '?')}")
        lines.append(f"   Live: {khl.get('is_live')}, Период: {khl.get('period')}")
    else:
        lines.append("❌ <b>khl.ru:</b> данных нет")

    # Telegram
    tg = results.get("metallurg_telegram")
    if tg and isinstance(tg, list):
        lines.append(f"✅ <b>Telegram:</b> {len(tg)} постов")
        if tg:
            last = tg[-1].get("text", "")[:100]
            lines.append(f"   Последний: <i>{last}...</i>")
    else:
        lines.append("❌ <b>Telegram:</b> данных нет")

    # VK
    vk = results.get("metallurg_vk")
    if vk and isinstance(vk, list):
        lines.append(f"✅ <b>VK:</b> {len(vk)} постов")
        if vk:
            last = vk[-1].get("text", "")[:100]
            lines.append(f"   Последний: <i>{last}...</i>")
    else:
        lines.append("❌ <b>VK:</b> данных нет")

    # ТВ-ИН
    tv_in = results.get("tv_in")
    if tv_in:
        lines.append(f"✅ <b>ТВ-ИН:</b> {tv_in.get('text', '')[:100]}")
    else:
        lines.append("❌ <b>ТВ-ИН:</b> данных нет")

    # ОТВ
    otv = results.get("otv")
    if otv:
        lines.append(f"✅ <b>ОТВ:</b> {otv.get('text', '')[:100]}")
    else:
        lines.append("❌ <b>ОТВ:</b> данных нет")

    # Итог
    match = collector.get_match()
    if match:
        lines.append(f"\n🏒 <b>Определённый матч:</b>")
        lines.append(f"   {match.get('home', '?')} {match.get('score', '—')} {match.get('away', '?')}")
        opponent = find_opponent_in_text(
            f"{match.get('home', '')} {match.get('away', '')}")
        if opponent:
            lines.append(f"   Соперник: <b>{opponent}</b>")
    else:
        lines.append("\n⚠️ Матч Металлурга не определён")

    lines.append(f"\n📡 Источников работает: "
                 f"{sum(1 for v in collector.ok.values() if v)}/{len(collector.ok)}")

    await wait_msg.edit_text("\n".join(lines), parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    S.reset_match()
    await msg.answer("🛑 Отслеживание остановлено. Данные матча сброшены.")

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

    # Определяем соперника для более информативного вывода
    opponent = away if is_mg(home) else home if is_mg(away) else ""
    if opponent:
        parts.append(f"🆚 Соперник: <b>{opponent}</b>")

    if not tr.equal():
        parts.append(f"👥 На льду: <b>{tr.strength()}</b>")
    pen = fmt_active_pen(home, away)
    if pen:
        parts.append(pen)

    await send("\n".join(parts), source)

# ─────────────────────────── WATCHER ─────────────────────────────

async def watcher():
    logger.info("🏒 Watcher запущен (глубокий парсинг)")

    while True:
        try:
            results = await collector.check_all()

            # ── Пересылка постов ──
            await forward_metallurg_posts()

            if not results:
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            # ── Данные о матче ──
            match_data = collector.get_match()

            if match_data:
                home = match_data.get("home", "?")
                away = match_data.get("away", "?")
                score = match_data.get("score", "")
                is_live = match_data.get("is_live", False)
                period = match_data.get("period", 0)
                source = match_data.get("source", "")

                S.home_team = home
                S.away_team = away

                # Определяем соперника
                opponent = find_opponent_in_text(f"{home} {away}")
                if opponent:
                    logger.info("Соперник Металлурга: %s", opponent)

                # Начало матча
                if is_live and not S.is_live:
                    S.is_live = True
                    if not S.notified_start:
                        opp_text = f"\n🆚 Соперник: <b>{opponent}</b>" if opponent else ""
                        await send(
                            f"{random.choice(START)}\n\n"
                            f"🏒 <b>{home}</b> vs <b>{away}</b>"
                            f"{opp_text}",
                            source)
                        S.notified_start = True

                # Период
                if period > 0 and period != S.period:
                    pt = PERIODS.get(period, f"▶️ Период {period}")
                    await send(f"{pt}\n🏒 {home} <b>{score}</b> {away}", source)
                    S.period = period

                # Счёт
                if score and score != S.score and S.score:
                    await on_score_change(home, away, S.score, score, source)

                # Конец матча
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
                        continue

                if score:
                    S.score = score

            # ── Посты из соцсетей — штрафы ──
            for post in collector.get_social_posts():
                text = post.get("text", "")
                src = post.get("source", "")
                pid = hash(text[:80])
                if pid in S.seen_posts:
                    continue
                if not any(a in text.lower() for a in MG_ALIASES):
                    continue

                S.seen_posts.add(pid)

                # Штраф
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
                            home = S.home_team or "Металлург"
                            away = S.away_team or "Соперник"
                            is_home = is_mg(home)
                            team = home if is_home else away
                            S.penalties.add(team, player, mins, "нарушение",
                                           S.period or 1, "??:??", is_home)
                            emoji = "😤" if is_mg(team) else "😏"
                            tr = S.penalties
                            await send(
                                f"{random.choice(PEN)} {emoji}\n\n"
                                f"🏒 {team} — <b>{player}</b>\n"
                                f"⏱ {mins} мин\n"
                                f"👥 На льду: <b>{tr.strength()}</b>",
                                src)
                        break

                # Счёт из поста
                score_from_post = _extract_score(text)
                if score_from_post and S.score and score_from_post != S.score:
                    home = S.home_team or "Металлург"
                    away = S.away_team or "Соперник"
                    await on_score_change(home, away, S.score, score_from_post, src)
                    S.score = score_from_post

        except Exception as e:
            logger.exception("Watcher error: %s", e)

        interval = CHECK_INTERVAL if S.is_live else IDLE_INTERVAL
        await asyncio.sleep(interval)

# ─────────────────────────── MAIN ────────────────────────────────

async def main():
    me = await bot.get_me()
    logger.info("🏒 Бот @%s запущен (глубокий парсинг)", me.username)
    logger.info("📡 Источники: metallurg.ru, khl.ru, Telegram, VK, ТВ-ИН, ОТВ")
    logger.info("🏒 Команды КХЛ для поиска соперника: %d", len(KHL_TEAMS))

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
