# Build de l'image du worker CESI (Windows / PowerShell).
#
# Usage (depuis n'importe où) :
#   .\build-worker.ps1                  # tague warm-cesi-worker:latest
#   .\build-worker.ps1 -Tag v1.2.0      # tague warm-cesi-worker:v1.2.0
#   .\build-worker.ps1 -NoCache         # reconstruit sans cache Docker
#
# Le contexte de build est la RACINE du dépôt (le Dockerfile vit dans
# cesiscrapper/). L'image n'est pas un service docker-compose : créez une
# instance dans le dashboard PlatformScrapper avec `warm-cesi-worker:latest`.

param(
  [string]$Tag = "warm-cesi-worker:latest",
  [switch]$NoCache
)

$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$RepoRoot = Split-Path $ProjectDir -Parent
$Dockerfile = Join-Path $ProjectDir "Dockerfile"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker est introuvable dans le PATH."
}

if (-not (Test-Path $Dockerfile)) {
  throw "Dockerfile introuvable : $Dockerfile"
}

$DockerArgs = @("build", "-f", $Dockerfile, "-t", $Tag)
if ($NoCache) {
  $DockerArgs += "--no-cache"
}
$DockerArgs += $RepoRoot

Write-Host "==> Build de l'image worker CESI : $Tag"
Write-Host "    (context: $RepoRoot, dockerfile: $Dockerfile)"

& docker @DockerArgs
if ($LASTEXITCODE -ne 0) {
  throw "Le build a échoué (code $LASTEXITCODE)."
}

Write-Host ""
Write-Host "Build terminé. Créez une instance PlatformScrapper avec l'image : $Tag"
