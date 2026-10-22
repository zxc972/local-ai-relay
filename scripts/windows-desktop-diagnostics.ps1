# Read-only discovery. Does not change app configuration or send Agent messages.
# Compatible with Windows PowerShell 5.1 and PowerShell 7.
param([string]$ReportPath)
$ErrorActionPreference = 'Stop'
$roots = @()
$packages = @()
$warnings = @()

if (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue) {
    try {
        $packages = @(Get-AppxPackage | Where-Object {
            $_.Name -match '(?i)(OpenAI|Codex|ChatGPT|Claude|Anthropic)'
        } | Select-Object Name, Version, InstallLocation)
        $roots += @($packages | ForEach-Object { $_.InstallLocation })
    } catch { $warnings += 'Could not list current-user MSIX packages.' }
}

# Running GUI processes reveal installation paths even when no CLI is on PATH.
$apps = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '^(?i:Codex|ChatGPT|Claude)$'
} | ForEach-Object {
    try {
        $path = $_.Path
        if ($path) {
            $roots += Split-Path -Parent $path
            [pscustomobject]@{
                name = $_.ProcessName
                executable = $path
                version = (Get-Item -LiteralPath $path).VersionInfo.ProductVersion
            }
        }
    } catch { $warnings += 'A running application path was inaccessible.' }
})

foreach ($parent in @("$env:LOCALAPPDATA\Programs", $env:LOCALAPPDATA, $env:ProgramFiles)) {
    if ($parent -and (Test-Path -LiteralPath $parent)) {
        $roots += @(Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^(?i:Codex|ChatGPT|Claude|Anthropic|OpenAI)([ ._-].*)?$' } |
            ForEach-Object { $_.FullName })
    }
}
$roots = @($roots | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Sort-Object -Unique)
$candidates = @()
foreach ($root in $roots) {
    $candidates += @(Get-ChildItem -LiteralPath $root -Filter codex.exe -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName })
}
$onPath = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
if ($onPath) { $candidates += @($onPath | ForEach-Object { $_.Source }) }
$candidates = @($candidates | Sort-Object -Unique)

function Invoke-ReadOnlyProbe([string]$Executable, [string]$Arguments) {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            [void]$process.WaitForExit(2000)
            return @{ status = 'timeout'; command = $Arguments }
        }
        $out = $stdout.GetAwaiter().GetResult()
        $err = $stderr.GetAwaiter().GetResult()
        return @{
            status = 'completed'; command = $Arguments; exit_code = $process.ExitCode
            stdout = $out.Substring(0, [Math]::Min($out.Length, 4000))
            stderr = $err.Substring(0, [Math]::Min($err.Length, 2000))
        }
    } catch {
        return @{ status = 'error'; command = $Arguments; error = $_.Exception.Message }
    } finally { $process.Dispose() }
}

$codex = @($candidates | ForEach-Object {
    $binary = $_
    $version = Invoke-ReadOnlyProbe $binary '--version'
    $queue = $null
    $endpoint = $null
    if ($version.exit_code -eq 0 -and $version.stdout -match '(?i)codex') {
        $queue = Invoke-ReadOnlyProbe $binary 'queue --help'
        if ($queue.exit_code -eq 0 -and $queue.stdout -match '--thread') {
            $endpoint = Invoke-ReadOnlyProbe $binary 'app-server daemon version'
        }
    }
    @{ executable = $binary; version = $version; queue = $queue; existing_daemon = $endpoint }
})

$report = [ordered]@{
    schema = 'airelay.windows-desktop-diagnostics.v1'
    powershell = $PSVersionTable.PSVersion.ToString()
    packages = $packages
    running_apps = $apps
    searched_install_directories = $roots
    codex = $codex
    warnings = $warnings
    messages_sent = 0
    live_wake_verified = $false
    claude_desktop_channel_verified = $false
}
$output = $ReportPath
if (-not $output) {
    $output = Join-Path ([System.IO.Path]::GetTempPath()) ('airelay-desktop-' + [guid]::NewGuid().ToString('N') + '.json')
}
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $output -Encoding UTF8
Write-Output "Report saved: $output"
Write-Output 'No messages sent. This report is discovery only, not proof of automatic wake-up.'
Write-Output 'The report includes local installation paths; redact your Windows username before sharing if desired.'
Get-Content -LiteralPath $output
