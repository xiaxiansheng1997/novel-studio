<#
打 Novel Studio 桌面壳 exe（onedir）。

用法（在项目根目录）：
    powershell -ExecutionPolicy Bypass -File desktop\build.ps1

产物：desktop\dist\NovelStudio\NovelStudio.exe
中间文件：desktop\build\   （都可以删，删了下次重打）

只打壳，不打后端：exe 里不含 FastAPI / Playwright，运行时它去拉起 .venv 里的 Python。
所以打完 exe 换机器时，仍然要在目标机器上跑一次 scripts\setup_desktop.ps1 建环境。
#>
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$root = Split-Path -Parent $here
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "== 检查打包依赖 ==" -ForegroundColor Cyan
& $py -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   缺 PyInstaller，装一下 requirements-desktop.txt" -ForegroundColor Yellow
    & $py -m pip install --disable-pip-version-check -r (Join-Path $root "requirements-desktop.txt")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$icon = Join-Path $here "assets\novel-studio.ico"
if (-not (Test-Path $icon)) {
    Write-Host "== 生成图标 ==" -ForegroundColor Cyan
    & $py (Join-Path $here "make_icon.py")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "== PyInstaller ==" -ForegroundColor Cyan
# 给 .spec 时不能再传 --specpath（PyInstaller 会直接拒），spec 自己用 SPECPATH 定位
& $py -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $here "dist") `
    --workpath (Join-Path $here "build") `
    (Join-Path $here "NovelStudio.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$exe = Join-Path $here "dist\NovelStudio\NovelStudio.exe"
if (-not (Test-Path $exe)) {
    Write-Host "打包结束但找不到 $exe" -ForegroundColor Red
    exit 1
}
$sizeMB = [math]::Round(((Get-ChildItem (Split-Path -Parent $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "`n完成：$exe（整个目录 $sizeMB MB）" -ForegroundColor Green
Write-Host "下一步：跑 scripts\setup_desktop.ps1 建桌面快捷方式（会自动指向这个 exe）" -ForegroundColor Gray
