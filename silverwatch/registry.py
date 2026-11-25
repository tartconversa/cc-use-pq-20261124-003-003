from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import attribution
from silverwatch.contracts import NeedBatch, Provider, TouchBatch
from silverwatch.errors import invalid
from silverwatch.ledger import append, current_entities, latest


def register_provider(db: Connection, provider: Provider, source_ref: str, revision: int) -> dict:
    return append(db, "provider", provider.provider_id, revision, source_ref,
                  provider.model_dump(mode="json"), provider.updated_at)


def register_touch(db: Connection, batch: TouchBatch) -> dict:
    providers = {row["entity_id"] for row in latest(db, "provider")}
    if batch.provider_id not in providers:
        raise invalid("provider_missing", "触达批次的服务提供方尚未登记")
    missing = sorted(set(attribution.participant_ids(batch.model_dump(mode="json")))
                     - providers)
    if missing:
        raise invalid("participant_missing", "联合活动的参与机构尚未登记",
                      {"provider_ids": missing})
    return append(db, "touch", batch.batch_id, batch.source_revision,
                  batch.source_ref, batch.model_dump(mode="json"), batch.observed_at)


def register_need(db: Connection, batch: NeedBatch) -> dict:
    return append(db, "need", batch.estimate_id, batch.source_revision,
                  batch.source_ref, batch.model_dump(mode="json"), batch.period_end)


def register_case(db: Connection, body: dict, source_ref: str, revision: int) -> dict:
    required = {"case_id", "provider_id", "batch_id", "kind", "severity", "reported_at",
                "allegation", "source_ref", "affected_people", "status"}
    optional = {"scope", "respondent_provider_ids"}
    if not required <= set(body) or set(body) - required - optional:
        raise invalid("case_shape", "线索记录字段不完整或包含未支持字段")
    try:
        from silverwatch.contracts import Case

        case = Case.model_validate(body)
    except ValidationError as exc:
        raise invalid("case_invalid", "线索记录不符合业务字段", exc.errors(include_url=False)) from exc
    providers = {row["entity_id"] for row in current_entities(db, "provider")}
    if case.provider_id not in providers:
        raise invalid("provider_missing", "线索指向的服务提供方尚未登记")
    if not any(row["entity_id"] == case.batch_id for row in current_entities(db, "touch")):
        raise invalid("touch_missing", "线索分母批次尚未登记")
    batch = next(row["payload"] for row in current_entities(db, "touch") if row["entity_id"] == case.batch_id)
    try:
        targets = attribution.case_targets_for(case.model_dump(mode="json"), batch)
    except (KeyError, ValueError) as exc:
        raise invalid("case_scope", "线索归属与活动参与方不一致", {"reason": str(exc)}) from exc
    missing = sorted(set(targets["respondent_provider_ids"]) - providers)
    if missing:
        raise invalid("provider_missing", "线索责任机构尚未登记", {"provider_ids": missing})
    body = case.model_dump(mode="json")
    if not body["respondent_provider_ids"]:
        body["respondent_provider_ids"] = targets["respondent_provider_ids"]
    if case.affected_people > batch["exposed_people"] + batch["nonresponse_people"]:
        raise invalid("case_population", "影响人数不能超过触达及未回应总人数")
    return append(db, "case", case.case_id, revision, source_ref, body, case.reported_at)


def catalog(db: Connection, *, region: str | None = None, cut: int | None = None) -> list[dict]:
    providers = current_entities(db, "provider", cut=cut)
    result = []
    for row in providers:
        value = row["payload"]
        if region and value["region"] != region:
            continue
        result.append({**value, "source_revision": row["revision"], "source_ref": row["source_ref"],
                       "event_seq": row["seq"], "digest": row["digest"]})
    return result


def touch_facts(db: Connection, *, region: str | None = None, cut: int | None = None) -> list[dict]:
    result = []
    for row in current_entities(db, "touch", cut=cut):
        value = row["payload"]
        if region and value["region"] != region:
            continue
        result.append({**value, "attribution": attribution.summary(value),
                       "event_seq": row["seq"], "digest": row["digest"]})
    return result


def needs(db: Connection, *, region: str | None = None, cut: int | None = None,
          as_of: datetime | None = None) -> list[dict]:
    result = []
    for row in current_entities(db, "need", cut=cut, as_of=as_of):
        value = row["payload"]
        if as_of is not None and datetime.fromisoformat(value["period_end"]) > as_of:
            continue
        if region is None or value["region"] == region:
            result.append({**value, "event_seq": row["seq"], "digest": row["digest"]})
    return result


def provider_capacity(db: Connection, *, region: str, service_kind: str,
                      at: datetime, cut: int | None = None) -> list[dict]:
    """返回所选时点仍在服务有效期内的机构。"""
    candidates = catalog(db, region=region, cut=cut)
    result = []
    instant = at.astimezone(timezone.utc)
    for provider in candidates:
        if provider["service_kind"] != service_kind or provider["status"] != "active":
            continue
        start = datetime.fromisoformat(provider["active_from"])
        end = datetime.fromisoformat(provider["active_until"]) if provider["active_until"] else None
        if start <= instant and (end is None or instant < end):
            result.append(provider)
    return result


def registry_summary(db: Connection, *, region: str, cut: int | None = None) -> dict:
    providers = catalog(db, region=region, cut=cut)
    by_kind: dict[str, dict] = {}
    for row in providers:
        group = by_kind.setdefault(row["service_kind"], {"providers": 0, "capacity": 0,
                                                          "active": 0, "suspended": 0,
                                                          "closed": 0})
        group["providers"] += 1
        group["capacity"] += row["capacity_per_month"]
        group[row["status"]] += 1
    return {"region": region, "provider_count": len(providers),
            "service_kinds": dict(sorted(by_kind.items())),
            "cut": cut if cut is not None else source_cut(db)["cut"]}


def source_cut(db: Connection) -> dict:
    from silverwatch.ledger import current_cut

    return {"cut": current_cut(db), "captured_at": datetime.now(timezone.utc).isoformat()}
