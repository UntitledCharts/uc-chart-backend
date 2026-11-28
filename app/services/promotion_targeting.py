import math
import random
import secrets
import statistics

from app.database.accounts import AccountRow
from app.database.promotions import ActivePromotionRow
from app.settings import PromotionConstants

VIEW_CODE_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def generate_view_code(length: int) -> str:
    """Generate a short view code for click attribution.

    Args:
        length: Number of characters.

    Returns:
        str: The code.
    """
    return "".join(secrets.choice(VIEW_CODE_ALPHABET) for _ in range(length))


def base_show_chance(account: AccountRow, constants: PromotionConstants) -> float:
    """Chance of showing a promotion to an account, from its click history.

    Args:
        account: The viewing account.
        constants: Promotion constants.

    Returns:
        float: Show chance between the configured bounds.
    """
    views = account.ad_views or 0
    if views == 0:
        return constants.default_show_chance
    click_rate = (account.ad_clicks or 0) / views
    computed = constants.min_show_chance + (click_rate / constants.high_click_rate) * (
        constants.max_show_chance - constants.min_show_chance
    )
    computed = min(constants.max_show_chance, max(constants.min_show_chance, computed))
    confidence = min(1.0, views / constants.confidence_views)
    chance = constants.default_show_chance * (1 - confidence) + computed * confidence
    return min(constants.max_show_chance, max(constants.min_show_chance, chance))


def level_center_and_sigma(
    levels: list[int], constants: PromotionConstants
) -> tuple[float, float] | None:
    """Estimate an account's preferred rating range from opened charts.

    Args:
        levels: Ratings of recently opened charts.
        constants: Promotion constants.

    Returns:
        tuple[float, float] | None: Center and spread, or None when there is no clear preference.
    """
    if len(levels) < constants.min_opens_for_range:
        return None
    center = float(statistics.median(levels))
    ordered = sorted(levels)
    count = len(ordered)
    sigma = (ordered[(3 * count) // 4] - ordered[count // 4]) / 1.349
    if sigma > constants.max_range_sigma:
        return None
    return center, max(constants.min_range_sigma, sigma)


def relevance(rating: int, center: float, sigma: float, constants: PromotionConstants) -> float:
    """Weight of a promotion by how close its rating is to the preferred range.

    Args:
        rating: Promoted chart rating.
        center: Preferred rating.
        sigma: Preferred spread.
        constants: Promotion constants.

    Returns:
        float: Weight between 0 and 1.
    """
    distance = rating - center
    if distance < 0:
        sigma *= constants.below_range_sigma_factor
    return math.exp(-(distance * distance) / (2 * sigma * sigma))


def frequency_multiplier(shown: int, constants: PromotionConstants) -> float:
    """Weight reduction for promotions already shown often to an account.

    Args:
        shown: Times the promotion was shown to the account.
        constants: Promotion constants.

    Returns:
        float: Multiplier between 0 and 1.
    """
    if shown >= constants.frequency_stop_at:
        return 0.0
    if shown >= constants.frequency_quarter_at:
        return 0.25
    if shown >= constants.frequency_half_at:
        return 0.5
    return 1.0


def choose_promotion(
    account: AccountRow | None,
    active: list[ActivePromotionRow],
    shown_counts: dict[int, int],
    constants: PromotionConstants,
) -> ActivePromotionRow | None:
    """Pick a promotion to show, or none.

    Args:
        account: Viewing account, None for anonymous viewers.
        active: Promotions that can be shown.
        shown_counts: Times each promotion was shown to the account.
        constants: Promotion constants.

    Returns:
        ActivePromotionRow | None: The chosen promotion, or None.
    """
    if not active:
        return None
    if account is None:
        if random.random() >= constants.guest_show_chance:
            return None
        return random.choice(active)
    base = base_show_chance(account, constants)
    preferred = level_center_and_sigma(account.recent_opened_levels or [], constants)
    weights = []
    for promotion in active:
        weight = frequency_multiplier(shown_counts.get(promotion.id, 0), constants)
        if preferred is not None:
            weight *= relevance(promotion.chart_rating, *preferred, constants)
        weights.append(weight)
    best = max(weights)
    if best <= 0 or random.random() >= base * best:
        return None
    return random.choices(active, weights=weights, k=1)[0]
