import math
import random
import statistics
from typing import Optional

from helpers.models import Account, ActivePromotion

MIN_SHOW_CHANCE = 0.15
MAX_SHOW_CHANCE = 0.60
DEFAULT_SHOW_CHANCE = 0.35
GUEST_SHOW_CHANCE = 0.35
# click rate at which a logged-in user reaches the max show chance
HIGH_CLICK_RATE = 0.25
# ad views before the user's own behavior fully sets their chance
CONFIDENCE_VIEWS = 20
# opened levels before a level range is considered "decided"
MIN_OPENS_FOR_RANGE = 20

VIEW_CODE_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
VIEW_CODE_LENGTH = 4


def generate_view_code() -> str:
    return "".join(random.choice(VIEW_CODE_ALPHABET) for _ in range(VIEW_CODE_LENGTH))


def _base_show_chance(account: Account) -> float:
    views = account.ad_views or 0
    if views == 0:
        return DEFAULT_SHOW_CHANCE
    click_rate = (account.ad_clicks or 0) / views
    computed = MIN_SHOW_CHANCE + (click_rate / HIGH_CLICK_RATE) * (
        MAX_SHOW_CHANCE - MIN_SHOW_CHANCE
    )
    computed = min(MAX_SHOW_CHANCE, max(MIN_SHOW_CHANCE, computed))
    # drift from the default toward the behaviour-based value as data builds up
    confidence = min(1.0, views / CONFIDENCE_VIEWS)
    chance = DEFAULT_SHOW_CHANCE * (1 - confidence) + computed * confidence
    return min(MAX_SHOW_CHANCE, max(MIN_SHOW_CHANCE, chance))


def _level_center_and_sigma(levels: list[int]) -> Optional[tuple[float, float]]:
    if not levels or len(levels) < MIN_OPENS_FOR_RANGE:
        return None
    center = float(statistics.median(levels))
    # robust spread from the IQR so occasional joke-chart outliers don't widen it
    srt = sorted(levels)
    n = len(srt)
    iqr = srt[(3 * n) // 4] - srt[n // 4]
    sigma = min(15.0, max(4.0, iqr / 1.349))
    return center, sigma


def _relevance(rating: int, center: float, sigma: float) -> float:
    d = rating - center
    return math.exp(-(d * d) / (2 * sigma * sigma))


def choose_promotion(
    account: Optional[Account],
    is_logged_in: bool,
    active: list[ActivePromotion],
) -> Optional[ActivePromotion]:
    """
    Pick a promotion to show, or None to show nothing this time. Guests get a flat
    chance and a uniform pick; logged-in users get a chance based on how often they
    click ads, and (once their level range is decided) promotions near that range.
    """
    if not active:
        return None

    if not is_logged_in or account is None:
        if random.random() >= GUEST_SHOW_CHANCE:
            return None
        return random.choice(active)

    base = _base_show_chance(account)
    range_info = _level_center_and_sigma(account.recent_opened_levels or [])
    if range_info is None:
        if random.random() >= base:
            return None
        return random.choice(active)

    center, sigma = range_info
    weights = [_relevance(p.chart_rating, center, sigma) for p in active]
    best = max(weights)
    if random.random() >= base * best:
        return None
    return random.choices(active, weights=weights, k=1)[0]
