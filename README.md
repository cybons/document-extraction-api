# Document Extraction API

請求書・納品書をClaude APIで読み取り、社内システムへJSONを返す独立サービスです。Python 3.14 / FastAPI / Anthropic SDK / Pydanticを使います。帳票1件の失敗をジョブ単位で記録し、後続の処理を続けます。

開発担当者はまず「HTTPで起動する」を読み、Windowsへの配置とPAD連携は[運用手順](docs/operations.md)を参照してください。[構築方針](docs/architecture.md)には、処理の境界と障害時の動作をまとめています。

## HTTPで起動する

```sh
uv sync --locked
cp .env.example .env
```

`.env`にAnthropic APIキー、構造化出力に対応するモデルID、呼び出し元ごとのAPIキーを設定します。APIキーは32文字以上のランダム値を使用してください。サンプルの仮値では起動しません。モデル名と単価は利用契約・現行モデルに合わせて設定します。単価未設定の概算費用はnullです。

```sh
uv run python -m document_extraction_api
```

既定の待受先は `http://127.0.0.1:8001` です。HTTPSや証明書はローカル開発に不要です。`GET /health/ready` がHTTP 200なら受付準備ができています。認証付きの `GET /openapi.json` からAPI仕様を取得できます。

## 帳票を送って結果を取得する

`POST /api/v1/documents/extract` にmultipart形式で `file`、`task_id`、任意の `rotation` を送ります。ヘッダーに `Authorization: Bearer <呼び出し元APIキー>` と `Idempotency-Key` が必要です。

| 項目 | 初期仕様 |
| --- | --- |
| タスク | `invoice.extract`、`delivery.extract` |
| ファイル | PDF / JPEG / PNGを1点、20MiBまで、PDFは10ページまで |
| 回転 | `0` / `90` / `180` / `270` 度。時計回り。メタ情報の補正後に適用 |
| 新規受付 | HTTP 202、`job_id`と`result_url`を返却 |
| 同じ依頼の再送 | HTTP 200、既存ジョブを返却。同じキーで内容が変わる場合は409 |
| 結果取得 | `GET /api/v1/documents/jobs/{job_id}` |
| 結果保存期限後 | GETは410、`data: null`、`result_expired: true` |

同じ依頼の再送には同じキーを使います。意図的な再読み取りには新しいキーを使い、ファイルを再送してください。通信切断後に新しいキーを発行すると二重処理になり得ます。重複防止記録は処理完了から90日で削除します。

レスポンスの主な項目は `job_id`、`task_id`、`status`、`data`、`review_reasons`、`error`、`versions` です。状態は `queued` → `running` → `succeeded` / `needs_review` / `failed` と進みます。受付前のエラーは `{"error":{"code":"..."}}` を返します。

Pythonからの利用例は標準ライブラリだけで動作します。環境変数 `DEX_CLIENT_KEY` に呼び出し元キーを設定して実行します。

```sh
python examples/extract.py sample.pdf --idempotency-key invoice-test-001
```

クライアントは最大15分ポーリングし、成功で終了コード0、要確認・失敗で2を返します。待ち時間を超えた場合は表示されたジョブIDで取得を再開してください。結果JSONは標準出力に出るため、業務データを含む出力の保存先にはアクセス制御が必要です。

## テストと現在の確認範囲

```sh
uv run ruff check src tests examples
uv run pytest -q
```

2026-10-27時点で34件のテストが成功しています。実HTTP経由のPythonクライアント疎通と、Windows x64 / Python 3.14向け依存パッケージの解決も確認しました。

テストでは合成帳票とClaudeのモック応答を使い、実APIの利用料金は発生しません。実帳票の読取精度、実Claudeへの接続、WindowsサービスとPADでの動作は、対象環境での受入確認が必要です。実APIキー・本番証明書・実帳票はリポジトリに含めません。
