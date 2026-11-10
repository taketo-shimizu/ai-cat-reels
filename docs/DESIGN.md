# AI猫動画 自動生成・自動投稿システム 設計書

Nov 9, 2026 · @Taketo

## 1. 概要

Instagramで伸びているAI猫動画の傾向を分析し、Kling APIで新しい猫動画を1日1本生成して、Instagramにリールとして自動投稿する。まずはローカルPCでコマンド1つで動く簡易版を作る。

**スコープ(今回作るもの)**

- Instagram APIによる人気AI猫動画の収集(ハッシュタグ検索、指定アカウント監視)
- 収集した動画の分析(LLMの画像認識でネタ・構成を抽出)
- 分析結果と過去の投稿をもとにした、新しいネタとKling用プロンプトの生成
- Kling APIによる動画生成とダウンロード
- Instagram APIによるリール投稿(ローカルファイルのresumable upload)
- 投稿履歴と自分の投稿の反応(いいね・コメント数)の記録

**対象外(今回は作らない)**

- 人による承認ステップ(LINE/Slack通知)
- ffmpegによるテロップ・BGMの合成
- クラウド上での定期実行(ローカルのcronやタスクスケジューラで十分とする)
- 管理画面・Web UI

## 2. 全体フロー

処理は2つの流れに分かれる。「収集と分析」はハッシュタグの制限があるため週に1〜2回だけ行い、「生成と投稿」は毎日行う。2つの流れはSQLiteを介してつながる。

&#91;embedded content: 処理の流れ · 週次の分析と毎日の投稿\]

毎日の流れでは、まず前日までの自分の投稿の反応を記録する。そのうえで、伸びた型と過去の投稿をもとにネタを決め、動画を生成して投稿する。

## 3. 技術スタック・構成・設定

Python 3.11以上で、外部サービスは Instagram Graph API・Kling API・Claude API の3つだけを使う。データはローカルのSQLiteに保存する。

| 用途 | 採用するもの |
| --- | --- |
| 言語 | Python 3.11+ |
| HTTP | requests |
| Kling認証(JWT) | PyJWT |
| 分析・プロンプト生成 | anthropic SDK(Claude、画像入力あり) |
| 動画からのコマ切り出し | ffmpeg(分析用のみ) |
| DB | sqlite3(標準ライブラリ) |
| 設定 | python-dotenv |
| CLI | argparse(標準ライブラリ) |
| ログ | logging(コンソール + logs/app.log) |

**ディレクトリ構成**

```
ai-cat-reels/
├─ .env.example
├─ requirements.txt
├─ README.md
├─ config/
│  ├─ hashtags.txt        # 収集対象のハッシュタグ(1行1つ、#なし)
│  └─ accounts.txt        # 監視するAI猫アカウントのユーザー名
├─ app/
│  ├─ main.py             # CLIの入口
│  ├─ config.py           # .env読み込み
│  ├─ db.py               # SQLite接続・テーブル作成
│  ├─ instagram_client.py # Graph API(収集・投稿・自分の投稿の反応)
│  ├─ collector.py        # 人気動画の収集とスコアリング
│  ├─ analyzer.py         # コマ切り出し + Claudeで分析
│  ├─ planner.py          # 今日のネタとKlingプロンプトを生成
│  ├─ kling_client.py     # Kling API(JWT・生成・ポーリング・DL)
│  └─ publisher.py        # リール投稿の一連の流れ
├─ data/
│  ├─ app.db
│  ├─ tmp/                # 分析用の一時ファイル(分析後に削除)
│  └─ videos/             # 生成した自分の動画
└─ logs/
```

**設定(.env)**

| 変数 | 内容 | 例 |
| --- | --- | --- |
| IG\_ACCESS\_TOKEN | Facebookログイン経由の長期ユーザーアクセストークン | EAAG... |
| IG\_USER\_ID | 自分のInstagramビジネスアカウントID | 1784... |
| IG\_GRAPH\_VERSION | Graph APIのバージョン | v25.0 |
| KLING\_ACCESS\_KEY | Kling開発者コンソールのAccess Key |  |
| KLING\_SECRET\_KEY | 同 Secret Key |  |
| KLING\_BASE\_URL | Kling APIのドメイン | https://api-singapore.klingai.com |
| KLING\_MODEL | 使うモデル名(コンソールで確認) | kling-v2-6 |
| KLING\_MODE | std / pro | std |
| KLING\_DURATION | 秒数 | 5 |
| ANTHROPIC\_API\_KEY | Claude APIキー |  |
| CLAUDE\_MODEL | 分析・生成に使うモデル | claude-sonnet-5-5 |
| DRY\_RUN | trueならInstagramに投稿せず動画生成まで | false |

## 4. 各モジュール仕様

Instagramはハッシュタグ検索とBusiness Discoveryを使うため、「Instagram API with Facebook Login」(graph.facebook.com)側で実装する。必要な権限は instagram\_basic・instagram\_content\_publish・pages\_show\_list・pages\_read\_engagement と、機能「Instagram Public Content Access」。

### 4.1 instagram\_client.py

| メソッド | API | 備考 |
| --- | --- | --- |
| search\_hashtag\_id(tag) | GET /ig\_hashtag\_search?user\_id={IG\_USER\_ID}&q={tag} | 取得したIDはDBにキャッシュし、上限を消費しない |
| hashtag\_top\_media(hashtag\_id) | GET /{hashtag\_id}/top\_media?user\_id=...&fields=id,caption,media\_type,media\_url,permalink,like\_count,comments\_count,timestamp | media\_type=VIDEOのみ使う |
| hashtag\_recent\_media(hashtag\_id) | GET /{hashtag\_id}/recent\_media(fieldsは同上) | 直近24時間分 |
| account\_media(username) | GET /{IG\_USER\_ID}?fields=business\_discovery.username({u}){followers\_count,media{id,caption,media\_type,media\_url,permalink,like\_count,comments\_count,view\_count,timestamp}} | 直近25件程度 |
| my\_media() | GET /{IG\_USER\_ID}/media?fields=id,caption,like\_count,comments\_count,timestamp,permalink | 自分の投稿の反応取得 |
| create\_reel\_container\_resumable(caption) | POST /{IG\_USER\_ID}/media media\_type=REELS, upload\_type=resumable, caption, share\_to\_feed=true | 戻り値の id を使う |
| upload\_video(container\_id, path) | POST https://rupload.facebook.com/ig-api-upload/{ver}/{container\_id} ヘッダ: Authorization: OAuth {token}, offset: 0, file\_size: {bytes}、ボディ: 動画バイナリ | ローカルファイルを直接送るので公開ストレージ不要 |
| wait\_container(container\_id) | GET /{container\_id}?fields=status\_code | FINISHEDまで10秒間隔で最大10分待つ。ERROR/EXPIREDは失敗 |
| publish(container\_id) | POST /{IG\_USER\_ID}/media\_publish creation\_id | 投稿後のmedia idを返す |
| publishing\_limit() | GET /{IG\_USER\_ID}/content\_publishing\_limit | 投稿前に確認 |

### 4.2 collector.py

1. config/hashtags.txt と config/accounts.txt を読む(初期値例: aicat, ai猫, aicats, aianimals)。
2. 各ハッシュタグのtop\_mediaと、各アカウントの投稿を取得する。動画以外と、すでに取得済みのmedia idは飛ばす。
3. スコア = (like\_count + comments\_count × 3) ÷ 投稿からの経過時間(h, 最小1)。view\_countがあれば view\_count ÷ 経過時間 も保存する。
4. reference\_postsテーブルに保存する。media\_urlも保存するが、動画自体はこの段階ではダウンロードしない。

### 4.3 analyzer.py

1. 未分析のreference\_postsをスコア順に最大10件選ぶ。
2. media\_urlからdata/tmpにダウンロードし、ffmpegで等間隔に4枚のコマをJPEGで切り出す。media\_urlがない投稿はキャプションだけで分析する。
3. コマ画像とキャプションをClaudeに渡し、JSONで返させる。項目: situation(場面)、hook(最初の1秒のつかみ)、punchline(オチ)、camera\_style(防犯カメラ風、スマホ風など)、cat\_type(猫の種類・毛色)、format\_tags(例: 擬人化, ハプニング, おあずけ)、why\_it\_works(伸びた理由の推測)。
4. 結果をanalysesテーブルに保存し、data/tmpのファイルは必ず削除する。

### 4.4 planner.py

1. 直近14日のanalyses上位20件、自分の過去30日の投稿(ネタ・プロンプト・反応)を集める。
2. Claudeに渡し、JSONで1件返させる。項目: idea\_ja(ネタの要約)、kling\_prompt(英語)、negative\_prompt、caption\_ja(ハッシュタグ込み、2,200文字以内)、based\_on(参考にしたformat\_tags)。
3. システムプロンプトで次を必ず指示する。「参考にするのは伸びている型だけ。特定の投稿の構図・ストーリー・キャラクターを再現しない。」「自分の過去の投稿と同じネタを避ける。」「9:16、5秒、動きは1〜2個に絞る。」
4. キャプションの末尾に「#AI生成」を必ず付ける。

### 4.5 kling\_client.py

- 認証: JWT(HS256)。ヘッダ alg=HS256, typ=JWT、ペイロード iss=Access Key, exp=現在+1800秒, nbf=現在−5秒、Secret Keyで署名。Authorization: Bearer {token}。リクエストごとに作り直してよい。
- 生成: POST {KLING\_BASE\_URL}/v1/videos/text2video、ボディ model\_name, prompt, negative\_prompt, mode, duration(文字列 "5"), aspect\_ratio="9:16"。戻り値の data.task\_id を保存。
- ポーリング: GET /v1/videos/text2video/{task\_id} を15秒間隔で最大15分。task\_status が succeed なら task\_result.videos\[0\].url を取得、failed なら task\_status\_msg を記録して失敗。
- ダウンロード: 動画URLは短時間で失効するため、完了直後に data/videos/{yyyymmdd}\_{task\_id}.mp4 へ保存する。
- パラメータ名・モデル名は実装前にKling公式のAPIリファレンスで確認する。

### 4.6 publisher.py

1. DRY\_RUN=trueなら動画のパスとキャプションを表示して終了する。
2. publishing\_limitを確認し、上限なら中止する。
3. コンテナ作成 → アップロード → FINISHED待ち → media\_publish の順に実行する。
4. 結果(ig\_media\_id, permalink)をpostsテーブルに保存する。

## 5. データモデル(SQLite: data/app.db)

テーブルは5つ。初回起動時に db.py が CREATE TABLE IF NOT EXISTS で作る。

```sql
CREATE TABLE hashtags (
  name TEXT PRIMARY KEY,          -- 例: aicat
  hashtag_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE reference_posts (
  media_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,           -- 'hashtag:aicat' / 'account:xxxx'
  caption TEXT,
  media_url TEXT,
  permalink TEXT,
  like_count INTEGER,
  comments_count INTEGER,
  view_count INTEGER,
  posted_at TEXT,
  score REAL,
  collected_at TEXT NOT NULL,
  analyzed INTEGER DEFAULT 0
);

CREATE TABLE analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id TEXT REFERENCES reference_posts(media_id),
  result_json TEXT NOT NULL,      -- situation, hook, punchline, camera_style, cat_type, format_tags, why_it_works
  created_at TEXT NOT NULL
);

CREATE TABLE generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_ja TEXT,
  kling_prompt TEXT NOT NULL,
  negative_prompt TEXT,
  caption_ja TEXT,
  based_on TEXT,                  -- JSON配列
  kling_task_id TEXT,
  status TEXT NOT NULL,           -- planned / generating / generated / failed / posted
  video_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE posts (
  ig_media_id TEXT PRIMARY KEY,
  generation_id INTEGER REFERENCES generations(id),
  permalink TEXT,
  posted_at TEXT NOT NULL,
  like_count INTEGER,
  comments_count INTEGER,
  metrics_updated_at TEXT
);
```

## 6. CLIコマンドと実行方法

各工程を単独で動かせるコマンドと、毎日の流れをまとめて動かす `daily` を用意する。

| コマンド | 内容 | 想定する頻度 |
| --- | --- | --- |
| python -m app.main init | DB・フォルダ作成、.envの必須項目チェック | 初回のみ |
| python -m app.main collect | 人気動画の収集 | 週1〜2回 |
| python -m app.main analyze \[--limit 10\] | 未分析の参考動画を分析 | collectの後 |
| python -m app.main plan | 今日のネタとプロンプトを生成して表示 | 毎日(dailyに含む) |
| python -m app.main generate \[--generation-id N\] | Klingで動画生成・ダウンロード | 毎日(dailyに含む) |
| python -m app.main publish \[--generation-id N\] | 生成済み動画をリール投稿 | 毎日(dailyに含む) |
| python -m app.main metrics | 自分の投稿のいいね・コメント数を更新 | 毎日(dailyに含む) |
| python -m app.main daily \[--with-collect\] | metrics → (collect → analyze) → plan → generate → publish | 毎日1回 |

全コマンドで `--dry-run` を受け付け、指定時はInstagramへの投稿を行わない。

**定期実行(任意)**

- Mac / Linux: `crontab -e` で `0 19 * * * cd /path/to/ai-cat-reels && .venv/bin/python -m app.main daily >> logs/cron.log 2>&1`
- Windows: タスクスケジューラで同じコマンドを毎日実行する
- PCがスリープ中は動かない。同じ日に二重投稿しないよう、dailyは「今日すでにpostedがあれば何もしない」。

## 7. エラー処理・制限・注意事項

設計で一番効いてくるのは、ハッシュタグ検索の「7日で30種類まで」という制限。収集は固定の少数のタグを使い回す。

**APIの制限**

| 対象 | 制限 | 対応 |
| --- | --- | --- |
| ハッシュタグ検索 | 7日間でユニーク30タグまで | hashtags.txtは10個以内。hashtag\_idをキャッシュ |
| 他人のリールのmedia\_url | 投稿者がダウンロードをオフにしていると返らない | キャプションだけで分析 |
| コンテンツ投稿 | 24時間で100件まで | 投稿前にcontent\_publishing\_limitを確認 |
| コンテナ | 24時間で期限切れ | 作成したらすぐ投稿する |
| リール動画 | 最大90秒、H.264のMP4/MOV | Kling出力はそのままで通る想定。失敗時はffmpegで再エンコード |
| アクセストークン | 長期トークンは約60日で失効 | 期限切れエラーを検知したら更新手順をログに出す |
| Kling動画URL | 短時間で失効 | 完了直後にダウンロード |
| Kling APIの支払い | プリペイドのパッケージ。Web版の無料クレジットは使えない | 残高不足エラーはそのまま中止してログに出す |

**エラー処理の方針**

- HTTP 429・5xxは指数バックオフで最大3回リトライ。401/403と残高不足はリトライしない。
- 1件の参考動画の分析失敗で全体を止めない(スキップして続行)。
- 動画生成が失敗したら、同じプロンプトで最大1回だけ再生成する。それでも失敗なら generations.status=failed にしてその日は投稿しない。
- どの工程も、途中で止まっても再実行で続きから動くようにする(statusを見て判断)。
- トークンやキーはログに出さない。

**運用上の注意**

- 参考動画は分析にだけ使い、分析後に削除する。他人の動画のコマをKlingに入力したり、構図やオチを再現したりしない。
- 投稿にはAI生成であることを明示する(キャプションに #AI生成)。InstagramのAIラベルがAPI投稿でどう扱われるかは、運用開始前に確認する。
- 承認ステップがないため、崩れた動画も投稿されうる。運用開始後しばらくは DRY\_RUN=true で動かし、出来を目で確認してから自動投稿に切り替える。

## 8. Claude Codeへの実装指示・受け入れ基準

この設計書をMarkdownで書き出し、リポジトリに `docs/DESIGN.md` として置いてから、下の指示文をClaude Codeに渡す。

```markdown
docs/DESIGN.md の設計に従って、ai-cat-reels を実装してください。
- 実装前に Kling API と Instagram Graph API の公式ドキュメントで、エンドポイント・パラメータ名・モデル名を確認し、設計書と違う点があれば報告してから進めてください。
- モジュールごとに実装し、外部APIはモックした pytest を付けてください。
- 実際のAPIを呼ぶ確認は、私が .env を用意してから、--dry-run で行います。
```

**実装の順番**

1. config.py / db.py / main.py(init)
2. kling\_client.py → generate が単独で動くことを確認
3. instagram\_client.py + publisher.py → 手元のMP4を publish できることを確認
4. collector.py → analyzer.py → planner.py
5. daily と metrics

**受け入れ基準**

- [ ] `init` でDBとフォルダが作られ、.envに足りない項目があれば名前付きで表示される
- [ ] `collect` で reference\_posts に動画投稿が保存され、同じ投稿が重複しない
- [ ] `analyze` で analyses にJSONが保存され、data/tmp が空になる
- [ ] `plan` で英語プロンプトと日本語キャプション(#AI生成 付き)が表示される
- [ ] `generate` で data/videos に 9:16・約5秒のMP4が保存される
- [ ] `publish --dry-run` で投稿せずに内容が表示される
- [ ] `publish` でリールが投稿され、posts に permalink が保存される
- [ ] `daily` を同じ日に2回動かしても投稿は1本だけ
- [ ] ログにトークン・キーが出ない

**未確定事項**

- Kling 3.0系の model\_name(コンソールで確認。公式ドキュメントの例は kling-v2-6)
- 自分のアカウントがInstagram Public Content Accessを使えるか(使えなければ、アカウント監視だけで動かす)
- 監視するAI猫アカウントのリスト

**参考**

- [Instagram Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- [Instagram Hashtag Search](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/hashtag-search)
- [Instagram Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)
- [Kling AI API Pricing(Atlas Cloud)](https://www.atlascloud.ai/blog/tips/kling-ai-api-pricing)
- [Kling AI Text-to-Video API(apis.io)](https://apis.io/apis/kling-ai/kling-ai-text-to-video-api/)
