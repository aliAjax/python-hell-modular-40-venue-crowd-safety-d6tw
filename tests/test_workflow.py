import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "workflow.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("venue-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def test_incident_response_and_task_arrival(self):
        venue = self.service.create(
            self.coordinator,
            "venue",
            {"name": "Grand Hall", "address": "1 Stadium Road"},
        )
        zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": "North Stand", "capacity": 1000},
        )
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": "Gate A", "zone_ids": [zone["id"]]},
        )
        post = self.service.create(
            self.supervisor,
            "post",
            {"venue_id": venue["id"], "zone_id": zone["id"], "staff_count": 8, "duty": "crowd"},
        )
        self.service.create(
            self.supervisor,
            "medical_point",
            {"venue_id": venue["id"], "zone_id": zone["id"], "capacity": 12, "equipment_level": "advanced"},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "gate-operator"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
        zone = self.service.transition(
            self.operator,
            zone["id"],
            "admit",
            {"gate_id": gate["id"], "count": 420, "admitted_at": "2026-09-27T18:00:00Z"},
        )
        self.assertEqual(zone["data"]["current_occupancy"], 420)

        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": "radio-17",
                "incident_type": "medical",
                "severity": "high",
                "reported_at": "2026-09-27T18:10:00Z",
            },
        )
        incident = self.service.transition(
            self.supervisor, incident["id"], "triage", {"priority": "medical"}
        )
        incident = self.service.transition(
            self.coordinator, incident["id"], "dispatch", {"commander_id": "commander-1"}
        )
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": "medic-team-1",
                "task_type": "medical",
            },
        )
        task = self.service.transition(self.coordinator, task["id"], "assign", {"assigned_at": "2026-09-27T18:11:00Z"})
        task = self.service.transition(self.operator, task["id"], "acknowledge", {"acknowledged_at": "2026-09-27T18:12:00Z"})
        task = self.service.transition(self.operator, task["id"], "arrive", {"arrived_at": "2026-09-27T18:14:00Z"})
        task = self.service.transition(
            self.operator,
            task["id"],
            "complete",
            {"completed_at": "2026-09-27T18:20:00Z", "outcome": "patient transferred"},
        )
        incident = self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "patient handed to ambulance"}
        )
        self.assertEqual(task["status"], "completed")
        self.assertEqual(incident["status"], "resolved")
        self.assertTrue(post["id"])


if __name__ == "__main__":
    unittest.main()
