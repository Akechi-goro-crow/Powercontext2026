"""The pre-change implementation kept as an audit snapshot."""


def qualifies_for_free_shipping(subtotal_cny: float, region: str) -> bool:
    if subtotal_cny < 0:
        raise ValueError("subtotal must be non-negative")
    return subtotal_cny >= 99
