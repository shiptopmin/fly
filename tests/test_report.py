"""
tests/test_report.py - 대시보드 상단(상태 문구, 최저가 카드)이 판정 엔진과 같은 근거를 쓰는지

실행:
    python -m unittest tests.test_report -v

합성 데이터를 임시 폴더의 CSV 로만 만들고, 테스트가 끝나면 지웁니다. 실제 가격이 아닙니다.
"""
import csv
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import config
import storage
from analyzer import deals
from analyzer.report import build_report

TODAY = date(2026, 10, 1)


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=storage.FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def row(day, dep, ret, price, run="09:10:00"):
    return {"collected_at": f"{day.isoformat()}T{run}+09:00", "origin": "ICN", "destination": "TST",
            "departure_date": dep.isoformat(), "return_date": ret.isoformat(),
            "nights": (ret - dep).days, "price": price, "currency": "KRW",
            "price_type": "round_trip_total", "source": "test", "source_tag": ""}


class ReportUsesSameEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ft_test_")
        self.tracker = os.path.join(self.dir, "history", "ICN-TST.csv")
        self.confirm = os.path.join(self.dir, "confirm", "ICN-TST.csv")
        self.route = SimpleNamespace(origin="ICN", destination="TST", year=2026, month=11,
                                     min_nights=1, max_nights=7, history_file=self.tracker)
        self.dep, self.ret = date(2026, 11, 10), date(2026, 11, 13)
        # 과거 7일 + 이번 수집(TODAY) = 8일치. 판정이 열리는 최소 이력(7일)을 채운다.
        write_csv(self.tracker, [row(TODAY - timedelta(days=i), self.dep, self.ret, 250000)
                                 for i in range(7, -1, -1)])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _report(self, confirm_rows):
        write_csv(self.confirm, confirm_rows)
        return build_report(self.route, config,
                            sources=[("tracker", self.tracker), ("confirm", self.confirm)])

    def test_status_and_card_use_cheaper_confirm_observation(self):
        cheap = (date(2026, 11, 20), date(2026, 11, 23))     # 대상 월 안, 같은 3박
        rp = self._report([row(TODAY - timedelta(days=4), *cheap, 200000)])
        self.assertEqual(rp.current_min, 250000)
        self.assertEqual(rp.s_all.low, 200000, "카드는 정밀검색에서 본 더 싼 값을 보여야 합니다")
        self.assertEqual(rp.observed_low_info["source"], "confirm")
        self.assertFalse(rp.observed_low_is_current)
        self.assertNotIn("신저가", rp.status, f"이미 더 싼 값을 봤으므로 신저가라고 하면 안 됩니다: {rp.status}")
        self.assertTrue(any("200,000" in r for r in rp.verdict.reasons), rp.verdict.reasons)

    def test_status_is_the_verdict_title(self):
        """상단 상태 문구와 판정 블록은 판정 결과 하나를 공유한다 (따로 계산하지 않음)."""
        rp = self._report([])
        self.assertEqual(rp.status, rp.verdict.title)
        self.assertIsNotNone(rp.verdict_trip)

    def test_same_history_object_is_shared_with_verdict(self):
        cheap = (date(2026, 11, 20), date(2026, 11, 23))
        rp = self._report([row(TODAY - timedelta(days=4), *cheap, 200000)])
        self.assertEqual(set(rp.hist.sources), {"tracker", "confirm"})
        t = rp.top[0]
        v = deals.judge(t.price, rp.hist, datetime.fromisoformat(rp.latest_collected_at), nights=t.nights,
                        dep=t.departure_date, ret=t.return_date, rules=config.DEAL_RULES)
        self.assertEqual(v, rp.verdict)
        self.assertNotIn("below_low_all", v.passed)

    def test_out_of_scope_observation_is_not_counted_in_the_card(self):
        """다른 달 출발 관측은 이 노선(11월) 카드의 '수집 이후 관측 최저'에 들어가면 안 된다."""
        other_month = (date(2026, 10, 20), date(2026, 10, 23))
        rp = self._report([row(TODAY - timedelta(days=4), *other_month, 150000)])
        self.assertEqual(rp.s_all.low, 250000)
        self.assertEqual(rp.status, rp.verdict.title)

    def test_current_observation_low_is_marked_as_current(self):
        """이번 수집이 곧 신저가인 경우: 카드는 '이번 수집' 으로 표시하고, 상태와 판정이 신저가로 일치한다."""
        prices = [250000] * 7 + [225000]                      # 어제까지 250,000원, 이번 수집 225,000원
        write_csv(self.tracker, [row(TODAY - timedelta(days=7 - i), self.dep, self.ret, p)
                                 for i, p in enumerate(prices)])
        rp = self._report([])
        self.assertEqual(rp.current_min, 225000)
        self.assertEqual(rp.s_all.low, 225000)
        self.assertTrue(rp.observed_low_is_current)
        self.assertEqual(rp.status, rp.verdict.title)
        self.assertTrue(rp.status.startswith("WATCH · 신저가"), rp.status)
        self.assertIn("below_low_all", rp.verdict.passed)     # 이전에는 자기 자신과 동률이 되어 통과하지 못했다

    def test_tie_with_past_low_is_not_marked_as_current_low(self):
        rp = self._report([])
        self.assertEqual(rp.s_all.low, 250000)
        self.assertFalse(rp.observed_low_is_current)          # 가장 이른 동일 가격 관측이 기준
        self.assertTrue(rp.status.startswith("NORMAL"), rp.status)


if __name__ == "__main__":
    unittest.main()
