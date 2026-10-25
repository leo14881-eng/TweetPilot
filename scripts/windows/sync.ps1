# TweetPilot sync — 电脑 → 服务器配置同步（Windows 定时任务每 5 分钟运行一次，由 launcher.ps1 调用）
# 只做固定的几件事，不执行任意命令，不读取、不输出配置内容：
#   1. config\ 里的配置文件有变化（或有 app\sync.request）→ 上传到服务器 /opt/tweetpilot/secret/，运行 install.sh 生效
#   2. 有 app\status.request → 把服务器上的运行状态写到 logs\status.txt
# 连接服务器沿用电脑上 TrendPilot 的 SSH 密钥（只按路径引用，不复制、不修改 TrendPilot 的任何文件）。
param([string]$Root)
$ErrorActionPreference = 'Continue'
if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }

$Cfg    = Join-Path $Root 'config'
$App    = Join-Path $Root 'app'
$Logs   = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $App, $Logs | Out-Null
$LogFile   = Join-Path $Logs 'sync.log'
$Synced    = Join-Path $App 'synced.sha256'
$Failed    = Join-Path $App 'sync-failed.sha256'
$SyncReq   = Join-Path $App 'sync.request'
$StatusReq = Join-Path $App 'status.request'
$Files     = @('tweetpilot.env', 'x-auth-token.txt')
$TrendPilot = Join-Path (Split-Path -Parent $Root) 'TrendPilot'
$Key       = Join-Path $TrendPilot 'ssh\id_ed25519'
$KnownHosts = Join-Path $App 'known_hosts'
$Ssh       = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$Scp       = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'

$CmdPrepare = 'mkdir -p /opt/tweetpilot/secret && chmod 700 /opt/tweetpilot/secret'
$CmdInstall = 'curl -fsSL https://raw.githubusercontent.com/leo14881-eng/TweetPilot/main/scripts/install.sh | bash'
$CmdStatus  = 'if [ -f /opt/tweetpilot/scripts/install.sh ]; then bash /opt/tweetpilot/scripts/install.sh --status; else echo "TweetPilot 还没有安装"; fi'

function Log($msg) {
    Add-Content -Path $LogFile -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
}

function Read-Text($path) {
    if (Test-Path $path) { return ([IO.File]::ReadAllText($path)).Trim() }
    return ''
}

# 运行 ssh / scp（最多 20 分钟），输出写进 $OutFile（默认追加到 sync.log），返回退出码
function Invoke-Tool($exe, $argLine, $stdinText, $OutFile = $LogFile) {
    $in = Join-Path $App 'tool-in.tmp'; $out = Join-Path $App 'tool-out.tmp'; $err = Join-Path $App 'tool-err.tmp'
    $p = @{ FilePath = $exe; ArgumentList = $argLine; NoNewWindow = $true; PassThru = $true
        RedirectStandardOutput = $out; RedirectStandardError = $err }
    if ($stdinText) {
        [IO.File]::WriteAllText($in, $stdinText + "`n", (New-Object Text.UTF8Encoding $false))
        $p.RedirectStandardInput = $in
    }
    $proc = $null
    try { $proc = Start-Process @p } catch { Log "无法启动 ${exe}：$($_.Exception.Message)" }
    if ($null -eq $proc) { Remove-Item $in, $out, $err -ErrorAction SilentlyContinue; return -1 }
    $null = $proc.Handle
    if (-not $proc.WaitForExit(20 * 60 * 1000)) {
        try { $proc.Kill(); $proc.WaitForExit(30000) | Out-Null } catch { }
        Log '超过 20 分钟，已中止'
    }
    foreach ($f in $out, $err) {
        if (Test-Path $f) {
            $text = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8).TrimEnd()
            if ($text) { Add-Content -Path $OutFile -Value $text -Encoding UTF8 }
        }
    }
    Remove-Item $in, $out, $err -ErrorAction SilentlyContinue
    if ($null -eq $proc.ExitCode) { return -1 }
    return $proc.ExitCode
}

function Get-ConfigHash {
    if (-not (Test-Path (Join-Path $Cfg 'tweetpilot.env'))) { return $null }
    $parts = foreach ($n in $Files) {
        $f = Join-Path $Cfg $n
        if (Test-Path $f) { "$n=" + (Get-FileHash $f -Algorithm SHA256).Hash }
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($parts -join ';'))
    return [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '')
}

$wantSync = Test-Path $SyncReq
$wantStatus = Test-Path $StatusReq
$hash = Get-ConfigHash
if (-not $hash) {
    if ($wantSync -or $wantStatus) { Log '还没有 config\tweetpilot.env，跳过' }
    Remove-Item $SyncReq, $StatusReq -ErrorAction SilentlyContinue
    exit 0
}
$changed = $hash -ne (Read-Text $Synced)
$recentFail = (Test-Path $Failed) -and (Read-Text $Failed) -eq $hash -and (Get-Item $Failed).LastWriteTime -gt (Get-Date).AddMinutes(-30)
if (-not $wantStatus -and -not $wantSync -and (-not $changed -or $recentFail)) { exit 0 }

# ---------- 连接准备 ----------
$ip = Read-Text (Join-Path $Cfg 'server-ip.txt')
if (-not $ip) { $ip = Read-Text (Join-Path $TrendPilot 'app\server-ip.txt') }
if ($ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { Log "服务器 IP 无效：$ip"; exit 0 }
if (-not (Test-Path $Key) -or -not (Test-Path $Ssh)) { Log '无法连接：SSH 密钥或系统自带的 ssh 不存在'; exit 0 }
$opts = "-i `"$Key`" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o `"UserKnownHostsFile=$KnownHosts`" -o ConnectTimeout=20 -o ServerAliveInterval=30"

# ---------- 运行状态 ----------
if ($wantStatus) {
    Remove-Item $StatusReq -ErrorAction SilentlyContinue
    $statusFile = Join-Path $Logs 'status.txt'
    Set-Content -Path $statusFile -Value ('== 服务器状态 {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding UTF8
    $code = Invoke-Tool $Ssh "$opts root@$ip bash -s" $CmdStatus $statusFile
    Log "查看状态：退出码 $code"
}

# ---------- 配置同步 ----------
if ($changed -or $wantSync) {
    Remove-Item $SyncReq -ErrorAction SilentlyContinue
    Log '== 同步配置到服务器'
    $ok = (Invoke-Tool $Ssh "$opts root@$ip bash -s" $CmdPrepare) -eq 0
    foreach ($n in $Files) {
        $f = Join-Path $Cfg $n
        if (-not $ok -or -not (Test-Path $f)) { continue }
        $code = Invoke-Tool $Scp "$opts -q `"$f`" root@${ip}:/opt/tweetpilot/secret/$n" $null
        Log "上传 ${n}：退出码 $code"
        $ok = $code -eq 0
    }
    if ($ok) { $ok = (Invoke-Tool $Ssh "$opts root@$ip bash -s" $CmdInstall) -eq 0 }
    if ($ok) {
        Set-Content -Path $Synced -Value $hash -Encoding ASCII
        Remove-Item $Failed -ErrorAction SilentlyContinue
        Log '配置已同步并生效'
    } else {
        Set-Content -Path $Failed -Value $hash -Encoding ASCII
        Log '同步没有完成，30 分钟后自动再试'
    }
}

# 日志只保留最近约 2000 行
try {
    $lines = Get-Content $LogFile -Encoding UTF8 -ErrorAction Stop
    if ($lines.Count -gt 3000) { $lines | Select-Object -Last 2000 | Set-Content $LogFile -Encoding UTF8 }
} catch { }
