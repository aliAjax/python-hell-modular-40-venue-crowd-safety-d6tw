import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SCHEMA_VERSION, SQLiteRepository, UnitOfWork, utcnow
from src.rules import RuleEngine, TASK_INFLIGHT_STATUSES
from src.service import DomainService


class EvacuationOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "evacuation.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.coordinator = Actor("venue-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("gate-operator", "operator")
        self.viewer = Actor("watch", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue(self, name="Grand Hall"):
        return self.service.create(
            self.coordinator, "venue", {"name": name, "address": "1 Stadium Road"}
        )

    def _zone(self, venue, name="North Stand", capacity=1000):
        return self.service.create(
            self.coordinator, "zone", {"venue_id": venue["id"], "name": name, "capacity": capacity}
        )

    def _gate(self, venue, zones, name="Gate A"):
        gate = self.service.create(
            self.coordinator, "gate", {"venue_id": venue["id"], "name": name, "zone_ids": zones}
        )
        return self.service.transition(
            self.operator, gate["id"], "open", {"operator_id": self.operator.user_id}
        )

    def _open_zone(self, zone):
        return self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})

    def _incident(self, venue, zone, ref="radio-1", severity="high", incident_type="fire"):
        return self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": ref,
                "incident_type": incident_type,
                "severity": severity,
                "reported_at": "2026-10-04T18:00:00Z",
            },
        )

    def _dispatch(self, incident):
        incident = self.service.transition(
            self.supervisor, incident["id"], "triage", {"priority": "fire"}
        )
        return self.service.transition(
            self.coordinator, incident["id"], "dispatch", {"commander_id": "commander-1"}
        )

    def _assigned_task(self, venue, zone, incident, team="team-1", task_type="medical"):
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": team,
                "task_type": task_type,
            },
        )
        task = self.service.transition(
            self.coordinator, task["id"], "assign", {"assigned_at": "t1"}
        )
        task = self.service.transition(
            self.operator, task["id"], "acknowledge", {"acknowledged_at": "t2"}
        )
        return self.service.transition(
            self.operator, task["id"], "arrive", {"arrived_at": "t3"}
        )

    def _issue(self, zone, incident, command_no="CMD-1", actor=None, **extra):
        payload = {
            "zone_id": zone["id"],
            "incident_id": incident["id"],
            "command_no": command_no,
            "reason": "fire alarm",
        }
        payload.update(extra)
        return self.service.issue_evacuation(actor or self.coordinator, payload)

    def test_issue_order_closes_zone_gates_and_returns_tasks(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        task = self._assigned_task(venue, zone, incident)

        order = self._issue(zone, incident)

        self.assertEqual(order["status"], "executing")
        self.assertEqual(self.service.get(zone["id"])["status"], "evacuating")
        self.assertEqual(self.service.get(gate["id"])["status"], "closed")
        self.assertEqual(
            self.service.get(gate["id"])["data"]["evacuation_holds"], ["CMD-1"]
        )
        returned = self.service.get(task["id"])
        self.assertEqual(returned["status"], "in_review")
        self.assertEqual(returned["data"]["returned_for_review"]["command_no"], "CMD-1")
        self.assertEqual(order["data"]["task_ids"], [task["id"]])
        self.assertEqual(order["data"]["closed_gate_ids"], [gate["id"]])

    def test_resubmit_same_command_no_returns_original_order(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))

        first = self._issue(zone, incident, "CMD-42")
        replay = self._issue(zone, incident, "CMD-42")
        self.assertEqual(first["id"], replay["id"])

        # Even after the incident is closed and the order completed, resubmitting
        # the same command number must hand back the original completed order.
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        self.service.complete_evacuation(
            self.coordinator, first["id"], {"checklist": "clear"}
        )
        late_replay = self._issue(zone, incident, "CMD-42")
        self.assertEqual(late_replay["id"], first["id"])
        self.assertEqual(late_replay["status"], "completed")

        # Idempotency-Key on the HTTP layer behaves the same way.
        keyed = self.service.issue_evacuation(
            self.coordinator,
            {
                "zone_id": zone["id"],
                "incident_id": incident["id"],
                "command_no": "CMD-42",
                "reason": "fire alarm",
            },
            "idem-1",
        )
        self.assertEqual(keyed["id"], first["id"])

    def test_command_no_bound_to_other_target_conflicts(self):
        venue = self._venue()
        zone_a = self._zone(venue, "Zone A")
        zone_b = self._zone(venue, "Zone B")
        self._open_zone(zone_a)
        self._open_zone(zone_b)
        incident_a = self._dispatch(self._incident(venue, zone_a, "radio-a"))
        incident_b = self._dispatch(self._incident(venue, zone_b, "radio-b"))
        self._issue(zone_a, incident_a, "CMD-X")
        with self.assertRaises(ConflictError):
            self._issue(zone_b, incident_b, "CMD-X")

    def test_late_admission_via_another_gate_sees_conflict(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate_a = self._gate(venue, [zone["id"]], "Gate A")
        gate_b = self._gate(venue, [zone["id"]], "Gate B")
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        self._issue(zone, incident)

        # Both gates are closed; the error names the holding command.
        for gate in (gate_a, gate_b):
            with self.assertRaises(ConflictError) as caught:
                self.service.transition(
                    self.operator,
                    zone["id"],
                    "admit",
                    {"gate_id": gate["id"], "count": 1, "admitted_at": "t"},
                )
            self.assertIn("CMD-1", str(caught.exception))

        # Gates cannot be manually reopened while held either.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator, gate_a["id"], "open", {"operator_id": "op"}
            )

    def test_shared_gate_stays_closed_until_last_zone_recovers(self):
        venue = self._venue()
        zone_a = self._zone(venue, "Zone A")
        zone_b = self._zone(venue, "Zone B")
        shared = self._gate(venue, [zone_a["id"], zone_b["id"]], "Shared Gate")
        self._open_zone(zone_a)
        self._open_zone(zone_b)
        incident_a = self._dispatch(self._incident(venue, zone_a, "radio-a"))
        incident_b = self._dispatch(self._incident(venue, zone_b, "radio-b"))

        order_a = self._issue(zone_a, incident_a, "CMD-A")
        self.assertEqual(self.service.get(shared["id"])["status"], "closed")
        order_b = self._issue(zone_b, incident_b, "CMD-B")
        self.assertEqual(
            self.service.get(shared["id"])["data"]["evacuation_holds"],
            ["CMD-A", "CMD-B"],
        )

        incident_a = self.service.transition(
            self.coordinator, incident_a["id"], "resolve", {"resolution": "ok"}
        )
        order_a = self.service.complete_evacuation(
            self.coordinator, order_a["id"], {"checklist": "clear", "recovered_at": "r1"}
        )
        # Zone A recovered, but the shared gate still serves evacuating zone B.
        self.assertEqual(self.service.get(zone_a["id"])["status"], "open")
        self.assertEqual(self.service.get(shared["id"])["status"], "closed")
        self.assertEqual(
            self.service.get(shared["id"])["data"]["evacuation_holds"], ["CMD-B"]
        )
        self.assertEqual(order_a["data"]["reopened_gate_ids"], [])

        incident_b = self.service.transition(
            self.coordinator, incident_b["id"], "resolve", {"resolution": "ok"}
        )
        order_b = self.service.complete_evacuation(
            self.coordinator, order_b["id"], {"checklist": "clear", "recovered_at": "r2"}
        )
        self.assertEqual(self.service.get(shared["id"])["status"], "open")
        self.assertEqual(
            self.service.get(shared["id"])["data"]["evacuation_holds"], []
        )
        self.assertEqual(order_b["data"]["reopened_gate_ids"], [shared["id"]])

    def test_recover_requires_closed_incident_and_no_inflight_tasks(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        self._assigned_task(venue, zone, incident)
        order = self._issue(zone, incident)

        # Incident still open.
        with self.assertRaises(ConflictError) as caught:
            self.service.complete_evacuation(
                self.coordinator, order["id"], {"checklist": "clear"}
            )
        self.assertIn("incident", str(caught.exception))

        # Re-dispatch the returned task: it is in flight again and blocks recovery.
        task = self.service.list("task")[0]
        self.assertEqual(task["status"], "in_review")
        self.service.transition(
            self.coordinator, task["id"], "assign", {"assigned_at": "t9"}
        )
        self.service.transition(
            self.supervisor, incident["id"], "resolve", {"resolution": "handled"}
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.complete_evacuation(
                self.coordinator, order["id"], {"checklist": "clear"}
            )
        self.assertIn("in flight", str(caught.exception))

        # Completing the task clears the way; returned-only tasks do not block.
        self.service.transition(self.operator, task["id"], "acknowledge", {"acknowledged_at": "t10"})
        self.service.transition(self.operator, task["id"], "arrive", {"arrived_at": "t11"})
        self.service.transition(
            self.operator, task["id"], "complete",
            {"completed_at": "t12", "outcome": "done"},
        )
        completed = self.service.complete_evacuation(
            self.coordinator, order["id"], {"checklist": "clear"}
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.service.get(zone["id"])["status"], "open")
        self.assertEqual(completed["data"]["reopened_gate_ids"], order["data"]["gate_ids"])

    def test_unauthorized_roles_are_rejected(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))

        with self.assertRaises(PermissionDenied):
            self._issue(zone, incident, actor=self.operator)
        with self.assertRaises(PermissionDenied):
            self._issue(zone, incident, actor=self.viewer)

        order = self._issue(zone, incident)
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.complete_evacuation(
                self.operator, order["id"], {"checklist": "clear"}
            )
        # Supervisor is allowed to recover.
        done = self.service.complete_evacuation(
            self.supervisor, order["id"], {"checklist": "clear"}
        )
        self.assertEqual(done["status"], "completed")

    def test_any_step_failure_leaves_no_partial_effects(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        self._assigned_task(venue, zone, incident)

        # Inject a fault when the order row itself is written: everything rolls back.
        original_create = UnitOfWork.create_entity

        def failing_create(self, entity_id, kind, status, data, actor_id):
            if kind == "evacuation_order":
                raise sqlite3.OperationalError("injected storage fault")
            return original_create(self, entity_id, kind, status, data, actor_id)

        UnitOfWork.create_entity = failing_create
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self._issue(zone, incident, "CMD-FAIL")
        finally:
            UnitOfWork.create_entity = original_create

        self.assertEqual(self.service.get(zone["id"])["status"], "open")
        self.assertEqual(self.service.get(gate["id"])["status"], "open")
        self.assertEqual(
            self.service.get(gate["id"])["data"].get("evacuation_holds", []), []
        )
        self.assertEqual(self.service.list("task")[0]["status"], "on_scene")
        self.assertEqual(self.service.list("evacuation_order"), [])

        # The same command can be retried after the failure and fully applies.
        order = self._issue(zone, incident, "CMD-FAIL")
        self.assertEqual(order["status"], "executing")
        self.assertEqual(self.service.get(zone["id"])["status"], "evacuating")
        self.assertEqual(self.service.get(gate["id"])["status"], "closed")
        self.assertEqual(self.service.list("task")[0]["status"], "in_review")

    def test_cannot_issue_twice_for_same_zone(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        self._issue(zone, incident, "CMD-1")
        second = self._incident(venue, zone, "radio-2")
        second = self._dispatch(second)
        with self.assertRaises(ConflictError) as caught:
            self._issue(zone, second, "CMD-2")
        self.assertIn("CMD-1", str(caught.exception))

    def test_cannot_issue_for_resolved_incident_or_wrong_zone(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        with self.assertRaises(InvalidTransition):
            self._issue(zone, incident)

        other_zone = self._zone(venue, "Other")
        other_incident = self._dispatch(self._incident(venue, other_zone, "radio-x"))
        with self.assertRaises(ValidationError):
            self._issue(zone, other_incident)

    def test_completed_order_cannot_be_completed_again(self):
        venue = self._venue()
        zone = self._zone(venue)
        self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        order = self._issue(zone, incident)
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        self.service.complete_evacuation(
            self.coordinator, order["id"], {"checklist": "clear"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.complete_evacuation(
                self.coordinator, order["id"], {"checklist": "again"}
            )

    def test_concurrent_issue_and_admit_never_leave_gate_open_during_evacuation(self):
        venue = self._venue()
        zone = self._zone(venue, capacity=100000)
        gate = self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        zone_id, gate_id, incident_id = zone["id"], gate["id"], incident["id"]

        errors = []

        def evacuate():
            try:
                self.service.issue_evacuation(
                    self.coordinator,
                    {
                        "zone_id": zone_id,
                        "incident_id": incident_id,
                        "command_no": "CMD-RACE",
                        "reason": "fire",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(("issue", exc))

        admitted = []

        def admit(index):
            try:
                self.service.transition(
                    self.operator,
                    zone_id,
                    "admit",
                    {"gate_id": gate_id, "count": 1, "admitted_at": "t%d" % index},
                )
                admitted.append(index)
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(("admit", exc))

        threads = [
            threading.Thread(target=evacuate),
            *[threading.Thread(target=admit, args=(i,)) for i in range(8)],
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final_zone = self.service.get(zone_id)
        final_gate = self.service.get(gate_id)
        order = self.service.list("evacuation_order")[0]
        self.assertEqual(order["data"]["command_no"], "CMD-RACE")
        self.assertEqual(final_zone["status"], "evacuating")
        self.assertEqual(final_gate["status"], "closed")
        # Every failed admission must have been reported as an evacuation conflict.
        admit_errors = [exc for kind, exc in errors if kind == "admit"]
        for exc in admit_errors:
            self.assertIsInstance(exc, ConflictError)
            self.assertIn("CMD-RACE", str(exc))
        # Successful admissions committed before the order was issued, and
        # exactly those admissions are reflected in occupancy: none sneaks in
        # after the zone starts evacuating.
        self.assertEqual(len(admitted) + len(admit_errors), 8)
        self.assertEqual(final_zone["data"].get("current_occupancy", 0), len(admitted))

    def test_gate_already_closed_before_evacuation_stays_closed_after_recovery(self):
        venue = self._venue()
        zone = self._zone(venue)
        open_gate = self._gate(venue, [zone["id"]], "Open Gate")
        closed_gate = self.service.create(
            self.coordinator, "gate",
            {"venue_id": venue["id"], "name": "Shut Gate", "zone_ids": [zone["id"]]},
        )
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        order = self._issue(zone, incident)
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        done = self.service.complete_evacuation(
            self.coordinator, order["id"], {"checklist": "clear"}
        )
        self.assertEqual(self.service.get(open_gate["id"])["status"], "open")
        self.assertEqual(self.service.get(closed_gate["id"])["status"], "closed")
        self.assertEqual(done["data"]["reopened_gate_ids"], [open_gate["id"]])

    def test_reopened_gate_accepts_admission_after_recovery(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open_zone(zone)
        incident = self._dispatch(self._incident(venue, zone))
        order = self._issue(zone, incident)
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        self.service.complete_evacuation(
            self.coordinator, order["id"], {"checklist": "clear"}
        )
        admitted = self.service.transition(
            self.operator,
            zone["id"],
            "admit",
            {"gate_id": gate["id"], "count": 12, "admitted_at": "after"},
        )
        self.assertEqual(admitted["data"]["current_occupancy"], 12)

    def test_generic_create_of_evacuation_order_is_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.coordinator,
                "evacuation_order",
                {"command_no": "X", "zone_id": "z", "incident_id": "i", "reason": "r"},
            )

    def test_legacy_evacuating_zone_is_backfilled_and_aligned(self):
        # Build an old-schema database with an evacuating zone, an open gate and
        # an on-scene task, then open it through the current repository.
        legacy = SQLiteRepository(self.db_path)
        with legacy.unit_of_work() as tx:
            tx.set_user_version(1)
            venue = tx.create_entity(
                "venue-1", "venue", "ready",
                {"name": "Old Hall", "address": "Road"}, "old-admin",
            )
            zone = tx.create_entity(
                "zone-1", "zone", "evacuating",
                {"venue_id": venue["id"], "name": "Old Stand", "capacity": 500,
                 "current_occupancy": 100},
                "old-admin",
            )
            gate = tx.create_entity(
                "gate-1", "gate", "open",
                {"venue_id": venue["id"], "name": "Old Gate", "zone_ids": ["zone-1"]},
                "old-admin",
            )
            incident = tx.create_entity(
                "incident-1", "incident", "dispatched",
                {"venue_id": venue["id"], "zone_id": "zone-1",
                 "incident_key": "venue-1:radio-old", "source_ref": "radio-old",
                 "incident_type": "fire", "severity": "high", "priority_score": 70},
                "old-admin",
            )
            tx.create_entity(
                "task-1", "task", "on_scene",
                {"incident_id": "incident-1", "venue_id": venue["id"],
                 "zone_id": "zone-1", "team_id": "team-old", "task_type": "rescue"},
                "old-admin",
            )
        del legacy

        migrated = DomainService(SQLiteRepository(self.db_path), RuleEngine())

        orders = migrated.list("evacuation_order")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertTrue(order["data"]["backfilled"])
        self.assertEqual(order["data"]["zone_id"], "zone-1")
        self.assertEqual(order["data"]["incident_id"], "incident-1")
        self.assertEqual(order["status"], "executing")
        gate = migrated.get("gate-1")
        self.assertEqual(gate["status"], "closed")
        self.assertEqual(gate["data"]["evacuation_holds"], [order["data"]["command_no"]])
        self.assertEqual(migrated.get("task-1")["status"], "in_review")
        self.assertEqual(migrated.get("zone-1")["status"], "evacuating")

        # The backfilled order can drive normal recovery now.
        migrated.transition(
            self.coordinator, "incident-1", "resolve", {"resolution": "closed after upgrade"}
        )
        done = migrated.complete_evacuation(
            self.coordinator, order["id"], {"checklist": "post-upgrade check"}
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(migrated.get("gate-1")["status"], "open")
        self.assertEqual(migrated.get("zone-1")["status"], "open")

    def test_legacy_zone_without_incident_gets_synthetic_one(self):
        legacy = SQLiteRepository(self.db_path)
        with legacy.unit_of_work() as tx:
            tx.set_user_version(1)
            tx.create_entity("venue-9", "venue", "ready", {"name": "V"}, "a")
            tx.create_entity(
                "zone-9", "zone", "evacuating",
                {"venue_id": "venue-9", "name": "Z", "capacity": 10}, "a",
            )
        del legacy

        migrated = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        order = migrated.list("evacuation_order")[0]
        incident = migrated.get(order["data"]["incident_id"])
        self.assertTrue(incident["data"].get("synthetic"))
        self.assertNotEqual(incident["status"], "resolved")

    def test_migration_runs_once_and_is_idempotent(self):
        service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        with service.repository.unit_of_work() as tx:
            self.assertEqual(tx.get_user_version(), SCHEMA_VERSION)
        # Reopening the same database must not duplicate anything.
        again = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.assertEqual(again.list("evacuation_order"), [])


if __name__ == "__main__":
    unittest.main()
