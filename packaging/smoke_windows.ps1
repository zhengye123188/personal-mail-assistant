# Exercise the published installer in the Windows build runner's clean profile.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $repo 'dist\PersonalMailAssistant-Windows-x64-Setup.exe'
$testDir = Join-Path ([IO.Path]::GetTempPath()) ('maildesk-' + [guid]::NewGuid())
$dataDir = Join-Path $env:LOCALAPPDATA 'PersonalMailAssistant'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\io.github.zhengye123188.personal-mail-assistant_is1'
if ((Test-Path $dataDir) -or (Test-Path $uninstallKey)) {
    throw 'Run the installer smoke test in a fresh Windows user profile to protect existing mail data.'
}

$flags = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
$setup = Start-Process $installer -ArgumentList ($flags + "/DIR=`"$testDir`"") -Wait -PassThru
if ($setup.ExitCode -ne 0) { throw "Setup failed with exit code $($setup.ExitCode)." }
$app = Join-Path $testDir 'PersonalMailAssistant.exe'
$uninstaller = Join-Path $testDir 'unins000.exe'
$first = $null
$second = $null
try {
    if (-not (Test-Path $uninstallKey)) { throw 'Missing per-user uninstall entry.' }
    foreach ($leaf in @('qwindows.dll', 'QtWebEngineProcess.exe', 'qtwebengine_resources.pak')) {
        if (-not (Get-ChildItem $testDir -Recurse -File -Filter $leaf | Select-Object -First 1)) {
            throw "Installed application is missing $leaf."
        }
    }
    $first = Start-Process $app -PassThru
    $deadline = [datetime]::UtcNow.AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 250
        $first.Refresh()
        if ($first.HasExited) { throw "Application exited on startup with code $($first.ExitCode)." }
        $ready = (Test-Path (Join-Path $dataDir 'accounts.sqlite3')) -and ($first.MainWindowHandle -ne 0)
    } until ($ready -or ([datetime]::UtcNow -gt $deadline))
    if (-not $ready) { throw 'The installed application did not open its mailbox window.' }

    $second = Start-Process $app -PassThru
    if (-not $second.WaitForExit(20000) -or $second.ExitCode -ne 0 -or $first.HasExited) {
        throw 'The second launch did not activate the running application.'
    }
} finally {
    foreach ($process in @($second, $first)) {
        if ($process -and -not $process.HasExited) {
            if (-not $process.CloseMainWindow() -or -not $process.WaitForExit(10000)) {
                $process.Kill()
                $process.WaitForExit()
            }
        }
    }
    $uninstall = Start-Process $uninstaller -ArgumentList $flags -Wait -PassThru
    if ($uninstall.ExitCode -ne 0 -or (Test-Path $app) -or (Test-Path $uninstallKey)) {
        throw 'The installer did not uninstall cleanly.'
    }
}
if (-not (Test-Path (Join-Path $dataDir 'accounts.sqlite3'))) {
    throw 'Uninstall unexpectedly removed the user database.'
}
Write-Output 'Verified per-user setup, Qt reader resources, desktop launch, single-instance activation and uninstall.'
