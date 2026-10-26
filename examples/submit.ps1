# Windows PowerShell 5.1+. Outputs the API JSON, with no credentials in output.
param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [Parameter(Mandatory=$true)][string]$IdempotencyKey,
    [string]$BaseUrl = "http://127.0.0.1:8001",
    [ValidateSet("invoice.extract", "delivery.extract")][string]$TaskId = "invoice.extract",
    [ValidateSet(0,90,180,270)][int]$Rotation = 0
)
$ErrorActionPreference = "Stop"
if (-not $env:DEX_CLIENT_KEY) { throw "Set DEX_CLIENT_KEY in the process environment." }
Add-Type -AssemblyName System.Net.Http
$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect = $false
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(75)
$form = [System.Net.Http.MultipartFormDataContent]::new()
$response = $null
try {
    $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $env:DEX_CLIENT_KEY)
    $client.DefaultRequestHeaders.Add("Idempotency-Key", $IdempotencyKey)
    $form.Add([System.Net.Http.StringContent]::new($TaskId), "task_id")
    $form.Add([System.Net.Http.StringContent]::new([string]$Rotation), "rotation")
    $stream = [System.IO.File]::OpenRead($FilePath)
    $form.Add([System.Net.Http.StreamContent]::new($stream), "file", "document")
    $response = $client.PostAsync($BaseUrl.TrimEnd('/') + "/api/v1/documents/extract", $form).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) { throw ("HTTP " + [int]$response.StatusCode) }
    $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
} finally {
    if ($response) { $response.Dispose() }
    $form.Dispose()
    $client.Dispose()
    $handler.Dispose()
}
