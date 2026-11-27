"""첫 화면: 데이터 정리(analyzer/offers.py)와 선별 규칙(dashboard_home/view.js). 사이트 접속 없음."""
import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from urllib.parse import quote

import build_dashboard
import build_home
import config
from analyzer import offers, report
from collectors.google_flights import GoogleFlightsCollector
from routes import load_routes

KST = offers.KST
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=KST)
HEAD = ["collected_at", "origin", "destination", "departure_date", "return_date", "nights", "price", "currency",
        "price_type", "source", "source_tag"]
KEYS = ["HISTORY_DIR", "PROBE_DIR", "PROBE_FEED_DIR", "PROBE_PREROLL_DIR", "LAST_RUN_FILE", "OUTLIERS_FILE"]
NODE = shutil.which("node")
VIEW_JS = os.path.join(build_home.ASSETS, "view.js")


def row(seen, dest, dep, ret, price, origin="ICN", currency="KRW"):
    n = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
    return [seen, origin, dest, dep, ret, n, price, currency, "round_trip_total", "google_flights", ""]


class DataTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(config, k) for k in KEYS}
        self.tmp = tempfile.mkdtemp()
        config.HISTORY_DIR = os.path.join(self.tmp, "history")
        config.PROBE_DIR = os.path.join(self.tmp, "probes")
        config.PROBE_FEED_DIR = os.path.join(self.tmp, "probes", "feed")
        config.PROBE_PREROLL_DIR = os.path.join(self.tmp, "probes", "preroll")
        config.LAST_RUN_FILE = os.path.join(self.tmp, "last_run.json")
        config.OUTLIERS_FILE = os.path.join(self.tmp, "outliers.csv")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mark(self, seen, dest="KIX"):
        with open(config.OUTLIERS_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["collected_at", "origin", "destination", "reason", "marked_on"])
            w.writerow([seen, "ICN", dest, "test", "2026-11-27"])

    def test_marked_run_is_left_out_of_prices_and_trail_but_listed_and_files_are_untouched(self):
        bad = "2026-11-26T18:35:47+09:00"
        self.write("history/ICN-KIX-2026-12.csv", [
            row("2026-11-24T17:00:00+09:00", "KIX", "2026-12-06", "2026-12-09", 270000),
            row("2026-11-25T17:00:00+09:00", "KIX", "2026-12-06", "2026-12-09", 275000)])
        self.write("probes/ICN-KIX.csv", [row(bad, "KIX", "2026-12-06", "2026-12-09", 83000)])
        path = os.path.join(self.tmp, "probes", "ICN-KIX.csv")
        with open(path, "rb") as f:
            original = f.read()
        before, = self.offer(offers.build_data(config, NOW), "KIX", "2026-12-06", "2026-12-09")
        self.assertEqual(before["p"], 83000)                          # 표시하기 전에는 최신값
        self.mark(bad)
        data = offers.build_data(config, NOW)
        o, = self.offer(data, "KIX", "2026-12-06", "2026-12-09")
        self.assertEqual((o["p"], o["pp"], o.get("bp")), (275000, 270000, None))
        self.assertEqual(o.get("vx"), 1)                              # 판정 비교 기록에 이상값이 섞일 수 있음
        self.assertEqual([f["p"] for f in data["flagged"]], [83000])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original)

    def write(self, rel, rows):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEAD)
            w.writerows(rows)

    def offer(self, data, dest, dep, ret):
        return [o for o in data["offers"] if (o["d"], o["dep"], o["ret"]) == (dest, dep, ret)]

    def test_latest_observation_wins_across_sources_and_previous_is_an_earlier_day(self):
        self.write("history/ICN-KIX-2026-12.csv", [
            row("2026-11-24T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 300000),
            row("2026-11-26T09:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 290000),
            row("2026-11-26T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 280000)])
        self.write("probes/ICN-KIX.csv", [row("2026-11-26T19:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 310000)])
        o, = self.offer(offers.build_data(config, NOW), "KIX", "2026-12-04", "2026-12-07")
        self.assertEqual((o["p"], o["t"], o["s"]), (310000, "2026-11-26T19:00:00+09:00", "probe"))
        self.assertEqual((o["pp"], o["pt"]), (300000, "2026-11-24T17:00:00+09:00"))   # 같은 날 값은 직전이 아님

    def test_previous_older_than_a_week_past_departures_preroll_and_bad_rows_are_ignored(self):
        self.write("history/ICN-KIX-2026-12.csv", [
            row("2026-11-10T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 300000),
            row("2026-11-26T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 280000),
            row("2026-11-20T17:00:00+09:00", "KIX", "2026-11-25", "2026-11-28", 100000),
            row("2026-11-26T17:00:00+09:00", "KIX", "2026-12-05", "2026-12-07", 1000, currency="USD")])
        self.write("probes/preroll/ICN-KIX.csv", [row("2026-11-26T19:00:00+09:00", "KIX", "2027-01-12", "2027-01-15", 1)])
        data = offers.build_data(config, NOW)
        self.assertEqual([(o["dep"], o["p"]) for o in data["offers"]], [("2026-12-04", 280000)])
        self.assertNotIn("pp", data["offers"][0])
        self.assertEqual(data["skipped_rows"], 1)

    def test_only_the_same_itinerary_is_compared(self):
        self.write("history/ICN-KIX-2026-12.csv", [
            row("2026-11-24T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-08", 900000),   # 귀국일이 다른 일정
            row("2026-11-25T17:00:00+09:00", "KIX", "2026-12-05", "2026-12-07", 900000),   # 출발일이 다른 일정
            row("2026-11-25T18:00:00+09:00", "FUK", "2026-12-04", "2026-12-07", 900000),   # 목적지가 다른 일정
            row("2026-11-26T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 200000)])
        o, = self.offer(offers.build_data(config, NOW), "KIX", "2026-12-04", "2026-12-07")
        self.assertNotIn("pp", o)
        self.assertNotIn("bp", o)

    def test_three_most_recent_check_days_within_a_week_of_each_other(self):
        self.write("history/ICN-KIX-2026-12.csv", [
            row("2026-11-10T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 111111),   # 8일 넘게 떨어짐: 끊김
            row("2026-11-20T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 400000),
            row("2026-11-24T09:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 999999),   # 같은 날 이른 값은 무시
            row("2026-11-24T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 300000),
            row("2026-11-26T17:00:00+09:00", "KIX", "2026-12-04", "2026-12-07", 290000)])
        o, = self.offer(offers.build_data(config, NOW), "KIX", "2026-12-04", "2026-12-07")
        self.assertEqual((o["p"], o["pp"], o["bp"]), (290000, 300000, 400000))

    def test_drop_follow_up_counts_how_often_a_drop_went_back(self):
        def trip(*prices):
            return [(datetime(2026, 11, 1 + i, 17, tzinfo=KST), p, "tracker") for i, p in enumerate(prices)]
        got = offers.drop_follow_up({"a": trip(100, 80, 100), "b": trip(100, 80, 80, 81), "c": trip(100, 80, 81, 99)})
        self.assertEqual(got["once"], {"n": 3, "back": 1})      # a 는 돌아감, b/c 의 첫 하락은 다음에도 낮음
        self.assertEqual(got["twice"], {"n": 2, "back": 1})     # b 는 낮게 유지, c 는 돌아감

    def test_targets_and_attempts_come_from_the_recorded_json(self):
        os.makedirs(config.PROBE_FEED_DIR)
        with open(os.path.join(config.PROBE_FEED_DIR, "latest.json"), "w", encoding="utf-8") as f:
            json.dump({"origin": "ICN", "run_at": "2026-11-26T19:42:00+09:00", "blocked": False,
                       "anchor": {"dep": "2026-12-08", "ret": "2026-12-11", "pairs": 36},
                       "probes": [{"code": "HND", "records": 0}]}, f)
        with open(config.LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump({"routes": [{"destination": "KIX", "target_month": "2026-11", "blocked": False,
                                   "started_at": "2026-11-26T17:13:00+09:00", "finished_at": "2026-11-26T17:18:00+09:00",
                                   "records_collected": 0,
                                   "missing_pairs": ["2026-11-02->2026-11-03", "2026-11-28->2026-11-29"]}]}, f)
        data = offers.build_data(config, NOW)
        self.assertEqual(len(data["targets"]["HND"]), 36)
        self.assertEqual(len(data["targets"]["KIX"]), 6 + 189)          # 11월은 11/27~29 출발 6개(귀국 11월 안), 12월 189개
        self.assertIn({"kind": "tracker", "d": "KIX", "month": "2026-11", "at": "2026-11-26T17:18:00+09:00",
                       "got": 0, "expected": 1, "blocked": False}, data["attempts"])    # 지난 출발일은 세지 않음
        self.assertIn({"kind": "probe", "d": "HND", "month": None, "at": "2026-11-26T19:42:00+09:00",
                       "got": 0, "expected": 36, "blocked": False}, data["attempts"])
        self.assertEqual(data["offers"], [])

    def test_search_link_matches_the_collector_url(self):
        data = offers.build_data(config, NOW)
        mine = data["search_url"].replace("{q}", quote("Flights from ICN to KIX on 2026-12-04 through 2026-12-07"))
        theirs = GoogleFlightsCollector(currency=config.CURRENCY, language=config.LANGUAGE)._build_url(
            "ICN", "KIX", date(2026, 12, 4), date(2026, 12, 7))
        self.assertEqual(mine, theirs)

    def test_page_embeds_data_safely(self):
        page = build_home.render({"generated_at": NOW.isoformat(), "airports": {"KIX": "</script><b>x"}, "offers": [],
                                  "targets": {}, "attempts": [], "search_url": "{q}"})
        self.assertNotIn("</script><b>", page)
        self.assertNotIn("__DATA__", page)
        self.assertNotIn("__VIEW_JS__", page)


class BuildDashboardTests(unittest.TestCase):
    """기존 build_dashboard.py 가 새 첫 화면을 만들고, 이전 화면들을 보존하는지."""
    KEYS = KEYS + ["DASHBOARD_FILE", "PUBLIC_THROUGH"]

    def test_marked_run_never_looks_like_a_normal_low_and_the_verdict_is_unchanged(self):
        path = os.path.join(config.HISTORY_DIR, f"ICN-KIX-{self.month}.csv")
        bad = (datetime.now(KST) - timedelta(days=2)).replace(microsecond=0).isoformat()
        os.makedirs(os.path.join(config.PROBE_DIR), exist_ok=True)
        with open(os.path.join(config.PROBE_DIR, "ICN-KIX.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEAD)
            w.writerow(row(bad, "KIX", self.dep.isoformat(), (self.dep + timedelta(days=3)).isoformat(), 83000))
        route = [r for r in load_routes(datetime.now(KST).date()) if r.history_file == path][0]
        unmarked = report.build_report(route, config)
        self.assertEqual(unmarked.s_all.low, 83000)
        with open(config.OUTLIERS_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["collected_at", "origin", "destination"])
            w.writerow([bad, "ICN", "KIX"])
        marked = report.build_report(route, config)
        self.assertEqual((marked.s_all.low, marked.s30.low), (300000, 300000))
        self.assertEqual(marked.hist.observed_min(lambda r: True)["price"], 300000)      # 그래프 이력에서도 제외
        self.assertEqual((marked.verdict.label, marked.verdict.reasons), (unmarked.verdict.label, unmarked.verdict.reasons))
        self.assertEqual(len(marked.flagged), 1)
        self.build()
        page = self.read("routes", os.path.basename(route.page_file))
        self.assertIn("수집 이상값으로 표시한 관측", page)
        self.assertIn("저장 행 1개", page)
        for hit in re.finditer("83,000원", page):          # 나오는 곳마다 이상값이라는 표시가 함께 있어야 함
            around = page[max(0, hit.start() - 120):hit.end() + 12]
            self.assertTrue("[이상값]" in around or "이상값으로 표시" in around, around)
        self.assertIn("비교 기록에 이상값 포함 가능", self.read("history.html"))

    def setUp(self):
        self.saved = {k: getattr(config, k) for k in self.KEYS}
        self.tmp = tempfile.mkdtemp()
        config.HISTORY_DIR = os.path.join(self.tmp, "history")
        config.PROBE_DIR = os.path.join(self.tmp, "probes")
        config.PROBE_FEED_DIR = os.path.join(self.tmp, "probes", "feed")
        config.LAST_RUN_FILE = os.path.join(self.tmp, "last_run.json")
        config.DASHBOARD_FILE = os.path.join(self.tmp, "docs", "index.html")
        config.OUTLIERS_FILE = os.path.join(self.tmp, "outliers.csv")
        config.PUBLIC_THROUGH = None
        today = datetime.now(KST).date()
        self.dep = today.replace(day=1) + timedelta(days=40)       # 다음 달 어느 날
        os.makedirs(config.HISTORY_DIR)
        self.month = f"{self.dep.year:04d}-{self.dep.month:02d}"
        with open(os.path.join(config.HISTORY_DIR, f"ICN-KIX-{self.month}.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEAD)
            w.writerow(row(datetime.now(KST).replace(microsecond=0).isoformat(), "KIX", self.dep.isoformat(),
                           (self.dep + timedelta(days=3)).isoformat(), 300000))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build_dashboard.main()
        return code, out.getvalue()

    def read(self, *parts):
        with open(os.path.join(self.tmp, "docs", *parts), encoding="utf-8") as f:
            return f.read()

    def test_new_home_links_to_the_preserved_pages(self):
        code, _ = self.build()
        self.assertEqual(code, 0)
        home = self.read("index.html")
        self.assertIn("오늘의 추천", home)
        self.assertIn(f"routes/ICN-KIX-{self.month}.html", home)
        self.assertIn('"history_page":true', home)
        self.assertIn(f"routes/ICN-KIX-{self.month}.html", self.read("history.html"))
        detail = self.read("routes", f"ICN-KIX-{self.month}.html")
        self.assertIn("마지막 수집 때 최저가", detail)
        self.assertNotIn("현재 최저가", detail)
        self.assertIn("검증 전", detail)
        self.assertIn('href="../index.html"', detail)

    def test_a_failure_in_the_new_home_never_fails_the_build(self):
        original = build_home.render
        build_home.render = lambda data: 1 / 0
        try:
            code, out = self.build()
        finally:
            build_home.render = original
        self.assertEqual(code, 0)                                   # 수집 데이터 커밋이 막히면 안 됨
        self.assertIn("::warning::", out)
        self.assertIn("Tracked Routes", self.read("index.html"))    # 이전 첫 화면으로 대신함


@unittest.skipUnless(NODE, "node 가 없으면 화면 선별 규칙 시험은 건너뜁니다")
class ViewRuleTests(unittest.TestCase):
    def view(self, offer_list, targets=None, now=NOW, airports=("KIX", "FUK", "HND")):
        data = {"generated_at": now.isoformat(), "airports": {c: c for c in airports}, "offers": offer_list,
                "targets": targets or {}, "attempts": [], "search_url": "{q}"}
        script = ("const m=require(process.argv[1]);const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "process.stdout.write(JSON.stringify(m.buildView(i.data,i.now)))")
        out = subprocess.run([NODE, "-e", script, VIEW_JS], capture_output=True, text=True, encoding="utf-8", check=True,
                             input=json.dumps({"data": data, "now": int(now.timestamp() * 1000)}))
        return json.loads(out.stdout)

    @staticmethod
    def o(dest, price, hours_ago, dep="2026-12-04", ret="2026-12-07", prev=None, base=None, verdict=None):
        item = {"o": "ICN", "d": dest, "dep": dep, "ret": ret, "p": price,
                "t": (NOW - timedelta(hours=hours_ago)).isoformat(), "s": "tracker"}
        if prev:
            item["pp"], item["pt"] = prev, (NOW - timedelta(hours=hours_ago + 24)).isoformat()
        if base:
            item["bp"], item["bt"] = base, (NOW - timedelta(hours=hours_ago + 48)).isoformat()
        if verdict:
            item["v"] = verdict
        return item

    def picked(self, view):
        return [(x["d"], x["p"]) for x in view["drops"] + view["weekend"]]

    def test_recommendations_use_only_prices_confirmed_within_one_day(self):
        v = self.view([self.o("KIX", 100000, 24.5, prev=100000, base=200000, verdict="DEAL"),   # 24시간 넘음: 제외
                       self.o("FUK", 300000, 23.5)])
        self.assertEqual(self.picked(v), [("FUK", 300000)])
        self.assertEqual(v["freshCount"], 1)

    def test_current_lowest_excludes_prices_older_than_three_days(self):
        v = self.view([self.o("KIX", 100000, 73, dep="2026-12-05"), self.o("KIX", 250000, 71),
                       self.o("HND", 90000, 200)])
        dests = {d["code"]: d for d in v["dests"]}
        self.assertEqual(dests["KIX"]["best"]["p"], 250000)
        self.assertEqual(dests["KIX"]["best"]["age"], "recent")
        self.assertIsNone(dests["HND"]["best"])
        self.assertEqual(dests["HND"]["reference"]["p"], 90000)       # 참고용으로만
        self.assertIsNone(dests["FUK"]["best"])
        self.assertIsNone(dests["FUK"]["reference"])                  # 데이터 없음
        self.assertEqual([d["code"] for d in v["dests"]], ["KIX", "FUK", "HND"])

    def test_age_is_measured_when_the_page_is_opened_not_when_it_was_built(self):
        item = [self.o("KIX", 300000, 1)]
        later = self.view(item, now=NOW + timedelta(days=4))
        self.assertEqual(self.picked(later), [])
        self.assertIsNone(later["dests"][0]["best"])

    def test_verdict_label_does_not_change_selection_or_order(self):
        base = [self.o("KIX", 300000, 2), self.o("FUK", 200000, 2)]
        marked = [dict(base[0], v="DEAL"), dict(base[1], v="HOLD")]
        self.assertEqual(self.picked(self.view(base)), self.picked(self.view(marked)))
        self.assertEqual([x["d"] for x in self.view(marked)["weekend"]], ["FUK", "KIX"])

    def test_drop_needs_two_low_checks_and_must_not_be_expensive_for_that_destination(self):
        others = [self.o("KIX", p, 2, dep="2026-12-0%d" % i, ret="2026-12-1%d" % i)
                  for i, p in ((1, 200000), (2, 220000), (3, 240000))]
        v = self.view(others + [
            self.o("KIX", 400000, 2, "2026-12-08", "2026-12-10", prev=410000, base=800000),     # 두 번 확인, 비쌈
            self.o("KIX", 150000, 2, "2026-12-09", "2026-12-11", prev=300000, base=290000),     # 한 번만 확인
            self.o("KIX", 160000, 2, "2026-12-14", "2026-12-16", prev=260000),                  # 비교할 그 전 값 없음
            self.o("KIX", 215000, 2, "2026-12-15", "2026-12-17", prev=214000, base=220000),     # 5% 미만
            self.o("KIX", 190000, 2, "2026-12-10", "2026-12-12", prev=195000, base=230000)])    # 추천 대상
        self.assertEqual([x["p"] for x in v["drops"]], [190000])
        self.assertAlmostEqual(v["drops"][0]["drop"], (230000 - 190000) / 230000 * 100)
        left = {g["key"]: g for g in v["excluded"]}
        self.assertEqual([x["p"] for x in left["pricey"]["examples"]], [400000])
        self.assertEqual(sorted(x["p"] for x in left["single"]["examples"]), [150000, 160000])

    def test_implausibly_low_price_seen_once_is_kept_out_but_listed(self):
        normal = [self.o("KIX", p, 2, dep="2026-12-0%d" % i, ret="2026-12-1%d" % i)
                  for i, p in ((1, 290000), (2, 300000), (3, 310000))]
        v = self.view(normal + [
            self.o("KIX", 83000, 2, "2026-12-04", "2026-12-07", prev=260000),       # 평소의 30%, 직전은 정상가
            self.o("KIX", 90000, 2, "2026-12-11", "2026-12-14"),                    # 평소의 30%, 처음 확인
            self.o("KIX", 170000, 2, "2026-12-18", "2026-12-21", prev=172000)])     # 낮지만 직전에도 같은 수준
        self.assertEqual(v["dests"][0]["best"]["p"], 170000)
        self.assertNotIn(83000, [x["p"] for x in v["drops"] + v["weekend"]])
        self.assertNotIn(90000, [x["p"] for x in v["drops"] + v["weekend"]])
        left = {g["key"]: g for g in v["excluded"]}
        self.assertEqual([x["p"] for x in left["suspect"]["examples"]], [83000, 90000])

    def test_excluded_prices_are_listed_with_their_reason(self):
        v = self.view([self.o("KIX", 100000, 100), self.o("KIX", 200000, 50, "2026-12-05", "2026-12-07"),
                       self.o("KIX", 300000, 2, "2026-11-27", "2026-11-29"),
                       self.o("KIX", 400000, 2, "2026-12-11", "2026-12-14")])
        self.assertEqual({g["key"]: g["count"] for g in v["excluded"]},
                         {"flagged": 0, "suspect": 0, "old": 1, "recent": 1, "single": 0, "pricey": 0, "today": 1})
        self.assertEqual([x["p"] for x in v["weekend"]], [400000])

    def test_weekend_rule_today_departures_and_one_card_per_destination(self):
        v = self.view([self.o("KIX", 100000, 2, dep="2026-11-27", ret="2026-11-29"),   # 오늘 출발: 제외
                       self.o("KIX", 150000, 2, dep="2026-12-07", ret="2026-12-09"),   # 월요일 출발
                       self.o("KIX", 160000, 2, dep="2026-12-04", ret="2026-12-11"),   # 7박
                       self.o("KIX", 300000, 2, dep="2026-12-04", ret="2026-12-07"),
                       self.o("KIX", 320000, 2, dep="2026-12-05", ret="2026-12-08")])
        self.assertEqual(self.picked(v), [("KIX", 300000)])
        self.assertEqual(v["dests"][0]["best"]["p"], 150000)

    def test_status_separates_missing_data_from_missed_collection(self):
        targets = {"KIX": [["2026-12-01", "2026-12-02"], ["2026-12-02", "2026-12-03"], ["2026-12-03", "2026-12-04"],
                           ["2026-12-04", "2026-12-05"], ["2026-11-27", "2026-11-28"]]}
        v = self.view([self.o("KIX", 1000, 2, "2026-12-01", "2026-12-02"), self.o("KIX", 1000, 50, "2026-12-02", "2026-12-03"),
                       self.o("KIX", 1000, 100, "2026-12-03", "2026-12-04")], targets)
        s = {x["code"]: x for x in v["status"]}["KIX"]
        self.assertEqual((s["total"], s["fresh"], s["recent"], s["old"], s["none"]), (4, 1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
