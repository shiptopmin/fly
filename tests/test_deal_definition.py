"""
tests/test_deal_definition.py - DEAL 정의(③ AND (① OR ②))와 현재 관측 / 과거 비교 대상 분리 회귀 테스트

실행:
    python -m unittest tests.test_deal_definition -v

합성 사례의 가격은 가짜입니다. 실제 사례(9/28 오사카)는 tests/fixtures 의 고정본을 씁니다.

모든 사례는 '현재 관측이 이미 history 에 저장된 상태'(운영과 같은 조건)와 '저장되지 않은 상태'를
둘 다 판정해서, 두 결과가 완전히 같은지 검증합니다.
"""
import csv
import os
import unittest
from datetime import date, datetime, timedelta

from analyzer import deals
from analyzer.history import RouteHistory
from tests.test_deals import KST, RULES, TH, TODAY, days_back, row

NOW = datetime(2026, 10, 1, 14, 0, 0, tzinfo=KST)     # 판정 시점 (오늘 14:00). 이력의 과거 행은 모두 이보다 이전
S = (date(2026, 11, 10), date(2026, 11, 13))          # 판정할 일정 (3박)
O = (date(2026, 11, 17), date(2026, 11, 20))          # 같은 숙박일수의 다른 일정
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "osaka_20260928_1night.csv")


def rows_for(sched, prices):
    """days_back(7) 의 날짜 순서대로 prices 를 매일 09:10 관측으로 만든다 (가짜 가격)."""
    return [row(d, sched[0], sched[1], p) for d, p in zip(days_back(len(prices)), prices)]


def judge_both(price, past_rows, rules=None, sched=S, nights=3, as_of=NOW, extra_groups=()):
    """현재 관측이 '저장되지 않은' 이력과 '이미 저장된' 이력으로 각각 판정하고, 같은 결과인지 확인한 뒤 돌려준다."""
    rules = rules or RULES
    unsaved = RouteHistory.from_groups([("tracker", past_rows), *extra_groups], "ICN", "KIX", as_of.date(), TH)
    current = row(as_of.date(), sched[0], sched[1], price, run=as_of.strftime("%H:%M:%S"))
    saved = RouteHistory.from_groups([("tracker", past_rows + [current]), *extra_groups],
                                     "ICN", "KIX", as_of.date(), TH)
    rows_before = len(saved.rows)
    v_unsaved = deals.judge(price, unsaved, as_of, nights=nights, dep=sched[0], ret=sched[1], rules=rules)
    v_saved = deals.judge(price, saved, as_of, nights=nights, dep=sched[0], ret=sched[1], rules=rules)
    assert len(saved.rows) == rows_before, "판정이 이력 원본을 바꾸면 안 됩니다"
    assert current in saved.rows, "현재 관측은 이력에 그대로 남아 있어야 합니다"
    assert v_saved == v_unsaved, f"저장 여부에 따라 결과가 달라짐:\n{v_saved}\n{v_unsaved}"
    return v_saved


class RegressionTrioTests(unittest.TestCase):
    """과거 7일이 모두 250,000원일 때 오늘 관측값에 따른 판정 (오늘 관측은 이미 저장된 상태)."""

    PAST = staticmethod(lambda: rows_for(S, [250000] * 7))

    def test_new_low_is_recognised(self):
        v = judge_both(225000, self.PAST())
        self.assertIs(v.conditions["new_low"], True)
        self.assertIn("below_low_all", v.passed)                     # 최저가 규칙 통과
        self.assertEqual(v.label, deals.LABEL_WATCH)                 # 평소보다는 10% 라 ③ 15% 미만 -> DEAL 아님
        self.assertEqual(v.signal, deals.SIGNAL_STRONG)
        self.assertTrue(v.title.startswith("WATCH · 신저가"), v.title)
        self.assertTrue(any("① 수집 이후 관측 최저 250,000원보다 10.0% 낮음" in r for r in v.reasons), v.reasons)

    def test_new_low_with_usual_low_is_deal(self):
        v = judge_both(212000, self.PAST())                          # 평소보다 15.2% 낮음
        self.assertEqual(v.label, deals.LABEL_DEAL)
        self.assertTrue(v.title.startswith("DEAL · 신저가 + 평소 대비 저가"), v.title)

    def test_usual_low_boundary_is_exact(self):
        self.assertEqual(judge_both(212500, self.PAST()).label, deals.LABEL_DEAL)        # 정확히 15.0%
        self.assertEqual(judge_both(212501, self.PAST()).label, deals.LABEL_WATCH)       # 14.9996%

    def test_tie_is_noise(self):
        v = judge_both(250000, self.PAST())
        self.assertEqual(v.label, deals.LABEL_NORMAL)
        self.assertEqual(v.passed, [])
        self.assertIs(v.conditions["new_low"], False)
        self.assertTrue(any("동률" in r and "잡음 범위" in r for r in v.reasons), v.reasons)

    def test_higher_price_is_not_a_new_low(self):
        v = judge_both(260000, self.PAST())
        self.assertEqual(v.label, deals.LABEL_NORMAL)
        self.assertEqual(v.passed, [])
        self.assertIs(v.conditions["new_low"], False)
        self.assertTrue(any("+10,000원" in r for r in v.reasons), v.reasons)

    def test_noise_boundary_for_new_low(self):
        self.assertIs(judge_both(242500, self.PAST()).conditions["new_low"], True)       # 정확히 3.0% 낮음
        self.assertIs(judge_both(242501, self.PAST()).conditions["new_low"], False)      # 3% 미만은 잡음


class EightCombinationTests(unittest.TestCase):
    """① ② ③ 의 여덟 가지 조합. (②의 '-' 는 같은 일정 이력 5일 미만 = 비교 불가)"""

    def check(self, v, label, c1, c2, c3, title_start):
        self.assertEqual(v.label, label, v.summary)
        self.assertEqual((v.conditions["new_low"], v.conditions["pair_improved"], v.conditions["usual_low"]),
                         (c1, c2, c3), v.summary)
        self.assertTrue(v.title.startswith(title_start), v.title)

    def test_1_new_low_and_pair_down_without_usual_low_is_not_deal(self):
        """①O ②O ③X: ① 과 ② 는 같은 사실이라 두 근거로 세지 않는다 -> WATCH (다수결 D 안이면 DEAL 이 됐을 사례)"""
        v = judge_both(225000, rows_for(S, [250000] * 7) + rows_for(O, [260000] * 7))
        self.check(v, "WATCH", True, True, False, "WATCH · 신저가")
        self.assertTrue(any("따로 세지 않음" in r for r in v.reasons), v.reasons)

    def test_2_new_low_and_usual_low_on_new_schedule_is_deal(self):
        """①O ②- ③O: 처음 보는 일정이 기록 경신 + 평소보다 20% 쌈 -> DEAL"""
        v = judge_both(200000, rows_for(O, [250000] * 7))
        self.check(v, "DEAL", True, None, True, "DEAL · 신저가 + 평소 대비 저가")

    def test_3_pair_down_and_usual_low_without_record_is_deal(self):
        """①X ②O ③O: 과거 다른 일정의 특가가 더 쌌지만, 이 일정은 크게 내려 평소보다도 쌈 -> DEAL"""
        past = rows_for(S, [300000] * 7) + rows_for(O, [300000] * 6 + [180000])
        v = judge_both(230000, past)
        self.check(v, "DEAL", False, True, True, "DEAL · 동일 일정 하락 + 평소 대비 저가")

    def test_4_new_low_only_is_watch(self):
        """①O ②- ③X: 처음 보는 일정이 근소하게 기록 경신"""
        v = judge_both(240000, rows_for(O, [250000] * 7))
        self.check(v, "WATCH", True, None, False, "WATCH · 신저가")

    def test_5_pair_down_only_is_watch(self):
        """①X ②O ③X: 이 일정만 내렸고 기록도 평소 대비 저가도 아님"""
        v = judge_both(245000, rows_for(S, [260000] * 7) + rows_for(O, [240000] * 7))
        self.check(v, "WATCH", False, True, False, "WATCH · 동일 일정 하락")

    def test_6_usual_low_only_is_watch_with_warning(self):
        """①X ②X ③O: 평소보다 싸지만 이 일정은 최근 더 쌌다 -> WATCH + 경고"""
        past = rows_for(S, [300000] * 6 + [190000]) + rows_for(O, [300000] * 7)
        v = judge_both(240000, past)
        self.check(v, "WATCH", False, False, True, "WATCH · 평소 대비 저가")
        self.assertIn("⚠ 이 일정은 9/30 190,000원이었음", v.detail)
        self.assertEqual(v.metrics["pair_cheaper_before"]["price"], 190000)

    def test_7_all_three_is_deal(self):
        v = judge_both(230000, rows_for(S, [300000] * 7) + rows_for(O, [280000] * 7))
        self.check(v, "DEAL", True, True, True, "DEAL · 신저가 + 평소 대비 저가")

    def test_8_none_is_normal(self):
        v = judge_both(250000, rows_for(S, [250000] * 7) + rows_for(O, [250000] * 7))
        self.check(v, "NORMAL", False, False, False, "NORMAL")

    def test_hold_means_only_insufficient_history(self):
        v = judge_both(1000, rows_for(S, [250000] * 6))                      # 6일치뿐 (오늘은 과거가 아님)
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.detail, "판정 보류 (이력 6일)")
        self.assertEqual(v.passed, [])

    def test_weak_signal_below_average_is_watch(self):
        """①②③ 은 아니지만 평소보다 5% 이상 낮음 -> 약한 신호 WATCH (정밀확인 후보로 넘길 가치가 있는 가격)"""
        past = rows_for(S, [200000] + [260000] * 6) + rows_for(O, [260000] * 7)   # 과거 최저 200,000 이 더 쌈
        v = judge_both(238000, past)                                              # 평균 251,429 보다 5.3% 낮음
        self.assertEqual((v.conditions["new_low"], v.conditions["pair_improved"], v.conditions["usual_low"]),
                         (False, False, False))
        self.assertEqual((v.label, v.signal), (deals.LABEL_WATCH, deals.SIGNAL_WEAK), v.summary)
        self.assertIn("watch_below_avg30_pct", v.passed)
        self.assertTrue(v.title.startswith("WATCH · 약한 신호"), v.title)

    def test_weak_signal_one_day_drop_is_watch(self):
        past = rows_for(S, [200000] * 6 + [260000]) + rows_for(O, [200000] * 7)   # 직전 수집일 같은 일정 260,000
        v = judge_both(234000, past)                                              # 직전 대비 정확히 10% 하락
        self.assertEqual((v.label, v.signal), (deals.LABEL_WATCH, deals.SIGNAL_WEAK), v.summary)
        self.assertIn("drop_1d_pct", v.passed)
        self.assertFalse(v.conditions["usual_low"])
        self.assertEqual(judge_both(234001, past).label, deals.LABEL_NORMAL)       # 10% 미만이면 약한 신호도 아님

    def test_pair_down_required_policy_B(self):
        """B 정책(기본 꺼짐): DEAL 이려면 ② 도 필요"""
        rules_b = dict(RULES, deal_requires_pair_low=True)
        # ②를 계산할 수 없는 사례(처음 보는 일정)는 ③ 과 ① 이 성립해도 DEAL 을 보류한다
        v = judge_both(200000, rows_for(O, [250000] * 7), rules=rules_b)
        self.assertEqual(v.label, deals.LABEL_WATCH)
        self.assertIn("정책: DEAL 은 ② 동일 일정 하락이 필요", v.detail)
        # ② 가 성립하면 그대로 DEAL
        self.assertEqual(judge_both(230000, rows_for(S, [300000] * 7) + rows_for(O, [280000] * 7),
                                    rules=rules_b).label, deals.LABEL_DEAL)
        # 기본값은 꺼져 있다
        self.assertFalse(deals.DEFAULT_RULES["deal_requires_pair_low"])


class PastDefinitionTests(unittest.TestCase):
    """과거 비교 대상 = 판정 시점보다 먼저 수집된 모든 출처의 관측."""

    def test_earlier_observation_from_another_source_counts_as_past(self):
        """트래커가 오전에 더 싼 값을 봤다면, 오후의 같은 가격은 신저가가 아니다."""
        past = rows_for(S, [250000] * 7)
        morning = [row(TODAY, S[0], S[1], 200000, run="09:10:00")]            # 같은 날, 판정 시점(14:00)보다 이전
        v = judge_both(225000, past, extra_groups=[("probe", morning)])
        self.assertIs(v.conditions["new_low"], False)
        self.assertTrue(any("200,000" in r for r in v.reasons), v.reasons)

    def test_later_observation_is_not_past(self):
        past = rows_for(S, [250000] * 7)
        later = [row(TODAY, S[0], S[1], 100000, run="15:00:00")]              # 판정 시점 이후
        v = judge_both(225000, past, extra_groups=[("probe", later)])
        self.assertIs(v.conditions["new_low"], True)

    def test_same_event_probe_record_is_not_past_for_confirmation(self):
        """정밀확인은 Probe 가 연 발견 사건이므로, 판정 시점이 Probe 수집 시각이면 그 Probe 기록은 과거가 아니다."""
        past = rows_for(S, [250000] * 7)
        probe_at = datetime(2026, 10, 1, 17, 34, 0, tzinfo=KST)
        probe_rec = [row(TODAY, S[0], S[1], 225000, run="17:34:00")]
        confirm_rec = [row(TODAY, S[0], S[1], 225000, run="17:35:00")]
        v = judge_both(225000, past, as_of=probe_at,
                       extra_groups=[("probe", probe_rec), ("confirm", confirm_rec)])
        self.assertIs(v.conditions["new_low"], True)                          # 자기 발견과 '동률'이 되지 않음
        self.assertEqual(v.metrics["past_observations"], 7)

    def test_naive_datetime_is_rejected(self):
        h = RouteHistory.from_groups([("tracker", rows_for(S, [250000] * 7))], "ICN", "KIX", TODAY, TH)
        with self.assertRaises(ValueError):
            deals.judge(225000, h, datetime(2026, 10, 1, 14, 0), nights=3, dep=S[0], ret=S[1], rules=RULES)

    def test_judge_does_not_modify_history_or_inputs(self):
        past = rows_for(S, [250000] * 7)
        snapshot = [dict(r) for r in past]
        h = RouteHistory.from_groups([("tracker", past)], "ICN", "KIX", TODAY, TH)
        deals.judge(225000, h, NOW, nights=3, dep=S[0], ret=S[1], rules=RULES)
        self.assertEqual(past, snapshot)
        self.assertEqual(len(h.rows), 7)


class MonthScopeTests(unittest.TestCase):
    """①③ 은 현재 관측과 같은 출발월의 관측끼리만 비교한다 (Probe 기준일이 월 경계에서 바뀌는 문제).

    2026-10-01 에 Probe 기준일이 10월 창에서 11월 창으로 넘어가며, 10월 이력과 11월 요금을 섞어 비교했다."""

    OCT = (date(2026, 10, 13), date(2026, 10, 16))                 # 3박, 10월 출발
    NOV = S                                                         # 3박, 11월 출발 (11/10 -> 11/13)

    def test_other_month_history_is_not_used_hold(self):
        """10월 이력만 7일 있는데 11월 출발이 훨씬 싸다 -> 비교하지 않고 HOLD (가짜 DEAL 이 되지 않음)."""
        v = judge_both(150000, rows_for(self.OCT, [250000] * 7), sched=self.NOV)
        self.assertEqual(v.label, deals.LABEL_HOLD)
        self.assertEqual(v.passed, [])
        self.assertEqual(v.detail, "판정 보류 (11월 출발 이력 없음)")
        self.assertTrue(any("다른 달 출발 관측" in r and "비교하지 않음" in r for r in v.reasons), v.reasons)
        self.assertEqual(v.metrics["scope_month"], "2026-11")

    def test_only_same_month_enters_the_baseline(self):
        """10월 250,000원 / 11월 300,000원 이력. 11월 255,000원은 11월 기준으로만 판정 (정확히 15% 낮음 -> DEAL)."""
        past = rows_for(self.OCT, [250000] * 7) + rows_for(self.NOV, [300000] * 7)
        v = judge_both(255000, past, sched=self.NOV)
        self.assertEqual(v.label, deals.LABEL_DEAL, v.summary)
        self.assertEqual(v.metrics["avg30"], 300000.0)
        self.assertEqual(v.metrics["low_all"], 300000)
        self.assertTrue(any("11월 출발 3박 모든 일정" in r for r in v.reasons), v.reasons)
        self.assertEqual(judge_both(255001, past, sched=self.NOV).label, deals.LABEL_WATCH)   # 14.9997%

    def test_cheaper_other_month_does_not_block_a_real_new_low(self):
        """10월에 훨씬 싼 요금(100,000원)이 있어도, 11월 안에서의 신저가 판정을 막지 않는다."""
        past = rows_for(self.OCT, [100000] * 7) + rows_for(self.NOV, [300000] * 7)
        v = judge_both(280000, past, sched=self.NOV)
        self.assertIs(v.conditions["new_low"], True)
        self.assertEqual(v.metrics["low_all"], 300000)

    def test_expensive_other_month_does_not_inflate_the_baseline(self):
        """10월이 400,000원이어도 11월 평균을 끌어올리지 않는다 (11월 평소 = 300,000원)."""
        past = rows_for(self.OCT, [400000] * 7) + rows_for(self.NOV, [300000] * 7)
        v = judge_both(300000, past, sched=self.NOV)
        self.assertEqual(v.label, deals.LABEL_NORMAL)
        self.assertEqual(v.metrics["avg30"], 300000.0)

    def test_scope_is_the_departure_month_even_if_return_is_next_month(self):
        oct_end = (date(2026, 10, 30), date(2026, 11, 2))              # 10월 출발, 귀국은 11월
        past = rows_for(oct_end, [250000] * 7)
        v = judge_both(200000, past, sched=oct_end)
        self.assertEqual(v.metrics["scope_month"], "2026-10")
        self.assertIs(v.conditions["new_low"], True)

    def test_without_departure_date_there_is_no_month_scope(self):
        h = RouteHistory.from_groups([("tracker", rows_for(self.OCT, [250000] * 7))], "ICN", "KIX", TODAY, TH)
        v = deals.judge(200000, h, NOW, nights=3, rules=RULES)
        self.assertIsNone(v.metrics["scope_month"])
        self.assertIs(v.conditions["new_low"], True)

    def test_empty_history_message_is_unchanged(self):
        v = judge_both(200000, [], sched=self.NOV)
        self.assertEqual(v.detail, "판정 보류 (이력 없음)")


class Osaka0928Tests(unittest.TestCase):
    """2026-09-28 실제 오사카 사례 (217,811원, 10/07->10/08, 1박). 고정본: tests/fixtures.

    이전 코드는 현재 관측을 비교 대상에 넣어 최저가 규칙이 하나도 통과하지 못하고 'WATCH' 만 나왔다.
    """

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        cls.groups = {}
        for r in rows:
            cls.groups.setdefault(r.pop("source_group"), []).append(r)
        cls.as_of = datetime.fromisoformat("2026-09-28T14:18:18+09:00")
        cls.hist = RouteHistory.from_groups(list(cls.groups.items()), "ICN", "KIX", cls.as_of.date(), TH)

    def verdict(self, hist=None):
        return deals.judge(217811, hist or self.hist, self.as_of, nights=1, dep=date(2026, 10, 7),
                           ret=date(2026, 10, 8), rules=RULES)

    def test_fixture_contains_current_observation_as_saved(self):
        saved = [r for _, rows in self.groups.items() for r in rows
                 if r["collected_at"] == self.as_of.isoformat(timespec="seconds")]
        self.assertTrue(any(r["price"] == "217811" and r["departure_date"] == "2026-10-07" for r in saved))

    def test_pair_improvement_is_recognised(self):
        v = self.verdict()
        self.assertIs(v.conditions["pair_improved"], True)
        self.assertIn("below_pair_low", v.passed)                    # 이전 코드에서는 통과하지 못했던 규칙
        self.assertEqual(v.metrics["pair_low"], 232300)
        self.assertAlmostEqual(v.metrics["pct_vs_pair_low"], 6.2, places=1)
        self.assertEqual(v.metrics["pair_days"], 10)

    def test_not_claimed_as_all_time_low_or_deal(self):
        v = self.verdict()
        self.assertIs(v.conditions["new_low"], False)                # 9/18 의 219,989원과 1% 차이: 잡음
        self.assertIs(v.conditions["usual_low"], False)              # 평소보다 2.6% 낮을 뿐
        self.assertEqual(v.label, deals.LABEL_WATCH)
        self.assertTrue(v.title.startswith("WATCH · 동일 일정 하락"), v.title)
        self.assertTrue(any("① " in r and "잡음 범위" in r for r in v.reasons), v.reasons)

    def test_same_result_without_the_saved_current_observation(self):
        now = self.as_of.isoformat(timespec="seconds")
        trimmed = [(label, [r for r in rows if r["collected_at"] != now]) for label, rows in self.groups.items()]
        h2 = RouteHistory.from_groups(trimmed, "ICN", "KIX", self.as_of.date(), TH)
        self.assertEqual(self.verdict(), self.verdict(h2))


if __name__ == "__main__":
    unittest.main()
