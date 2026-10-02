from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository

# 允许办理续期的权属状态；关闭、暂停等状态一律受限
RENEWABLE_STATUSES = {"active", "expired"}


def advance_years(base: date, years: int) -> date:
    """按整年顺延权属到期日。

    规则：到期日按“年”对年对月顺延；目标年没有对应日（典型为 2 月 29 日
    落到平年）时，回落到该月最后一天（平年 2 月 28 日）。普通月末日期
    （如 1 月 31 日）在各年均存在，保持对日不变。闰日一旦回落为 2 月 28 日，
    后续续期即在 2 月 28 日的对日链条上继续顺延。
    """
    target_year = base.year + years
    try:
        return base.replace(year=target_year)
    except ValueError:
        last_day = calendar.monthrange(target_year, base.month)[1]
        return date(target_year, base.month, min(base.day, last_day))


class MortuaryService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MortuaryRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    def create_case(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        values = dict(payload)
        values["death_time"] = to_storage(values["death_time"])
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            if repo.case_ref(values["external_ref"]):
                raise ConflictError("外部业务编号已存在")
            cursor = connection.execute(
                "INSERT INTO mortuary_cases(external_ref,decedent_name,identity_number,death_time,received_from,family_contact,family_phone,special_notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (values["external_ref"], values["decedent_name"], values.get("identity_number"), values["death_time"], values["received_from"], values["family_contact"], values["family_phone"], values.get("special_notes", ""), now, now),
            )
            case = repo.case(int(cursor.lastrowid)) or {}
            repo.event("case", case["id"], "case.registered", actor, {"external_ref": case["external_ref"], "received_from": case["received_from"]}, now)
            return case

    def list_cases(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mortuary_cases"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def get_case(self, case_id: int) -> dict[str, Any]:
        case = self.repository.case(case_id)
        if case is None:
            raise NotFoundError("逝者业务档案不存在")
        case["custody_transfers"] = [dict(row) for row in self.connection.execute("SELECT * FROM custody_transfers WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
        case["reservations"] = [dict(row) for row in self.connection.execute("SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.case_id=? ORDER BY r.start_at", (case_id,)).fetchall()]
        case["service_orders"] = [dict(row) for row in self.connection.execute("SELECT * FROM funeral_service_orders WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
        case["timeline"] = self.repository.timeline("case", case_id)
        return case

    def request_transfer(self, case_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            case = repo.case(case_id)
            if case is None:
                raise NotFoundError("逝者业务档案不存在")
            existing = repo.transfer_key(case_id, payload["idempotency_key"])
            if existing:
                return existing
            if case["current_location"] and case["current_location"] != payload["from_location"]:
                raise ConflictError("交接起点与当前保管位置不一致", context={"current_location": case["current_location"]})
            cursor = connection.execute("INSERT INTO custody_transfers(case_id,from_location,to_location,seal_code,requested_by,idempotency_key,requested_at) VALUES(?,?,?,?,?,?,?)", (case_id, payload["from_location"], payload["to_location"], payload["seal_code"], payload["requested_by"], payload["idempotency_key"], now))
            transfer = repo.transfer(int(cursor.lastrowid)) or {}
            repo.event("case", case_id, "custody.requested", payload["requested_by"], {"transfer_id": transfer["id"], "from": payload["from_location"], "to": payload["to_location"]}, now)
            return transfer

    def accept_transfer(self, transfer_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            transfer = repo.transfer(transfer_id)
            if transfer is None:
                raise NotFoundError("保管交接记录不存在")
            if transfer["status"] == "accepted":
                return transfer
            if transfer["status"] != "pending":
                raise ConflictError("当前交接状态不能确认")
            if transfer["seal_code"] != payload["observed_seal_code"]:
                connection.execute("UPDATE custody_transfers SET status='rejected',accepted_by=?,observed_seal_code=?,condition_note=?,accepted_at=? WHERE id=?", (payload["accepted_by"], payload["observed_seal_code"], payload["condition_note"], now, transfer_id))
                repo.event("case", transfer["case_id"], "custody.rejected", payload["accepted_by"], {"transfer_id": transfer_id, "reason": "seal_mismatch"}, now)
                raise ConflictError("封签编号不一致，交接已拒绝")
            connection.execute("UPDATE custody_transfers SET status='accepted',accepted_by=?,observed_seal_code=?,condition_note=?,accepted_at=? WHERE id=?", (payload["accepted_by"], payload["observed_seal_code"], payload["condition_note"], now, transfer_id))
            connection.execute("UPDATE mortuary_cases SET status='in_custody',current_location=?,version=version+1,updated_at=? WHERE id=?", (transfer["to_location"], now, transfer["case_id"]))
            repo.event("case", transfer["case_id"], "custody.accepted", payload["accepted_by"], {"transfer_id": transfer_id, "location": transfer["to_location"]}, now)
            return repo.transfer(transfer_id) or {}

    def create_resource(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            if repo.resource_code(payload["code"]):
                raise ConflictError("设施资源编码已存在")
            cursor = connection.execute("INSERT INTO facility_resources(code,name,kind,site_code,capacity,attributes_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["code"], payload["name"], payload["kind"], payload["site_code"], payload["capacity"], json.dumps(payload["attributes"], ensure_ascii=False, sort_keys=True), now, now))
            resource = repo.resource(int(cursor.lastrowid)) or {}
            repo.event("resource", resource["id"], "resource.created", actor, {"code": payload["code"], "kind": payload["kind"]}, now)
            return resource

    def list_resources(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.connection.execute("SELECT * FROM facility_resources WHERE kind=? ORDER BY code", (kind,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM facility_resources ORDER BY kind,code").fetchall()
        return [dict(row) for row in rows]

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        start_at = to_storage(payload["start_at"])
        end_at = to_storage(payload["end_at"])
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            resource = repo.resource_code(payload["resource_code"])
            if resource is None or not resource["active"]:
                raise NotFoundError("设施资源不存在或已停用")
            if repo.case(payload["case_id"]) is None:
                raise NotFoundError("逝者业务档案不存在")
            existing = repo.reservation_key(resource["id"], payload["idempotency_key"])
            if existing:
                if existing["case_id"] != payload["case_id"] or existing["start_at"] != start_at or existing["end_at"] != end_at:
                    raise ConflictError("同一幂等键对应了不同预约内容")
                return repo.reservation(existing["id"]) or existing
            conflicts = repo.conflicts(resource["id"], start_at, end_at)
            if len(conflicts) >= int(resource["capacity"]):
                raise ConflictError("预约时段与现有安排冲突", context={"conflict_ids": [x["id"] for x in conflicts]})
            cursor = connection.execute("INSERT INTO facility_reservations(resource_id,case_id,start_at,end_at,purpose,created_by,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (resource["id"], payload["case_id"], start_at, end_at, payload["purpose"], payload["created_by"], payload["idempotency_key"], now, now))
            reservation = repo.reservation(int(cursor.lastrowid)) or {}
            connection.execute("UPDATE mortuary_cases SET status='services_planned',version=version+1,updated_at=? WHERE id=? AND status IN ('registered','in_custody')", (now, payload["case_id"]))
            repo.event("case", payload["case_id"], "reservation.confirmed", payload["created_by"], {"reservation_id": reservation["id"], "resource_code": payload["resource_code"], "start_at": start_at, "end_at": end_at}, now)
            return reservation

    def cancel_reservation(self, reservation_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            reservation = repo.reservation(reservation_id)
            if reservation is None:
                raise NotFoundError("预约记录不存在")
            if reservation["status"] == "cancelled":
                return reservation
            if reservation["status"] == "completed":
                raise ConflictError("已完成的预约不能取消")
            connection.execute("UPDATE facility_reservations SET status='cancelled',updated_at=? WHERE id=?", (now, reservation_id))
            repo.event("case", reservation["case_id"], "reservation.cancelled", actor, {"reservation_id": reservation_id, "reason": reason}, now)
            return repo.reservation(reservation_id) or {}

    def add_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        amount = payload["quantity"] * payload["unit_price_cents"]
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            if repo.case(payload["case_id"]) is None:
                raise NotFoundError("逝者业务档案不存在")
            cursor = connection.execute("INSERT INTO funeral_service_orders(case_id,service_code,quantity,unit_price_cents,amount_cents,requested_by,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (payload["case_id"], payload["service_code"], payload["quantity"], payload["unit_price_cents"], amount, payload["requested_by"], payload["notes"], now, now))
            order = repo.order(int(cursor.lastrowid)) or {}
            repo.event("case", payload["case_id"], "service_order.created", payload["requested_by"], {"order_id": order["id"], "service_code": payload["service_code"], "amount_cents": amount}, now)
            return order

    def confirm_order(self, order_id: int, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            order = repo.order(order_id)
            if order is None:
                raise NotFoundError("服务订单不存在")
            if order["status"] == "confirmed":
                return order
            if order["status"] != "draft":
                raise ConflictError("当前订单状态不能确认")
            connection.execute("UPDATE funeral_service_orders SET status='confirmed',updated_at=? WHERE id=?", (now, order_id))
            repo.event("case", order["case_id"], "service_order.confirmed", actor, {"order_id": order_id}, now)
            return repo.order(order_id) or {}

    def create_right(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            if repo.right_plot(payload["plot_code"]):
                raise ConflictError("墓位已经登记权属")
            cursor = connection.execute("INSERT INTO burial_rights(plot_code,holder_name,holder_identity,starts_on,expires_on,case_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["plot_code"], payload["holder_name"], payload["holder_identity"], payload["starts_on"].isoformat(), payload["expires_on"].isoformat(), payload.get("case_id"), now, now))
            right = repo.right(int(cursor.lastrowid)) or {}
            repo.event("burial_right", right["id"], "right.created", actor, {"plot_code": right["plot_code"], "expires_on": right["expires_on"]}, now)
            return right

    def get_right(self, right_id: int) -> dict[str, Any]:
        right = self.repository.right(right_id)
        if right is None:
            raise NotFoundError("墓位权属不存在")
        return self._right_detail(right_id, self.repository)

    def _right_detail(self, right_id: int, repo: MortuaryRepository, *, renewal: dict[str, Any] | None = None, replayed: bool = False) -> dict[str, Any]:
        right = repo.right(right_id)
        if right is None:  # pragma: no cover - 调用前已确认存在
            raise NotFoundError("墓位权属不存在")
        detail = dict(right)
        detail["renewals"] = repo.renewals(right_id)
        if renewal is not None:
            detail["latest_renewal"] = renewal
            detail["replayed"] = replayed
        return detail

    def renew_right(self, right_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        years = int(payload["years"])
        payment_reference = payload["payment_reference"]
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            right = repo.right(right_id)
            if right is None:
                raise NotFoundError("墓位权属不存在")

            # 同一支付凭证只能对应一次续期：重复提交直接重放首次办理结果，
            # 不改动权属、不新增历史，也不受权属后续状态变化影响。
            existing = repo.renewal_by_payment(payment_reference)
            if existing is not None:
                if int(existing["right_id"]) != right_id:
                    raise ConflictError(
                        "支付凭证已用于其他墓位权属的续期",
                        context={"payment_reference": payment_reference, "right_id": int(existing["right_id"])},
                    )
                if int(existing["years"]) != years:
                    raise ConflictError(
                        "同一支付凭证对应了不同的续期年限",
                        context={"payment_reference": payment_reference, "years": int(existing["years"]), "requested_years": years},
                    )
                return self._right_detail(right_id, repo, renewal=existing, replayed=True)

            if right["status"] not in RENEWABLE_STATUSES:
                label = {"closed": "已关闭", "suspended": "已暂停", "revoked": "已注销", "transferred": "已转出"}.get(right["status"], right["status"])
                raise ConflictError(
                    f"权属当前为{label}状态，不能续期",
                    context={"right_id": right_id, "status": right["status"]},
                )

            previous = date.fromisoformat(right["expires_on"])
            # 续期自上一到期日起按整年顺延（不缩短已购期限）；闰日落到平年时回落到 2 月 28 日。
            new_expiry = advance_years(previous, years)
            previous_version = int(right["version"])
            new_version = previous_version + 1
            try:
                cursor = connection.execute(
                    "INSERT INTO burial_right_renewals(right_id,previous_expires_on,new_expires_on,years,payment_reference,handled_by,previous_version,new_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (right_id, previous.isoformat(), new_expiry.isoformat(), years, payment_reference, payload["handled_by"], previous_version, new_version, now),
                )
            except sqlite3.IntegrityError:
                # 并发下凭证唯一约束兜底：回滚本次写入，重放已存在的那一次续期。
                existing = repo.renewal_by_payment(payment_reference)
                if existing is not None and int(existing["right_id"]) == right_id and int(existing["years"]) == years:
                    return self._right_detail(right_id, repo, renewal=existing, replayed=True)
                raise ConflictError("支付凭证已被使用", context={"payment_reference": payment_reference})
            renewal = repo.renewal(int(cursor.lastrowid)) or {}
            connection.execute(
                "UPDATE burial_rights SET expires_on=?,status='active',version=?,updated_at=? WHERE id=?",
                (new_expiry.isoformat(), new_version, now, right_id),
            )
            repo.event(
                "burial_right", right_id, "right.renewed", payload["handled_by"],
                {"previous_expires_on": previous.isoformat(), "new_expires_on": new_expiry.isoformat(),
                 "years": years, "payment_reference": payment_reference,
                 "previous_version": previous_version, "new_version": new_version},
                now,
            )
            return self._right_detail(right_id, repo, renewal=renewal)

    def create_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        order_ids = list(dict.fromkeys(payload["order_ids"]))
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            orders = [repo.order(order_id) for order_id in order_ids]
            if any(order is None for order in orders):
                raise NotFoundError("存在找不到的服务订单")
            typed = [order for order in orders if order is not None]
            if any(order["case_id"] != payload["case_id"] for order in typed):
                raise ValidationError("账单只能包含同一业务档案的订单")
            if any(order["status"] != "confirmed" for order in typed):
                raise ConflictError("只有已确认订单可以开具账单")
            amount = sum(int(order["amount_cents"]) for order in typed)
            cursor = connection.execute("INSERT INTO invoices(case_id,amount_cents,created_by,created_at,updated_at) VALUES(?,?,?,?,?)", (payload["case_id"], amount, payload["created_by"], now, now))
            invoice_id = int(cursor.lastrowid)
            for order in typed:
                connection.execute("INSERT INTO invoice_items(invoice_id,order_id,amount_cents) VALUES(?,?,?)", (invoice_id, order["id"], order["amount_cents"]))
                connection.execute("UPDATE funeral_service_orders SET status='invoiced',updated_at=? WHERE id=?", (now, order["id"]))
            repo.event("case", payload["case_id"], "invoice.issued", payload["created_by"], {"invoice_id": invoice_id, "amount_cents": amount, "order_ids": order_ids}, now)
            return self.get_invoice(invoice_id, repo)

    def pay(self, invoice_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            invoice = repo.invoice(invoice_id)
            if invoice is None:
                raise NotFoundError("账单不存在")
            duplicate = connection.execute("SELECT * FROM payments WHERE external_reference=?", (payload["external_reference"],)).fetchone()
            if duplicate:
                if duplicate["invoice_id"] != invoice_id or duplicate["amount_cents"] != payload["amount_cents"]:
                    raise ConflictError("支付流水号已用于其他款项")
                return self.get_invoice(invoice_id, repo)
            remaining = int(invoice["amount_cents"]) - int(invoice["paid_cents"])
            if payload["amount_cents"] > remaining:
                raise ValidationError("支付金额超过账单未付余额")
            connection.execute("INSERT INTO payments(invoice_id,amount_cents,channel,external_reference,received_by,received_at) VALUES(?,?,?,?,?,?)", (invoice_id, payload["amount_cents"], payload["channel"], payload["external_reference"], payload["received_by"], now))
            paid = int(invoice["paid_cents"]) + payload["amount_cents"]
            status = "paid" if paid == int(invoice["amount_cents"]) else "partially_paid"
            connection.execute("UPDATE invoices SET paid_cents=?,status=?,updated_at=? WHERE id=?", (paid, status, now, invoice_id))
            repo.event("case", invoice["case_id"], "invoice.payment_received", payload["received_by"], {"invoice_id": invoice_id, "amount_cents": payload["amount_cents"], "external_reference": payload["external_reference"]}, now)
            return self.get_invoice(invoice_id, repo)

    def get_invoice(self, invoice_id: int, repo: MortuaryRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        invoice = repo.invoice(invoice_id)
        if invoice is None:
            raise NotFoundError("账单不存在")
        invoice["items"] = [dict(row) for row in repo.connection.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY order_id", (invoice_id,)).fetchall()]
        invoice["payments"] = [dict(row) for row in repo.connection.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()]
        invoice["remaining_cents"] = int(invoice["amount_cents"]) - int(invoice["paid_cents"])
        return invoice
