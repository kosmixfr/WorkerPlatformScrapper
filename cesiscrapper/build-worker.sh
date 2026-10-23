#!/usr/bin/env bash
# Build de l'image du worker CESI (Linux / macOS / CI).
#
# Usage :
#   ./build-worker.sh                  # tague warm-cesi-worker:latest
#   ./build-worker.sh v1.2.0           # tague warm-cesi-worker:v1.2.0
#
# Le contexte de build est la racine de ce projet. L'image n'est pas un service
# docker-compose : créez une instance dans le dashboard PlatformScrapper avec
# l'image `warm-cesi-worker:latest`.

set -euo pipefail

TAG="${1:-warm-cesi-worker:latest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$REPO_ROOT/Dockerfile"

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "Dockerfile introuvable : $DOCKERFILE" >&2
  exit 1
fi

echo "==> Build de l'image worker CESI : $TAG"
echo "    (context: $REPO_ROOT, dockerfile: $DOCKERFILE)"

docker build -f "$DOCKERFILE" -t "$TAG" "$REPO_ROOT"

echo ""
echo "Build terminé. Créez une instance PlatformScrapper avec l'image : $TAG"
