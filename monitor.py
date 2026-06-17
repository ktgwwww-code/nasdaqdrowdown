#!/usr/bin/env python3
"""NASDAQ100 ドローダウン監視ツール

^NDX（NASDAQ100指数）の「観測史上最高値からの下落率」を監視し、
閾値（-25% / -30% / -40% / -50%）に到達したときだけ Discord に通知する。

設計思想（仕様書 §10）:
- 普段は沈黙、暴落だけが起こす。
- 判定は原指数 ^NDX、行動はレバ比率の組み替えのみ（売却指示は出さない）。
- 一度発火した閾値は、史上最高値が更新されるまで再通知しない（§3.4）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


# --- 定数 -----------------------------------------------------------------

TICKER = "^NDX"
STATE_PATH = Path(__file__).with_name("state.json")

# 取得失敗が何日連続したら「監視が死んでいる」と通知するか（§8）
FAILURE_ALERT_THRESHOLD = 3

# 閾値（下落率%）。判定は大きい順に行う。
THRESHOLDS = [50, 40, 30, 25]

# 閾値ごとのアクション文（§3.3 / §6.2）
ACTIONS: dict[int, dict[str, str]] = {
    25: {
        "headline": "レバを増額",
        "body": "通常フェーズ「等倍30・レバ5」→「等倍30・レバ10」へ組み替え（L≒1.25）",
        "extra": "",
    },
    30: {
        "headline": "レバをさらに増額",
        "body": "「等倍30・レバ15」へ組み替え（L≒1.33）",
        "extra": "",
    },
    40: {
        "headline": "レバ大幅増額",
        "body": "「等倍30・レバ25」へ組み替え（L≒1.45）",
        "extra": "妻のオルカン資金をレバナスへ寄せる検討開始。",
    },
    50: {
        "headline": "レバ最大",
        "body": "「等倍30・レバ30」へ組み替え（L≒1.50）",
        "extra": "妻資金フル投入。妻の同意を取る。",
    },
}

FOOTER = (
    "※ 判定は原指数 ^NDX。レバナス基準価額ではない。\n"
    "※ 売らない。安く拾えるボーナス。握る論理を思い出すこと。"
)


# --- state 入出力 ----------------------------------------------------------

def default_state() -> dict:
    return {
        "all_time_high": 0.0,
        "fired_level": 0,
        "last_run": None,
        "last_price": None,
        "fail_count": 0,
        "fail_alerted": False,
    }


def load_state() -> dict:
    """state.json を読む。無い／壊れている場合は初期値を返す（§8）。"""
    if not STATE_PATH.exists():
        return default_state()
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] state.json を読めませんでした（初期化します）: {e}")
        return default_state()

    state = default_state()
    state.update(data)
    return state


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- データ取得 ------------------------------------------------------------

def fetch_ndx() -> tuple[float, float, date]:
    """^NDX を period="max" で取得し、(史上最高終値, 最新終値, 最新営業日) を返す。

    取得失敗時は RuntimeError を送出する。
    """
    if yf is None:
        raise RuntimeError("yfinance がインストールされていません")

    hist = yf.Ticker(TICKER).history(period="max", auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise RuntimeError("yfinance から終値データを取得できませんでした")

    closes = hist["Close"].dropna()
    if closes.empty:
        raise RuntimeError("終値系列が空でした")

    hist_high = float(closes.max())
    current_price = float(closes.iloc[-1])
    last_index = closes.index[-1]
    last_date = last_index.date() if hasattr(last_index, "date") else date.today()
    return hist_high, current_price, last_date


# --- 判定ロジック ----------------------------------------------------------

def level_for(drawdown: float) -> int:
    """下落率から到達している最大の閾値を返す（§5 step7）。"""
    for th in THRESHOLDS:
        if drawdown >= th:
            return th
    return 0


# --- Discord 通知 ----------------------------------------------------------

def post_discord(webhook_url: str, content: str) -> None:
    resp = requests.post(webhook_url, json={"content": content}, timeout=30)
    resp.raise_for_status()


def build_alert_message(all_time_high: float, current_price: float,
                        drawdown: float, level: int) -> str:
    act = ACTIONS[level]
    lines = [
        "🔴 **NASDAQ100 ドローダウン警報**",
        "",
        f"史上最高値: {all_time_high:,.0f}",
        f"現在値: {current_price:,.0f}",
        f"下落率: −{drawdown:.1f}%（到達閾値: −{level}%）",
        "",
        f"▶ アクション: {act['headline']}",
        f"　{act['body']}",
        "　原資: 妻のオルカン資金をレバナスへ振り替え",
    ]
    if act["extra"]:
        lines.append(f"　{act['extra']}")
    lines += ["", FOOTER]
    return "\n".join(lines)


def build_failure_message(fail_count: int) -> str:
    return (
        "⚠️ **NASDAQ100 監視ツール 異常**\n\n"
        f"データ取得に {fail_count} 日連続で失敗しています。\n"
        "監視が機能していない可能性があります。GitHub Actions のログを確認してください。"
    )


# --- メイン ---------------------------------------------------------------

def run() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    today = datetime.now(timezone.utc).date().isoformat()

    state = load_state()

    # --- データ取得（失敗時は通知せず正常終了。連続失敗のみ通知）§8 ---
    try:
        hist_high, current_price, last_date = fetch_ndx()
    except RuntimeError as e:
        state["fail_count"] = int(state.get("fail_count", 0)) + 1
        state["last_run"] = today
        print(f"[error] データ取得失敗（{state['fail_count']}日連続）: {e}")

        if state["fail_count"] >= FAILURE_ALERT_THRESHOLD and not state.get("fail_alerted"):
            if webhook_url:
                try:
                    post_discord(webhook_url, build_failure_message(state["fail_count"]))
                    state["fail_alerted"] = True
                    print("[info] 連続失敗を Discord に通知しました")
                except requests.RequestException as pe:
                    print(f"[error] 失敗通知の送信にも失敗: {pe}")
            else:
                print("[warn] DISCORD_WEBHOOK_URL 未設定のため失敗通知を送れません")

        save_state(state)
        return 0

    # 取得成功 → 失敗カウンタをリセット
    state["fail_count"] = 0
    state["fail_alerted"] = False

    # --- 史上最高値の更新（§5 step4-5） ---
    prev_high = float(state.get("all_time_high") or 0.0)
    all_time_high = max(prev_high, hist_high, current_price)

    if all_time_high > prev_high:
        # 新高値で再装填（リセット）
        if state.get("fired_level", 0):
            print(f"[info] 新高値更新 {prev_high:,.0f} → {all_time_high:,.0f}。fired_level リセット")
        state["fired_level"] = 0

    # --- ドローダウン算出と判定（§5 step6-8） ---
    drawdown = (all_time_high - current_price) / all_time_high * 100 if all_time_high > 0 else 0.0
    level = level_for(drawdown)
    fired_level = int(state.get("fired_level", 0))

    print(f"[info] high={all_time_high:,.2f} price={current_price:,.2f} "
          f"drawdown=-{drawdown:.2f}% level={level} fired_level={fired_level}")

    notified = False
    if level > fired_level:
        message = build_alert_message(all_time_high, current_price, drawdown, level)
        if webhook_url:
            try:
                post_discord(webhook_url, message)
                state["fired_level"] = level
                notified = True
                print(f"[info] −{level}% 警報を Discord に通知しました")
            except requests.RequestException as e:
                # 通知失敗時は fired_level を更新しない（後日リトライさせる）
                print(f"[error] Discord 通知に失敗: {e}")
        else:
            print("[warn] DISCORD_WEBHOOK_URL 未設定。以下のメッセージを送るはずでした:\n" + message)

    if not notified and level <= fired_level:
        print("[info] 新規発火なし（沈黙）")

    # --- state 保存（§5 step9） ---
    state["all_time_high"] = all_time_high
    state["last_price"] = current_price
    state["last_run"] = today
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
