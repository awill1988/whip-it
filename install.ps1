param(
    [Parameter(Mandatory=$true)][ValidateSet('claude','codex','agy')][string]$Client,
    [string]$Version = '0.1.0',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'whip-it/plugins')
)
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$') { throw 'invalid version' }
if (-not [IO.Path]::IsPathRooted($InstallDir)) { throw 'install directory must be absolute' }
$null = Get-Command $Client -ErrorAction Stop
$target = switch ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
    'X64' { 'x86_64-pc-windows-msvc' }
    'Arm64' { 'aarch64-pc-windows-msvc' }
    default { throw 'unsupported windows architecture' }
}
$null = New-Item -ItemType Directory -Force -Path $InstallDir
$workDir = Join-Path $InstallDir ('.whip-it-install-' + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $workDir
try {
    $archive = "whip-it-plugin-$target.zip"
    $base = "https://github.com/awill1988/whip-it/releases/download/v$Version"
    foreach ($name in @($archive, "$archive.sha256")) {
        Invoke-WebRequest "$base/$name" -OutFile (Join-Path $workDir $name) -TimeoutSec 120
    }
    $expected = ((Get-Content (Join-Path $workDir "$archive.sha256") -First 1) -split '\s+')[0]
    if ($expected -notmatch '^[a-fA-F0-9]{64}$') { throw 'invalid checksum' }
    $actual = (Get-FileHash (Join-Path $workDir $archive) -Algorithm SHA256).Hash
    if ($actual -ne $expected) { throw 'checksum mismatch; existing installation preserved' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead((Join-Path $workDir $archive))
    $staged = Join-Path $workDir 'plugin'
    $files = @('plugin.json','hooks.json','LICENSE','release.json',
        '.claude-plugin/plugin.json','.claude-plugin/marketplace.json',
        '.codex-plugin/plugin.json','hooks/hooks.json','hooks/codex-plugin.json','bin/whip-it.exe')
    try {
        foreach ($name in $files) {
            $entry = $zip.GetEntry($name)
            if ($null -eq $entry) { throw "archive is missing $name" }
            $output = Join-Path $staged $name
            $null = New-Item -ItemType Directory -Force -Path (Split-Path $output)
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $output)
        }
    } finally { $zip.Dispose() }
    $reported = & (Join-Path $staged 'bin/whip-it.exe') --version
    if ($LASTEXITCODE -ne 0 -or $reported -ne "whip-it $Version") { throw 'release version mismatch' }
    $destination = Join-Path $InstallDir "$Version-$target-$($expected.ToLowerInvariant())"
    if (Test-Path $destination) {
        foreach ($name in $files) {
            if ((Get-FileHash (Join-Path $staged $name)).Hash -ne (Get-FileHash (Join-Path $destination $name)).Hash) {
                throw 'existing release directory differs'
            }
        }
    } else { [IO.Directory]::Move($staged, $destination) }
    if ($Client -eq 'agy') {
        & $Client plugin install $destination
        if ($LASTEXITCODE -ne 0) { throw 'plugin registration failed; rerun setup to retry' }
    } else {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $registration = & $Client plugin marketplace add $destination 2>&1
            $registrationCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previousPreference }
        if ($registrationCode -ne 0) {
            if ($Client -eq 'codex' -and ($registration | Out-String).Contains("marketplace 'awill1988' is already added from a different source")) {
                & $Client plugin marketplace remove awill1988
                if ($LASTEXITCODE -ne 0) { throw 'marketplace removal failed; previous package files preserved' }
                & $Client plugin marketplace add $destination
                if ($LASTEXITCODE -ne 0) { throw 'marketplace registration failed; rerun setup to retry' }
            } else { throw "marketplace registration failed: $registration" }
        } else { $registration | Write-Output }
        if ($Client -eq 'claude') { & $Client plugin install whip-it@awill1988 }
        else { & $Client --enable plugins plugin add whip-it@awill1988 }
        if ($LASTEXITCODE -ne 0) { throw 'plugin registration failed; rerun setup to retry' }
    }
    Write-Output "installed whip-it $Version for $Client"
    Write-Output 'restart the client and trust its hooks, then verify a delegation denial'
} finally { Remove-Item -LiteralPath $workDir -Recurse -Force }
