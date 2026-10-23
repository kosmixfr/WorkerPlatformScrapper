# Build de l'image du worker CESI (Windows / PowerShell).
#
# Usage (depuis n'importe où) :
#   .\build-worker.ps1                  # tague warm-cesi-worker:latest
#   .\build-worker.ps1 -Tag v1.2.0      # tague warm-cesi-worker:v1.2.0
#
# Le contexte de build est la racine de ce projet. L'image n'est pas un service
# docker-compose : créez une instance dans le dashboard PlatformScrapper avec
# l'image `warm-cesi-worker:latest`.

param(
  [string]$Tag = "warm-cesi-worker:latest"
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$Dockerfile = Join-Path $RepoRoot "Dockerfile"

if (-not (Test-Path $Dockerfile)) {
  throw "Dockerfile introuvable : $Dockerfile"
}

Write-Host "==> Build de l'image worker CESI : $Tag"
Write-Host "    (context: $RepoRoot, dockerfile: $Dockerfile)"

docker build -f $Dockerfile -t $Tag $RepoRoot
if ($LASTEXITCODE -ne 0) {
  throw "Le build a échoué (code $LASTEXITCODE)."
}

Write-Host ""
Write-Host "Build terminé. Créez une instance PlatformScrapper avec l'image : $Tag"
