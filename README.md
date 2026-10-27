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
- 起動の失敗（設定の誤り、ポート使用中、多重起動）は `log_file` に残る。
- API キー・ファイル名・帳票の本文はログに出さない。

## API

すべての要求に `X-API-Key` が要る。health も同じ。キーで拠点が決まり、ほかの拠点のジョブは見えない。

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
| 400 `invalid_request` | `file` か `task_id` がない |
| 404 `task_not_found` | この拠点では使えないタスク |
| 413 `file_too_large` | `max_upload_bytes` を超える |
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
  "data": {"document_number": "D-1", "items": [{"product_name": "部品A", "quantity": null}]},
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
- 定義の形に合わない値（日付・金額・数量など、どの項目も）は `null` にし、元の文字を `issues` の `raw` に残して要確認にする。
- Claude が読めずに `null` で返した項目は、`null` のまま。要確認にするかは、各処理の `check` が決める。
- `versions.process` は処理のファイルの内容から作る版。`versions.model` は実際に読んだモデル。

失敗にするのは、次の3つの場合だけ。

| 場合 | `error.code` |
|---|---|
| ファイルが読めない | `unreadable_file`、`too_many_pages`（`max_pages` を超える）、`too_large`（画像にすると Claude API の要求の上限を超える）、`render_timeout` |
| Claude が応答しない | `claude_timeout`、`claude_connection_error`、`claude_http_<状態>`、`claude_refused`、`claude_no_response`（中断が `max_attempts` 回続いた）、`job_timeout` |
| 応答が JSON にならない | `invalid_json`、`response_truncated`（`max_tokens` で途切れた） |

このほか、想定外の例外は `internal_error`、処理の定義が消えたときは `process_not_found` になる。

## 処理を足す・直す

`processes/<拠点>/<フォルダ名>/` に2つのファイルを置き、サービスを再起動する。基盤のコードは変えない。

- `prompt.md`: Claude への指示文。基盤はこれ以外の指示文を足さない。
- `definition.py`: タスクIDと出力の形。

```python
from document_extraction_api.fields import Date, Issue, Number, Record, Text

TASK_ID = "delivery.extract"   # 呼び出し側が送るタスクID。拠点によらず同じにする


class Item(Record):
    product_name: Text
    quantity: Number


class Output(Record):          # 出力の形
    issue_date: Date
    items: list[Item]


def check(data: Output) -> list[Issue]:   # 任意。指摘があれば要確認になる
    return [Issue(f"items[{i}].quantity", "数量がない")
            for i, item in enumerate(data.items) if item.quantity is None]
```

- 項目の型は `Text`・`Number`・`Date` を使う。Claude には、どの項目も「書かれている文字」か `null` で書かせ、数値・日付への変換は基盤が行う（[fields.py](src/document_extraction_api/fields.py)）。
  - `Number` は桁区切りのカンマと全角数字を受け付ける。
  - `Date` は西暦（`2026-10-27`、`2026/10/27`、`2026年10月27日`）と令和・平成を受け付ける。
- 拠点に処理のないタスクは、その拠点では使えない。共通の処理への切り替えはしない。
- `processes/` にある請求書・納品書の処理は仮の中身。項目とプロンプトは運用前に調整する。

## 仕組み

- ジョブは SQLite（`data_dir/jobs.sqlite3`）に記録する。ファイルと Claude の生の応答は `data_dir/jobs/<job_id>/` に置く。
- 同時に処理するのは1件。前処理（PDF の画像化、ページの回転、写真の向き、透過の白背景合成）は子プロセスで行い、時間を超えたらプロセスツリーごと終了する。子プロセス自身も制限時間を持つ。
- ページは長辺 2576px までの PNG にする。スキャンなどで 1.5MB を超えるページは JPEG（品質90）にする。Claude API の上限は、画像1枚 10MB・要求全体 32MB。
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
