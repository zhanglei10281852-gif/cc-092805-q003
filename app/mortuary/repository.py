from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r''' 
CREATE TABLE IF NOT EXISTS mortuary_cases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_ref TEXT NOT NULL UNIQUE,
 decedent_name TEXT NOT NULL, identity_number TEXT, death_time TEXT NOT NULL,
 received_from TEXT NOT NULL, family_contact TEXT NOT NULL, family_phone TEXT NOT NULL,
 special_notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'registered',
 current_location TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 from_location TEXT NOT NULL, to_location TEXT NOT NULL, seal_code TEXT NOT NULL,
 requested_by TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', observed_seal_code TEXT NOT NULL DEFAULT '',
 condition_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 idempotency_key TEXT NOT NULL, requested_at TEXT NOT NULL, accepted_at TEXT,
 UNIQUE(case_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS facility_resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 kind TEXT NOT NULL, site_code TEXT NOT NULL, capacity INTEGER NOT NULL,
 attributes_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_reservations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id INTEGER NOT NULL REFERENCES facility_resources(id),
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created_by TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(resource_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reservation_window ON facility_reservations(resource_id,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS funeral_service_orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 service_code TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
 amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft', requested_by TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_rights (
 id INTEGER PRIMARY KEY AUTOINCREMENT, plot_code TEXT NOT NULL UNIQUE, holder_name TEXT NOT NULL,
 holder_identity TEXT NOT NULL, starts_on TEXT NOT NULL, expires_on TEXT NOT NULL,
 case_id INTEGER REFERENCES mortuary_cases(id), status TEXT NOT NULL DEFAULT 'active',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_right_renewals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, right_id INTEGER NOT NULL REFERENCES burial_rights(id),
 previous_expires_on TEXT NOT NULL, new_expires_on TEXT NOT NULL, years INTEGER NOT NULL,
 payment_reference TEXT NOT NULL UNIQUE, handled_by TEXT NOT NULL,
 previous_version INTEGER NOT NULL DEFAULT 0, new_version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_right_renewals ON burial_right_renewals(right_id,id);
CREATE TABLE IF NOT EXISTS invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 amount_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'issued', created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_items (
 invoice_id INTEGER NOT NULL REFERENCES invoices(id), order_id INTEGER NOT NULL UNIQUE REFERENCES funeral_service_orders(id),
 amount_cents INTEGER NOT NULL, PRIMARY KEY(invoice_id,order_id)
);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
 amount_cents INTEGER NOT NULL, channel TEXT NOT NULL, external_reference TEXT NOT NULL UNIQUE,
 received_by TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mortuary_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mortuary_event ON mortuary_events(aggregate_type,aggregate_id,id);
'''


class MortuaryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(burial_right_renewals)").fetchall()}
        if "previous_version" not in columns:
            self.connection.execute("ALTER TABLE burial_right_renewals ADD COLUMN previous_version INTEGER NOT NULL DEFAULT 0")
        if "new_version" not in columns:
            self.connection.execute("ALTER TABLE burial_right_renewals ADD COLUMN new_version INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute("INSERT INTO mortuary_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)", (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def case_ref(self, ref: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE external_ref=?", (ref,)).fetchone())

    def transfer(self, transfer_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE id=?", (transfer_id,)).fetchone())

    def transfer_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def resource_code(self, code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE code=?", (code,)).fetchone())

    def resource(self, resource_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE id=?", (resource_id,)).fetchone())

    def reservation_key(self, resource_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND idempotency_key=?", (resource_id, key)).fetchone())

    def reservation(self, reservation_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.id=?", (reservation_id,)).fetchone())

    def conflicts(self, resource_id: int, start_at: str, end_at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND status='confirmed' AND start_at>=? AND start_at<? ORDER BY start_at", (resource_id, start_at, end_at)).fetchall()
        return [dict(row) for row in rows]

    def order(self, order_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM funeral_service_orders WHERE id=?", (order_id,)).fetchone())

    def right(self, right_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE id=?", (right_id,)).fetchone())

    def right_plot(self, plot_code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE plot_code=?", (plot_code,)).fetchone())

    def renewal(self, renewal_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_right_renewals WHERE id=?", (renewal_id,)).fetchone())

    def renewal_by_payment(self, payment_reference: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_right_renewals WHERE payment_reference=?", (payment_reference,)).fetchone())

    def renewals(self, right_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM burial_right_renewals WHERE right_id=? ORDER BY id", (right_id,)).fetchall()
        return [dict(row) for row in rows]

    def invoice(self, invoice_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM mortuary_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id", (kind, str(aggregate_id))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
