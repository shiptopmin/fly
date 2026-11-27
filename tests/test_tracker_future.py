"""Tracker 검색 대상에서 지난 출발일이 빠지는지 확인합니다. (사이트 접속 없음, 임시 폴더만 사용)"""
import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta

import config
import main
from collectors import google_flights
from collectors.base import CollectResult, PriceRecord
from collectors.google_flights import GRID_HALF_WINDOW, GoogleFlightsCollector
from routes import load_routes
from search_conditions import SearchQuery

plan = GoogleFlightsCollector._plan_anchors


def month_query(route):
    return SearchQuery.for_month(route.origin, route.destination, route.year, route.month,
                                 route.min_nights, route.max_nights, config.ALLOW_NEXT_MONTH_RETURN)


def covered(pair, anchors):
    """7x7 날짜 표(기준일 ±3일) 중 하나라도 이 (출발, 귀국) 조합을 보여 주는가."""
    h = timedelta(days=GRID_HALF_WINDOW)
    return any(a - h <= pair[0] <= a + h and b - h <= pair[1] <= b + h for a, b in anchors)


class FakeCollector(google_flights.GoogleFlightsCollector):
    """실제 collect()(트래커용)는 그대로 쓰고, 사이트에 접속하는 collect_query 만 대신합니다."""

    def __init__(self):
        super().__init__()
        self.queries = []

    def collect_query(self, query, headless):
        self.queries.append(query)
        res = CollectResult()
        for d, r in query.needed_pairs():
            res.records.append(PriceRecord(d.isoformat(), r.isoformat(), 200000, "KRW", "google_flights",
                                           "2026-11-27T15:00:00+09:00", "round_trip_total"))
        res.page_loads = len(plan(query.needed_pairs()))
        return res


class CollectRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (config.HISTORY_DIR, config.SEMANTICS_LOG, main.datetime, google_flights.datetime)
        config.HISTORY_DIR = os.path.join(self.tmp, "history")
        config.SEMANTICS_LOG = os.path.join(self.tmp, "diag", "semantics.csv")

    def tearDown(self):
        config.HISTORY_DIR, config.SEMANTICS_LOG, main.datetime, google_flights.datetime = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_route(self, today, month):
        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(today.year, today.month, today.day, 15, 0, 0, tzinfo=tz)

        main.datetime = google_flights.datetime = FakeDT
        route = [r for r in load_routes(today, include_ended=True)
                 if (r.year, r.month) == month and r.destination == "KIX"][0]
        fake = FakeCollector()
        with contextlib.redirect_stdout(io.StringIO()):
            summary, ok = main.collect_route(fake, route, True, logging.getLogger("t"))
        return route, fake, summary, ok

    def test_past_departures_are_not_searched(self):
        _, fake, summary, ok = self.run_route(date(2026, 11, 27), (2026, 11))
        pairs = fake.queries[0].needed_pairs()
        self.assertEqual(min(d for d, _ in pairs), date(2026, 11, 27))
        self.assertEqual(pairs, [(date(2026, 11, 27), date(2026, 11, 28)), (date(2026, 11, 27), date(2026, 11, 29)),
                                 (date(2026, 11, 27), date(2026, 11, 30)), (date(2026, 11, 28), date(2026, 11, 29)),
                                 (date(2026, 11, 28), date(2026, 11, 30)), (date(2026, 11, 29), date(2026, 11, 30))])
        self.assertTrue(all(a >= date(2026, 11, 27) for a, _ in plan(pairs)))
        self.assertTrue(ok)
        self.assertEqual(summary["records_added_to_csv"], 6)

    def test_departure_today_is_included(self):
        _, fake, _, _ = self.run_route(date(2026, 11, 29), (2026, 11))
        self.assertEqual(fake.queries[0].needed_pairs(), [(date(2026, 11, 29), date(2026, 11, 30))])

    def test_future_month_is_unchanged(self):
        route, fake, _, _ = self.run_route(date(2026, 11, 27), (2026, 12))
        self.assertEqual(fake.queries[0], month_query(route))
        self.assertEqual(plan(fake.queries[0].needed_pairs()), plan(month_query(route).needed_pairs()))

    def test_only_departure_range_start_changes(self):
        route, fake, _, _ = self.run_route(date(2026, 11, 27), (2026, 11))
        before, after = month_query(route), fake.queries[0]
        before.depart_from = date(2026, 11, 27)
        self.assertEqual(after, before)

    def test_month_with_no_remaining_departure_is_skipped_without_visit(self):
        for today in (date(2026, 11, 30), date(2026, 12, 5)):
            _, fake, summary, ok = self.run_route(today, (2026, 11))
            self.assertEqual(fake.queries, [])
            self.assertTrue(ok)
            self.assertEqual(summary["skipped"], "남은 출발일 없음")
            self.assertEqual(summary["page_loads"], 0)

    def test_existing_history_rows_are_kept_byte_for_byte(self):
        route = [r for r in load_routes(date(2026, 11, 27)) if r.destination == "KIX" and r.month == 11][0]
        os.makedirs(os.path.dirname(route.history_file), exist_ok=True)
        old = FakeCollector().collect_query(
            SearchQuery(route.origin, route.destination, date(2026, 11, 2), date(2026, 11, 2), 1, 1), True)
        for rec in old.records:
            rec.collected_at = "2026-11-01T15:00:00+09:00"
        main.storage.append_records(route.history_file, old.records, route.origin, route.destination)
        with open(route.history_file, "rb") as f:
            original = f.read()
        self.run_route(date(2026, 11, 27), (2026, 11))
        with open(route.history_file, "rb") as f:
            after = f.read()
        self.assertTrue(after.startswith(original))
        self.assertGreater(len(after), len(original))


class SearchPlanTests(unittest.TestCase):
    """실행일을 하루씩 옮겨 가며(2026-10-01 ~ 2027-03-31) 실제 검색 계획 함수로 확인합니다."""

    def test_every_remaining_pair_is_covered_and_no_anchor_is_in_the_past(self):
        day = date(2026, 10, 1)
        while day <= date(2027, 3, 31):
            for route in load_routes(day):
                full = month_query(route)
                q = month_query(route)
                q.depart_from = max(q.depart_from, day)
                pairs = q.needed_pairs()
                expected = [p for p in full.needed_pairs() if p[0] >= day]
                self.assertEqual(pairs, expected, (day, route.slug))
                anchors = plan(pairs)
                self.assertTrue(all(a >= day and b > a for a, b in anchors), (day, route.slug, anchors))
                self.assertEqual([p for p in pairs if not covered(p, anchors)], [], (day, route.slug))
                self.assertLessEqual(len(anchors), len(plan(full.needed_pairs())), (day, route.slug))
            day += timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
