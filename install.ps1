param(
    [string]$Version = '0.1.0',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'whip-it/bin'),
    [switch]$NoModifyPath
)
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'invalid release version' }
if (-not [IO.Path]::IsPathRooted($InstallDir)) { throw 'install directory must be absolute' }
$architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$target = switch ($architecture) {
    'X64' { 'x86_64-pc-windows-msvc' }
    'Arm64' { 'aarch64-pc-windows-msvc' }
    default { throw 'unsupported windows architecture' }
}
$null = New-Item -ItemType Directory -Force -Path $InstallDir
$workDir = Join-Path $InstallDir ('.whip-it-install-' + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $workDir
try {
    $archive = "whip-it-$target.zip"
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
    $staged = Join-Path $workDir 'whip-it.exe'
    try {
        $entry = $zip.GetEntry('whip-it.exe')
        if ($null -eq $entry) { throw 'archive is missing whip-it.exe' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $staged)
    } finally { $zip.Dispose() }
    & $staged --version
    if ($LASTEXITCODE -ne 0) { throw 'downloaded executable failed verification' }
    $destination = Join-Path $InstallDir 'whip-it.exe'
    if (Test-Path $destination) {
        if ((Get-Item $destination).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'refusing to replace a managed symlink'
        }
        [IO.File]::Replace($staged, $destination, (Join-Path $workDir 'previous.exe'))
    } else { [IO.File]::Move($staged, $destination) }
    if (-not $NoModifyPath) {
        $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
        if (($userPath -split ';') -notcontains $InstallDir) {
            [Environment]::SetEnvironmentVariable('PATH', "$InstallDir;$userPath", 'User')
        }
        $env:PATH = "$InstallDir;$env:PATH"
    }
    Write-Output "installed: $destination"
    Write-Output 'restart your terminal and agent client before enabling hooks'
} finally { Remove-Item -LiteralPath $workDir -Recurse -Force }
