$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $repoRoot "reports"
$reportBase = "final_submission"
$reportTex = Join-Path $reportDir "$reportBase.tex"

if (-not (Test-Path $reportTex)) {
    throw "Report source not found: $reportTex"
}

$pdflatex = Get-Command pdflatex -ErrorAction SilentlyContinue
if ($null -eq $pdflatex) {
    throw "pdflatex was not found on PATH. Install a TeX distribution, then rerun this script."
}

Push-Location $reportDir
try {
    & $pdflatex.Source -interaction=nonstopmode -halt-on-error $reportBase
    & $pdflatex.Source -interaction=nonstopmode -halt-on-error $reportBase
}
finally {
    Pop-Location
}

Write-Output (Join-Path $reportDir "$reportBase.pdf")
