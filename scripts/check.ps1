<#
一键检查：ruff（app + tests + desktop）→ pytest → 前端单测（Vitest）→ 前端构建 → 浏览器冒烟。
用法（在项目根目录）：
    powershell -ExecutionPolicy Bypass -File scripts/check.ps1
任一环节失败即以非 0 退出码中断；浏览器冒烟在缺 dist / 缺 Chromium 时自行跳过（SKIP 不算失败）。
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = "1"

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$ruff = Join-Path $root ".venv\Scripts\ruff.exe"
if (-not (Test-Path $ruff)) { $ruff = "ruff" }

Write-Host "== ruff check app tests desktop ==" -ForegroundColor Cyan
& $ruff check app tests desktop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== pytest ==" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== frontend vitest ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "frontend")
try {
    npm run test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "== npm run build ==" -ForegroundColor Cyan
    npm run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

Write-Host "== frontend smoke (playwright) ==" -ForegroundColor Cyan
& $py -m scripts.smoke_frontend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "全部通过" -ForegroundColor Green
