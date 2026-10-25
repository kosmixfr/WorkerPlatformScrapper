#!/usr/bin/env bash
# Build de l'image du worker CESI (Linux / macOS / CI).
#
# Usage (depuis n'importe où) :
#   ./build-worker.sh                  # tague warm-cesi-worker:latest
#   ./build-worker.sh v1.2.0           # tague warm-cesi-worker:v1.2.0
#   ./build-worker.sh v1.2.0 --no-cache
#
# Le contexte de build est la RACINE du dépôt (le Dockerfile vit dans
# cesiscrapper/). L'image n'est pas un service docker-compose : créez une
# instance dans le dashboard PlatformScrapper avec `warm-cesi-worker:latest`.

set -euo pipefail

TAG="warm-cesi-worker:latest"
NO_CACHE=0

for arg in "$@"; do
  case "$arg" in
    --no-cache) NO_CACHE=1 ;;
    -*) echo "Option inconnue : $arg" >&2; exit 1 ;;
    *) TAG="$arg" ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
DOCKERFILE="$PROJECT_DIR/Dockerfile"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker est introuvable dans le PATH." >&2
  exit 1
fi

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "Dockerfile introuvable : $DOCKERFILE" >&2
  exit 1
fi

BUILD_ARGS=(-f "$DOCKERFILE" -t "$TAG")
if [[ "$NO_CACHE" == "1" ]]; then
  BUILD_ARGS+=(--no-cache)
fi

echo "==> Build de l'image worker CESI : $TAG"
echo "    (context: $REPO_ROOT, dockerfile: $DOCKERFILE)"

docker build "${BUILD_ARGS[@]}" "$REPO_ROOT"

echo ""
echo "Build terminé. Créez une instance PlatformScrapper avec l'image : $TAG"
