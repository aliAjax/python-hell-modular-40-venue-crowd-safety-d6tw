from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .evacuation import complete_evacuation, issue_evacuation, upgrade_legacy_evacuations
from .rules import RuleEngine

# Kinds that are not created through the generic entity endpoint.
ORCHESTRATED_KINDS = ("evacuation_order",)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self._migrate_legacy_data()

    def _migrate_legacy_data(self):
        with self.repository.unit_of_work() as tx:
            upgrade_legacy_evacuations(tx, self.rules)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind in ORCHESTRATED_KINDS:
            raise PermissionDenied(
                "evacuation orders are issued via POST /api/evacuation_orders/issue"
            )
        payload = dict(data or {})
        with self.repository.unit_of_work() as tx:
            lookup = lambda k, f, v: tx.find_entities(k, f, v)
            if idempotency_key:
                existing = tx.get_idempotency(actor.user_id, idempotency_key)
                if existing:
                    entity = tx.get_entity(existing)
                    if entity:
                        return entity
            validated = self.rules.validate_create(actor, kind, payload, lookup)
            if validated:
                payload.update(validated)
            entity_id = str(payload.pop("id", "") or uuid4())
            if tx.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind)
            entity = tx.create_entity(entity_id, kind, status, payload, actor.user_id)
            tx.append_audit(
                entity_id, actor.user_id, actor.role, "create", None, status, {"kind": kind}
            )
            if idempotency_key:
                tx.save_idempotency(actor.user_id, idempotency_key, entity_id)
            return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.repository.unit_of_work() as tx:
            entity = tx.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = int(expected_version) if expected_version is not None else entity["version"]
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}),
                lambda k, f, v: tx.find_entities(k, f, v),
            )
            merged = dict(entity["data"])
            merged.update(patch)
            updated = tx.update_entity(entity_id, expected, next_status, merged)
            tx.append_audit(
                entity_id,
                actor.user_id,
                actor.role,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
            )
            return updated

    def issue_evacuation(self, actor, data, idempotency_key=None):
        payload = dict(data or {})
        with self.repository.unit_of_work() as tx:
            if idempotency_key:
                existing = tx.get_idempotency(actor.user_id, idempotency_key)
                if existing:
                    entity = tx.get_entity(existing)
                    if entity:
                        return entity
            order, replayed = issue_evacuation(tx, self.rules, actor, payload)
            if not replayed and idempotency_key:
                tx.save_idempotency(actor.user_id, idempotency_key, order["id"])
            return order

    def complete_evacuation(self, actor, order_id, data=None, expected_version=None):
        with self.repository.unit_of_work() as tx:
            return complete_evacuation(
                tx, self.rules, actor, order_id, dict(data or {}), expected_version
            )

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
