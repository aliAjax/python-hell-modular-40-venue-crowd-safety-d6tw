from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied
from .rules import (
    DISPOSAL_INITIATE_ROLES,
    FINISHED_TASK_STATUSES,
    IN_TRANSIT_TASK_STATUSES,
    RuleEngine,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "disposal":
            return self._create_disposal(actor, data or {}, idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "disposal":
            if action == "recover":
                return self._recover_disposal(actor, entity, data or {}, expected_version)
            raise InvalidTransition("unknown action %s for disposal" % action)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def _ensure_disposal_role(self, actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    def _create_disposal(self, actor, data, idempotency_key):
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        zone_id = data.get("zone_id")
        if zone_id:
            for disposal in self._lookup("disposal", "zone_id", zone_id) or []:
                if disposal["status"] == "active":
                    return disposal
        payload = dict(data)
        payload.pop("id", None)
        validated = self.rules.validate_create(actor, "disposal", payload, self._lookup)
        if validated:
            payload.update(validated)
        command_no = payload["command_no"]
        zone = self.repository.get_entity(zone_id)
        incident = self.repository.get_entity(payload["incident_id"]) if payload.get("incident_id") else None
        disposal, zone_update, gate_updates, task_updates = self._build_disposal(
            actor, zone, incident, command_no
        )
        created = self.repository.apply_disposal_initiation(
            disposal, zone_update, gate_updates, task_updates
        )
        self.audit.record(
            created["id"],
            actor,
            "initiate",
            None,
            "active",
            {
                "command_no": command_no,
                "zone_id": zone_id,
                "gate_ids": disposal["data"]["gate_ids"],
                "task_ids": disposal["data"]["task_ids"],
            },
        )
        self.audit.record(
            zone["id"],
            actor,
            "evacuate",
            zone_update["from_status"],
            "evacuating",
            {"disposal_id": created["id"], "command_no": command_no},
        )
        for gate in gate_updates:
            self.audit.record(
                gate["id"],
                actor,
                "close",
                gate["from_status"],
                "closed",
                {"disposal_id": created["id"], "command_no": command_no},
            )
        for task in task_updates:
            self.audit.record(
                task["id"],
                actor,
                "return_for_review",
                task["from_status"],
                "draft",
                {"disposal_id": created["id"], "command_no": command_no},
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, created["id"])
        return created

    def _build_disposal(self, actor, zone, incident, command_no):
        now = utcnow()
        disposal_id = str(uuid4())
        gates = [
            gate
            for gate in self._lookup("gate", "venue_id", zone["data"]["venue_id"])
            if zone["id"] in (gate["data"].get("zone_ids") or [])
        ]
        gate_updates = []
        gate_ids = []
        for gate in gates:
            commands = list(gate["data"].get("evacuation_commands") or [])
            if command_no not in commands:
                commands.append(command_no)
            new_data = dict(gate["data"])
            new_data["evacuation_commands"] = commands
            gate_updates.append(
                {
                    "id": gate["id"],
                    "expected_version": gate["version"],
                    "status": "closed",
                    "data": new_data,
                    "from_status": gate["status"],
                }
            )
            gate_ids.append(gate["id"])
        task_updates = []
        task_ids = []
        seen = set()
        candidates = []
        if incident:
            candidates.extend(self._lookup("task", "incident_id", incident["id"]))
        candidates.extend(self._lookup("task", "zone_id", zone["id"]))
        for task in candidates:
            if task["id"] in seen:
                continue
            seen.add(task["id"])
            if task["status"] in FINISHED_TASK_STATUSES:
                continue
            new_data = dict(task["data"])
            new_data["returned_from"] = task["status"]
            new_data["returned_at"] = now
            new_data["return_reason"] = "evacuation"
            task_updates.append(
                {
                    "id": task["id"],
                    "expected_version": task["version"],
                    "status": "draft",
                    "data": new_data,
                    "from_status": task["status"],
                }
            )
            task_ids.append(task["id"])
        zone_update = {
            "id": zone["id"],
            "expected_version": zone["version"],
            "status": "evacuating",
            "data": dict(zone["data"]),
            "from_status": zone["status"],
        }
        disposal_data = {
            "venue_id": zone["data"]["venue_id"],
            "zone_id": zone["id"],
            "incident_id": incident["id"] if incident else None,
            "command_no": command_no,
            "gate_ids": gate_ids,
            "task_ids": task_ids,
            "initiated_by": actor.user_id,
            "initiated_at": now,
        }
        disposal = {
            "id": disposal_id,
            "kind": "disposal",
            "status": "active",
            "data": disposal_data,
        }
        return disposal, zone_update, gate_updates, task_updates

    def _recover_disposal(self, actor, disposal, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, disposal, "recover", dict(data), self._lookup
        )
        command_no = disposal["data"]["command_no"]
        zone = self.repository.get_entity(disposal["data"]["zone_id"])
        now = utcnow()
        recovering_zone_id = zone["id"]
        gate_updates = []
        for gate_id in disposal["data"].get("gate_ids") or []:
            gate = self.repository.get_entity(gate_id)
            if not gate:
                continue
            commands = [
                item
                for item in (gate["data"].get("evacuation_commands") or [])
                if item != command_no
            ]
            new_data = dict(gate["data"])
            new_data["evacuation_commands"] = commands
            all_recovered = True
            for zone_id in gate["data"].get("zone_ids") or []:
                if zone_id == recovering_zone_id:
                    continue
                linked = self.repository.get_entity(zone_id)
                if linked and linked["status"] == "evacuating":
                    all_recovered = False
                    break
            status = "open" if not commands and all_recovered else "closed"
            gate_updates.append(
                {
                    "id": gate["id"],
                    "expected_version": gate["version"],
                    "status": status,
                    "data": new_data,
                    "from_status": gate["status"],
                }
            )
        zone_update = {
            "id": zone["id"],
            "expected_version": zone["version"],
            "status": "open",
            "data": dict(zone["data"]),
            "from_status": zone["status"],
        }
        disposal_data = dict(disposal["data"])
        disposal_data.update(patch)
        disposal_data["recovered_at"] = now
        disposal_update = {
            "id": disposal["id"],
            "expected_version": int(expected_version) if expected_version is not None else disposal["version"],
            "status": next_status,
            "data": disposal_data,
            "from_status": disposal["status"],
        }
        self.repository.apply_disposal_recovery(disposal_update, zone_update, gate_updates)
        self.audit.record(
            disposal["id"],
            actor,
            "recover",
            disposal["status"],
            next_status,
            {"patch": patch},
        )
        self.audit.record(
            zone["id"],
            actor,
            "recover",
            zone_update["from_status"],
            "open",
            {"disposal_id": disposal["id"]},
        )
        for gate in gate_updates:
            self.audit.record(
                gate["id"],
                actor,
                "restore",
                gate["from_status"],
                gate["status"],
                {"disposal_id": disposal["id"]},
            )
        return self.repository.get_entity(disposal["id"])

    def backfill_legacy_evacuations(self, actor):
        self._ensure_disposal_role(actor, DISPOSAL_INITIATE_ROLES)
        zones = self.repository.list_entities(kind="zone", status="evacuating")
        created = []
        for zone in zones:
            active = [
                disposal
                for disposal in self._lookup("disposal", "zone_id", zone["id"])
                if disposal["status"] == "active"
            ]
            if active:
                continue
            incidents = self._lookup("incident", "zone_id", zone["id"])
            incident = incidents[-1] if incidents else None
            command_no = "CMD-" + uuid4().hex[:12].upper()
            disposal, zone_update, gate_updates, task_updates = self._build_disposal(
                actor, zone, incident, command_no
            )
            self.repository.apply_disposal_initiation(
                disposal, zone_update, gate_updates, task_updates
            )
            self.audit.record(
                disposal["id"],
                actor,
                "backfill",
                None,
                "active",
                {"command_no": command_no, "legacy": True},
            )
            for gate in gate_updates:
                self.audit.record(
                    gate["id"],
                    actor,
                    "close",
                    gate["from_status"],
                    "closed",
                    {"disposal_id": disposal["id"], "legacy": True},
                )
            created.append(self.repository.get_entity(disposal["id"]))
        return created
