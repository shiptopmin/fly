"""
tests/test_routes.py - (노선, 월) 단위 추적, 월별 이력 분리, 월 전환 회귀 테스트

실행:
    python -m unittest tests.test_routes -v

실제 사이트에는 접속하지 않습니다. 수집기는 가짜, 모든 파일은 임시 폴더, 가격은 모두 가짜입니다.
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

import build_dashboard
import config
import main as tracker_main
import routes
from analyzer import report
from analyzer.combinations import load_rows
from analyzer.history import RouteHistory, thresholds_from_config
from collectors.base import CollectResult, PriceRecord
from search_conditions import SearchQuery

KST = routes.KST
def rb(path):
    with open(path, "rb") as f:
        return f.read()


def rt(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


KEYS = ["HISTORY_DIR", "PROBE_DIR", "SEMANTICS_LOG", "LAST_RUN_FILE", "DASHBOARD_FILE", "PUBLIC_THROUGH",
        "PAGE_LOAD_DELAY_SEC"]


class Clock:
    now = datetime(2026, 10, 17, 14, 30, tzinfo=KST)


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now


class FakeCollector:
    """남은 출발일 조합에만 가짜 가격을 돌려주는 수집기. 어떤 달을 요청받았는지 기록합니다."""
    calls = []

    def collect(self, origin, destination, year, month, min_nights, max_nights, allow_next_month_return, headless):
        FakeCollector.calls.append((origin, destination, year, month))
        q = SearchQuery.for_month(origin, destination, year, month, min_nights, max_nights, allow_next_month_return)
        stamp = Clock.now.replace(microsecond=0).isoformat()
        res = CollectResult(page_loads=9, semantics_verified=True)
        for i, (d, r) in enumerate(q.needed_pairs()):
            if d < Clock.now.date():
                res.missing_pairs.append((d, r))
                continue
            res.records.append(PriceRecord(d.isoformat(), r.isoformat(), 200000 + 100 * i + 10000 * (month % 3),
                                           "KRW", "google_flights", stamp, "round_trip_total"))
        return res


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(config, k) for k in KEYS}
        self.tmp = tempfile.mkdtemp()
        config.HISTORY_DIR = os.path.join(self.tmp, "data", "history")
        config.PROBE_DIR = os.path.join(self.tmp, "data", "probes")
        config.SEMANTICS_LOG = os.path.join(self.tmp, "data", "diag", "s.csv")
        config.LAST_RUN_FILE = os.path.join(self.tmp, "data", "last_run.json")
        config.DASHBOARD_FILE = os.path.join(self.tmp, "docs", "index.html")
        config.PUBLIC_THROUGH = (2026, 10)
        config.PAGE_LOAD_DELAY_SEC = 0
        self._dt = tracker_main.datetime
        tracker_main.datetime = FakeDT
        self._bd = build_dashboard.datetime
        build_dashboard.datetime = FakeDT
        self._bc = tracker_main.build_collector
        tracker_main.build_collector = lambda name: FakeCollector()
        FakeCollector.calls = []
        Clock.now = datetime(2026, 10, 17, 14, 30, tzinfo=KST)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        tracker_main.datetime, build_dashboard.datetime = self._dt, self._bd
        tracker_main.build_collector = self._bc
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call_main(self):
        old = sys.argv
        sys.argv = ["main.py"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return tracker_main.main()
        finally:
            sys.argv = old

    def build_pages(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return build_dashboard.main()


class TrackedMonthsTests(unittest.TestCase):
    def test_this_and_next_month_from_the_first_month(self):
        self.assertEqual(routes.tracked_months(date(2026, 10, 17)), [(2026, 10), (2026, 11)])

    def test_november_first_starts_december_and_keeps_october_as_ended(self):
        self.assertEqual(routes.tracked_months(date(2026, 11, 1)), [(2026, 10), (2026, 11), (2026, 12)])
        rs = routes.load_routes(date(2026, 11, 1), include_ended=True)
        self.assertEqual([(r.slug, r.ended(date(2026, 11, 1))) for r in rs],
                         [("ICN-KIX", True), ("ICN-FUK", True), ("ICN-KIX-2026-11", False),
                          ("ICN-FUK-2026-11", False), ("ICN-KIX-2026-12", False), ("ICN-FUK-2026-12", False)])
        self.assertEqual([r.slug for r in routes.load_routes(date(2026, 11, 1))],
                         ["ICN-KIX-2026-11", "ICN-FUK-2026-11", "ICN-KIX-2026-12", "ICN-FUK-2026-12"])

    def test_next_month_is_always_tracked_from_the_first_day_of_each_month(self):
        """2026-10-01 ~ 2029-12-31 의 모든 날짜: 다음 달 항목이 이미 있고, 이번 달 항목도 있다 (월 전환 시 이력 공백 방지)."""
        d = date(2026, 10, 1)
        while d <= date(2029, 12, 31):
            months = {(r.year, r.month) for r in routes.load_routes(d)}
            nxt = (d.year + (d.month == 12), d.month % 12 + 1)
            self.assertIn((d.year, d.month), months, d)
            self.assertIn(nxt, months, d)
            d += timedelta(days=1)

    def test_year_end(self):
        self.assertIn((2027, 1), routes.tracked_months(date(2026, 12, 1)))

    def test_slug_unchanged_for_october_and_month_suffixed_after(self):
        rs = {r.slug: r for r in routes.load_routes(date(2026, 10, 17))}
        self.assertEqual(sorted(rs), ["ICN-FUK", "ICN-FUK-2026-11", "ICN-KIX", "ICN-KIX-2026-11"])
        self.assertEqual(rs["ICN-KIX"].history_file, os.path.join(config.HISTORY_DIR, "ICN-KIX.csv"))
        self.assertEqual(rs["ICN-KIX-2026-11"].history_file, os.path.join(config.HISTORY_DIR, "ICN-KIX-2026-11.csv"))
        self.assertTrue(rs["ICN-KIX"].page_file.endswith(os.path.join("routes", "ICN-KIX.html")))
        self.assertEqual(len({r.history_file for r in rs.values()}), 4)       # 서로 다른 파일
        self.assertEqual(len({r.page_file for r in rs.values()}), 4)

    def test_public_gate(self):
        saved = config.PUBLIC_THROUGH
        try:
            config.PUBLIC_THROUGH = (2026, 10)
            pub = {r.slug: r.public for r in routes.load_routes(date(2026, 10, 17))}
            self.assertEqual(pub, {"ICN-KIX": True, "ICN-FUK": True, "ICN-KIX-2026-11": False, "ICN-FUK-2026-11": False})
            config.PUBLIC_THROUGH = None
            self.assertTrue(all(r.public for r in routes.load_routes(date(2026, 10, 17))))
        finally:
            config.PUBLIC_THROUGH = saved

    def test_same_route_and_month_twice_is_error_but_other_months_are_fine(self):
        saved = config.ROUTES
        try:
            config.ROUTES = [{"origin": "ICN", "destination": "KIX", "year": 2026, "month": 10},
                             {"origin": "ICN", "destination": "KIX", "year": 2026, "month": 11}]
            self.assertEqual(len(routes.load_routes(date(2026, 10, 17))), 2)
            config.ROUTES = [{"origin": "ICN", "destination": "KIX", "year": 2026, "month": 10},
                             {"origin": "ICN", "destination": "KIX", "year": 2026, "month": 10}]
            with self.assertRaises(ValueError):
                routes.load_routes(date(2026, 10, 17))
        finally:
            config.ROUTES = saved

    def test_observation_sources_default_is_unchanged(self):
        src = routes.observation_sources("ICN", "KIX")
        self.assertEqual([s[0] for s in src], ["tracker", "probe", "confirm"])
        self.assertEqual(src[0][1], os.path.join(config.HISTORY_DIR, "ICN-KIX.csv"))
        src = routes.observation_sources("ICN", "KIX", tracker_file="x.csv")
        self.assertEqual(src[0][1], "x.csv")


class CollectionTests(TempEnv):
    def history(self, slug):
        return load_rows(os.path.join(config.HISTORY_DIR, f"{slug}.csv"))

    def test_two_months_are_collected_into_separate_month_pure_files(self):
        self.assertEqual(self._call_main(), 0)
        self.assertEqual(sorted({c[3] for c in FakeCollector.calls}), [10, 11])
        for slug, ym in (("ICN-KIX", "2026-10"), ("ICN-KIX-2026-11", "2026-11"),
                         ("ICN-FUK", "2026-10"), ("ICN-FUK-2026-11", "2026-11")):
            rows = self.history(slug)
            self.assertTrue(rows, slug)
            self.assertEqual({r["departure_date"][:7] for r in rows}, {ym}, slug)

    def test_later_november_run_does_not_hide_or_erase_october(self):
        """같은 날 11월 실행이 10월보다 늦어도 10월 보고서/이력은 그대로입니다 (한 파일에 섞었다면 깨졌을 상황)."""
        self._call_main()
        oct_route = [r for r in routes.load_routes(Clock.now.date()) if r.slug == "ICN-KIX"][0]
        rp = report.build_report(oct_route, config)
        self.assertIsNotNone(rp)
        self.assertEqual(rp.latest_collected_at[:10], "2026-10-17")
        self.assertEqual({t.departure_date.month for t in rp.trips}, {10})
        # 10월 파일은 11월 수집이 있든 없든 같은 내용
        before = rb(oct_route.history_file)
        Clock.now += timedelta(hours=3)                       # 같은 날 더 늦은 두 번째 실행
        self._call_main()
        after = rb(oct_route.history_file)
        self.assertTrue(after.startswith(before))              # 기존 행은 한 글자도 안 바뀌고 뒤에만 추가
        rp2 = report.build_report(oct_route, config)
        self.assertEqual(rp2.latest_collected_at[:10], "2026-10-17")
        self.assertGreater(rp2.latest_collected_at, rp.latest_collected_at)

    def test_october_collection_identical_with_and_without_november(self):
        """11월 항목이 추가돼도 10월 수집 요청과 저장 내용은 같습니다."""
        self._call_main()
        with_nov = rb(os.path.join(config.HISTORY_DIR, "ICN-KIX.csv"))
        shutil.rmtree(os.path.join(self.tmp, "data"))
        saved = config.ROUTES
        config.ROUTES = [{"origin": "ICN", "destination": "KIX", "year": 2026, "month": 10},
                         {"origin": "ICN", "destination": "FUK", "year": 2026, "month": 10}]
        try:
            self._call_main()
        finally:
            config.ROUTES = saved
        only_oct = rb(os.path.join(config.HISTORY_DIR, "ICN-KIX.csv"))
        self.assertEqual(with_nov, only_oct)

    def test_month_with_no_remaining_departures_is_skipped_not_failed(self):
        Clock.now = datetime(2026, 10, 31, 14, 30, tzinfo=KST)
        self.assertEqual(self._call_main(), 0)                  # 실패(1)가 아님
        self.assertEqual(sorted({c[3] for c in FakeCollector.calls}), [11])    # 10월은 접속하지 않음
        import json
        summ = {s["route"]: s for s in json.loads(rt(config.LAST_RUN_FILE))["routes"]}
        self.assertEqual(summ["ICN-KIX"]["skipped"], "남은 출발일 없음")
        self.assertFalse(os.path.exists(os.path.join(config.HISTORY_DIR, "ICN-KIX.csv")))

    def test_ended_month_is_not_collected_and_december_starts_on_nov_first(self):
        Clock.now = datetime(2026, 11, 1, 14, 30, tzinfo=KST)
        self.assertEqual(self._call_main(), 0)
        self.assertEqual(sorted({c[3] for c in FakeCollector.calls}), [11, 12])
        for slug in ("ICN-KIX-2026-11", "ICN-KIX-2026-12", "ICN-FUK-2026-12"):
            self.assertTrue(self.history(slug), slug)


class PageTests(TempEnv):
    def test_hidden_months_have_no_card_or_page_until_published(self):
        self._call_main()
        self.build_pages()
        docs = os.path.dirname(config.DASHBOARD_FILE)
        self.assertEqual(sorted(os.listdir(os.path.join(docs, "routes"))), ["ICN-FUK.html", "ICN-KIX.html"])
        home = rt(config.DASHBOARD_FILE)
        self.assertIn("routes/ICN-KIX.html", home)
        self.assertNotIn("2026-11", home)
        self.assertNotIn("11월", home)
        config.PUBLIC_THROUGH = None
        self.build_pages()
        self.assertEqual(len(os.listdir(os.path.join(docs, "routes"))), 4)
        self.assertIn("routes/ICN-KIX-2026-11.html", rt(config.DASHBOARD_FILE))

    def test_ended_month_stays_on_screen_marked_ended(self):
        self._call_main()
        Clock.now = datetime(2026, 11, 2, 14, 30, tzinfo=KST)
        config.PUBLIC_THROUGH = None
        self.build_pages()
        docs = os.path.dirname(config.DASHBOARD_FILE)
        page = rt(os.path.join(docs, "routes", "ICN-KIX.html"))
        self.assertIn("종료된 달", page)
        self.assertIn("(종료)", rt(config.DASHBOARD_FILE))

    def test_route_page_chart_uses_only_its_own_departure_month(self):
        self._call_main()
        oct_route = [r for r in routes.load_routes(Clock.now.date()) if r.slug == "ICN-KIX"][0]
        rp = report.build_report(oct_route, config)
        # Probe 11월 창에서 10월 이력 최저보다 훨씬 싼 11월 출발 관측이 있다고 가정 (가짜)
        probe_path = os.path.join(config.PROBE_DIR, "ICN-KIX.csv")
        os.makedirs(os.path.dirname(probe_path), exist_ok=True)
        import storage
        storage.append_records(probe_path, [PriceRecord("2026-11-10", "2026-11-11", 50000, "KRW", "google_flights",
                                                        Clock.now.replace(hour=17, microsecond=0).isoformat(),
                                                        "round_trip_total")], "ICN", "KIX")
        rp = report.build_report(oct_route, config)
        html = build_dashboard.history_block(rp.hist, rp)
        self.assertNotIn("50000", html.replace(",", ""))
        caption = re.search(r"출처: ([^)]*)\)", html).group(1)
        self.assertNotIn("probe", caption)                      # 11월 행만 있는 probe 는 10월 그래프 출처에서 빠짐
        self.assertIn("tracker", caption)


if __name__ == "__main__":
    unittest.main()
