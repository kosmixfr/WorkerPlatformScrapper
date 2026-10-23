"""Contrats JSON Schema (draft-07) des fonctions exposées par le worker CESI.

L'hôte valide les entrées/sorties avec Ajv (`useDefaults: true`), puis convertit
ces schémas en documentation MCP. Ils sont volontairement explicites et
`additionalProperties: false`, comme les schémas Zod `.strict()` du SDK.
"""

from __future__ import annotations

from typing import Any

JSONSchema = dict[str, Any]


def _obj(
    properties: dict[str, JSONSchema],
    required: list[str] | None = None,
    description: str | None = None,
) -> JSONSchema:
    schema: JSONSchema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    if description:
        schema["description"] = description
    return schema


def _nullable(schema: JSONSchema) -> JSONSchema:
    current = schema.get("type")
    if isinstance(current, str):
        return {**schema, "type": [current, "null"]}
    return schema


_USERNAME: JSONSchema = {
    "type": "string",
    "minLength": 1,
    "description": "Identifiant ENT CESI (ex. prenom.nom@viacesi.fr). Par défaut CESI_USERNAME (env du worker). [secret]",
    "writeOnly": True,
    "secret": True,
}

_PASSWORD: JSONSchema = {
    "type": "string",
    "minLength": 1,
    "description": "Mot de passe ENT CESI. Par défaut CESI_PASSWORD (env du worker). [secret]",
    "writeOnly": True,
    "secret": True,
}

_USE_PROXY: JSONSchema = {
    "type": "boolean",
    "default": True,
    "description": "Route les requêtes via la Proxy Chain de la plateforme si un proxy est assigné.",
}

_STORE_IN_KV: JSONSchema = {
    "type": "boolean",
    "default": True,
    "description": "Stocke le résultat dans le Default KV de la Run (persistant, purgé selon KV_RETENTION_DAYS).",
}

_SESSION: JSONSchema = {
    "type": "string",
    "minLength": 1,
    "description": "Slug de session CESI, ex. cpi-a2-info-26-27-nanterre-nr6dp201 (premier segment de https://ent.cesi.fr/<session>/documents-partages). Une URL complète est aussi acceptée.",
}

_DOCUMENT_URL: JSONSchema = {
    "type": "string",
    "minLength": 8,
    "description": "URL absolue d'une fiche document CESI (ex. https://ent.cesi.fr/<session>/documents-partages/planning-s43).",
}

_FILE_URL: JSONSchema = {
    "type": "string",
    "minLength": 8,
    "description": "URL absolue d'un fichier CESI (ex. https://ent.cesi.fr/medias/fichier/....-pdf).",
}

_PUBLIC_URL_EXPIRES: JSONSchema = {
    "type": "integer",
    "minimum": 0,
    "maximum": 2_592_000,
    "default": 0,
    "description": "Durée de validité de l'URL publique signée en secondes (0 = 7 jours, max 30 jours).",
}

_KV_STORE: JSONSchema = {
    "type": "string",
    "default": "",
    "pattern": "^$|^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
    "description": "Nom d'un Named KV Store (vide = Default KV de la Run). Doit commencer par une lettre ou un chiffre.",
}

_MAX_FILE_SIZE: JSONSchema = {
    "type": "integer",
    "minimum": 1,
    "maximum": 2_147_483_648,
    "default": 104_857_600,
    "description": "Taille maximale d'un fichier téléchargé en octets (défaut 100 Mo).",
}

# --------------------------------------------------------------------- list


LIST_SHARED_DOCUMENTS_INPUT = _obj(
    {
        "session": _SESSION,
        "entUsername": _USERNAME,
        "entPassword": _PASSWORD,
        "useHostProxy": _USE_PROXY,
        "storeInKv": _STORE_IN_KV,
    },
    required=["session"],
    description="Critères de récupération de l'index des documents partagés d'une session CESI.",
)

SHARED_DOCUMENT = _obj(
    {
        "section": {"type": "string", "description": "Nom de la section (ex. « Documents importants à lire absolument », « plannings »)."},
        "title": {"type": "string", "description": "Titre de la fiche document."},
        "url": {"type": "string", "description": "URL absolue de la fiche document."},
    },
    required=["section", "title", "url"],
)

SHARED_SECTION = _obj(
    {
        "name": {"type": "string", "description": "Nom de la section."},
        "count": {"type": "integer", "minimum": 0, "description": "Nombre de documents dans la section."},
    },
    required=["name", "count"],
)

LIST_SHARED_DOCUMENTS_OUTPUT = _obj(
    {
        "session": {"type": "string", "description": "Slug de session interrogé."},
        "url": {"type": "string", "description": "URL de la page documents-partages."},
        "count": {"type": "integer", "minimum": 0, "description": "Nombre total de documents."},
        "sections": {"type": "array", "items": SHARED_SECTION, "description": "Sections de la page, avec leur nombre de documents."},
        "documents": {"type": "array", "items": SHARED_DOCUMENT, "description": "Documents partagés, dans l'ordre de la page."},
        "proxyUsed": {"type": "boolean", "description": "Indique si la Proxy Chain a été utilisée."},
        "durationMs": {"type": "integer", "minimum": 0, "description": "Durée de l'invocation en millisecondes."},
    },
    required=["session", "url", "count", "sections", "documents", "proxyUsed", "durationMs"],
)

# -------------------------------------------------------------------- files


LIST_DOCUMENT_FILES_INPUT = _obj(
    {
        "documentUrl": _DOCUMENT_URL,
        "entUsername": _USERNAME,
        "entPassword": _PASSWORD,
        "useHostProxy": _USE_PROXY,
        "storeInKv": _STORE_IN_KV,
    },
    required=["documentUrl"],
    description="Critères de récupération des fichiers joints d'une fiche document CESI.",
)

DOCUMENT_FILE = _obj(
    {
        "filename": {"type": "string", "description": "Nom du fichier joint."},
        "url": {"type": "string", "description": "URL absolue du fichier."},
        "meta": {"type": "string", "description": "Métadonnée brute affichée par l'ENT (ex. « (PDF, 179 Ko) »)."},
        "fileType": {"type": "string", "description": "Type de fichier déduit (ex. PDF)."},
        "size": {"type": "string", "description": "Taille lisible affichée par l'ENT (ex. « 179 Ko »)."},
    },
    required=["filename", "url", "meta", "fileType", "size"],
)

LIST_DOCUMENT_FILES_OUTPUT = _obj(
    {
        "documentUrl": {"type": "string", "description": "Fiche document analysée."},
        "count": {"type": "integer", "minimum": 0, "description": "Nombre de fichiers joints."},
        "files": {"type": "array", "items": DOCUMENT_FILE, "description": "Fichiers joints de la fiche."},
        "proxyUsed": {"type": "boolean", "description": "Indique si la Proxy Chain a été utilisée."},
        "durationMs": {"type": "integer", "minimum": 0, "description": "Durée de l'invocation en millisecondes."},
    },
    required=["documentUrl", "count", "files", "proxyUsed", "durationMs"],
)

# ----------------------------------------------------------------- download

DOWNLOAD_SHARED_DOCUMENTS_INPUT = _obj(
    {
        "session": _SESSION,
        "onlySections": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Ne traite que ces sections (comparaison insensible à la casse, nom exact). Vide = toutes.",
        },
        "titleContains": {
            "type": "string",
            "default": "",
            "description": "Ne traite que les documents dont le titre contient cette chaîne (insensible à la casse). Vide = tous.",
        },
        "includeFileTypes": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Extensions/types de fichiers à télécharger (ex. [\"PDF\"]). Vide = tous les fichiers.",
        },
        "limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 500,
            "default": 0,
            "description": "Nombre maximum de fiches documents à parcourir (0 = toutes).",
        },
        "maxFileSizeBytes": _MAX_FILE_SIZE,
        "entUsername": _USERNAME,
        "entPassword": _PASSWORD,
        "useHostProxy": _USE_PROXY,
        "storeInKv": _STORE_IN_KV,
        "kvStore": _KV_STORE,
        "publicUrlExpiresIn": _PUBLIC_URL_EXPIRES,
    },
    required=["session"],
    description="Parcourt les documents partagés d'une session CESI, télécharge leurs fichiers et les stocke dans le KV.",
)

PUBLIC_URL = _obj(
    {
        "url": {"type": "string", "description": "URL publique signée de téléchargement."},
        "expiresAt": {"type": "string", "description": "Date d'expiration ISO 8601."},
    },
    required=["url", "expiresAt"],
)

DOWNLOADED_FILE = _obj(
    {
        "section": {"type": "string", "description": "Section du document source."},
        "title": {"type": "string", "description": "Titre du document source."},
        "documentUrl": {"type": "string", "description": "URL de la fiche document source."},
        "filename": {"type": "string", "description": "Nom du fichier téléchargé."},
        "fileUrl": {"type": "string", "description": "URL d'origine du fichier."},
        "sizeBytes": {"type": "integer", "minimum": 0, "description": "Taille réelle du fichier en octets."},
        "sha256": {"type": "string", "description": "Empreinte SHA-256 du fichier."},
        "mimeType": {"type": "string", "description": "Type MIME renvoyé par l'ENT."},
        "storedInKv": {"type": "boolean", "description": "Indique si le fichier a été stocké dans le KV."},
        "kvStore": {"type": "string", "description": "Nom du KV Store (vide = Default KV)."},
        "kvKey": _nullable({"type": "string", "description": "Clé KV du fichier."}),
        "publicUrl": _nullable({**PUBLIC_URL, "description": "URL publique signée, ou null."}),
    },
    required=[
        "section",
        "title",
        "documentUrl",
        "filename",
        "fileUrl",
        "sizeBytes",
        "sha256",
        "mimeType",
        "storedInKv",
        "kvStore",
        "kvKey",
        "publicUrl",
    ],
)

DOWNLOAD_ERROR = _obj(
    {
        "documentUrl": _nullable({"type": "string", "description": "Fiche concernée."}),
        "filename": _nullable({"type": "string", "description": "Fichier concerné."}),
        "error": {"type": "string", "description": "Message d'erreur."},
    },
    required=["documentUrl", "filename", "error"],
)

DOWNLOAD_SHARED_DOCUMENTS_OUTPUT = _obj(
    {
        "session": {"type": "string", "description": "Slug de session traité."},
        "documentsScanned": {"type": "integer", "minimum": 0, "description": "Nombre de fiches documents parcourues."},
        "filesFound": {"type": "integer", "minimum": 0, "description": "Nombre de fichiers joints trouvés."},
        "filesDownloaded": {"type": "integer", "minimum": 0, "description": "Nombre de fichiers téléchargés avec succès."},
        "files": {"type": "array", "items": DOWNLOADED_FILE, "description": "Fichiers téléchargés."},
        "errors": {"type": "array", "items": DOWNLOAD_ERROR, "description": "Erreurs non bloquantes rencontrées."},
        "proxyUsed": {"type": "boolean", "description": "Indique si la Proxy Chain a été utilisée."},
        "durationMs": {"type": "integer", "minimum": 0, "description": "Durée de l'invocation en millisecondes."},
    },
    required=[
        "session",
        "documentsScanned",
        "filesFound",
        "filesDownloaded",
        "files",
        "errors",
        "proxyUsed",
        "durationMs",
    ],
)

DOWNLOAD_FILE_INPUT = _obj(
    {
        "fileUrl": _FILE_URL,
        "filename": {
            "type": "string",
            "default": "",
            "description": "Nom de fichier à utiliser (vide = déduit de l'URL / Content-Disposition).",
        },
        "referer": {
            "type": "string",
            "default": "",
            "description": "URL de la fiche document à envoyer comme Referer (optionnel).",
        },
        "maxFileSizeBytes": _MAX_FILE_SIZE,
        "entUsername": _USERNAME,
        "entPassword": _PASSWORD,
        "useHostProxy": _USE_PROXY,
        "storeInKv": _STORE_IN_KV,
        "kvStore": _KV_STORE,
        "publicUrlExpiresIn": _PUBLIC_URL_EXPIRES,
    },
    required=["fileUrl"],
    description="Télécharge un fichier CESI authentifié et le stocke optionnellement dans le KV.",
)

DOWNLOAD_FILE_OUTPUT = _obj(
    {
        "filename": {"type": "string", "description": "Nom du fichier téléchargé."},
        "fileUrl": {"type": "string", "description": "URL d'origine du fichier."},
        "sizeBytes": {"type": "integer", "minimum": 0, "description": "Taille réelle du fichier en octets."},
        "sha256": {"type": "string", "description": "Empreinte SHA-256 du fichier."},
        "mimeType": {"type": "string", "description": "Type MIME renvoyé par l'ENT."},
        "storedInKv": {"type": "boolean", "description": "Indique si le fichier a été stocké dans le KV."},
        "kvStore": {"type": "string", "description": "Nom du KV Store (vide = Default KV)."},
        "kvKey": _nullable({"type": "string", "description": "Clé KV du fichier."}),
        "publicUrl": _nullable({**PUBLIC_URL, "description": "URL publique signée, ou null."}),
        "durationMs": {"type": "integer", "minimum": 0, "description": "Durée de l'invocation en millisecondes."},
    },
    required=[
        "filename",
        "fileUrl",
        "sizeBytes",
        "sha256",
        "mimeType",
        "storedInKv",
        "kvStore",
        "kvKey",
        "publicUrl",
        "durationMs",
    ],
)

# --------------------------------------------------------------- check login

CHECK_LOGIN_INPUT = _obj(
    {
        "entUsername": _USERNAME,
        "entPassword": _PASSWORD,
        "useHostProxy": _USE_PROXY,
    },
    description="Vérifie les identifiants CESI en ouvrant l'accueil apprenant.",
)

CHECK_LOGIN_OUTPUT = _obj(
    {
        "ok": {"type": "boolean", "description": "True si la session authentifiée est valide."},
        "username": {"type": "string", "description": "Identifiant testé."},
        "proxyUsed": {"type": "boolean", "description": "Indique si la Proxy Chain a été utilisée."},
        "durationMs": {"type": "integer", "minimum": 0, "description": "Durée de l'invocation en millisecondes."},
    },
    required=["ok", "username", "proxyUsed", "durationMs"],
)
