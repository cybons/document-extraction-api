# HTTPで疎通し、確認後にWindowsサービスへ配置する

APIの配置担当者とPADフローの作成者向けの手順です。初回は「Windowsで起動する」「PADから送信する」を読み、障害対応と更新手順は必要なときに参照してください。Python 3.14とuvが使用できるWindowsを前提とします。

## Windowsで起動する

プロジェクトを専用フォルダ（例：`C:\Services\document-extraction-api`）へ配置します。Macの `.venv` はコピーせず、Windows側で依存を導入します。

```powershell
Set-Location C:\Services\document-extraction-api
uv sync --locked --no-dev
Copy-Item .env.example .env
```

`.env`へ実際のモデルID・APIキーを設定します。呼び出し元APIキーはPAD用とPython用を分け、`DEX_CLIENTS` の `tasks` に使用可能なタスクを指定してください。設定値の一覧と既定値は `src/document_extraction_api/config.py` が正本です。

```powershell
.\scripts\start.ps1
```

別のPowerShellで確認します。

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health/ready
```

PADを別端末で動かす場合は `DEX_HOST` にサーバーの社内IPを指定して再起動します。Windowsファイアウォールでは、そのポートへの接続元をテスト端末に限定してください。HTTP疎通には専用のテストキーと機密情報を含まない帳票を使います。

本番では専用ホスト名と証明書を決め、IISなどでTLS終端を設けます。FastAPIは内部のHTTPで動作させられます。既存証明書の流用は前提にしません。ワイルドカード証明書も対象ホスト名の適合とTLS終端の設定が必要です。アプリにHTTPS強制リダイレクトはありません。

## PADから送信する

PADのHTTPアクションから、認証ヘッダーを付けたGETで結果を取得できます。バイナリファイルのmultipart送信には同梱の `examples\submit.ps1` を利用できます。このスクリプトはWindows PowerShell 5.1以降のHttpClientを使い、バイナリを文字列へ変換せず送信します。

1. PADの実行プロセスで `DEX_CLIENT_KEY` を参照できるようにします。キーをフローのログやコマンドライン引数に書かないでください。
2. 依頼ごとにIdempotency-Keyを発行し、ファイルとの対応を保存します。通信失敗で再送するときは同じキーを使います。
3. PADの「PowerShellスクリプトの実行」から、次のように送信スクリプトを呼び出します。実際のフローではファイルパスとキーを変数から渡します。

```powershell
& 'C:\Services\document-extraction-api\examples\submit.ps1' `
    -FilePath 'C:\TestDocuments\invoice.pdf' `
    -IdempotencyKey 'pad-test-001' `
    -BaseUrl 'http://SERVER-IP:8001' `
    -TaskId 'invoice.extract'
```

4. 出力JSONをカスタムオブジェクトへ変換し、`job_id`を保存します。
5. 2秒待ってから「Webサービスの呼び出し」で、BaseUrlと`result_url`を連結したURLへGETします。ヘッダーは `Authorization: Bearer <キー>`、応答はJSONです。
6. `queued` / `running` なら待機を繰り返します。`succeeded` は業務側の照合へ、`needs_review` は確認担当へ、`failed` はエラー処理へ分岐します。待機上限は例として15分とし、超過したらジョブIDを残して取得を中断します。

POSTの応答が失われても、同じキーで再送すれば既存ジョブを取得できます。429は一定時間待って再送します。409は同じキーの内容違いなので依頼記録を確認してください。結果が410の場合は7日の保存期限を過ぎています。

PADのHTTPアクション仕様は[Microsoft公式資料](https://learn.microsoft.com/en-gb/power-automate/desktop-flows/actions-reference/web)を参照してください。PAD本体とPowerShellの実機動作は、このMacでの自動テストには含まれません。

## 常駐化は専用サービスアカウントで行う

手動起動で疎通できた後、組織で承認された[WinSW](https://github.com/winsw/winsw)を用意し、`deploy\document-extraction.exe` に配置します。同名の `document-extraction.xml` を同梱しています。

管理者権限のPowerShellで次を実行します。

```powershell
.\deploy\document-extraction.exe install
```

起動前にWindowsのサービス管理画面で、`document-extraction-api` のログオン先を専用の最小権限アカウントへ変更してください。テンプレートにはアカウント資格情報を含めていません。設定ファイルとコードは読み取り、データ・ログ・専用一時領域は書き込みできるようACLを設定します。`data`はローカルディスクを使ってください。TEMP/TMPも専用領域に向けると一時ファイルの管理範囲を限定できます。

```powershell
.\deploy\document-extraction.exe start
```

サービスプロセスが異常終了すると30秒後に再起動します。個別ジョブの失敗ではサービスを再起動しません。常駐設定の形式は[WinSW公式仕様](https://github.com/winsw/winsw/blob/v3/docs/xml-config-file.md)を参照してください。

## エラーコードから対応を決める

| 状況・コード | 対応 |
| --- | --- |
| `invalid_document` | 破損・暗号化・ページ数・画像サイズを確認。修正後に新しいキーで再送 |
| `job_timeout` / `connection_or_timeout` | 通信状態と上流の状況を確認。課金の可能性を確認して再実行を判断 |
| `upstream_401` / `upstream_403` | Anthropic側のキー・権限を修正し、サービスを再起動 |
| `upstream_429` / `upstream_5xx` | 再試行上限に到達。利用制限・上流障害を確認 |
| `invalid_extraction` | スキーマに合わない出力。帳票・モデル・プロンプトを確認 |
| `incomplete_or_refused_output` | 出力打切りまたは拒否。帳票の大きさや出力上限を確認 |
| `interrupted` | サービス停止中の処理。自動再送しないため必要性を確認して再実行 |
| `internal_error` | 処理IDと発生時刻を保守担当へ連絡 |
| readinessが503 | DB・ディスク・掃除処理・ワーカーを確認。単なる帳票失敗と区別 |

`GET /health/live` はAPIプロセスの応答、`GET /health/ready` はDB・ワーカー・掃除処理の状態を確認します。運用監視ではreadinessの継続的な503とディスク残量を監視してください。ログは `data\logs\service.log`、履歴は `data\jobs.sqlite3` に保存されます。SQLiteの `jobs` が処理履歴、`attempts` がAPI試行と使用量です。本文があるDBを汎用ログ収集へ送らないでください。

## 更新前にキューを空にする

呼び出し元の新規送信を止め、待機・実行中のジョブがなくなったことを確認します。サービスを停止し、コードとロックファイルを配置して `uv sync --locked --no-dev` を実行してから再開します。プロンプトやスキーマを変更するときはタスクバージョンを上げます。初期版にはDBの自動マイグレーション機構がないため、テーブル変更は個別の移行手順とバックアップを用意してください。

バックアップには原本や抽出結果が含まれ得ます。本体の保存期限とは別に削除運用を定めてください。ディスク障害では受付や保存が止まるため、readinessだけでなくバックアップ復元も事前に確認します。

## 実環境で受入確認する

合成の請求書・納品書を各1件、破損PDF、90度回転画像を送信します。破損PDFが失敗した後も正常帳票が完了すること、同じキーの再送でジョブが増えないこと、別クライアントから結果を取得できないことを確認してください。続いて、処理途中のサービス停止・再起動、結果の保存期限、ログへの本文・キー混入がないことを確認します。

実帳票の精度評価は別に行い、主要項目と明細の正解データに照合します。自動テストの成功は読取精度の保証ではありません。

用語：ジョブは帳票1件の処理単位、Idempotency-Keyは再送時の重複を防ぐ識別子、readinessは処理受付の準備状態です。解決しない場合は、処理ID・時刻・定型コードを添えて本サービスの保守担当へ連絡してください。担当者と連絡先は運用開始時に設定します。

最終更新：2026-10-27
