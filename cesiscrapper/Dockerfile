# Worker CESI autonome pour la Warm Scraper Platform.
#
# L'image n'est PAS un service docker-compose : elle est construite une fois puis
# lancée à la demande par l'hôte (Dockerode) depuis le dashboard PlatformScrapper.
# Aucun port n'est publié : le worker se connecte en sortie à PLATFORM_GRPC_ADDRESS
# et au KV daemon (PLATFORM_KV_ADDRESS).
#
# Contexte de build : la RACINE du dépôt (recommandé) :
#   docker build -f cesiscrapper/Dockerfile -t warm-cesi-worker:latest .
#
# Pour construire depuis le dossier cesiscrapper/ lui-même :
#   docker build --build-arg PROJECT_DIR=. -t warm-cesi-worker:latest .
FROM python:3.13-slim-bookworm

# Chemin du projet dans le contexte de build. Par défaut le contexte est la
# racine du dépôt ; les scripts build-worker.* passent déjà ce contexte.
ARG PROJECT_DIR=cesiscrapper

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CESI_HEADLESS=true \
    CESI_COOKIES_FILE=/data/cesi-cookies.json \
    CESI_WORK_DIR=/data

# Dépendances Python puis Chromium (patchright) + bibliothèques système requises.
# PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD évite un téléchargement automatique pendant
# `pip install` ; le CLI `patchright install` télécharge explicitement ensuite.
# --with-deps installe les paquets apt nécessaires au runtime Chromium.
COPY ${PROJECT_DIR}/requirements.txt /tmp/requirements.txt
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install --no-cache-dir --retries 5 -r /tmp/requirements.txt \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

# Utilisateur non-root : Chromium refuse de tourner en root avec le sandbox actif.
RUN useradd --create-home --uid 1000 worker \
    && mkdir -p /app /data \
    && chown -R worker:worker /app /data /ms-playwright

WORKDIR /app

# Le worker embarque ses stubs gRPC générés ; `proto/` est conservé pour référence
# et pour régénérer les stubs sans dépendre du dépôt PlatformScrapper.
COPY --chown=worker:worker ${PROJECT_DIR}/worker ./worker
COPY --chown=worker:worker ${PROJECT_DIR}/proto ./proto
COPY --chown=worker:worker ${PROJECT_DIR}/README.md ./README.md

USER worker

# Le container reste « chaud » : le process garde le stream gRPC ouvert et
# réutilise les cookies CESI entre les invocations.
CMD ["python", "-m", "worker"]
