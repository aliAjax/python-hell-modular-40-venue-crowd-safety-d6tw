"""Evacuation disposal order orchestration.

An order ties a zone, its incident, every connected gate and the in-flight
field tasks into one command. Both issue and completion run inside a single
serialized SQLite transaction: any failure rolls the whole order back.
"""
from uuid import uuid4

from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .repository import SCHEMA_VERSION, utcnow
from .rules import (
    EVACUATION_ACTIVE_STATUSES,
    TASK_INFLIGHT_STATUSES,
    TASK_REVIEW_STATUS,
)

ISSUE_REQUIRED = ("zone_id", "incident_id", "command_no", "reason")
RESOLVED_INCIDENT_STATUSES = ("resolved",)
SYSTEM_ACTOR_ID = "system-migration"


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def issue_evacuation(tx, rules, actor, data, order_id=None, backfill=False):
    """Issue (or replay) an evacuation order atomically.

    Returns (order, replayed). With backfill=True the zone is assumed to
    already be evacuating and preconditions are limited to data alignment.
    """
    if not backfill:
        rules.ensure_role(actor, rules.EVACUATION_ROLES)
    _require(data, ISSUE_REQUIRED)
    zone_id = data["zone_id"]
    incident_id = data["incident_id"]
    command_no = data["command_no"]

    zone = tx.get_entity(zone_id)
    if not zone or zone["kind"] != "zone":
        raise NotFoundError("zone not found: " + zone_id)
    incident = tx.get_entity(incident_id)
    if not incident or incident["kind"] != "incident":
        raise NotFoundError("incident not found: " + incident_id)
    if incident["data"].get("zone_id") != zone_id:
        raise ValidationError("incident does not belong to the evacuation zone")
    if incident["data"].get("venue_id") != zone["data"].get("venue_id"):
        raise ValidationError("incident and zone must belong to the same venue")

    # Same command number is idempotent and wins over state preconditions:
    # re-submission (even after completion) hands back the original order.
    existing = tx.find_entities("evacuation_order", "command_no", command_no)
    if existing:
        order = existing[0]
        if order["data"].get("zone_id") != zone_id or order["data"].get("incident_id") != incident_id:
            raise ConflictError(
                "command_no %s is already bound to a different disposal order" % command_no
            )
        return order, True

    if incident["status"] in RESOLVED_INCIDENT_STATUSES and not backfill:
        raise InvalidTransition("cannot evacuate for a resolved incident")

    if not backfill:
        for order in tx.find_entities("evacuation_order", "zone_id", zone_id):
            if order["status"] in EVACUATION_ACTIVE_STATUSES:
                raise ConflictError(
                    "zone %s already has active evacuation order %s (command %s)"
                    % (zone_id, order["id"], order["data"].get("command_no"))
                )
        if zone["status"] not in ("open", "limited"):
            raise InvalidTransition("cannot evacuate zone from status %s" % zone["status"])

    gate_ids = []
    closed_gate_ids = []
    for gate in tx.list_entities(kind="gate"):
        if zone_id not in (gate["data"].get("zone_ids") or []):
            continue
        gate_ids.append(gate["id"])
        gate_data = dict(gate["data"])
        holds = list(gate_data.get("evacuation_holds") or [])
        if command_no not in holds:
            holds.append(command_no)
        gate_data["evacuation_holds"] = holds
        if gate["status"] == "open":
            gate_data["status_before_evacuation"] = "open"
            tx.update_entity(gate["id"], gate["version"], "closed", gate_data)
            tx.append_audit(
                gate["id"], actor.user_id, actor.role, "evacuation_close",
                "open", "closed", {"command_no": command_no, "order_zone": zone_id},
            )
            closed_gate_ids.append(gate["id"])
        else:
            if "status_before_evacuation" not in gate_data:
                gate_data["status_before_evacuation"] = gate["status"]
            tx.update_entity(gate["id"], gate["version"], gate["status"], gate_data)
            tx.append_audit(
                gate["id"], actor.user_id, actor.role, "evacuation_hold",
                gate["status"], gate["status"], {"command_no": command_no, "order_zone": zone_id},
            )

    task_ids = []
    for task in tx.list_entities(kind="task"):
        if task["data"].get("zone_id") != zone_id:
            continue
        if task["status"] not in TASK_INFLIGHT_STATUSES:
            continue
        task_ids.append(task["id"])
        task_data = dict(task["data"])
        history = list(task_data.get("returned_for_review_history") or [])
        record = {
            "reason": data["reason"],
            "command_no": command_no,
            "actor_id": actor.user_id,
            "from_status": task["status"],
        }
        history.append(record)
        task_data["returned_for_review"] = dict(record)
        task_data["returned_for_review_history"] = history
        tx.update_entity(task["id"], task["version"], TASK_REVIEW_STATUS, task_data)
        tx.append_audit(
            task["id"], actor.user_id, actor.role, "return_for_review",
            task["status"], TASK_REVIEW_STATUS,
            {"reason": data["reason"], "command_no": command_no},
        )

    zone_data = dict(zone["data"])
    zone_data["evacuation_command"] = command_no
    if "status_before_evacuation" not in zone_data:
        zone_data["status_before_evacuation"] = zone["status"] if not backfill else "open"
    zone_data["evacuation_reason"] = data["reason"]
    target_status = "evacuating"
    tx.update_entity(zone["id"], zone["version"], target_status, zone_data)
    tx.append_audit(
        zone["id"], actor.user_id, actor.role, "evacuate",
        zone["status"], target_status, {"command_no": command_no, "reason": data["reason"]},
    )

    now = utcnow()
    order_data = {
        "command_no": command_no,
        "zone_id": zone_id,
        "incident_id": incident_id,
        "venue_id": zone["data"].get("venue_id"),
        "reason": data["reason"],
        "gate_ids": gate_ids,
        "closed_gate_ids": closed_gate_ids,
        "task_ids": task_ids,
        "issued_at": data.get("issued_at") or now,
        "issued_by": actor.user_id,
    }
    if backfill:
        order_data["backfilled"] = True
    order_id = order_id or str(uuid4())
    order = tx.create_entity(order_id, "evacuation_order", "executing", order_data, actor.user_id)
    tx.append_audit(
        order_id, actor.user_id, actor.role, "create", None, "executing",
        {"kind": "evacuation_order", "command_no": command_no, "zone_id": zone_id,
         "backfilled": bool(backfill)},
    )
    return order, False


def complete_evacuation(tx, rules, actor, order_id, data, expected_version=None):
    """Close an order and recover the zone; shared gates reopen only on last hold."""
    rules.ensure_role(actor, rules.EVACUATION_ROLES)
    order = tx.get_entity(order_id)
    if not order or order["kind"] != "evacuation_order":
        raise NotFoundError("evacuation order not found: " + order_id)
    expected = int(expected_version) if expected_version is not None else order["version"]
    if order["status"] != "executing":
        raise InvalidTransition("cannot complete evacuation order from status %s" % order["status"])

    command_no = order["data"].get("command_no")
    zone_id = order["data"].get("zone_id")
    zone = tx.get_entity(zone_id)
    if not zone:
        raise NotFoundError("zone not found: " + zone_id)

    incident = tx.get_entity(order["data"].get("incident_id"))
    if not incident:
        raise NotFoundError("incident not found: " + order["data"].get("incident_id"))
    if incident["status"] not in RESOLVED_INCIDENT_STATUSES:
        raise ConflictError(
            "zone cannot recover before incident %s is closed (status=%s)"
            % (incident["id"], incident["status"])
        )

    inflight = [
        task["id"]
        for task in tx.list_entities(kind="task")
        if task["data"].get("zone_id") == zone_id
        and task["status"] in TASK_INFLIGHT_STATUSES
    ]
    if inflight:
        raise ConflictError(
            "zone cannot recover while tasks are in flight: %s" % ",".join(inflight)
        )

    if zone["status"] != "evacuating":
        raise InvalidTransition("zone %s is not evacuating" % zone_id)

    reopened_gate_ids = []
    for gate_id in order["data"].get("gate_ids") or []:
        gate = tx.get_entity(gate_id)
        if not gate:
            continue
        gate_data = dict(gate["data"])
        holds = list(gate_data.get("evacuation_holds") or [])
        if command_no in holds:
            holds.remove(command_no)
        gate_data["evacuation_holds"] = holds
        if holds:
            tx.update_entity(gate["id"], gate["version"], gate["status"], gate_data)
            tx.append_audit(
                gate["id"], actor.user_id, actor.role, "evacuation_hold_released",
                gate["status"], gate["status"],
                {"command_no": command_no, "remaining": holds},
            )
            continue
        prior = gate_data.pop("status_before_evacuation", None)
        # Restore the gate to exactly its pre-evacuation state; a gate that was
        # closed before the evacuation stays closed.
        target = prior if prior in ("open", "restricted", "closed") else "open"
        if gate["status"] == target:
            tx.update_entity(gate["id"], gate["version"], target, gate_data)
        else:
            tx.update_entity(gate["id"], gate["version"], target, gate_data)
            tx.append_audit(
                gate["id"], actor.user_id, actor.role, "evacuation_reopen",
                gate["status"], target,
                {"command_no": command_no, "shared_zone_recovery": True},
            )
        if target in ("open", "restricted"):
            reopened_gate_ids.append(gate["id"])

    zone_data = dict(zone["data"])
    prior_zone = zone_data.pop("status_before_evacuation", None)
    recovered_status = prior_zone if prior_zone in ("open", "limited") else "open"
    zone_data.pop("evacuation_command", None)
    zone_data["recovered_by"] = actor.user_id
    if data and data.get("checklist"):
        zone_data["recovery_checklist"] = data["checklist"]
    tx.update_entity(zone["id"], zone["version"], recovered_status, zone_data)
    tx.append_audit(
        zone["id"], actor.user_id, actor.role, "recover",
        "evacuating", recovered_status, {"command_no": command_no},
    )

    order_data = dict(order["data"])
    order_data["reopened_gate_ids"] = reopened_gate_ids
    order_data["recovered_at"] = (data or {}).get("recovered_at") or utcnow()
    order_data["recovered_by"] = actor.user_id
    if data and data.get("checklist"):
        order_data["checklist"] = data["checklist"]
    updated = tx.update_entity(order["id"], expected, "completed", order_data)
    tx.append_audit(
        order_id, actor.user_id, actor.role, "complete",
        "executing", "completed",
        {"command_no": command_no, "reopened_gate_ids": reopened_gate_ids},
    )
    return updated


def upgrade_legacy_evacuations(tx, rules):
    """Backfill orders for zones already evacuating in a pre-upgrade database."""
    if tx.get_user_version() >= SCHEMA_VERSION:
        return []
    created = []
    for zone in tx.list_entities(kind="zone", status="evacuating"):
        if any(
            order["status"] in EVACUATION_ACTIVE_STATUSES
            for order in tx.find_entities("evacuation_order", "zone_id", zone["id"])
        ):
            continue
        incident = None
        for candidate in tx.list_entities(kind="incident"):
            if candidate["data"].get("zone_id") == zone["id"] and candidate["status"] != "resolved":
                incident = candidate
                break
        if incident is None:
            incident_data = {
                "venue_id": zone["data"].get("venue_id"),
                "zone_id": zone["id"],
                "incident_key": "migration:%s" % zone["id"],
                "source_ref": "legacy-evacuation-%s" % zone["id"],
                "incident_type": "security",
                "severity": "high",
                "priority_score": 65,
                "reported_at": zone.get("created_at"),
                "synthetic": True,
            }
            incident = tx.create_entity(
                "incident-%s" % uuid4(), "incident", "dispatched", incident_data, SYSTEM_ACTOR_ID
            )
            tx.append_audit(
                incident["id"], SYSTEM_ACTOR_ID, "admin", "create", None, "dispatched",
                {"kind": "incident", "backfilled": True},
            )
        order_id = "evacuation-order-legacy-%s" % zone["id"]
        command_no = "LEGACY-%s" % zone["id"]
        actor = type("MigrationActor", (), {"user_id": SYSTEM_ACTOR_ID, "role": "admin"})()
        order, _ = issue_evacuation(
            tx,
            rules,
            actor,
            {
                "zone_id": zone["id"],
                "incident_id": incident["id"],
                "command_no": command_no,
                "reason": "legacy data upgrade: backfill evacuation order",
            },
            order_id=order_id,
            backfill=True,
        )
        created.append(order)
    tx.set_user_version(SCHEMA_VERSION)
    return created
