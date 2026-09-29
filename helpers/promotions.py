import math
import random
import statistics
from typing import Optional

from helpers.models import Account, ActivePromotion

MIN_SHOW_CHANCE = 0.20
MAX_SHOW_CHANCE = 0.75
DEFAULT_SHOW_CHANCE = 0.60
GUEST_SHOW_CHANCE = 0.60
# click rate at which a logged-in user reaches the max show chance
HIGH_CLICK_RATE = 0.25
# ad views before the user's own behavior fully sets their chance
CONFIDENCE_VIEWS = 20
# opened levels before a level range is considered "decided"
MIN_OPENS_FOR_RANGE = 50
# above this spread the opens are too scattered to imply a preferred range
MAX_RANGE_SIGMA = 10.0
# a promotion below the range is punished slightly less than one the same distance above
BELOW_RANGE_SIGMA_FACTOR = 1.2
# per-user times a promotion is shown before its rate is halved / quartered / stopped
FREQUENCY_HALF_AT = 5
FREQUENCY_QUARTER_AT = 25
FREQUENCY_STOP_AT = 50

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
    confidence = min(1.0, views / CONFIDENCE_VIEWS)
    chance = DEFAULT_SHOW_CHANCE * (1 - confidence) + computed * confidence
    return min(MAX_SHOW_CHANCE, max(MIN_SHOW_CHANCE, chance))


def _level_center_and_sigma(levels: list[int]) -> Optional[tuple[float, float]]:
    if not levels or len(levels) < MIN_OPENS_FOR_RANGE:
        return None
    center = float(statistics.median(levels))
    srt = sorted(levels)
    n = len(srt)
    iqr = srt[(3 * n) // 4] - srt[n // 4]
    sigma = iqr / 1.349
    if sigma > MAX_RANGE_SIGMA:
        return None
    return center, max(4.0, sigma)


def _relevance(rating: int, center: float, sigma: float) -> float:
    d = rating - center
    if d < 0:
        sigma = sigma * BELOW_RANGE_SIGMA_FACTOR
    return math.exp(-(d * d) / (2 * sigma * sigma))


def _frequency_multiplier(shown: int) -> float:
    if shown >= FREQUENCY_STOP_AT:
        return 0.0
    if shown >= FREQUENCY_QUARTER_AT:
        return 0.25
    if shown >= FREQUENCY_HALF_AT:
        return 0.5
    return 1.0


def choose_promotion(
    account: Optional[Account],
    is_logged_in: bool,
    active: list[ActivePromotion],
    shown_counts: Optional[dict[int, int]] = None,
) -> Optional[ActivePromotion]:
    if not active:
        return None

    if not is_logged_in or account is None:
        if random.random() >= GUEST_SHOW_CHANCE:
            return None
        return random.choice(active)

    shown_counts = shown_counts or {}
    base = _base_show_chance(account)
    range_info = _level_center_and_sigma(account.recent_opened_levels or [])

    weights = []
    for p in active:
        weight = _frequency_multiplier(shown_counts.get(p.id, 0))
        if range_info is not None:
            weight *= _relevance(p.chart_rating, *range_info)
        weights.append(weight)

    best = max(weights)
    if best <= 0 or random.random() >= base * best:
        return None
    return random.choices(active, weights=weights, k=1)[0]
