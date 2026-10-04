import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError

SCHEMA_VERSION = 2


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UnitOfWork:
    """All reads/writes inside one unit run against a single locked connection."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        row = self.connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        row = self.connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        self.connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ?",
            (status, payload, now, entity_id),
        )
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        self.connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def get_idempotency(self, actor_id, idem_key):
        row = self.connection.execute(
            "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
            (actor_id, idem_key),
        ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        self.connection.execute(
            "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (actor_id, idem_key, entity_id, utcnow()),
        )

    def get_user_version(self):
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def set_user_version(self, version):
        self.connection.execute("PRAGMA user_version = %d" % int(version))


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @contextmanager
    def unit_of_work(self):
        connection = self._connect()
        connection.isolation_level = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield UnitOfWork(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_entity(self, entity_id, kind, status, data, actor_id):
        with self.unit_of_work() as tx:
            return tx.create_entity(entity_id, kind, status, data, actor_id)

    def get_entity(self, entity_id):
        with self.unit_of_work() as tx:
            return tx.get_entity(entity_id)

    def list_entities(self, kind=None, status=None):
        with self.unit_of_work() as tx:
            return tx.list_entities(kind=kind, status=status)

    def find_entities(self, kind, field, value):
        with self.unit_of_work() as tx:
            return tx.find_entities(kind, field, value)

    def update_entity(self, entity_id, expected_version, status, data):
        with self.unit_of_work() as tx:
            return tx.update_entity(entity_id, expected_version, status, data)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self.unit_of_work() as tx:
            return tx.append_audit(
                entity_id, actor_id, actor_role, action, from_status, to_status, detail
            )

    def list_audit(self, entity_id=None):
        with self.unit_of_work() as tx:
            if entity_id:
                rows = tx.connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = tx.connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self.unit_of_work() as tx:
            return tx.get_idempotency(actor_id, idem_key)

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self.unit_of_work() as tx:
            return tx.save_idempotency(actor_id, idem_key, entity_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
