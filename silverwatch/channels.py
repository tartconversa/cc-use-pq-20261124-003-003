from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import attribution, cases, reconciliation
from silverwatch.analysis import outreach_bounds
from silverwatch.contracts import ServiceKind
from silverwatch.errors import invalid
from silverwatch.registry import touch_facts


def compare_channels(db: Connection, *, region: str, as_of: datetime, cut: int,
                     service_kind: ServiceKind | None = None,
                     cohort: str | None = None, minimum_exposure: int = 100) -> dict:
    if as_of.tzinfo is None:
        raise invalid("channel_time", "渠道比较截点必须包含时区")
    if minimum_exposure < 1:
        raise invalid("channel_threshold", "渠道比较阈值必须为正")
    as_of = as_of.astimezone(timezone.utc)
    batches = [row for row in touch_facts(db, region=region, cut=cut)
               if datetime.fromisoformat(row["observed_at"]) <= as_of
               and attribution.offers_service(row, service_kind)
               and (cohort is None or row["cohort"] == cohort)]
    current_cases = cases.current_cases(db, cut=cut, as_of=as_of)
    findings = cases.all_findings(db, cut=cut, as_of=as_of)
    by_batch = defaultdict(list)
    for item in current_cases:
        by_batch[item["payload"]["batch_id"]].append(item["payload"])
    reconciliation_state = reconciliation.reconcile(db, cut=cut, as_of=as_of)
    incomplete = {item["batch_id"] for item in reconciliation_state["batches"] if not item["complete"]}
    groups = defaultdict(lambda: {"exposed": 0, "nonresponse": 0, "reports": 0,
                                  "completed_services": 0, "substantiated": 0,
                                  "pending": 0, "inconclusive": 0, "batches": [],
                                  "coverage_unknown_batches": 0,
                                  "case_intake_incomplete_batches": 0,
                                  "attributed_people": 0, "shared_people": 0,
                                  "unallocated_people": 0, "joint_activity_count": 0,
                                  "participant_activity_count": 0,
                                  "attribution_bases": set(), "case_ids": set()})
    for activity in attribution.activity_groups(batches).values():
        primary = min(activity, key=lambda row: row["batch_id"])
        key = (primary["channel"], service_kind or primary["service_kind"], primary["cohort"])
        group = groups[key]
        scope = attribution.activity_scope(primary)
        group["exposed"] += primary["exposed_people"]
        group["nonresponse"] += primary["nonresponse_people"]
        group["reports"] += primary["report_count"]
        group["completed_services"] += primary["completed_services"]
        group["attributed_people"] += scope["attributed_people"]
        group["shared_people"] += scope["shared_people"]
        group["unallocated_people"] += scope["unallocated_people"]
        group["joint_activity_count"] += int(scope["joint"])
        group["participant_activity_count"] += scope["participant_count"]
        group["attribution_bases"].add(scope["basis"])
        for batch in sorted(activity, key=lambda row: row["batch_id"]):
            group["batches"].append(batch["batch_id"])
            group["coverage_unknown_batches"] += int(not batch["coverage_complete"])
            group["case_intake_incomplete_batches"] += int(batch["batch_id"] in incomplete)
            for case in by_batch.get(batch["batch_id"], []):
                case_id = case["case_id"]
                if case_id in group["case_ids"]:
                    continue
                if not attribution.case_in_service(case, batch, service_kind):
                    continue
                group["case_ids"].add(case_id)
                status = findings[case_id]["finding"]
                if status in {"substantiated", "pending", "inconclusive"}:
                    group[status] += 1
    rows = []
    for (channel, kind, age), value in sorted(groups.items()):
        value.pop("case_ids")
        bases = value.pop("attribution_bases")
        value["attribution_basis"] = next(iter(bases)) if len(bases) == 1 else "mixed"
        value["unattributed_people"] = value["shared_people"] + value["unallocated_people"]
        bounds = outreach_bounds(value["exposed"], value["nonresponse"],
                                 value["substantiated"],
                                 value["coverage_unknown_batches"] == 0)
        issues = []
        if value["exposed"] < minimum_exposure:
            issues.append("minimum_exposure")
        if value["coverage_unknown_batches"]:
            issues.append("coverage_unknown")
        if value["case_intake_incomplete_batches"]:
            issues.append("case_intake_incomplete")
        if value["unallocated_people"]:
            issues.append("attribution_incomplete")
        rows.append({"channel": channel, "service_kind": kind, "cohort": age,
                     **value, "substantiated_rate": bounds["observed_rate"],
                     "potential_rate_bounds": bounds["possible_rate_bounds"],
                     "touch_coverage_bp": bounds["coverage_bp"],
                     "eligible": not issues, "gaps": issues})
    outreach = attribution.totals(batches)
    return {"region": region, "service_kind": service_kind, "cohort": cohort,
            "as_of": as_of.isoformat(), "cut": cut, "minimum_exposure": minimum_exposure,
            "channels": rows,
            "attribution": {**outreach,
                            "unattributed_people": outreach["shared_people"]
                            + outreach["unallocated_people"]},
            "note": "A channel with incomplete source coverage or an unobserved denominator is not comparable to a fully observed channel."}


def rate_differences(report: dict) -> list[dict]:
    rows = [row for row in report["channels"] if row["eligible"] and row["substantiated_rate"] is not None]
    differences = []
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            left_rate = float(left["substantiated_rate"].removesuffix("bp"))
            right_rate = float(right["substantiated_rate"].removesuffix("bp"))
            differences.append({"left_channel": left["channel"], "right_channel": right["channel"],
                                "left_rate_bp": left_rate, "right_rate_bp": right_rate,
                                "difference_bp": round(left_rate - right_rate, 2),
                                "left_exposed": left["exposed"], "right_exposed": right["exposed"]})
    return differences
