"""
analyzer/offers.py - 저장된 CSV / JSON 을 첫 화면용 '현재 가격 목록'으로 정리합니다. (읽기 전용)

원칙
- 실제로 저장된 가격만 씁니다. 추정/보간/환산을 하지 않습니다.
- 일정(출발지, 목적지, 출발일, 귀국일)마다 '가장 최근에 확인한 관측' 하나를 현재 가격으로 봅니다.
- 신선도(1일/3일 이내)는 여기서 정하지 않습니다. 화면을 '여는 시각' 기준으로 계산해야 하므로
  확인 시각(seen)을 그대로 넘깁니다. (dashboard_home/view.js)
- 사전 측정(preroll)은 판정/후보 선정에 쓰지 않는 기존 원칙에 따라 읽지 않습니다.
"""
import csv
import glob
import json
import os
from datetime import date, datetime, timedelta, timezone

from destinations import load_airports
from routes import load_routes, observation_sources
from search_conditions import SearchQuery

from . import deals, outliers
from .history import RouteHistory, thresholds_from_config

KST = timezone(timedelta(hours=9))
PREV_WINDOW_DAYS = 7          # '직전 확인 가격'으로 인정하는 최대 간격(일)
VERDICT_MAX_AGE_DAYS = 7      # 이보다 오래된 가격에는 판정을 계산하지 않음
PROBE_GRID_HALF = 3           # 탐색 수집 날짜 표: 기준일 앞뒤 3일


def _observation_files(cfg):
    yield "tracker", sorted(glob.glob(os.path.join(cfg.HISTORY_DIR, "*.csv")))
    yield "probe", sorted(glob.glob(os.path.join(cfg.PROBE_DIR, "*.csv")))
    yield "confirm", sorted(glob.glob(os.path.join(cfg.PROBE_DIR, "confirm", "*.csv")))


def read_observations(cfg, today: date = None, flagged_rows: list = None):
    """관측 모음. {(origin, dest, dep, ret): [(seen_datetime, price, source), ...]}

    열쇠가 (출발지, 목적지, 출발일, 귀국일) 이므로 가격 비교는 항상 '같은 일정'끼리만 이루어집니다.
    today 를 주면 그날 이후 출발만 읽습니다. 이상값으로 표시한 실행의 행은 결과에서 빼고,
    flagged_rows 목록을 주면 거기에 담습니다. (원본 파일은 읽기만 합니다)
    """
    marked = outliers.load(cfg)
    out, skipped = {}, 0
    floor = today.isoformat() if today else ""
    for source, files in _observation_files(cfg):
        for path in files:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        if r["departure_date"] < floor:
                            continue
                        if r["currency"] != cfg.CURRENCY or r["price_type"] != "round_trip_total":
                            skipped += 1
                            continue
                        price = int(r["price"])
                        seen = datetime.fromisoformat(r["collected_at"])
                        dep = date.fromisoformat(r["departure_date"])
                        ret = date.fromisoformat(r["return_date"])
                        if price <= 0 or ret <= dep or seen.tzinfo is None:
                            skipped += 1
                            continue
                        key = (r["origin"], r["destination"], r["departure_date"], r["return_date"])
                    except (KeyError, TypeError, ValueError):
                        skipped += 1
                        continue
                    if outliers.is_flagged(r, marked):
                        if flagged_rows is not None:
                            flagged_rows.append({"o": key[0], "d": key[1], "dep": key[2], "ret": key[3], "p": price,
                                                 "t": seen.isoformat(), "s": source})
                        continue
                    out.setdefault(key, []).append((seen, price, source))
    return out, skipped


def daily_checks(observations):
    """한 일정의 '확인한 날'마다 그날 마지막 관측 하나. 날짜 순."""
    by_day = {}
    for o in sorted(observations, key=lambda o: o[0]):
        by_day[o[0].astimezone(KST).date()] = o
    return [by_day[d] for d in sorted(by_day)]


def recent_checks(observations, limit=3):
    """가장 최근 확인부터 거꾸로, 앞 확인과의 간격이 PREV_WINDOW_DAYS 이내인 만큼. [최근, 직전, 그 전]"""
    out = []
    for o in reversed(daily_checks(observations)):
        if out and (out[-1][0].astimezone(KST).date() - o[0].astimezone(KST).date()).days > PREV_WINDOW_DAYS:
            break
        out.append(o)
        if len(out) == limit:
            break
    return out


def drop_follow_up(observations_by_trip, drop_pct=5, back_pct=3):
    """저장된 기록에서 '내려간 가격'이 그다음 확인 때 원래 수준으로 돌아간 비율.

    once  : 직전 확인보다 한 번 drop_pct% 이상 낮았던 경우
    twice : 최근 두 번의 확인이 모두 그 앞 가격보다 drop_pct% 이상 낮았던 경우 (화면의 하락 추천 조건)
    back  : 그다음 확인 가격이 내리기 전 가격의 (100-back_pct)% 이상으로 돌아간 건수
    """
    result = {"once": [0, 0], "twice": [0, 0]}          # [사건 수, 돌아간 수]
    low = (100 - drop_pct) / 100
    back = (100 - back_pct) / 100
    for observations in observations_by_trip.values():
        checks = daily_checks(observations)
        days = [c[0].astimezone(KST).date() for c in checks]
        price = [c[1] for c in checks]
        near = [i > 0 and (days[i] - days[i - 1]).days <= PREV_WINDOW_DAYS for i in range(len(checks))]
        for i in range(1, len(checks) - 1):
            if not (near[i] and near[i + 1]):
                continue
            if price[i] <= price[i - 1] * low:
                result["once"][0] += 1
                result["once"][1] += price[i + 1] >= price[i - 1] * back
            if i >= 2 and near[i - 1] and price[i - 1] <= price[i - 2] * low and price[i] <= price[i - 2] * low:
                result["twice"][0] += 1
                result["twice"][1] += price[i + 1] >= price[i - 2] * back
    return {k: {"n": n, "back": b} for k, (n, b) in result.items()}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tracker_targets(cfg, today: date):
    """정기 수집(월 전체)이 지금 확인해야 하는 일정. {dest: set((dep, ret))}"""
    out = {}
    for route in load_routes(today):
        q = SearchQuery.for_month(route.origin, route.destination, route.year, route.month,
                                  route.min_nights, route.max_nights, cfg.ALLOW_NEXT_MONTH_RETURN)
        q.depart_from = max(q.depart_from, today)
        out.setdefault(route.destination, set()).update(
            (d.isoformat(), r.isoformat()) for d, r in q.needed_pairs())
    return out


def probe_targets(feed, today: date):
    """탐색 수집이 마지막 실행에서 잰 기준일 창의 일정 (요약 JSON 의 anchor 기준). {dest: set((dep, ret))}"""
    if not feed:
        return {}
    dep = date.fromisoformat(feed["anchor"]["dep"])
    ret = date.fromisoformat(feed["anchor"]["ret"])
    h = timedelta(days=PROBE_GRID_HALF)
    q = SearchQuery(origin=feed["origin"], destination="XXX", depart_from=dep - h, depart_to=dep + h,
                    min_nights=1, max_nights=7, return_from=ret - h, return_to=ret + h)
    pairs = {(d.isoformat(), r.isoformat()) for d, r in q.needed_pairs() if d >= today}
    return {p["code"]: set(pairs) for p in feed["probes"]}


def attempts(cfg, feed):
    """가장 최근 수집 '시도'의 결과 (요약 JSON 에 기록된 사실 그대로)."""
    out = []
    last = _read_json(cfg.LAST_RUN_FILE)
    for r in (last or {}).get("routes", []):
        if r.get("skipped"):
            continue
        run_day = r["started_at"][:10]
        missing = sum(1 for m in r.get("missing_pairs", []) if m[:10] >= run_day)   # 지난 출발일은 세지 않음
        out.append({"kind": "tracker", "d": r["destination"], "month": r["target_month"], "at": r["finished_at"],
                    "got": r["records_collected"], "expected": r["records_collected"] + missing,
                    "blocked": bool(r.get("blocked"))})
    if feed:
        for p in feed["probes"]:
            out.append({"kind": "probe", "d": p["code"], "month": None, "at": feed["run_at"],
                        "got": p["records"], "expected": feed["anchor"]["pairs"],
                        "blocked": bool(feed.get("blocked"))})
    return out


def _verdict_fn(cfg, today: date):
    """기존 판정 엔진(analyzer/deals.py)을 그대로 불러 라벨만 가져옵니다. 규칙은 바꾸지 않습니다."""
    routes = {(r.origin, r.destination, r.year, r.month): r for r in load_routes(today, include_ended=True)}
    cache = {}

    def verdict(origin, dest, dep: date, ret: date, price: int, seen: datetime):
        key = (origin, dest, dep.year, dep.month)
        if key not in cache:
            route = routes.get(key)
            sources = observation_sources(origin, dest, tracker_file=route.history_file if route else None)
            cache[key] = RouteHistory.from_files(sources, origin, dest, today, thresholds_from_config(cfg))
        return deals.judge(price, cache[key], seen, nights=(ret - dep).days, dep=dep, ret=ret,
                           rules=cfg.DEAL_RULES).label

    return verdict


def detail_pages(cfg, today: date, docs_dir):
    """docs_dir 에 실제로 있는 기존 노선별 상세 페이지 목록 (공개 설정을 따름)."""
    pages = []
    for route in load_routes(today, include_ended=True):
        name = os.path.basename(route.page_file)
        if route.public and os.path.exists(os.path.join(docs_dir, "routes", name)):
            pages.append({"d": route.destination, "month": f"{route.year:04d}-{route.month:02d}",
                          "href": f"routes/{name}", "ended": route.ended(today)})
    return pages


def build_data(cfg, now: datetime, docs_dir=None):
    """첫 화면에 넣을 데이터 묶음. now 는 화면 생성 시각(시간대 포함).

    docs_dir: 첫 화면이 놓일 폴더. 주면 그 안의 기존 상세 페이지/이전 첫 화면으로 연결합니다.
    """
    today = now.astimezone(KST).date()
    flagged = []
    everything, skipped = read_observations(cfg, flagged_rows=flagged)
    observed = {k: v for k, v in everything.items() if k[2] >= today.isoformat()}
    verdict = _verdict_fn(cfg, today)
    tainted = {(f["d"], f["dep"][:7]) for f in flagged}      # 판정 비교 기록에 이상값이 섞일 수 있는 (목적지, 출발월)
    offers = []
    for (origin, dest, dep, ret), obs in sorted(observed.items()):
        checks = recent_checks(obs)
        seen, price, source = checks[0]
        item = {"o": origin, "d": dest, "dep": dep, "ret": ret, "p": price,
                "t": seen.isoformat(), "s": source}
        if len(checks) > 1:                       # 같은 일정의 직전 확인
            item["pp"], item["pt"] = checks[1][1], checks[1][0].isoformat()
        if len(checks) > 2:                       # 같은 일정의 그 전 확인
            item["bp"], item["bt"] = checks[2][1], checks[2][0].isoformat()
        if (today - seen.astimezone(KST).date()).days <= VERDICT_MAX_AGE_DAYS:
            item["v"] = verdict(origin, dest, date.fromisoformat(dep), date.fromisoformat(ret), price, seen)
            if (dest, dep[:7]) in tainted:
                item["vx"] = 1
        offers.append(item)

    feed = _read_json(os.path.join(cfg.PROBE_FEED_DIR, "latest.json"))
    targets = tracker_targets(cfg, today)
    for dest, pairs in probe_targets(feed, today).items():
        targets.setdefault(dest, set()).update(pairs)

    airports = {a.code: a.city_ko for a in load_airports() if a.enabled}
    # 같은 일정이 여러 파일에 같은 값으로 저장된 경우는 한 번만 보여 줍니다
    unique = {(f["o"], f["d"], f["dep"], f["ret"], f["p"]): f for f in flagged if f["dep"] >= today.isoformat()}

    for dest in set(targets) | {o["d"] for o in offers}:
        airports.setdefault(dest, dest)

    return {
        "generated_at": now.astimezone(KST).replace(microsecond=0).isoformat(),
        "search_url": ("https://www.google.com/travel/flights?q={q}"
                       f"&hl={cfg.LANGUAGE}&curr={cfg.CURRENCY}"),
        "airports": airports,
        "offers": offers,
        "flagged": [unique[k] for k in sorted(unique)],
        "targets": {d: sorted(map(list, p)) for d, p in sorted(targets.items())},
        "attempts": attempts(cfg, feed),
        "drop_follow_up": drop_follow_up(everything),
        "pages": detail_pages(cfg, today, docs_dir) if docs_dir else [],
        "history_page": bool(docs_dir) and os.path.exists(os.path.join(docs_dir, "history.html")),
        "skipped_rows": skipped,
    }
