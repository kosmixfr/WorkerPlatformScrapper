"""Fonctions exposées par le worker CESI.

Chaque handler :
1. résout les identifiants (input ou `CESI_USERNAME` / `CESI_PASSWORD`) ;
2. obtient un proxy via la Proxy Chain si demandé ;
3. s'authentifie (cookies réutilisés) puis scrape `ent.cesi.fr` ;
4. pousse les résultats dans le Dataset et le KV de la plateforme (best-effort).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from .cesi_scraper import (
    CesiScraper,
    Credentials,
    ProxyInfo,
    assert_cesi_url,
    build_kv_key,
    normalize_session,
)
from .platform_worker import PlatformContext, WarmWorker, WorkerError
from .schemas import (
    CHECK_LOGIN_INPUT,
    CHECK_LOGIN_OUTPUT,
    DOWNLOAD_FILE_INPUT,
    DOWNLOAD_FILE_OUTPUT,
    DOWNLOAD_SHARED_DOCUMENTS_INPUT,
    DOWNLOAD_SHARED_DOCUMENTS_OUTPUT,
    LIST_DOCUMENT_FILES_INPUT,
    LIST_DOCUMENT_FILES_OUTPUT,
    LIST_SHARED_DOCUMENTS_INPUT,
    LIST_SHARED_DOCUMENTS_OUTPUT,
)

LOGGER = logging.getLogger("cesi-worker")

DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024
DEFAULT_PUBLIC_URL_EXPIRES = 7 * 24 * 60 * 60
MAX_PUBLIC_URL_EXPIRES = 30 * 24 * 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _credentials(payload: dict[str, Any]) -> Credentials:
    username = str(payload.get("entUsername") or os.getenv("CESI_USERNAME") or "").strip()
    password = str(payload.get("entPassword") or os.getenv("CESI_PASSWORD") or "").strip()
    if not username or not password:
        raise WorkerError(
            "Identifiants CESI manquants : renseignez CESI_USERNAME / CESI_PASSWORD "
            "dans la configuration de l'instance, ou entUsername / entPassword dans l'entrée."
        )
    return Credentials(username=username, password=password)


async def _proxy(context: PlatformContext, payload: dict[str, Any]) -> ProxyInfo:
    if not bool(payload.get("useHostProxy", True)):
        return ProxyInfo()
    try:
        result = await context.proxy.get()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Proxy Chain indisponible, tentative en direct : %s", exc)
        return ProxyInfo()
    url = result.get("url") if isinstance(result, dict) else None
    token = result.get("token") if isinstance(result, dict) else None
    if not url:
        return ProxyInfo()
    LOGGER.info("Proxy Chain utilisée : %s", url)
    return ProxyInfo(url=str(url), token=str(token) if token else None)


async def _push_dataset(context: PlatformContext, dataset: str, item: dict[str, Any]) -> None:
    try:
        await context.dataset.push(dataset, item)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Dataset %s indisponible : %s", dataset, exc)


def _public_expires(payload: dict[str, Any]) -> int:
    value = int(payload.get("publicUrlExpiresIn") or 0)
    if value <= 0:
        return DEFAULT_PUBLIC_URL_EXPIRES
    return min(value, MAX_PUBLIC_URL_EXPIRES)


async def _prepare_kv(
    context: PlatformContext,
    payload: dict[str, Any],
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Retourne (store_in_kv, kv_store, erreurs)."""
    errors: list[dict[str, Any]] = []
    store_in_kv = bool(payload.get("storeInKv", True))
    kv_store = str(payload.get("kvStore") or "").strip()
    if not store_in_kv:
        return False, kv_store, errors
    if not context.kv.configured:
        LOGGER.warning(
            "KV non configuré (PLATFORM_KV_RUN_TOKEN absent) : storeInKv ignoré."
        )
        return False, kv_store, errors
    if kv_store:
        try:
            await context.kv.create_store(kv_store)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "documentUrl": None,
                    "filename": None,
                    "error": f"Named KV Store {kv_store!r} indisponible : {exc}",
                }
            )
            return False, kv_store, errors
    return True, kv_store, errors


async def _kv_metric(context: PlatformContext, key: str, value: Any, store: str = "") -> None:
    try:
        await context.kv.set_value(key, value, store=store)
        await context.kv.flush()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Métrique KV %s non enregistrée : %s", key, exc)


class CesiFunctions:
    def __init__(self, scraper: CesiScraper) -> None:
        self.scraper = scraper

    # ------------------------------------------------------------- list docs

    async def list_shared_documents(
        self,
        payload: dict[str, Any],
        context: PlatformContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        session = normalize_session(payload["session"])
        credentials = _credentials(payload)
        proxy = await _proxy(context, payload)

        cookies = await self.scraper.ensure_login(credentials, proxy)
        url, documents = await self.scraper.list_shared_documents(session, cookies, proxy)

        counts: dict[str, int] = {}
        for document in documents:
            counts[document.section] = counts.get(document.section, 0) + 1
        sections = [{"name": name, "count": count} for name, count in counts.items()]

        if bool(payload.get("storeInKv", True)):
            try:
                await context.kv.set_value(
                    f"documents:{session}:list",
                    {
                        "session": session,
                        "url": url,
                        "count": len(documents),
                        "sections": sections,
                        "documents": [document.__dict__ for document in documents],
                        "updatedAt": _now_iso(),
                    },
                )
                await context.kv.set_value(f"documents:{session}:count", len(documents))
                await context.kv.flush()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Index des documents non stocké en KV : %s", exc)

        await asyncio.gather(
            *(
                _push_dataset(
                    context,
                    "cesi-shared-documents",
                    {
                        "session": session,
                        "section": document.section,
                        "title": document.title,
                        "url": document.url,
                        "at": _now_iso(),
                    },
                )
                for document in documents
            )
        )

        return {
            "session": session,
            "url": url,
            "count": len(documents),
            "sections": sections,
            "documents": [
                {"section": document.section, "title": document.title, "url": document.url}
                for document in documents
            ],
            "proxyUsed": bool(proxy.url),
            "durationMs": _elapsed_ms(started),
        }

    # ------------------------------------------------------------ list files

    async def list_document_files(
        self,
        payload: dict[str, Any],
        context: PlatformContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        document_url = str(payload["documentUrl"]).strip()
        assert_cesi_url(document_url)
        credentials = _credentials(payload)
        proxy = await _proxy(context, payload)

        cookies = await self.scraper.ensure_login(credentials, proxy)
        files = await self.scraper.list_document_files(document_url, cookies, proxy)

        if bool(payload.get("storeInKv", True)):
            try:
                key = f"documents:files:{hashlib.sha256(document_url.encode('utf-8')).hexdigest()[:16]}"
                await context.kv.set_value(
                    key,
                    {
                        "documentUrl": document_url,
                        "count": len(files),
                        "files": [
                            {
                                "filename": file.filename,
                                "url": file.url,
                                "meta": file.meta,
                                "fileType": file.file_type,
                                "size": file.size,
                            }
                            for file in files
                        ],
                        "updatedAt": _now_iso(),
                    },
                )
                await context.kv.flush()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Liste des fichiers non stockée en KV : %s", exc)

        await asyncio.gather(
            *(
                _push_dataset(
                    context,
                    "cesi-document-files",
                    {
                        "documentUrl": document_url,
                        "filename": file.filename,
                        "fileUrl": file.url,
                        "fileType": file.file_type,
                        "size": file.size,
                        "at": _now_iso(),
                    },
                )
                for file in files
            )
        )

        return {
            "documentUrl": document_url,
            "count": len(files),
            "files": [
                {
                    "filename": file.filename,
                    "url": file.url,
                    "meta": file.meta,
                    "fileType": file.file_type,
                    "size": file.size,
                }
                for file in files
            ],
            "proxyUsed": bool(proxy.url),
            "durationMs": _elapsed_ms(started),
        }

    # ------------------------------------------------------- download many

    async def download_shared_documents(
        self,
        payload: dict[str, Any],
        context: PlatformContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        session = normalize_session(payload["session"])
        credentials = _credentials(payload)
        proxy = await _proxy(context, payload)

        only_sections = {
            str(value).strip().lower()
            for value in (payload.get("onlySections") or [])
            if str(value).strip()
        }
        title_contains = str(payload.get("titleContains") or "").strip().lower()
        include_types = {
            str(value).strip().upper()
            for value in (payload.get("includeFileTypes") or [])
            if str(value).strip()
        }
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        try:
            max_size = int(payload.get("maxFileSizeBytes") or DEFAULT_MAX_FILE_SIZE)
        except (TypeError, ValueError):
            max_size = DEFAULT_MAX_FILE_SIZE
        expires_in = _public_expires(payload)

        store_in_kv, kv_store, errors = await _prepare_kv(context, payload)

        cookies = await self.scraper.ensure_login(credentials, proxy)
        _, documents = await self.scraper.list_shared_documents(session, cookies, proxy)

        selected = [
            document
            for document in documents
            if (not only_sections or document.section.strip().lower() in only_sections)
            and (not title_contains or title_contains in document.title.lower())
        ]
        if limit > 0:
            selected = selected[:limit]

        files_found = 0
        downloaded_files: list[dict[str, Any]] = []

        for document in selected:
            try:
                document_files = await self.scraper.list_document_files(document.url, cookies, proxy)
            except Exception as exc:  # noqa: BLE001
                errors.append({"documentUrl": document.url, "filename": None, "error": str(exc)})
                LOGGER.warning("Fiche %s illisible : %s", document.url, exc)
                continue

            files_found += len(document_files)
            for document_file in document_files:
                if include_types and document_file.file_type.strip().upper() not in include_types:
                    continue
                try:
                    downloaded = await self.scraper.download_file(
                        document_file.url,
                        cookies,
                        proxy,
                        referer=document.url,
                        max_size=max_size,
                        filename=document_file.filename,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "documentUrl": document.url,
                            "filename": document_file.filename,
                            "error": str(exc),
                        }
                    )
                    LOGGER.warning("Téléchargement %s échoué : %s", document_file.url, exc)
                    continue

                stored_in_kv = False
                kv_key: str | None = None
                public_url: dict[str, str] | None = None
                if store_in_kv:
                    kv_key = build_kv_key(
                        session,
                        document.title,
                        downloaded.filename,
                        downloaded.sha256,
                    )
                    try:
                        await context.kv.set_bytes(
                            kv_key,
                            downloaded.data,
                            store=kv_store,
                            content_type=downloaded.mime_type,
                        )
                        stored_in_kv = True
                        try:
                            public_url = await context.kv.get_public_url(
                                kv_key,
                                store=kv_store,
                                expires_in=expires_in,
                            )
                        except Exception as exc:  # noqa: BLE001
                            errors.append(
                                {
                                    "documentUrl": document.url,
                                    "filename": downloaded.filename,
                                    "error": f"URL publique indisponible : {exc}",
                                }
                            )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            {
                                "documentUrl": document.url,
                                "filename": downloaded.filename,
                                "error": f"Stockage KV échoué : {exc}",
                            }
                        )
                        LOGGER.warning("Stockage KV de %s échoué : %s", downloaded.filename, exc)

                entry = {
                    "section": document.section,
                    "title": document.title,
                    "documentUrl": document.url,
                    "filename": downloaded.filename,
                    "fileUrl": downloaded.url,
                    "sizeBytes": len(downloaded.data),
                    "sha256": downloaded.sha256,
                    "mimeType": downloaded.mime_type,
                    "storedInKv": stored_in_kv,
                    "kvStore": kv_store if stored_in_kv else "",
                    "kvKey": kv_key if stored_in_kv else None,
                    "publicUrl": public_url,
                }
                downloaded_files.append(entry)
                await _push_dataset(
                    context,
                    "cesi-downloads",
                    {**entry, "session": session, "at": _now_iso()},
                )

        if store_in_kv:
            await _kv_metric(
                context,
                f"documents:{session}:lastRun",
                {
                    "at": _now_iso(),
                    "documentsScanned": len(selected),
                    "filesDownloaded": len(downloaded_files),
                },
                store=kv_store,
            )
            await _kv_metric(context, f"documents:{session}:lastCount", len(downloaded_files), store=kv_store)

        return {
            "session": session,
            "documentsScanned": len(selected),
            "filesFound": files_found,
            "filesDownloaded": len(downloaded_files),
            "files": downloaded_files,
            "errors": errors,
            "proxyUsed": bool(proxy.url),
            "durationMs": _elapsed_ms(started),
        }

    # -------------------------------------------------------- download one

    async def download_file(
        self,
        payload: dict[str, Any],
        context: PlatformContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        file_url = str(payload["fileUrl"]).strip()
        assert_cesi_url(file_url)
        credentials = _credentials(payload)
        proxy = await _proxy(context, payload)

        try:
            max_size = int(payload.get("maxFileSizeBytes") or DEFAULT_MAX_FILE_SIZE)
        except (TypeError, ValueError):
            max_size = DEFAULT_MAX_FILE_SIZE
        expires_in = _public_expires(payload)
        store_in_kv, kv_store, errors = await _prepare_kv(context, payload)

        cookies = await self.scraper.ensure_login(credentials, proxy)
        downloaded = await self.scraper.download_file(
            file_url,
            cookies,
            proxy,
            referer=str(payload.get("referer") or "") or None,
            max_size=max_size,
            filename=str(payload.get("filename") or "") or None,
        )

        stored_in_kv = False
        kv_key: str | None = None
        public_url: dict[str, str] | None = None
        if store_in_kv:
            kv_key = build_kv_key("fichiers", "fichier", downloaded.filename, downloaded.sha256)
            try:
                await context.kv.set_bytes(
                    kv_key,
                    downloaded.data,
                    store=kv_store,
                    content_type=downloaded.mime_type,
                )
                stored_in_kv = True
                public_url = await context.kv.get_public_url(
                    kv_key,
                    store=kv_store,
                    expires_in=expires_in,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "documentUrl": str(payload.get("referer") or "") or None,
                        "filename": downloaded.filename,
                        "error": str(exc),
                    }
                )
                LOGGER.warning("Stockage KV de %s échoué : %s", downloaded.filename, exc)

        if errors:
            LOGGER.warning("Téléchargement %s : %d erreur(s) non bloquante(s)", downloaded.filename, len(errors))

        return {
            "filename": downloaded.filename,
            "fileUrl": downloaded.url,
            "sizeBytes": len(downloaded.data),
            "sha256": downloaded.sha256,
            "mimeType": downloaded.mime_type,
            "storedInKv": stored_in_kv,
            "kvStore": kv_store if stored_in_kv else "",
            "kvKey": kv_key if stored_in_kv else None,
            "publicUrl": public_url,
            "durationMs": _elapsed_ms(started),
        }

    # ---------------------------------------------------------- check login

    async def check_login(
        self,
        payload: dict[str, Any],
        context: PlatformContext,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        credentials = _credentials(payload)
        proxy = await _proxy(context, payload)
        await self.scraper.ensure_login(credentials, proxy)
        return {
            "ok": True,
            "username": credentials.username,
            "proxyUsed": bool(proxy.url),
            "durationMs": _elapsed_ms(started),
        }


def build_worker() -> WarmWorker:
    worker = WarmWorker(
        worker_id=os.getenv("WORKER_ID", "cesi-worker"),
        grpc_address=os.getenv("PLATFORM_GRPC_ADDRESS", "127.0.0.1:50051"),
        worker_key=os.getenv("PLATFORM_WORKER_KEY", "demo-worker-key"),
    )
    functions = CesiFunctions(CesiScraper())

    worker.expose(
        name="list_shared_documents",
        description=(
            "Liste les documents partagés d'une session CESI "
            "(https://ent.cesi.fr/<session>/documents-partages) : sections, titres et URLs "
            "des fiches. Authentification via CESI_USERNAME/CESI_PASSWORD ou paramètres d'entrée."
        ),
        input_schema=LIST_SHARED_DOCUMENTS_INPUT,
        output_schema=LIST_SHARED_DOCUMENTS_OUTPUT,
        handler=functions.list_shared_documents,
    )
    worker.expose(
        name="list_document_files",
        description=(
            "Liste les fichiers joints (PDF, etc.) d'une fiche document CESI à partir de son "
            "URL absolue. Retourne le nom, l'URL, le type et la taille de chaque fichier."
        ),
        input_schema=LIST_DOCUMENT_FILES_INPUT,
        output_schema=LIST_DOCUMENT_FILES_OUTPUT,
        handler=functions.list_document_files,
    )
    worker.expose(
        name="download_shared_documents",
        description=(
            "Parcourt les documents partagés d'une session CESI, télécharge leurs fichiers "
            "joints et les stocke optionnellement dans le KV de la plateforme avec URL publique "
            "signée. Retourne les empreintes SHA-256 et les erreurs non bloquantes."
        ),
        input_schema=DOWNLOAD_SHARED_DOCUMENTS_INPUT,
        output_schema=DOWNLOAD_SHARED_DOCUMENTS_OUTPUT,
        handler=functions.download_shared_documents,
    )
    worker.expose(
        name="download_file",
        description=(
            "Télécharge un fichier CESI (URL /medias/fichier/...) avec la session authentifiée "
            "et le stocke optionnellement dans le KV de la plateforme avec URL publique signée."
        ),
        input_schema=DOWNLOAD_FILE_INPUT,
        output_schema=DOWNLOAD_FILE_OUTPUT,
        handler=functions.download_file,
    )
    worker.expose(
        name="check_login",
        description=(
            "Vérifie que les identifiants CESI permettent d'ouvrir l'accueil apprenant. "
            "Utile pour tester la configuration de l'instance avant un scraping."
        ),
        input_schema=CHECK_LOGIN_INPUT,
        output_schema=CHECK_LOGIN_OUTPUT,
        handler=functions.check_login,
    )
    return worker
