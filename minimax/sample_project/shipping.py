"""Shipping eligibility used by the MiniMax SDD sample."""


REMOTE_REGIONS = frozenset({"Tibet", "Hong Kong", "Macao", "Taiwan"})


def qualifies_for_free_shipping(subtotal_cny: float, region: str) -> bool:
    """Return whether an order is eligible under the current shipping contract."""

    if subtotal_cny < 0:
        raise ValueError("subtotal must be non-negative")
    if region in REMOTE_REGIONS:
        return False
    return subtotal_cny >= 99
