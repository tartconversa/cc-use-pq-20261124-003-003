from __future__ import annotations

import unittest

from silverwatch import (analysis, attribution, cases, channels, comparison,
                          exports, intake, publication, queries, reconciliation,
                          registry, review, workqueue)
from silverwatch.contracts import AnalysisPolicy, Case, NeedBatch, Provider, TouchBatch
from silverwatch.errors import Problem
from silverwatch.storage import Store
from tests.support import (DatabaseCase, instant, need_data, provider_data,
                          touch_data)


def joint_touch(**updates):
    """一场共用名单的联合活动：午餐与日间照料共同参与。"""
    value = touch_data(
        batch_id="joint-open-day", campaign_id="open-day", provider_id="provider-one",
        service_kind="meal", channel="community", cohort="70_79",
        exposed_people=300, nonresponse_people=10, report_count=1,
        participants=[
            {"provider_id": "provider-one", "role": "lead", "service_kind": "meal",
             "service_content": "集体午餐", "commitment": "每周三次午餐",
             "attributed_people": 120},
            {"provider_id": "provider-two", "role": "co_host", "service_kind": "day_center",
             "service_content": "日间照料体验", "commitment": "提供半天照料",
             "attributed_people": 80}],
        shared_people=100)
    return value | updates


def joint_case(**updates):
    value = {"case_id": "joint-case", "provider_id": "provider-one",
             "batch_id": "joint-open-day", "kind": "misleading_claim",
             "severity": "medium", "reported_at": "2026-10-10T09:00:00Z",
             "allegation": "The joint leaflet overstated the services on offer.",
             "source_ref": "hotline", "affected_people": 2, "status": "investigating",
             "scope": "joint_campaign"}
    return value | updates


def policy(**updates):
    return AnalysisPolicy.model_validate({"policy_id": "attribution-policy",
                                          "minimum_exposure": 100,
                                          "minimum_supply_coverage_bp": 0,
                                          "max_substantiated_rate_bp": 10_000,
                                          "minimum_touch_coverage_bp": 0} | updates)


class JointActivityTests(DatabaseCase, unittest.TestCase):
    def seed(self, db, *, touch=None, cases_rows=(), need=True):
        registry.register_provider(db, Provider.model_validate(provider_data()), "feed", 1)
        registry.register_provider(db, Provider.model_validate(provider_data(
            provider_id="provider-two", display_name="Day Centre",
            service_kind="day_center")), "feed", 1)
        if need:
            registry.register_need(db, NeedBatch.model_validate(need_data()))
        registry.register_touch(db, TouchBatch.model_validate(touch or joint_touch()))
        for row in cases_rows:
            cases.create(db, Case.model_validate(row), "case-feed", 1)

    def run_analysis(self, db, *, service_kind="meal", as_of="2026-11-02T00:00:00Z"):
        return analysis.run(db, cut=registry.source_cut(db)["cut"], as_of=instant(as_of),
                            region="suburban", service_kind=service_kind, cohort="70_79",
                            policy=policy())

    def test_shared_roster_is_counted_once_and_split_by_attribution(self):
        with self.transaction() as db:
            self.seed(db)
            area = self.run_analysis(db)["areas"][0]
            self.assertEqual(area["exposed"], 300)
            self.assertEqual(area["attributed_people"], 200)
            self.assertEqual(area["shared_people"], 100)
            self.assertEqual(area["unallocated_people"], 0)
            self.assertEqual(area["attribution_basis"], "shared")
            self.assertEqual(area["joint_activity_count"], 1)
            self.assertEqual(
                {row["provider_id"] for row in area["participants"]},
                {"provider-one", "provider-two"})
            services = {row["service_kind"]: row for row in area["participants"]}
            self.assertEqual(services["day_center"]["service_content"], "日间照料体验")
            self.assertEqual(services["day_center"]["attributed_people"], 80)

    def test_missing_participant_data_stays_unallocated_instead_of_copying_people(self):
        with self.transaction() as db:
            self.seed(db, touch=joint_touch(participants_complete=False,
                                            shared_people=None))
            area = self.run_analysis(db)["areas"][0]
            self.assertEqual(area["exposed"], 300)
            self.assertEqual(area["attributed_people"], 200)
            self.assertEqual(area["shared_people"], 0)
            self.assertEqual(area["unallocated_people"], 100)
            self.assertEqual(area["attribution_basis"], "unallocated")
            self.assertIn("attribution_incomplete", area["gaps"])
            self.assertFalse(area["eligible"])

    def test_explicit_split_must_reconcile_with_the_activity_total(self):
        with self.assertRaises(ValueError):
            TouchBatch.model_validate(joint_touch(shared_people=70))
        with self.assertRaises(ValueError):
            TouchBatch.model_validate(joint_touch(exposed_people=150))

    def test_participants_must_be_registered_and_have_one_lead(self):
        with self.transaction() as db:
            self.seed(db)
            missing = joint_touch(batch_id="joint-missing-participant",
                                  participants=[
                                      {"provider_id": "provider-one", "role": "lead",
                                       "attributed_people": 120},
                                      {"provider_id": "provider-three", "role": "co_host",
                                       "attributed_people": 80}],
                                  shared_people=100)
            with self.assertRaises(Problem) as captured:
                registry.register_touch(db, TouchBatch.model_validate(missing))
            self.assertEqual(captured.exception.code, "participant_missing")
            two_leads = joint_touch(batch_id="joint-two-leads", participants=[
                {"provider_id": "provider-one", "role": "lead"},
                {"provider_id": "provider-two", "role": "lead"}])
            with self.assertRaises(ValueError):
                TouchBatch.model_validate(two_leads)

    def test_joint_campaign_case_targets_every_participant(self):
        with self.transaction() as db:
            self.seed(db, cases_rows=[joint_case()])
            case = cases.current_cases(db)[0]["payload"]
            self.assertEqual(case["scope"], "joint_campaign")
            self.assertEqual(set(case["respondent_provider_ids"]),
                             {"provider-one", "provider-two"})
            self.assertEqual(queries.provider_profile(db, "provider-two")["activity"]["case_count"], 1)
            self.assertEqual(queries.provider_profile(db, "provider-two")["activity"]
                             ["joint_activity_count"], 1)

    def test_service_case_only_binds_the_named_provider(self):
        with self.transaction() as db:
            self.seed(db, cases_rows=[joint_case(case_id="service-case",
                                                 scope="provider_service",
                                                 provider_id="provider-two")])
            case = cases.current_cases(db)[0]["payload"]
            self.assertEqual(case["respondent_provider_ids"], ["provider-two"])
            self.assertEqual(queries.provider_profile(db, "provider-one")["activity"]["case_count"], 0)
            self.assertEqual(queries.provider_profile(db, "provider-two")["activity"]["case_count"], 1)

    def test_respondents_must_belong_to_the_activity(self):
        with self.transaction() as db:
            self.seed(db)
            with self.assertRaises(Problem) as captured:
                cases.create(db, Case.model_validate(joint_case(
                    respondent_provider_ids=["provider-one", "provider-nine"])), "feed", 1)
            self.assertEqual(captured.exception.code, "case_scope")

    def test_joint_campaign_requires_a_joint_activity(self):
        with self.transaction() as db:
            self.seed(db, touch=touch_data())
            with self.assertRaises(Problem) as captured:
                cases.create(db, Case.model_validate(joint_case(
                    case_id="solo-joint", batch_id="touch-one")), "feed", 1)
            self.assertEqual(captured.exception.code, "case_scope")

    def test_joint_service_analysis_counts_the_activity_under_each_offered_service(self):
        with self.transaction() as db:
            self.seed(db, cases_rows=[
                joint_case(case_id="joint-ad", scope="joint_campaign"),
                joint_case(case_id="day-service", scope="provider_service",
                           provider_id="provider-two", kind="service_failure")])
            meal = self.run_analysis(db, service_kind="meal")["areas"][0]
            day = self.run_analysis(db, service_kind="day_center")["areas"][0]
            self.assertEqual(meal["attributed_people"], 200)
            self.assertEqual(day["attributed_people"], 200)
            # 整场联合宣传计入活动涉及的两个服务口径。
            self.assertEqual(meal["pending"], 1)
            # 针对日间照料具体服务的线索只出现在日间照料口径中。
            self.assertEqual(day["pending"], 2)
            self.assertEqual(meal["exposed"], 300)

    def test_district_overview_explains_totals_basis_and_unallocated_part(self):
        with self.transaction() as db:
            self.seed(db, touch=joint_touch(participants_complete=False,
                                            shared_people=None))
            overview = queries.district_overview(db, "suburban",
                                                 as_of=instant("2026-11-02T00:00:00Z"))
            area = overview["areas"][0]
            self.assertEqual(area["exposed_people"], 300)
            self.assertEqual(area["attributed_people"], 200)
            self.assertEqual(area["unallocated_people"], 100)
            self.assertEqual(area["activity_count"], 1)
            self.assertEqual(area["joint_activity_count"], 1)
            self.assertEqual(area["attribution_basis"], "unallocated")
            self.assertEqual(overview["attribution"]["activity_people"], 300)
            self.assertEqual(overview["attribution"]["unallocated_people"], 100)
            self.assertEqual(overview["attribution"]["unattributed_people"], 100)
            self.assertIn("不会按参与机构重复计入", overview["note"])

    def test_reconciliation_flags_incomplete_attribution_and_duplicate_rosters(self):
        with self.transaction() as db:
            self.seed(db, touch=joint_touch(participants_complete=False,
                                            shared_people=None))
            report = reconciliation.reconcile(db)
            row = report["batches"][0]
            self.assertIn("attribution_incomplete", row["problems"])
            self.assertEqual(row["unallocated_people"], 100)
            duplicate = touch_data(batch_id="joint-copy", campaign_id="open-day",
                                   provider_id="provider-two", service_kind="meal",
                                   exposed_people=300)
            registry.register_touch(db, TouchBatch.model_validate(duplicate))
            report = reconciliation.reconcile(db)
            self.assertEqual(sorted(report["duplicate_roster_batches"]),
                             ["joint-copy", "joint-open-day"])
            self.assertFalse(report["complete"])

    def test_overlapping_roster_with_different_counts_is_flagged_not_merged(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_touch(db, TouchBatch.model_validate(touch_data(
                batch_id="partial-copy", campaign_id="open-day",
                provider_id="provider-two", service_kind="meal", exposed_people=120)))
            report = reconciliation.reconcile(db)
            self.assertEqual(report["duplicate_roster_batches"], [])
            document = self.run_analysis(db)
            overlaps = document["attribution"]["possible_roster_overlaps"]
            self.assertEqual(len(overlaps), 1)
            self.assertFalse(overlaps[0]["exact"])
            self.assertIn("roster_overlap_unverified", document["risk_flags"][0]["gaps"])
            # 不能确认是同一名单时不合并，保留两条记录供人工核实。
            self.assertEqual(document["areas"][0]["exposed"], 420)

    def test_duplicate_roster_blocks_publication(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_touch(db, TouchBatch.model_validate(touch_data(
                batch_id="joint-copy", campaign_id="open-day", provider_id="provider-two",
                service_kind="meal", exposed_people=300)))
            document = self.run_analysis(db)
            self.assertEqual(document["attribution"]["duplicate_roster_batches"],
                             ["joint-copy", "joint-open-day"])
            self.assertIn("duplicate_roster", document["risk_flags"][0]["gaps"])
            # 同一名单的重复登记不再把 300 人算成 600 人。
            self.assertEqual(document["areas"][0]["exposed"], 300)
            self.assertEqual(document["areas"][0]["activity_count"], 1)
            self.assertEqual(document["attribution"]["activity_people"], 300)


class PublicAttributionTests(DatabaseCase, unittest.TestCase):
    def test_public_view_keeps_service_content_but_hides_participant_identities(self):
        row = {"district": "district-a", "service_kind": "meal", "cohort": "70_79",
               "exposed": 300, "nonresponse": 10, "attributed_people": 200,
               "shared_people": 100, "unallocated_people": 0, "unattributed_people": 100,
               "activity_count": 1, "joint_activity_count": 1,
               "participants": [{"provider_id": "provider-one", "role": "lead",
                                 "service_kind": "meal", "service_content": "集体午餐",
                                 "commitment": "每周三次午餐", "attributed_people": 120},
                                {"provider_id": "provider-two", "role": "co_host",
                                 "service_kind": "day_center", "service_content": "日间照料体验",
                                 "commitment": "提供半天照料", "attributed_people": 80}],
               "eligible": False, "gaps": ["attribution_incomplete"]}
        bulletin = {"edition": 1, "document": {
            "policy": {"minimum_exposure": 20}, "areas": [row], "risk_flags": [],
            "supply": {}, "case_counts": {"substantiated": 0, "pending": 1, "inconclusive": 0},
            "attribution": {"activity_count": 1, "activity_people": 300,
                            "attributed_people": 200, "shared_people": 100,
                            "unallocated_people": 0, "unattributed_people": 100,
                            "duplicate_roster_batches": ["joint-open-day"]}}}
        view = exports.public_view(bulletin)
        document = view["document"]
        self.assertEqual(document["areas"][0]["attributed_people"], 200)
        self.assertEqual(document["areas"][0]["unattributed_people"], 100)
        self.assertNotIn("participants", document["areas"][0])
        self.assertEqual(document["attribution"]["shared_people"], 100)
        self.assertNotIn("duplicate_roster_batches", document["attribution"])
        services = document["attribution"]["participants"]
        self.assertEqual({item["service_content"] for item in services},
                         {"集体午餐", "日间照料体验"})
        self.assertNotIn("provider-one", str(view))
        self.assertNotIn("provider-two", str(view))

    def test_small_joint_group_suppresses_participant_counts(self):
        row = {"district": "district-a", "service_kind": "meal", "cohort": "80_plus",
               "exposed": 12, "attributed_people": 6, "shared_people": 6,
               "unallocated_people": 0, "unattributed_people": 6,
               "participants": [{"provider_id": "provider-one", "role": "lead",
                                 "service_kind": "meal", "service_content": "午餐",
                                 "commitment": "配餐", "attributed_people": 6}],
               "eligible": False, "gaps": ["minimum_exposure"]}
        bulletin = {"edition": 1, "document": {
            "policy": {"minimum_exposure": 20}, "areas": [row], "risk_flags": [],
            "supply": {}, "case_counts": {"substantiated": 0, "pending": 0, "inconclusive": 0},
            "attribution": {}}}
        document = exports.public_view(bulletin)["document"]
        published = document["areas"][0]
        self.assertIsNone(published["attributed_people"])
        self.assertIsNone(published["shared_people"])
        self.assertIsNone(published["unattributed_people"])
        directory = document["attribution"]["participants"]
        self.assertEqual(directory[0]["service_content"], "午餐")
        self.assertIsNone(directory[0]["attributed_people"])
        self.assertEqual(directory[0]["disclosure_state"], "suppressed_small_group")


class CsvJointActivityTests(unittest.TestCase):
    def test_csv_touch_rows_accept_json_participant_lists(self):
        header = ("batch_id,campaign_id,provider_id,region,district,service_kind,channel,"
                  "cohort,period_start,period_end,exposed_people,completed_services,"
                  "report_count,nonresponse_people,coverage_complete,participants,"
                  "participants_complete,shared_people,unallocated_people,"
                  "source_revision,source_ref,observed_at")
        participants = ('[{"provider_id":"provider-one","role":"lead","service_kind":"meal",'
                        '"attributed_people":120}]')
        row = ('joint-open-day,open-day,provider-one,suburban,district-a,meal,community,'
               '70_79,2026-10-01T00:00:00Z,2026-10-31T00:00:00Z,300,150,1,10,true,'
               '"' + participants.replace('"', '""') + '",true,180,0,1,'
               'district-feed,2026-11-01T00:00:00Z')
        from silverwatch import csvio

        decoded = csvio.decode(header + "\n" + row + "\n", kind="touches",
                               batch_id="csv-joint", source_ref="csv", revision=1)
        data = decoded["records"][0]["data"]
        self.assertIsInstance(data["participants"], list)
        self.assertEqual(data["participants"][0]["role"], "lead")

    def test_csv_may_omit_columns_that_default_in_the_contract(self):
        header = ("batch_id,campaign_id,provider_id,region,district,service_kind,channel,"
                  "cohort,period_start,period_end,exposed_people,completed_services,"
                  "report_count,nonresponse_people,participants,source_revision,"
                  "source_ref,observed_at")
        row = ('joint-open-day,open-day,provider-one,suburban,district-a,meal,community,'
               '70_79,2026-10-01T00:00:00Z,2026-10-31T00:00:00Z,300,150,1,10,'
               '"[]",1,district-feed,2026-11-01T00:00:00Z')
        from silverwatch import csvio

        decoded = csvio.decode(header + "\n" + row + "\n", kind="touches",
                               batch_id="csv-joint", source_ref="csv", revision=1)
        data = decoded["records"][0]["data"]
        self.assertNotIn("coverage_complete", data)
        self.assertNotIn("participants_complete", data)
        self.assertEqual(data["participants"], [])

    def test_wrong_shape_participant_column_is_reported_at_the_source_line(self):
        header = ("batch_id,campaign_id,provider_id,region,district,service_kind,channel,"
                  "cohort,period_start,period_end,exposed_people,completed_services,"
                  "report_count,nonresponse_people,coverage_complete,participants,"
                  "source_revision,source_ref,observed_at")
        row = ('joint-open-day,open-day,provider-one,suburban,district-a,meal,community,'
               '70_79,2026-10-01T00:00:00Z,2026-10-31T00:00:00Z,300,150,1,10,true,'
               'not-json,1,district-feed,2026-11-01T00:00:00Z')
        from silverwatch import csvio

        with self.assertRaises(Problem) as captured:
            csvio.decode(header + "\n" + row + "\n", kind="touches",
                         batch_id="csv-joint", source_ref="csv", revision=1)
        self.assertEqual(captured.exception.details["errors"][0]["line"], 2)


if __name__ == "__main__":
    unittest.main()
