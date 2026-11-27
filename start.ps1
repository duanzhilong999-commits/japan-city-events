# 启动网页（默认 http://127.0.0.1:8100）
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path (Split-Path -Parent $here) ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $here
& $py -m uvicorn app.web:app --host 127.0.0.1 --port 8100
