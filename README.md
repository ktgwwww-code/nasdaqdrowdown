# NASDAQ100 ドローダウン監視ツール

NASDAQ100指数（`^NDX`）の **観測史上最高値からの下落率** を毎日自動で監視し、
閾値（**−25% / −30% / −40% / −50%**）に到達したときだけ Discord に通知する。

> 普段は沈黙。暴落だけがあなたを起こす。

詳しい設計は [仕様書](nasdaq_drawdown_monitor_spec.md) を参照。

## 仕組み

1. GitHub Actions の cron が1日1回（米国引け後）スクリプトを起動。
2. `yfinance` で `^NDX` を全期間取得し、史上最高終値と最新終値を求める。
3. `state.json` の `all_time_high` を更新（新高値なら `fired_level` を 0 にリセット）。
4. ドローダウンを計算し、到達した閾値が前回発火レベルを **超えたときだけ** Discord へ通知。
5. `state.json` を更新してコミット＆プッシュ。

一度発火した閾値は、史上最高値が更新されるまで再通知しない（暴落中の連日通知を防ぐ）。

## セットアップ

1. このディレクトリを GitHub リポジトリにする。

   ```bash
   git init
   git add .
   git commit -m "init: NASDAQ100 drawdown monitor"
   git branch -M main
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. Discord でチャンネルの Webhook URL を発行。
3. GitHub リポジトリの **Settings → Secrets and variables → Actions** に
   `DISCORD_WEBHOOK_URL` を登録する。
4. **Actions** タブを開き、ワークフローを有効化。`Run workflow` で手動テスト可能。

## ローカルでの動作確認

```bash
pip install -r requirements.txt

# Webhook を設定すれば実際に通知が飛ぶ（未設定なら本文をログ出力するだけ）
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."   # 任意
python monitor.py
```

`DISCORD_WEBHOOK_URL` を設定せずに実行すると、通知は送らず「送るはずだった本文」を
標準出力に表示する（ドライラン）。

## state.json

| キー | 意味 |
|---|---|
| `all_time_high` | 観測した史上最高値（終値ベース） |
| `fired_level` | 直近で通知済みの閾値（0/25/30/40/50）。0 = 未発火 |
| `last_run` | 最終実行日（UTC） |
| `last_price` | 最終取得価格 |
| `fail_count` | データ取得の連続失敗回数 |
| `fail_alerted` | 連続失敗を通知済みか |

## cron 時刻について

`.github/workflows/monitor.yml` の cron は `0 0 * * 2-6`（UTC 0:00 = 日本 9:00）。
米国の夏時間／冬時間で引けの UTC がずれるため、終値がきちんと取れているかを
ログで確認して時刻を調整すること。
