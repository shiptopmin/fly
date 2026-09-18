"""
analyzer/deals.py - 좋은 가격 판정 엔진 (Phase 7-1)

judge() 는 순수 함수입니다: 현재 가격 + 노선 이력(RouteHistory) + 규칙(DEAL_RULES) -> Verdict.
사이트 접근, 파일 쓰기, 점수 합산을 하지 않습니다. 통과한 규칙의 설명 문장이 그대로 결과입니다.

비교 기준(basis) 우선순위
    1. pair   동일 노선 + 동일 출발일 + 동일 귀국일 (이력이 MIN_DAYS_PAIR 이상일 때)
    2. nights 동일 노선 + 동일 숙박일수              (등급 OK 이상일 때)
    3. route  동일 노선 전체 흐름                    (등급 OK 이상일 때)
어느 층도 기준이 될 만큼 이력이 없으면 label = HOLD (판단 보류). 이력이 부족한데 DEAL 이 되는 일은 없습니다.

라벨
    DEAL   : 30일 평균 대비 임계값 이상 낮음 / 30일 최저 이하 / 수집 이후 최저 이하 / 동일 일정 과거 최저보다 낮음 중 하나 이상
    WATCH  : 30일 평균 대비 watch 임계값 이상 낮음(단 DEAL 미만) 또는 직전 수집일 대비 큰 하락
    NORMAL : 위에 해당 없음
    HOLD   : 이력 부족으로 판단 보류

정밀도: 비율 비교는 fractions.Fraction 으로 정확히 계산해 부동소수점 경계 오차를 없앱니다.
"""
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

from .history import (GRADE_NONE, GRADE_OK, GRADE_RICH, GRADE_THIN,
                      LEVEL_NIGHTS, LEVEL_PAIR, LEVEL_ROUTE, RouteHistory, SeriesStats)

LABEL_DEAL, LABEL_WATCH, LABEL_NORMAL, LABEL_HOLD = "DEAL", "WATCH", "NORMAL", "HOLD"

DEFAULT_RULES = {
    "below_avg30_pct": 15,
    "watch_below_avg30_pct": 5,
    "below_low30": True,
    "below_low_all": True,
    "below_pair_low": True,
    "drop_1d_pct": 10,
    "noise_pct": 3,
}


@dataclass
class Verdict:
    label: str                     # DEAL / WATCH / NORMAL / HOLD
    basis: str                     # 비교에 쓴 층: pair / nights / route / none
    grade: str                     # 그 층의 이력 등급
    price: int
    reasons: list = field(default_factory=list)   # 사람이 읽는 근거 문장들
    metrics: dict = field(default_factory=dict)   # 숫자 근거 (avg30, low30, low_all, pct_vs_avg30 ...)
    passed: list = field(default_factory=list)    # 통과한 규칙 이름

    @property
    def summary(self) -> str:
        return f"[{self.label}] " + " / ".join(self.reasons)


# ----------------------------------------------------------------------
# 정확한 비율 계산 (부동소수점 오차 없음)
# ----------------------------------------------------------------------
def pct_below(price: int, ref) -> Fraction:
    """ref 대비 price 가 몇 % 낮은지 (양수 = 낮음, 음수 = 높음). ref 는 int 또는 Fraction."""
    ref = Fraction(ref)
    if ref <= 0:
        return Fraction(0)
    return (ref - Fraction(price)) / ref * 100


def exact_avg(points) -> Fraction:
    """DayPoint 목록의 평균을 분수로 (float 평균을 쓰지 않음)."""
    if not points:
        return Fraction(0)
    return Fraction(sum(p.min_price for p in points), len(points))


def _fmt_pct(x: Fraction) -> str:
    return f"{float(x):.1f}%"


# ----------------------------------------------------------------------
# 판정
# ----------------------------------------------------------------------
def choose_basis(hist: RouteHistory, nights, dep, ret):
    """우선순위대로 비교 기준 층을 고릅니다. (SeriesStats, 후보들의 dict) 를 돌려줍니다."""
    cands = {}
    if dep is not None and ret is not None:
        cands[LEVEL_PAIR] = hist.pair(dep, ret)
    if nights is not None:
        cands[LEVEL_NIGHTS] = hist.nights(nights)
    cands[LEVEL_ROUTE] = hist.route()
    for level in (LEVEL_PAIR, LEVEL_NIGHTS, LEVEL_ROUTE):
        s = cands.get(level)
        if s is not None and s.grade in (GRADE_OK, GRADE_RICH):
            return s, cands
    return None, cands


def judge(price: int, hist: RouteHistory, nights=None, dep: date = None, ret: date = None,
          rules: dict = None) -> Verdict:
    """현재 가격을 노선 이력과 비교해 Verdict 를 돌려줍니다.

    price : 지금 관측한 왕복 총액 (원)
    hist  : 같은 노선의 RouteHistory (today 가 기준일)
    nights, dep, ret : 관측한 조합. dep/ret 가 있으면 pair 층부터 시도
    rules : DEAL_RULES (없으면 DEFAULT_RULES)
    """
    rules = {**DEFAULT_RULES, **(rules or {})}
    noise = Fraction(str(rules["noise_pct"]))
    deal_pct = Fraction(str(rules["below_avg30_pct"]))
    watch_pct = Fraction(str(rules["watch_below_avg30_pct"]))
    drop_pct = Fraction(str(rules["drop_1d_pct"]))
    th = hist.th

    basis, cands = choose_basis(hist, nights, dep, ret)
    reasons, passed, metrics = [], [], {"price": price, "days_collected": hist.days_collected}

    # ---- 이력 부족: 판단 보류 (어떤 층도 OK 가 아님) ----
    if basis is None:
        best = max(cands.values(), key=lambda s: s.days)
        need = th.min_days_pair if best.level == LEVEL_PAIR else th.min_days_30
        if best.grade == GRADE_NONE:
            reasons.append("이력 없음 (이 노선은 아직 수집된 적이 없음) - 판단 보류")
        else:
            reasons.append(f"이력 부족 ({best.key} 기준 수집 {best.days}일치, 최소 {need}일 필요) - 판단 보류")
            if best.w_all and best.w_all.days:
                metrics["low_all_ref"] = best.w_all.low
                reasons.append(f"참고: 수집 이후 최저 {best.w_all.low:,}원 대비 "
                               f"{'+' if price > best.w_all.low else ''}{price - best.w_all.low:,}원 (판단에 쓰지 않음)")
        return Verdict(label=LABEL_HOLD, basis="none", grade=best.grade, price=price,
                       reasons=reasons, metrics=metrics)

    # ---- 기준 층의 지표 ----
    lvl_name = {LEVEL_PAIR: f"동일 일정 {basis.key}", LEVEL_NIGHTS: f"{basis.key} 조합", LEVEL_ROUTE: "노선 전체"}[basis.level]
    w30, w90, w_all = basis.w30, basis.w90, basis.w_all
    pts30 = [p for p in basis.points if (hist.today - p.day).days <= 29 and p.day <= hist.today]
    avg30 = exact_avg(pts30)
    metrics.update({"basis": basis.level, "basis_key": basis.key, "basis_days": basis.days,
                    "avg30": float(avg30), "low30": w30.low, "high30": w30.high,
                    "low_all": w_all.low, "days30": w30.days, "days90": w90.days, "days_all": w_all.days})
    reasons.append(f"비교 기준: {lvl_name} (이력 {basis.days}일치, 등급 {basis.grade})")

    is_deal = is_watch = False

    # 규칙 1) 30일 평균 대비
    p_avg = pct_below(price, avg30)
    metrics["pct_vs_avg30"] = float(p_avg)
    if abs(p_avg) < noise:
        reasons.append(f"최근 30일 평균 {float(avg30):,.0f}원과 {_fmt_pct(abs(p_avg))} 차이: 잡음 범위(±{float(noise):g}%), 변화로 보지 않음")
    elif p_avg >= deal_pct:
        is_deal = True
        passed.append("below_avg30_pct")
        reasons.append(f"최근 30일 평균 {float(avg30):,.0f}원보다 {_fmt_pct(p_avg)} 낮음 (기준 {float(deal_pct):g}%)")
    elif p_avg >= watch_pct:
        is_watch = True
        passed.append("watch_below_avg30_pct")
        reasons.append(f"최근 30일 평균 {float(avg30):,.0f}원보다 {_fmt_pct(p_avg)} 낮음 (DEAL 기준 {float(deal_pct):g}% 미만)")
    elif p_avg > 0:
        reasons.append(f"최근 30일 평균 {float(avg30):,.0f}원보다 {_fmt_pct(p_avg)} 낮음 (WATCH 기준 {float(watch_pct):g}% 미만)")
    else:
        reasons.append(f"최근 30일 평균 {float(avg30):,.0f}원보다 {_fmt_pct(-p_avg)} 높음")

    # "최저보다 낮다" 는 판단도 잡음 기준을 넘어야 인정합니다. (동률이나 1~2% 차이는 변화로 보지 않음)
    def below_low(low):
        """(잡음 이상으로 낮음?, 낮은 비율)"""
        p = pct_below(price, low)
        return p >= noise, p

    # 규칙 2) 30일 최저 대비
    if rules["below_low30"] and w30.days:
        ok, p = below_low(w30.low)
        metrics["pct_vs_low30"] = float(p)
        if ok:
            is_deal = True
            passed.append("below_low30")
            reasons.append(f"최근 30일 최저 {w30.low:,}원보다 {_fmt_pct(p)} 낮음")
        elif p >= 0:
            reasons.append(f"최근 30일 최저 {w30.low:,}원과 {'동률' if p == 0 else _fmt_pct(p) + ' 차이'}: 잡음 범위")
        else:
            reasons.append(f"최근 30일 최저 {w30.low:,}원 대비 +{price - w30.low:,}원")

    # 규칙 3) 수집 이후 최저 대비 (수집일이 충분할 때만)
    if rules["below_low_all"] and w_all.days >= th.min_days_30:
        ok, p = below_low(w_all.low)
        metrics["pct_vs_low_all"] = float(p)
        if ok:
            is_deal = True
            passed.append("below_low_all")
            reasons.append(f"수집 이후 최저 {w_all.low:,}원보다 {_fmt_pct(p)} 낮음 ({basis.first_day} 부터 {w_all.days}일치 기준)")
        elif p >= 0:
            reasons.append(f"수집 이후 최저 {w_all.low:,}원과 {'동률' if p == 0 else _fmt_pct(p) + ' 차이'}: 잡음 범위")
    elif rules["below_low_all"]:
        reasons.append(f"수집 이후 최저 비교는 보류 (수집 {w_all.days}일치, 최소 {th.min_days_30}일 필요)")

    # 규칙 4) 동일 일정 과거 최저 (기준 층이 pair 가 아니어도, pair 이력이 충분하면 추가로 본다)
    pair = cands.get(LEVEL_PAIR)
    if rules["below_pair_low"] and pair is not None:
        if pair.days >= th.min_days_pair:
            metrics["pair_low"] = pair.w_all.low
            metrics["pair_days"] = pair.days
            ok, p = below_low(pair.w_all.low)
            metrics["pct_vs_pair_low"] = float(p)
            if ok:
                is_deal = True
                passed.append("below_pair_low")
                reasons.append(f"동일 일정({pair.key})의 과거 최저 {pair.w_all.low:,}원보다 {_fmt_pct(p)} 낮음 ({pair.days}일치)")
            elif p >= 0:
                reasons.append(f"동일 일정({pair.key}) 과거 최저 {pair.w_all.low:,}원과 {'동률' if p == 0 else _fmt_pct(p) + ' 차이'}: 잡음 범위")
            elif basis.level != LEVEL_PAIR:
                reasons.append(f"동일 일정({pair.key}) 과거 최저 {pair.w_all.low:,}원 대비 +{price - pair.w_all.low:,}원")
        elif pair.days:
            reasons.append(f"동일 일정({pair.key}) 이력 {pair.days}일치 (최소 {th.min_days_pair}일) - 동일 일정 비교 보류")

    # 규칙 5) 직전 수집일 대비 하락
    # "직전" = 기준일(today)보다 앞선 마지막 수집일. 오늘 수집분이 이력에 이미 있어도(트래커) 없어도(검색) 같게 동작.
    prev_pts = [p for p in basis.points if p.day < hist.today]
    if prev_pts:
        prev = prev_pts[-1]
        p_drop = pct_below(price, prev.min_price)
        metrics["prev_day"] = prev.day.isoformat()
        metrics["prev_price"] = prev.min_price
        metrics["pct_vs_prev"] = float(p_drop)
        if abs(p_drop) < noise:
            reasons.append(f"직전 수집일({prev.day}) {prev.min_price:,}원과 {_fmt_pct(abs(p_drop))} 차이: 잡음 범위")
        elif p_drop >= drop_pct:
            is_watch = True
            passed.append("drop_1d_pct")
            reasons.append(f"직전 수집일({prev.day}) {prev.min_price:,}원 대비 {price - prev.min_price:,}원 ({_fmt_pct(p_drop)} 하락)")
        elif p_drop > 0:
            reasons.append(f"직전 수집일({prev.day}) 대비 {price - prev.min_price:,}원 ({_fmt_pct(p_drop)} 하락, 기준 {float(drop_pct):g}% 미만)")
        else:
            reasons.append(f"직전 수집일({prev.day}) 대비 +{price - prev.min_price:,}원 ({_fmt_pct(-p_drop)} 상승)")

    label = LABEL_DEAL if is_deal else (LABEL_WATCH if is_watch else LABEL_NORMAL)
    return Verdict(label=label, basis=basis.level, grade=basis.grade, price=price,
                   reasons=reasons, metrics=metrics, passed=passed)
