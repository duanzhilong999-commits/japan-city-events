# 全量抓取（约 5–8 分钟）
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path (Split-Path -Parent $here) ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $here
& $py -m app.cli collect
