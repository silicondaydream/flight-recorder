"""Quotes come back from the payments sandbox."""

import unittest

from pricing.gateway import quote


class GatewayTest(unittest.TestCase):
    def test_quote_round_trip(self) -> None:
        self.assertEqual(quote(1999)["amount"], 1999)


if __name__ == "__main__":
    unittest.main()
