from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from sqlite3 import Connection

from silverwatch import attribution, cases, evidence, registry, review
from silverwatch.errors import missing
from silverwatch.ledger import current_entities, history, revisions


def provider_profile(db: Connection, provider_id: str, *, cut: int | None = None) -> dict:
    provider = next((row for row in registry.catalog(db, cut=cut)
                     if row["provider_id"] == provider_id), None)
    if provider is None:
        raise missing("provider", provider_id)
    all_batches = registry.touch_facts(db, cut=cut)
    activity_rows = []
    seen = set()
    for group in attribution.activity_groups(all_batches).values():
        member_batches = [row for row in group
                          if provider_id in attribution.participant_ids(row)]
        if not member_batches:
            continue
        # 同一活动的重复名单只记录一次参与，人数按活动计。
        batch = min(member_batches, key=lambda row: row["batch_id"])
        seen.add(attribution.activity_key(batch))
        detail = attribution.participant_activity(batch, provider_id)
        activity_rows.append({**detail,
                              "district": batch["district"], "cohort": batch["cohort"],
                              "channel": batch["channel"],
                              "activity_people": batch["exposed_people"],
                              "nonresponse_people": batch["nonresponse_people"],
                              "batch_id": batch["batch_id"],
                              "batch_ids": sorted(row["batch_id"] for row in group)})
    participating = [row for row in all_batches
                     if provider_id in attribution.participant_ids(row)]
    related = review.cases_for_provider(db, provider_id, cut=cut)
    by_finding = defaultdict(int)
    for item in related:
        by_finding[item["finding"]["finding"]] += 1
    attributed = sum(row["attributed_people"] for row in activity_rows)
    return {"provider": provider,
            "activity": {"touch_batches": len(participating),
                         "activity_count": len(activity_rows),
                         "exposed_people": sum(row["activity_people"] for row in activity_rows),
                         "attributed_people": attributed,
                         "shared_people": sum(row["shared_people"] for row in activity_rows),
                         "unallocated_people": sum(row["unallocated_people"] for row in activity_rows),
                         "unanswered_people": sum(row["nonresponse_people"] for row in activity_rows),
                         "joint_activity_count": sum(row["joint"] for row in activity_rows),
                         "participant_activity_count": sum(
                             row["participant_count"] for row in activity_rows),
                         "attribution_basis": _basis(activity_rows),
                         "participation": activity_rows,
                         "case_count": len(related), "findings": dict(sorted(by_finding.items()))},
            "cases": related, "cut": cut}


def _basis(rows: list[dict]) -> str:
    values = {row["attribution_basis"] for row in rows}
    if not values:
        return "no_activity"
    return next(iter(values)) if len(values) == 1 else "mixed"


def district_overview(db: Connection, region: str, *, cut: int | None = None,
                      as_of: datetime | None = None) -> dict:
    providers = registry.catalog(db, region=region, cut=cut)
    batches = registry.touch_facts(db, region=region, cut=cut)
    if as_of is not None:
        batches = [row for row in batches if datetime.fromisoformat(row["observed_at"]) <= as_of]
    demand = registry.needs(db, region=region, cut=cut, as_of=as_of)
    cases_now = cases.current_cases(db, cut=cut, as_of=as_of)
    findings = cases.all_findings(db, cut=cut, as_of=as_of)
    areas = defaultdict(lambda: {"providers": 0, "capacity_per_month": 0,
                                 "exposed_people": 0, "nonresponse_people": 0,
                                 "attributed_people": 0, "shared_people": 0,
                                 "unallocated_people": 0, "activity_count": 0,
                                 "joint_activity_count": 0,
                                 "participant_activity_count": 0,
                                 "reported_issues": 0, "substantiated": 0,
                                 "pending": 0, "inconclusive": 0, "estimated_need": 0,
                                 "attribution_bases": defaultdict(int)})
    for row in providers:
        area = areas[row["district"]]
        area["providers"] += 1
        if row["status"] == "active":
            area["capacity_per_month"] += row["capacity_per_month"]
    duplicates = attribution.duplicate_roster_batches(batches)
    for group in attribution.activity_groups(batches).values():
        primary = min(group, key=lambda row: row["batch_id"])
        area = areas[primary["district"]]
        scope = attribution.activity_scope(primary)
        area["exposed_people"] += primary["exposed_people"]
        area["nonresponse_people"] += primary["nonresponse_people"]
        area["attributed_people"] += scope["attributed_people"]
        area["shared_people"] += scope["shared_people"]
        area["unallocated_people"] += scope["unallocated_people"]
        area["activity_count"] += 1
        area["joint_activity_count"] += int(scope["joint"])
        area["participant_activity_count"] += scope["participant_count"]
        area["attribution_bases"][scope["basis"]] += 1
        area["reported_issues"] += primary["report_count"]
    for row in demand:
        areas[row["district"]]["estimated_need"] += row["estimated_people"]
    for row in cases_now:
        district = next((batch["district"] for batch in batches
                         if batch["batch_id"] == row["payload"]["batch_id"]), None)
        if district is None:
            continue
        finding = findings[row["entity_id"]]["finding"]
        if finding in {"substantiated", "pending", "inconclusive"}:
            areas[district][finding] += 1
    result = []
    for district, value in sorted(areas.items()):
        exposure = value["exposed_people"]
        total = exposure + value["nonresponse_people"]
        bases = value.pop("attribution_bases")
        value["attribution_basis"] = (next(iter(bases)) if len(bases) == 1
                                      else "mixed" if bases else "no_activity")
        value["attribution_basis_counts"] = dict(sorted(bases.items()))
        value["touch_coverage_bp"] = round(exposure * 10_000 / total) if total else None
        value["reported_issues_per_1000"] = round(value["reported_issues"] * 1000 / exposure, 2) if exposure else None
        value["supply_per_need_bp"] = round(value["capacity_per_month"] * 10_000 / value["estimated_need"]) if value["estimated_need"] else None
        result.append({"district": district, **value})
    summary = attribution.totals(batches)
    return {"region": region, "as_of": as_of.isoformat() if as_of else None,
            "cut": cut, "areas": result, "provider_total": len(providers),
            "touch_batch_total": len(batches),
            "activity_total": summary["activity_count"], "case_total": len(cases_now),
            "attribution": {**summary,
                            "unattributed_people": (summary["shared_people"]
                                                    + summary["unallocated_people"]),
                            "duplicate_roster_batches": sorted(duplicates)},
            "note": "活动人数按活动去重；专门归属、共享与未分配人数分别列出，同一批老人不会按参与机构重复计入。"}


def case_timeline(db: Connection, case_id: str, *, cut: int | None = None,
                  as_of: datetime | None = None) -> dict:
    case = next((row for row in cases.current_cases(db, cut=cut, as_of=as_of)
                 if row["entity_id"] == case_id), None)
    if case is None:
        raise missing("case", case_id)
    return {"case": case["payload"], "case_event": case["seq"],
            "evidence": evidence.for_case(db, case_id, cut=cut),
            "decisions": cases.decisions(db, case_id, cut=cut, as_of=as_of),
            "finding": cases.finding(db, case_id, cut=cut, as_of=as_of),
            "remediations": [row for row in cases.current_remediations(db, cut=cut, as_of=as_of)
                             if row["case_id"] == case_id],
            "revisions": revisions(db, "case", case_id, cut=cut)}


def source_history(db: Connection, event_type: str, entity_id: str,
                   source_ref: str | None = None) -> dict:
    rows = history(db, event_type, entity_id, source_ref)
    if not rows:
        raise missing(event_type, entity_id)
    return {"event_type": event_type, "entity_id": entity_id,
            "source_ref": source_ref, "revisions": rows,
            "current_by_source": [row for row in current_entities(db, event_type)
                                  if row["entity_id"] == entity_id]}


def validate_references(db: Connection, *, cut: int | None = None) -> dict:
    """检查分析所用的事实关联，并保留原始来源数据不作修复。"""
    problems = []
    providers = {row["entity_id"] for row in current_entities(db, "provider", cut=cut)}
    touches = {row["entity_id"]: row["payload"] for row in current_entities(db, "touch", cut=cut)}
    needs = current_entities(db, "need", cut=cut)
    for batch_id, row in touches.items():
        if row["provider_id"] not in providers:
            problems.append({"entity": batch_id, "issue": "provider_missing"})
    for row in current_entities(db, "case", cut=cut):
        item = row["payload"]
        batch = touches.get(item["batch_id"])
        if batch is None:
            problems.append({"entity": row["entity_id"], "issue": "touch_missing"})
            continue
        participants = set(attribution.participant_ids(batch))
        respondents = set(attribution.case_respondents(item, batch))
        if item["provider_id"] not in participants:
            problems.append({"entity": row["entity_id"], "issue": "provider_scope_mismatch"})
        elif not respondents <= participants:
            problems.append({"entity": row["entity_id"], "issue": "respondent_scope_mismatch"})
        elif item.get("scope") == "joint_campaign" and not attribution.is_joint(batch):
            problems.append({"entity": row["entity_id"], "issue": "joint_scope_without_joint_activity"})
    for row in needs:
        item = row["payload"]
        if item["period_end"] <= item["period_start"]:
            problems.append({"entity": row["entity_id"], "issue": "invalid_need_period"})
    return {"valid": not problems, "checked": {"providers": len(providers),
            "touches": len(touches), "needs": len(needs)}, "issues": problems,
            "cut": cut}
