import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("shipping.py")


def load_shipping(path: Path):
    spec = importlib.util.spec_from_file_location("shipping_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


shipping = load_shipping(MODULE_PATH)


@pytest.mark.parametrize(
    ("subtotal", "region", "expected"),
    [
        (98.99, "Zhejiang", False),
        (99, "Zhejiang", True),
        (120, "Zhejiang", True),
        (120, "Tibet", False),
        (120, "Hong Kong", False),
    ],
)
def test_free_shipping_rules(subtotal, region, expected):
    assert shipping.qualifies_for_free_shipping(subtotal, region) is expected


def test_negative_subtotal_is_rejected():
    with pytest.raises(ValueError):
        shipping.qualifies_for_free_shipping(-1, "Zhejiang")
