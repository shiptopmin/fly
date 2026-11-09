"""
probe_diag.py - Probe 수집 진단 기록 (진단 기능 A단계)

목적: 다음에 "저장 0건" 같은 문제가 생겼을 때, 수집기가 이미 돌려준 정보(오류 문구 전문, 페이지 로드 수, 저장 행 수,
누락 조합 수, 차단 여부, 목적지별 소요 시간)를 날짜·목적지·실행번호별로 추적할 수 있게 별도 파일에 남깁니다.

원칙
- 기본 꺼짐 (config.PROBE_DIAGNOSTICS["enabled"]). 꺼져 있으면 아무 일도 하지 않고 파일도 만들지 않습니다.
- 수집·판정 로직은 건드리지 않습니다. 이 모듈은 수집이 끝난 뒤의 결과를 '읽기만' 합니다.
- 기록 중 어떤 예외가 나도 밖으로 내보내지 않습니다 (record_destination 은 항상 True/False 만 돌려줌).
- 기존 CSV, Probe 요약 JSON, data/last_run.json, semantics_checks.csv 는 열지도 쓰지도 않습니다.
  새 파일 data/diagnostics/probe_trace/YYYY-MM.jsonl 에 한 줄(JSON)씩 추가만 합니다.
- 용량: 월별 파일, 파일당 상한(max_file_kb) 초과 시 더 쓰지 않음, retention_days 가 지난 월 파일은 정리.

한 줄 = 한 번의 (실행, 맥락, 목적지) 시도. 맥락: probe(본 측정) / preroll(사전 측정) / confirm(정밀 확인).
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

SCHEMA_VERSION = 1
log = logging.getLogger("probe_diag")

DEFAULTS = {"enabled": False, "retention_days": 120, "max_file_kb": 2048, "max_errors": 20, "max_chars": 400}

# 수집기가 남기는 오류 문구 -> 안정된 실패 코드. 앞에서부터 처음 맞는 것. 문구는 collectors/google_flights.py 의 것.
RULES = [
    ("BLOCK_CAPTCHA", "block_check", r"\[ERROR\] CAPTCHA detected"),
    ("BLOCK_CONSENT", "block_check", r"\[ERROR\] Consent page shown"),
    ("NAV_TIMEOUT", "page_load", r"^Timeout opening page for"),
    ("NO_GRID_BUTTON", "grid_button", r"'날짜 표' button not found"),
    ("GRID_DIALOG_TIMEOUT", "grid_open", r"Date grid dialog did not open"),
    ("GRID_UNSTABLE", "grid_render", r"Grid cells did not stabilize"),
    ("SEM_NO_SELECTED_CELL", "validate", r"원인=no_selected_cell"),
    ("SEM_PRICE_MISMATCH", "validate", r"원인=price_mismatch"),
    ("SEM_ANCHOR_MISMATCH", "validate", r"원인=anchor_mismatch"),
    ("SEM_OTHER", "validate", r"Semantics cross-check mismatch"),
]
_COMPILED = [(c, s, re.compile(p)) for c, s, p in RULES]
_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")


def classify(message):
    """오류 문구 -> (코드, 단계). 모르는 문구는 ("UNKNOWN", "unknown")."""
    text = str(message)
    for code, stage, rx in _COMPILED:
        if rx.search(text):
            return code, stage
    return "UNKNOWN", "unknown"


def settings(cfg):
    out = dict(DEFAULTS)
    out.update(getattr(cfg, "PROBE_DIAGNOSTICS", None) or {})
    return out


def enabled(cfg):
    return bool(settings(cfg).get("enabled"))


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def _best(best):
    if best is None:
        return None
    try:
        return {"price": int(best.price), "dep": best.departure_date.isoformat(), "ret": best.return_date.isoformat(),
                "nights": int(best.nights)}
    except Exception:
        return None


def build_record(cfg, *, context, origin, dest, run_date, run_at, env, anchor, res, saved, elapsed_sec, best=None):
    """수집 결과(res)에서 진단 한 줄(dict)을 만듭니다. res 의 값을 바꾸지 않습니다."""
    st = settings(cfg)
    errors = [str(e) for e in (getattr(res, "errors", None) or [])]
    records = len(getattr(res, "records", None) or [])
    missing = len(getattr(res, "missing_pairs", None) or [])
    checks = list(getattr(res, "semantics_checks", None) or [])
    blocked = bool(getattr(res, "blocked", False))
    codes = [classify(e)[0] for e in errors]
    counts = {}
    for c in codes:
        counts[c] = counts.get(c, 0) + 1
    if blocked:
        status = "blocked"
    elif records == 0:
        status = "empty"
    elif missing > 0:
        status = "partial"
    else:
        status = "ok"
    first_stage = next((classify(e)[1] for e in errors), None)
    a = None
    if anchor is not None:
        a = [x.isoformat() if hasattr(x, "isoformat") else str(x) for x in anchor]
    return {
        "v": SCHEMA_VERSION,
        "run_date": run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date),
        "run_at": str(run_at),
        "run_id": os.environ.get("GITHUB_RUN_ID") or None,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or None,
        "run_number": os.environ.get("GITHUB_RUN_NUMBER") or None,
        "sha": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "env": env,
        "context": context,
        "origin": origin,
        "dest": dest,
        "anchor": a,
        "status": status,
        "page_loads": int(getattr(res, "page_loads", 0) or 0),
        "records": records,
        "saved": saved,
        "missing_pairs": missing,
        "expected_pairs": records + missing,
        "blocked": blocked,
        "semantics_verified": bool(getattr(res, "semantics_verified", False)),
        "semantics_checks": len(checks),
        "semantics_mismatches": sum(1 for c in checks if isinstance(c, dict) and not c.get("ok", True)),
        "elapsed_ms": int(round(float(elapsed_sec) * 1000)),
        "error_count": len(errors),
        "error_codes": counts,
        "first_error_stage": first_stage,
        "errors": [_clip(e, st["max_chars"]) for e in errors[: st["max_errors"]]],
        "errors_truncated": len(errors) > st["max_errors"],
        "best": _best(best),
    }


def trace_path(cfg, run_date):
    d = run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date)
    return os.path.join(cfg.PROBE_TRACE_DIR, f"{d[:7]}.jsonl")


def prune_old(cfg, today):
    """retention_days 보다 오래된 월별 진단 파일(YYYY-MM.jsonl)만 지웁니다. 다른 이름의 파일은 건드리지 않습니다."""
    days = int(settings(cfg)["retention_days"])
    removed = []
    folder = cfg.PROBE_TRACE_DIR
    if not os.path.isdir(folder):
        return removed
    cutoff = today - timedelta(days=days)
    for name in os.listdir(folder):
        m = _FILE_RE.match(name)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        end = date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1)       # 그 달의 마지막 날
        if end < cutoff:
            os.remove(os.path.join(folder, name))
            removed.append(name)
    return removed


def append_record(cfg, record, run_date):
    """한 줄을 월별 파일 끝에 추가. 파일 상한을 넘었으면 쓰지 않고 False."""
    path = trace_path(cfg, run_date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cap = int(settings(cfg)["max_file_kb"]) * 1024
    if os.path.exists(path) and os.path.getsize(path) >= cap:
        log.warning("probe_diag: %s 가 상한(%d KB)에 도달해 기록하지 않습니다", path, cap // 1024)
        return False
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    return True


_pruned = set()


def record_destination(cfg, **kw):
    """수집 한 번의 결과를 진단 파일에 남깁니다. 꺼져 있으면 아무것도 하지 않습니다. 절대 예외를 내보내지 않습니다.

    돌려주는 값: 기록했으면 True, 아니면 False.
    """
    try:
        if not enabled(cfg):
            return False
        run_date = kw["run_date"]
        folder = cfg.PROBE_TRACE_DIR
        if folder not in _pruned:                      # 실행(프로세스)당 한 번만 오래된 월 파일 정리
            _pruned.add(folder)
            try:
                prune_old(cfg, run_date if isinstance(run_date, date) else date.fromisoformat(str(run_date)))
            except Exception as e:                     # 정리 실패는 기록을 막지 않음
                log.warning("probe_diag: 오래된 진단 파일 정리 실패: %s", e)
        record = build_record(cfg, **kw)
        return append_record(cfg, record, run_date)
    except Exception as e:                             # 어떤 오류도 수집을 막지 않음
        try:
            log.warning("probe_diag: 진단 기록 실패(수집에는 영향 없음): %s: %s", type(e).__name__, e)
        except Exception:
            pass
        return False
