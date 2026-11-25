"""联合活动的参与方与触达人数归属口径。

一次联合活动只登记一条触达事实，``exposed_people`` 是这份共用名单中的
独立人数，不能在参与机构之间重复相加。参与方可以声明各自专门服务的
人数（``attributed_people``），其余部分保留为共享（shared）或未分配
（unallocated）范围，而不是默认复制给每一家机构。
"""
from __future__ import annotations

from collections import Counter


def participant_rows(batch: dict) -> list[dict]:
    """返回活动的参与方，旧记录视为单一牵头服务方。"""
    rows = batch.get("participants") or []
    if rows:
        return rows
    return [{"provider_id": batch["provider_id"], "role": "lead",
             "service_kind": batch.get("service_kind"),
             "service_content": None, "commitment": None,
             "attributed_people": batch.get("exposed_people"),
             "completed_services": batch.get("completed_services")}]


def is_joint(batch: dict) -> bool:
    return bool(batch.get("participants"))


def participant_ids(batch: dict) -> list[str]:
    return [row["provider_id"] for row in participant_rows(batch)]


def offers_service(batch: dict, service_kind: str | None) -> bool:
    """活动是否涉及指定服务，含共同参与方各自介绍的服务。"""
    if service_kind is None:
        return True
    if batch.get("service_kind") == service_kind:
        return True
    return any((row.get("service_kind") or batch.get("service_kind")) == service_kind
               for row in participant_rows(batch))


def service_content(batch: dict) -> list[dict]:
    """参与方介绍的服务与承诺，不包含机构标识。"""
    scope = activity_scope(batch)
    return [{"role": row.get("role") or "co_host",
             "service_kind": row.get("service_kind") or batch.get("service_kind"),
             "service_content": row.get("service_content"),
             "commitment": row.get("commitment"),
             "attributed_people": (row.get("attributed_people")
                                   if scope["joint"] else scope["activity_people"])}
            for row in scope["participants"]]


def _remainder_split(batch: dict, explicit: int) -> tuple[int, int]:
    """把未明确归属的人数拆为共享与未分配，绝不复制给各参与方。"""
    remainder = max(int(batch.get("exposed_people", 0)) - explicit, 0)
    declared_shared = batch.get("shared_people")
    declared_unallocated = batch.get("unallocated_people")
    if batch.get("participants_complete", True):
        default_shared, default_unallocated = remainder, 0
    else:
        default_shared, default_unallocated = 0, remainder
    if declared_shared is None and declared_unallocated is None:
        return default_shared, default_unallocated
    if declared_shared is None:
        unallocated = int(declared_unallocated or 0)
        return remainder - unallocated, unallocated
    if declared_unallocated is None:
        shared = int(declared_shared or 0)
        return shared, remainder - shared
    return int(declared_shared or 0), int(declared_unallocated or 0)


def activity_scope(batch: dict) -> dict:
    """计算一次活动的独立人数、专门归属人数、共享及未分配人数。

    ``attributed_people`` 只统计明确声明为某一家机构专门服务的人数。
    未声明的剩余人数按名单完整性归入 ``shared_people``（参与方共同服务
    但无法拆分）或 ``unallocated_people``（参与方资料不完整）。
    """
    exposed = int(batch.get("exposed_people", 0))
    rows = batch.get("participants") or []
    if not rows:
        provider_rows = participant_rows(batch)
        return {"joint": False, "participants": provider_rows,
                "participant_ids": [row["provider_id"] for row in provider_rows],
                "participant_count": 1,
                "activity_people": exposed,
                "attributed_people": exposed,
                "shared_people": 0, "unallocated_people": 0,
                "participants_complete": True, "basis": "provider_only"}

    explicit = sum(int(row.get("attributed_people") or 0) for row in rows
                   if row.get("attributed_people") is not None)
    shared, unallocated = _remainder_split(batch, explicit)
    shared, unallocated = max(shared, 0), max(unallocated, 0)

    normalized = []
    for row in rows:
        item = dict(row)
        item["service_kind"] = item.get("service_kind") or batch.get("service_kind")
        normalized.append(item)
    if shared and not unallocated:
        basis = "shared"
    elif unallocated and not shared:
        basis = "unallocated"
    elif shared or unallocated:
        basis = "mixed"
    else:
        basis = "allocated"
    return {"joint": True, "participants": normalized,
            "participant_ids": [row["provider_id"] for row in normalized],
            "participant_count": len(normalized),
            "activity_people": exposed,
            "attributed_people": explicit,
            "shared_people": shared, "unallocated_people": unallocated,
            "participants_complete": bool(batch.get("participants_complete", True)),
            "basis": basis}


def summary(batch: dict) -> dict:
    """活动层级的归属摘要，用于地区与公告输出。"""
    scope = activity_scope(batch)
    return {"activity_people": scope["activity_people"],
            "attributed_people": scope["attributed_people"],
            "shared_people": scope["shared_people"],
            "unallocated_people": scope["unallocated_people"],
            "attribution_basis": scope["basis"],
            "participant_count": scope["participant_count"],
            "joint": scope["joint"],
            "participants_complete": scope["participants_complete"]}


def participant_activity(batch: dict, provider_id: str) -> dict | None:
    """一家机构在活动中的角色、服务内容和归属人数。"""
    scope = activity_scope(batch)
    entry = next((row for row in scope["participants"]
                  if row["provider_id"] == provider_id), None)
    if entry is None:
        return None
    attributed = entry.get("attributed_people")
    if not scope["joint"]:
        basis = "provider_only"
    elif attributed:
        basis = "exclusive"
    elif scope["shared_people"]:
        basis = "shared"
    elif scope["unallocated_people"]:
        basis = "unallocated"
    else:
        basis = "joint"
    return {"batch_id": batch["batch_id"], "provider_id": provider_id,
            "participant_count": scope["participant_count"],
            "role": entry.get("role") or ("lead" if not scope["joint"] else "co_host"),
            "service_kind": entry.get("service_kind"),
            "service_content": entry.get("service_content"),
            "commitment": entry.get("commitment"),
            "joint": scope["joint"],
            "activity_people": scope["activity_people"],
            "attributed_people": (attributed if scope["joint"] else scope["activity_people"]),
            "shared_people": scope["shared_people"],
            "unallocated_people": scope["unallocated_people"],
            "participants_complete": scope["participants_complete"],
            "attribution_basis": basis}


def case_in_service(case: dict, batch: dict, service_kind: str | None) -> bool:
    """服务筛选下线索是否计入：整场宣传计入活动涉及的所有服务，
    针对一家机构具体服务的线索只计入该机构介绍的服务。"""
    if service_kind is None:
        return True
    if case.get("scope") == "joint_campaign":
        return offers_service(batch, service_kind)
    scope = activity_scope(batch)
    entry = next((row for row in scope["participants"]
                  if row["provider_id"] == case["provider_id"]), None)
    kind = (entry or {}).get("service_kind") or batch.get("service_kind")
    return kind == service_kind


def case_targets_for(case: dict, batch: dict) -> dict:
    """按线索指向对象解析责任机构。

    ``provider_service`` 只指向一家机构的具体服务；``joint_campaign``
    指向整场联合宣传，默认由全部参与机构共同承担责任，也可显式限定为
    参与机构中的若干家。
    """
    scope = activity_scope(batch)
    ids = scope["participant_ids"]
    targeted = case.get("scope", "provider_service")
    if case["provider_id"] not in ids:
        raise ValueError("provider_not_participant")
    if targeted == "joint_campaign":
        if not scope["joint"]:
            raise ValueError("joint_campaign_requires_joint_activity")
        supplied = case.get("respondent_provider_ids") or []
        targets = list(supplied) if supplied else list(ids)
        if not targets or not set(targets) <= set(ids):
            raise ValueError("respondent_not_participant")
    else:
        if case["provider_id"] not in ids:
            raise ValueError("provider_not_participant")
        supplied = case.get("respondent_provider_ids") or []
        if supplied and supplied != [case["provider_id"]]:
            raise ValueError("provider_service_scope_conflict")
        targets = [case["provider_id"]]
    entry = next((row for row in scope["participants"]
                  if row["provider_id"] == case["provider_id"]), None)
    if targeted == "joint_campaign":
        basis = "joint_campaign" if scope["joint"] else "provider_only"
    elif not scope["joint"]:
        basis = "provider_only"
    elif entry and entry.get("attributed_people"):
        basis = "exclusive"
    elif scope["shared_people"]:
        basis = "shared"
    elif scope["unallocated_people"]:
        basis = "unallocated"
    else:
        basis = "joint"
    return {"scope": targeted, "respondent_provider_ids": targets,
            "attribution_basis": basis}


def case_respondents(case: dict, batch: dict | None = None) -> list[str]:
    """责任的机构清单；优先使用线索登记时冻结的归属。"""
    if case.get("respondent_provider_ids"):
        return list(case["respondent_provider_ids"])
    if batch is None:
        return [case["provider_id"]]
    try:
        return case_targets_for(case, batch)["respondent_provider_ids"]
    except (KeyError, ValueError):
        return [case["provider_id"]]


def activity_key(batch: dict) -> tuple:
    """识别同一场活动共用名单的键；不包含服务方或服务类别。"""
    return (batch.get("campaign_id"), batch.get("region"), batch.get("district"),
            batch.get("cohort"), batch.get("channel"), batch.get("period_start"),
            batch.get("period_end"), batch.get("exposed_people"))


def activity_groups(rows: list[dict]) -> dict[tuple, list[dict]]:
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        grouped.setdefault(activity_key(row), []).append(row)
    return grouped


def roster_overlaps(rows: list[dict]) -> list[dict]:
    """同一活动被不同机构分别登记时，指出名单是否可能重复。

    活动、地区、统计期、渠道与人群相同的多条触达，若分属不同机构，就
    可能把同一批老人算作多次独立触达。人数完全相同时可确认是同一名单
    （``exact``）；人数不同则只标记疑似重叠，交由人工核实，不擅自合并。
    """
    by_scope: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row.get("campaign_id"), row.get("region"), row.get("district"),
               row.get("cohort"), row.get("channel"), row.get("period_start"),
               row.get("period_end"))
        by_scope.setdefault(key, []).append(row)
    overlaps = []
    for key, group in sorted(by_scope.items(), key=lambda item: str(item[0])):
        if len({row.get("provider_id") for row in group}) < 2:
            continue
        counts = {row.get("exposed_people") for row in group}
        overlap = {"campaign_id": key[0], "district": key[2], "cohort": key[3],
                   "channel": key[4], "batch_ids": sorted(row["batch_id"] for row in group),
                   "provider_ids": sorted(row.get("provider_id") for row in group),
                   "exposed_counts": sorted(counts),
                   "exact": len(counts) == 1,
                   "multiple_activities": False}
        overlaps.append(overlap)
    return overlaps


def duplicate_roster_batches(rows: list[dict]) -> dict[str, list[str]]:
    """确认是同一名单、可安全按活动去重的批次。

    仅当活动、地区、统计期、渠道、人群与独立人数都相同，且分属不同机构
    时，才视为同一名单被重复登记。这里报告风险并按活动去重，不擅自把
    人数分配给任一参与方。
    """
    result = {}
    for overlap in roster_overlaps(rows):
        if not overlap["exact"]:
            continue
        ids = overlap["batch_ids"]
        for batch_id in ids:
            result[batch_id] = ids
    return result


def possible_roster_overlaps(rows: list[dict]) -> list[dict]:
    """分属不同机构但人数不一致的重叠登记，仅标记、不合并。"""
    return [overlap for overlap in roster_overlaps(rows) if not overlap["exact"]]


def totals(rows: list[dict], *, provider_id: str | None = None) -> dict:
    """汇总多条活动的归属口径；活动人数按活动去重累计。"""
    basis_counts = Counter()
    activity = attributed = shared = unallocated = 0
    joint = participants = included = 0
    for group in activity_groups(rows).values():
        scope = activity_scope(min(group, key=lambda row: row["batch_id"]))
        member = None
        if provider_id is not None:
            members = [activity_scope(row)["participants"] for row in group]
            member = next((entry for entry in
                           (row for rows_ in members for row in rows_
                            if row["provider_id"] == provider_id)), None)
            if member is None:
                continue
            own = member.get("attributed_people")
            if not scope["joint"]:
                own = scope["activity_people"]
            attributed += int(own or 0)
        included += 1
        if provider_id is None:
            attributed += scope["attributed_people"]
        activity += scope["activity_people"]
        shared += scope["shared_people"]
        unallocated += scope["unallocated_people"]
        joint += int(scope["joint"])
        participants += scope["participant_count"]
        basis_counts[scope["basis"]] += 1
    if not basis_counts:
        basis = "no_activity"
    elif len(basis_counts) == 1:
        basis = next(iter(basis_counts))
    else:
        basis = "mixed"
    return {"activity_count": included, "joint_activity_count": joint,
            "activity_people": activity, "attributed_people": attributed,
            "shared_people": shared, "unallocated_people": unallocated,
            "participant_activity_count": participants,
            "attribution_basis": basis, "basis_counts": dict(sorted(basis_counts.items()))}
