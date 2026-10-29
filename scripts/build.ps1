param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& $Python -c "import sys; assert sys.version_info[:2] == (3, 12), 'Python 3.12 is required'"
if ($LASTEXITCODE -ne 0) { throw 'Use Python 3.12 x64.' }
$version = & $Python -c "from desktop import __version__; print(__version__)"
& $Python -m PyInstaller --noconfirm --distpath dist --workpath build chaoxing.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
$release = Join-Path $projectRoot "dist\ChaoxingDesktop-v$version-windows-x64"
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $release 'LICENSE')
Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination (Join-Path $release 'README.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\USER_GUIDE.md') -Destination (Join-Path $release 'USER_GUIDE.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'NOTICE.md') -Destination (Join-Path $release 'NOTICE.md')
Write-Output "Built: $release"
