from app.catalog import SPEC
from app.domain.helpers import D, DAY, I, M, TS, optional
from app.intake.normalize import canonical


PARSERS = {
    "money": lambda value, code: M(value, code),
    "decimal": lambda value, code: D(value, code),
    "decimal_optional": lambda value, code: optional(D, value, code),
    "int": lambda value, code: I(value, code),
    "int_positive": lambda value, code: positive(I(value, code)),
    "date": lambda value, code: DAY(value, code),
    "date_optional": lambda value, code: optional(DAY, value, code),
    "datetime": lambda value, code: TS(value, code),
}


def positive(value):
    if value <= 0:
        raise ValueError("BAD_POSITIVE")
    return value


def _typed(rows):
    typed, issues = [], []
    for raw in rows:
        item = dict(raw)
        try:
            if not canonical(item.get(SPEC["key"], "")):
                raise ValueError("KEY_MISSING")
            for field, kind in SPEC["types"].items():
                code = "INVALID_" + field.upper()
                item[field] = PARSERS[kind](item.get(field), code)
            typed.append(item)
        except ValueError as exc:
            issues.append({"line": raw.get("_line", 0), "key": raw.get(SPEC["key"], ""), "code": str(exc)})
    return typed, issues


def prepare_rows(rows, hold_overrides=None):
    typed, issues = _typed(rows)
    rows = typed
    hold_overrides = hold_overrides or {}
    items = []
    for item in typed:
        try:
            if not canonical(item.get(SPEC["key"], "")):
                raise ValueError("KEY_MISSING")
            if item["retention_months"] < 0:
                raise ValueError("BAD_RETENTION")
            hold = item["legal_hold"].upper()
            if hold not in {"Y", "N", "YES", "NO", "1", "0"}:
                raise ValueError("BAD_HOLD")
            item["legal_hold"] = hold in {"Y", "YES", "1"}
            # 迟到补录的保全在原判定日已生效，覆盖原始保全标记并优先阻止处置。
            if hold_overrides.get(item[SPEC["key"]]):
                item["legal_hold"] = True
                item["hold_backfilled"] = True
            if item["disposed_at"] and item["disposed_at"] < item["collected_at"]:
                raise ValueError("DISPOSED_BEFORE_COLLECTED")
            items.append(item)
        except ValueError as exc:
            issues.append({"line": item.get("_line", 0), "key": item.get(SPEC["key"], ""), "code": str(exc)})
    return items, issues
