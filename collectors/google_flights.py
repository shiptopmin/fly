"""
collectors/google_flights.py - Google Flights "날짜 표(Date Grid)" 에서 왕복 가격 수집

[실제로 확인한 페이지 구조 - 2026-09-18, hl=ko, curr=KRW]
- 검색 URL:
  https://www.google.com/travel/flights?q=Flights from ICN to KIX on 2026-10-10 through 2026-10-12&hl=ko&curr=KRW
  -> 왕복 검색 결과 페이지가 열립니다. (로그인 불필요, CAPTCHA 없이 열림)
- 결과 목록의 각 가격 옆에는 "왕복" 이라고 표시되고,
  "가격에는 성인 1명의 필수 세금과 수수료가 포함됩니다." 문구가 있습니다.
- "날짜 표" 버튼을 누르면 대화상자(role=dialog)가 열리고, 그 안에
  <canvas role="grid" aria-label="날짜 표"> 와, 셀마다
  <div role="button" aria-label="₩356,900, 10월 10일~10월 12일" data-row=.. data-col=..> 가 있습니다.
  * aria-label 형식: "₩가격[, 저가|최저가], M월 D일~M월 D일[, 선택됨]"
  * 열 = 출발일, 행 = 귀국일.
  * 처음 열리면 출발일 = 검색 출발일 ±3일(7열), 귀국일 = 검색 귀국일 ±3일(7행) 범위가 표시됩니다.
    (10/10~10/12 검색 -> 열 10/7..10/13, 행 10/9..10/15 / 10/1~10/5 검색 -> 열 9/28..10/4, 행 10/2..10/8 로 확인)
  * 그리드의 스크롤 버튼("오른쪽으로 스크롤" 등)은 화면(canvas)은 움직이지만 접근성 셀 목록이
    마우스 위치에 따라 불규칙하게 바뀌는 것이 관찰되어 사용하지 않습니다.
    대신 검색 기준 날짜를 바꿔 페이지를 여러 번(10월 기준 9회) 열어 전체 범위를 덮습니다.
  * "가격 로드 중" 이라는 aria-label 을 가진 span 4개는 항상 존재하는 자리표시자입니다(로딩 상태 아님).
- 대화상자 하단에 선택된 조합 금액이 "356900 대한민국 원" 으로도 표시됩니다. (통화 = KRW 확인)

[가격 의미 검증 방법]
- 검색 결과 상단 "최저가 ₩356,900부터" 와 그리드에서 "선택됨" 표시된 셀(검색한 날짜 조합)의
  가격이 같으면, 그리드 셀 가격 = 해당 출발일/귀국일 조합의 왕복 최저 총액(성인 1명, 세금 포함)
  이라고 판단합니다. 페이지를 열 때마다 이 비교를 반복합니다.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .base import BaseCollector, CollectResult, PriceRecord

log = logging.getLogger("collector.google_flights")

KST = timezone(timedelta(hours=9))

# aria-label 안의 날짜 구간 "10월 10일~10월 12일" 을 읽는 정규식
DATE_RANGE_RE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일\s*~\s*(\d{1,2})월\s*(\d{1,2})일")
# 라벨 맨 앞의 가격 "₩356,900" -> 356900
PRICE_RE = re.compile(r"^₩\s*([\d,]+)")
# 결과 목록 상단 "최저가 ₩356,900부터"
LIST_MIN_RE = re.compile(r"최저가\s*₩\s*([\d,]+)\s*부터")

GRID_BUTTON_NAME = "날짜 표"
GRID_HALF_WINDOW = 3   # 그리드가 기준일 ±3일을 보여줌 (7열 x 7행)
GRID_WINDOW = GRID_HALF_WINDOW * 2 + 1


class GoogleFlightsCollector(BaseCollector):
    name = "google_flights"

    def __init__(self, currency="KRW", language="ko", page_load_delay=2.0):
        self.currency = currency
        self.language = language
        self.page_load_delay = page_load_delay

    # ------------------------------------------------------------------
    # 공개 메서드
    # ------------------------------------------------------------------
    def collect(self, origin, destination, year, month, min_nights, max_nights,
                allow_next_month_return, headless) -> CollectResult:
        result = CollectResult()

        # 1) 이번 실행에서 확인해야 하는 (출발일, 귀국일) 조합 목록
        needed = self._needed_pairs(year, month, min_nights, max_nights, allow_next_month_return)
        log.debug("Needed (departure, return) pairs: %d", len(needed))

        # 2) 필요한 조합을 모두 덮는 검색 기준일(출발, 귀국) 목록
        anchors = self._plan_anchors(needed)
        log.debug("Planned page loads: %d -> %s", len(anchors),
                  ", ".join(f"{d.isoformat()}~{r.isoformat()}" for d, r in anchors))

        collected_at = datetime.now(KST).replace(microsecond=0).isoformat()
        seen = {}           # (dep, ret) -> dict(price, tag, raw)  가격이 확인된 셀
        seen_no_price = {}  # (dep, ret) -> raw label  셀은 있는데 가격이 없는 경우
        verified_loads = 0

        with sync_playwright() as p:
            log.debug("Launching Chromium (headless=%s)", headless)
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul",
                                          viewport={"width": 1280, "height": 900})
            page = context.new_page()
            try:
                for i, (anchor_dep, anchor_ret) in enumerate(anchors, start=1):
                    if i > 1:
                        time.sleep(self.page_load_delay)  # 연속 요청 사이 대기 (사이트 부하 최소화)
                    url = self._build_url(origin, destination, anchor_dep, anchor_ret)
                    log.debug("[%d/%d] Opening page: %s", i, len(anchors), url)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    except PlaywrightTimeoutError:
                        result.errors.append(f"Timeout opening page for {anchor_dep}~{anchor_ret}")
                        continue
                    result.page_loads += 1

                    if self._check_blocked(page, result):
                        break  # 차단되면 더 시도하지 않고 종료 (우회하지 않음)

                    grid_button = page.get_by_role("button", name=GRID_BUTTON_NAME)
                    try:
                        grid_button.wait_for(timeout=30_000)
                    except PlaywrightTimeoutError:
                        result.errors.append(f"'{GRID_BUTTON_NAME}' button not found for {anchor_dep}~{anchor_ret}")
                        self._check_blocked(page, result)
                        if result.blocked:
                            break
                        continue
                    list_min_price = self._read_list_min_price(page)
                    log.debug("Result list min price: %s", list_min_price)

                    # 날짜 표 열기
                    grid_button.click()
                    dialog = page.locator('[role="dialog"]').filter(
                        has=page.locator('[role="grid"][aria-label="날짜 표"]'))
                    try:
                        dialog.wait_for(timeout=30_000)
                    except PlaywrightTimeoutError:
                        result.errors.append(f"Date grid dialog did not open for {anchor_dep}~{anchor_ret}")
                        continue
                    cells = self._wait_cells_stable(dialog, result)
                    log.debug("Calendar loaded: %d cells", len(cells))

                    # 가격 의미 검증 (선택된 셀 가격 == 결과 목록 최저가 ?)
                    ok = self._validate_semantics(dialog, list_min_price, result, i == 1)
                    if ok:
                        verified_loads += 1

                    # 셀 병합
                    new_count = 0
                    for key, info in cells.items():
                        if info["price"] is None:
                            seen_no_price[key] = info["raw"]
                            continue
                        prev = seen.get(key)
                        if prev is None:
                            new_count += 1
                        elif prev["price"] != info["price"]:
                            log.warning("Price differs between loads for %s: %s -> %s (keeping first)",
                                        key, prev["price"], info["price"])
                            continue
                        seen[key] = info
                    deps = sorted({k[0] for k in cells})
                    rets = sorted({k[1] for k in cells})
                    log.debug("Grid window: dep %s..%s, ret %s..%s, new pairs=%d",
                              deps[0] if deps else "-", deps[-1] if deps else "-",
                              rets[0] if rets else "-", rets[-1] if rets else "-", new_count)
            finally:
                context.close()
                browser.close()

        # 3) 결과 정리: 필요한 조합만 PriceRecord 로 변환
        for key in needed:
            info = seen.get(key)
            if info is None:
                result.missing_pairs.append(key)
                continue
            result.records.append(PriceRecord(
                departure_date=key[0].isoformat(),
                return_date=key[1].isoformat(),
                price=info["price"],
                currency=self.currency,
                source=self.name,
                collected_at=collected_at,
                price_type="round_trip_total",
                source_tag=info["tag"],
            ))
        result.semantics_verified = (result.page_loads > 0 and verified_loads == result.page_loads)
        result.price_semantics += f" [교차검증 {verified_loads}/{result.page_loads} 페이지 성공]"
        log.debug("Found price entries (all visible cells incl. outside target): %d", len(seen))
        log.debug("Cells without price: %d", len(seen_no_price))
        return result

    # ------------------------------------------------------------------
    # 내부 도우미
    # ------------------------------------------------------------------
    @staticmethod
    def _needed_pairs(year, month, min_nights, max_nights, allow_next_month_return):
        """대상 월의 출발일 x 숙박일수 조합을 (출발일, 귀국일) 날짜 튜플로 만듭니다."""
        pairs = []
        d = date(year, month, 1)
        while d.month == month:
            for n in range(min_nights, max_nights + 1):
                r = d + timedelta(days=n)
                if r.month != month and not allow_next_month_return:
                    continue
                pairs.append((d, r))
            d += timedelta(days=1)
        return pairs

    @staticmethod
    def _plan_anchors(needed):
        """필요한 조합을 7x7 창으로 모두 덮는 검색 기준일 목록을 만듭니다.

        출발일을 7일씩 묶고(열 창), 각 묶음에서 필요한 귀국일 범위를 다시 7일씩 묶어(행 창)
        창의 가운데 날짜를 검색 기준일로 씁니다.
        """
        if not needed:
            return []
        anchors = []
        dep_min = min(k[0] for k in needed)
        dep_max = max(k[0] for k in needed)
        c0 = dep_min
        while c0 <= dep_max:
            c6 = c0 + timedelta(days=GRID_WINDOW - 1)
            rets = sorted({r for d, r in needed if c0 <= d <= c6})
            anchor_dep = c0 + timedelta(days=GRID_HALF_WINDOW)
            r0 = rets[0] if rets else None
            while r0 is not None and r0 <= rets[-1]:
                anchor_ret = r0 + timedelta(days=GRID_HALF_WINDOW)
                if anchor_ret <= anchor_dep:          # 검색은 귀국일 > 출발일 이어야 함
                    anchor_ret = anchor_dep + timedelta(days=1)
                anchors.append((anchor_dep, anchor_ret))
                r0 = r0 + timedelta(days=GRID_WINDOW)
            c0 = c6 + timedelta(days=1)
        return anchors

    def _build_url(self, origin, destination, dep, ret):
        q = f"Flights from {origin} to {destination} on {dep.isoformat()} through {ret.isoformat()}"
        return (f"https://www.google.com/travel/flights?q={quote(q)}"
                f"&hl={self.language}&curr={self.currency}")

    @staticmethod
    def _check_blocked(page, result) -> bool:
        """CAPTCHA / 차단 / 동의 페이지가 나타났는지 확인합니다. 우회하지 않고 기록만 합니다."""
        url = page.url
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            pass
        low = body.lower()
        if "/sorry/" in url or "recaptcha" in low or "비정상적인 트래픽" in body or "unusual traffic" in low:
            log.error("CAPTCHA detected (url=%s)", url)
            result.errors.append("[ERROR] CAPTCHA detected")
            result.blocked = True
            return True
        if "consent.google.com" in url:
            log.error("Consent page shown; automatic acceptance is not implemented (url=%s)", url)
            result.errors.append("[ERROR] Consent page shown - cannot proceed automatically")
            result.blocked = True
            return True
        return False

    @staticmethod
    def _read_list_min_price(page, timeout_sec=20):
        """검색 결과 상단의 '최저가 ₩356,900부터' 문구에서 가격 숫자를 읽습니다.

        결과 목록은 페이지가 열린 뒤 몇 초 동안 비동기로 채워지므로, 문구가 나타날 때까지
        잠시 기다립니다. 끝내 없으면 None 을 돌려줍니다(추측하지 않음).
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                body = page.locator("body").inner_text(timeout=5_000)
            except Exception:
                body = ""
            m = LIST_MIN_RE.search(body)
            if m:
                return int(m.group(1).replace(",", ""))
            time.sleep(0.5)
        return None

    def _wait_cells_stable(self, dialog, result, timeout_sec=15, interval=0.7):
        """가격 셀 목록이 두 번 연속 같게 읽힐 때까지 기다린 뒤 그 셀들을 돌려줍니다."""
        deadline = time.time() + timeout_sec
        prev = None
        while time.time() < deadline:
            cur = self._read_cells(dialog, result)
            priced = {k: v["price"] for k, v in cur.items() if v["price"] is not None}
            if prev is not None and priced and priced == prev:
                return cur
            prev = priced
            time.sleep(interval)
        log.warning("Grid cells did not stabilize within %ss; using last read", timeout_sec)
        return self._read_cells(dialog, result)

    def _read_cells(self, dialog, result):
        """대화상자 안의 모든 가격 셀을 읽어 {(dep, ret): {...}} 로 돌려줍니다."""
        raw = dialog.locator('div[role="button"][aria-label]').evaluate_all(
            "els => els.map(e => ({label: e.getAttribute('aria-label'), "
            "row: e.getAttribute('data-row'), col: e.getAttribute('data-col')}))")
        today = datetime.now(KST).date()
        cells = {}
        for item in raw:
            label = item["label"] or ""
            m = DATE_RANGE_RE.search(label)
            if not m:
                continue  # 날짜 구간이 없는 라벨은 가격 셀이 아님
            dep = self._to_date(int(m.group(1)), int(m.group(2)), today)
            ret = self._to_date(int(m.group(3)), int(m.group(4)), today)
            if dep is None or ret is None:
                result.errors.append(f"Unparseable date in label: {label}")
                continue
            # 날짜-가격 매칭 교차 확인: data-col/data-row 는 '오늘부터 며칠 뒤' 로 관찰되었음
            try:
                if (dep - today).days != int(item["col"]) or (ret - today).days != int(item["row"]):
                    log.warning("Date cross-check mismatch for label %r (col=%s,row=%s)",
                                label, item["col"], item["row"])
            except (TypeError, ValueError):
                pass
            pm = PRICE_RE.match(label)
            price = int(pm.group(1).replace(",", "")) if pm else None
            tag = ""
            for t in (t.strip() for t in label.split(",")):
                if t in ("저가", "최저가"):
                    tag = t
            cells[(dep, ret)] = {"price": price, "tag": tag, "raw": label}
        return cells

    @staticmethod
    def _to_date(m, d, today):
        """'M월 D일' 에 연도를 붙입니다. 오늘보다 이전이면 다음 해로 봅니다(항공권은 미래 날짜만 검색되므로)."""
        try:
            cand = date(today.year, m, d)
        except ValueError:
            return None
        if cand < today - timedelta(days=1):
            try:
                cand = date(today.year + 1, m, d)
            except ValueError:
                return None
        return cand

    @staticmethod
    def _validate_semantics(dialog, list_min_price, result, first_load) -> bool:
        """그리드의 '선택됨' 셀 가격과 결과 목록 최저가를 비교해 가격 의미를 확인합니다."""
        selected = None
        try:
            labels = dialog.locator('div[role="button"][aria-label*="선택됨"]').evaluate_all(
                "els => els.map(e => e.getAttribute('aria-label'))")
            if labels:
                pm = PRICE_RE.match(labels[0])
                selected = int(pm.group(1).replace(",", "")) if pm else None
        except Exception:
            pass
        krw_text = ""
        try:
            # 화면에는 보이지 않는(접근성용) 텍스트라 inner_text 대신 text_content 로 읽습니다
            krw_text = (dialog.locator("text=/대한민국 원/").first.text_content(timeout=3_000) or "").strip()
        except Exception:
            pass
        ok = (selected is not None and list_min_price is not None and selected == list_min_price)
        log.debug("Price semantics check: selected cell=%s, list min=%s, currency text=%r -> %s",
                  selected, list_min_price, krw_text, "OK" if ok else "MISMATCH")
        if first_load:
            result.price_semantics = (
                "Google Flights 날짜 표 셀 가격 = 해당 출발일/귀국일 조합의 왕복 총액 "
                "(성인 1명, 필수 세금·수수료 포함, 검색 결과 중 최저가)"
                + (f" / 통화 표시: '{krw_text}'" if krw_text else ""))
        if not ok:
            result.errors.append(f"Semantics cross-check mismatch: selected cell={selected}, list min={list_min_price}")
        return ok
