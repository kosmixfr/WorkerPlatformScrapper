# Worker CESI pour PlatformScrapper

Worker **Python autonome** qui expose le scraping des documents partagés de
l'ENT CESI (`https://ent.cesi.fr`) comme fonctions d'une instance
[PlatformScrapper](https://github.com/kosmixfr/PlatformScrapper).

Il communique avec l'hôte via le contrat gRPC `warmplatform.ContainerGateway`
(stream bidirectionnel `Connect`) et implémente :

- l'enregistrement (`Hello`) avec les JSON Schemas des fonctions ;
- les invocations (`Invocation` → `InvocationResult`) ;
- les capacités hôte `dataset.push` et `proxy.get` (Proxy Chain) ;
- le KV daemon de la plateforme (`Get/Set/SetStream/GetStream/PublicUrl`),
  avec bascule automatique en streaming pour les PDF volumineux.

Le worker reste « chaud » : le process garde le stream gRPC ouvert et réutilise
les cookies CESI entre les invocations. Un login navigateur n'est refait que
lorsque les cookies ne sont plus valides.

> Le script `main.py` d'origine (CLI interactif) est conservé tel quel. Le worker
> vit dans `worker/` et n'en dépend pas.

## Sommaire

- [Fonctions exposées](#fonctions-exposées)
- [Construction de l'image](#construction-de-limage)
- [Utilisation dans PlatformScrapper](#utilisation-dans-platformscrapper)
- [Variables d'environnement](#variables-denvironnement)
- [Comportement KV / Dataset / Proxy](#comportement-kv--dataset--proxy)
- [Développement local](#développement-local)
- [Architecture des fichiers](#architecture-des-fichiers)
- [Limites connues](#limites-connues)

## Fonctions exposées

| Fonction | Rôle |
| --- | --- |
| `list_shared_documents` | Liste les sections et documents de `/<session>/documents-partages`. |
| `list_document_files` | Liste les fichiers joints d'une fiche document. |
| `download_shared_documents` | Télécharge les fichiers d'une session et les stocke en KV. |
| `download_file` | Télécharge un fichier précis (`/medias/fichier/...`). |
| `check_login` | Vérifie les identifiants CESI (accueil apprenant). |

### `list_shared_documents`

Entrée :

```json
{
  "session": "cpi-a2-info-26-27-nanterre-nr6dp201",
  "useHostProxy": true,
  "storeInKv": true
}
```

`session` accepte aussi une URL complète
(`https://ent.cesi.fr/<session>/documents-partages`) et en extrait le slug.
`entUsername` / `entPassword` permettent de surcharger les variables d'env.

Sortie : `session`, `url`, `count`, `sections[]` (`name`, `count`),
`documents[]` (`section`, `title`, `url`), `proxyUsed`, `durationMs`.

### `list_document_files`

Entrée :

```json
{
  "documentUrl": "https://ent.cesi.fr/cpi-a2-info-26-27-nanterre-nr6dp201/documents-partages/planning-s43"
}
```

Sortie : `documentUrl`, `count`, `files[]` (`filename`, `url`, `meta`,
`fileType`, `size`), `proxyUsed`, `durationMs`.

### `download_shared_documents`

Entrée :

```json
{
  "session": "cpi-a2-info-26-27-nanterre-nr6dp201",
  "onlySections": ["plannings"],
  "titleContains": "S43",
  "includeFileTypes": ["PDF"],
  "limit": 0,
  "storeInKv": true,
  "kvStore": "",
  "publicUrlExpiresIn": 0
}
```

- `onlySections` : noms de sections exacts, insensibles à la casse.
- `titleContains` : filtre sur le titre (insensible à la casse).
- `includeFileTypes` : ex. `["PDF"]` ; vide = tous.
- `limit` : nombre max de fiches parcourues (0 = toutes).
- `maxFileSizeBytes` : défaut 100 Mo.
- `kvStore` : vide = Default KV de la Run ; sinon Named KV Store.
- `publicUrlExpiresIn` : 0 = 7 jours, max 30 jours.

Sortie : `documentsScanned`, `filesFound`, `filesDownloaded`,
`files[]` (section, titre, URL, nom, SHA-256, MIME, clé KV, URL publique),
`errors[]` (erreurs non bloquantes), `proxyUsed`, `durationMs`.

### `download_file`

Entrée :

```json
{
  "fileUrl": "https://ent.cesi.fr/medias/fichier/....-pdf",
  "filename": "planning S43.pdf",
  "referer": "https://ent.cesi.fr/<session>/documents-partages/planning-s43",
  "storeInKv": true
}
```

Sortie : `filename`, `fileUrl`, `sizeBytes`, `sha256`, `mimeType`,
`storedInKv`, `kvStore`, `kvKey`, `publicUrl`, `durationMs`.

### `check_login`

Entrée : `{ "useHostProxy": true }` (ou des identifiants explicites).
Sortie : `ok`, `username`, `proxyUsed`, `durationMs`.

## Construction de l'image

Le worker n'est **pas** un service `docker-compose` : l'hôte PlatformScrapper
lance les conteneurs à la demande via Dockerode. Construisez l'image une fois :

```powershell
# Windows
.\build-worker.ps1
.\build-worker.ps1 -Tag v1.0.0
```

```bash
# Linux / macOS / CI
./build-worker.sh
./build-worker.sh v1.0.0
```

L'image produite est `warm-cesi-worker:latest`. Le contexte de build est la
racine de ce projet ; `.env`, `cookies.json` et `downloads/` sont exclus par
`.dockerignore`.

> L'image doit être visible par le daemon Docker de la machine qui héberge
> PlatformScrapper. Sur Windows avec Bun, pensez à activer
> « Expose daemon on tcp://localhost:2375 without TLS » dans Docker Desktop (ou
> à définir `DOCKER_HOST`), comme indiqué dans le README de PlatformScrapper.

## Utilisation dans PlatformScrapper

1. Démarrez la plateforme (`docker compose up --build` dans le dépôt
   PlatformScrapper).
2. Dans le dashboard, créez une **nouvelle instance** :
   - **Image** : `warm-cesi-worker:latest`
   - **Worker ID** : `cesi-worker` (ou un identifiant unique
     `[A-Za-z0-9_-]{1,80}`)
3. Renseignez les identifiants dans l'onglet **Configuration** de l'instance :

   ```text
   CESI_USERNAME=prenom.nom@viacesi.fr
   CESI_PASSWORD=...
   ```

   L'hôte injecte automatiquement `WORKER_ID`, `PLATFORM_GRPC_ADDRESS`,
   `PLATFORM_WORKER_KEY`, `PLATFORM_KV_RUN_TOKEN`, `PLATFORM_KV_ADDRESS`,
   `PLATFORM_PROXY_CHAIN_ADDRESS` et `PLATFORM_PROXY_CHAIN_TOKEN`.
4. Démarrez l'instance. Le worker s'enregistre en gRPC et l'hôte lui attribue
   les routes :

   ```text
   POST /api/{userId}/{instanceId}/list_shared_documents
   POST /api/{userId}/{instanceId}/list_document_files
   POST /api/{userId}/{instanceId}/download_shared_documents
   POST /api/{userId}/{instanceId}/download_file
   POST /api/{userId}/{instanceId}/check_login
   ```

Les fonctions sont aussi disponibles via MCP (`cesi-worker__<fonction>`) et le
Playground du dashboard.

## Variables d'environnement

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `CESI_USERNAME` | — | Identifiant ENT (obligatoire si non fourni en entrée). |
| `CESI_PASSWORD` | — | Mot de passe ENT (obligatoire si non fourni en entrée). |
| `CESI_COOKIES_FILE` | `/data/cesi-cookies.json` | Cache des cookies par identifiant (hors image). |
| `CESI_HEADLESS` | `true` | `false` = Chromium visible (nécessite un display X). |
| `CESI_WORK_DIR` | `/data` | Répertoire de travail réservé au worker. |
| `WORKER_ID` | `cesi-worker` | Injecté par l'hôte. |
| `PLATFORM_GRPC_ADDRESS` | `127.0.0.1:50051` | Injecté par l'hôte. |
| `PLATFORM_WORKER_KEY` | `demo-worker-key` | Injecté par l'hôte. |
| `PLATFORM_KV_ADDRESS` | `unix:///var/run/platform/kv.sock` | Injecté par l'hôte. |
| `PLATFORM_KV_RUN_TOKEN` | — | Injecté par l'hôte (KV indisponible sans). |
| `PLATFORM_PROXY_CHAIN_ADDRESS` | — | Injecté par l'hôte. |
| `PLATFORM_PROXY_CHAIN_TOKEN` | — | Injecté par l'hôte. |

Les identifiants ne sont **jamais** copiés dans l'image : `.env` est exclu par
`.dockerignore`. Utilisez la Configuration de l'instance ou les paramètres
`entUsername` / `entPassword` (marqués `[secret]` dans les schémas, donc
redactés par la plateforme).

## Comportement KV / Dataset / Proxy

- **Default KV de la Run** : index (`documents:<session>:list`), compteurs et
  métriques de dernière exécution. Purgé selon `KV_RETENTION_DAYS`.
- **Named KV Store** : passez `kvStore: "cesi-documents"` pour un stockage
  partageable et persistant. Les clés ont la forme
  `documents/<session>/<sha16>-<titre>-<fichier>`.
- **Fichiers** : stockés via `Set` (< 3,5 Mo) ou `SetStream` (au-delà), avec
  `content_type` réel. `download_shared_documents` / `download_file` génèrent
  une URL publique signée (`PublicUrl`) si `storeInKv` est actif.
- **Dataset** : `cesi-shared-documents`, `cesi-document-files`, `cesi-downloads`
  (un item par document / fichier).
- **Proxy Chain** : si `useHostProxy` est vrai (défaut), le worker demande
  `platform.proxy.get()` et utilise l'URL + le jeton de la chaîne pour Chromium
  et pour `AsyncFetcher`. En cas d'indisponibilité, il tente en direct et
  journalise un avertissement.
- Toutes les écritures KV/Dataset sont **best-effort** : un KV indisponible
  n'empêche pas le scraping ; l'erreur est journalisée et, pour les fichiers,
  reportée dans `errors[]`.

## Développement local

Prérequis : Python 3.11+ (testé avec 3.15), Docker optionnel.

```powershell
# Dépendances
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\patchright install chromium

# Vérification statique des schémas et des fonctions (sans gRPC)
.\.venv\Scripts\python -m worker --check

# Lancement local : l'hôte PlatformScrapper doit écouter sur 127.0.0.1:50051
$env:CESI_USERNAME="prenom.nom@viacesi.fr"
$env:CESI_PASSWORD="..."
python -m worker
```

Pour le KV en local, exportez `PLATFORM_KV_ADDRESS` et
`PLATFORM_KV_RUN_TOKEN` fournis par l'hôte (ou laissez le KV désactivé et
utilisez `storeInKv: false`).

### Régénérer les stubs gRPC

Les stubs sont versionnés dans `worker/generated/`. Pour les régénérer après
une mise à jour du contrat PlatformScrapper :

```bash
cp <PlatformScrapper>/proto/{platform,kv}.proto proto/
python -m grpc_tools.protoc -I proto \
  --python_out=worker/generated \
  --grpc_python_out=worker/generated \
  proto/platform.proto proto/kv.proto
```

Puis remplacez dans `worker/generated/*_pb2_grpc.py` les imports absolus
(`import platform_pb2 as ...`) par des imports relatifs
(`from . import platform_pb2 as ...`).

## Architecture des fichiers

```text
cesiscrapper/
  main.py                     # CLI interactif d'origine (inchangé)
  worker/
    __main__.py               # point d'entrée python -m worker
    platform_worker.py        # runtime gRPC (Hello, Invocation, HostRequest)
    kv_client.py              # client du KV daemon (Default/Named, streaming)
    functions.py              # handlers et câblage KV/Dataset
    schemas.py                # JSON Schemas draft-07 des entrées/sorties
    cesi_scraper.py           # login WAYF/ADFS + parsing + téléchargements
    generated/                # stubs platform_pb2 / kv_pb2
  proto/
    platform.proto            # contrat copié depuis PlatformScrapper
    kv.proto                  # contrat KV copié depuis PlatformScrapper
  Dockerfile
  requirements.txt
  build-worker.ps1 / .sh
```

## Limites connues

- Le worker est prévu pour tourner **headless** ; `CESI_HEADLESS=false` exige un
  serveur X (l'image ne fournit pas Xvfb/x11vnc).
- Un seul login est effectué à la fois (`asyncio.Lock`) : les invocations
  concurrentes partagent les mêmes cookies et le même compte.
- Les cookies sont conservés dans le conteneur (`/data`) ; ils sont perdus à
  l'arrêt du conteneur, ce qui déclenche un nouveau login au démarrage suivant.
- Le login navigateur peut échouer si l'ENT active une MFA ou modifie ses
  sélecteurs ; dans ce cas `check_login` renvoie l'erreur détaillée dans les
  logs de l'instance (onglet Logs live).
- Le worker ne contourne pas les restrictions d'accès de l'ENT : il utilise les
  identifiants fournis par l'utilisateur et les mêmes parcours que le site.
