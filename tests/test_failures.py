import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "failures.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("coordinator", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone_gate(self, capacity=100):
        venue = self.service.create(self.coordinator, "venue", {"name": "V", "address": "A"})
        zone = self.service.create(
            self.coordinator, "zone", {"venue_id": venue["id"], "name": "Z", "capacity": capacity}
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "operator"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        return venue, zone, gate

    def test_capacity_and_permission_conflicts(self):
        venue, zone, gate = self._venue_zone_gate()
        self.service.transition(
            self.operator,
            zone["id"],
            "admit",
            {"gate_id": gate["id"], "count": 90, "admitted_at": "t1"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator,
                zone["id"],
                "admit",
                {"gate_id": gate["id"], "count": 11, "admitted_at": "t2"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                zone["id"],
                "admit",
                {"gate_id": gate["id"], "count": 1, "admitted_at": "t3"},
            )

    def test_duplicate_incident_and_version_conflict(self):
        venue, zone, _ = self._venue_zone_gate()
        data = {
            "venue_id": venue["id"],
            "zone_id": zone["id"],
            "source_ref": "radio-1",
            "incident_type": "crowd",
            "severity": "high",
            "reported_at": "2026-09-27T18:00:00Z",
        }
        incident = self.service.create(self.operator, "incident", data)
        with self.assertRaises(ConflictError):
            self.service.create(self.operator, "incident", data)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor,
                incident["id"],
                "triage",
                {"priority": "crowd"},
                expected_version=999,
            )

    def test_team_conflict_and_idempotency(self):
        venue, zone, _ = self._venue_zone_gate()
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": "radio-2",
                "incident_type": "medical",
                "severity": "medium",
                "reported_at": "2026-09-27T18:00:00Z",
            },
        )
        payload = {
            "incident_id": incident["id"],
            "venue_id": venue["id"],
            "zone_id": zone["id"],
            "team_id": "team-1",
            "task_type": "medical",
        }
        first = self.service.create(self.supervisor, "task", payload, "task-key")
        self.service.transition(self.coordinator, first["id"], "assign", {"assigned_at": "t1"})
        second = self.service.create(self.supervisor, "task", payload)
        with self.assertRaises(ConflictError):
            self.service.transition(self.coordinator, second["id"], "assign", {"assigned_at": "t2"})
        repeated = self.service.create(self.supervisor, "task", payload, "task-key")
        self.assertEqual(first["id"], repeated["id"])


if __name__ == "__main__":
    unittest.main()
