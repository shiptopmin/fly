"""
tests/test_preroll.py - 사전 측정(preroll) 회귀 테스트

실행:
    python -m unittest tests.test_preroll -v

실제 사이트에는 접속하지 않습니다. 수집기는 가짜로 바꾸고, 모든 파일은 임시 폴더에만 씁니다.
가격은 모두 가짜입니다.
"""
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

import config
import probe
import storage
from analyzer import deals
from analyzer.combinations import load_rows
from collectors.base import CollectResult, PriceRecord
from collectors.google_flights import GoogleFlightsCollector

KST = probe.KST
PATH_KEYS = ["HISTORY_DIR", "PROBE_DIR", "PROBE_FEED_DIR", "PROBE_PREROLL_DIR", "PROBE_PREROLL_STATUS",
             "SEMANTICS_LOG", "PROBE_PREROLL_DAYS", "PAGE_LOAD_DELAY_SEC"]


class Clock:
    """가짜 시계. now 를 바꾸면 probe.datetime.now() 와 가짜 수집기의 collected_at 이 함께 바뀝니다."""
    now = datetime(2026, 10, 24, 17, 30, 0, tzinfo=KST)


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now


class FakeCollector(GoogleFlightsCollector):
    """사이트에 접속하지 않는 수집기. behavior(query, n) 로 상황을 만듭니다."""
    behavior = None
    calls = []

    def __init__(self, *a, **k):
        pass

    def collect_query(self, query, headless):
        n = len(FakeCollector.calls)
        FakeCollector.calls.append(query)
        beh = (FakeCollector.behavior or (lambda q, i: {}))(query, i := n) or {}
        if beh.get("raise"):
            raise RuntimeError(beh["raise"])
        res = CollectResult()
        stamp = (Clock.now + timedelta(seconds=n)).replace(microsecond=0).isoformat()
        drop = beh.get("drop", 0)
        pairs = query.needed_pairs()
        for k, (d, r) in enumerate(pairs):
            if k < drop:
                continue
            res.records.append(PriceRecord(
                departure_date=d.isoformat(), return_date=r.isoformat(),
                price=200000 + 1000 * k + 10 * query.depart_from.day, currency="KRW",
                source="google_flights", collected_at=stamp, price_type="round_trip_total"))
        res.page_loads = beh.get("loads", 1)
        res.semantics_verified = beh.get("verified", True)
        res.semantics_checks = [{"ok": not beh.get("mismatch", False)}]
        res.errors = list(beh.get("errors", []))
        res.blocked = beh.get("blocked", False)
        if res.blocked:
            res.errors.append("[ERROR] CAPTCHA detected")
        return res


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(config, k) for k in PATH_KEYS}
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        config.HISTORY_DIR = os.path.join(t, "history")
        config.PROBE_DIR = os.path.join(t, "probes")
        config.PROBE_FEED_DIR = os.path.join(t, "probes", "feed")
        config.PROBE_PREROLL_DIR = os.path.join(t, "probes", "preroll")
        config.PROBE_PREROLL_STATUS = os.path.join(t, "probes", "preroll", "status.csv")
        config.SEMANTICS_LOG = os.path.join(t, "diag", "semantics.csv")
        config.PROBE_PREROLL_DAYS = 8
        config.PAGE_LOAD_DELAY_SEC = 0
        self._dt, self._gfc = probe.datetime, probe.GoogleFlightsCollector
        probe.datetime, probe.GoogleFlightsCollector = FakeDT, FakeCollector
        FakeCollector.behavior, FakeCollector.calls = None, []
        Clock.now = datetime(2026, 10, 24, 17, 30, 0, tzinfo=KST)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        probe.datetime, probe.GoogleFlightsCollector = self._dt, self._gfc
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, *argv):
        old = sys.argv
        sys.argv = ["probe.py", "--no-confirm", *argv]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = probe.main()
        finally:
            sys.argv = old
        return code, buf.getvalue()

    def feed(self):
        with open(os.path.join(config.PROBE_FEED_DIR, "latest.json"), encoding="utf-8") as f:
            return json.load(f)

    def tree(self):
        """임시 폴더의 모든 파일 -> 내용 (바이트)."""
        out = {}
        for root, _, files in os.walk(self.tmp):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, self.tmp)] = f.read()
        return out


# ----------------------------------------------------------------------
# 1. 기준일과 켜지는 날짜
# ----------------------------------------------------------------------
class AnchorTests(unittest.TestCase):
    def test_boundaries(self):
        """8일 설정의 경계 (설정값과 무관하게 8일을 명시해서 검증)."""
        class C8:
            PROBE_PREROLL_DAYS = 8
        self.assertFalse(probe.preroll_active(date(2026, 10, 23), C8))
        self.assertTrue(probe.preroll_active(date(2026, 10, 24), C8))
        self.assertTrue(probe.preroll_active(date(2026, 10, 31), C8))
        self.assertFalse(probe.preroll_active(date(2026, 11, 1), C8))

    def test_boundaries_follow_configured_days(self):
        """실제 설정값 PROBE_PREROLL_DAYS(N) 에 맞춰: 11/1 의 N일 전부터 월말까지 켜지고, 11/1 에는 꺼진다."""
        n = config.PROBE_PREROLL_DAYS
        first = date(2026, 11, 1) - timedelta(days=n)
        self.assertFalse(probe.preroll_active(first - timedelta(days=1)))
        self.assertTrue(probe.preroll_active(first))
        self.assertTrue(probe.preroll_active(date(2026, 10, 31)))
        self.assertFalse(probe.preroll_active(date(2026, 11, 1)))

    def test_off_switch(self):
        class Cfg:
            PROBE_PREROLL_DAYS = 0
        self.assertFalse(probe.preroll_active(date(2026, 10, 31), Cfg))

    def _check_every_month(self, days):
        """2026~2028 모든 날짜: 켜진 날의 사전 측정 기준일 == 다음 달 1일 본 측정 기준일, 달마다 정확히 days 일."""
        class Cfg:
            PROBE_PREROLL_DAYS = days
        per_month = {}
        d = date(2026, 1, 1)
        while d <= date(2028, 12, 31):
            if probe.preroll_active(d, Cfg):
                per_month[(d.year, d.month)] = per_month.get((d.year, d.month), 0) + 1
                nxt = probe.add_months(d, 1)
                self.assertEqual(probe.probe_anchor(d, extra_months=1), probe.probe_anchor(nxt), d)
            d += timedelta(days=1)
        self.assertEqual(set(per_month.values()), {days})
        self.assertEqual(len(per_month), 36)

    def test_every_month_has_8_days_and_anchor_continues(self):
        """8일 설정 (설정값과 무관하게 8일을 명시해서 검증)."""
        self._check_every_month(8)

    def test_every_month_matches_configured_days(self):
        """실제 설정값(현재 10)으로도 같은 검증: 달마다 정확히 N일, 기준일 연속성 유지."""
        self._check_every_month(config.PROBE_PREROLL_DAYS)

    def test_default_anchor_unchanged(self):
        self.assertEqual(probe.probe_anchor(date(2026, 10, 24)), (date(2026, 11, 10), date(2026, 11, 13)))
        self.assertEqual(probe.probe_anchor(date(2026, 10, 24), extra_months=1),
                         (date(2026, 12, 8), date(2026, 12, 11)))


# ----------------------------------------------------------------------
# 2. 완료 판정: 행 수만이 아니라 하나하나 확인
# ----------------------------------------------------------------------
def good_rows(today=date(2026, 10, 24), code="KIX"):
    dep, ret = probe.probe_anchor(today, extra_months=1)
    pairs = probe.probe_query("ICN", code, dep, ret).needed_pairs()
    stamp = datetime(2026, 10, 24, 17, 30, tzinfo=KST).isoformat()
    rows = [{"collected_at": stamp, "origin": "ICN", "destination": code, "departure_date": d.isoformat(),
             "return_date": r.isoformat(), "nights": str((r - d).days), "price": str(200000 + 1000 * i),
             "currency": "KRW", "price_type": "round_trip_total", "source": "google_flights", "source_tag": ""}
            for i, (d, r) in enumerate(pairs)]
    return rows, set(pairs)


class CheckRowsTests(unittest.TestCase):
    def check(self, rows, pairs, today=date(2026, 10, 24)):
        return probe.check_preroll_rows(rows, pairs, "ICN", "KIX", today)

    def test_good(self):
        rows, pairs = good_rows()
        self.assertEqual(len(rows), 36)
        problems, counts = self.check(rows, pairs)
        self.assertEqual(problems, [])
        self.assertEqual(counts, {1: 5, 2: 6, 3: 7, 4: 6, 5: 5, 6: 4, 7: 3})

    def test_each_defect_is_not_complete(self):
        rows, pairs = good_rows()
        cases = {
            "행 없음": [],
            "하나 부족": rows[1:],
            "중복": rows + [rows[0]],
            "수집 시각 2종": [dict(r, collected_at=r["collected_at"][:-1] + "9") if i == 0 else r
                              for i, r in enumerate(rows)],
            "가격 0": [dict(r, price="0") if i == 3 else r for i, r in enumerate(rows)],
            "통화 다름": [dict(r, currency="USD") if i == 3 else r for i, r in enumerate(rows)],
            "가격 의미 다름": [dict(r, price_type="one_way") if i == 3 else r for i, r in enumerate(rows)],
            "숙박일수 불일치": [dict(r, nights="9") if i == 3 else r for i, r in enumerate(rows)],
            "노선 다름": [dict(r, destination="FUK") if i == 3 else r for i, r in enumerate(rows)],
            "초과 조합": rows[:-1] + [dict(rows[-1], departure_date="2026-12-20", return_date="2026-12-21",
                                         nights="1")],
        }
        for name, bad in cases.items():
            problems, _ = self.check(bad, pairs)
            self.assertTrue(problems, name)

    def test_wrong_day_is_not_complete(self):
        rows, pairs = good_rows()
        problems, _ = self.check(rows, pairs, today=date(2026, 10, 25))
        self.assertTrue(problems)

    def test_nights_count_mismatch_detected_even_if_total_is_36(self):
        """총 36행이라도 숙박일수별 분포가 다르면 정상이 아닙니다 (같은 조합 수가 아니라 내용을 봅니다)."""
        rows, pairs = good_rows()
        bad = list(rows)
        a, b = bad[0], bad[1]          # 같은 36행 중 두 행을 서로 다른 (존재하지 않는) 조합으로 바꿈
        bad[0] = dict(a, departure_date="2026-12-01", return_date="2026-12-02", nights="1")
        bad[1] = dict(b, departure_date="2026-12-01", return_date="2026-12-03", nights="2")
        self.assertEqual(len(bad), 36)
        problems, _ = self.check(bad, pairs)
        self.assertTrue(problems)


# ----------------------------------------------------------------------
# 3. 사전 측정 실행: 저장, 완료 기록, 재실행 건너뛰기, 실패 처리
# ----------------------------------------------------------------------
class RunPrerollTests(TempEnv):
    def airports(self, *codes):
        from destinations import resolve
        return resolve(",".join(codes))

    def run_pre(self, codes=("KIX", "FUK")):
        return probe.run_preroll(FakeCollector(), self.airports(*codes), "ICN", Clock.now.date(),
                                 Clock.now.isoformat(), "local", say=lambda *_: None, sleep=lambda *_: None)

    def status_rows(self):
        return load_rows(config.PROBE_PREROLL_STATUS)

    def test_complete_writes_only_preroll_files(self):
        out = self.run_pre()
        self.assertTrue(out["all_complete"])
        self.assertEqual([d["status"] for d in out["destinations"]], ["complete", "complete"])
        files = sorted(self.tree())
        self.assertEqual(files, sorted([os.path.join("diag", "semantics.csv"),
                                        os.path.join("probes", "preroll", "ICN-KIX.csv"),
                                        os.path.join("probes", "preroll", "ICN-FUK.csv"),
                                        os.path.join("probes", "preroll", "status.csv")]))
        st = self.status_rows()
        self.assertEqual([s["complete"] for s in st], ["1", "1"])
        self.assertEqual(st[0]["nights_counts"], "1:5|2:6|3:7|4:6|5:5|6:4|7:3")
        rows = load_rows(probe.preroll_file("ICN", "KIX"))
        self.assertEqual(len(rows), 36)
        self.assertTrue(all(r["departure_date"].startswith("2026-12") for r in rows))

    def test_same_day_rerun_skips_complete_destinations(self):
        self.run_pre()
        before = self.tree()
        calls = len(FakeCollector.calls)
        out = self.run_pre()
        self.assertEqual([d["status"] for d in out["destinations"]], ["skipped_complete"] * 2)
        self.assertEqual(len(FakeCollector.calls), calls)        # 접속하지 않음
        self.assertEqual(self.tree(), before)                    # 파일 한 글자도 변하지 않음
        self.assertTrue(out["all_complete"])

    def test_incomplete_is_not_done_and_is_retried(self):
        FakeCollector.behavior = lambda q, i: {"drop": 3} if i == 0 else {}
        out = self.run_pre(("KIX",))
        self.assertEqual(out["destinations"][0]["status"], "incomplete")
        self.assertFalse(out["all_complete"])
        self.assertEqual(self.status_rows()[0]["complete"], "0")
        # 같은 날 재실행: 불완전했으므로 다시 잰다 -> 나중 정상 측정이 그날 값이 된다
        Clock.now += timedelta(minutes=5)
        out = self.run_pre(("KIX",))
        self.assertEqual(out["destinations"][0]["status"], "complete")
        self.assertTrue(probe.preroll_done("ICN", "KIX", Clock.now.date(), date(2026, 12, 8),
                                           set(probe.probe_query("ICN", "KIX", *probe.probe_anchor(
                                               Clock.now.date(), extra_months=1)).needed_pairs())))

    def test_each_failure_kind_is_incomplete(self):
        kinds = {"오류": {"errors": ["Timeout opening page"]}, "불일치": {"mismatch": True},
                 "교차검증 미완료": {"verified": False}, "로드 수": {"loads": 2}, "일부 누락": {"drop": 1}}
        for name, beh in kinds.items():
            with self.subTest(name):
                shutil.rmtree(os.path.join(self.tmp, "probes"), ignore_errors=True)
                FakeCollector.calls = []
                FakeCollector.behavior = lambda q, i, b=beh: b
                out = self.run_pre(("KIX",))
                self.assertEqual(out["destinations"][0]["status"], "incomplete")
                self.assertTrue(out["destinations"][0]["problems"])
                self.assertEqual(self.status_rows()[0]["complete"], "0")

    def test_blocked_stops_and_is_flagged(self):
        FakeCollector.behavior = lambda q, i: {"blocked": True, "drop": 36} if i == 1 else {}
        out = self.run_pre(("KIX", "FUK", "HND"))
        self.assertTrue(out["blocked"])
        self.assertEqual(len(FakeCollector.calls), 2)            # 세 번째 목적지는 접속하지 않음
        self.assertEqual([d["code"] for d in out["destinations"]], ["KIX", "FUK"])
        self.assertEqual(out["destinations"][1]["status"], "incomplete")

    def test_complete_mark_alone_is_not_trusted(self):
        """상태 기록에 complete=1 이 있어도 저장된 행이 깨졌으면 건너뛰지 않습니다."""
        self.run_pre(("KIX",))
        path = probe.preroll_file("ICN", "KIX")
        rows = load_rows(path)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=storage.FIELDS)
            w.writeheader()
            w.writerows(rows[:-1])                                # 한 행 손실
        out = self.run_pre(("KIX",))
        self.assertEqual(out["destinations"][0]["status"], "complete")   # 다시 재서 정상 완료
        self.assertEqual(len(FakeCollector.calls), 2)

    def test_later_incomplete_run_makes_day_not_done(self):
        self.run_pre(("KIX",))
        Clock.now += timedelta(minutes=5)
        FakeCollector.behavior = lambda q, i: {"drop": 5}
        # 수동으로 불완전한 측정을 같은 날 뒤에 붙인 상황 (정상 완료분은 이미 있지만 '마지막 측정'이 아님)
        res = FakeCollector().collect_query(probe.probe_query("ICN", "KIX", *probe.probe_anchor(
            Clock.now.date(), extra_months=1)), True)
        storage.append_records(probe.preroll_file("ICN", "KIX"), res.records, "ICN", "KIX")
        pairs = set(probe.probe_query("ICN", "KIX", *probe.probe_anchor(Clock.now.date(), extra_months=1)).needed_pairs())
        self.assertFalse(probe.preroll_done("ICN", "KIX", Clock.now.date(), date(2026, 12, 8), pairs))


# ----------------------------------------------------------------------
# 4. 유효 이력: 날짜가 7개 있다는 것과 판정에 쓸 수 있다는 것은 다르다
# ----------------------------------------------------------------------
def seed_preroll(code, day_list, drop_nights=(), stamp_hour=17):
    """day_list 의 각 날짜에 12월 창 36조합을 사전 측정 파일로 만든다 (가짜 가격). drop_nights 숙박은 그날 빠짐."""
    path = probe.preroll_file("ICN", code)
    for day in day_list:
        stamp = datetime(day.year, day.month, day.day, stamp_hour, 30, tzinfo=KST).isoformat()
        dep, ret = probe.probe_anchor(day, extra_months=1)
        recs = []
        for i, (d, r) in enumerate(probe.probe_query("ICN", code, dep, ret).needed_pairs()):
            if (r - d).days in drop_nights:
                continue
            recs.append(PriceRecord(d.isoformat(), r.isoformat(), 250000 + 500 * i, "KRW", "google_flights",
                                    stamp, "round_trip_total"))
        storage.append_records(path, recs, "ICN", code)


def main_row_set(code, day, price=240000):
    """day 의 본 측정(probe 파일) 36조합 — 월 전환 후 첫 판정일을 만들 때 쓴다."""
    stamp = datetime(day.year, day.month, day.day, 17, 30, tzinfo=KST).isoformat()
    dep, ret = probe.probe_anchor(day)
    recs = [PriceRecord(d.isoformat(), r.isoformat(), price + 500 * i, "KRW", "google_flights", stamp,
                        "round_trip_total")
            for i, (d, r) in enumerate(probe.probe_query("ICN", code, dep, ret).needed_pairs())]
    storage.append_records(probe.probe_file("ICN", code), recs, "ICN", code)
    return recs


class ValidHistoryTests(TempEnv):
    def judge_on_1101(self, code="KIX", nights=2):
        today = date(2026, 11, 1)
        hist = probe.history_for("ICN", code, today)
        as_of = datetime(2026, 11, 1, 17, 30, tzinfo=KST)
        dep, ret = date(2026, 12, 8), date(2026, 12, 8) + timedelta(days=nights)
        return deals.judge(200000, hist, as_of, nights=nights, dep=dep, ret=ret, rules=config.DEAL_RULES)

    def days(self, a, b):
        return [date(2026, 10, a) + timedelta(days=i) for i in range(b - a + 1)]

    def test_8_days_from_1024_is_judged_on_1101_for_every_nights(self):
        seed_preroll("KIX", self.days(24, 31))
        main_row_set("KIX", date(2026, 11, 1))
        for n in range(1, 8):
            v = self.judge_on_1101(nights=n)
            self.assertNotEqual(v.label, deals.LABEL_HOLD, n)
            self.assertEqual(v.metrics["base_days"], 8, n)

    def test_exactly_7_days_still_judged_6_days_is_hold(self):
        seed_preroll("KIX", self.days(25, 31))
        self.assertNotEqual(self.judge_on_1101().label, deals.LABEL_HOLD)
        shutil.rmtree(os.path.join(self.tmp, "probes"))
        seed_preroll("KIX", self.days(26, 31))
        v = self.judge_on_1101()
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.metrics["base_days"], 6)

    def test_one_failed_day_out_of_8_leaves_7_and_is_judged(self):
        seed_preroll("KIX", [d for d in self.days(24, 31) if d.day != 27])
        v = self.judge_on_1101()
        self.assertNotEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.metrics["base_days"], 7)

    def test_two_failed_days_is_hold_not_error(self):
        seed_preroll("KIX", [d for d in self.days(24, 31) if d.day not in (27, 29)])
        v = self.judge_on_1101()
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.metrics["base_days"], 6)

    def test_partial_grid_day_only_hurts_the_missing_nights(self):
        """7박 조합이 빠진 날은 7박 이력에서만 빠집니다. 날짜가 있다는 것과 그 숙박일수의 유효 이력은 다릅니다."""
        seed_preroll("KIX", [d for d in self.days(25, 31) if d.day != 28])          # 6일 정상
        seed_preroll("KIX", [date(2026, 10, 28)], drop_nights=(7,))                   # 28일은 7박만 빠짐
        self.assertNotEqual(self.judge_on_1101(nights=3).label, deals.LABEL_HOLD)
        self.assertEqual(self.judge_on_1101(nights=3).metrics["base_days"], 7)
        v7 = self.judge_on_1101(nights=7)
        self.assertEqual(v7.label, deals.LABEL_HOLD)
        self.assertEqual(v7.metrics["base_days"], 6)

    def test_same_file_would_destroy_history_so_separate_file_is_used(self):
        """같은 파일에 두 창을 같은 날 쌓으면 나중 실행만 인정됩니다 - 별도 파일을 쓰는 이유."""
        for day in self.days(24, 31):
            main_row_set("KIX", day)                                   # 본 측정(11월 창)
            stamp = datetime(day.year, day.month, day.day, 17, 40, tzinfo=KST).isoformat()
            dep, ret = probe.probe_anchor(day, extra_months=1)
            recs = [PriceRecord(d.isoformat(), r.isoformat(), 250000, "KRW", "google_flights", stamp,
                                "round_trip_total")
                    for d, r in probe.probe_query("ICN", "KIX", dep, ret).needed_pairs()]
            storage.append_records(probe.probe_file("ICN", "KIX"), recs, "ICN", "KIX")   # 같은 파일, 더 늦게
        hist = probe.history_for("ICN", "KIX", date(2026, 10, 31)).before(datetime(2026, 11, 1, tzinfo=KST))
        self.assertEqual(hist.nights(2, (2026, 11)).points, [])         # 11월 창의 하루하루가 사라짐

    def test_history_for_hides_next_window_until_it_becomes_main(self):
        seed_preroll("KIX", self.days(24, 31))
        h_oct = probe.history_for("ICN", "KIX", date(2026, 10, 31))
        self.assertEqual(h_oct.sources, {})                              # 아직 본 창이 아님: 근거 문구에도 안 나타남
        h_nov = probe.history_for("ICN", "KIX", date(2026, 11, 1))
        self.assertEqual(h_nov.sources, {"preroll": 8 * 36})


# ----------------------------------------------------------------------
# 5. probe.main() 통합: 기존 결과 불변, feed 분리, 종료 코드
# ----------------------------------------------------------------------
def seed_main_history(code_list, days):
    for code in code_list:
        for day in days:
            main_row_set(code, day, price=230000 + 700 * day.day)


class MainIntegrationTests(TempEnv):
    CODES = ["FUK", "HND", "KIX", "NRT", "CTS", "NGO", "OKA", "KOJ"]

    def seeded_run(self, preroll_days, now=datetime(2026, 10, 26, 17, 30, tzinfo=KST), behavior=None):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        config.PROBE_PREROLL_DAYS = preroll_days
        Clock.now = now
        FakeCollector.calls, FakeCollector.behavior = [], behavior
        seed_main_history(self.CODES, [date(2026, 10, d) for d in range(15, 26)])
        return self.run_main()

    def test_main_results_identical_with_and_without_preroll(self):
        code0, _ = self.seeded_run(0)
        off_tree, off_feed = self.tree(), self.feed()
        code1, out = self.seeded_run(8)
        on_tree, on_feed = self.tree(), self.feed()
        self.assertEqual((code0, code1), (0, 0))
        self.assertNotIn("preroll", off_feed)
        pre = on_feed.pop("preroll")
        self.assertTrue(pre["all_complete"])
        self.assertEqual(pre["anchor"]["dep"], "2026-12-08")
        self.assertEqual(on_feed, off_feed)                               # 기존 feed 값 전부 동일
        # 본 측정 CSV / 진단 로그 외의 변화는 preroll 폴더 안에만 있다
        changed = {k for k in on_tree if off_tree.get(k) != on_tree[k]}
        self.assertTrue(all("preroll" in k or k.endswith("semantics.csv") or k.endswith("latest.json")
                            or k.endswith("2026-10-26.json") for k in changed), changed)
        for code in self.CODES:
            rel = os.path.join("probes", f"ICN-{code}.csv")
            self.assertEqual(on_tree[rel], off_tree[rel], rel)           # 본 측정 파일은 바이트 단위로 동일

    def test_inactive_day_has_no_preroll_key_and_no_preroll_files(self):
        code, _ = self.seeded_run(8, now=datetime(2026, 10, 20, 17, 30, tzinfo=KST))
        self.assertEqual(code, 0)
        self.assertNotIn("preroll", self.feed())
        self.assertFalse(os.path.exists(config.PROBE_PREROLL_DIR))

    def test_preroll_blocked_fails_run_but_keeps_main_results(self):
        FakeCollector.behavior = None
        self.seeded_run(0)
        base_feed = self.feed()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        config.PROBE_PREROLL_DAYS = 8
        seed_main_history(self.CODES, [date(2026, 10, d) for d in range(15, 26)])
        FakeCollector.calls = []
        FakeCollector.behavior = lambda q, i: {"blocked": True, "drop": 36} if i == 8 + 2 else {}
        code, _ = self.run_main()
        feed = self.feed()
        self.assertEqual(code, 1)                                        # 워크플로 실패 표시
        self.assertTrue(feed["preroll"]["blocked"])
        pre = feed.pop("preroll")
        self.assertEqual(feed, base_feed)                                # 본 측정 결과는 그대로
        self.assertEqual(len(pre["destinations"]), 3)

    def test_preroll_exception_is_isolated(self):
        self.seeded_run(0)
        base_feed = self.feed()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        config.PROBE_PREROLL_DAYS = 8
        seed_main_history(self.CODES, [date(2026, 10, d) for d in range(15, 26)])
        FakeCollector.calls = []
        FakeCollector.behavior = lambda q, i: {"raise": "boom"} if i == 8 else {}
        code, out = self.run_main()
        feed = self.feed()
        self.assertEqual(code, 1)
        self.assertIn("boom", feed["preroll"]["error"])
        feed.pop("preroll")
        self.assertEqual(feed, base_feed)

    def test_main_blocked_skips_preroll(self):
        code, _ = self.seeded_run(8, behavior=lambda q, i: {"blocked": True, "drop": 36} if i == 0 else {})
        self.assertEqual(code, 1)
        self.assertIn("skipped", self.feed()["preroll"])
        self.assertFalse(os.path.exists(config.PROBE_PREROLL_DIR))

    def test_rerun_same_day_skips_completed_preroll(self):
        self.seeded_run(8)
        before = len(FakeCollector.calls)
        self.assertEqual(before, 16)                                      # 본 8 + 사전 8
        self.run_main()
        self.assertEqual(len(FakeCollector.calls) - before, 8)           # 본 측정만 다시, 사전 측정은 건너뜀
        self.assertEqual({d["status"] for d in self.feed()["preroll"]["destinations"]}, {"skipped_complete"})

    def test_today_flag_requires_dry_run(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_main("--today", "2026-10-24")


if __name__ == "__main__":
    unittest.main()
