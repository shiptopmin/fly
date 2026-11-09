"""
tests/test_probe_diag.py - Probe 진단 기록(A단계) 시험

  분류표        실제로 기록된 오류 문구를 안정된 실패 코드로 바꾼다 (미분류 0)
  기록 내용     오류 문구 전문, 페이지 로드 수, 저장/누락 행 수, 차단 여부, 소요 시간, 날짜·목적지·실행번호
  꺼짐          꺼져 있으면 파일도 폴더도 만들지 않는다
  중립성        진단 켬/끔에서 수집 결과(CSV, 요약 JSON, 의미 점검 로그, 종료 코드)가 바이트 단위로 같다
  격리          기록이 실패(쓰기 불가, 직렬화 오류, 이상한 입력)해도 수집은 그대로 진행된다
  저장소        추가 전용 / 월별 파일 / 상한 / 오래된 월 파일 정리는 진단 폴더 안의 YYYY-MM.jsonl 만

실제 사이트에는 접속하지 않습니다. 수집기는 가짜, 파일은 임시 폴더, 가격은 모두 가짜입니다.
"""
import json
import os
import shutil
import types
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta
from unittest import mock

import config
import probe
import probe_diag
from collectors.base import CollectResult, PriceRecord
from tests.test_preroll import Clock, FakeCollector, KST, TempEnv, seed_main_history

CODES = ["FUK", "HND", "KIX", "NRT", "CTS", "NGO", "OKA", "KOJ"]


class RealisticFake(FakeCollector):
    """실제 수집기처럼 받지 못한 조합을 missing_pairs 에 채워 주는 가짜 수집기(결과 값은 FakeCollector 와 동일)."""
    def collect_query(self, query, headless):
        res = FakeCollector.collect_query(self, query, headless)
        got = {(r.departure_date, r.return_date) for r in res.records}
        res.missing_pairs = [(d, r) for d, r in query.needed_pairs() if (d.isoformat(), r.isoformat()) not in got]
        return res


def res_of(records=3, missing=0, errors=(), loads=1, blocked=False, verified=True, checks=None):
    r = CollectResult()
    r.records = [PriceRecord("2026-12-08", "2026-12-11", 200000 + i, "KRW", "google_flights", "2026-11-05T18:00:00+09:00", "round_trip_total")
                 for i in range(records)]
    r.missing_pairs = [(date(2026, 12, 9), date(2026, 12, 12))] * missing
    r.errors = list(errors)
    r.page_loads = loads
    r.blocked = blocked
    r.semantics_verified = verified
    r.semantics_checks = checks if checks is not None else [{"ok": True}]
    return r


class DiagEnv(TempEnv):
    """TempEnv + 진단 폴더/설정을 임시 폴더로."""
    def setUp(self):
        super().setUp()
        self._diag = (config.PROBE_TRACE_DIR, dict(config.PROBE_DIAGNOSTICS))
        config.PROBE_TRACE_DIR = os.path.join(self.tmp, "diag", "probe_trace")
        config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": False}
        probe_diag._pruned.clear()

    def tearDown(self):
        config.PROBE_TRACE_DIR, config.PROBE_DIAGNOSTICS = self._diag
        super().tearDown()

    def turn_on(self, **kw):
        config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": True, **kw}

    def lines(self, name=None):
        d = config.PROBE_TRACE_DIR
        out = []
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name and fn != name:
                continue
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                out += [json.loads(x) for x in f.read().splitlines()]
        return out


# ----------------------------------------------------------------------
class ClassifyTests(unittest.TestCase):
    REAL = {
        "NO_GRID_BUTTON": "'날짜 표' button not found for 2026-12-08~2026-12-11",
        "GRID_UNSTABLE": "Grid cells did not stabilize within 15s (anchor=2026-11-04~2026-11-05); using last read (가격이 갱신되는 중이었을 수 있음)",
        "SEM_ANCHOR_MISMATCH": "Semantics cross-check mismatch (재확인 1회 후에도 불일치): anchor=2026-11-10~2026-11-11, 선택셀이 기준일과 다름: 2026-11-11~2026-11-12 305,684원, 목록최저=305,684원, 원인=anchor_mismatch",
        "SEM_NO_SELECTED_CELL": "Semantics cross-check mismatch (재확인 1회 후에도 불일치): anchor=2026-11-10~2026-11-11, 선택 표시 셀 없음, 목록최저=260,800원, 원인=no_selected_cell",
        "SEM_PRICE_MISMATCH": "Semantics cross-check mismatch (재확인 1회 후에도 불일치): anchor=2026-12-08~2026-12-11, 선택셀 2026-12-08~2026-12-11 250,799원, 목록최저=190원, 원인=price_mismatch",
        "BLOCK_CAPTCHA": "[ERROR] CAPTCHA detected",
        "BLOCK_CONSENT": "[ERROR] Consent page shown - cannot proceed automatically",
        "NAV_TIMEOUT": "Timeout opening page for 2026-12-08~2026-12-11",
        "GRID_DIALOG_TIMEOUT": "Date grid dialog did not open for 2026-12-08~2026-12-11",
    }

    def test_real_messages_map_to_stable_codes(self):
        for code, msg in self.REAL.items():
            self.assertEqual(probe_diag.classify(msg)[0], code, msg)

    def test_unknown_message_is_labelled_unknown_not_dropped(self):
        self.assertEqual(probe_diag.classify("something new")[0], "UNKNOWN")

    def test_every_rule_code_is_covered_by_the_real_table(self):
        self.assertEqual({c for c, _, _ in probe_diag.RULES} - {"SEM_OTHER"}, set(self.REAL))


# ----------------------------------------------------------------------
class RecordContentTests(unittest.TestCase):
    def build(self, res, **kw):
        base = dict(context="probe", origin="ICN", dest="NGO", run_date=date(2026, 11, 5), run_at="2026-11-05T18:19:03+09:00",
                    env="actions", anchor=(date(2026, 12, 8), date(2026, 12, 11)), res=res, saved=0, elapsed_sec=31.84)
        base.update(kw)
        return probe_diag.build_record(config, **base)

    def test_empty_destination_keeps_full_error_text_and_code(self):
        msg = "'날짜 표' button not found for 2026-12-08~2026-12-11"
        r = self.build(res_of(records=0, missing=36, errors=[msg]))
        self.assertEqual((r["status"], r["records"], r["missing_pairs"], r["expected_pairs"]), ("empty", 0, 36, 36))
        self.assertEqual(r["errors"], [msg])
        self.assertEqual(r["error_codes"], {"NO_GRID_BUTTON": 1})
        self.assertEqual(r["first_error_stage"], "grid_button")
        self.assertEqual((r["page_loads"], r["elapsed_ms"], r["blocked"]), (1, 31840, False))
        self.assertEqual((r["dest"], r["run_date"], r["anchor"], r["context"]), ("NGO", "2026-11-05", ["2026-12-08", "2026-12-11"], "probe"))

    def test_status_values(self):
        self.assertEqual(self.build(res_of())["status"], "ok")
        self.assertEqual(self.build(res_of(records=30, missing=6))["status"], "partial")
        self.assertEqual(self.build(res_of(records=0, missing=36))["status"], "empty")
        self.assertEqual(self.build(res_of(records=0, blocked=True, errors=["[ERROR] CAPTCHA detected"]))["status"], "blocked")

    def test_semantics_summary_and_best(self):
        checks = [{"ok": True}, {"ok": False}, {"ok": False}]
        best = types.SimpleNamespace(price=222461, departure_date=date(2026, 12, 8), return_date=date(2026, 12, 11), nights=3)
        r = self.build(res_of(checks=checks, verified=False), best=best, saved=36)
        self.assertEqual((r["semantics_checks"], r["semantics_mismatches"], r["semantics_verified"], r["saved"]), (3, 2, False, 36))
        self.assertEqual(r["best"], {"price": 222461, "dep": "2026-12-08", "ret": "2026-12-11", "nights": 3})
        self.assertIsNone(self.build(res_of())["best"])

    def test_run_id_comes_from_the_actions_environment(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123456789", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_RUN_NUMBER": "44", "GITHUB_SHA": "4992500abcdef"}):
            r = self.build(res_of())
        self.assertEqual((r["run_id"], r["run_attempt"], r["run_number"], r["sha"]), ("123456789", "2", "44", "4992500"))

    def test_errors_are_limited_but_counted(self):
        errs = [f"Grid cells did not stabilize within 15s (anchor=2026-11-0{i % 9 + 1}~2026-11-1{i % 9})" + "x" * 600 for i in range(30)]
        r = self.build(res_of(errors=errs))
        self.assertEqual(r["error_count"], 30)
        self.assertEqual(len(r["errors"]), config.PROBE_DIAGNOSTICS["max_errors"])
        self.assertTrue(r["errors_truncated"])
        self.assertTrue(all(len(e) <= config.PROBE_DIAGNOSTICS["max_chars"] for e in r["errors"]))
        self.assertEqual(r["error_codes"], {"GRID_UNSTABLE": 30})

    def test_building_a_record_does_not_change_the_result(self):
        res = res_of(records=2, missing=1, errors=["'날짜 표' button not found for x"])
        before = (list(res.records), list(res.missing_pairs), list(res.errors), res.page_loads, res.blocked, list(res.semantics_checks))
        self.build(res)
        self.assertEqual(before, (res.records, res.missing_pairs, res.errors, res.page_loads, res.blocked, res.semantics_checks))


# ----------------------------------------------------------------------
class StorageTests(DiagEnv):
    def rec(self, **kw):
        d = dict(context="probe", origin="ICN", dest="KIX", run_date=date(2026, 11, 5), run_at="2026-11-05T18:00:00+09:00", env="local",
                 anchor=None, res=res_of(), saved=36, elapsed_sec=1.2)
        d.update(kw)
        return d

    def test_disabled_writes_nothing_and_creates_no_folder(self):
        self.assertFalse(probe_diag.record_destination(config, **self.rec()))
        self.assertFalse(os.path.exists(config.PROBE_TRACE_DIR))

    def test_enabled_appends_one_json_line_in_a_monthly_file(self):
        self.turn_on()
        self.assertTrue(probe_diag.record_destination(config, **self.rec()))
        self.assertTrue(probe_diag.record_destination(config, **self.rec(dest="HND", run_date=date(2026, 11, 6))))
        self.assertTrue(probe_diag.record_destination(config, **self.rec(dest="OKA", run_date=date(2026, 12, 1))))
        self.assertEqual(sorted(os.listdir(config.PROBE_TRACE_DIR)), ["2026-11.jsonl", "2026-12.jsonl"])
        nov = self.lines("2026-11.jsonl")
        self.assertEqual([(x["dest"], x["run_date"]) for x in nov], [("KIX", "2026-11-05"), ("HND", "2026-11-06")])
        self.assertEqual(len(self.lines("2026-12.jsonl")), 1)

    def test_append_only_keeps_existing_bytes_and_korean_text(self):
        self.turn_on()
        res = res_of(errors=["'날짜 표' button not found for 2026-12-08~2026-12-11"])
        probe_diag.record_destination(config, **self.rec(res=res))
        path = os.path.join(config.PROBE_TRACE_DIR, "2026-11.jsonl")
        first = open(path, "rb").read()
        probe_diag.record_destination(config, **self.rec(dest="NRT"))
        second = open(path, "rb").read()
        self.assertTrue(second.startswith(first))
        self.assertIn("날짜 표".encode("utf-8"), first)                      # ensure_ascii=False
        self.assertEqual(second.count(b"\n"), 2)

    def test_file_size_cap_stops_writing(self):
        self.turn_on(max_file_kb=1)
        n = sum(probe_diag.record_destination(config, **self.rec(dest=f"D{i}")) for i in range(20))
        self.assertTrue(0 < n < 20)
        self.assertLessEqual(os.path.getsize(os.path.join(config.PROBE_TRACE_DIR, "2026-11.jsonl")), 1024 + 2000)

    def test_prune_removes_only_old_monthly_trace_files(self):
        self.turn_on(retention_days=120)
        os.makedirs(config.PROBE_TRACE_DIR)
        for name in ("2026-06.jsonl", "2026-07.jsonl", "2026-08.jsonl", "2026-11.jsonl", "notes.txt", "2026-06.jsonl.bak"):
            Path(config.PROBE_TRACE_DIR, name).write_text("x\n")
        removed = probe_diag.prune_old(config, date(2026, 11, 9))
        # 기준일 11/9 - 120일 = 7/12: 6월 파일(6/30 끝)만 오래됨. 7월(7/31 끝)부터는 보존
        self.assertEqual(sorted(removed), ["2026-06.jsonl"])
        self.assertEqual(sorted(os.listdir(config.PROBE_TRACE_DIR)),
                         ["2026-06.jsonl.bak", "2026-07.jsonl", "2026-08.jsonl", "2026-11.jsonl", "notes.txt"])

    def test_pruning_never_touches_other_diagnostics_files(self):
        self.turn_on()
        other = os.path.join(self.tmp, "diag", "semantics.csv")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        Path(other).write_text("keep\n")
        probe_diag.record_destination(config, **self.rec(run_date=date(2027, 6, 1)))
        self.assertEqual(Path(other).read_text(), "keep\n")


# ----------------------------------------------------------------------
class IsolationTests(DiagEnv):
    def rec(self, **kw):
        d = dict(context="probe", origin="ICN", dest="KIX", run_date=date(2026, 11, 5), run_at="x", env="local", anchor=None,
                 res=res_of(), saved=1, elapsed_sec=0.1)
        d.update(kw)
        return d

    def test_unwritable_location_does_not_raise(self):
        self.turn_on()
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("a file, not a folder")
        config.PROBE_TRACE_DIR = os.path.join(blocker, "probe_trace")              # 폴더를 만들 수 없음
        self.assertFalse(probe_diag.record_destination(config, **self.rec()))

    def test_open_failure_serialization_failure_and_odd_input_do_not_raise(self):
        self.turn_on()
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(probe_diag.record_destination(config, **self.rec()))
        with mock.patch("probe_diag.json.dumps", side_effect=TypeError("nope")):
            self.assertFalse(probe_diag.record_destination(config, **self.rec()))
        self.assertIsInstance(probe_diag.record_destination(config, **self.rec(res=None)), bool)         # 예외 없음
        self.assertFalse(probe_diag.record_destination(config, **self.rec(elapsed_sec="abc")))
        self.assertFalse(probe_diag.record_destination(config, context="probe"))                       # 필수 인자 누락
        self.assertIsInstance(probe_diag.record_destination(config, **self.rec(res=object())), bool)

    def test_broken_settings_do_not_raise(self):
        config.PROBE_DIAGNOSTICS = None
        self.assertFalse(probe_diag.record_destination(config, **self.rec()))
        config.PROBE_DIAGNOSTICS = {"enabled": True, "max_file_kb": "not a number"}
        self.assertFalse(probe_diag.record_destination(config, **self.rec()))


# ----------------------------------------------------------------------
class ProbeMainTests(DiagEnv):
    """probe.main() 전체를 가짜 수집기로 돌려 진단 켬/끔을 비교."""
    NOW = datetime(2026, 10, 26, 17, 30, tzinfo=KST)

    def run_once(self, on, behavior=None, preroll_days=8, confirm=False):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        config.PROBE_TRACE_DIR = os.path.join(self.tmp, "diag", "probe_trace")
        config.PROBE_PREROLL_DAYS = preroll_days
        config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": on}
        probe_diag._pruned.clear()
        Clock.now = self.NOW
        FakeCollector.calls, FakeCollector.behavior = [], behavior
        seed_main_history(CODES, [date(2026, 10, d) for d in range(15, 26)])
        return self.run_main()

    def tree_without_trace(self):
        t = self.tree()
        return {k: v for k, v in t.items() if os.sep + "probe_trace" + os.sep not in os.sep + k}

    def trace_lines(self):
        return self.lines()

    # run_main 은 항상 --no-confirm 을 붙이므로, 정밀 확인 포함 시험은 직접 main 을 부른다
    def run_main_confirm(self):
        import contextlib, io, sys
        old = sys.argv
        sys.argv = ["probe.py"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = probe.main()
        finally:
            sys.argv = old
        return code, buf.getvalue()

    def test_on_and_off_produce_identical_results_and_on_adds_only_trace_lines(self):
        def run(on):
            shutil.rmtree(self.tmp, ignore_errors=True)
            os.makedirs(self.tmp)
            config.PROBE_TRACE_DIR = os.path.join(self.tmp, "diag", "probe_trace")
            config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": on}
            probe_diag._pruned.clear()
            Clock.now = self.NOW
            FakeCollector.calls, FakeCollector.behavior = [], None
            seed_main_history(CODES, [date(2026, 10, d) for d in range(15, 26)])
            code, out = self.run_main()
            return code, self.tree(), out
        code0, off, out0 = run(False)
        code1, on, out1 = run(True)
        self.assertEqual((code0, code1), (0, 0))
        trace = {k for k in on if os.sep + "probe_trace" + os.sep in os.sep + k}
        self.assertEqual({k for k in off if os.sep + "probe_trace" + os.sep in os.sep + k}, set())
        self.assertEqual(len(trace), 1)
        self.assertEqual({k: v for k, v in on.items() if k not in trace}, off)            # 나머지 모든 파일 바이트 동일
        self.assertEqual(out0, out1)                                                      # 화면 출력도 동일
        lines = [json.loads(x) for k in trace for x in on[k].decode("utf-8").splitlines()]
        self.assertEqual(len(lines), 16)                                                  # 본 측정 8 + 사전 측정 8
        from destinations import resolve
        self.assertEqual([x["dest"] for x in lines if x["context"] == "probe"], [a.code for a in resolve(config.PROBE_DESTINATIONS)])
        self.assertEqual(sum(1 for x in lines if x["context"] == "preroll"), 8)

    def test_failures_are_recorded_per_destination_and_results_stay_identical(self):
        err = "'날짜 표' button not found for 2026-11-10~2026-11-13"
        beh = lambda q, i: {"drop": 36, "errors": [err]} if i == 2 else ({"errors": ["Grid cells did not stabilize within 15s (anchor=2026-11-10~2026-11-13); using last read"]} if i == 5 else {})
        probe.GoogleFlightsCollector = RealisticFake          # TempEnv.tearDown 이 원래대로 되돌림
        self.run_once(False, beh, preroll_days=0)
        off_tree = self.tree_without_trace()
        code, _ = self.run_once(True, beh, preroll_days=0)
        self.assertEqual(code, 0)
        self.assertEqual(self.tree_without_trace(), off_tree)
        lines = self.trace_lines()
        by = {x["dest"]: x for x in lines}
        feed = self.feed()
        self.assertEqual(len(lines), 8)
        dests = [p["code"] for p in feed["probes"]]
        self.assertEqual([x["dest"] for x in lines], dests)                                # 목적지 순서 그대로
        z = by[dests[2]]
        self.assertEqual((z["status"], z["records"], z["saved"], z["missing_pairs"]), ("empty", 0, 0, 36))
        self.assertEqual((z["errors"], z["error_codes"]), ([err], {"NO_GRID_BUTTON": 1}))
        u = by[dests[5]]
        self.assertEqual((u["status"], u["error_codes"]), ("ok", {"GRID_UNSTABLE": 1}))
        for x, p in zip(lines, feed["probes"]):                                           # 피드의 저장 수와 일치
            self.assertEqual((x["saved"], x["run_date"], x["context"]), (p["saved"], "2026-10-26", "probe"))
            self.assertGreaterEqual(x["elapsed_ms"], 0)
            self.assertEqual(x["page_loads"], 1)

    def test_blocked_run_is_recorded_and_stops_exactly_as_before(self):
        beh = lambda q, i: {"blocked": True, "drop": 36, "loads": 1} if i == 1 else {}
        self.run_once(False, beh, preroll_days=8)
        code0 = self.feed().get("blocked")
        off_tree = self.tree_without_trace()
        code, _ = self.run_once(True, beh, preroll_days=8)
        self.assertEqual(code, 1)
        self.assertTrue(code0)
        self.assertEqual(self.tree_without_trace(), off_tree)
        lines = self.trace_lines()
        self.assertEqual(len(lines), 2)                                                   # 두 번째에서 중단: 2곳만 기록
        self.assertTrue(lines[1]["blocked"])
        self.assertEqual((lines[1]["status"], lines[1]["error_codes"]), ("blocked", {"BLOCK_CAPTCHA": 1}))

    def test_preroll_failure_is_recorded_with_context(self):
        beh = lambda q, i: {"drop": 36, "errors": ["'날짜 표' button not found for 2026-12-08~2026-12-11"]} if i == 8 + 3 else {}
        self.run_once(False, beh)
        off_tree = self.tree_without_trace()
        self.run_once(True, beh)
        self.assertEqual(self.tree_without_trace(), off_tree)
        pre = [x for x in self.trace_lines() if x["context"] == "preroll"]
        self.assertEqual(len(pre), 8)
        bad = [x for x in pre if x["status"] != "ok"]
        self.assertEqual([(x["status"], x["error_codes"]) for x in bad], [("empty", {"NO_GRID_BUTTON": 1})])
        self.assertEqual(bad[0]["anchor"], ["2026-12-08", "2026-12-11"])

    def test_write_failure_does_not_change_the_run(self):
        self.run_once(False)
        off_tree = self.tree_without_trace()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp)
        blocker = os.path.join(self.tmp, "blocker")
        Path(blocker).write_text("file")
        config.PROBE_TRACE_DIR = os.path.join(blocker, "probe_trace")
        config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": True}
        probe_diag._pruned.clear()
        Clock.now = self.NOW
        FakeCollector.calls, FakeCollector.behavior = [], None
        config.PROBE_PREROLL_DAYS = 8
        seed_main_history(CODES, [date(2026, 10, d) for d in range(15, 26)])
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        on_tree = {k: v for k, v in self.tree().items() if k != "blocker"}
        self.assertEqual(on_tree, off_tree)

    def test_confirm_step_is_recorded_when_candidates_exist(self):
        import search
        from search import SearchOutcome
        calls = []

        def fake_run_search(base, airports, headless=True, on_progress=None):
            out = SearchOutcome(base=base, airports=list(airports), started_at=datetime.now(KST))
            for a in airports:
                q = __import__("dataclasses").replace(base, destination=a.code)
                res = FakeCollector().collect_query(q, headless)
                res.errors.append("Grid cells did not stabilize within 15s (anchor=2026-11-10~2026-11-13); using last read")
                out.collected.append((a, res))
                out.trips.extend(probe.records_to_trips(res.records, base.origin, a))
            calls.append([a.code for a in airports])
            return out
        from tests.test_preroll import main_row_set
        config.PROBE_PREROLL_DAYS = 0
        Clock.now = self.NOW
        base = None
        with mock.patch.object(probe, "run_search", fake_run_search):
            for on in (False, True):
                shutil.rmtree(self.tmp, ignore_errors=True)
                os.makedirs(self.tmp)
                config.PROBE_TRACE_DIR = os.path.join(self.tmp, "diag", "probe_trace")
                config.PROBE_DIAGNOSTICS = {**probe_diag.DEFAULTS, "enabled": on}
                probe_diag._pruned.clear()
                FakeCollector.calls, FakeCollector.behavior = [], None
                # 과거 이력을 높게 깔아 오늘 가격이 싸게 보이도록 → 후보가 생김
                for code in CODES:
                    for d in range(15, 26):
                        main_row_set(code, date(2026, 10, d), price=400000)
                calls.clear()
                code, _ = self.run_main_confirm()
                tree = self.tree_without_trace()
                if not on:
                    base = (code, tree, len(calls))
                    self.assertGreaterEqual(len(calls), 1, "후보가 생기지 않아 정밀 확인이 실행되지 않음")
        self.assertEqual((code, tree, len(calls)), base)
        conf = [x for x in self.trace_lines() if x["context"] == "confirm"]
        self.assertEqual(len(conf), len(calls))
        self.assertTrue(all(x["saved"] is None and x["error_codes"] == {"GRID_UNSTABLE": 1} for x in conf))


if __name__ == "__main__":
    unittest.main()
