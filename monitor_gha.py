#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""저스템(417840) 가격 모니터 - GitHub Actions 단일 실행 버전.

GitHub Actions cron(평일 5분 간격)으로 반복 실행되어:
  1. KST 거래 시간대(09:00-15:30, 16:00-20:00)일 때만 동작
  2. 현재가를 Naver 금융 API에서 조회
  3. 트리거(상방 돌파/하방 -3% 경고/-5% 손절/추적손절/종가 손절) 발생 시 텔레그램 발송
  4. 상태를 state.json 에 저장(워크플로 actions/cache 로 실행 간 유지)

환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (필수, GitHub Secrets)
  BUY_PRICE(기본 19700), SHARES(기본 500), TRAIL_PCT(기본 5.0)  (선택)
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
STATE_PATH = "state.json"

BUY_PRICE = int(os.environ.get("BUY_PRICE", "19700"))
SHARES = int(os.environ.get("SHARES", "500"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

UP_LEVELS = [20000, 20500, 21500, 22000, 22250]
PCT_WARN, PCT_STOP = 3.0, 5.0
WARN_LEVEL = int(round(BUY_PRICE * (1 - PCT_WARN / 100.0)))
STOP_LEVEL = int(round(BUY_PRICE * (1 - PCT_STOP / 100.0)))
DOWN_LEVELS = [WARN_LEVEL, STOP_LEVEL]
CLOSE_LEVEL = STOP_LEVEL
TRAIL_PCT = float(os.environ.get("TRAIL_PCT", "5.0"))
VOL_RELIABLE = 1.5

REG_S, REG_E = 9 * 60, 15 * 60 + 30
AFT_S, AFT_E = 16 * 60, 20 * 60

PLAYBOOK = {
    20000: ("단기 저항 돌파", "보유 유지 (돌파 확인 시 홀딩)"),
    20500: ("상승 추세 강화 가능성", "보유 유지"),
    21500: ("1차 분할익절 검토 (약 150주 매도 고려)", "일부 익절"),
    22000: ("전고점 접근 경고", "일부 익절 / 추격매수 자제"),
    22250: ("2차 분할익절 검토 (약 200주 매도 고려)", "일부 익절"),
    WARN_LEVEL: ("1차 경고 (-3%) - 추세 약화", "경고: 추가매수 금지, 비중 축소 준비"),
    STOP_LEVEL: ("손절선 이탈 (-5%)", "손절 실행 (기계적 손절, 반등 기대 금지)"),
}
STOP_LOSS_LINES = [(WARN_LEVEL, "경고선 (-3%)"), (STOP_LEVEL, "손절선 (-5%)")]


def won(n):
    return "{:,}".format(int(round(n)))


def signed_won(n):
    return ("+" if n >= 0 else "-") + "{:,}".format(abs(int(round(n))))


def signed_pct(p):
    return ("+" if p >= 0 else "-") + "%.2f%%" % abs(p)


def log(msg):
    print(msg, flush=True)


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load_state(today):
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if state.get("date") != today:
        state = {"date": today, "up_active": {}, "down_active": {},
                 "close_done": False, "break_seq": [], "peak_price": 0, "trail_armed": True}
    state.setdefault("up_active", {})
    state.setdefault("down_active", {})
    state.setdefault("peak_price", 0)
    state.setdefault("trail_armed", True)
    state.setdefault("break_seq", [])
    state.setdefault("close_done", False)
    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_quote():
    url = "https://polling.finance.naver.com/api/realtime/domestic/stock/417840"
    d = http_json(url)["datas"][0]
    return {
        "name": d.get("stockName", "저스템"),
        "code": d.get("itemCode", "417840"),
        "price": int(d.get("closePriceRaw") or 0),
        "prev_close": int(d.get("closePriceRaw") or 0) - int(d.get("compareToPreviousClosePriceRaw") or 0),
        "open": int(d.get("openPriceRaw") or 0),
        "high": int(d.get("highPriceRaw") or 0),
        "low": int(d.get("lowPriceRaw") or 0),
        "volume": int(d.get("accumulatedTradingVolumeRaw") or 0),
        "market_status": d.get("marketStatus", "UNKNOWN"),
        "local_traded_at": d.get("localTradedAt", ""),
    }


def fetch_avg_volume(today_str):
    url = ("https://api.finance.naver.com/siseJson.naver?symbol=417840"
           "&requestType=1&startTime=20260101&endTime=%s&timeframe=day" % today_str)
    txt = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20
    ).read().decode("utf-8", "replace").replace("'", '"')
    arr = json.loads(txt[txt.find("["):txt.rfind("]") + 1])
    vols = []
    for row in arr[1:]:
        try:
            if str(row[0]) != today_str:
                vols.append(int(row[5]))
        except Exception:
            continue
    vols = vols[-20:]
    return (sum(vols) / len(vols)) if vols else 0


def elapsed_fraction(local_traded_at):
    try:
        t = datetime.fromisoformat(local_traded_at).astimezone(KST)
    except Exception:
        return 1.0
    return max(0.02, min(1.0, (t.hour * 60 + t.minute - 9 * 60) / 390.0))


def stop_loss_text():
    lines = ["[경고/손절 라인] 매수가 %s원 기준" % won(BUY_PRICE)]
    for lvl, name in STOP_LOSS_LINES:
        lines.append("- %s: %s원 (%s, %s원)" % (
            name, won(lvl), signed_pct((lvl / BUY_PRICE - 1) * 100), signed_won((lvl - BUY_PRICE) * SHARES)))
    return "\n".join(lines)


def build_body(q, trigger_label, judgement, action, vol_ratio, vol_note, kind):
    price = q["price"]
    pl = (price - BUY_PRICE) * SHARES
    ret = (price / BUY_PRICE - 1.0) * 100.0
    lines = [
        "%s %s" % (q["name"], q["code"]),
        "현재가: %s원" % won(price),
        "매수가 대비: %s" % signed_pct(ret),
        "평가손익: %s원" % signed_won(pl),
        "트리거: %s" % trigger_label,
        "구분: %s" % kind,
        "판단: %s" % judgement,
        "대응: %s" % action,
        "거래량: 20일 평균 대비 약 %.2f배%s" % (vol_ratio, (" - " + vol_note) if vol_note else ""),
        "당일: 시가 %s / 고가 %s / 저가 %s (전일종가 %s)" % (
            won(q["open"]), won(q["high"]), won(q["low"]), won(q["prev_close"])),
        "",
        stop_loss_text(),
    ]
    if TRAIL_PCT and q.get("high"):
        tl = int(round(q["high"] * (1 - TRAIL_PCT / 100.0)))
        lines += ["", "[추적 손절] 당일 고점 %s원 대비 -%s%% = %s원" % (won(q["high"]), TRAIL_PCT, won(tl))]
    return "\n".join(lines)


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        log("TELEGRAM 미설정 - 스킵")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as resp:
            out = resp.read().decode("utf-8", "replace")
        ok = '"ok":true' in out
        log("TELEGRAM %s" % ("SENT" if ok else "FAILED: " + out[:200]))
        return ok
    except Exception as e:
        log("TELEGRAM_ERROR: %s" % e)
        return False


def notify(subject, body):
    send_telegram("%s\n%s" % (subject, body))


def emit_alert(q, level, direction, kind, vol_ratio):
    judgement, action = PLAYBOOK[level]
    if direction == "up":
        tag = " [신뢰도 높은 돌파]" if vol_ratio >= VOL_RELIABLE else ""
        label = "%s원 %s%s" % (won(level), "돌파" if level in (20000, 20500, 22250) else "도달", tag)
        vol_note = "거래량 동반" if vol_ratio >= VOL_RELIABLE else "거래량 미동반(돌파 신뢰도 낮음)"
    elif level == STOP_LEVEL:
        label, vol_note = "%s원 하향 이탈 [손절선 -5%%]" % won(level), ""
    elif level == WARN_LEVEL:
        label, vol_note = "%s원 하향 이탈 [경고선 -3%%]" % won(level), ""
    else:
        label, vol_note = "%s원 하향 이탈" % won(level), ""
    body = build_body(q, label, judgement, action, vol_ratio, vol_note, kind)
    log("ALERT %s level=%s price=%s" % (direction, level, q["price"]))
    notify("[저스템] %s - %s" % (label, signed_pct((q["price"] / BUY_PRICE - 1) * 100)), body)


def main():
    now = datetime.now(KST)
    if now.weekday() >= 5:
        log("주말 - 스킵"); return
    m = now.hour * 60 + now.minute
    if REG_S <= m < REG_E:
        phase = "regular"
    elif REG_E <= m < AFT_S:
        phase = "gap"
    elif AFT_S <= m < AFT_E:
        phase = "afterhours"
    else:
        log("거래시간 외(%s) - 스킵" % now.strftime("%H:%M")); return

    today = now.strftime("%Y%m%d")
    state = load_state(today)
    up_active = state["up_active"]
    down_active = state["down_active"]

    q = fetch_quote()
    price = q["price"]
    if price <= 0:
        log("가격 없음 - 스킵"); return

    # 체결 시각 정체(휴장) 방지
    try:
        traded = datetime.fromisoformat(q["local_traded_at"]).astimezone(KST)
        if (now - traded).total_seconds() > 15 * 60:
            log("체결 정체 - 스킵"); return
    except Exception:
        pass

    avg_vol = fetch_avg_volume(today)
    frac = elapsed_fraction(q["local_traded_at"])
    vol_ratio = ((q["volume"] / frac) / avg_vol) if (avg_vol > 0 and frac > 0) else 1.0
    kind = "장중"

    if phase == "gap":
        if not state.get("close_done"):
            if price <= CLOSE_LEVEL:
                body = build_body(q, "%s원 이하 종가 마감 [손절선 -5%%]" % won(CLOSE_LEVEL),
                                  "손절선 이하 종가 마감", "손절 실행 또는 비중 축소 (추가매수 보류)",
                                  vol_ratio, "", "종가 기준")
                notify("[저스템] %s원 이하 종가 마감" % won(CLOSE_LEVEL), body)
            state["close_done"] = True
        save_state(state)
        log("gap 종가평가 완료 price=%s" % won(price)); return

    # 상향
    for lvl in UP_LEVELS:
        an = up_active.get(str(lvl), False)
        if price >= lvl and not an:
            emit_alert(q, lvl, "up", kind, vol_ratio)
            up_active[str(lvl)] = True
            state["break_seq"].append({"lvl": lvl, "vol": round(vol_ratio, 2)})
        elif price < lvl and an:
            up_active[str(lvl)] = False
            if vol_ratio < VOL_RELIABLE:
                body = build_body(q, "%s원 재이탈" % won(lvl), "가짜 돌파 가능성",
                                  "돌파 신뢰도 낮음 - 추격매수 자제", vol_ratio, "거래량 미동반", kind)
                notify("[저스템] 가짜 돌파 가능성 - %s" % won(lvl), body)

    # 하향
    for lvl in DOWN_LEVELS:
        an = down_active.get(str(lvl), False)
        if price <= lvl and not an:
            emit_alert(q, lvl, "down", kind, vol_ratio)
            down_active[str(lvl)] = True
        elif price > lvl and an:
            down_active[str(lvl)] = False

    # 추적 손절
    if TRAIL_PCT:
        peak = max(state.get("peak_price") or 0, q["high"] or price, price)
        if peak > (state.get("peak_price") or 0):
            state["peak_price"] = peak
            state["trail_armed"] = True
        trail_level = int(round(peak * (1 - TRAIL_PCT / 100.0)))
        if state.get("trail_armed", True) and price <= trail_level:
            body = build_body(q, "추적 손절 발동 (고점 %s원 대비 -%s%%)" % (won(peak), TRAIL_PCT),
                              "고점 대비 -%s%% 되돌림 → 이익 반납 경계" % TRAIL_PCT,
                              "추적 손절: 이익 보호를 위해 비중 축소/청산 검토", vol_ratio, "", kind)
            log("TRAIL_STOP peak=%s level=%s price=%s" % (won(peak), won(trail_level), won(price)))
            notify("[저스템] 추적 손절 발동 - 고점 %s원 대비 -%s%%" % (won(peak), TRAIL_PCT), body)
            state["trail_armed"] = False

    state["last_price"] = price
    state["last_update"] = now.isoformat()
    save_state(state)
    log("tick[%s] price=%s volRatio=%.2f up=%s down=%s" % (
        phase, won(price), vol_ratio,
        ",".join(k for k, v in up_active.items() if v),
        ",".join(k for k, v in down_active.items() if v)))


if __name__ == "__main__":
    main()
