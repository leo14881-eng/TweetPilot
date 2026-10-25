# TweetPilot sync launcher — 定时任务入口。
# 先从 GitHub 取最新的 sync.ps1（取不到就用上次的），再运行它。这样同步脚本以后可以远程修复，不用再动电脑。
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$App  = Join-Path $Root 'app'
New-Item -ItemType Directory -Force -Path $App | Out-Null
$Current = Join-Path $App 'sync.ps1'
$New     = Join-Path $App 'sync.ps1.new'

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -OutFile $New `
        -Uri 'https://raw.githubusercontent.com/leo14881-eng/TweetPilot/main/scripts/windows/sync.ps1'
    $text = [IO.File]::ReadAllText($New)
    if ($text.Contains('# TweetPilot sync') -and $text.Length -gt 1000) {
        Move-Item -Force $New $Current
    } else {
        Remove-Item $New -ErrorAction SilentlyContinue
    }
} catch {
    Remove-Item $New -ErrorAction SilentlyContinue
}

if (-not (Test-Path $Current)) { $Current = Join-Path $PSScriptRoot 'sync.ps1' }
& $Current -Root $Root
