from __future__ import annotations


def create_case(client, ref: str = "CASE-001") -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={"external_ref": ref, "decedent_name": "张德安", "identity_number": "ID-440100-1938", "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": "家属要求核对随身物品"})
    assert response.status_code == 201, response.text
    return response.json()


def test_case_custody_timeline_and_idempotency(client):
    case = create_case(client)
    payload = {"from_location": "接运车辆A", "to_location": "冷藏室C-01", "seal_code": "SEAL-1001", "requested_by": "driver-li", "idempotency_key": "custody-case-001"}
    requested = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    repeated = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    assert requested.status_code == 201
    assert repeated.json()["id"] == requested.json()["id"]
    accepted = client.post(f"/api/mortuary/custody-transfers/{requested.json()['id']}/accept", json={"accepted_by": "keeper-wang", "observed_seal_code": "SEAL-1001", "condition_note": "封签完整"})
    assert accepted.status_code == 200
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "in_custody"
    assert detail["current_location"] == "冷藏室C-01"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.accepted"]


def test_resource_reservation_and_cancellation(client):
    first = create_case(client, "CASE-R-001")
    second = create_case(client, "CASE-R-002")
    assert client.post("/api/mortuary/resources?actor=scheduler", json={"code": "HALL-A", "name": "送别厅A", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {"seats": 120}}).status_code == 201
    payload = {"resource_code": "HALL-A", "case_id": first["id"], "start_at": "2026-09-29T09:00:00Z", "end_at": "2026-09-29T10:00:00Z", "purpose": "家属告别", "created_by": "scheduler", "idempotency_key": "hall-a-first-001"}
    reserved = client.post("/api/mortuary/reservations", json=payload)
    assert reserved.status_code == 201
    next_slot = dict(payload, case_id=second["id"], start_at="2026-09-29T10:00:00Z", end_at="2026-09-29T11:00:00Z", idempotency_key="hall-a-second-001")
    assert client.post(f"/api/mortuary/reservations/{reserved.json()['id']}/cancel?actor=scheduler&reason=家属调整时间").status_code == 200
    assert client.post("/api/mortuary/reservations", json=next_slot).status_code == 201


def test_orders_invoice_and_payment(client):
    case = create_case(client, "CASE-F-001")
    order_ids = []
    for code, quantity, price in (("body-care", 1, 80000), ("farewell-hall", 2, 120000)):
        order = client.post("/api/mortuary/service-orders", json={"case_id": case["id"], "service_code": code, "quantity": quantity, "unit_price_cents": price, "requested_by": "family-service", "notes": "已与家属核对"})
        assert order.status_code == 201
        order_ids.append(order.json()["id"])
        assert client.post(f"/api/mortuary/service-orders/{order.json()['id']}/confirm?actor=finance-reviewer").status_code == 200
    invoice = client.post("/api/mortuary/invoices", json={"case_id": case["id"], "order_ids": order_ids, "created_by": "cashier"})
    assert invoice.status_code == 201
    payment = {"amount_cents": 100000, "channel": "bank", "external_reference": "PAY-20260928-001", "received_by": "cashier"}
    assert client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment).status_code == 200
    repeated = client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment)
    assert repeated.status_code == 200 and repeated.json()["paid_cents"] == 100000


def test_burial_right_normal_renewal(client):
    right = client.post("/api/mortuary/burial-rights?actor=cemetery-clerk", json={"plot_code": "A-01-001", "holder_name": "李冬梅", "holder_identity": "ID-3301-001", "starts_on": "2025-01-01", "expires_on": "2045-01-01", "case_id": None})
    assert right.status_code == 201
    renewed = client.post(f"/api/mortuary/burial-rights/{right.json()['id']}/renew", json={"years": 5, "handled_by": "cemetery-clerk", "payment_reference": "RIGHT-PAY-001"})
    assert renewed.status_code == 200 and renewed.json()["expires_on"] == "2050-01-01"
    record = renewed.json()["latest_renewal"]
    assert record["previous_expires_on"] == "2045-01-01"
    assert record["new_expires_on"] == "2050-01-01"
    assert (record["previous_version"], record["new_version"]) == (1, 2)
    assert renewed.json()["version"] == 2


def _make_right(client, plot: str, expires_on: str) -> dict:
    response = client.post("/api/mortuary/burial-rights?actor=cemetery-clerk", json={"plot_code": plot, "holder_name": "权属人", "holder_identity": f"ID-{plot}", "starts_on": "2005-01-01", "expires_on": expires_on, "case_id": None})
    assert response.status_code == 201, response.text
    return response.json()


def _renew(client, right_id: int, reference: str, years: int = 1, handled_by: str = "cemetery-clerk"):
    return client.post(f"/api/mortuary/burial-rights/{right_id}/renew", json={"years": years, "handled_by": handled_by, "payment_reference": reference})


def test_leap_day_renewal_does_not_500_and_uses_feb28_rule(client):
    # 核心事故场景：2 月 29 日到期，普通年份续期过去直接 500
    right = _make_right(client, "A-02-029", "2024-02-29")
    first = _renew(client, right["id"], "LEAP-PAY-001")
    assert first.status_code == 200, first.text
    assert first.json()["expires_on"] == "2025-02-28"
    # 续期链条在 2 月 28 日对上继续顺延
    second = _renew(client, right["id"], "LEAP-PAY-002")
    assert second.status_code == 200 and second.json()["expires_on"] == "2026-02-28"
    third = _renew(client, right["id"], "LEAP-PAY-003", years=2)
    assert third.status_code == 200 and third.json()["expires_on"] == "2028-02-28"
    # 直接按 4 的倍数跨闰年：闰日对日仍为 2 月 29 日
    direct = _make_right(client, "A-02-030", "2024-02-29")
    response = _renew(client, direct["id"], "LEAP-PAY-004", years=4)
    assert response.status_code == 200 and response.json()["expires_on"] == "2028-02-29"
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert [r["new_expires_on"] for r in detail["renewals"]] == ["2025-02-28", "2026-02-28", "2028-02-28"]
    assert detail["version"] == 4


def test_month_end_and_multi_year_renewal(client):
    jan31 = _make_right(client, "A-03-031", "2026-01-31")
    response = _renew(client, jan31["id"], "MONTHEND-PAY-001")
    assert response.status_code == 200 and response.json()["expires_on"] == "2027-01-31"
    long_term = _make_right(client, "A-04-001", "2024-12-31")
    response = _renew(client, long_term["id"], "LONG-PAY-001", years=20)
    assert response.status_code == 200 and response.json()["expires_on"] == "2044-12-31"
    assert response.json()["latest_renewal"]["years"] == 20


def test_duplicate_payment_reference_replays_single_renewal(client):
    right = _make_right(client, "A-05-001", "2026-03-15")
    payload = {"years": 3, "handled_by": "cemetery-clerk", "payment_reference": "DUP-PAY-001"}
    first = client.post(f"/api/mortuary/burial-rights/{right['id']}/renew", json=payload)
    assert first.status_code == 200
    assert first.json()["expires_on"] == "2029-03-15" and first.json()["replayed"] is False
    # 柜台重复提交同一支付凭证：只形成一次续期
    second = client.post(f"/api/mortuary/burial-rights/{right['id']}/renew", json=payload)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["expires_on"] == "2029-03-15"
    assert second.json()["version"] == 2
    assert second.json()["latest_renewal"]["id"] == first.json()["latest_renewal"]["id"]
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert len(detail["renewals"]) == 1


def test_replay_succeeds_even_after_right_suspended(client):
    from app.database import get_connection
    right = _make_right(client, "A-06-001", "2026-04-10")
    first = _renew(client, right["id"], "REPLAY-PAY-001", years=2)
    assert first.status_code == 200
    get_connection().execute("UPDATE burial_rights SET status='suspended' WHERE id=?", (right["id"],))
    # 已暂停不影响同一凭证的结果重放，也不会再次续期
    second = _renew(client, right["id"], "REPLAY-PAY-001", years=2)
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert second.json()["expires_on"] == "2028-04-10"
    assert len(second.json()["renewals"]) == 1


def test_payment_reference_bound_to_other_right_conflicts(client):
    first = _make_right(client, "A-07-001", "2026-05-01")
    other = _make_right(client, "A-07-002", "2026-05-01")
    assert _renew(client, first["id"], "SHARED-PAY-001").status_code == 200
    response = _renew(client, other["id"], "SHARED-PAY-001")
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["context"]["payment_reference"] == "SHARED-PAY-001"
    # 被拒的一方没有留下任何续期记录，版本也不变
    detail = client.get(f"/api/mortuary/burial-rights/{other['id']}").json()
    assert detail["renewals"] == [] and detail["version"] == 1


def test_closed_or_suspended_right_cannot_renew_and_leaves_no_history(client):
    from app.database import get_connection
    for plot, status in (("A-08-001", "closed"), ("A-08-002", "suspended")):
        right = _make_right(client, plot, "2026-06-01")
        get_connection().execute("UPDATE burial_rights SET status=? WHERE id=?", (status, right["id"]))
        response = _renew(client, right["id"], f"BLOCKED-PAY-{plot}")
        assert response.status_code == 409
        assert response.json()["error"]["context"]["status"] == status
        detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
        assert detail["status"] == status
        assert detail["expires_on"] == "2026-06-01"
        assert detail["version"] == 1 and detail["renewals"] == []


def test_expired_right_renewal_reactivates_with_full_history(client):
    right = _make_right(client, "A-09-001", "2024-02-29")
    from app.database import get_connection
    get_connection().execute("UPDATE burial_rights SET status='expired' WHERE id=?", (right["id"],))
    response = _renew(client, right["id"], "EXPIRED-PAY-001", years=4)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active" and body["expires_on"] == "2028-02-29"
    record = body["latest_renewal"]
    assert record["previous_expires_on"] == "2024-02-29"
    assert record["handled_by"] == "cemetery-clerk"
    assert record["payment_reference"] == "EXPIRED-PAY-001"
    assert (record["previous_version"], record["new_version"]) == (1, 2)



def test_invalid_seal_rejects_transfer(client):
    case = create_case(client, "CASE-SEAL-001")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-001"})
    response = client.post(f"/api/mortuary/custody-transfers/{transfer.json()['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert response.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "registered"
