"""迟到保全补录、签发停用与证据留存。"""
from app.catalog import SPEC
from app.review.backfill import DECISION_AS_OF


def create_run(client, profile="clean"):
    response = client.post("/api/runs", json={"profile": profile})
    assert response.status_code == 200
    return response.get_json()


def approve(client, run):
    response = client.post(
        f"/api/runs/{run['run_id']}/review",
        json={"decision": "APPROVED", "comment": "签发", "expected_revision": run["revision"]},
    )
    assert response.status_code == 200
    return response.get_json()


def backfill(client, sample_id="S2", effective_on="2026-11-01", profile="clean", note="补录"):
    return client.post("/api/holds", json={
        "profile": profile, "sample_id": sample_id,
        "effective_on": effective_on, "note": note,
    })


def verdict_of(run, sample_id):
    row = next(item for item in run["rows"] if item.get(SPEC["key"]) == sample_id)
    return row["verdict"], row.get("reason")


# ---- 补录改判与停用 -------------------------------------------------

def test_backfill_deactivates_affected_approved_run(client):
    run = approve(client, create_run(client))
    before, _ = verdict_of(run, "S2")
    assert before == "REVIEW"  # S2 待处置（DUE_OVERDUE → REVIEW）

    response = backfill(client)
    assert response.status_code == 200
    result = response.get_json()
    assert result["verdict"]["verdict"] == "PASS"
    assert result["deactivated_count"] == 1
    assert result["affected_runs"][0]["run_id"] == run["run_id"]
    assert result["affected_runs"][0]["previous_verdict"] == "REVIEW"

    updated = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert updated["status"] == "DEACTIVATED"


def test_unaffected_approval_keeps_working(client):
    run = approve(client, create_run(client))
    # S3 原本就带保全（RETAIN/PASS），补录不改变其判定，签发照常使用。
    response = backfill(client, sample_id="S3", effective_on="2026-11-01")
    assert response.status_code == 200
    assert response.get_json()["affected_runs"] == []
    still = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert still["status"] == "APPROVED"


def test_backfill_future_effective_date_is_rejected(client):
    approve(client, create_run(client))
    response = backfill(client, effective_on="2026-11-30")
    assert response.status_code == 422
    assert response.get_json()["code"] == "HOLD_NOT_EFFECTIVE"


def test_backfill_unknown_sample_is_rejected(client):
    approve(client, create_run(client))
    response = backfill(client, sample_id="NOPE")
    assert response.status_code == 404
    assert response.get_json()["code"] == "SAMPLE_UNKNOWN"
    # 校验不通过不登记任何保全或停用记录。
    assert client.get("/api/holds").get_json()["total"] == 0
    assert client.get("/api/deactivations").get_json()["total"] == 0


def test_backfill_requires_fields(client):
    response = client.post("/api/holds", json={"sample_id": "S2"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "BAD_REQUEST"


# ---- 幂等重试 -------------------------------------------------------

def test_repeated_backfill_does_not_duplicate_deactivation(client):
    run = approve(client, create_run(client))
    first = backfill(client).get_json()
    assert first["deactivated_count"] == 1

    second = backfill(client)
    assert second.status_code == 200
    body = second.get_json()
    assert body["hold"]["created"] is False
    assert body["deactivated_count"] == 0
    assert body["affected_runs"][0]["duplicate"] is True

    listing = client.get("/api/deactivations").get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["run_id"] == run["run_id"]
    assert client.get("/api/holds").get_json()["total"] == 1


def test_draft_run_is_not_deactivated_then_new_run_includes_hold(client):
    draft = create_run(client)  # 未签发
    backfill(client)
    # 草稿不是签发，不登记停用。
    assert client.get("/api/deactivations").get_json()["total"] == 0
    # 补录与新批次同时提交时，新结论必须计入保全：S2 改判 PASS。
    rerun = create_run(client)
    verdict, reason = verdict_of(rerun, "S2")
    assert verdict == "PASS"
    assert reason == "RETAIN"
    # 草稿本身仍是 DRAFT。
    assert client.get(f"/api/runs/{draft['run_id']}").get_json()["status"] == "DRAFT"


# ---- 证据留存 -------------------------------------------------------

def test_evidence_still_downloadable_after_deactivation(client):
    run = approve(client, create_run(client))
    before = client.get(f"/api/runs/{run['run_id']}/evidence").get_json()
    backfill(client)

    response = client.get(f"/api/runs/{run['run_id']}/evidence")
    assert response.status_code == 200
    after = response.get_json()
    # 摘要保持原样，证据摘要不变。
    assert after["summary"] == before["summary"]
    assert after["source"] == before["source"]
    assert after["counts"] == before["counts"]
    assert after["run_id"] == run["run_id"]
    # 停用前后证据摘要值完全一致，审计可还原原签发。
    assert after["artifact_sha256"] == before["artifact_sha256"]
    # 摘要块冻结为停用前的签发状态，停用标记在摘要之外。
    assert after["review"] == {"status": "APPROVED", "revision": 1, "comment": "签发"}
    assert after["deactivated"] is True
    assert after["current_status"] == "DEACTIVATED"


def test_deactivated_run_cannot_be_reapproved(client):
    run = approve(client, create_run(client))
    backfill(client)
    response = client.post(
        f"/api/runs/{run['run_id']}/review",
        json={"decision": "APPROVED", "comment": "再签", "expected_revision": 1},
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "RUN_DEACTIVATED"


def test_held_run_is_not_deactivated(client):
    run = create_run(client)
    client.post(f"/api/runs/{run['run_id']}/review",
                json={"decision": "HELD", "comment": "暂缓", "expected_revision": 0})
    response = backfill(client)
    assert response.get_json()["affected_runs"] == []
    assert client.get(f"/api/runs/{run['run_id']}").get_json()["status"] == "HELD"


# ---- 停用登记与清单 -------------------------------------------------

def test_hold_listing_and_deactivation_audit(client):
    run = approve(client, create_run(client))
    backfill(client, note="法院保全")
    holds = client.get("/api/holds").get_json()
    assert holds["total"] == 1
    item = holds["items"][0]
    assert item["sample_id"] == "S2"
    assert item["effective_on"] == "2026-11-01"
    assert item["active"] is True
    assert item["decision_as_of"] == DECISION_AS_OF.isoformat()

    deactivations = client.get("/api/deactivations").get_json()
    assert deactivations["total"] == 1
    record = deactivations["items"][0]
    assert record["run_id"] == run["run_id"]
    assert record["sample_id"] == "S2"
    assert "REVIEW" in record["reason"] and "PASS" in record["reason"]


def test_hold_on_boundary_decision_day_is_effective(client):
    approve(client, create_run(client))
    # 生效日恰为原判定日，应当生效并改判。
    response = backfill(client, effective_on=DECISION_AS_OF.isoformat())
    assert response.status_code == 200
    assert response.get_json()["deactivated_count"] == 1


def test_disposed_sample_hold_is_a_real_change(client):
    run = approve(client, create_run(client))
    # S1 已处置：补录保全产生 HOLD_VIOLATION（PASS → REJECT），属于改判，原签发停用。
    response = backfill(client, sample_id="S1", effective_on="2026-11-01")
    assert response.status_code == 200
    body = response.get_json()
    assert body["verdict"]["verdict"] == "REJECT"
    assert body["verdict"]["reason"] == "HOLD_VIOLATION"
    assert body["deactivated_count"] == 1
    assert client.get(f"/api/runs/{run['run_id']}").get_json()["status"] == "DEACTIVATED"


def test_multiple_approvals_only_affected_are_deactivated(client):
    first = approve(client, create_run(client))
    # 同一 request_key 的重复创建返回同一批次，因此用暂缓批次制造第二个签发前，
    # 这里直接验证：补录后新批次可正常签发（已含保全）。
    backfill(client)
    rerun = create_run(client)
    assert verdict_of(rerun, "S2")[0] == "PASS"
    second = approve(client, rerun)
    assert second["status"] == "APPROVED"
    # 旧签发停用、新签发有效。
    assert client.get(f"/api/runs/{first['run_id']}").get_json()["status"] == "DEACTIVATED"
    assert client.get(f"/api/runs/{second['run_id']}").get_json()["status"] == "APPROVED"


def test_deactivated_run_keeps_original_payload_summary(client):
    run = approve(client, create_run(client))
    original_summary = dict(run["summary"])
    backfill(client)
    stored = client.get(f"/api/runs/{run['run_id']}").get_json()
    # 原批次结论冻结在 payload 中，不被补录保全重算覆盖。
    assert stored["summary"] == original_summary
    assert verdict_of(stored, "S2")[0] == "REVIEW"
    assert stored["frozen_review"] == {"status": "APPROVED", "revision": 1, "comment": "签发"}


def test_stage_preview_does_not_mutate_stored_holds(client):
    approve(client, create_run(client))
    backfill(client)
    # 阶段预览是无状态演算，不读补录保全，也不写入任何停用记录。
    preview = client.post("/api/stages/decision", json={"profile": "clean"}).get_json()
    assert verdict_of(preview, "S2")[0] == "REVIEW"
    assert client.get("/api/deactivations").get_json()["total"] == 1


def test_new_run_payload_carries_active_holds(client):
    create_run(client)
    backfill(client)
    rerun = create_run(client)
    assert rerun["active_holds"] == [{"sample_id": "S2", "effective_on": "2026-11-01"}]
    # 同一保全集合重复提交保持幂等。
    again = create_run(client)
    assert again["run_id"] == rerun["run_id"]
    assert again["active_holds"] == rerun["active_holds"]


def test_backfill_bad_date_is_rejected(client):
    approve(client, create_run(client))
    response = client.post("/api/holds", json={
        "profile": "clean", "sample_id": "S2", "effective_on": "not-a-date",
    })
    assert response.status_code == 400
    assert response.get_json()["code"] == "BAD_DATE"


def test_backfill_unknown_profile_is_rejected(client):
    response = client.post("/api/holds", json={
        "profile": "mystery", "sample_id": "S2", "effective_on": "2026-11-01",
    })
    assert response.status_code == 404
    assert response.get_json()["code"] == "SAMPLE_UNKNOWN"
