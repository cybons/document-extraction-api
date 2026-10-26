$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& "$ProjectRoot\.venv\Scripts\python.exe" -m document_extraction_api
exit $LASTEXITCODE
