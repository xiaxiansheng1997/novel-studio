<#
Novel Studio 一键装机 + 桌面快捷方式。

用法（在项目根目录，或右键本文件"用 PowerShell 运行"）：
    powershell -ExecutionPolicy Bypass -File scripts\setup_desktop.ps1

它按顺序做六件事，每步都可以重跑（幂等）：
    1. 建 .venv（没有就建，用系统里能找到的 Python 3.11+）
    2. 装后端依赖 requirements.txt + 桌面壳依赖 requirements-desktop.txt
    3. playwright install chromium（浏览器自动化要用的内核，约 150MB）
    4. 构建前端（npm ci + npm run build），后端单端口就能服整套界面
    5. 生成 .env（从 .env.example 抄一份，不动已有文件）
    6. 打桌面壳 exe 并在桌面建 "Novel Studio" 快捷方式

可选开关：
    -SkipPlaywright   公司网络拉不动 chromium 内核时先跳过（推流/登录功能暂不可用）
    -NoExe            不打 exe，快捷方式直接指向 pythonw.exe + launcher.py
    -NoShortcut       只装环境，不碰桌面
    -Rebuild          已经有产物也重打一遍（默认跳过：前端 dist 与 exe 存在就不动）

为什么默认跳过：npm ci 会先把 node_modules 清空再装，只要有一个 node 进程还开着
rolldown 的 .node 就报 EPERM；PyInstaller 一次要两分钟。装机脚本要敢重跑，
就不能每次都去动这些已经好的东西。
#>
[CmdletBinding()]
param(
    [switch]$SkipPlaywright,
    [switch]$NoExe,
    [switch]$NoShortcut,
    [switch]$Rebuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
# 让 pip / python 的输出按 UTF-8 走，中文报错不至于糊成乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# 不再动 [Console]::OutputEncoding：launcher --check 自己会把控制台代码页设成 65001，
# PowerShell 跟着它解码（实测 Out-File 存出来的中文正常）。在这里强制 UTF-8 反而会
# 把 taskkill / pip 这类按代码页 936 说中文的原生命令输出糊掉。

function Step($msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function Die($msg) { Write-Host "`n$msg" -ForegroundColor Red; exit 1 }

Write-Host "项目目录：$root" -ForegroundColor Gray

# ---------------------------------------------------------------- 1. 虚拟环境
Step "1/6 虚拟环境"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $py) {
    Write-Host "   已存在，跳过创建"
} else {
    $basePy = $null
    foreach ($cand in @("py", "python", "python3")) {
        # $ErrorActionPreference = "Stop" 下，& 一个不存在的命令是终止性错误：不先探测就
        # 会当场退在这里，下面那句「没找到 Python 3.11+」的人话报错根本走不到。
        # py launcher 并不是人人都有（Microsoft Store 版、conda、什么都没装的），
        # 而那正是最需要脚本指条路的场合。
        if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
        try {
            $probe = @(& $cand -c "import sys;print('%d.%d' % sys.version_info[:2]);print(sys.executable)" 2>$null)
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or $probe.Count -lt 2) { continue }
        $ver = [version]$probe[0]
        if ($ver.Major -eq 3 -and $ver.Minor -ge 11) { $basePy = $probe[1]; break }
    }
    if (-not $basePy) {
        Die "没找到 Python 3.11+。装一个（winget install Python.Python.3.12）再重跑本脚本。"
    }
    Write-Host "   用 $basePy 创建 .venv"
    & $basePy -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { Die "python -m venv 失败" }
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { Die ".venv 建完了但找不到 python.exe" }
}

# ---------------------------------------------------------------- 2. 依赖
Step "2/6 Python 依赖"
& $py -m pip install --disable-pip-version-check --upgrade pip
& $py -m pip install --disable-pip-version-check -r requirements.txt -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { Die "pip 安装失败，检查网络或代理后重跑" }

# ---------------------------------------------------------------- 3. 浏览器内核
Step "3/6 Playwright 浏览器内核"
if ($SkipPlaywright) {
    Write-Host "   已按 -SkipPlaywright 跳过：控制台「浏览器」相关功能暂不可用" -ForegroundColor Yellow
} else {
    & $py -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   内核下载失败（多半是网络）。可以先继续，稍后手动跑：" -ForegroundColor Yellow
        Write-Host "       .venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 4. 前端
Step "4/6 前端构建"
$distIndex = Join-Path $root "frontend\dist\index.html"
if ((Test-Path $distIndex) -and -not $Rebuild) {
    Write-Host "   已有构建产物，跳过（要重看前端改动就加 -Rebuild）"
} else {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Die "没找到 npm。装 Node.js LTS（winget install OpenJS.NodeJS.LTS）后重跑本脚本。"
    }
    Push-Location (Join-Path $root "frontend")
    try {
        if (Test-Path "node_modules") {
            # npm install 是增量，npm ci 会先删空 node_modules（占用中就 EPERM）
            npm install
            if ($LASTEXITCODE -ne 0) { Die "npm install 失败" }
        } elseif (Test-Path "package-lock.json") {
            npm ci
            if ($LASTEXITCODE -ne 0) { Die "npm ci 失败" }
        } else {
            npm install
            if ($LASTEXITCODE -ne 0) { Die "npm install 失败" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { Die "npm run build 失败" }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path $distIndex)) { Die "构建结束但没有 frontend\dist\index.html" }
}

# ---------------------------------------------------------------- 5. .env
Step "5/6 配置文件"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "   已从 .env.example 生成 .env（默认监听 127.0.0.1:8000、不设口令）" -ForegroundColor Yellow
    Write-Host "   要加访问口令就改 W2A_API_KEY，改完重启桌面版才生效" -ForegroundColor Gray
} else {
    Write-Host "   已存在，保持原样"
}

# ---------------------------------------------------------------- 6. 壳与快捷方式
Step "6/6 桌面壳"
$exe = Join-Path $root "desktop\dist\NovelStudio\NovelStudio.exe"
if ($NoExe) {
    Write-Host "   按 -NoExe 跳过打包"
} elseif ((Test-Path $exe) -and -not $Rebuild) {
    Write-Host "   exe 已存在，跳过打包（改了 desktop/ 下的代码要加 -Rebuild 重打）"
} else {
    & (Join-Path $root "desktop\build.ps1")
    if ($LASTEXITCODE -ne 0) { Die "打壳失败，看上面的 PyInstaller 输出" }
}
$icon = Join-Path $root "desktop\assets\novel-studio.ico"
if (-not (Test-Path $icon)) {
    & $py (Join-Path $root "desktop\make_icon.py")
}

if ($NoShortcut) {
    Write-Host "`n已按 -NoShortcut 跳过快捷方式" -ForegroundColor Yellow
} else {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnkPath = Join-Path $desktop "Novel Studio.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    if (Test-Path $exe) {
        $sc.TargetPath = $exe
        $sc.Arguments = "--root `"$root`""
    } else {
        # 没打成 exe 也能用：pythonw 跑脚本，同样不闪黑框
        $sc.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
        $sc.Arguments = "`"$root\desktop\launcher.py`" --root `"$root`""
    }
    $sc.WorkingDirectory = $root
    $sc.IconLocation = "$icon,0"
    $sc.Description = "Novel Studio 写作台：双击即起，关窗即停"
    $sc.Save()
    Write-Host "`n快捷方式已生成：$lnkPath" -ForegroundColor Green
}

Step "自检"
& $py (Join-Path $root "desktop\launcher.py") --check
Write-Host "`n装好了。桌面上双击 `"Novel Studio`" 即可；要停就关窗口（写作队列会自动退回，下次接着写）。" -ForegroundColor Green
