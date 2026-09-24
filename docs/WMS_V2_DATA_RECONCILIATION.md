# WMS V2 Data Reconciliation

Proved locally by `tests/test_v2_phase10_reconciliation.py` and `assert_invariants()`.

Closed order: Ordered = Allocated = Packed = Shipped.

Inventory identity: IMPORT − SHIP = current ON_HAND (no returns/transfers in the cert dataset).

No unit with warehouse.client_id ≠ unit.client_id. No unit with two ACTIVE allocations.
