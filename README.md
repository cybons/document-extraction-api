# document-extraction-api

PDF・画像を Claude API で読み取り、項目を抜き出して JSON で返す社内共通の REST API。PAD や Python から呼ぶ。

- 要件の原文: [docs/requirements.md](docs/requirements.md)
- 作業の前提と決まったこと: [AGENTS.md](AGENTS.md)
- 必ず満たす失敗経路: [docs/failure-paths.md](docs/failure-paths.md)

基盤は帳票の中身を知らない。何を抜き出すかとプロンプトは、拠点ごとの「処理」（`processes/<拠点>/<タスク>/`）に書く。QR・バーコード、明細の一意化、得意先の判定、業務登録は呼び出し側が行う。

## 動かす

```bash
uv sync
cp settings.example.toml settings.toml   # 中身を書き換える（Git に入れない）
uv run python -m document_extraction_api new-key   # 呼び出し側のキーとハッシュを作る
uv run python -m document_extraction_api serve settings.toml
```

- Python は 3.14 に固定。
- 設定の項目は [settings.example.toml](settings.example.toml) と [settings.py](src/document_extraction_api/settings.py) を見る。
- 拠点ごとに `claude_api_key` が要る。全拠点で同じキーを書いてもよい。
- 拠点の `name` は閲覧画面に出す名前（省略すると設定のキー名を出す）。
- 起動の失敗（設定の誤り、ポート使用中、多重起動）は `log_file` に残る。
- API キー・ファイル名・帳票の本文はログに出さない。

## API

すべての要求に `X-API-Key` が要る。health も同じ（例外は閲覧画面の HTML の `GET /ui` だけ）。キーで拠点が決まり、ほかの拠点のジョブは見えない。

### 受付

```bash
curl -H "X-API-Key: <キー>" -F "file=@納品書.pdf" -F "task_id=delivery.extract" \
  http://127.0.0.1:8001/api/v1/documents/extract
```

```json
{"job_id": "019a2c...", "status": "queued"}
```

- 202 で返る。1回の要求で送るのは帳票1件（ファイル1つ）。複数の帳票がまとまった PDF は、呼び出し側で分けてから送る。
- 受け付けるファイル: PDF・PNG・JPEG・TIFF・WebP。
- 同じ拠点・タスク・ファイル内容・処理の版・モデルのジョブ（失敗以外）があれば、新しく作らずにそのジョブを返す。失敗したジョブは、送り直すと新しいジョブになる。

| 状態 | 意味 |
|---|---|
| 401 `unauthorized` | キーがないか、正しくない |
| 400 `invalid_request` | `file` か `task_id` がない、またはファイルが2つ以上ある |
| 404 `task_not_found` | この拠点では使えないタスク |
| 413 `file_too_large` | ファイルが `max_upload_bytes` を超える |
| 415 `unsupported_file` | 受け付けないファイルの種類 |
| 503 `busy` | 受付が混み合っている。少し待って送り直す |

### 結果の取得

```bash
curl -H "X-API-Key: <キー>" http://127.0.0.1:8001/api/v1/jobs/<job_id>
```

```json
{
  "job_id": "019a2c...",
  "task_id": "delivery.extract",
  "site": "parent",
  "status": "needs_review",
  "data": {
    "document_number": "D-1",
    "issue_date": {"value": "2026-10-27", "raw": "令和8年10月27日"},
    "items": [{"product_name": "部品A", "quantity": null}]
  },
  "issues": [{"path": "items[0].quantity", "raw": "1O", "message": "数値として読めない"}],
  "error": null,
  "versions": {"process": "parent/delivery@de711f25ba68", "model": "claude-opus-5-5"},
  "usage": {"input_tokens": 4210, "output_tokens": 830, "cost_usd": 0.0334}
}
```

| status | 意味 |
|---|---|
| `queued` / `processing` | 待ち・処理中。数秒おきに取りに行く |
| `succeeded` | 成功 |
| `needs_review` | 要確認。`issues` を見る |
| `failed` | 失敗。`error.code` を見る |

- `data` の形は、選ばれた処理の定義が決める。
- 定義の形に合わない値（金額・数量など、どの項目も）は `null` にし、元の文字を `issues` の `raw` に残して要確認にする。
- 日付の項目は `{"value": 変換した日付, "raw": 帳票に書かれていた文字}` の組で返す。日付に変換できないときは `value` を `null` にし、`raw` は残したうえで `issues` にも入れて要確認にする。書かれていない日付は両方 `null`。
- Claude が読めずに `null` で返した項目は、`null` のまま。要確認にするかは、各処理の `check` が決める。
- `versions.process` は処理のファイルの内容から作る版。`versions.model` は実際に読んだモデル。
- 待ちのジョブは、受け付けたときの処理とモデルで読む。設定のモデルを変えても、変える前に受け付けたジョブには効かない。

失敗にするのは、次の3つの場合だけ。

| 場合 | `error.code` |
|---|---|
| ファイルが読めない | `unreadable_file`（Claude へ送る前の誤りや中断が `max_attempts` 回続いたときも）、`too_many_pages`（`max_pages` を超える）、`too_large`（画像にすると Claude API の画像1枚（10MB）か要求全体（32MB、プロンプトと出力の形を含む）の上限を超える。`max_request_image_bytes` は上限を厳しくすることにしか使えない）、`render_timeout` |
| Claude が応答しない | `claude_timeout`、`claude_connection_error`、`claude_http_<状態>`、`claude_error`（そのほかの呼び出しの誤り）、`claude_refused`、`claude_no_response`（送った後の中断が `max_attempts` 回続いた）、`job_timeout` |
| 応答が JSON にならない | `invalid_json`、`response_truncated`（`max_tokens` で途切れた） |

想定外の誤りも、起きた段階で上のどれかになる。Claude の応答を保存した後の想定外の誤りは失敗にせず、要確認にする。
- 処理の `check` が例外で止まったとき、または `Issue`（`path` と `message` が文字）以外を返したときは、その旨を `issues` に入れて要確認にする。
- 処理の定義を読み込めないとき、JSON は読めたが形が定義と合わないとき、検証が想定外の例外で止まったときは、検証していない応答を `data` に入れて要確認にする。
- 応答のモデル名やトークン数の型が合わないときは、その値を `null` として記録し、その旨を `issues` に入れて要確認にする。元の応答はそのまま保存してある。
- 結果は、記録して返せる値にそろえてから記録する。UTF-8 にできない文字、有限でない数、32段を超える入れ子は `null` にし、その箇所を `issues` に入れて要確認にする。そろえる途中で想定外に止まっても、`data` を `null` にして要確認で終える。
- 設定の単価（`[prices.<モデル>]`）は0以上の有限の数にする。そうでなければ起動しない。

### 閲覧（見るだけ）

ブラウザで `http://<サーバー>:8001/ui` を開き、呼び出しに使っているキーを入れる。見えるのはそのキーの拠点のジョブだけで、直す・消すなどの操作はない。

- ダッシュボード：期間（今月・先月・今日・過去7日間・指定）と帳票で絞り、対応が必要な件数（失敗＋要確認）、読み取り件数、成功率（前の期間との比較）、費用（ドル）を上に並べる。その下に日ごとの件数のグラフ、帳票ごとの内訳、読み取りの一覧（対応が必要・すべて・成功）。日の区切りは日本時間。費用には、失敗したジョブで使ったトークンの分も入る。
- 詳細：元のファイル（PDF・画像の表示とダウンロード）と、読み取った内容を項目名付きの表で並べる。確認が必要な項目には印と理由が付く。下に受付・送信・完了の時刻、試行回数、モデル、使用量。ジョブ ID・版・Claude の生の応答・受け付けたときの処理の写しは「技術者向けの情報」に畳んである。
- 帳票名と項目名は、処理の定義の `NAME` と `Field(title=...)` から出す（[処理を足す・直す](#処理を足す直す)）。詳細は受け付けたときの処理の写しから読むので、処理を直しても過去のジョブの詳細の表示は変わらない。ダッシュボードと一覧の帳票名は今の処理から読む。`NAME` がないときと、処理を消したタスクは、タスクIDを出す。
- `/ui` の HTML はデータを含まない固定のページで、キーなしで返す。データはすべて下の API からキーを付けて取る。キーはそのタブの `sessionStorage` に置き、タブを閉じると消える。
- 送られたファイル名は DB に残して画面に出す（フォルダの部分は落とし、255文字まで）。同じ内容のファイルを送り直して既存のジョブが返るときは、最初のファイル名のまま。ログにはファイル名を出さない。

画面が使う API。拠点はキーで決まり、パラメータでは指定できない。ほかの拠点のジョブは 404。

| API | 返すもの |
|---|---|
| `GET /api/v1/summary?from=&to=&task_id=&utc_offset=` | 拠点の表示名（`site_name`）、状態ごとの件数、トークンと費用の合計、費用の出ないジョブ（単価の設定がないモデル）の件数、タスクごとの内訳（`task_name` 付き）、日ごとの件数と費用（`days`）。日は `utc_offset`（例 `+09:00`、既定 `+00:00`）の時差で区切る |
| `GET /api/v1/jobs?from=&to=&status=&task_id=&limit=&before=` | ジョブの一覧（新しい順）。`status` は繰り返して複数指定できる。`limit` は 1〜200（既定 50）。続きは、応答の `next_before` を `before` に入れて取る |
| `GET /api/v1/jobs/<job_id>/detail` | 結果の取得と同じ内容に、帳票名（`task_name`）、項目名（`labels`）、指摘の項目を言葉にしたもの（`issue_fields`）、ファイル名・ハッシュ・指定したモデル・試行回数・受付/送信/完了の時刻を足したもの |
| `GET /api/v1/jobs/<job_id>/file` | 送られた元のファイル |
| `GET /api/v1/jobs/<job_id>/response` | Claude の生の応答（まだなければ 404 `response_not_found`） |
| `GET /api/v1/jobs/<job_id>/process` | 受け付けたときの処理の写し（`prompt.md`・`definition.py` の中身） |

- `from`・`to` は受付時刻で絞る。時差の付いた ISO 8601（`2026-10-01T00:00:00+09:00`）で書き、`from` 以上 `to` 未満。

## 処理を足す・直す

`processes/<拠点>/<フォルダ名>/` に2つのファイルを置き、サービスを再起動する。基盤のコードは変えない。

- `prompt.md`: Claude への指示文。基盤はこれ以外の指示文を足さない。
- `definition.py`: タスクIDと出力の形。

```python
from pydantic import Field

from document_extraction_api.fields import Date, Issue, Number, Record, Text

TASK_ID = "delivery.extract"   # 呼び出し側が送るタスクID。拠点によらず同じにする
NAME = "納品書"                 # 任意。閲覧画面に出す帳票名


class Item(Record):
    product_name: Text = Field(title="品名")   # title は任意。閲覧画面に出す項目名
    quantity: Number = Field(title="数量")


class Output(Record):          # 出力の形
    issue_date: Date = Field(title="発行日")
    items: list[Item] = Field(title="明細")


def check(data: Output) -> list[Issue]:   # 任意。指摘があれば要確認になる
    return [Issue(f"items[{i}].quantity", "数量がない")
            for i, item in enumerate(data.items) if item.quantity is None]
```

- 項目の型は `Text`・`Number`・`Date` を使う。Claude には、どの項目も「書かれている文字」か `null` で書かせ、数値・日付への変換は基盤が行う（[fields.py](src/document_extraction_api/fields.py)）。
  - `Number` は桁区切りのカンマと全角数字を受け付ける。
  - `Date` は西暦（`2026-10-27`、`2026/10/27`、`2026年10月27日`）と令和・平成を受け付ける。`check` には `value`（`datetime.date` か `None`）と `raw` を持つ値で渡る。
- `NAME` と `Field(title=...)` は表示のためだけに使い、Claude へ送る出力の形には入らない。
- 受け付けたジョブは、受け付けたときの処理で読む。処理を直したり消したりしても、待ちのジョブには効かない。
- 拠点に処理のないタスクは、その拠点では使えない。共通の処理への切り替えはしない。
- `processes/` にある請求書・納品書の処理は仮の中身。項目とプロンプトは運用前に調整する。

## 仕組み

- ジョブは SQLite（`data_dir/jobs.sqlite3`）に記録する。ファイル、受け付けたときの処理の写し、Claude の生の応答は `data_dir/jobs/<job_id>/` に置く。
- 同時に処理するのは1件。前処理（PDF の画像化、ページの回転、写真の向き、透過の白背景合成）は子プロセスで行い、時間を超えたらプロセスツリーごと終了する。子プロセス自身も制限時間を持つ。
- ページは長辺 2576px までの PNG にする。スキャンなどで 1.5MB を超えるページは JPEG（品質90）にする。Claude API の上限は、画像1枚 10MB・要求全体 32MB。要求全体は、画像・プロンプト・出力の形を合わせて多めに見積もって確かめる。
- Claude へ送る前に送信時刻を記録し、受け取った応答は DB より先にディスクへ保存する。処理中のまま止まったジョブは、起動時と手が空いたときに立て直す。
  - 応答が保存済みなら、Claude を呼ばずに結果を確定する。
  - まだ送っていなければ、待ちに戻す。
  - 送った後に止まったなら、`max_attempts` 回まで送り直す。
- 費用は、設定の `[prices.<モデル>]` の単価で計算する。拠点ごとの集計は、`jobs` 表の `site` と `cost_usd` を集計する。

## 検査

```bash
uv run ruff check src tests processes
uv run pytest -q
```

実 Claude API はテストで呼ばない。
