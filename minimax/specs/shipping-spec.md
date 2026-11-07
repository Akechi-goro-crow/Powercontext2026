# Free shipping eligibility specification

## Scope

The checkout service decides whether an order qualifies for free shipping.

## Current rules

1. Orders with a merchandise subtotal of at least 99 CNY qualify.
2. Orders from Tibet, Hong Kong, Macao, and Taiwan do not qualify because the
   carrier contract does not cover those destinations.
3. The decision must be deterministic and must not mutate the order.
4. Existing callers receive a boolean result. Do not change the public
   function signature.

## Compatibility

Orders from ordinary mainland regions keep the existing amount-only behavior.
The boundary at 99 CNY is inclusive. Invalid negative subtotals are rejected.

## Verification

Run `python -m pytest minimax/sample_project/test_shipping.py -q`.
The same test file must fail against `minimax/sample_project/before/shipping.py`
and pass against the current implementation.
