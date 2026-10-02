from __future__ import annotations


def create_right(client, plot_code: str, starts_on: str, expires_on: str) -> dict:
    response = client.post("/api/mortuary/burial-rights?actor=cemetery-clerk", json={"plot_code": plot_code, "holder_name": "李冬梅", "holder_identity": "ID-3301-001", "starts_on": starts_on, "expires_on": expires_on, "case_id": None})
    assert response.status_code == 201, response.text
    return response.json()


def renew(client, right_id: int, *, years: int, payment_reference: str, handled_by: str = "cemetery-clerk"):
    return client.post(f"/api/mortuary/burial-rights/{right_id}/renew", json={"years": years, "handled_by": handled_by, "payment_reference": payment_reference})


def change_status(client, right_id: int, status: str, reason: str = "柜台办理", handled_by: str = "cemetery-clerk"):
    return client.post(f"/api/mortuary/burial-rights/{right_id}/status", json={"status": status, "reason": reason, "handled_by": handled_by})


def test_leap_day_renewal_into_common_year_clamps_to_month_end(client):
    right = create_right(client, "L-02-029", "2004-02-29", "2024-02-29")
    response = renew(client, right["id"], years=1, payment_reference="PAY-LEAP-001")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_on"] == "2025-02-28"
    assert body["status"] == "active"
    assert body["version"] == right["version"] + 1
    assert body["replayed"] is False
    last = body["last_renewal"]
    assert last["previous_expires_on"] == "2024-02-29"
    assert last["new_expires_on"] == "2025-02-28"
    assert last["date_adjustment"] == "leap_day_clamped_to_month_end"
    assert last["payment_reference"] == "PAY-LEAP-001"
    assert last["handled_by"] == "cemetery-clerk"
    assert (last["version_before"], last["version_after"]) == (right["version"], right["version"] + 1)


def test_leap_day_multi_year_renewal_lands_back_on_leap_day(client):
    right = create_right(client, "L-02-029-4Y", "2004-02-29", "2024-02-29")
    response = renew(client, right["id"], years=4, payment_reference="PAY-LEAP-004")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_on"] == "2028-02-29"
    assert body["last_renewal"]["date_adjustment"] == ""


def test_leap_day_multi_year_into_common_year_clamps_once(client):
    right = create_right(client, "L-02-029-5Y", "2004-02-29", "2024-02-29")
    response = renew(client, right["id"], years=5, payment_reference="PAY-LEAP-005")
    assert response.status_code == 200, response.text
    assert response.json()["expires_on"] == "2029-02-28"
    assert response.json()["last_renewal"]["date_adjustment"] == "leap_day_clamped_to_month_end"


def test_month_end_and_ordinary_dates_shift_exactly(client):
    end_of_month = create_right(client, "M-01-31", "2005-01-31", "2025-01-31")
    response = renew(client, end_of_month["id"], years=1, payment_reference="PAY-M-001")
    assert response.status_code == 200 and response.json()["expires_on"] == "2026-01-31"
    ordinary = create_right(client, "M-06-30", "2005-06-30", "2025-06-30")
    response = renew(client, ordinary["id"], years=3, payment_reference="PAY-M-002")
    assert response.status_code == 200 and response.json()["expires_on"] == "2028-06-30"


def test_same_payment_reference_replays_single_renewal(client):
    right = create_right(client, "R-REPLAY", "2005-03-15", "2025-03-15")
    first = renew(client, right["id"], years=5, payment_reference="PAY-REPLAY-001")
    assert first.status_code == 200
    second = renew(client, right["id"], years=5, payment_reference="PAY-REPLAY-001")
    assert second.status_code == 200
    replayed = second.json()
    assert replayed["replayed"] is True
    assert replayed["expires_on"] == first.json()["expires_on"] == "2030-03-15"
    assert replayed["version"] == first.json()["version"]
    assert replayed["renewals"] == first.json()["renewals"]
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert len(detail["renewals"]) == 1
    assert [event["event_type"] for event in detail["timeline"]].count("right.renewed") == 1


def test_same_payment_reference_with_different_content_conflicts(client):
    right = create_right(client, "R-CONFLICT", "2005-03-15", "2025-03-15")
    assert renew(client, right["id"], years=5, payment_reference="PAY-CONFLICT-001").status_code == 200
    before = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    other_years = renew(client, right["id"], years=10, payment_reference="PAY-CONFLICT-001")
    assert other_years.status_code == 409
    assert other_years.json()["error"]["context"]["recorded_renewal"]["years"] == 5
    other_handler = renew(client, right["id"], years=5, payment_reference="PAY-CONFLICT-001", handled_by="other-clerk")
    assert other_handler.status_code == 409
    other_right = create_right(client, "R-CONFLICT-2", "2005-03-15", "2025-03-15")
    cross_right = renew(client, other_right["id"], years=5, payment_reference="PAY-CONFLICT-001")
    assert cross_right.status_code == 409
    after = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert after["expires_on"] == before["expires_on"]
    assert after["version"] == before["version"]
    assert len(after["renewals"]) == 1
    assert client.get(f"/api/mortuary/burial-rights/{other_right['id']}").json()["renewals"] == []


def test_suspended_and_closed_rights_reject_renewal(client):
    right = create_right(client, "R-STATUS", "2005-03-15", "2025-03-15")
    assert change_status(client, right["id"], "suspended", reason="继承纠纷暂缓办理").status_code == 200
    suspended = renew(client, right["id"], years=5, payment_reference="PAY-STATUS-001")
    assert suspended.status_code == 409
    assert suspended.json()["error"]["context"]["status"] == "suspended"
    assert change_status(client, right["id"], "active", reason="纠纷调解完成").status_code == 200
    assert change_status(client, right["id"], "closed", reason="墓位已收回").status_code == 200
    closed = renew(client, right["id"], years=5, payment_reference="PAY-STATUS-002")
    assert closed.status_code == 409
    assert closed.json()["error"]["context"]["status"] == "closed"
    assert change_status(client, right["id"], "active", reason="尝试恢复").status_code == 409


def test_failed_renewal_leaves_no_partial_state(client):
    right = create_right(client, "R-ATOMIC", "2005-03-15", "2025-03-15")
    assert change_status(client, right["id"], "suspended", reason="费用争议").status_code == 200
    before = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    failed = renew(client, right["id"], years=5, payment_reference="PAY-ATOMIC-001")
    assert failed.status_code == 409
    after = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert after["renewals"] == []
    assert after["version"] == before["version"]
    assert after["expires_on"] == before["expires_on"]
    assert "right.renewed" not in [event["event_type"] for event in after["timeline"]]
    assert change_status(client, right["id"], "active", reason="争议已处理").status_code == 200
    retried = renew(client, right["id"], years=5, payment_reference="PAY-ATOMIC-001")
    assert retried.status_code == 200 and retried.json()["expires_on"] == "2030-03-15"


def test_invalid_renewal_request_leaves_no_trace(client):
    right = create_right(client, "R-INVALID", "2005-03-15", "2025-03-15")
    assert renew(client, right["id"], years=0, payment_reference="PAY-INVALID-001").status_code == 422
    assert renew(client, right["id"], years=21, payment_reference="PAY-INVALID-002").status_code == 422
    assert renew(client, 999999, years=1, payment_reference="PAY-INVALID-003").status_code == 404
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert detail["renewals"] == []
    assert detail["version"] == right["version"]


def test_expired_right_can_renew_and_reactivates(client):
    right = create_right(client, "R-EXPIRED", "2005-03-15", "2025-03-15")
    from app.database import get_connection
    get_connection().execute("UPDATE burial_rights SET status='expired' WHERE id=?", (right["id"],))
    response = renew(client, right["id"], years=10, payment_reference="PAY-EXPIRED-001")
    assert response.status_code == 200, response.text
    assert response.json()["expires_on"] == "2035-03-15"
    assert response.json()["status"] == "active"


def test_renewal_history_preserves_versions_and_payment_basis(client):
    right = create_right(client, "R-HISTORY", "2004-02-29", "2024-02-29")
    first = renew(client, right["id"], years=1, payment_reference="PAY-HIS-001", handled_by="clerk-a")
    assert first.status_code == 200
    second = renew(client, right["id"], years=3, payment_reference="PAY-HIS-002", handled_by="clerk-b")
    assert second.status_code == 200
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert detail["expires_on"] == "2028-02-28"
    assert detail["version"] == right["version"] + 2
    renewals = detail["renewals"]
    assert [(item["previous_expires_on"], item["new_expires_on"]) for item in renewals] == [("2024-02-29", "2025-02-28"), ("2025-02-28", "2028-02-28")]
    assert [(item["version_before"], item["version_after"]) for item in renewals] == [(1, 2), (2, 3)]
    assert [item["payment_reference"] for item in renewals] == ["PAY-HIS-001", "PAY-HIS-002"]
    assert [item["handled_by"] for item in renewals] == ["clerk-a", "clerk-b"]
    renewed_events = [event for event in detail["timeline"] if event["event_type"] == "right.renewed"]
    assert [event["payload"]["version_after"] for event in renewed_events] == [2, 3]
    assert renewed_events[0]["payload"]["date_adjustment"] == "leap_day_clamped_to_month_end"


def test_status_transitions_are_audited(client):
    right = create_right(client, "R-AUDIT", "2005-03-15", "2025-03-15")
    assert change_status(client, right["id"], "closed", reason="直接关闭").status_code == 200
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    event = [item for item in detail["timeline"] if item["event_type"] == "right.status_changed"][-1]
    assert event["actor"] == "cemetery-clerk"
    assert event["payload"]["previous_status"] == "active"
    assert event["payload"]["new_status"] == "closed"
    assert event["payload"]["reason"] == "直接关闭"
    assert (event["payload"]["version_before"], event["payload"]["version_after"]) == (1, 2)
    assert detail["version"] == 2


def test_status_change_rejects_invalid_target(client):
    right = create_right(client, "R-BADTARGET", "2005-03-15", "2025-03-15")
    response = client.post(f"/api/mortuary/burial-rights/{right['id']}/status", json={"status": "expired", "reason": "手工标记", "handled_by": "cemetery-clerk"})
    assert response.status_code == 422
    assert change_status(client, 999999, "closed").status_code == 404


def test_renewal_beyond_representable_year_is_explainable(client):
    right = create_right(client, "R-OVERFLOW", "8000-01-01", "9990-01-01")
    response = renew(client, right["id"], years=20, payment_reference="PAY-OVERFLOW-001")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    detail = client.get(f"/api/mortuary/burial-rights/{right['id']}").json()
    assert detail["renewals"] == []
    assert detail["expires_on"] == "9990-01-01"


def test_get_unknown_right_returns_404(client):
    assert client.get("/api/mortuary/burial-rights/999999").status_code == 404
