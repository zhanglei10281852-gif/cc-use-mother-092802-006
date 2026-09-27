import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from curtailment_case.contracts import CaseState, DispatchInterval


class CurtailmentContractTests(unittest.TestCase):
    def test_dispatch_has_explicit_interval(self):
        start = datetime(2026, 4, 2, 23, tzinfo=timezone.utc)
        end = datetime(2026, 4, 3, 1, tzinfo=timezone.utc)
        item = DispatchInterval("d-1", "plant-4", start, end, Decimal("18"))
        self.assertGreater(item.ends_at, item.starts_at)

    def test_case_states_are_stable_values(self):
        self.assertEqual(CaseState.SETTLED.value, "settled")


if __name__ == "__main__":
    unittest.main()
