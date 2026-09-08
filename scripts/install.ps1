param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE "bin")
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$launcher = Join-Path $InstallDir "ykt_signin.cmd"
$launcherContent = @"
@echo off
setlocal
python "$projectRoot\launch.py" %*
"@
Set-Content -Path $launcher -Value $launcherContent -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ";" | Where-Object { $_ -and $_.Trim() })
if ($pathEntries -notcontains $InstallDir) {
    $newPath = (($pathEntries + $InstallDir) -join ";")
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$env:Path;$InstallDir"
}

Write-Host "已安装 ykt_signin -> $launcher"
Write-Host "请打开新的 PowerShell 窗口，然后运行：ykt_signin"
