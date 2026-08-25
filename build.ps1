<#
.SYNOPSIS
    Fetches the bundled tools and builds ytdl.exe with PyInstaller.

.DESCRIPTION
    This is the single build definition, used both by the GitHub Actions workflow
    and by anyone building locally. It downloads yt-dlp, deno, ffmpeg and ffprobe
    into the working directory, then runs PyInstaller.

    None of the downloaded files are tracked in git - they are build inputs,
    fetched fresh every time. versions.json says which versions to fetch.

.PARAMETER YtDlpVersion
    Which yt-dlp to bundle. "latest" (default) resolves to the newest release.

.EXAMPLE
    .\build.ps1
    Builds dist\ytdl.exe using the newest yt-dlp and the versions in versions.json.
#>
[CmdletBinding()]
param(
    [string] $YtDlpVersion = "latest"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # makes Invoke-WebRequest downloads much faster

$root = $PSScriptRoot
Set-Location $root

$versions = Get-Content (Join-Path $root "versions.json") -Raw | ConvertFrom-Json

# Everything that ends up inside the final exe. Added to as we fetch.
$bundle = [System.Collections.Generic.List[string]]::new()

function Get-File {
    param([string] $Url, [string] $OutFile)
    Write-Host "  downloading $([System.IO.Path]::GetFileName($OutFile)) ..."
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    if (-not (Test-Path $OutFile)) { throw "download produced no file: $Url" }
    $kb = [math]::Round((Get-Item $OutFile).Length / 1KB)
    if ($kb -lt 100) { throw "download looks truncated ($kb KB): $Url" }
}

# --- yt-dlp ------------------------------------------------------------------
if ($YtDlpVersion -eq "latest") {
    $ytUrl = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
} else {
    $ytUrl = "https://github.com/yt-dlp/yt-dlp/releases/download/$YtDlpVersion/yt-dlp.exe"
}
Write-Host "yt-dlp ($YtDlpVersion)"
Get-File -Url $ytUrl -OutFile (Join-Path $root "yt-dlp.exe")
$bundle.Add("yt-dlp.exe")

# Ask the binary what it actually is, rather than trusting the tag we asked for.
$ytResolved = (& (Join-Path $root "yt-dlp.exe") --version 2>&1 | Select-Object -First 1).Trim()
Write-Host "  -> yt-dlp $ytResolved"

# --- deno (yt-dlp's JavaScript runtime, needed for YouTube's challenges) ------
Write-Host "deno ($($versions.deno))"
$denoZip = Join-Path $root "deno.zip"
Get-File -Url "https://github.com/denoland/deno/releases/download/$($versions.deno)/deno-x86_64-pc-windows-msvc.zip" -OutFile $denoZip
Expand-Archive -Path $denoZip -DestinationPath (Join-Path $root "_deno") -Force
$denoSrc = Get-ChildItem -Path (Join-Path $root "_deno") -Filter "deno.exe" -Recurse | Select-Object -First 1
if (-not $denoSrc) { throw "deno.exe not found inside the downloaded zip" }
Copy-Item $denoSrc.FullName (Join-Path $root "deno.exe") -Force
$bundle.Add("deno.exe")

# --- ffmpeg + ffprobe --------------------------------------------------------
# Two shapes are supported, chosen by ffmpeg_variant in versions.json:
#   *-gpl / *-lgpl          each .exe is fully self-contained (~145 MB each)
#   *-gpl-shared / *-lgpl-shared  small .exe files plus shared .dll files
# The shared variants are much smaller overall because the two exes stop carrying
# a duplicate copy of the same libraries.
$variant = $versions.ffmpeg_variant
Write-Host "ffmpeg ($($versions.ffmpeg) / $variant)"
$ffZip = Join-Path $root "ffmpeg.zip"
Get-File -Url "https://github.com/BtbN/FFmpeg-Builds/releases/download/$($versions.ffmpeg)/ffmpeg-master-latest-$variant.zip" -OutFile $ffZip
Expand-Archive -Path $ffZip -DestinationPath (Join-Path $root "_ffmpeg") -Force

foreach ($name in @("ffmpeg.exe", "ffprobe.exe")) {
    $src = Get-ChildItem -Path (Join-Path $root "_ffmpeg") -Filter $name -Recurse | Select-Object -First 1
    if (-not $src) { throw "$name not found inside the downloaded zip" }
    Copy-Item $src.FullName (Join-Path $root $name) -Force
    $bundle.Add($name)
}

# Shared builds need their DLLs alongside the exes. PyInstaller unpacks everything
# into one folder at runtime, and Windows looks for DLLs next to the .exe that
# needs them, so putting them all at the top level is what makes this work.
# ffplay.exe is deliberately skipped - it is ~17 MB and nothing here uses it.
if ($variant -like "*-shared") {
    $dlls = Get-ChildItem -Path (Join-Path $root "_ffmpeg") -Filter "*.dll" -Recurse
    if (-not $dlls) { throw "variant '$variant' is a shared build but no .dll files were found" }
    foreach ($dll in $dlls) {
        Copy-Item $dll.FullName (Join-Path $root $dll.Name) -Force
        $bundle.Add($dll.Name)
    }
    Write-Host "  -> $($dlls.Count) shared library file(s)"
}

$ffResolved = ((& (Join-Path $root "ffmpeg.exe") -version 2>&1 | Select-Object -First 1) -split '\s+')[2]
Write-Host "  -> ffmpeg $ffResolved"

# --- build -------------------------------------------------------------------
$inputMb = [math]::Round((($bundle | ForEach-Object { (Get-Item (Join-Path $root $_)).Length }) | Measure-Object -Sum).Sum / 1MB, 1)
Write-Host ""
Write-Host "bundling $($bundle.Count) file(s), $inputMb MB before compression"

$addData = @()
foreach ($f in $bundle) { $addData += "--add-data"; $addData += "$f;." }

Write-Host "building with PyInstaller ..."
pyinstaller --onefile --noconsole $addData ytdl.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$built = Join-Path $root "dist\ytdl.exe"
if (-not (Test-Path $built)) { throw "PyInstaller reported success but dist\ytdl.exe is missing" }

# --- tidy up the intermediate downloads --------------------------------------
Remove-Item (Join-Path $root "_deno"), (Join-Path $root "_ffmpeg") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $denoZip, $ffZip -Force -ErrorAction SilentlyContinue

$mb = [math]::Round((Get-Item $built).Length / 1MB, 1)
Write-Host ""
Write-Host "built dist\ytdl.exe ($mb MB)  [yt-dlp $ytResolved, ffmpeg $ffResolved, deno $($versions.deno)]"

# Hand the resolved versions back to the workflow.
if ($env:GITHUB_OUTPUT) {
    "ytdlp_version=$ytResolved"  | Out-File -FilePath $env:GITHUB_OUTPUT -Append
    "ffmpeg_version=$ffResolved" | Out-File -FilePath $env:GITHUB_OUTPUT -Append
    "built_mb=$mb"               | Out-File -FilePath $env:GITHUB_OUTPUT -Append
}
