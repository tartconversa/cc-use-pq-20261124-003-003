from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Region = Literal["urban", "suburban", "rural"]
ServiceKind = Literal["home_care", "meal", "medical", "day_center", "transport", "financial_guidance"]
IssueKind = Literal["misleading_claim", "payment_dispute", "service_failure", "suspected_fraud", "access_barrier"]
Finding = Literal["pending", "substantiated", "not_substantiated", "inconclusive"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_time(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(timezone.utc).replace(microsecond=0)
        return value


class Provider(Strict):
    provider_id: Key
    display_name: Text
    region: Region
    district: Text
    service_kind: ServiceKind
    capacity_per_month: Annotated[int, Field(ge=0, le=10_000_000)]
    active_from: datetime
    active_until: datetime | None = None
    status: Literal["active", "suspended", "closed"] = "active"
    updated_at: datetime

    @model_validator(mode="after")
    def period(self):
        if self.active_until and self.active_until <= self.active_from:
            raise ValueError("供给结束时间必须晚于生效时间")
        return self


class Participant(Strict):
    """联合活动的参与机构及其在该活动中的服务内容。"""
    provider_id: Key
    role: Literal["lead", "co_host"] = "co_host"
    service_kind: ServiceKind | None = None
    service_content: Text | None = None
    commitment: Text | None = None
    attributed_people: Annotated[int, Field(ge=0, le=100_000_000)] | None = None
    completed_services: Annotated[int, Field(ge=0, le=100_000_000)] | None = None


class TouchBatch(Strict):
    batch_id: Key
    campaign_id: Key
    provider_id: Key
    region: Region
    district: Text
    service_kind: ServiceKind
    channel: Literal["in_person", "phone", "web", "community"]
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    period_start: datetime
    period_end: datetime
    exposed_people: Annotated[int, Field(ge=0, le=100_000_000)]
    completed_services: Annotated[int, Field(ge=0, le=100_000_000)]
    report_count: Annotated[int, Field(ge=0, le=10_000_000)]
    nonresponse_people: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    coverage_complete: bool = True
    participants: Annotated[list[Participant], Field(max_length=20)] = Field(default_factory=list)
    participants_complete: bool = True
    shared_people: Annotated[int, Field(ge=0, le=100_000_000)] | None = None
    unallocated_people: Annotated[int, Field(ge=0, le=100_000_000)] | None = None
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text
    observed_at: datetime

    @model_validator(mode="after")
    def bounds(self):
        if self.period_end <= self.period_start:
            raise ValueError("触达统计期必须为正区间")
        if self.exposed_people + self.nonresponse_people < self.report_count:
            raise ValueError("触达及未回应总人数不能小于线索数")
        if not self.participants:
            if self.shared_people not in (None, 0) or self.unallocated_people not in (None, 0):
                raise ValueError("单一服务方的触达不能声明共享或未分配人数")
            return self
        identities = [row.provider_id for row in self.participants]
        if len(set(identities)) != len(identities):
            raise ValueError("联合活动不能重复登记同一参与机构")
        if self.provider_id not in identities:
            raise ValueError("牵头服务方必须是活动参与机构")
        leads = [row for row in self.participants if row.role == "lead"]
        if len(leads) != 1 or leads[0].provider_id != self.provider_id:
            raise ValueError("联合活动必须有且仅有一家牵头机构")
        attributed = sum(row.attributed_people or 0 for row in self.participants)
        if attributed > self.exposed_people:
            raise ValueError("各参与方专门服务人数不能超过活动触达总人数")
        if self.shared_people is not None or self.unallocated_people is not None:
            shared = self.shared_people or 0
            unallocated = self.unallocated_people or 0
            if attributed + shared + unallocated != self.exposed_people:
                raise ValueError("明确声明的归属人数必须与活动触达总人数一致")
        return self


class Case(Strict):
    case_id: Key
    provider_id: Key
    batch_id: Key
    kind: IssueKind
    severity: Literal["low", "medium", "high", "critical"]
    reported_at: datetime
    allegation: Text
    source_ref: Text
    affected_people: Annotated[int, Field(ge=0, le=1_000_000)]
    status: Literal["open", "investigating", "closed"] = "open"
    scope: Literal["provider_service", "joint_campaign"] = "provider_service"
    respondent_provider_ids: Annotated[list[Key], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def respondents(self):
        if len(set(self.respondent_provider_ids)) != len(self.respondent_provider_ids):
            raise ValueError("责任机构不能重复")
        if self.scope == "provider_service" and self.respondent_provider_ids not in (
                [], [self.provider_id]):
            raise ValueError("针对具体服务的线索只能由该服务方承担责任")
        return self


class NeedBatch(Strict):
    estimate_id: Key
    region: Region
    district: Text
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    service_kind: ServiceKind
    estimated_people: Annotated[int, Field(ge=0, le=100_000_000)]
    period_start: datetime
    period_end: datetime
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text

    @model_validator(mode="after")
    def estimate_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("需求估计周期必须为正区间")
        return self


class CaseEvidence(Strict):
    evidence_id: Key
    case_id: Key
    evidence_type: Literal["contract", "recording", "receipt", "interview", "inspection", "other"]
    source_ref: Text
    content_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    observed_at: datetime
    verified: bool = False


class ReviewDecision(Strict):
    decision_id: Key
    case_id: Key
    reviewer: Key
    role: Literal["investigator", "compliance"]
    finding: Finding
    decided_at: datetime
    rationale: Text
    evidence_refs: list[Text] = Field(min_length=1, max_length=30)


class Remediation(Strict):
    action_id: Key
    case_id: Key
    provider_id: Key
    owner: Key
    due_at: datetime
    action: Text
    evidence_ref: Text | None = None
    verified_by: Key | None = None
    verified_at: datetime | None = None
    status: Literal["planned", "in_progress", "submitted", "verified", "rejected"] = "planned"

    @model_validator(mode="after")
    def verification(self):
        if self.status == "verified" and (not self.verified_by or not self.verified_at or not self.evidence_ref):
            raise ValueError("已核验整改需要独立人员、时间和证据")
        if self.verified_by == self.owner:
            raise ValueError("整改责任人不能自行核验")
        return self


class AnalysisPolicy(Strict):
    policy_id: Key
    minimum_exposure: Annotated[int, Field(ge=1, le=1_000_000_000)] = 100
    minimum_supply_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    max_substantiated_rate_bp: Annotated[int, Field(ge=0, le=10_000)] = 50
    severe_open_cases_block: bool = True
    require_verified_remediation: bool = True
    minimum_touch_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000


class AnalysisRequest(Strict):
    analysis_id: Key
    region: Region
    service_kind: ServiceKind | None = None
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"] | None = None
    policy: AnalysisPolicy
    through_cut: Annotated[int, Field(ge=0, le=2**63 - 1)]
    as_of: datetime


class Approval(Strict):
    approval_id: Key
    analysis_id: Key
    reviewer: Key
    role: Literal["service_planning", "consumer_protection"]
    approved: bool
    analysis_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    rationale: Text


class PublishRequest(Strict):
    analysis_id: Key
    expected_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    requested_at: datetime
