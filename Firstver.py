import requests
import time
import random
import asyncio
import logging
import re
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─────────────────────────── НАСТРОЙКИ ───────────────────────────

TOKEN = "ТВОЙ_ТОКЕН"

CHECK_INTERVAL = 30        # секунд между проверками во время матча
IDLE_INTERVAL = 120        # секунд между проверками в покое
REQUEST_TIMEOUT = 15       # таймаут HTTP-запросов

# ─────────────────────────── ЛОГИРОВАНИЕ ─────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────── ИНИЦИАЛИЗАЦИЯ ───────────────────────

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ─────────────────────────── FSM ─────────────────────────────────

class SetChannelStates(StatesGroup):
    waiting_for_channel_id = State()

# ─────────────────────────── ШТРАФЫ ──────────────────────────────

@dataclass
class Penalty:
    team: str
    player: str
    minutes: int
    reason: str
    period: int
    game_time: str
    start_timestamp: float
    is_active: bool = True

    @property
    def end_timestamp(self) -> float:
        return self.start_timestamp + self.minutes * 60

    def remaining_seconds(self) -> float:
        return max(0.0, self.end_timestamp - time.time())

    def remaining_str(self) -> str:
        rem = int(self.remaining_seconds())
        if rem <= 0:
            return "завершён"
        m, s = divmod(rem, 60)
        return f"{m}:{s:02d}"


@dataclass
class PenaltyTracker:
    home_penalties: List[Penalty] = field(default_factory=list)
    away_penalties: List[Penalty] = field(default_factory=list)
    notified_ids: set = field(default_factory=set)

    def add(self, team: str, player: str, minutes: int,
            reason: str, period: int, game_time: str,
            is_home: bool) -> Penalty:
        pen = Penalty(
            team=team, player=player, minutes=minutes,
            reason=reason, period=period, game_time=game_time,
            start_timestamp=time.time(),
        )
        if is_home:
            self.home_penalties.append(pen)
        else:
            self.away_penalties.append(pen)
        return pen

    def _expire(self):
        now = time.time()
        for p in self.home_penalties + self.away_penalties:
            if p.is_active and now >= p.end_timestamp:
                p.is_active = False

    def active_home(self) -> List[Penalty]:
        self._expire()
        return [p for p in self.home_penalties if p.is_active]

    def active_away(self) -> List[Penalty]:
        self._expire()
        return [p for p in self.away_penalties if p.is_active]

    def home_on_ice(self) -> int:
        return max(3, 5 - len(self.active_home()))

    def away_on_ice(self) -> int:
        return max(3, 5 - len(self.active_away()))

    def strength_str(self) -> str:
        return f"{self.home_on_ice()} на {self.away_on_ice()}"

    def is_powerplay_home(self) -> bool:
        return self.home_on_ice() > self.away_on_ice()

    def is_powerplay_away(self) -> bool:
        return self.away_on_ice() > self.home_on_ice()

    def is_equal(self) -> bool:
        return self.home_on_ice() == self.away_on_ice()

    def cancel_minor_on_goal(self, home_scored: bool):
        targets = self.away_penalties if home_scored else self.home_penalties
        for p in targets:
            if p.is_active and p.minutes == 2:
                p.is_active = False
                break

    def all_sorted(self) -> List[Penalty]:
        combined = self.home_penalties + self.away_penalties
        combined.sort(key=lambda p: p.start_timestamp)
        return combined

    def clear(self):
        self.home_penalties.clear()
        self.away_penalties.clear()
        self.notified_ids.clear()


# ─────────────────────────── СОСТОЯНИЕ ───────────────────────────

@dataclass
class MatchState:
    channel_id: Optional[int] = None
    is_live: bool = False
    last_score: str = ""
    last_period: int = 0
    notified_start: bool = False
    notified_end: bool = False
    home_team: str = ""
    away_team: str = ""
    penalty_tracker: PenaltyTracker = field(default_factory=PenaltyTracker)
    pp_goals_home: int = 0
    pp_goals_away: int = 0
    sh_goals_home: int = 0
    sh_goals_away: int = 0
    seen_posts: set = field(default_factory=set)


state = MatchState()

# ─────────────────────────── ТЕКСТЫ ──────────────────────────────

METALLURG_ALIASES = [
    "металлург", "металлург мг", "металлург магнитогорск",
    "metallurg", "магнитка", "mmg",
]

GOAL_TEXTS = [
    "🥅🔥 МЕТАЛЛУРГ ЗАБИВАЕТ!",
    "⚡ ГОООЛ МЕТАЛЛУРГА!",
    "🚨 МАГНИТКА ЗАБИВАЕТ!",
]
GOAL_PP_TEXTS = [
    "🥅🔥⚡ МЕТАЛЛУРГ ЗАБИВАЕТ В БОЛЬШИНСТВЕ!",
    "💪🚨 ГОЛ В БОЛЬШИНСТВЕ!",
]
GOAL_SH_TEXTS = [
    "🥅😱 МЕТАЛЛУРГ ЗАБИВАЕТ В МЕНЬШИНСТВЕ!",
    "🔥🛡 ГОЛ В МЕНЬШИНСТВЕ!",
]
CONCEDE_TEXTS = [
    "😤 Пропустили...",
    "😔 Гол в наши ворота...",
]
CONCEDE_PP_TEXTS = [
    "😤 Соперник реализовал большинство...",
]
CONCEDE_SH_TEXTS = [
    "😱 Соперник забил в меньшинстве!",
]
START_TEXTS = [
    "🟢 Матч начался!",
    "🏒 Погнали, Магнитка!",
    "🏟 Шайба вброшена!",
]
END_TEXTS = [
    "🏁 Матч завершён!",
    "🔔 Финальная сирена!",
]
PENALTY_TEXTS = [
    "🟡 Удаление!",
    "⚠️ Штраф!",
]
PERIOD_TEXTS = {
    1: "1️⃣ Начался первый период",
    2: "2️⃣ Начался второй период",
    3: "3️⃣ Начался третий период",
    4: "⏱ Овертайм!",
    5: "🎯 Буллиты!",
}

# ─────────────────────────── УТИЛИТЫ ─────────────────────────────

def is_metallurg(name: str) -> bool:
    if not name:
        return False
    low = name.lower().strip()
    return any(a in low for a in METALLURG_ALIASES)


def make_post_id(text: str, suffix: str = "") -> str:
    return f"{hash(text[:80])}_{suffix}"


# ─────────────────────────── ПАРСЕРЫ ─────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def parse_metallurg_site() -> Optional[Dict[str, Any]]:
    """Парсит сайт metallurg.ru — ищет текущий/ближайший матч."""
    try:
        resp = requests.get(
            "https://metallurg.ru/",
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Ищем виджет матча на главной
        # Пробуем разные CSS-селекторы, характерные для сайта
        selectors = [
            ".match-widget",
            ".game-widget",
            ".live-game",
            ".current-match",
            "[class*='match']",
            "[class*='game']",
            ".main-match",
            ".score-widget",
        ]

        for selector in selectors:
            block = soup.select_one(selector)
            if not block:
                continue

            # Ищем названия команд
            team_els = block.select(
                ".team-name, .team__name, .team, "
                "[class*='team-name'], [class*='teamName']"
            )
            score_els = block.select(
                ".score, .result, .game-score, "
                "[class*='score'], [class*='result']"
            )

            if len(team_els) < 2:
                continue

            home = team_els[0].get_text(strip=True)
            away = team_els[1].get_text(strip=True)

            score = "0:0"
            if score_els:
                raw_score = score_els[0].get_text(strip=True)
                # Ищем паттерн "число:число" или "число-число"
                m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', raw_score)
                if m:
                    score = f"{m.group(1)}:{m.group(2)}"

            # Определяем, идёт ли матч
            block_text = block.get_text(strip=True).lower()
            block_classes = " ".join(block.get("class", [])).lower()
            live = any(
                kw in block_text or kw in block_classes
                for kw in ["live", "онлайн", "идёт", "идет", "прямой"]
            )

            # Период
            period = 0
            period_el = block.select_one(
                ".period, [class*='period'], [class*='half']"
            )
            if period_el:
                pt = period_el.get_text(strip=True)
                pm = re.search(r'(\d)', pt)
                if pm:
                    period = int(pm.group(1))

            result = {
                "home": home,
                "away": away,
                "score": score,
                "is_live": live,
                "period": period,
                "source": "metallurg_site",
            }

            logger.info(
                "metallurg.ru: %s %s %s (live=%s, period=%d)",
                home, score, away, live, period,
            )
            return result

        logger.debug("metallurg.ru: виджет матча не найден")
        return None

    except requests.RequestException as e:
        logger.error("metallurg.ru — ошибка: %s", e)
        return None


def parse_telegram_channel(channel: str) -> List[Dict[str, str]]:
    """Получает последние посты из публичного Telegram-канала."""
    try:
        if "t.me/" in channel:
            username = channel.split("t.me/")[-1].strip("/")
        else:
            username = channel.lstrip("@")

        url = f"https://t.me/s/{username}"
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        posts: List[Dict[str, str]] = []

        for msg in soup.select(".tgme_widget_message")[-10:]:
            text_el = msg.select_one(".tgme_widget_message_text")
            time_el = msg.select_one(".tgme_widget_message_date time")
            if text_el:
                post: Dict[str, str] = {
                    "text": text_el.get_text(separator=" ", strip=True),
                }
                if time_el:
                    post["datetime"] = time_el.get("datetime", "")
                posts.append(post)

        return posts

    except requests.RequestException as e:
        logger.error("Telegram %s — ошибка: %s", channel, e)
        return []


def parse_vk_group(group_url: str) -> List[Dict[str, str]]:
    """Получает последние посты из публичной группы VK."""
    try:
        group_id = group_url.rstrip("/").split("/")[-1]
        url = f"https://m.vk.com/{group_id}"
        vk_headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)"
            ),
        }
        resp = requests.get(url, headers=vk_headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        posts: List[Dict[str, str]] = []

        for post in soup.select(".wall_item, .post, [class*='wall_post']")[:10]:
            text_el = post.select_one(".wall_post_text, .pi_text")
            if text_el:
                posts.append({
                    "text": text_el.get_text(separator=" ", strip=True),
                })

        return posts

    except requests.RequestException as e:
        logger.error("VK %s — ошибка: %s", group_url, e)
        return []


def extract_score_from_text(text: str) -> Optional[str]:
    """Ищет счёт вида 'N:N' или 'N-N' в тексте."""
    m = re.search(r'(\d+)\s*[:\-–]\s*(\d+)', text)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None


def extract_penalty_from_text(text: str) -> Optional[Dict[str, str]]:
    """Пытается извлечь информацию о штрафе из текста."""
    patterns = [
        # "Удалён Иванов (2 мин) за задержку"
        r'удал[её]н\w*\s+([А-Яа-яA-Za-z\s\-]+?)\s*[\(\[]\s*(\d+)\s*мин',
        # "Штраф 2 мин — Иванов"
        r'штраф\s+(\d+)\s*мин\w*\s*[—\-:]\s*([А-Яа-яA-Za-z\s\-]+)',
        # "2 мин Иванов задержка"
        r'(\d+)\s*мин\w*\.?\s+([А-Яа-яA-Za-z]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text.lower())
        if m:
            groups = m.groups()
            if len(groups) >= 2:
                # Определяем, где player, где minutes
                if groups[0].isdigit():
                    return {"minutes": groups[0], "player": groups[1].strip().title()}
                else:
                    return {"player": groups[0].strip().title(), "minutes": groups[1]}
    return None


def is_match_related(text: str) -> bool:
    """Проверяет, относится ли пост к текущему матчу."""
    keywords = [
        r'гол', r'забил', r'счёт', r'счет', r'\d+[:\-]\d+',
        r'период', r'буллит', r'овертайм', r'удал[её]н',
        r'штраф', r'матч\s+начал', r'шайба\s+вброшена',
        r'финальн', r'сирен', r'перерыв',
    ]
    low = text.lower()
    return any(re.search(kw, low) for kw in keywords)


# ─────────────────────────── СБОР ДАННЫХ ─────────────────────────

class DataCollector:
    """Собирает данные из всех источников Металлурга."""

    SOURCES = {
        "metallurg_site": {
            "name": "Металлург Сайт",
            "url": "https://metallurg.ru",
        },
        "metallurg_telegram": {
            "name": "Металлург Telegram",
            "url": "https://t.me/metallurgmgn",
        },
        "metallurg_vk": {
            "name": "Металлург VK",
            "url": "https://vk.com/hcmetallurg",
        },
    }

    def __init__(self):
        self.last_results: Dict[str, Any] = {}
        self.source_ok: Dict[str, bool] = {k: False for k in self.SOURCES}

    async def check_all(self) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        results: Dict[str, Any] = {}

        # Сайт
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, parse_metallurg_site),
                timeout=REQUEST_TIMEOUT + 5,
            )
            if data:
                results["metallurg_site"] = data
                self.source_ok["metallurg_site"] = True
            else:
                self.source_ok["metallurg_site"] = False
        except Exception as e:
            logger.error("Источник metallurg_site: %s", e)
            self.source_ok["metallurg_site"] = False

        # Telegram
        try:
            posts = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    parse_telegram_channel,
                    "https://t.me/metallurgmgn",
                ),
                timeout=REQUEST_TIMEOUT + 5,
            )
            if posts:
                results["metallurg_telegram"] = {"posts": posts}
                self.source_ok["metallurg_telegram"] = True
            else:
                self.source_ok["metallurg_telegram"] = False
        except Exception as e:
            logger.error("Источник metallurg_telegram: %s", e)
            self.source_ok["metallurg_telegram"] = False

        # VK
        try:
            posts = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    parse_vk_group,
                    "https://vk.com/hcmetallurg",
                ),
                timeout=REQUEST_TIMEOUT + 5,
            )
            if posts:
                results["metallurg_vk"] = {"posts": posts}
                self.source_ok["metallurg_vk"] = True
            else:
                self.source_ok["metallurg_vk"] = False
        except Exception as e:
            logger.error("Источник metallurg_vk: %s", e)
            self.source_ok["metallurg_vk"] = False

        self.last_results = results
        return results

    def get_match_data(self) -> Optional[Dict[str, Any]]:
        """Возвращает лучшие данные о матче (приоритет: сайт)."""
        site = self.last_results.get("metallurg_site")
        if site:
            return site
        # Из социальных сетей можно вытащить счёт
        for key in ("metallurg_telegram", "metallurg_vk"):
            data = self.last_results.get(key)
            if not data:
                continue
            posts = data.get("posts", [])
            for post in reversed(posts):
                text = post.get("text", "")
                if is_match_related(text):
                    score = extract_score_from_text(text)
                    if score:
                        return {
                            "score": score,
                            "source": key,
                            "text": text,
                        }
        return None

    def get_social_posts(self) -> List[Dict[str, str]]:
        """Все посты из соцсетей для анализа."""
        all_posts: List[Dict[str, str]] = []
        for key in ("metallurg_telegram", "metallurg_vk"):
            data = self.last_results.get(key)
            if data:
                for p in data.get("posts", []):
                    p["_source"] = key
                    all_posts.append(p)
        return all_posts


collector = DataCollector()

# ─────────────────────────── ФОРМАТИРОВАНИЕ ──────────────────────

def fmt_active_penalties(home: str, away: str, tracker: PenaltyTracker) -> str:
    lines: List[str] = []
    ha = tracker.active_home()
    aa = tracker.active_away()

    if ha:
        lines.append(f"\n🟡 Штрафы <b>{home}</b>:")
        for p in ha:
            lines.append(
                f"   • {p.player} — {p.minutes} мин "
                f"({p.reason}) [ост. {p.remaining_str()}]"
            )
    if aa:
        lines.append(f"\n🟡 Штрафы <b>{away}</b>:")
        for p in aa:
            lines.append(
                f"   • {p.player} — {p.minutes} мин "
                f"({p.reason}) [ост. {p.remaining_str()}]"
            )
    if ha or aa:
        lines.append(f"\n👥 На льду: <b>{tracker.strength_str()}</b>")

    return "\n".join(lines)


def fmt_all_penalties(home: str, away: str, tracker: PenaltyTracker) -> str:
    all_p = tracker.all_sorted()
    if not all_p:
        return "🟢 Штрафов в матче пока нет."

    lines = ["📋 <b>Все штрафы матча:</b>\n"]
    for p in all_p:
        icon = "⏳" if p.is_active else "✅"
        lines.append(
            f"{icon} {p.team} | {p.player} — "
            f"{p.minutes} мин ({p.reason}) "
            f"[{p.period}-й пер., {p.game_time}]"
        )

    th = sum(p.minutes for p in tracker.home_penalties)
    ta = sum(p.minutes for p in tracker.away_penalties)
    lines.append(f"\nИтого: {home} — {th} мин, {away} — {ta} мин")
    return "\n".join(lines)


# ─────────────────────────── ОТПРАВКА ────────────────────────────

async def send_to_channel(text: str, source: str = ""):
    if not state.channel_id:
        logger.warning("Канал не установлен — сообщение не отправлено")
        return
    if source:
        src_name = collector.SOURCES.get(source, {}).get("name", source)
        text += f"\n\n<i>📡 {src_name}</i>"
    try:
        await bot.send_message(state.channel_id, text, parse_mode="HTML")
        logger.info("📤 Отправлено в канал (источник: %s)", source or "—")
    except Exception as e:
        logger.error("Ошибка отправки: %s", e)


# ─────────────────────────── КОМАНДЫ ─────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🏒 <b>Бот ХК «Металлург» Магнитогорск</b>\n\n"
        "Отслеживаю матчи из источников:\n"
        "• metallurg.ru\n"
        "• Telegram @metallurgmgn\n"
        "• VK hcmetallurg\n\n"
        "<b>Команды:</b>\n"
        "/setchannel — привязать канал\n"
        "/status — текущий статус\n"
        "/score — текущий счёт\n"
        "/penalties — штрафы матча\n"
        "/sources — статус источников\n"
        "/forcecheck — принудительная проверка\n"
        "/stop — остановить отслеживание",
        parse_mode="HTML",
    )


@dp.message(Command("setchannel"))
async def cmd_setchannel(message: types.Message, state_fsm: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) >= 2:
        await _try_set_channel(message, args[1].strip())
        return

    await message.answer(
        "📺 <b>Установка канала</b>\n\n"
        "1️⃣ Добавьте бота в канал\n"
        "2️⃣ Назначьте его <b>администратором</b> "
        "(право «Отправка сообщений»)\n"
        "3️⃣ Узнайте ID канала через @getmyid_bot или @RawDataBot\n"
        "4️⃣ Отправьте мне ID (начинается с <code>-100</code>)\n\n"
        "💡 Пример: <code>-1001234567890</code>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await state_fsm.set_state(SetChannelStates.waiting_for_channel_id)


@dp.message(SetChannelStates.waiting_for_channel_id)
async def process_channel_id(message: types.Message, state_fsm: FSMContext):
    await _try_set_channel(message, message.text.strip())
    await state_fsm.clear()


async def _try_set_channel(message: types.Message, raw: str):
    raw = raw.strip()

    # Валидация числа
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer(
            "❌ ID должен быть числом, начинающимся с <code>-100</code>.\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    if not str(chat_id).startswith("-100"):
        await message.answer(
            "⚠️ ID канала всегда начинается с <code>-100</code>.\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    # Проверяем доступность канала
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        await message.answer(
            f"❌ Не удалось найти канал.\n<code>{e}</code>\n"
            "Убедитесь, что бот добавлен в канал.\nПопробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    if chat.type != "channel":
        await message.answer(
            f"⚠️ Это не канал (тип: {chat.type}).\n"
            "Бот работает только с каналами.\nПопробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    # Проверяем права бота
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as e:
        await message.answer(
            f"❌ Не удалось проверить права бота.\n<code>{e}</code>\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    if member.status not in ("administrator", "creator"):
        await message.answer(
            "⚠️ Бот не является администратором канала.\n"
            "Добавьте бота как администратора с правом отправки сообщений.\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    can_post = getattr(member, "can_post_messages", None)
    if can_post is False:
        await message.answer(
            "⚠️ У бота нет права «Отправка сообщений».\n"
            "Включите его в настройках администратора.\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    # Тестовое сообщение
    try:
        test_msg = await bot.send_message(
            chat_id,
            "✅ Бот подключён! Трансляции матчей Металлурга будут здесь.",
            parse_mode="HTML",
        )
        await asyncio.sleep(5)
        try:
            await bot.delete_message(chat_id, test_msg.message_id)
        except Exception:
            pass
    except Exception as e:
        await message.answer(
            f"❌ Не удалось отправить сообщение.\n<code>{e}</code>\n"
            "Попробуйте: /setchannel",
            parse_mode="HTML",
        )
        return

    state.channel_id = chat_id
    await message.answer(
        f"✅ <b>Канал установлен!</b>\n\n"
        f"📺 <b>{chat.title}</b>\n"
        f"🆔 <code>{chat_id}</code>",
        parse_mode="HTML",
    )
    logger.info("Канал установлен: %s (%s)", chat.title, chat_id)


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if state.channel_id:
        try:
            chat = await bot.get_chat(state.channel_id)
            ch_info = f"📺 {chat.title}"
        except Exception:
            ch_info = f"📺 <code>{state.channel_id}</code>"
    else:
        ch_info = "📺 <b>не установлен</b>"

    active = sum(1 for v in collector.source_ok.values() if v)
    total = len(collector.SOURCES)
    tr = state.penalty_tracker

    lines = [
        "🏒 <b>Статус бота</b>\n",
        ch_info,
        f"📊 Счёт: <b>{state.last_score or '—'}</b>",
        f"🔴 Матч: <b>{'идёт' if state.is_live else 'нет'}</b>",
        f"👥 На льду: <b>{tr.strength_str()}</b>",
        f"📡 Источники: {active}/{total}",
    ]

    h_pim = sum(p.minutes for p in tr.home_penalties)
    a_pim = sum(p.minutes for p in tr.away_penalties)
    if h_pim or a_pim:
        lines.append(
            f"🟡 Штрафы: {state.home_team or 'Хоз.'} {h_pim} мин / "
            f"{state.away_team or 'Гости'} {a_pim} мин"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(message: types.Message):
    await message.answer("🔄 Проверяю...")
    await collector.check_all()
    data = collector.get_match_data()

    if not data:
        await message.answer(
            "⚠️ Данные о матче не найдены.\n"
            "Возможно, сейчас нет игры Металлурга."
        )
        return

    home = data.get("home", state.home_team or "?")
    away = data.get("away", state.away_team or "?")
    score = data.get("score", "—")
    source = data.get("source", "metallurg_site")
    src_name = collector.SOURCES.get(source, {}).get("name", source)

    tr = state.penalty_tracker
    pen_block = fmt_active_penalties(home, away, tr)

    text = (
        f"🏒 <b>{home}</b>  {score}  <b>{away}</b>\n"
        f"👥 На льду: <b>{tr.strength_str()}</b>"
        f"{pen_block}\n\n"
        f"📡 {src_name}"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("penalties"))
async def cmd_penalties(message: types.Message):
    home = state.home_team or "Хозяева"
    away = state.away_team or "Гости"
    text = fmt_all_penalties(home, away, state.penalty_tracker)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("sources"))
async def cmd_sources(message: types.Message):
    lines = ["📡 <b>Источники:</b>\n"]
    for sid, info in collector.SOURCES.items():
        ok = "✅" if collector.source_ok.get(sid) else "❌"
        lines.append(f"{ok} <b>{info['name']}</b>\n   └ {info['url']}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_forcecheck(message: types.Message):
    await message.answer("🔄 Проверяю все источники...")
    results = await collector.check_all()

    lines = ["📊 <b>Результаты:</b>\n"]
    for sid in collector.SOURCES:
        has_data = sid in results and results[sid]
        name = collector.SOURCES[sid]["name"]
        lines.append(f"{'✅' if has_data else '❌'} {name}")

    data = collector.get_match_data()
    if data:
        lines.append(f"\n🏒 Счёт: <b>{data.get('score', '—')}</b>")
        lines.append(
            f"🏠 {data.get('home', '?')} — "
            f"🏃 {data.get('away', '?')}"
        )
    else:
        lines.append("\n⚠️ Матч не найден")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    state.is_live = False
    state.last_score = ""
    state.last_period = 0
    state.notified_start = False
    state.notified_end = False
    state.home_team = ""
    state.away_team = ""
    state.pp_goals_home = 0
    state.pp_goals_away = 0
    state.sh_goals_home = 0
    state.sh_goals_away = 0
    state.penalty_tracker.clear()
    state.seen_posts.clear()
    await message.answer("🛑 Отслеживание остановлено.")


# ─────────────────────────── ОБРАБОТКА СОБЫТИЙ ───────────────────

async def handle_score_change(
    home: str, away: str,
    old_score: str, new_score: str,
    source: str,
):
    """Обрабатывает изменение счёта."""
    try:
        old_a, old_b = map(int, old_score.split(":"))
        new_a, new_b = map(int, new_score.split(":"))
    except ValueError:
        return

    home_scored = new_a > old_a
    scorer = home if home_scored else away
    mg_scored = is_metallurg(scorer)

    tr = state.penalty_tracker
    is_pp_h = tr.is_powerplay_home()
    is_pp_a = tr.is_powerplay_away()

    goal_type = "equal"
    goal_type_text = ""

    if home_scored:
        if is_pp_h:
            goal_type = "pp"
            state.pp_goals_home += 1
            goal_type_text = "💪 <b>Гол в БОЛЬШИНСТВЕ!</b>"
        elif is_pp_a:
            goal_type = "sh"
            state.sh_goals_home += 1
            goal_type_text = "🛡 <b>Гол в МЕНЬШИНСТВЕ!</b>"
    else:
        if is_pp_a:
            goal_type = "pp"
            state.pp_goals_away += 1
            goal_type_text = "💪 <b>Гол в БОЛЬШИНСТВЕ!</b>"
        elif is_pp_h:
            goal_type = "sh"
            state.sh_goals_away += 1
            goal_type_text = "🛡 <b>Гол в МЕНЬШИНСТВЕ!</b>"

    # Выбираем текст
    if mg_scored:
        if goal_type == "pp":
            text = random.choice(GOAL_PP_TEXTS)
        elif goal_type == "sh":
            text = random.choice(GOAL_SH_TEXTS)
        else:
            text = random.choice(GOAL_TEXTS)
    else:
        if goal_type == "pp":
            text = random.choice(CONCEDE_PP_TEXTS)
        elif goal_type == "sh":
            text = random.choice(CONCEDE_SH_TEXTS)
        else:
            text = random.choice(CONCEDE_TEXTS)

    if goal_type == "pp":
        tr.cancel_minor_on_goal(home_scored)

    parts = [text, ""]
    if goal_type_text:
        parts.append(goal_type_text)
    parts.append(f"🏒 {home} <b>{new_score}</b> {away}")
    parts.append(f"⚽ Забил: <b>{scorer}</b>")

    if not tr.is_equal():
        parts.append(f"👥 Формат: <b>{tr.strength_str()}</b>")

    pen_block = fmt_active_penalties(home, away, tr)
    if pen_block:
        parts.append(pen_block)

    await send_to_channel("\n".join(parts), source)


async def handle_social_posts(posts: List[Dict[str, str]]):
    """Анализирует посты из соцсетей на предмет штрафов и событий."""
    home = state.home_team or "Металлург"
    away = state.away_team or "Соперник"

    for post in posts:
        text = post.get("text", "")
        source = post.get("_source", "")

        if not is_match_related(text):
            continue

        pid = make_post_id(text, "event")
        if pid in state.seen_posts:
            continue
        state.seen_posts.add(pid)

        # Штрафы
        pen_info = extract_penalty_from_text(text)
        if pen_info:
            player = pen_info.get("player", "?")
            minutes = int(pen_info.get("minutes", 2))

            # Определяем чья команда
            text_low = text.lower()
            pen_is_home = is_metallurg(home) and any(
                a in text_low for a in METALLURG_ALIASES
            )

            team = home if pen_is_home else away

            tr = state.penalty_tracker
            pen_id = f"{player}_{minutes}_{len(tr.all_sorted())}"
            if pen_id not in tr.notified_ids:
                tr.notified_ids.add(pen_id)
                tr.add(
                    team=team,
                    player=player,
                    minutes=minutes,
                    reason="нарушение правил",
                    period=state.last_period or 1,
                    game_time="??:??",
                    is_home=pen_is_home,
                )

                mg_pen = is_metallurg(team)
                emoji = "😤" if mg_pen else "😏"

                strength = tr.strength_str()
                situation = ""
                if not tr.is_equal():
                    pp_team = home if tr.is_powerplay_home() else away
                    situation = f"\n💪 Большинство: <b>{pp_team}</b>"

                await send_to_channel(
                    f"{random.choice(PENALTY_TEXTS)} {emoji}\n\n"
                    f"🏒 {team} — <b>{player}</b>\n"
                    f"⏱ {minutes} мин\n"
                    f"👥 На льду: <b>{strength}</b>"
                    f"{situation}",
                    source,
                )

        # Счёт из поста
        score = extract_score_from_text(text)
        if score and score != state.last_score and state.last_score:
            await handle_score_change(home, away, state.last_score, score, source)
            state.last_score = score


# ─────────────────────────── ОСНОВНОЙ ЦИКЛ ───────────────────────

async def watcher():
    logger.info("🏒 Watcher запущен")

    while True:
        try:
            results = await collector.check_all()

            if not results:
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            # Данные с сайта — основной источник
            site_data = results.get("metallurg_site")

            if site_data:
                home = site_data.get("home", "?")
                away = site_data.get("away", "?")
                score = site_data.get("score", "0:0")
                is_live = site_data.get("is_live", False)
                period = site_data.get("period", 0)

                state.home_team = home
                state.away_team = away

                # Начало матча
                if is_live and not state.is_live:
                    state.is_live = True
                    if not state.notified_start:
                        await send_to_channel(
                            f"{random.choice(START_TEXTS)}\n\n"
                            f"🏒 <b>{home}</b> vs <b>{away}</b>",
                            "metallurg_site",
                        )
                        state.notified_start = True

                # Смена периода
                if period > 0 and period != state.last_period:
                    period_text = PERIOD_TEXTS.get(
                        period, f"▶️ Период {period}"
                    )
                    await send_to_channel(
                        f"{period_text}\n"
                        f"🏒 {home} <b>{score}</b> {away}",
                        "metallurg_site",
                    )
                    state.last_period = period

                # Изменение счёта
                if score != state.last_score and state.last_score:
                    await handle_score_change(
                        home, away, state.last_score, score,
                        "metallurg_site",
                    )

                # Конец матча
                if not is_live and state.is_live and score != "0:0":
                    if not state.notified_end:
                        tr = state.penalty_tracker
                        h_pim = sum(p.minutes for p in tr.home_penalties)
                        a_pim = sum(p.minutes for p in tr.away_penalties)

                        pen_sum = ""
                        if h_pim or a_pim:
                            pen_sum = (
                                f"\n\n🟡 <b>Штрафы:</b>\n"
                                f"   {home} — {h_pim} мин\n"
                                f"   {away} — {a_pim} мин"
                            )

                        pp_sum = ""
                        if any([
                            state.pp_goals_home, state.pp_goals_away,
                            state.sh_goals_home, state.sh_goals_away,
                        ]):
                            pp_sum = (
                                f"\n\n💪 Бол-во: {home} {state.pp_goals_home} / "
                                f"{away} {state.pp_goals_away}"
                                f"\n🛡 Мен-во: {home} {state.sh_goals_home} / "
                                f"{away} {state.sh_goals_away}"
                            )

                        await send_to_channel(
                            f"{random.choice(END_TEXTS)}\n\n"
                            f"🏒 <b>{home}</b> {score} <b>{away}</b>"
                            f"{pen_sum}{pp_sum}",
                            "metallurg_site",
                        )
                        state.notified_end = True

                    # Сброс через 5 минут
                    await asyncio.sleep(300)
                    state.is_live = False
                    state.last_score = ""
                    state.last_period = 0
                    state.notified_start = False
                    state.notified_end = False
                    state.pp_goals_home = 0
                    state.pp_goals_away = 0
                    state.sh_goals_home = 0
                    state.sh_goals_away = 0
                    state.penalty_tracker.clear()
                    state.seen_posts.clear()
                    continue

                state.last_score = score

            # Обрабатываем посты из соцсетей
            social_posts = collector.get_social_posts()
            if social_posts:
                await handle_social_posts(social_posts)

        except Exception as e:
            logger.exception("Ошибка в watcher: %s", e)

        interval = CHECK_INTERVAL if state.is_live else IDLE_INTERVAL
        await asyncio.sleep(interval)


# ─────────────────────────── ЗАПУСК ──────────────────────────────

async def main():
    me = await bot.get_me()
    logger.info("🏒 Бот запущен: @%s", me.username)

    watcher_task = asyncio.create_task(watcher())

    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
