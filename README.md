# ai-cat-reels

Instagram で伸びている AI 猫 Reels の傾向を分析し、Kling API で新しい猫動画を 1 日 1 本生成して、Instagram にリール投稿する CLI ツール。

詳細設計は [docs/DESIGN.md](docs/DESIGN.md) を参照。

## セットアップ (初回 1 回だけ)

```bash
# 1) 仮想環境
python3.11 -m venv .venv    # 3.11/3.12/3.13 いずれでも可
source .venv/bin/activate
pip install -r requirements.txt

# 2) ffmpeg (分析用のコマ切り出しに使う)
brew install ffmpeg    # macOS

# 3) 設定
cp .env.example .env
# .env を編集してトークン・キーを埋める

# 4) 初期化 (DB とフォルダ作成、.env 必須項目チェック)
python -m app.main init
```

## 一番よく使うコマンド

**[1] いつもの毎日運用** (plan → generate → publish 一気通貫):

```bash
python -m app.main daily
```

同日に既に投稿済みなら skip されるので再実行しても安全。約 **5〜10 分で完了** (Kling のエンコード時間が支配的)。

**[2] 週 1〜2 回: 参考データ更新も含める** (collect → analyze → plan → generate → publish):

```bash
python -m app.main daily --with-collect
```

ハッシュタグの収集枠 (7 日/30 ユニーク) を使うため、毎日の実行には向かない。週 1〜2 回で十分。

**[3] Instagram に投稿せずに動作確認**:

```bash
python -m app.main daily --dry-run
```

Kling は実際に生成する (料金発生) が、Instagram 投稿だけスキップ。生成結果は `data/videos/*.mp4` に残る。

## 個別コマンド (トラブル調査・部分再実行用)

毎日は `daily` を使えば OK ですが、どこかで失敗した / 中身を確認したい時に個別に呼べます。

| コマンド | 内容 | 使いどき |
| --- | --- | --- |
| `python -m app.main init` | DB・フォルダ作成、.env 必須項目チェック | 初回 / .env 変更後 |
| `python -m app.main collect` | 参考 Reels の収集 (ハッシュタグ + 監視アカウント) | 週 1〜2 回、設定変更後 |
| `python -m app.main analyze --limit 10` | 未分析 Reels を Claude vision で分析 (URL ありを優先) | `collect` の後 |
| `python -m app.main plan` | 今日のネタと Kling プロンプトを生成 | 任意のタイミング |
| `python -m app.main generate` | 直近の planned 行を Kling で生成・DL | plan の後 |
| `python -m app.main generate --generation-id 42` | 特定の plan を指定して生成 | 失敗した plan をリトライ |
| `python -m app.main publish` | 直近の generated 行を IG に投稿 | generate の後 |
| `python -m app.main publish --dry-run` | 投稿せずキャプション・動画パスだけ表示 | 投稿内容の最終確認 |
| `python -m app.main metrics` | 自分の過去投稿のいいね・コメント数を更新 | 毎日 (daily に含まれる) |

**注意**: `--dry-run` は **サブコマンドの後ろ** に書く。`--dry-run publish` のようにサブコマンド前に書くとパースエラー。

## 定期実行 (cron)

```bash
# macOS / Linux: `crontab -e` で登録
# 毎日 19:00 に通常運用
0 19 * * * cd /Users/shimizu/ai-cat-reels && .venv/bin/python -m app.main daily >> logs/cron.log 2>&1
# 月・木だけ参考データも更新
0 18 * * 1,4 cd /Users/shimizu/ai-cat-reels && .venv/bin/python -m app.main daily --with-collect >> logs/cron.log 2>&1
```

PC がスリープ中は動かない点に注意。

## パイプラインの中身

```
┌──────────┐                                                    
│ collect  │ ─── hashtag recent_media + REELS フィルタ          
│          │     Business Discovery (accounts.txt のアカウント) 
│          │ ──> reference_posts テーブルに保存                  
└──────────┘                                                    
      ↓                                                         
┌──────────┐                                                    
│ analyze  │ ─── media_url があれば ffmpeg で 4 コマ抽出 → Claude vision
│          │     URL 無しなら caption-only (フォールバック)     
│          │ ──> analyses テーブルに JSON (situation/hook/punchline/cat_type/format_tags…)
└──────────┘                                                    
      ↓                                                         
┌──────────┐                                                    
│  plan    │ ─── 直近 14 日 analyses + 自分の 30 日分の投稿 → Claude
│          │     英語キャプション + #AIGenerated + 小太り茶トラ指定を強制
│          │ ──> generations テーブルに planned 行                
└──────────┘                                                    
      ↓                                                         
┌──────────┐                                                    
│ generate │ ─── Kling text2video (9:16, 5s) → MP4 DL             
│          │ ──> generations.status = generated / video_path 保存
└──────────┘                                                    
      ↓                                                         
┌──────────┐                                                    
│ publish  │ ─── IG Graph API: container → upload → wait → publish
│          │     is_ai_generated=true、quota 事前チェック        
│          │ ──> posts テーブルに ig_media_id + permalink 保存     
└──────────┘                                                    
```

## スタイル指定 (将来変更する場合)

今の設定は「英語キャプション + 小太り茶トラ猫」。変えたい時は `app/planner.py` 冒頭の 3 定数だけ編集:

```python
CAT_DESCRIPTION = "a small chubby cute brown tabby cat (slightly plump, round face, warm brown fur with classic tabby stripes)"
CAPTION_LANGUAGE = "English"
AI_LABEL_TAG = "#AIGenerated"
```

システムプロンプト全体に自動反映される。

## config/ の設定ファイル

| ファイル | 内容 | 枠 |
| --- | --- | --- |
| `config/hashtags.txt` | 収集ハッシュタグ (1行1つ、# なし) | **7 日間でユニーク 30 個まで**。10 個以内に抑える |
| `config/accounts.txt` | 監視する Instagram ユーザー名 (Business Discovery) | 枠制限なし。相手が Business/Creator である必要 |

ハッシュタグは **top_media ではなく recent_media** で取るため直近 24h 内の投稿のみ。古く伸びた Reels を取りたければ別路線 (未実装)。

## テスト

```bash
python -m pytest -v
```

外部 API (Instagram / Kling / Claude) はすべてモック済。全 47 テスト。

実 API での動作確認は `.env` 設定後、以下の順:

```bash
python -m app.main init                    # .env 完備チェック
python -m app.main collect                 # 実 Instagram API 消費
python -m app.main analyze --limit 3       # 実 Claude 消費 ($0.05〜程度)
python -m app.main plan                    # 実 Claude 消費 ($0.01〜程度)
python -m app.main generate                # 実 Kling 消費 ($0.15〜0.30/本)
python -m app.main publish --dry-run       # 投稿せず内容確認
python -m app.main publish                 # 実投稿
```

## 設計書からの差分

公式ドキュメント確認時点 (2026-11-09) と実運用の中で判明した事項を反映。

- **Graph API バージョン**: 設計書 `v25.0` → 現行 `v23.0` 以上を推奨
- **コンテナステータス ポーリング**: 設計書「10 秒間隔・最大 10 分」→ 実装は **30 秒間隔・最大 10 分**
- **Business Discovery の権限**: `instagram_manage_insights` の追加が必要
- **`is_ai_generated=true`**: コンテナ作成時に付与
- **Kling モデル**: `kling-v2-6` もしくは `kling-v3-0` を .env で指定
- **Kling 認証方式**: 設計書は AK+SK+JWT のみ。実装は **新方式 (`KLING_API_KEY`) 優先、AK+SK はフォールバック**
- **task_status**: `submitted` / `processing` 中間状態も待機対象
- **Claude モデル名**: 設計書 `claude-sonnet-5-5` → 現行 `claude-sonnet-4-6`
- **ハッシュタグは `recent_media` を使う**: `top_media` は仕様上 Reels を返さない (FEED のみ) ため
- **Reels フィルタ**: `media_product_type == REELS` で絞り込み、通常 Feed 動画や広告を除外
- **analyze の優先順**: `media_url` があるものを先に処理 (コマ抽出可能)。URL 枯れたら caption-only フォールバック
- **キャプション/猫の指定**: `app/planner.py` の 3 定数で制御。英語キャプション + 小太り茶トラ猫をデフォルトに
- **`--dry-run` の位置**: サブコマンドの**後ろ** (`publish --dry-run`)。前に書くとパースエラー

## ディレクトリ構成

```
ai-cat-reels/
├─ .env.example
├─ requirements.txt
├─ README.md
├─ config/
│  ├─ hashtags.txt
│  └─ accounts.txt
├─ app/
│  ├─ main.py             # CLI 入口
│  ├─ config.py           # .env 読み込み
│  ├─ db.py               # SQLite 接続・DDL
│  ├─ instagram_client.py # Graph API
│  ├─ collector.py        # 収集・スコアリング (Reels only)
│  ├─ analyzer.py         # ffmpeg 4 コマ + Claude vision
│  ├─ planner.py          # ネタ + Kling プロンプト生成 (英語/茶トラ)
│  ├─ kling_client.py     # Kling API (JWT と API Key 両対応)
│  └─ publisher.py        # リール投稿フロー
├─ data/
│  ├─ app.db
│  ├─ tmp/                # 分析中の一時ファイル (分析後に自動削除)
│  └─ videos/             # Kling 生成動画の保存先
├─ logs/
│  └─ app.log             # 全実行ログ
├─ docs/
│  └─ DESIGN.md
└─ tests/
```
