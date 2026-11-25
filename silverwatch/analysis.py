from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from sqlite3 import Connection

from silverwatch import attribution, cases
from silverwatch.contracts import AnalysisPolicy, ServiceKind
from silverwatch.digest import digest
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut
from silverwatch.registry import catalog, needs, touch_facts
from silverwatch.reconciliation import reconcile


def format_rate(value: Fraction | None) -> str | None:
    if value is None:
        return None
    scaled = round(value * 10_000)
    return f"{scaled:.2f}bp"


def outreach_bounds(exposed: int, nonresponse: int, substantiated: int,
                    coverage_complete: bool) -> dict:
    if min(exposed, nonresponse, substantiated) < 0:
        raise invalid("outreach_counts", "触达人数和核实线索数不能为负")
    if substantiated > exposed + nonresponse:
        raise invalid("outreach_counts", "核实线索数不能超过已触达与未回应人数")
    known_total = exposed + nonresponse
    observed_rate = Fraction(substantiated, exposed) if exposed else None
    lower_rate = Fraction(substantiated, known_total) if known_total else None
    upper_rate = Fraction(substantiated + nonresponse, known_total) if known_total else None
    coverage = Fraction(exposed, known_total) if known_total else None
    return {"coverage_bp": round(float(coverage * 10_000)) if coverage is not None else None,
            "observed_rate": format_rate(observed_rate),
            "possible_rate_bounds": {"low": format_rate(lower_rate), "high": format_rate(upper_rate)},
            "coverage_complete": coverage_complete and nonresponse == 0}


def aggregate_touch(batches: list[dict]) -> dict:
    by_cohort = defaultdict(lambda: {"exposed": 0, "nonresponse": 0, "services": 0,
                                     "reports": 0, "batches": [], "incomplete": 0})
    for row in batches:
        group = by_cohort[row["cohort"]]
        group["exposed"] += row["exposed_people"]
        group["nonresponse"] += row["nonresponse_people"]
        group["services"] += row["completed_services"]
        group["reports"] += row["report_count"]
        group["incomplete"] += int(not row["coverage_complete"])
        group["batches"].append(row["batch_id"])
    result = {}
    for cohort, value in by_cohort.items():
        denominator = value["exposed"]
        result[cohort] = {
            **value, "report_rate": format_rate(Fraction(value["reports"], denominator) if denominator else None),
            "service_rate": format_rate(Fraction(value["services"], denominator) if denominator else None),
            "coverage_complete": value["incomplete"] == 0,
        }
    return dict(sorted(result.items()))


def supply_coverage(providers: list[dict], need: list[dict], region: str,
                    as_of: datetime | None = None) -> dict:
    denominator = Counter()
    available = Counter()
    examples = defaultdict(list)
    for item in need:
        if item["region"] != region:
            continue
        if as_of is not None and item["period_end"] > as_of.isoformat():
            continue
        key = item["service_kind"]
        denominator[key] += item["estimated_people"]
    for provider in providers:
        if provider["region"] != region or provider["status"] != "active":
            continue
        if as_of is not None:
            starts = datetime.fromisoformat(provider["active_from"])
            ends = datetime.fromisoformat(provider["active_until"]) if provider["active_until"] else None
            if starts > as_of or ends is not None and ends <= as_of:
                continue
        key = provider["service_kind"]
        available[key] += provider["capacity_per_month"]
        examples[key].append(provider["provider_id"])
    result = {}
    for kind in sorted(set(denominator) | set(available)):
        demand = denominator[kind]
        capacity = available[kind]
        result[kind] = {
            "estimated_people": demand, "monthly_capacity": capacity,
            "capacity_ratio": format_rate(Fraction(capacity, demand) if demand else None),
            "capacity_providers": sorted(examples[kind]),
            "demand_observed": bool(demand),
        }
    return result


def scoped_batches(db: Connection, cut: int, as_of: datetime, *, region: str,
                   service_kind: ServiceKind | None, cohort: str | None) -> list[dict]:
    """按活动涉及的服务筛选；联合活动的共同参与方服务也计入。"""
    all_batches = touch_facts(db, region=region, cut=cut)
    return [row for row in all_batches if row["observed_at"] <= as_of.isoformat()
            and attribution.offers_service(row, service_kind)
            and (cohort is None or row["cohort"] == cohort)]


def risk_rows(db: Connection, cut: int, as_of: datetime, *, region: str,
              service_kind: ServiceKind | None, cohort: str | None) -> list[dict]:
    filtered = scoped_batches(db, cut, as_of, region=region,
                              service_kind=service_kind, cohort=cohort)
    case_by_batch = defaultdict(list)
    from silverwatch.ledger import current_entities

    for event in current_entities(db, "case", cut=cut, as_of=as_of):
        case_by_batch[event["payload"]["batch_id"]].append(event["payload"])
    decisions = cases.all_findings(db, cut=cut, as_of=as_of)
    remediations = cases.current_remediations(db, cut=cut, as_of=as_of)
    remediation_by_case = defaultdict(list)
    for action in remediations:
        remediation_by_case[action["case_id"]].append(action)
    grouped = defaultdict(lambda: {"exposed": 0, "nonresponse": 0, "reports": 0,
                                   "substantiated": 0, "pending": 0, "inconclusive": 0,
                                   "open_severe": 0, "verified_remediations": 0,
                                   "attributed_people": 0, "shared_people": 0,
                                   "unallocated_people": 0, "joint_activity_count": 0,
                                   "participant_activity_count": 0,
                                   "attribution_bases": Counter(),
                                   "activity_count": 0,
                                   "participants": {}, "participant_service_kinds": set(),
                                   "batches": [], "case_ids": set()})
    # 同一活动的多条相同名单只计一次人数，线索与金额仍按各自批次核对。
    for group in attribution.activity_groups(filtered).values():
        primary = min(group, key=lambda row: row["batch_id"])
        effective_kind = service_kind or primary["service_kind"]
        row = grouped[(primary["district"], effective_kind, primary["cohort"])]
        scope = attribution.activity_scope(primary)
        row["exposed"] += primary["exposed_people"]
        row["nonresponse"] += primary["nonresponse_people"]
        row["reports"] += primary["report_count"]
        row["activity_count"] += 1
        row["attributed_people"] += scope["attributed_people"]
        row["shared_people"] += scope["shared_people"]
        row["unallocated_people"] += scope["unallocated_people"]
        row["joint_activity_count"] += int(scope["joint"])
        row["participant_activity_count"] += scope["participant_count"]
        row["attribution_bases"][scope["basis"]] += 1
        for participant in scope["participants"]:
            member_key = (participant["provider_id"], participant.get("service_kind"))
            entry = row["participants"].setdefault(member_key, {
                "provider_id": participant["provider_id"],
                "service_kind": participant.get("service_kind"),
                "role": participant.get("role") or "co_host",
                "service_content": participant.get("service_content"),
                "commitment": participant.get("commitment"),
                "attributed_people": 0})
            entry["attributed_people"] += int(participant.get("attributed_people") or 0)
            row["participant_service_kinds"].add(participant.get("service_kind"))
        for batch in sorted(group, key=lambda row: row["batch_id"]):
            row["batches"].append(batch["batch_id"])
            if not batch["coverage_complete"] and batch["nonresponse_people"] == 0:
                row["coverage_unknown_batches"] = row.get("coverage_unknown_batches", 0) + 1
            for case in case_by_batch.get(batch["batch_id"], []):
                if case["case_id"] in row["case_ids"]:
                    continue
                if not attribution.case_in_service(case, batch, service_kind):
                    continue
                row["case_ids"].add(case["case_id"])
                finding = decisions[case["case_id"]]["finding"]
                if finding == "substantiated":
                    row["substantiated"] += 1
                elif finding == "pending":
                    row["pending"] += 1
                elif finding == "inconclusive":
                    row["inconclusive"] += 1
                if case["severity"] in {"high", "critical"} and case["status"] != "closed":
                    row["open_severe"] += 1
                if any(action["status"] == "verified"
                       for action in remediation_by_case[case["case_id"]]):
                    row["verified_remediations"] += 1
    duplicates = attribution.duplicate_roster_batches(filtered)
    overlaps = attribution.possible_roster_overlaps(filtered)
    result = []
    for (district, kind, age), row in sorted(grouped.items()):
        row.pop("case_ids")
        bases = row.pop("attribution_bases")
        row["possible_roster_overlaps"] = [
            item for item in overlaps
            if set(item["batch_ids"]) & set(row["batches"])]
        participants = sorted(row.pop("participants").values(),
                              key=lambda item: (str(item["service_kind"]), item["provider_id"]))
        service_kinds = sorted(kind for kind in row.pop("participant_service_kinds") if kind)
        exposure = row["exposed"]
        bounds = outreach_bounds(exposure, row["nonresponse"], row["substantiated"],
                                 row.get("coverage_unknown_batches", 0) == 0)
        result.append({
            "district": district, "service_kind": kind, "cohort": age,
            **row,
            "batch_count": len(row["batches"]),
            "attribution_basis": next(iter(bases)) if len(bases) == 1 else "mixed",
            "attribution_basis_counts": dict(sorted(bases.items())),
            "participants": participants,
            "participant_service_kinds": service_kinds,
            "services_offered": sorted({item["service_kind"] for item in participants
                                        if item.get("service_kind")}),
            "service_content": [
                {"role": item.get("role"), "service_kind": item.get("service_kind"),
                 "service_content": item.get("service_content"),
                 "commitment": item.get("commitment")}
                for item in participants],
            "unattributed_people": row["shared_people"] + row["unallocated_people"],
            "duplicate_roster_batches": sorted(
                batch_id for batch_id in row["batches"] if batch_id in duplicates),
            "uncertainty_people": row["nonresponse"],
            "coverage_unknown_batches": row.get("coverage_unknown_batches", 0),
            "substantiated_rate": bounds["observed_rate"],
            "potential_rate_bounds": bounds["possible_rate_bounds"],
            "touch_coverage_bp": bounds["coverage_bp"],
            "coverage_complete": bounds["coverage_complete"],
        })
    return result


def assess_row(row: dict, policy: AnalysisPolicy) -> dict:
    gaps = []
    if row["exposed"] < policy.minimum_exposure:
        gaps.append("minimum_exposure")
    if row["nonresponse"]:
        total = row["exposed"] + row["nonresponse"]
        coverage = row["exposed"] * 10_000 // total if total else 0
        if coverage < policy.minimum_touch_coverage_bp:
            gaps.append("touch_coverage")
    if row.get("coverage_unknown_batches"):
        gaps.append("coverage_unknown")
    if row.get("unallocated_people") or row.get("attribution_incomplete_batches"):
        gaps.append("attribution_incomplete")
    if row.get("duplicate_roster_batches"):
        gaps.append("duplicate_roster")
    if row.get("possible_roster_overlaps"):
        gaps.append("roster_overlap_unverified")
    if row.get("reconciliation_incomplete_batches"):
        gaps.append("case_intake_incomplete")
    observed = row["substantiated_rate"]
    rate_value = float(observed.removesuffix("bp")) if observed is not None else None
    if rate_value is not None and rate_value > policy.max_substantiated_rate_bp:
        gaps.append("risk_signal")
    if policy.severe_open_cases_block and row["open_severe"]:
        gaps.append("severe_open_cases")
    return {**row, "gaps": gaps, "eligible": not gaps}


def run(db: Connection, *, cut: int, as_of: datetime, region: str,
        service_kind: ServiceKind | None, cohort: str | None,
        policy: AnalysisPolicy) -> dict:
    if as_of.tzinfo is None:
        raise invalid("analysis_time", "分析截点必须带时区")
    as_of = as_of.astimezone(timezone.utc)
    current = current_cut(db)
    if cut > current:
        raise invalid("analysis_cut", "分析截点超出当前事实")
    providers = catalog(db, region=region, cut=cut)
    needs_rows = needs(db, region=region, cut=cut, as_of=as_of)
    filtered = scoped_batches(db, cut, as_of, region=region,
                              service_kind=service_kind, cohort=cohort)
    rows = risk_rows(db, cut, as_of, region=region, service_kind=service_kind, cohort=cohort)
    reconciliation = reconcile(db, cut=cut, as_of=as_of)
    invalid_batches = {row["batch_id"] for row in reconciliation["batches"] if not row["complete"]}
    attribution_incomplete = {row["batch_id"] for row in reconciliation["batches"]
                              if "attribution_incomplete" in row["problems"]}
    for row in rows:
        row["reconciliation_incomplete_batches"] = sum(
            batch_id in invalid_batches for batch_id in row["batches"])
        row["attribution_incomplete_batches"] = sum(
            batch_id in attribution_incomplete for batch_id in row["batches"])
    scoped_providers = [item for item in providers
                        if service_kind is None or item["service_kind"] == service_kind]
    scoped_needs = [item for item in needs_rows
                    if service_kind is None or item["service_kind"] == service_kind]
    supply = supply_coverage(scoped_providers, scoped_needs, region, as_of)
    scored = []
    for row in rows:
        item = assess_row(row, policy)
        coverage = supply.get(row["service_kind"], {}).get("capacity_ratio")
        if coverage is not None:
            coverage_bp = round(float(coverage.removesuffix("bp")))
            item["supply_coverage_bp"] = coverage_bp
            if coverage_bp < policy.minimum_supply_coverage_bp:
                item["gaps"].append("supply_coverage")
                item["eligible"] = False
        else:
            item["supply_coverage_bp"] = None
            if policy.minimum_supply_coverage_bp:
                item["gaps"].append("supply_unobserved")
                item["eligible"] = False
        if policy.require_verified_remediation and item["open_severe"]:
            item["gaps"].append("remediation_review_required")
            item["eligible"] = False
        if policy.require_verified_remediation and item["substantiated"] > item["verified_remediations"]:
            item["gaps"].append("remediation_unverified")
            item["eligible"] = False
        scored.append(item)
    risk_flags = [row for row in scored if row["gaps"]]
    duplicates = attribution.duplicate_roster_batches(filtered)
    overlaps = attribution.possible_roster_overlaps(filtered)
    outreach = attribution.totals(filtered)
    document = {
        "format": 1, "region": region, "service_kind": service_kind,
        "cohort": cohort, "cut": cut, "as_of": as_of.isoformat(),
        "policy": policy.model_dump(mode="json"), "supply": supply,
        "areas": scored, "risk_flags": risk_flags,
        "attribution": {**outreach,
                        "activity_people": sum(row["exposed"] for row in scored),
                        "unattributed_people": (outreach["shared_people"]
                                                + outreach["unallocated_people"]),
                        "duplicate_roster_batches": sorted(duplicates),
                        "possible_roster_overlaps": overlaps},
        "reconciliation": {"complete": reconciliation["complete"],
                           "incomplete_batch_count": len(invalid_batches),
                           "unmatched_case_count": len(reconciliation["unmatched_case_ids"])},
        "case_counts": {
            "substantiated": sum(row["substantiated"] for row in scored),
            "pending": sum(row["pending"] for row in scored),
            "inconclusive": sum(row["inconclusive"] for row in scored),
        },
        "method": "Each activity's shared roster is counted once as exposure; attributed, shared and unallocated groups are reported separately rather than copied to every participant. Only facts observed by both the sequence cut and time cut are included; incomplete outreach is shown as uncertain.",
    }
    document["digest"] = digest(document)
    return document
