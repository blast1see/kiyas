<#
.SYNOPSIS
    Creates a self-contained environment for kiyas and verifies it.

.DESCRIPTION
    Everything this script builds lives in the .venv directory next to it.
    Nothing is written to Program Files, nothing is added to PATH, and no
    registry keys are created. Deleting .venv undoes the whole thing.

    The one exception is %APPDATA%\vapoursynth\vapoursynth.toml, which
    'vapoursynth config' writes so that VSScript can find this interpreter.
    That file is additive and keyed by this environment's own vsscript.dll
    path, so other VapourSynth installations are unaffected.

.PARAMETER PythonVersion
    Which interpreter to build the virtualenv from. Defaults to 3.13.
    VapourSynth R78 ships an abi3 wheel built for cp312, so 3.12 and newer all
    work; 3.13 is the default because the surrounding ecosystem (awsmfunc,
    vstools) is most widely tested there.

.PARAMETER SkipVapourSynth
    Set up the environment but do not install the VapourSynth stack. kiyas
    still works through the ffmpeg engine. Useful for a quick look, and for
    machines where the ~400 MB download is not wanted.

.PARAMETER Force
    Delete and rebuild .venv even if one already exists. Required to change the
    Python version of an existing environment.

.EXAMPLE
    .\bootstrap.ps1
    .\bootstrap.ps1 -PythonVersion 3.12 -Force
    .\bootstrap.ps1 -SkipVapourSynth
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13",
    [switch]$SkipVapourSynth,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

# --------------------------------------------------------------- interpreter
Write-Step "Locating Python $PythonVersion"
# -join matters: 'py -0p' returns an array, and PowerShell's -match on an array
# filters elements instead of returning a boolean, so testing the array directly
# is always truthy and the check silently passes.
$available = (& py -0p 2>$null) -join "`n"
if (-not $available) {
    throw "The 'py' launcher was not found. Install Python from python.org, which includes it."
}
if ($available -notmatch [regex]::Escape("-V:$PythonVersion")) {
    Write-Host $available
    throw "Python $PythonVersion is not installed. Pick one of the versions listed above with -PythonVersion, or install $PythonVersion from python.org."
}
Write-Note (& py "-$PythonVersion" -c "import sys; print(sys.executable)")

# ------------------------------------------------------------------- venv
if ((Test-Path $python) -and $Force) {
    Write-Step "Removing the existing virtualenv (-Force)"
    Remove-Item $venv -Recurse -Force
}

if (Test-Path $python) {
    # Reusing a virtualenv built from a different interpreter is a silent trap:
    # you ask for 3.12, get the 3.13 environment you already had, and every
    # version-specific symptom you were chasing stays exactly the same.
    $existing = (& $python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))").Trim()
    if ($existing -ne $PythonVersion) {
        throw "The existing .venv is Python $existing but $PythonVersion was requested. Re-run with -Force to rebuild it, or pass -PythonVersion $existing to keep it."
    }
    Write-Step "Reusing existing virtualenv at .venv (Python $existing)"
} else {
    Write-Step "Creating virtualenv at .venv"
    & py "-$PythonVersion" -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

Write-Step "Upgrading pip"
& $python -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

# ------------------------------------------------------------------ kiyas
Write-Step "Installing kiyas (editable) with dev and audio extras"
& $python -m pip install -e "$root[dev,audio]"
if ($LASTEXITCODE -ne 0) { throw "kiyas installation failed" }

# ------------------------------------------------------------ vapoursynth
if ($SkipVapourSynth) {
    Write-Step "Skipping the VapourSynth stack (-SkipVapourSynth)"
    Write-Note "kiyas will fall back to the ffmpeg engine. Run 'kiyas setup' later to add it."
} else {
    Write-Step "Installing the VapourSynth stack"
    Write-Note "Roughly 300-500 MB. Everything lands inside .venv."
    & $python -m kiyas.cli setup
    if ($LASTEXITCODE -ne 0) { throw "VapourSynth setup failed" }
}

# ----------------------------------------------------------------- verify
Write-Step "Verifying the environment"
& $python -m kiyas.cli doctor
$doctorExit = $LASTEXITCODE

Write-Host ""
Write-Host "Activate the environment with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "Then run:" -ForegroundColor Green
Write-Host "    kiyas doctor"

exit $doctorExit
