"""
analyzer/deals.py - 좋은 가격 판정 엔진

judge() 는 순수 함수입니다: 현재 가격 + 판정 시점 + 노선 이력(RouteHistory) + 규칙 -> Verdict.
사이트 접근, 파일 쓰기, 점수 합산을 하지 않습니다. 통과한 조건의 설명이 그대로 결과입니다.

[현재 관측과 과거 비교 대상의 분리]
  - price 가 '지금 판정할 현재 관측' 입니다.
  - 과거 비교 대상은 as_of(판정 시점)보다 먼저 수집된 모든 출처의 관측뿐입니다 (RouteHistory.before).
  - 현재 관측이 이미 이력에 저장돼 있어도 저장돼 있지 않을 때와 같은 결과가 나옵니다.

[세 가지 조건 - 서로 다른 질문]
  ① 신저가       : 같은 숙박일수의 '모든 일정' 과거 최저보다 noise_pct 이상 낮은가 (극단값과 비교)
  ② 동일 일정 하락: 정확히 같은 일정의 과거 최저보다 noise_pct 이상 낮은가 (이 여행 자신의 변화)
                    같은 일정의 이력이 min_days_pair 일 미만이면 '비교 불가'
  ③ 평소 대비 저가: 같은 숙박일수 모든 일정의 최근 30일 '일별 최저가 평균'보다 below_avg30_pct 이상 낮은가

  [비교 범위: 같은 출발월] ①③ 은 현재 관측과 '출발일이 같은 달'인 관측끼리만 비교합니다. 출발 날짜의 달이 다르면
  시즌/연휴가 달라 요금 수준이 다르기 때문입니다(Probe 기준일이 10월 창에서 11월 창으로 바뀐 날 중앙값이 평소 변동의
  5배 움직였음). 같은 출발월 이력이 7일 미만이면 다른 달로 대신하지 않고 HOLD 입니다.
  ①이 성립하면 ②는 논리적으로 따라옵니다(같은 일정은 같은 숙박일수의 일부). 그래서 ①+② 는
  독립된 두 근거로 세지 않고, ① 이 성립하면 ② 는 세부 정보로만 보여줍니다.

[라벨]
  DEAL   : ③ 그리고 (① 또는 ②)       "평소보다 확실히 싸고, 그것이 새로 생긴 사실"
  WATCH  : 강한 신호 = ①, ②, ③ 중 하나만 성립
           약한 신호 = 평균보다 watch_below_avg30_pct 이상 낮음 또는 같은 일정이 직전 수집일보다 drop_1d_pct 이상 하락
  NORMAL : 위에 해당 없음
  HOLD   : 판정할 이력이 부족함 (같은 숙박일수의 30일 창 수집일 < min_days_30). 그 외의 뜻은 없습니다.
           HOLD 여도 가격 사실은 그대로 보여주며, 정밀확인 후보 선정과는 별개입니다.

정밀도: 비율 비교는 fractions.Fraction 으로 정확히 계산해 부동소수점 경계 오차를 없앱니다.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from fractions import Fraction

from . import history
from .history import (GRADE_NONE, GRADE_OK, GRADE_RICH, GRADE_THIN,
                      LEVEL_NIGHTS, LEVEL_PAIR, LEVEL_ROUTE, RouteHistory, SeriesStats)

LABEL_DEAL, LABEL_WATCH, LABEL_NORMAL, LABEL_HOLD = "DEAL", "WATCH", "NORMAL", "HOLD"
SIGNAL_STRONG, SIGNAL_WEAK = "strong", "weak"

DEFAULT_RULES = {
    "below_avg30_pct": 15,           # ③ 평소 대비 저가 기준 (%)
    "watch_below_avg30_pct": 5,      # 약한 신호: 평균 대비 이 % 이상 낮음
    "below_low_all": True,           # ① 신저가 조건 사용
    "below_pair_low": True,          # ② 동일 일정 하락 조건 사용
    "drop_1d_pct": 10,               # 약한 신호: 같은 일정이 직전 수집일 대비 이 % 이상 하락
    "noise_pct": 3,                  # ①② 에서 이 % 미만의 차이는 '변화 없음(잡음)'
    "deal_requires_pair_low": False, # B 정책: DEAL 이려면 ② 동일 일정 하락도 필요 (기본 꺼짐)
}


@dataclass
class Verdict:
    label: str                     # DEAL / WATCH / NORMAL / HOLD
    basis: str                     # 비교에 쓴 가장 세밀한 층: pair / nights / route / none
    grade: str                     # ①③ 기준 층의 이력 등급
    price: int
    reasons: list = field(default_factory=list)   # 사람이 읽는 근거 문장들
    metrics: dict = field(default_factory=dict)   # 숫자 근거
    passed: list = field(default_factory=list)    # 통과한 규칙 이름
    signal: str = ""               # WATCH 일 때 strong / weak
    conditions: dict = field(default_factory=dict)  # {"new_low": True/False/None, "pair_improved": ..., "usual_low": ...}
    detail: str = ""               # 라벨 뒤에 붙는 짧은 근거 (예: "신저가 + 평소 대비 저가")

    @property
    def summary(self) -> str:
        return f"[{self.label}] " + " / ".join(self.reasons)

    @property
    def headline(self) -> str:
        """표에 넣을 한 줄 요약(라벨 제외). 사실과 근거를 문장에서 다시 파싱하지 않고 만들어 둔 값을 씁니다."""
        return self.detail

    @property
    def title(self) -> str:
        """대시보드와 피드가 같이 쓰는 한 줄. 예: 'DEAL · 신저가 + 평소 대비 저가 (...)'"""
        return f"{self.label} · {self.detail}" if self.detail else self.label


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


def _md(iso_date: str) -> str:
    return f"{int(iso_date[5:7])}/{iso_date[8:10]}"


# ----------------------------------------------------------------------
# 판정
# ----------------------------------------------------------------------
def judge(price: int, hist: RouteHistory, as_of: datetime, nights=None, dep: date = None, ret: date = None,
          rules: dict = None) -> Verdict:
    """현재 가격을 판정 시점 이전의 모든 관측과 비교해 Verdict 를 돌려줍니다.

    price : 지금 판정할 현재 관측의 왕복 총액 (원)
    hist  : 같은 노선의 RouteHistory (모든 출처). 현재 관측이 들어 있어도 됩니다.
    as_of : 판정 시점 (시간대 있는 datetime). 이 시각보다 먼저 수집된 관측만 과거로 봅니다. 필수입니다.
    nights, dep, ret : 현재 관측의 조합. nights 가 없으면 노선 전체를 '평소'로 봅니다.
    rules : DEAL_RULES (없으면 DEFAULT_RULES)
    """
    rules = {**DEFAULT_RULES, **(rules or {})}
    noise = Fraction(str(rules["noise_pct"]))
    deal_pct = Fraction(str(rules["below_avg30_pct"]))
    watch_pct = Fraction(str(rules["watch_below_avg30_pct"]))
    drop_pct = Fraction(str(rules["drop_1d_pct"]))

    past = hist.before(as_of)               # 과거 비교 대상: 판정 시점 이전 관측만
    ym = (dep.year, dep.month) if dep is not None else None     # 비교 범위: 현재 관측과 같은 출발월
    th = past.th
    today = as_of.date()
    reasons, passed = [], []
    metrics = {"price": price, "as_of": as_of.isoformat(timespec="seconds"),
               "scope_month": f"{ym[0]:04d}-{ym[1]:02d}" if (dep is not None) else None,
               "past_observations": len(past.rows), "days_collected": past.days_collected,
               "sources": past.sources}

    # ①③ 의 기준 층: 같은 숙박일수(없으면 노선 전체), 그리고 '같은 출발월'.
    # 출발 날짜의 달이 다른 요금(시즌/연휴가 다름)은 평소와 비교할 수 없으므로 섞지 않습니다.
    # 같은 출발월 이력이 부족하면 억지로 다른 달과 비교하지 않고 HOLD 로 둡니다.
    scope = f"{ym[1]}월 출발 " if ym else ""
    if nights is not None:
        base, keep_base = past.nights(nights, ym), history.keep_nights(nights, ym)
        base_name = f"{scope}{nights}박 모든 일정"
    else:
        base = past.route(ym)
        keep_base = history.keep_route_month(ym) if ym else history.keep_route
        base_name = f"{scope}노선 전체" if ym else "노선 전체"
    pair = past.pair(dep, ret) if (dep is not None and ret is not None) else None

    reasons.append(f"판정 시점 {as_of:%Y-%m-%d %H:%M}: 이 시각 이전 관측 {len(past.rows):,}건과 비교 "
                   f"(현재 관측은 비교 대상에서 제외)")

    # ---- 이력 부족: 판정 보류 (HOLD 는 이 뜻으로만 씁니다) ----
    if base.grade not in (GRADE_OK, GRADE_RICH):
        if base.grade == GRADE_NONE and past.rows and ym:
            # 노선 이력은 있지만 같은 출발월의 관측이 없음: 다른 달과 억지로 비교하지 않고 보류
            reasons.append(f"{base_name} 이력 없음 (다른 달 출발 관측 {len(past.rows):,}건은 시즌이 달라 비교하지 않음) - 판단 보류")
            detail = f"판정 보류 ({ym[1]}월 출발 이력 없음)"
        elif base.grade == GRADE_NONE:
            reasons.append("이력 없음 (이 노선은 아직 수집된 적이 없음) - 판단 보류")
            detail = "판정 보류 (이력 없음)"
        else:
            reasons.append(f"이력 부족 ({base_name} 기준 수집 {base.days}일치, 최소 {th.min_days_30}일 필요) - 판단 보류")
            detail = f"판정 보류 (이력 {base.days}일)"
            if base.w_all and base.w_all.days:
                metrics["low_all_ref"] = base.w_all.low
                reasons.append(f"참고: 수집 이후 최저 {base.w_all.low:,}원 대비 "
                               f"{'+' if price > base.w_all.low else ''}{price - base.w_all.low:,}원 (판단에 쓰지 않음)")
        metrics["base_days"] = base.days
        return Verdict(label=LABEL_HOLD, basis="none", grade=base.grade, price=price,
                       reasons=reasons, metrics=metrics, detail=detail)

    # ---- 기준 층의 지표 ----
    pts30 = [p for p in base.points if (today - p.day).days <= 29 and p.day <= today]
    avg30 = exact_avg(pts30)
    src = past.sources
    reasons.append(f"비교 기준: {base_name} (이력 {base.days}일치, 등급 {base.grade}, "
                   f"관측 출처 {', '.join(f'{k} {v}건' for k, v in src.items()) if src else '없음'})")
    metrics.update({"base": base.level, "base_key": base.key, "base_days": base.days, "avg30": float(avg30),
                    "days30": len(pts30)})

    pair_usable = pair is not None and pair.days >= th.min_days_pair
    basis_level = LEVEL_PAIR if pair_usable else base.level
    metrics["basis"] = basis_level
    if pair is not None and not pair_usable:
        metrics["compared_other_dates"] = True
        reasons.append(f"⚠ 비교 대상은 검색한 날짜({dep.month}/{dep.day:02d}→{ret.month}/{ret.day:02d})가 아니라 "
                       f"같은 숙박일수의 다른 날짜 기록입니다. 여행 날짜가 다르므로 참고치로 보세요")

    def _where(obs):
        """그 값을 언제 어디서 봤는지 (주장의 출처를 밝히기 위해)."""
        return (f"{obs['day']} {obs['source']} 기준"
                + (f", {_md(obs['dep'])}→{_md(obs['ret'])}" if obs["dep"] != (dep.isoformat() if dep else None) else ""))

    # ---- ③ 평소 대비 저가 ----
    p3 = pct_below(price, avg30)
    metrics["pct_vs_avg30"] = float(p3)
    c3 = p3 >= deal_pct
    if abs(p3) < noise:
        reasons.append(f"③ 최근 30일 일별 최저 평균 {float(avg30):,.0f}원({base_name})과 {_fmt_pct(abs(p3))} 차이: "
                       f"잡음 범위(±{float(noise):g}%), 변화로 보지 않음")
    elif c3:
        passed.append("below_avg30_pct")
        reasons.append(f"③ 최근 30일 일별 최저 평균 {float(avg30):,.0f}원({base_name})보다 {_fmt_pct(p3)} 낮음 "
                       f"(기준 {float(deal_pct):g}%)")
    elif p3 >= watch_pct:
        passed.append("watch_below_avg30_pct")
        reasons.append(f"③ 최근 30일 일별 최저 평균 {float(avg30):,.0f}원({base_name})보다 {_fmt_pct(p3)} 낮음 "
                       f"(DEAL 기준 {float(deal_pct):g}% 미만)")
    elif p3 > 0:
        reasons.append(f"③ 최근 30일 일별 최저 평균 {float(avg30):,.0f}원({base_name})보다 {_fmt_pct(p3)} 낮음 "
                       f"(WATCH 기준 {float(watch_pct):g}% 미만)")
    else:
        reasons.append(f"③ 최근 30일 일별 최저 평균 {float(avg30):,.0f}원({base_name})보다 {_fmt_pct(-p3)} 높음")

    # ---- ① 신저가: 같은 숙박일수 모든 일정의 과거 최저 (원본 관측 전체에서 찾음) ----
    low_all = past.observed_min(keep_base)
    c1 = None
    if rules["below_low_all"] and low_all:
        p1 = pct_below(price, low_all["price"])
        metrics.update({"low_all": low_all["price"], "pct_vs_low_all": float(p1)})
        c1 = p1 >= noise
        if c1:
            passed.append("below_low_all")
            reasons.append(f"① 수집 이후 관측 최저 {low_all['price']:,}원보다 {_fmt_pct(p1)} 낮음 ({_where(low_all)})")
        elif p1 >= 0:
            reasons.append(f"① 수집 이후 관측 최저 {low_all['price']:,}원과 "
                           f"{'동률' if p1 == 0 else _fmt_pct(p1) + ' 차이'}: 잡음 범위 ({_where(low_all)})")
        else:
            reasons.append(f"① 수집 이후 관측 최저 {low_all['price']:,}원 대비 "
                           f"+{price - low_all['price']:,}원 ({_where(low_all)})")

    # ---- ② 동일 일정 하락: 정확히 같은 일정의 과거 최저 ----
    c2 = None
    pair_low = past.observed_min(history.keep_pair(dep, ret)) if (dep is not None and ret is not None) else None
    if rules["below_pair_low"] and pair is not None:
        if pair_usable and pair_low:
            p2 = pct_below(price, pair_low["price"])
            metrics.update({"pair_low": pair_low["price"], "pair_days": pair.days, "pct_vs_pair_low": float(p2)})
            c2 = p2 >= noise
            if c2:
                passed.append("below_pair_low")
                note = " (① 신저가에 포함되는 사실이라 따로 세지 않음)" if c1 else ""
                reasons.append(f"② 동일 일정({pair.key})의 관측 최저 {pair_low['price']:,}원보다 "
                               f"{_fmt_pct(p2)} 낮음 ({pair.days}일치){note}")
            elif p2 >= 0:
                reasons.append(f"② 동일 일정({pair.key}) 관측 최저 {pair_low['price']:,}원과 "
                               f"{'동률' if p2 == 0 else _fmt_pct(p2) + ' 차이'}: 잡음 범위")
            else:
                reasons.append(f"② 동일 일정({pair.key}) 관측 최저 {pair_low['price']:,}원 대비 "
                               f"+{price - pair_low['price']:,}원")
        elif pair.days:
            reasons.append(f"② 동일 일정({pair.key}) 이력 {pair.days}일치 (최소 {th.min_days_pair}일) - 동일 일정 비교 보류")

    # 같은 일정을 최근에 더 싸게 본 적이 있는지 (평소 대비 저가만 성립할 때 반드시 경고하기 위한 사실)
    cheaper = None
    if pair_low and (Fraction(price) - pair_low["price"]) * 100 >= Fraction(pair_low["price"]) * noise:
        cheaper = pair_low
        metrics["pair_cheaper_before"] = {"price": pair_low["price"], "day": pair_low["day"].isoformat(),
                                          "source": pair_low["source"]}

    # ---- 약한 신호: 같은 일정이 직전 수집일 대비 크게 하락 ----
    drop_hit = False
    if pair is not None:
        prev_pts = [p for p in pair.points if p.day < today]
        if prev_pts:
            prev = prev_pts[-1]
            p_drop = pct_below(price, prev.min_price)
            metrics.update({"prev_day": prev.day.isoformat(), "prev_price": prev.min_price,
                            "pct_vs_prev": float(p_drop)})
            if abs(p_drop) < noise:
                reasons.append(f"직전 수집일({prev.day}) {prev.min_price:,}원과 {_fmt_pct(abs(p_drop))} 차이: 잡음 범위")
            elif p_drop >= drop_pct:
                drop_hit = True
                passed.append("drop_1d_pct")
                reasons.append(f"직전 수집일({prev.day}) {prev.min_price:,}원 대비 "
                               f"{price - prev.min_price:,}원 ({_fmt_pct(p_drop)} 하락)")
            elif p_drop > 0:
                reasons.append(f"직전 수집일({prev.day}) 대비 {price - prev.min_price:,}원 "
                               f"({_fmt_pct(p_drop)} 하락, 기준 {float(drop_pct):g}% 미만)")
            else:
                reasons.append(f"직전 수집일({prev.day}) 대비 +{price - prev.min_price:,}원 ({_fmt_pct(-p_drop)} 상승)")

    # ---- 라벨 결정: DEAL = ③ 그리고 (① 또는 ②) ----
    new_low, pair_down = bool(c1), bool(c2)
    new_fact = new_low or pair_down                       # ①이 성립하면 ②는 별도 근거로 세지 않음
    deal = c3 and (pair_down if rules["deal_requires_pair_low"] else new_fact)
    strong = new_low or pair_down or c3
    weak = ("watch_below_avg30_pct" in passed) or drop_hit
    conditions = {"new_low": c1, "pair_improved": c2, "usual_low": c3}

    def pct_txt(key):
        v = metrics.get(key)
        return f"{v:.1f}%" if v is not None else ""

    if deal:
        label, signal = LABEL_DEAL, ""
        first = "신저가" if new_low else "동일 일정 하락"
        detail = (f"{first} + 평소 대비 저가 (평소보다 {pct_txt('pct_vs_avg30')}, "
                  + (f"이전 최저보다 {pct_txt('pct_vs_low_all')}" if new_low
                     else f"이 일정 과거 최저보다 {pct_txt('pct_vs_pair_low')}") + ")")
    elif strong:
        label, signal = LABEL_WATCH, SIGNAL_STRONG
        if new_low:
            detail = f"신저가 (이전 최저보다 {pct_txt('pct_vs_low_all')}, 평소 대비 {pct_txt('pct_vs_avg30')})"
        elif pair_down:
            detail = f"동일 일정 하락 (이 일정 과거 최저보다 {pct_txt('pct_vs_pair_low')}, 신저가 아님, 평소 대비 {pct_txt('pct_vs_avg30')})"
        else:
            detail = f"평소 대비 저가 (평소보다 {pct_txt('pct_vs_avg30')})"
            if cheaper:
                detail += f" ⚠ 이 일정은 {_md(cheaper['day'].isoformat())} {cheaper['price']:,}원이었음"
        if c3 and new_fact and not deal and rules["deal_requires_pair_low"]:
            detail += " (정책: DEAL 은 ② 동일 일정 하락이 필요해 보류)"
    elif weak:
        label, signal = LABEL_WATCH, SIGNAL_WEAK
        bits = []
        if "watch_below_avg30_pct" in passed:
            bits.append(f"평소보다 {pct_txt('pct_vs_avg30')} 낮음")
        if drop_hit:
            bits.append(f"같은 일정 전일 대비 {pct_txt('pct_vs_prev')} 하락")
        detail = "약한 신호 (" + ", ".join(bits) + ")"
    else:
        label, signal, detail = LABEL_NORMAL, "", "평소 수준"
    if metrics.get("compared_other_dates"):
        detail += " · 다른 날짜 기준 비교"

    metrics["signal"] = signal
    return Verdict(label=label, basis=basis_level, grade=base.grade, price=price, reasons=reasons,
                   metrics=metrics, passed=passed, signal=signal, conditions=conditions, detail=detail)
