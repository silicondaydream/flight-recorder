"""The card processor settles half-cents upwards. Our totals must match its export."""

import unittest
from decimal import Decimal

from pricing.money import line_total, order_total


class CardProcessorExportTest(unittest.TestCase):
    def test_half_cents_round_up(self) -> None:
        self.assertEqual(line_total(1, Decimal("2.665")), Decimal("2.67"))

    def test_order_matches_processor_export(self) -> None:
        lines = [(1, Decimal("2.665")), (3, Decimal("0.125"))]
        self.assertEqual(order_total(lines), Decimal("3.05"))


if __name__ == "__main__":
    unittest.main()
