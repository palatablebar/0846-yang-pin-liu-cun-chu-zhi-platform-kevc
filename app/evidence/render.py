from app.errors import DomainIssue
from app.evidence.manifest import stable_digest

# 停用后审计仍能下载原签发证据：DEACTIVATED 与 APPROVED 同样可下载。
EVIDENCE_STATUSES = {"APPROVED", "DEACTIVATED"}


def render(run):
    if run is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if run["status"] not in EVIDENCE_STATUSES:
        raise DomainIssue("NOT_APPROVED", 409)

    # 停用不重算、不改写原签发：摘要块使用停用前冻结的签发快照，
    # 因此停用前后证据 artifact_sha256 保持一致，审计可还原原签发。
    frozen = run.get("frozen_review")
    review_block = frozen if (run["status"] == "DEACTIVATED" and frozen) else {
        "status": run["status"], "revision": run["revision"], "comment": run["comment"],
    }
    artifact = {
        "schema": "workbench-evidence/v1",
        "run_id": run["run_id"],
        "project": run["project"],
        "rule": run["rule"],
        "profile": run["profile"],
        "source": run["source"],
        "counts": run["counts"],
        "ledger": run["ledger"],
        "summary": run["summary"],
        "review": review_block,
    }
    artifact["artifact_sha256"] = stable_digest(artifact)
    if run["status"] == "DEACTIVATED":
        # 停用标记在摘要之外单独呈现，不参与原证据摘要。
        artifact["deactivated"] = True
        artifact["current_status"] = run["status"]
    return artifact
