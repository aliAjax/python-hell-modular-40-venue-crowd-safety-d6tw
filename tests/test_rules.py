import unittest

from src.domain import ValidationError
from src.rules import capacity_available, incident_priority


class RulesTest(unittest.TestCase):
    def test_capacity_boundary(self):
        self.assertTrue(capacity_available(1000, 800, 200))
        self.assertFalse(capacity_available(1000, 800, 201))

    def test_incident_priority_rank(self):
        self.assertGreater(incident_priority("critical", "fire"), incident_priority("medium", "security"))
        with self.assertRaises(ValidationError):
            incident_priority("unknown", "medical")


if __name__ == "__main__":
    unittest.main()
