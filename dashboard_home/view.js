/* 첫 화면의 선별 규칙. 화면을 '여는 시각'(nowMs)을 기준으로 계산합니다. DOM 을 쓰지 않는 순수 함수. */
var RULES = {
  freshHours: 24,        // 오늘의 추천 후보: 1일 이내 확인
  recentHours: 72,       // 현재 최저가 산정: 3일 이내 확인. 그보다 오래되면 참고용
  dropPct: 5,            // '가격이 내려간 일정': 직전 확인 가격보다 이 % 이상 낮음
  weekendNights: [2, 4], // '주말 출발': 금/토 출발, 2~4박
  perGroup: 3,           // 추천 묶음마다 최대 장수 (목적지당 1장)
  outlierRatio: 0.6,     // 목적지의 3일 이내 가격 중간값의 이 배수보다 낮고
  confirmWithin: 1.1     // 직전 확인에서 비슷한 값(이 배수 이내)이 없었으면 '확인 필요'로 빼 둠
};
var HOUR = 3600000;

function kstDate(ms) { return new Date(ms + 9 * HOUR).toISOString().slice(0, 10); }
function weekday(day) { return new Date(day + "T00:00:00Z").getUTCDay(); }
function nightsOf(dep, ret) { return Math.round((Date.parse(ret) - Date.parse(dep)) / (24 * HOUR)); }

function ageClass(seenIso, nowMs) {
  var h = (nowMs - Date.parse(seenIso)) / HOUR;
  if (h <= RULES.freshHours) return "fresh";
  if (h <= RULES.recentHours) return "recent";
  return "old";
}

function median(values) {
  var v = values.slice().sort(function (a, b) { return a - b; }), m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

function byPrice(a, b) {
  return a.p - b.p || (a.dep < b.dep ? -1 : a.dep > b.dep ? 1 : 0) || (a.ret < b.ret ? -1 : a.ret > b.ret ? 1 : 0)
    || (a.d < b.d ? -1 : a.d > b.d ? 1 : 0);
}

function onePerDestination(list, used, limit) {
  var seen = {}, out = [];
  for (var i = 0; i < list.length && out.length < limit; i++) {
    var x = list[i], key = x.d + x.dep + x.ret;
    if (seen[x.d] || used[key]) continue;
    seen[x.d] = true;
    used[key] = true;
    out.push(x);
  }
  return out;
}

function buildView(data, nowMs) {
  var today = kstDate(nowMs);
  var offers = data.offers
    .filter(function (o) { return o.dep > today && Date.parse(o.t) <= nowMs; })   // 오늘 출발은 제외
    .map(function (o) {
      var c = {};
      for (var k in o) c[k] = o[k];
      c.age = ageClass(o.t, nowMs);
      c.nights = nightsOf(o.dep, o.ret);
      // 하락은 '같은 출발일·귀국일'의 이전 확인 가격과만 비교합니다.
      var low = 1 - RULES.dropPct / 100;
      c.confirmedDrop = !!(o.bp && o.pp && o.pp <= o.bp * low && o.p <= o.bp * low);   // 두 번 연속 낮게 확인
      c.singleDrop = !c.confirmedDrop && !!(o.pp && o.p <= o.pp * low);                 // 한 번만 확인
      c.drop = c.confirmedDrop ? (o.bp - o.p) / o.bp * 100 : c.singleDrop ? (o.pp - o.p) / o.pp * 100 : 0;
      return c;
    });

  // 평소보다 지나치게 낮은데 한 번만 확인된 값은 수집 오류일 수 있어 추천·최저가에서 뺍니다.
  var level = {};
  offers.forEach(function (o) { if (o.age !== "old") (level[o.d] = level[o.d] || []).push(o.p); });
  for (var k in level) level[k] = median(level[k]);
  offers.forEach(function (o) {
    o.suspect = o.age !== "old" && o.p < RULES.outlierRatio * level[o.d] && !(o.pp && o.pp <= o.p * RULES.confirmWithin);
  });
  var suspects = offers.filter(function (o) { return o.suspect; });

  var fresh = offers.filter(function (o) { return o.age === "fresh" && !o.suspect; });
  var used = {};
  var typical = {};   // 목적지별 '1일 이내 확인 가격'의 중간값: 내렸어도 여전히 비싼 일정은 추천하지 않음
  fresh.forEach(function (o) { (typical[o.d] = typical[o.d] || []).push(o.p); });
  for (var d in typical) typical[d] = median(typical[d]);
  var drops = onePerDestination(
    fresh.filter(function (o) { return o.confirmedDrop && o.p <= typical[o.d]; })
      .sort(function (a, b) { return b.drop - a.drop || byPrice(a, b); }), used, RULES.perGroup);
  var weekend = onePerDestination(
    fresh.filter(function (o) {
      var w = weekday(o.dep);
      return (w === 5 || w === 6) && o.nights >= RULES.weekendNights[0] && o.nights <= RULES.weekendNights[1];
    }).sort(byPrice), used, RULES.perGroup);

  var codes = Object.keys(data.airports);
  var dests = codes.map(function (code) {
    var mine = offers.filter(function (o) { return o.d === code; });
    var current = mine.filter(function (o) { return o.age !== "old" && !o.suspect; }).sort(byPrice);
    var best = current[0] || null;
    var bestFresh = current.filter(function (o) { return o.age === "fresh"; })[0] || null;
    var reference = null;
    if (!best) {   // 참고용: 가장 최근에 확인한 날의 최저가
      var old = mine.filter(function (o) { return !o.suspect; }).sort(function (a, b) {
        return (a.t.slice(0, 10) < b.t.slice(0, 10) ? 1 : a.t.slice(0, 10) > b.t.slice(0, 10) ? -1 : 0) || byPrice(a, b);
      });
      reference = old[0] || null;
    }
    return { code: code, best: best, bestFresh: (bestFresh && best && bestFresh !== best) ? bestFresh : null,
             reference: reference };
  }).sort(function (a, b) {
    if (a.best && b.best) return byPrice(a.best, b.best);
    if (a.best) return -1;
    if (b.best) return 1;
    return a.code < b.code ? -1 : 1;
  });

  var seenAt = {};
  offers.forEach(function (o) { seenAt[o.d + o.dep + o.ret] = o; });
  var status = codes.map(function (code) {
    var pairs = (data.targets[code] || []).filter(function (p) { return p[0] > today; });
    var n = { fresh: 0, recent: 0, old: 0, none: 0 };
    pairs.forEach(function (p) {
      var o = seenAt[code + p[0] + p[1]];
      n[o ? o.age : "none"]++;
    });
    var last = null;
    data.offers.forEach(function (o) {
      if (o.d === code && (!last || Date.parse(o.t) > Date.parse(last))) last = o.t;
    });
    return { code: code, total: pairs.length, fresh: n.fresh, recent: n.recent, old: n.old, none: n.none,
             lastSeen: last, attempts: data.attempts.filter(function (a) { return a.d === code; }) };
  });

  var lastSeen = null;
  data.offers.forEach(function (o) { if (!lastSeen || Date.parse(o.t) > Date.parse(lastSeen)) lastSeen = o.t; });
  // 추천이나 최저가 계산에서 빠진 가격: 이유별 개수와 예시
  function excluded(key, list, order) {
    return { key: key, count: list.length, examples: list.slice().sort(order || byPrice).slice(0, RULES.perGroup) };
  }
  function byDrop(a, b) { return b.drop - a.drop || byPrice(a, b); }
  var departing = data.offers.filter(function (o) { return o.dep === today; }).map(function (o) {
    var c = {};
    for (var k in o) c[k] = o[k];
    c.age = ageClass(o.t, nowMs);
    c.nights = nightsOf(o.dep, o.ret);
    return c;
  });
  var marked = (data.flagged || []).filter(function (o) { return o.dep > today; }).map(function (o) {
    var c = {};
    for (var k in o) c[k] = o[k];
    c.age = ageClass(o.t, nowMs);
    c.nights = nightsOf(o.dep, o.ret);
    return c;
  });
  var left = [
    excluded("flagged", marked),
    excluded("suspect", suspects),
    excluded("old", offers.filter(function (o) { return o.age === "old"; })),
    excluded("recent", offers.filter(function (o) { return o.age === "recent" && !o.suspect; })),
    excluded("single", fresh.filter(function (o) { return o.singleDrop; }), byDrop),
    excluded("pricey", fresh.filter(function (o) { return o.confirmedDrop && o.p > typical[o.d]; }), byDrop),
    excluded("today", departing)
  ];

  return { today: today, lastSeen: lastSeen, freshCount: fresh.length, drops: drops, weekend: weekend,
           dests: dests, status: status, excluded: left };
}

if (typeof module !== "undefined") module.exports = { buildView: buildView, RULES: RULES, kstDate: kstDate };
