import requests
import time
import random
import asyncio
import logging
import re
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
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

MG_ALIASES = ["металлург", "магнитка", "metallurg", "mmg", "мг"]

def is_mg(name: str) -> bool:
    if not name: return False
    low = name.lower()
    return any(a in low for a in MG_ALIASES)

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


def parse_metallurg_site() -> Optional[Dict[str, Any]]:
    """
    Парсит metallurg.ru — пробуем несколько подходов:
    1. Главная страница — виджет матча
    2. Страница расписания
    3. API если есть
    """
    result = _try_metallurg_main()
    if result:
        return result

    result = _try_metallurg_schedule()
    if result:
        return result

    result = _try_metallurg_api()
    if result:
        return result

    return None


def _try_metallurg_main() -> Optional[Dict[str, Any]]:
    """Парсинг главной страницы metallurg.ru"""
    html = fetch_page("https://metallurg.ru/")
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Логируем для отладки — какие блоки есть на странице
    all_classes = set()
    for tag in soup.find_all(True):
        classes = tag.get("class", [])
        for c in classes:
            all_classes.add(c.lower())

    logger.debug("metallurg.ru классы: %s", ", ".join(sorted(all_classes)[:50]))

    # Подход 1: ищем по тексту — находим блок со счётом
    # Ищем любой текст вида "Металлург" + счёт
    body_text = soup.get_text(separator="\n")
    lines = body_text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    score_data = _find_score_in_lines(lines)
    if score_data:
        return score_data

    # Подход 2: ищем блоки с двумя командами и счётом
    for tag in soup.find_all(["div", "section", "article", "a"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) < 5 or len(text) > 500:
            continue
        if not any(a in text.lower() for a in MG_ALIASES):
            continue

        score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
        if score_m:
            # Пробуем извлечь названия команд
            teams = _extract_teams_from_text(text)
            if teams:
                is_live = _check_if_live(tag, text)
                period = _extract_period(tag, text)
                return {
                    "home": teams[0],
                    "away": teams[1],
                    "score": f"{score_m.group(1)}:{score_m.group(2)}",
                    "is_live": is_live,
                    "period": period,
                    "source": "metallurg_site",
                }

    # Подход 3: просто ищем ближайший матч (без счёта)
    for tag in soup.find_all(["div", "section", "a"]):
        text = tag.get_text(separator=" ", strip=True)
        if any(a in text.lower() for a in MG_ALIASES):
            teams = _extract_teams_from_text(text)
            if teams:
                return {
                    "home": teams[0],
                    "away": teams[1],
                    "score": "",
                    "is_live": False,
                    "period": 0,
                    "source": "metallurg_site",
                }

    return None


def _try_metallurg_schedule() -> Optional[Dict[str, Any]]:
    """Парсинг страницы расписания."""
    for path in ["/matches/", "/schedule/", "/games/", "/calendar/"]:
        html = fetch_page(f"https://metallurg.ru{path}")
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.get_text(separator="\n")
        lines = [l.strip() for l in body_text.split("\n") if l.strip()]

        score_data = _find_score_in_lines(lines)
        if score_data:
            return score_data

    return None


def _try_metallurg_api() -> Optional[Dict[str, Any]]:
    """Пробуем найти JSON API на сайте."""
    api_urls = [
        "https://metallurg.ru/api/matches/",
        "https://metallurg.ru/api/v1/matches/",
        "https://metallurg.ru/local/api/matches.php",
    ]
    for url in api_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    logger.info("metallurg API (%s): %s", url, str(data)[:200])
                    return _parse_api_response(data)
                except Exception:
                    pass
        except Exception:
            pass
    return None


def _parse_api_response(data: Any) -> Optional[Dict[str, Any]]:
    """Пытается распарсить JSON-ответ API."""
    if isinstance(data, dict):
        # Ищем матчи в разных ключах
        for key in ["matches", "games", "data", "items", "result"]:
            if key in data and isinstance(data[key], list):
                return _find_match_in_list(data[key])
        # Может сам объект — матч
        if "home" in data or "team_a" in data or "score" in data:
            return _parse_single_match(data)

    elif isinstance(data, list):
        return _find_match_in_list(data)

    return None


def _find_match_in_list(matches: list) -> Optional[Dict[str, Any]]:
    """Ищет ближайший/текущий матч в списке."""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")

    for m in matches:
        if not isinstance(m, dict):
            continue
        # Проверяем дату
        date_str = str(m.get("date", m.get("game_date", m.get("datetime", ""))))
        if today in date_str or m.get("is_live") or m.get("status") == "live":
            return _parse_single_match(m)

    # Если не нашли сегодняшний — берём первый
    if matches and isinstance(matches[0], dict):
        return _parse_single_match(matches[0])

    return None


def _parse_single_match(m: dict) -> Optional[Dict[str, Any]]:
    """Парсит один матч из JSON."""
    home = (m.get("home", "") or m.get("team_a", "") or
            m.get("home_team", "") or m.get("team_home", ""))
    away = (m.get("away", "") or m.get("team_b", "") or
            m.get("away_team", "") or m.get("team_away", ""))

    if isinstance(home, dict):
        home = home.get("name", home.get("title", ""))
    if isinstance(away, dict):
        away = away.get("name", away.get("title", ""))

    if not home and not away:
        return None

    score_a = m.get("score_a", m.get("home_score", m.get("score_home", 0)))
    score_b = m.get("score_b", m.get("away_score", m.get("score_away", 0)))
    score = m.get("score", f"{score_a}:{score_b}")

    status = str(m.get("status", m.get("state", ""))).lower()
    is_live = status in ("live", "playing", "in_progress", "active")

    period = int(m.get("period", m.get("current_period", 0)) or 0)

    return {
        "home": str(home),
        "away": str(away),
        "score": str(score),
        "is_live": is_live,
        "period": period,
        "source": "metallurg_api",
    }


def _find_score_in_lines(lines: List[str]) -> Optional[Dict[str, Any]]:
    """Ищет счёт матча Металлурга в строках текста."""
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(a in low for a in MG_ALIASES):
            continue

        # Ищем счёт в этой строке и соседних
        window = " ".join(lines[max(0, i-3):i+4])
        score_m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', window)
        if not score_m:
            continue

        teams = _extract_teams_from_text(window)
        if teams:
            is_live = any(kw in window.lower() for kw in
                         ["live", "онлайн", "идёт", "идет", "прямой", "сейчас"])
            period = 0
            pm = re.search(r'(\d)\s*(?:период|пер)', window.lower())
            if pm:
                period = int(pm.group(1))

            return {
                "home": teams[0],
                "away": teams[1],
                "score": f"{score_m.group(1)}:{score_m.group(2)}",
                "is_live": is_live,
                "period": period,
                "source": "metallurg_site",
            }

    return None


def _extract_teams_from_text(text: str) -> Optional[List[str]]:
    """Пытается извлечь два названия команд из текста."""
    # Паттерн: "Команда1 — Команда2" или "Команда1 vs Команда2"
    m = re.search(
        r'([А-ЯЁа-яёA-Za-z\s\-\.]+?)\s*[—\-–vs\.]+\s*([А-ЯЁа-яёA-Za-z\s\-\.]+)',
        text
    )
    if m:
        t1 = m.group(1).strip()
        t2 = m.group(2).strip()
        # Фильтруем мусор
        if len(t1) > 2 and len(t2) > 2 and len(t1) < 40 and len(t2) < 40:
            return [t1, t2]

    # Если не нашли — ищем "Металлург" и любое другое слово рядом
    mg_m = re.search(r'(металлург\w*)', text.lower())
    if mg_m:
        # Убираем "Металлург" и ищем другую команду
        without_mg = re.sub(r'металлург\w*', '', text, flags=re.IGNORECASE).strip()
        # Ищем слово с заглавной буквы (название команды)
        team_m = re.search(r'([А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z]?[а-яёa-z]+)*)', without_mg)
        if team_m:
            other = team_m.group(1).strip()
            if len(other) > 2:
                return ["Металлург", other]

    return None


def _check_if_live(tag, text: str) -> bool:
    """Проверяет, идёт ли матч."""
    text_low = text.lower()
    if any(kw in text_low for kw in ["live", "онлайн", "идёт", "идет", "прямой"]):
        return True
    classes = " ".join(tag.get("class", [])).lower() if hasattr(tag, 'get') else ""
    if "live" in classes or "active" in classes or "current" in classes:
        return True
    return False


def _extract_period(tag, text: str) -> int:
    """Извлекает номер периода."""
    m = re.search(r'(\d)\s*(?:период|пер|per)', text.lower())
    if m:
        return int(m.group(1))
    if "от" in text.lower() or "овертайм" in text.lower():
        return 4
    if "булл" in text.lower():
        return 5
    return 0


# ── Telegram парсер ──────────────────────────────────────────────

def parse_telegram(channel: str = "metallurgmgn") -> List[Dict[str, str]]:
    """Парсит публичный канал через t.me/s/"""
    html = fetch_page(f"https://t.me/s/{channel}")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.select(".tgme_widget_message")[-15:]:
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(separator=" ", strip=True)
        if text:
            post = {"text": text, "source": "metallurg_telegram"}
            time_el = msg.select_one("time")
            if time_el:
                post["dt"] = time_el.get("datetime", "")
            posts.append(post)

    logger.info("Telegram @%s: %d постов", channel, len(posts))
    return posts


# ── VK парсер ────────────────────────────────────────────────────

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


# ─────────────────────────── СБОРЩИК ДАННЫХ ──────────────────────

class Collector:
    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.ok: Dict[str, bool] = {
            "metallurg_site": False,
            "metallurg_telegram": False,
            "metallurg_vk": False,
        }
        self.names = {
            "metallurg_site": "🌐 Metallurg.ru",
            "metallurg_telegram": "📱 Telegram",
            "metallurg_vk": "📘 VKontakte",
        }

    async def check_all(self) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        self.results = {}

        # Сайт
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_metallurg_site),
                timeout=REQUEST_TIMEOUT + 5)
            if data:
                self.results["metallurg_site"] = data
                self.ok["metallurg_site"] = True
                logger.info("✅ Сайт: %s %s %s",
                           data.get("home"), data.get("score"), data.get("away"))
            else:
                self.ok["metallurg_site"] = False
                logger.info("❌ Сайт: данных нет")
        except Exception as e:
            self.ok["metallurg_site"] = False
            logger.error("❌ Сайт: %s", e)

        # Telegram
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

        # VK
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

        return self.results

    def get_match(self) -> Optional[Dict]:
        """Лучшие данные о матче."""
        # Приоритет: сайт
        site = self.results.get("metallurg_site")
        if site and site.get("score"):
            return site

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
                    return {
                        "home": teams[0] if teams else "Металлург",
                        "away": teams[1] if teams else "Соперник",
                        "score": score,
                        "is_live": True,
                        "period": 0,
                        "source": key,
                    }

        # Сайт без счёта
        if site:
            return site

        return None

    def get_social_posts(self) -> List[Dict[str, str]]:
        all_posts = []
        for key in ("metallurg_telegram", "metallurg_vk"):
            posts = self.results.get(key, [])
            if isinstance(posts, list):
                all_posts.extend(posts)
        return all_posts


def _extract_score(text: str) -> Optional[str]:
    m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    return f"{m.group(1)}:{m.group(2)}" if m else None


collector = Collector()

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
        "Источники данных:\n"
        "• metallurg.ru\n"
        "• Telegram @metallurgmgn\n"
        "• VK hcmetallurg\n\n"
        "<b>Команды:</b>\n"
        "/setchannel — привязать канал\n"
        "/status — статус бота\n"
        "/score — текущий счёт\n"
        "/penalties — штрафы\n"
        "/sources — источники\n"
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
    # Парсим число
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

    # Проверяем канал
    try:
        chat = await bot.get_chat(cid)
    except Exception as e:
        await msg.answer(f"❌ Канал не найден.\n<code>{e}</code>", parse_mode="HTML")
        return

    if chat.type != "channel":
        await msg.answer(f"⚠️ Это не канал (тип: {chat.type}). Нужен именно канал.", parse_mode="HTML")
        return

    # Проверяем бота в канале
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

    # Тестовое сообщение
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

    lines = [
        "🏒 <b>Статус</b>\n",
        f"📺 Канал: <b>{ch}</b>",
        f"🔴 Матч: <b>{'идёт' if S.is_live else 'нет'}</b>",
        f"🏠 {S.home_team or '—'} vs 🏃 {S.away_team or '—'}",
        f"📊 Счёт: <b>{S.score or '—'}</b>",
        f"👥 На льду: <b>{tr.strength()}</b>",
        f"📡 Источники: {active}/{total}",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(msg: Message):
    wait_msg = await msg.answer("🔄 Проверяю источники...")

    await collector.check_all()
    data = collector.get_match()

    if not data:
        await wait_msg.edit_text(
            "⚠️ Матч Металлурга не найден.\n\n"
            "Возможно, сейчас нет игры или источники недоступны.\n"
            "Попробуйте /forcecheck")
        return

    home = data.get("home", "?")
    away = data.get("away", "?")
    score = data.get("score", "—")
    source = data.get("source", "")
    src_name = collector.names.get(source, source)
    is_live = data.get("is_live", False)
    period = data.get("period", 0)

    live_str = "🔴 LIVE" if is_live else "⚪ Не начался"
    period_str = PERIODS.get(period, "") if period else ""

    pen_block = fmt_active_pen(home, away)

    text = (
        f"🏒 <b>{home}</b>  {score}  <b>{away}</b>\n\n"
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
    lines = ["📡 <b>Источники:</b>\n"]
    urls = {
        "metallurg_site": "metallurg.ru",
        "metallurg_telegram": "t.me/metallurgmgn",
        "metallurg_vk": "vk.com/hcmetallurg",
    }
    for sid, name in collector.names.items():
        ok = "✅" if collector.ok.get(sid) else "❌"
        url = urls.get(sid, "")
        lines.append(f"{ok} <b>{name}</b>\n   └ {url}")
    lines.append(f"\nПоследняя проверка: данных получено "
                 f"{sum(1 for v in collector.ok.values() if v)}/{len(collector.ok)}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_forcecheck(msg: Message):
    wait_msg = await msg.answer("🔄 Проверяю все источники...")

    results = await collector.check_all()

    lines = ["📊 <b>Результаты проверки:</b>\n"]

    # Сайт
    site = results.get("metallurg_site")
    if site:
        lines.append(f"✅ <b>Сайт:</b> {site.get('home', '?')} "
                     f"{site.get('score', '—')} {site.get('away', '?')}")
        lines.append(f"   Live: {site.get('is_live')}, Период: {site.get('period')}")
    else:
        lines.append("❌ <b>Сайт:</b> данных нет")

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

    # Итог
    match = collector.get_match()
    if match:
        lines.append(f"\n🏒 <b>Матч:</b> {match.get('home', '?')} "
                     f"{match.get('score', '—')} {match.get('away', '?')}")
    else:
        lines.append("\n⚠️ Матч не определён")

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
    if not tr.equal():
        parts.append(f"👥 На льду: <b>{tr.strength()}</b>")
    pen = fmt_active_pen(home, away)
    if pen:
        parts.append(pen)

    await send("\n".join(parts), source)

# ─────────────────────────── WATCHER ─────────────────────────────

async def watcher():
    logger.info("🏒 Watcher запущен")

    while True:
        try:
            results = await collector.check_all()

            if not results:
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            # Данные с сайта
            site = results.get("metallurg_site")
            if site:
                home = site.get("home", "?")
                away = site.get("away", "?")
                score = site.get("score", "")
                is_live = site.get("is_live", False)
                period = site.get("period", 0)
                source = site.get("source", "metallurg_site")

                S.home_team = home
                S.away_team = away

                # Начало матча
                if is_live and not S.is_live:
                    S.is_live = True
                    if not S.notified_start:
                        await send(
                            f"{random.choice(START)}\n\n"
                            f"🏒 <b>{home}</b> vs <b>{away}</b>",
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

            # Посты из соцсетей — штрафы
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
                pen_m = re.search(
                    r'удал[её]н\w*\s+(.+?)\s*[\(\[]\s*(\d+)\s*мин', text.lower())
                if pen_m:
                    player = pen_m.group(1).strip().title()
                    mins = int(pen_m.group(2))
                    pen_id = f"{player}_{mins}_{len(S.penalties.all_sorted())}"
                    if pen_id not in S.penalties.seen:
                        S.penalties.seen.add(pen_id)
                        # Определяем команду
                        home = S.home_team or "Металлург"
                        away = S.away_team or "Соперник"
                        is_home = is_mg(home)  # упрощённо
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
    logger.info("🏒 Бот @%s запущен", me.username)

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
