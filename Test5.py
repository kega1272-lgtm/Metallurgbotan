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

TOKEN = "8756631618:AAGIoJFl8XBSJe_ISEdeZmcCgSLO9ilLQ78"

CHECK_INTERVAL = 30
IDLE_INTERVAL = 120
REQUEST_TIMEOUT = 15

# Канал Металлурга для пересылки постов
METALLURG_TG_CHANNEL_ID = -1001126797283
METALLURG_TG_USERNAME = "metallurgmgn"

# ─────────────────────────── КОМАНДЫ КХЛ ─────────────────────────

# Каноничные названия команд КХЛ (для валидации пар)
KHL_CANONICAL_TEAMS = {
    # Русские каноничные
    "Металлург Мг": ["металлург", "магнитка", "metallurg", "mmg", "мг",
                      "металлург мг", "металлург магнитогорск",
                      "metallurg mg", "metallurg magnitogorsk",
                      "магнитогорский металлург"],
    "Локомотив": ["локомотив", "lokomotiv", "локо"],
    "СКА": ["ска", "ska"],
    "Ак Барс": ["ак барс", "ak bars", "акбарс"],
    "Трактор": ["трактор", "traktor"],
    "Салават Юлаев": ["салават юлаев", "салават", "salavat yulaev", "salavat"],
    "Авангард": ["авангард", "avangard"],
    "ЦСКА": ["цска", "cska"],
    "Динамо Минск": ["динамо минск", "динамо мн", "dinamo minsk", "динамо мск минск"],
    "Динамо Москва": ["динамо москва", "динамо м", "dinamo moscow", "динамо мск"],
    "Лада": ["лада", "lada"],
    "Торпедо": ["торпедо", "torpedo"],
    "Адмирал": ["адмирал", "admiral"],
    "Северсталь": ["северсталь", "severstal"],
    "Сибирь": ["сибирь", "sibir"],
    "Амур": ["амур", "amur"],
    "Барыс": ["барыс", "barys"],
    "Автомобилист": ["автомобилист", "avtomobilist"],
    "Нефтехимик": ["нефтехимик", "neftekhimik"],
    "Спартак": ["спартак", "spartak"],
    "Шанхай Дрэгонс": ["шанхай дрэгонс", "шанхай драгонс", "shanghai dragons",
                         "шанхай", "shanghai", "куньлунь", "куньлунь ред стар",
                         "kunlun", "kunlun red star"],
}

# Плоский список всех алиасов для поиска
KHL_ALL_ALIASES: Dict[str, str] = {}  # alias_lower -> canonical_name
for canonical, aliases in KHL_CANONICAL_TEAMS.items():
    for alias in aliases:
        KHL_ALL_ALIASES[alias.lower()] = canonical

# Алиасы Металлурга
MG_ALIASES = KHL_CANONICAL_TEAMS["Металлург Мг"]

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
class ValidatedMatch:
    """Провалидированный матч с метаданными достоверности."""
    home: str  # Каноничное название
    away: str  # Каноничное название
    score: str
    is_live: bool
    period: int
    source: str
    confidence: float  # 0.0 - 1.0
    timestamp: float = field(default_factory=time.time)
    raw_home: str = ""
    raw_away: str = ""

    def is_fresh(self, max_age_seconds: int = 300) -> bool:
        return (time.time() - self.timestamp) < max_age_seconds

    def score_tuple(self) -> Optional[Tuple[int, int]]:
        if not self.score:
            return None
        m = re.match(r'(\d+):(\d+)', self.score)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None


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
    # Для валидации
    last_validated_match: Optional[ValidatedMatch] = None
    score_history: List[Tuple[str, float, str]] = field(default_factory=list)  # (score, timestamp, source)

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
        self.last_validated_match = None
        self.score_history.clear()


S = BotState()

# ─────────────────────────── ВАЛИДАЦИЯ КОМАНД ────────────────────

def canonicalize_team(name: str) -> Optional[str]:
    """Приводит название команды к каноничному. Возвращает None если не найдена."""
    if not name:
        return None
    low = name.lower().strip()

    # Точное совпадение
    if low in KHL_ALL_ALIASES:
        return KHL_ALL_ALIASES[low]

    # Проверяем вхождение алиаса в строку (для случаев типа "ХК Металлург Мг")
    # Сортируем по длине — длинные алиасы сначала (чтобы "динамо минск" не перекрывался "динамо")
    sorted_aliases = sorted(KHL_ALL_ALIASES.keys(), key=len, reverse=True)
    for alias in sorted_aliases:
        if alias in low:
            return KHL_ALL_ALIASES[alias]

    return None


def is_mg(name: str) -> bool:
    """Проверяет, является ли название Металлургом Мг."""
    if not name:
        return False
    return canonicalize_team(name) == "Металлург Мг"


def is_valid_khl_team(name: str) -> bool:
    """Проверяет, является ли название командой КХЛ."""
    return canonicalize_team(name) is not None


def validate_match_pair(home: str, away: str) -> Optional[Tuple[str, str]]:
    """
    Валидирует пару команд.
    Возвращает (canonical_home, canonical_away) или None если невалидно.
    Проверяет:
    1. Обе команды — из КХЛ
    2. Металлург — одна из них
    3. Они не одинаковые
    """
    c_home = canonicalize_team(home)
    c_away = canonicalize_team(away)

    if not c_home or not c_away:
        logger.debug("Невалидная пара: '%s' -> %s, '%s' -> %s", home, c_home, away, c_away)
        return None

    if c_home == c_away:
        logger.debug("Одинаковые команды: %s vs %s", c_home, c_away)
        return None

    # Металлург должен быть одной из команд
    if c_home != "Металлург Мг" and c_away != "Металлург Мг":
        logger.debug("Металлург не участвует: %s vs %s", c_home, c_away)
        return None

    return (c_home, c_away)


def find_opponent_in_text(text: str) -> Optional[str]:
    """Ищет название команды-соперника КХЛ в тексте (не Металлург)."""
    low = text.lower()
    # Сортируем по длине алиаса (длинные первые) для точности
    sorted_aliases = sorted(KHL_ALL_ALIASES.keys(), key=len, reverse=True)
    for alias in sorted_aliases:
        canonical = KHL_ALL_ALIASES[alias]
        if canonical == "Металлург Мг":
            continue
        if alias in low:
            return canonical
    return None


# ─────────────────────────── ВАЛИДАЦИЯ СЧЁТА ─────────────────────

def validate_score(score: str) -> Optional[str]:
    """Валидирует формат счёта. Возвращает нормализованный 'X:Y' или None."""
    if not score:
        return None
    m = re.match(r'^\s*(\d{1,2})\s*[:\-–]\s*(\d{1,2})\s*$', score)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    # Хоккейный счёт: обычно до 10-12 максимум
    if a > 15 or b > 15:
        logger.warning("Подозрительный счёт: %d:%d", a, b)
        return None
    return f"{a}:{b}"


def is_score_progression_valid(old_score: str, new_score: str) -> bool:
    """
    Проверяет, что изменение счёта логично:
    - Счёт может измениться только на +1 для одной из команд
    - Счёт не может уменьшиться
    """
    if not old_score or not new_score:
        return True

    old_m = re.match(r'(\d+):(\d+)', old_score)
    new_m = re.match(r'(\d+):(\d+)', new_score)
    if not old_m or not new_m:
        return True

    oa, ob = int(old_m.group(1)), int(old_m.group(2))
    na, nb = int(new_m.group(1)), int(new_m.group(2))

    # Счёт не может уменьшиться
    if na < oa or nb < ob:
        logger.warning("Счёт уменьшился: %s -> %s", old_score, new_score)
        return False

    # За один интервал проверки (30 сек) может быть максимум 1 гол (обычно)
    # Но разрешим до 2 на случай пропущенного обновления
    diff_a = na - oa
    diff_b = nb - ob
    total_diff = diff_a + diff_b

    if total_diff > 3:
        logger.warning("Подозрительное изменение счёта: %s -> %s (diff=%d)",
                      old_score, new_score, total_diff)
        return False

    # Не могут забить обе команды одновременно (за 30 сек это крайне маловероятно)
    if diff_a > 0 and diff_b > 0 and total_diff > 2:
        logger.warning("Обе команды забили: %s -> %s", old_score, new_score)
        return False

    return True


# ─────────────────────────── ПЕРЕКРЁСТНАЯ ВАЛИДАЦИЯ ──────────────

class CrossValidator:
    """Перекрёстная валидация данных из разных источников."""

    # Уровни доверия к источникам
    SOURCE_TRUST = {
        "khl_api": 0.95,
        "khl": 0.90,
        "metallurg_site": 0.85,
        "metallurg_api": 0.85,
        "metallurg_telegram": 0.70,
    }

    @staticmethod
    def validate_match_data(candidates: List[Dict[str, Any]]) -> Optional[ValidatedMatch]:
        """
        Валидирует данные матча из нескольких источников.
        Возвращает ValidatedMatch с оценкой достоверности.
        """
        if not candidates:
            return None

        # Фильтруем только валидные матчи (обе команды КХЛ, Металлург участвует)
        valid_candidates = []
        for c in candidates:
            home_raw = c.get("home", "")
            away_raw = c.get("away", "")

            pair = validate_match_pair(home_raw, away_raw)
            if pair:
                c["canonical_home"] = pair[0]
                c["canonical_away"] = pair[1]
                valid_candidates.append(c)
            else:
                logger.info("Отброшен невалидный кандидат: '%s' vs '%s' (источник: %s)",
                          home_raw, away_raw, c.get("source", "?"))

        if not valid_candidates:
            logger.info("Нет валидных кандидатов матча после проверки пар команд")
            return None

        # Группируем по парам команд
        pair_groups: Dict[Tuple[str, str], List[Dict]] = {}
        for c in valid_candidates:
            # Нормализуем порядок для группировки
            pair = tuple(sorted([c["canonical_home"], c["canonical_away"]]))
            pair_groups.setdefault(pair, []).append(c)

        # Выбираем пару с наибольшим количеством подтверждений
        best_pair = max(pair_groups.keys(), key=lambda p: len(pair_groups[p]))
        best_group = pair_groups[best_pair]

        if len(pair_groups) > 1:
            logger.warning("Разные пары команд из разных источников: %s",
                         {str(k): len(v) for k, v in pair_groups.items()})
            # Если пара подтверждена только одним ненадёжным источником — отбрасываем
            if len(best_group) == 1:
                src = best_group[0].get("source", "")
                trust = CrossValidator.SOURCE_TRUST.get(src, 0.5)
                if trust < 0.8:
                    logger.warning("Единственный кандидат от ненадёжного источника %s, пропускаем", src)
                    return None

        # Определяем home/away (кто хозяин)
        # Берём из самого надёжного источника
        best_group.sort(key=lambda c: CrossValidator.SOURCE_TRUST.get(c.get("source", ""), 0.5),
                       reverse=True)
        primary = best_group[0]

        canonical_home = primary["canonical_home"]
        canonical_away = primary["canonical_away"]

        # Валидация счёта
        scores = []
        for c in best_group:
            score = validate_score(c.get("score", ""))
            if score:
                src = c.get("source", "")
                trust = CrossValidator.SOURCE_TRUST.get(src, 0.5)
                scores.append((score, trust, src))

        validated_score = ""
        confidence = 0.5

        if scores:
            # Группируем одинаковые счета
            score_groups: Dict[str, float] = {}
            for score, trust, src in scores:
                score_groups[score] = score_groups.get(score, 0) + trust

            # Берём счёт с наибольшей суммой доверия
            best_score = max(score_groups.keys(), key=lambda s: score_groups[s])
            total_trust = score_groups[best_score]

            # Если есть разногласия по счёту
            if len(score_groups) > 1:
                logger.warning("Разные счета из разных источников: %s", score_groups)
                # Берём только если перевес значительный
                sorted_scores = sorted(score_groups.items(), key=lambda x: x[1], reverse=True)
                if len(sorted_scores) >= 2:
                    if sorted_scores[0][1] < sorted_scores[1][1] * 1.5:
                        logger.warning("Нет явного победителя по счёту, пропускаем обновление")
                        # Всё равно возвращаем, но с низкой уверенностью
                        confidence = 0.3
                    else:
                        confidence = min(0.9, total_trust / 2)
                validated_score = best_score
            else:
                validated_score = best_score
                # Уверенность зависит от количества подтверждений
                if len(scores) >= 2:
                    confidence = min(0.95, total_trust / 2)
                else:
                    confidence = CrossValidator.SOURCE_TRUST.get(scores[0][2], 0.5)

        # Валидация is_live
        live_votes = sum(1 for c in best_group if c.get("is_live"))
        not_live_votes = len(best_group) - live_votes
        is_live = live_votes > not_live_votes

        # Если только один источник и он ненадёжный — нужно live подтверждение
        if len(best_group) == 1 and not is_live:
            src = best_group[0].get("source", "")
            if CrossValidator.SOURCE_TRUST.get(src, 0.5) < 0.8:
                confidence *= 0.7

        # Период — берём максимальный из надёжных источников
        period = 0
        for c in best_group:
            p = c.get("period", 0)
            if isinstance(p, int) and p > period:
                period = p

        return ValidatedMatch(
            home=canonical_home,
            away=canonical_away,
            score=validated_score,
            is_live=is_live,
            period=period,
            source=primary.get("source", ""),
            confidence=confidence,
            raw_home=primary.get("home", ""),
            raw_away=primary.get("away", ""),
        )

    @staticmethod
    def validate_score_change(current: str, new_score: str,
                              match: ValidatedMatch) -> bool:
        """Проверяет допустимость изменения счёта."""
        if not current:
            return True

        if not is_score_progression_valid(current, new_score):
            return False

        # Проверяем уверенность
        if match.confidence < 0.5:
            logger.warning("Низкая уверенность в счёте (%.2f), не обновляем", match.confidence)
            return False

        return True


cross_validator = CrossValidator()

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
# ██  ПАРСИНГ metallurg.ru (с валидацией)
# ═══════════════════════════════════════════════════════════════════

def parse_metallurg_site() -> Optional[Dict[str, Any]]:
    """
    Глубокий парсинг metallurg.ru — множественные подходы.
    Каждый результат проходит валидацию пар команд.
    """
    result = _parse_metallurg_main_deep()
    if result and _is_valid_result(result):
        logger.info("metallurg.ru [main]: %s", result)
        return result

    sub_paths = [
        "/matches/", "/schedule/", "/games/", "/calendar/",
        "/team/matches/", "/hockey/matches/",
        "/match/", "/game/",
        "/results/",
    ]
    for path in sub_paths:
        r = _parse_metallurg_page(f"https://metallurg.ru{path}")
        if r and _is_valid_result(r):
            logger.info("metallurg.ru [%s]: %s", path, r)
            return r

    r = _try_metallurg_api()
    if r and _is_valid_result(r):
        logger.info("metallurg.ru [api]: %s", r)
        return r

    if result and _is_valid_result(result):
        return result

    return None


def _is_valid_result(result: Dict[str, Any]) -> bool:
    """Проверяет, что результат парсинга валиден."""
    home = result.get("home", "")
    away = result.get("away", "")

    # Обе команды должны быть из КХЛ
    c_home = canonicalize_team(home)
    c_away = canonicalize_team(away)

    if not c_home or not c_away:
        return False

    if c_home == c_away:
        return False

    # Металлург должен участвовать
    if c_home != "Металлург Мг" and c_away != "Металлург Мг":
        return False

    # Проверяем счёт если есть
    score = result.get("score", "")
    if score:
        validated = validate_score(score)
        if not validated:
            return False

    return True


def _parse_metallurg_main_deep() -> Optional[Dict[str, Any]]:
    html = fetch_page("https://metallurg.ru/")
    if not html:
        return None
    return _deep_parse_html(html, "metallurg_site")


def _parse_metallurg_page(url: str) -> Optional[Dict[str, Any]]:
    html = fetch_page(url)
    if not html:
        return None
    return _deep_parse_html(html, "metallurg_site")


def _deep_parse_html(html: str, source: str) -> Optional[Dict[str, Any]]:
    """
    Полный глубокий парсинг HTML — ищет матч Металлурга.
    ВАЖНО: ищет именно пару команд в одном блоке, а не отдельные упоминания.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── A) Ищем JSON в <script> тегах ──
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        result = _extract_match_from_js(text, source)
        if result and _is_valid_result(result):
            return result

    # ── B) Ищем по CSS классам, связанным с матчем ──
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
            result = _try_extract_match_strict(text, source)
            if result and _is_valid_result(result):
                return result

    # ── C) Ищем по id ──
    match_ids = [
        "match", "game", "score", "widget", "live",
        "scoreboard", "result", "current-match",
    ]
    for mid in match_ids:
        tag = soup.find(id=re.compile(mid, re.I))
        if tag:
            text = tag.get_text(separator=" ", strip=True)
            result = _try_extract_match_strict(text, source)
            if result and _is_valid_result(result):
                return result

    # ── D) Ищем контейнеры с ДВУМЯ командами рядом ──
    # Это критично: ищем блок, где упоминаются ОБЕ команды пары
    for tag in soup.find_all(["div", "section", "article", "table", "tr"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) < 5 or len(text) > 500:
            continue

        # Проверяем, что Металлург + другая команда КХЛ в одном блоке
        if not any(a in text.lower() for a in MG_ALIASES):
            continue

        opponent = find_opponent_in_text(text)
        if opponent:
            result = _try_extract_match_strict(text, source)
            if result and _is_valid_result(result):
                return result

    return None


def _extract_match_from_js(js_text: str, source: str) -> Optional[Dict[str, Any]]:
    """Извлекает данные матча из JavaScript кода."""
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
                if result and _is_valid_result(result):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

    return None


def _parse_json_data(data: Any, source: str) -> Optional[Dict[str, Any]]:
    """Пытается распарсить JSON-данные о матче."""
    if isinstance(data, dict):
        data_str = json.dumps(data, ensure_ascii=False).lower()
        if not any(a in data_str for a in MG_ALIASES):
            return None

        for key in ["matches", "games", "data", "items", "result", "events", "schedule"]:
            if key in data and isinstance(data[key], list):
                result = _find_match_in_list(data[key], source)
                if result:
                    return result

        result = _try_parse_single_match(data, source)
        if result:
            return result

    elif isinstance(data, list):
        return _find_match_in_list(data, source)

    return None


def _find_match_in_list(matches: list, source: str) -> Optional[Dict[str, Any]]:
    """Ищет матч Металлурга в списке с валидацией."""
    today = datetime.now().strftime("%Y-%m-%d")
    best = None

    for m in matches:
        if not isinstance(m, dict):
            continue

        m_str = json.dumps(m, ensure_ascii=False).lower()
        if not any(a in m_str for a in MG_ALIASES):
            continue

        result = _try_parse_single_match(m, source)
        if not result or not _is_valid_result(result):
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
    """Парсит один матч из JSON с валидацией пар."""
    home = ""
    away = ""

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

    # Валидация пары
    pair = validate_match_pair(home, away)
    if not pair:
        return None

    # Счёт
    score = ""
    if "score" in m:
        score = validate_score(str(m["score"])) or ""
    else:
        sa = m.get("score_a", m.get("home_score", m.get("score_home", "")))
        sb = m.get("score_b", m.get("away_score", m.get("score_away", "")))
        if sa != "" and sb != "":
            score = validate_score(f"{sa}:{sb}") or ""

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
        "home": pair[0],
        "away": pair[1],
        "score": score,
        "is_live": is_live,
        "period": period,
        "source": source,
    }


def _try_extract_match_strict(text: str, source: str) -> Optional[Dict[str, Any]]:
    """
    Строгое извлечение матча из текстового блока.
    Требует наличие ДВУХ команд КХЛ в тексте.
    """
    if len(text) < 5 or len(text) > 1000:
        return None
    if not any(a in text.lower() for a in MG_ALIASES):
        return None

    teams = _extract_teams_from_text_strict(text)
    if not teams:
        return None

    pair = validate_match_pair(teams[0], teams[1])
    if not pair:
        return None

    score_m = re.search(r'(\d{1,2})\s*[:\-–]\s*(\d{1,2})', text)
    score = ""
    if score_m:
        score = validate_score(f"{score_m.group(1)}:{score_m.group(2)}") or ""

    is_live = _check_if_live_text(text)
    period = _extract_period_text(text)

    return {
        "home": pair[0],
        "away": pair[1],
        "score": score,
        "is_live": is_live,
        "period": period,
        "source": source,
    }


def _try_metallurg_api() -> Optional[Dict[str, Any]]:
    """Пробуем разные API-эндпоинты metallurg.ru."""
    api_urls = [
        "https://metallurg.ru/api/matches/",
        "https://metallurg.ru/api/v1/matches/",
        "https://metallurg.ru/api/v2/matches/",
        "https://metallurg.ru/local/api/matches.php",
        "https://metallurg.ru/api/schedule/",
        "https://metallurg.ru/api/games/",
        "https://metallurg.ru/ajax/matches/",
        "https://metallurg.ru/ajax/schedule/",
    ]
    for url in api_urls:
        data = fetch_json(url)
        if data:
            logger.info("metallurg API hit: %s → %s", url, str(data)[:200])
            result = _parse_json_data(data, "metallurg_api")
            if result and _is_valid_result(result):
                return result
    return None


# ═══════════════════════════════════════════════════════════════════
# ██  ПАРСИНГ khl.ru (с валидацией)
# ═══════════════════════════════════════════════════════════════════

def parse_khl() -> Optional[Dict[str, Any]]:
    """Глубокий парсинг khl.ru с валидацией."""
    result = _parse_khl_main()
    if result and _is_valid_result(result):
        return result

    result = _parse_khl_schedule()
    if result:
        return result

    result = _parse_khl_api()
    if result:
        return result

    result = _parse_khl_team_page()
    if result:
        return result

    return None


def _parse_khl_main() -> Optional[Dict[str, Any]]:
    html = fetch_page("https://www.khl.ru/")
    if not html:
        html = fetch_page("https://khl.ru/")
    if not html:
        return None
    return _deep_parse_html(html, "khl")


def _parse_khl_schedule() -> Optional[Dict[str, Any]]:
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")

    urls = [
        f"https://www.khl.ru/calendar/{date_str}/",
        "https://www.khl.ru/calendar/",
        "https://www.khl.ru/schedule/",
        "https://www.khl.ru/games/",
    ]
    for url in urls:
        html = fetch_page(url)
        if not html:
            continue
        result = _deep_parse_html(html, "khl")
        if result and _is_valid_result(result):
            return result
    return None


def _parse_khl_api() -> Optional[Dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")

    api_urls = [
        f"https://khl.api.webcaster.pro/api/khl_mobile/events_v2.json?q[start_at_from_date]={today}",
        f"https://www.khl.ru/api/events/?date={today}",
        "https://www.khl.ru/api/events/today/",
    ]

    for url in api_urls:
        data = fetch_json(url)
        if data:
            logger.info("khl API hit: %s → %s", url, str(data)[:200])
            result = _parse_json_data(data, "khl_api")
            if result and _is_valid_result(result):
                return result
    return None


def _parse_khl_team_page() -> Optional[Dict[str, Any]]:
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
        if result and _is_valid_result(result):
            return result
    return None


# ═══════════════════════════════════════════════════════════════════
# ██  Telegram парсер
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


# ═══════════════════════════════════════════════════════════════════
# ██  УТИЛИТЫ ПАРСИНГА
# ═══════════════════════════════════════════════════════════════════

def _extract_teams_from_text_strict(text: str) -> Optional[List[str]]:
    """
    Строго извлекает два названия команд из текста.
    Оба должны быть из КХЛ. Одна — Металлург.
    """
    low = text.lower()

    # Ищем все команды КХЛ в тексте
    found_teams = []
    sorted_aliases = sorted(KHL_ALL_ALIASES.keys(), key=len, reverse=True)
    used_spans = []  # Чтобы не находить одно и то же дважды

    for alias in sorted_aliases:
        pos = low.find(alias)
        if pos == -1:
            continue

        # Проверяем что этот span не перекрывается с уже найденным
        end_pos = pos + len(alias)
        overlap = False
        for s, e in used_spans:
            if not (end_pos <= s or pos >= e):
                overlap = True
                break
        if overlap:
            continue

        canonical = KHL_ALL_ALIASES[alias]
        # Не добавляем дубликаты
        if canonical not in [t[0] for t in found_teams]:
            found_teams.append((canonical, pos))
            used_spans.append((pos, end_pos))

    if len(found_teams) < 2:
        return None

    # Металлург должен быть среди найденных
    mg_found = any(t[0] == "Металлург Мг" for t in found_teams)
    if not mg_found:
        return None

    # Сортируем по позиции в тексте
    found_teams.sort(key=lambda x: x[1])

    # Берём первые две разные команды
    return [found_teams[0][0], found_teams[1][0]]


def _extract_teams_from_text(text: str) -> Optional[List[str]]:
    """Обёртка для совместимости. Использует строгий метод."""
    return _extract_teams_from_text_strict(text)


def _check_if_live_text(text: str) -> bool:
    keywords = [
        "live", "онлайн", "идёт", "идет", "прямой",
        "сейчас", "текущий", "в эфире", "трансляция",
        "online", "playing", "in progress",
    ]
    text_low = text.lower()
    return any(kw in text_low for kw in keywords)


def _extract_period_text(text: str) -> int:
    m = re.search(r'(\d)\s*[-\s]?\s*(?:период|пер|per)', text.lower())
    if m:
        return int(m.group(1))
    if "от" in text.lower() or "овертайм" in text.lower() or "overtime" in text.lower():
        return 4
    if "булл" in text.lower() or "shootout" in text.lower():
        return 5
    m = re.search(r'(\d)\s*п(?:ер)?', text.lower())
    if m:
        return int(m.group(1))
    return 0


def _extract_score(text: str) -> Optional[str]:
    m = re.search(r'(\d{1,2})\s*[:\-–]\s*(\d{1,2})', text)
    if m:
        return validate_score(f"{m.group(1)}:{m.group(2)}")
    return None


# ═══════════════════════════════════════════════════════════════════
# ██  СБОРЩИК ДАННЫХ (без VK, ТВ-ИН, ОТВ)
# ═══════════════════════════════════════════════════════════════════

class Collector:
    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.ok: Dict[str, bool] = {
            "metallurg_site": False,
            "khl": False,
            "metallurg_telegram": False,
        }
        self.names = {
            "metallurg_site": "🌐 Metallurg.ru",
            "metallurg_api": "🌐 Metallurg.ru API",
            "khl": "🏒 KHL.ru",
            "khl_api": "🏒 KHL.ru API",
            "metallurg_telegram": "📱 Telegram @metallurgmgn",
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

        return self.results

    def get_match(self) -> Optional[ValidatedMatch]:
        """
        Лучшие данные о матче с перекрёстной валидацией.
        Возвращает ValidatedMatch или None.
        """
        candidates = []

        # Сайт metallurg
        site = self.results.get("metallurg_site")
        if site and isinstance(site, dict) and _is_valid_result(site):
            candidates.append(site)

        # KHL
        khl = self.results.get("khl")
        if khl and isinstance(khl, dict) and _is_valid_result(khl):
            candidates.append(khl)

        # Из постов Telegram
        posts = self.results.get("metallurg_telegram", [])
        if isinstance(posts, list):
            for post in reversed(posts):
                text = post.get("text", "")
                score = _extract_score(text)
                if score and any(a in text.lower() for a in MG_ALIASES):
                    teams = _extract_teams_from_text_strict(text)
                    if teams:
                        pair = validate_match_pair(teams[0], teams[1])
                        if pair:
                            candidate = {
                                "home": pair[0],
                                "away": pair[1],
                                "score": score,
                                "is_live": True,
                                "period": 0,
                                "source": "metallurg_telegram",
                            }
                            candidates.append(candidate)
                            break

        if not candidates:
            return None

        # Перекрёстная валидация
        validated = cross_validator.validate_match_data(candidates)
        return validated

    def get_social_posts(self) -> List[Dict[str, str]]:
        posts = self.results.get("metallurg_telegram", [])
        if isinstance(posts, list):
            return posts
        return []


collector = Collector()

# ─────────────────────────── ПЕРЕСЫЛКА ПОСТОВ ────────────────────

async def forward_metallurg_posts():
    """Пересылает новые посты из канала @metallurgmgn в установленный канал."""
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
        "• 📱 Telegram @metallurgmgn\n\n"
        "🛡 <b>Валидация:</b>\n"
        "• Перекрёстная проверка счёта между источниками\n"
        "• Проверка пар команд (обе из КХЛ)\n"
        "• Проверка логичности изменения счёта\n\n"
        "📢 Бот автоматически пересылает посты из TG-канала Металлурга!\n\n"
        "<b>Команды:</b>\n"
        "/setchannel — привязать канал\n"
        "/status — статус бота\n"
        "/score — текущий счёт\n"
        "/penalties — штрафы\n"
        "/sources — источники\n"
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

    confidence_str = "—"
    if S.last_validated_match:
        confidence_str = f"{S.last_validated_match.confidence:.0%}"

    lines = [
        "🏒 <b>Статус</b>\n",
        f"📺 Канал: <b>{ch}</b>",
        f"🔴 Матч: <b>{'идёт' if S.is_live else 'нет'}</b>",
        f"🏠 {S.home_team or '—'} vs 🏃 {S.away_team or '—'}",
        f"📊 Счёт: <b>{S.score or '—'}</b>",
        f"🛡 Достоверность: <b>{confidence_str}</b>",
        f"👥 На льду: <b>{tr.strength()}</b>",
        f"📡 Источники: {active}/{total}",
        f"📢 Переслано постов: {len(S.forwarded_post_ids)}",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("score"))
async def cmd_score(msg: Message):
    wait_msg = await msg.answer("🔄 Проверяю все источники...")

    await collector.check_all()
    match = collector.get_match()

    if not match:
        await wait_msg.edit_text(
            "⚠️ Матч Металлурга не найден.\n\n"
            "Возможно, сейчас нет игры или источники недоступны.\n\n"
            "🔍 Проверены:\n"
            "• metallurg.ru (глубокий парсинг)\n"
            "• khl.ru (глубокий парсинг)\n"
            "• Telegram @metallurgmgn\n\n"
            "Попробуйте /forcecheck")
        return

    home = match.home
    away = match.away
    score = match.score or "—"
    src_name = collector.names.get(match.source, match.source)
    is_live = match.is_live
    period = match.period

    live_str = "🔴 LIVE" if is_live else "⚪ Не начался" if not match.score else "🏁 Завершён"
    period_str = PERIODS.get(period, "") if period else ""

    pen_block = fmt_active_pen(home, away)

    confidence_str = f"{match.confidence:.0%}"

    text = (
        f"🏒 <b>{home}</b>  {score}  <b>{away}</b>\n\n"
        f"{live_str}"
        f"{f'  |  {period_str}' if period_str else ''}\n"
        f"👥 На льду: <b>{S.penalties.strength()}</b>\n"
        f"🛡 Достоверность: <b>{confidence_str}</b>"
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
    }
    for sid in collector.ok:
        name = collector.names.get(sid, sid)
        ok = "✅" if collector.ok.get(sid) else "❌"
        url = urls.get(sid, "")
        lines.append(f"{ok} <b>{name}</b>\n   └ {url}")

    lines.append(f"\n📊 Активных: "
                 f"{sum(1 for v in collector.ok.values() if v)}/{len(collector.ok)}")
    lines.append(f"\n🛡 <b>Валидация:</b>")
    lines.append(f"   • Перекрёстная проверка счёта")
    lines.append(f"   • Проверка пар команд КХЛ")
    lines.append(f"   • Проверка логичности счёта")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("teams"))
async def cmd_teams(msg: Message):
    """Список команд КХЛ."""
    teams_list = sorted(KHL_CANONICAL_TEAMS.keys())
    lines = ["🏒 <b>Команды КХЛ (сезон):</b>\n"]
    for i, team in enumerate(teams_list, 1):
        icon = "⭐" if team == "Металлург Мг" else "🏒"
        lines.append(f"{icon} {i}. {team}")

    lines.append(f"\n📊 Всего: {len(teams_list)} команд")
    lines.append("💡 Бот ищет соперника Металлурга среди всех этих команд.")
    lines.append("🛡 Обе команды матча валидируются по этому списку.")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("forcecheck"))
async def cmd_forcecheck(msg: Message):
    wait_msg = await msg.answer("🔄 Глубокая проверка всех источников с валидацией...")

    results = await collector.check_all()

    lines = ["📊 <b>Результаты глубокой проверки:</b>\n"]

    # Сайт metallurg.ru
    site = results.get("metallurg_site")
    if site:
        lines.append(f"✅ <b>metallurg.ru:</b> {site.get('home', '?')} "
                     f"{site.get('score', '—')} {site.get('away', '?')}")
        lines.append(f"   Live: {site.get('is_live')}, Период: {site.get('period')}")
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

    # Валидированный матч
    match = collector.get_match()
    if match:
        lines.append(f"\n🏒 <b>Валидированный матч:</b>")
        lines.append(f"   {match.home} {match.score or '—'} {match.away}")
        lines.append(f"   Live: {match.is_live}, Период: {match.period}")
        lines.append(f"   🛡 Достоверность: <b>{match.confidence:.0%}</b>")
        lines.append(f"   📡 Источник: {collector.names.get(match.source, match.source)}")
    else:
        lines.append("\n⚠️ Матч Металлурга не определён после валидации")
        # Показываем почему
        all_candidates = []
        for key in ("metallurg_site", "khl"):
            data = results.get(key)
            if data and isinstance(data, dict):
                all_candidates.append(data)
        if all_candidates:
            lines.append("\n🔍 <b>Отброшенные кандидаты:</b>")
            for c in all_candidates:
                h = c.get("home", "?")
                a = c.get("away", "?")
                pair = validate_match_pair(h, a)
                status = "✅" if pair else "❌"
                reason = ""
                if not pair:
                    ch = canonicalize_team(h)
                    ca = canonicalize_team(a)
                    if not ch:
                        reason = f"'{h}' не КХЛ команда"
                    elif not ca:
                        reason = f"'{a}' не КХЛ команда"
                    elif ch == ca:
                        reason = "одинаковые команды"
                    elif ch != "Металлург Мг" and ca != "Металлург Мг":
                        reason = "Металлург не участвует"
                lines.append(f"   {status} {h} vs {a} {f'({reason})' if reason else ''}")

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

    # Дополнительная проверка логичности
    if not is_score_progression_valid(old, new):
        logger.warning("Отброшено невалидное изменение счёта: %s -> %s", old, new)
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

    opponent = away if is_mg(home) else home if is_mg(away) else ""
    if opponent:
        parts.append(f"🆚 Соперник: <b>{opponent}</b>")

    if not tr.equal():
        parts.append(f"👥 На льду: <b>{tr.strength()}</b>")
    pen = fmt_active_pen(home, away)
    if pen:
        parts.append(pen)

    # Записываем в историю счёта
    S.score_history.append((new, time.time(), source))

    await send("\n".join(parts), source)

# ─────────────────────────── WATCHER ─────────────────────────────

async def watcher():
    logger.info("🏒 Watcher запущен (глубокий парсинг с валидацией)")

    while True:
        try:
            results = await collector.check_all()

            # ── Пересылка постов ──
            await forward_metallurg_posts()

            if not results:
                await asyncio.sleep(IDLE_INTERVAL)
                continue

            # ── Данные о матче с валидацией ──
            match = collector.get_match()

            if match:
                home = match.home
                away = match.away
                score = match.score
                is_live = match.is_live
                period = match.period
                source = match.source

                S.home_team = home
                S.away_team = away
                S.last_validated_match = match

                logger.info("Валидированный матч: %s %s %s (confidence: %.0f%%, source: %s)",
                          home, score or "—", away, match.confidence * 100, source)

                # Начало матча
                if is_live and not S.is_live:
                    S.is_live = True
                    if not S.notified_start:
                        opponent = away if is_mg(home) else home
                        await send(
                            f"{random.choice(START)}\n\n"
                            f"🏒 <b>{home}</b> vs <b>{away}</b>\n"
                            f"🆚 Соперник: <b>{opponent}</b>",
                            source)
                        S.notified_start = True

                # Период
                if period > 0 and period != S.period:
                    pt = PERIODS.get(period, f"▶️ Период {period}")
                    await send(f"{pt}\n🏒 {home} <b>{score}</b> {away}", source)
                    S.period = period

                # Счёт — с валидацией прогрессии
                if score and score != S.score and S.score:
                    if cross_validator.validate_score_change(S.score, score, match):
                        await on_score_change(home, away, S.score, score, source)
                    else:
                        logger.warning("Изменение счёта %s -> %s отклонено валидацией",
                                     S.score, score)

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

            # ── Посты из Telegram — штрафы ──
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
                            home_team = S.home_team or "Металлург Мг"
                            away_team = S.away_team or "Соперник"
                            is_home_mg = is_mg(home_team)
                            team = home_team if is_home_mg else away_team
                            S.penalties.add(team, player, mins, "нарушение",
                                           S.period or 1, "??:??", is_home_mg)
                            emoji = "😤" if is_mg(team) else "😏"
                            tr = S.penalties
                            await send(
                                f"{random.choice(PEN)} {emoji}\n\n"
                                f"🏒 {team} — <b>{player}</b>\n"
                                f"⏱ {mins} мин\n"
                                f"👥 На льду: <b>{tr.strength()}</b>",
                                src)
                        break

                # Счёт из поста — тоже с валидацией
                score_from_post = _extract_score(text)
                if score_from_post and S.score and score_from_post != S.score:
                    if is_score_progression_valid(S.score, score_from_post):
                        home_team = S.home_team or "Металлург Мг"
                        away_team = S.away_team or "Соперник"
                        await on_score_change(home_team, away_team,
                                            S.score, score_from_post, src)
                        S.score = score_from_post
                    else:
                        logger.warning("Счёт из поста %s -> %s отклонён",
                                     S.score, score_from_post)

        except Exception as e:
            logger.exception("Watcher error: %s", e)

        interval = CHECK_INTERVAL if S.is_live else IDLE_INTERVAL
        await asyncio.sleep(interval)

# ─────────────────────────── MAIN ────────────────────────────────

async def main():
    me = await bot.get_me()
    logger.info("🏒 Бот @%s запущен (глубокий парсинг с валидацией)", me.username)
    logger.info("📡 Источники: metallurg.ru, khl.ru, Telegram")
    logger.info("🏒 Команды КХЛ: %d", len(KHL_CANONICAL_TEAMS))
    logger.info("🛡 Валидация: перекрёстная проверка, проверка пар, проверка счёта")

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
