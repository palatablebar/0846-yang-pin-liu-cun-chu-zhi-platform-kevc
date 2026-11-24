"""迟到保全补录与签发停用。

规则：
- 库房按已签发的留存结论作业；补录的保全若在原判定日（DECISION_AS_OF）已生效，
  且把“待处置”样品改判为留存，受影响的签发立即停用。
- 保全未改变判定的签发照常使用。
- 停用不改动原批次摘要，审计仍可下载原签发证据。
- 补录对所有新批次生效，工作台不能放出漏算这条保全的新结论。
- 同一（样品、生效日）更正重试不重复登记，也不重复登记停用。
"""
import json
from datetime import date, datetime

from app.catalog import SPEC
from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.errors import DomainIssue
from app.intake.service import load
from app.service import pipeline

# 与计算层固定的判定日保持一致。
DECISION_AS_OF = date(2026, 11, 24)


def _parse_day(value, code="BAD_DATE"):
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DomainIssue(code, detail="生效日需为 ISO 日期（YYYY-MM-DD）")


def active_hold_map(store, as_of=DECISION_AS_OF):
    """判定日（含）前已生效的补录保全，按样品主键映射。"""
    return {row["sample_id"]: row for row in store.holds_effective_on(as_of)}


def recompute_verdict(profile, sample_id, hold_overrides):
    """带补录保全重算单个样品的判定，返回 (样品是否存在, 重算结论)。"""
    intake = load(profile)
    if not any(row.get(SPEC["key"]) == sample_id for row in intake["rows"]):
        return False, None
    prepared, _ = prepare_rows(intake["rows"], hold_overrides=hold_overrides)
    results = decide_rows(calculate_rows(prepared))
    verdict = next((item for item in results if item.get(SPEC["key"]) == sample_id), None)
    return verdict is not None, verdict


def backfill_hold(store, profile, sample_id, effective_on, note="", as_of=DECISION_AS_OF):
    """补录一条迟到保全并停用受影响的签发。

    返回字典描述补录登记、受影响批次与停用动作，保证重试幂等。
    """
    effective_on = _parse_day(effective_on)
    if effective_on > as_of:
        raise DomainIssue("HOLD_NOT_EFFECTIVE", 422,
                          detail="保全生效日晚于原判定日，不能改判既有结论")
    sample_id = str(sample_id or "").strip()
    if not sample_id:
        raise DomainIssue("BAD_REQUEST", detail="缺少样品主键")

    # 先确认样品在该批次来源中存在，并拿到补录后的判定；不通过则不登记任何记录。
    exists, new_verdict = recompute_verdict(profile, sample_id, {sample_id: True})
    if not exists:
        raise DomainIssue("SAMPLE_UNKNOWN", 404, detail="该批次来源中没有此样品")

    hold, created = store.add_hold(sample_id, effective_on.isoformat(), (note or "").strip())

    affected_runs = []
    for run_row in store.list_reviewable_runs():
        if run_row["profile"] != profile:
            continue
        payload = json.loads(run_row["payload"])
        old = next((row for row in payload.get("rows", []) if row.get(SPEC["key"]) == sample_id), None)
        if old is None or old.get("verdict") == new_verdict.get("verdict"):
            # 保全未改变判定的签发照常使用。
            continue
        already = store.deactivation_exists(run_row["id"], hold["id"])
        # 停用瞬间冻结签发状态，使审计证据与原签发完全一致（含摘要与摘要值）。
        frozen_review = {"status": run_row["status"], "revision": run_row["revision"],
                         "comment": run_row["comment"]}
        changed = store.deactivate_run(run_row["id"], hold["id"],
                                       f"补录保全 {sample_id} 生效日 {effective_on.isoformat()} 改判 {old.get('verdict')} → {new_verdict.get('verdict')}",
                                       frozen_review=frozen_review)
        affected_runs.append({
            "run_id": run_row["id"],
            "previous_verdict": old.get("verdict"),
            "current_verdict": new_verdict.get("verdict"),
            "deactivated": changed,
            "duplicate": already and not changed,
        })

    return {
        "hold": {
            "id": hold["id"],
            "sample_id": sample_id,
            "profile": profile,
            "effective_on": effective_on.isoformat(),
            "decision_as_of": as_of.isoformat(),
            "note": hold["note"],
            "created": created,
        },
        "verdict": {"verdict": new_verdict["verdict"], "reason": new_verdict.get("reason"),
                    "state": new_verdict.get("state")},
        "affected_runs": affected_runs,
        "deactivated_count": sum(1 for item in affected_runs if item["deactivated"]),
    }


def pipeline_with_holds(store, profile, as_of=DECISION_AS_OF):
    """执行批次时自动并入所有已生效的补录保全。"""
    holds = active_hold_map(store, as_of)
    overrides = {key: True for key in holds}
    payload = pipeline(profile, hold_overrides=overrides)
    # 已生效补录保全集合纳入批次身份：保全变化产生新批次，
    # 同一保全集合的重复提交仍然幂等，且不会放出漏算保全的旧结论。
    hold_token = "|".join(sorted(f"{key}@{holds[key]['effective_on']}" for key in holds))
    payload["run_key"] = payload["run_key"] + ":" + __import__("hashlib").sha256(
        hold_token.encode("utf-8")).hexdigest()[:12]
    payload["active_holds"] = [
        {"sample_id": key, "effective_on": holds[key]["effective_on"]} for key in sorted(holds)
    ]
    return payload


def serialize_hold(row, as_of=DECISION_AS_OF):
    if row is None:
        return None
    return {
        "id": row["id"],
        "sample_id": row["sample_id"],
        "effective_on": row["effective_on"],
        "decision_as_of": as_of.isoformat() if isinstance(as_of, (date, datetime)) else str(as_of),
        "note": row["note"],
        "created_at": row["created_at"],
        "active": _parse_day(row["effective_on"]) <= as_of,
    }
