import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from src.domain import Actor, ConflictError, PermissionDenied
from src.http_api import create_handler
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DisposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "disposal.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("commander", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue(self):
        return self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )

    def _zone(self, venue, name="Z", capacity=100):
        return self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": name, "capacity": capacity},
        )

    def _gate(self, venue, zone_ids, name="G"):
        return self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": name, "zone_ids": zone_ids},
        )

    def _open(self, gate, zone):
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "operator"})
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})

    def _incident(self, venue, zone, source_ref="radio-1"):
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": source_ref,
                "incident_type": "crowd",
                "severity": "high",
                "reported_at": "2026-09-27T18:00:00Z",
            },
        )
        self.service.transition(self.supervisor, incident["id"], "triage", {"priority": "crowd"})
        self.service.transition(self.coordinator, incident["id"], "dispatch", {"commander_id": "c1"})
        return incident

    def _task(self, incident, venue, zone, team_id="team-1"):
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": team_id,
                "task_type": "crowd",
            },
        )
        self.service.transition(self.coordinator, task["id"], "assign", {"assigned_at": "t1"})
        self.service.transition(self.operator, task["id"], "acknowledge", {"acknowledged_at": "t2"})
        return task

    def test_initiate_closes_gate_and_returns_tasks_for_review(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)
        task = self._task(incident, venue, zone)

        disposal = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )

        self.assertEqual(disposal["kind"], "disposal")
        self.assertEqual(disposal["status"], "active")
        self.assertEqual(disposal["data"]["command_no"], disposal["data"]["command_no"])
        self.assertEqual(disposal["data"]["zone_id"], zone["id"])
        self.assertEqual(disposal["data"]["incident_id"], incident["id"])
        self.assertIn(gate["id"], disposal["data"]["gate_ids"])
        self.assertIn(task["id"], disposal["data"]["task_ids"])

        zone_after = self.service.get(zone["id"])
        self.assertEqual(zone_after["status"], "evacuating")

        gate_after = self.service.get(gate["id"])
        self.assertEqual(gate_after["status"], "closed")
        self.assertIn(disposal["data"]["command_no"], gate_after["data"]["evacuation_commands"])

        task_after = self.service.get(task["id"])
        self.assertEqual(task_after["status"], "draft")
        self.assertEqual(task_after["data"]["returned_from"], "enroute")
        self.assertEqual(task_after["data"]["return_reason"], "evacuation")

    def test_resubmit_returns_original_order(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)

        first = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )
        second = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )
        self.assertEqual(first["id"], second["id"])

    def test_gate_open_during_evacuation_conflict_names_gate(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)
        self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.operator, gate["id"], "open", {"operator_id": "operator"}
            )
        self.assertIn(gate["id"], str(ctx.exception))

    def test_recover_requires_resolved_incident_and_no_transit_tasks(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)
        task = self._task(incident, venue, zone)
        disposal = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )

        # incident still dispatched -> conflict
        with self.assertRaises(ConflictError):
            self.service.transition(self.coordinator, disposal["id"], "recover", {})

        # resolve incident but a new task is still in transit -> conflict
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        in_transit = self._task(incident, venue, zone, team_id="team-2")
        with self.assertRaises(ConflictError):
            self.service.transition(self.coordinator, disposal["id"], "recover", {})

        # finish the in-transit task -> recover succeeds
        self.service.transition(
            self.operator, in_transit["id"], "arrive", {"arrived_at": "t3"}
        )
        self.service.transition(
            self.operator,
            in_transit["id"],
            "complete",
            {"completed_at": "t4", "outcome": "done"},
        )
        recovered = self.service.transition(self.coordinator, disposal["id"], "recover", {})
        self.assertEqual(recovered["status"], "recovered")

        zone_after = self.service.get(zone["id"])
        self.assertEqual(zone_after["status"], "open")
        gate_after = self.service.get(gate["id"])
        self.assertEqual(gate_after["status"], "open")
        self.assertEqual(gate_after["data"]["evacuation_commands"], [])

    def test_recover_unauthorized_rejected(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)
        task = self._task(incident, venue, zone)
        disposal = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )
        self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "ok"}
        )
        # task was returned to draft; re-dispatch and finish it
        self.service.transition(
            self.coordinator, task["id"], "assign", {"assigned_at": "t3"}
        )
        self.service.transition(
            self.operator, task["id"], "acknowledge", {"acknowledged_at": "t4"}
        )
        self.service.transition(
            self.operator, task["id"], "arrive", {"arrived_at": "t5"}
        )
        self.service.transition(
            self.operator, task["id"], "complete", {"completed_at": "t6", "outcome": "done"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator, disposal["id"], "recover", {})

    def test_shared_gate_reopens_only_when_all_zones_recovered(self):
        venue = self._venue()
        zone_a = self._zone(venue, name="A")
        zone_b = self._zone(venue, name="B")
        gate = self._gate(venue, [zone_a["id"], zone_b["id"]], name="shared")
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "op"})
        self.service.transition(self.operator, zone_a["id"], "open", {"checklist": "ok"})
        self.service.transition(self.operator, zone_b["id"], "open", {"checklist": "ok"})
        incident_a = self._incident(venue, zone_a, source_ref="ra")
        incident_b = self._incident(venue, zone_b, source_ref="rb")
        disposal_a = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone_a["id"], "incident_id": incident_a["id"]},
        )
        disposal_b = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone_b["id"], "incident_id": incident_b["id"]},
        )
        self.service.transition(
            self.coordinator, incident_a["id"], "resolve", {"resolution": "ok"}
        )
        self.service.transition(
            self.coordinator, incident_b["id"], "resolve", {"resolution": "ok"}
        )

        self.service.transition(self.coordinator, disposal_a["id"], "recover", {})
        gate_after = self.service.get(gate["id"])
        self.assertEqual(gate_after["status"], "closed")

        self.service.transition(self.coordinator, disposal_b["id"], "recover", {})
        gate_after = self.service.get(gate["id"])
        self.assertEqual(gate_after["status"], "open")

    def test_failed_step_rolls_back_and_retry_succeeds(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        incident = self._incident(venue, zone)
        task = self._task(incident, venue, zone)

        original = self.service.repository._update_entity

        def failing(connection, entity_id, expected_version, status, data, now):
            if entity_id == gate["id"]:
                raise ConflictError("simulated gate failure")
            return original(connection, entity_id, expected_version, status, data, now)

        self.service.repository._update_entity = failing
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "disposal",
                {"zone_id": zone["id"], "incident_id": incident["id"]},
            )
        self.service.repository._update_entity = original

        # nothing took effect
        self.assertEqual(self.service.list("disposal"), [])
        self.assertEqual(self.service.get(zone["id"])["status"], "open")
        self.assertEqual(self.service.get(gate["id"])["status"], "open")
        self.assertEqual(self.service.get(task["id"])["status"], "enroute")

        # retry succeeds
        disposal = self.service.create(
            self.coordinator,
            "disposal",
            {"zone_id": zone["id"], "incident_id": incident["id"]},
        )
        self.assertEqual(disposal["status"], "active")
        self.assertEqual(self.service.get(zone["id"])["status"], "evacuating")
        self.assertEqual(self.service.get(gate["id"])["status"], "closed")
        self.assertEqual(self.service.get(task["id"])["status"], "draft")

    def test_backfill_legacy_evacuations(self):
        venue = self._venue()
        zone = self._zone(venue)
        gate = self._gate(venue, [zone["id"]])
        self._open(gate, zone)
        zone = self.service.get(zone["id"])
        incident = self._incident(venue, zone)
        task = self._task(incident, venue, zone)

        # simulate legacy data: zone evacuating but no disposal order
        self.service.repository.update_entity(
            zone["id"], zone["version"], "evacuating", dict(zone["data"])
        )

        created = self.service.backfill_legacy_evacuations(self.coordinator)
        self.assertEqual(len(created), 1)
        disposal = created[0]
        self.assertEqual(disposal["status"], "active")
        self.assertEqual(disposal["data"]["zone_id"], zone["id"])
        self.assertIn(gate["id"], disposal["data"]["gate_ids"])
        self.assertIn(task["id"], disposal["data"]["task_ids"])

        gate_after = self.service.get(gate["id"])
        self.assertEqual(gate_after["status"], "closed")
        self.assertIn(disposal["data"]["command_no"], gate_after["data"]["evacuation_commands"])
        task_after = self.service.get(task["id"])
        self.assertEqual(task_after["status"], "draft")

        # idempotent: second backfill creates nothing
        self.assertEqual(self.service.backfill_legacy_evacuations(self.coordinator), [])

    def test_backfill_unauthorized_rejected(self):
        venue = self._venue()
        zone = self._zone(venue)
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "ok"})
        zone = self.service.get(zone["id"])
        self.service.repository.update_entity(
            zone["id"], zone["version"], "evacuating", dict(zone["data"])
        )
        with self.assertRaises(PermissionDenied):
            self.service.backfill_legacy_evacuations(self.operator)


class DisposalHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "http.db"),
            RuleEngine(),
        )
        handler = create_handler(self.service, RuleEngine(), str(Path(self.tmp.name)))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=payload,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_disposal_lifecycle_over_http(self):
        headers = {"X-User-Id": "commander", "X-Role": "coordinator"}
        _, venue = self._request("POST", "/api/venues", {"name": "V", "address": "A"}, headers)
        _, zone = self._request(
            "POST", "/api/zones",
            {"venue_id": venue["id"], "name": "Z", "capacity": 100}, headers,
        )
        _, gate = self._request(
            "POST", "/api/gates",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [zone["id"]]}, headers,
        )
        self._request("POST", "/api/entities/%s/actions" % gate["id"],
                      {"action": "open", "data": {"operator_id": "op"}},
                      {"X-User-Id": "op", "X-Role": "operator"})
        self._request("POST", "/api/entities/%s/actions" % zone["id"],
                      {"action": "open", "data": {"checklist": "ok"}},
                      {"X-User-Id": "op", "X-Role": "operator"})
        _, incident = self._request(
            "POST", "/api/incidents",
            {"venue_id": venue["id"], "zone_id": zone["id"], "source_ref": "r1",
             "incident_type": "crowd", "severity": "high", "reported_at": "t"},
            {"X-User-Id": "op", "X-Role": "operator"},
        )
        self._request("POST", "/api/entities/%s/actions" % incident["id"],
                      {"action": "triage", "data": {"priority": "crowd"}},
                      {"X-User-Id": "super", "X-Role": "supervisor"})
        self._request("POST", "/api/entities/%s/actions" % incident["id"],
                      {"action": "dispatch", "data": {"commander_id": "c"}}, headers)

        _, disposal = self._request(
            "POST", "/api/disposals",
            {"zone_id": zone["id"], "incident_id": incident["id"]}, headers,
        )
        self.assertEqual(disposal["status"], "active")
        gate_after = self._request("GET", "/api/entities/%s" % gate["id"])[1]
        self.assertEqual(gate_after["status"], "closed")

        self._request("POST", "/api/entities/%s/actions" % incident["id"],
                      {"action": "resolve", "data": {"resolution": "ok"}}, headers)
        _, recovered = self._request(
            "POST", "/api/entities/%s/actions" % disposal["id"],
            {"action": "recover", "data": {}}, headers,
        )
        self.assertEqual(recovered["status"], "recovered")

    def test_admin_upgrade_endpoint(self):
        headers = {"X-User-Id": "commander", "X-Role": "coordinator"}
        _, venue = self._request("POST", "/api/venues", {"name": "V", "address": "A"}, headers)
        _, zone = self._request(
            "POST", "/api/zones",
            {"venue_id": venue["id"], "name": "Z", "capacity": 100}, headers,
        )
        self._request("POST", "/api/entities/%s/actions" % zone["id"],
                      {"action": "open", "data": {"checklist": "ok"}},
                      {"X-User-Id": "op", "X-Role": "operator"})
        zone = self._request("GET", "/api/entities/%s" % zone["id"])[1]
        self.service.repository.update_entity(
            zone["id"], zone["version"], "evacuating", dict(zone["data"])
        )
        _, result = self._request("POST", "/api/admin/upgrade-evacuations", {}, headers)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
