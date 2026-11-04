"""The bank ledger uses banker's rounding. Our totals must match its export too."""

import unittest
from decimal import Decimal

from pricing.money import line_total, order_total


class BankLedgerExportTest(unittest.TestCase):
    def test_half_cents_round_to_even(self) -> None:
        self.assertEqual(line_total(1, Decimal("2.665")), Decimal("2.66"))

    def test_order_matches_ledger_export(self) -> None:
        lines = [(1, Decimal("2.665")), (3, Decimal("0.125"))]
        self.assertEqual(order_total(lines), Decimal("3.04"))


if __name__ == "__main__":
    unittest.main()
