"""
tests/test_deals.py - 판정 엔진(analyzer/history.py, analyzer/deals.py) 합성 데이터 테스트

실행:
    python -m unittest tests.test_deals -v

여기서 만드는 행(row)은 전부 가짜입니다. 파일에 저장하지 않고 메모리에서만 씁니다.
실제 항공권 가격과 무관합니다.
"""
import unittest
from datetime import date, timedelta
from fractions import Fraction

from analyzer import deals
from analyzer.history import (GRADE_NONE, GRADE_OK, GRADE_RICH, GRADE_THIN,
                              LEVEL_NIGHTS, LEVEL_PAIR, LEVEL_ROUTE, RouteHistory, Thresholds)

TODAY = date(2026, 10, 1)
TH = Thresholds(min_days_30=7, min_days_90=14, min_days_pair=5, min_days_rich=30)
RULES = {"below_avg30_pct": 15, "watch_below_avg30_pct": 5, "below_low30": True,
         "below_low_all": True, "below_pair_low": True, "drop_1d_pct": 10, "noise_pct": 3}

DEP = date(2026, 11, 10)
RET = date(2026, 11, 13)          # 3박


def row(day, dep, ret, price, run="09:10:00", origin="ICN", destination="KIX"):
    """CSV 한 행과 같은 모양의 dict (합성)."""
    return {"collected_at": f"{day.isoformat()}T{run}+09:00", "origin": origin, "destination": destination,
            "departure_date": dep.isoformat(), "return_date": ret.isoformat(),
            "nights": str((ret - dep).days), "price": str(price), "currency": "KRW",
            "price_type": "round_trip_total", "source": "test", "source_tag": ""}


def days_back(n, end=TODAY):
    """end 하루 전부터 n 일 (오래된 순). 예: n=7 -> end-7 .. end-1"""
    return [end - timedelta(days=i) for i in range(n, 0, -1)]


def hist(rows, today=TODAY, th=TH):
    return RouteHistory(rows, "ICN", "KIX", today, th)


class GradeTests(unittest.TestCase):
    def test_none_when_no_rows(self):
        self.assertEqual(hist([]).route().grade, GRADE_NONE)

    def test_thin_below_min_days_30(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(6)]
        self.assertEqual(hist(rows).route().grade, GRADE_THIN)

    def test_ok_at_exactly_min_days_30(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(7)]
        self.assertEqual(hist(rows).route().grade, GRADE_OK)

    def test_days_outside_30_window_do_not_count(self):
        # 31~37일 전 7일치만 있음 -> 30일 창 안에는 0일 -> THIN
        rows = [row(TODAY - timedelta(days=i), DEP, RET, 200000) for i in range(31, 38)]
        self.assertEqual(hist(rows).route().grade, GRADE_THIN)

    def test_rich_needs_total_30_and_90window_14(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(30)]
        self.assertEqual(hist(rows).route().grade, GRADE_RICH)
        rows29 = [row(d, DEP, RET, 200000) for d in days_back(29)]
        self.assertEqual(hist(rows29).route().grade, GRADE_OK)

    def test_pair_grade_uses_min_days_pair(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(5)]
        h = hist(rows)
        self.assertEqual(h.pair(DEP, RET).grade, GRADE_OK)     # pair 는 5일이면 OK
        self.assertEqual(h.route().grade, GRADE_THIN)          # route 는 7일 필요

    def test_same_day_uses_last_run_only(self):
        d = TODAY - timedelta(days=1)
        rows = [row(d, DEP, RET, 100000, run="09:00:00"), row(d, DEP, RET, 300000, run="15:00:00")]
        pts = hist(rows).route().points
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].min_price, 300000)   # 마지막 실행값


class HoldTests(unittest.TestCase):
    def test_hold_when_no_history(self):
        v = deals.judge(150000, hist([]), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.basis, "none")
        self.assertIn("이력 없음", v.reasons[0])

    def test_hold_when_thin_even_if_price_is_tiny(self):
        # 다른 일정 6일치(route/nights 는 7일 필요), 같은 일정 4일치(pair 는 5일 필요) -> 어느 층도 OK 아님 -> HOLD
        other = (DEP + timedelta(days=3), RET + timedelta(days=3))
        rows = [row(d, *other, 300000) for d in days_back(6)] + [row(d, DEP, RET, 300000) for d in days_back(4)]
        v = deals.judge(1000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.passed, [])
        self.assertTrue(any("판단 보류" in r for r in v.reasons))

    def test_pair_with_min_days_is_judged_even_if_route_is_thin(self):
        rows = [row(d, DEP, RET, 300000) for d in days_back(5)]   # pair 5일 = MIN_DAYS_PAIR
        v = deals.judge(200000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.basis, LEVEL_PAIR)

    def test_one_day_history_is_hold(self):
        rows = [row(TODAY, DEP, RET, 220000)]
        v = deals.judge(220000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.label, deals.LABEL_HOLD)


class Avg30ThresholdTests(unittest.TestCase):
    def setUp(self):
        # route/nights/pair 모두 7일치, 모든 날 200,000원 -> avg30 = 200,000 정확히
        self.rows = [row(d, DEP, RET, 200000) for d in days_back(7)]
        self.h = hist(self.rows)

    def test_exactly_15pct_is_deal(self):
        v = deals.judge(170000, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.label, deals.LABEL_DEAL)
        self.assertIn("below_avg30_pct", v.passed)

    def test_one_won_above_boundary_is_not_avg_deal(self):
        v = deals.judge(170001, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("below_avg30_pct", v.passed)
        self.assertIn("watch_below_avg30_pct", v.passed)     # 14.9995% -> WATCH 구간

    def test_watch_boundary_5pct(self):
        self.assertIn("watch_below_avg30_pct",
                      deals.judge(190000, self.h, nights=3, dep=DEP, ret=RET, rules=RULES).passed)
        v = deals.judge(190001, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("watch_below_avg30_pct", v.passed)

    def test_threshold_is_configurable(self):
        rules = dict(RULES, below_avg30_pct=20)
        v = deals.judge(170000, self.h, nights=3, dep=DEP, ret=RET, rules=rules)
        self.assertNotIn("below_avg30_pct", v.passed)      # 15% 는 20% 기준에 못 미침
        self.assertIn("below_avg30_pct",
                      deals.judge(160000, self.h, nights=3, dep=DEP, ret=RET, rules=rules).passed)

    def test_exact_fraction_boundary_with_awkward_average(self):
        # 평균이 딱 떨어지지 않는 경우: 7일 가격 합 = 1,400,003 -> avg = 200000.428...
        prices = [200000] * 6 + [200003]
        rows = [row(d, DEP, RET, p) for d, p in zip(days_back(7), prices)]
        h = hist(rows)
        total, n = sum(prices), len(prices)
        # (total - p*n)*100 >= 15*total 를 만족하는 최대 정수 p 를 분수로 정확히 구함
        p_max = int((Fraction(total) * (100 - 15) / 100) / n)
        while (Fraction(total) - p_max * n) / Fraction(total) * 100 < 15:
            p_max -= 1
        self.assertIn("below_avg30_pct", deals.judge(p_max, h, nights=3, dep=DEP, ret=RET, rules=RULES).passed)
        self.assertNotIn("below_avg30_pct", deals.judge(p_max + 1, h, nights=3, dep=DEP, ret=RET, rules=RULES).passed)


class NoiseTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(d, DEP, RET, 200000) for d in days_back(7)]
        self.h = hist(self.rows)

    def test_inside_noise_is_reported_as_no_change(self):
        # 이력이 모두 200,000 이므로 평균=최저. 2.5% 낮은 값은 평균/최저 어느 규칙에도 걸리면 안 됨
        v = deals.judge(195000, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)   # 2.5% 낮음
        self.assertTrue(any("잡음 범위" in r for r in v.reasons))
        self.assertEqual(v.passed, [])
        self.assertEqual(v.label, deals.LABEL_NORMAL)

    def test_exactly_noise_pct_is_a_real_change(self):
        v = deals.judge(194000, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)   # 정확히 3.0%
        self.assertFalse(any("잡음 범위" in r and "평균" in r for r in v.reasons))
        self.assertTrue(any("3.0% 낮음" in r for r in v.reasons))
        # 최저(200,000)보다 정확히 3.0% 낮으므로 최저 규칙은 통과 -> DEAL
        self.assertIn("below_low30", v.passed)

    def test_tie_with_low_is_noise_not_deal(self):
        v = deals.judge(200000, self.h, nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.passed, [])
        self.assertTrue(any("동률" in r for r in v.reasons))

    def test_noise_is_configurable(self):
        rules = dict(RULES, noise_pct=0)
        v = deals.judge(199999, self.h, nights=3, dep=DEP, ret=RET, rules=rules)
        self.assertFalse(any("잡음 범위" in r and "평균" in r for r in v.reasons))


class PairComparisonTests(unittest.TestCase):
    def test_pair_low_rule_with_enough_pair_days(self):
        rows = [row(d, DEP, RET, p) for d, p in zip(days_back(7), [240000, 235000, 230000, 225000, 232000, 238000, 236000])]
        v = deals.judge(215000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)   # 최저 225,000 보다 4.4% 낮음
        self.assertEqual(v.basis, LEVEL_PAIR)
        self.assertIn("below_pair_low", v.passed)
        self.assertEqual(v.label, deals.LABEL_DEAL)
        self.assertTrue(any("동일 일정" in r and "225,000원보다" in r for r in v.reasons))

    def test_slightly_below_pair_low_is_noise(self):
        rows = [row(d, DEP, RET, p) for d, p in zip(days_back(7), [240000, 235000, 230000, 225000, 232000, 238000, 236000])]
        v = deals.judge(220000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)   # 2.2% 낮음 -> 잡음
        self.assertNotIn("below_pair_low", v.passed)
        self.assertTrue(any("동일 일정" in r and "잡음 범위" in r for r in v.reasons))

    def test_equal_to_pair_low_is_not_below(self):
        rows = [row(d, DEP, RET, 225000) for d in days_back(7)]
        v = deals.judge(225000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("below_pair_low", v.passed)

    def test_different_dates_are_not_the_same_schedule(self):
        # 이력은 11/10->11/13 만 있음. 11/11->11/14 를 물으면 pair 층은 비어야 하고 nights 층으로 비교
        rows = [row(d, DEP, RET, 200000) for d in days_back(7)]
        v = deals.judge(150000, hist(rows), nights=3, dep=DEP + timedelta(days=1), ret=RET + timedelta(days=1), rules=RULES)
        self.assertEqual(v.basis, LEVEL_NIGHTS)
        self.assertNotIn("below_pair_low", v.passed)
        self.assertNotIn("pair_low", v.metrics)

    def test_pair_thin_falls_back_to_nights_but_notes_it(self):
        # pair 이력 4일(<5), nights 이력 7일 -> 기준은 nights, 동일 일정 비교는 보류 문구
        rows = [row(d, DEP, RET, 200000) for d in days_back(4)]
        other = (DEP + timedelta(days=5), RET + timedelta(days=5))
        rows += [row(d, *other, 210000) for d in days_back(7)]
        v = deals.judge(150000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.basis, LEVEL_NIGHTS)
        self.assertTrue(any("동일 일정 비교 보류" in r for r in v.reasons))


class NightsComparisonTests(unittest.TestCase):
    def test_compares_with_same_nights_not_whole_route(self):
        # 1박 조합은 매일 100,000원, 3박 조합은 매일 300,000원 (7일치)
        one = (date(2026, 11, 20), date(2026, 11, 21))
        three = (date(2026, 11, 20), date(2026, 11, 23))
        rows = []
        for d in days_back(7):
            rows.append(row(d, *one, 100000))
            rows.append(row(d, *three, 300000))
        # 3박 250,000원을 다른 날짜(11/25->11/28)로 물음 -> pair 없음 -> nights(3박) 기준 avg 300,000 -> 16.7% 낮음 -> DEAL
        v = deals.judge(250000, hist(rows), nights=3, dep=date(2026, 11, 25), ret=date(2026, 11, 28), rules=RULES)
        self.assertEqual(v.basis, LEVEL_NIGHTS)
        self.assertEqual(v.metrics["avg30"], 300000.0)
        self.assertIn("below_avg30_pct", v.passed)
        # 같은 가격을 노선 전체(최저 100,000) 기준으로 봤다면 '높음' 이었을 것 -> nights 층이 우선임을 확인
        v_route = deals.judge(250000, hist(rows), nights=None, rules=RULES)
        self.assertEqual(v_route.basis, LEVEL_ROUTE)
        self.assertEqual(v_route.metrics["avg30"], 100000.0)
        self.assertEqual(v_route.label, deals.LABEL_NORMAL)

    def test_nights_without_history_falls_back_to_route(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(7)]      # 3박만 있음
        v = deals.judge(150000, hist(rows), nights=5, rules=RULES)  # 5박 이력 없음 -> route 기준
        self.assertEqual(v.basis, LEVEL_ROUTE)


class SinceCollectionLowTests(unittest.TestCase):
    def test_since_low_with_enough_days(self):
        rows = [row(d, DEP, RET, p) for d, p in zip(days_back(7), [230000, 228000, 226000, 224000, 222000, 221000, 220000])]
        v = deals.judge(210000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)   # 최저 220,000 보다 4.5% 낮음
        self.assertIn("below_low_all", v.passed)
        self.assertTrue(any("수집 이후 최저" in r and "낮음" in r for r in v.reasons))
        # 0.45% 낮은 219,000 은 잡음 범위 -> 수집 이후 최저로 인정하지 않음
        self.assertNotIn("below_low_all", deals.judge(219000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES).passed)

    def test_since_low_tie_is_noise_unless_noise_is_zero(self):
        rows = [row(d, DEP, RET, 220000) for d in days_back(7)]
        v = deals.judge(220000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("below_low_all", v.passed)          # 동률은 잡음 범위
        v0 = deals.judge(219999, hist(rows), nights=3, dep=DEP, ret=RET, rules=dict(RULES, noise_pct=0))
        self.assertIn("below_low_all", v0.passed)            # noise 0 이면 1원 차이도 인정
        self.assertIn("below_low30", v0.passed)

    def test_since_low_not_claimed_with_few_days(self):
        # pair 5일치(OK) 이지만 수집일 5 < MIN_DAYS_30(7) -> 수집 이후 최저 비교는 보류
        rows = [row(d, DEP, RET, 220000) for d in days_back(5)]
        v = deals.judge(100000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("below_low_all", v.passed)
        self.assertTrue(any("수집 이후 최저 비교는 보류" in r for r in v.reasons))


class DropTests(unittest.TestCase):
    def _rows(self):
        # 가장 오래된 날만 200,000 (30일 최저), 이후 6일은 250,000 -> 직전 수집일 = 250,000
        prices = [200000] + [250000] * 6
        return [row(d, DEP, RET, p) for d, p in zip(days_back(7), prices)]

    def test_drop_10pct_is_watch(self):
        v = deals.judge(225000, hist(self._rows()), nights=3, dep=DEP, ret=RET, rules=RULES)  # 직전 대비 정확히 10% 하락
        self.assertIn("drop_1d_pct", v.passed)
        self.assertNotIn("below_low30", v.passed)            # 최저 200,000 보다는 높음
        self.assertEqual(v.label, deals.LABEL_WATCH)

    def test_drop_just_under_threshold_is_not_watch(self):
        v = deals.judge(225001, hist(self._rows()), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertNotIn("drop_1d_pct", v.passed)

    def test_prev_day_is_last_day_before_today_even_if_today_in_history(self):
        rows = [row(d, DEP, RET, 250000) for d in days_back(7)] + [row(TODAY, DEP, RET, 200000)]
        v = deals.judge(200000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertEqual(v.metrics["prev_day"], (TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(v.metrics["prev_price"], 250000)


class NoScoreTests(unittest.TestCase):
    def test_verdict_has_reasons_and_no_score(self):
        rows = [row(d, DEP, RET, 200000) for d in days_back(7)]
        v = deals.judge(170000, hist(rows), nights=3, dep=DEP, ret=RET, rules=RULES)
        self.assertTrue(v.reasons)
        self.assertFalse(hasattr(v, "score"))
        self.assertTrue(v.summary.startswith("[DEAL]"))

    def test_other_route_rows_are_ignored(self):
        rows = [row(d, DEP, RET, 100000, destination="FUK") for d in days_back(7)]
        self.assertEqual(hist(rows).route().grade, GRADE_NONE)


if __name__ == "__main__":
    unittest.main()
