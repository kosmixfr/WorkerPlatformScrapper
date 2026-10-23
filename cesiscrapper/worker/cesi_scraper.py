"""Scraper CESI basé sur scrapling/patchright.

Reprend la logique validée de `main.py` (sélecteurs CSS des documents partagés,
login WAYF + ADFS) en l'adaptant à un worker chaud :

* login navigateur uniquement quand les cookies ne sont plus valides ;
* cookies persistés par identifiant dans `CESI_COOKIES_FILE` ;
* requêtes HTML/fichiers via `AsyncFetcher` avec les cookies de session ;
* support de la Proxy Chain de la plateforme (proxy + jeton).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

from patchright.async_api import Page
from scrapling.fetchers import AsyncFetcher, AsyncStealthySession
from scrapling.parser import Adaptor

LOGGER = logging.getLogger("cesi-worker")

ENT_HOME = "https://ent.cesi.fr/"
ENT_WAYF = "https://ent.cesi.fr/identification/wayf"
ENT_ACCUEIL = "https://ent.cesi.fr/accueil-apprenant"

ALLOWED_SUFFIXES = (".cesi.fr", ".viacesi.fr")


class CesiError(RuntimeError):
    """Erreur de scraping / d'authentification CESI."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class ProxyInfo:
    url: str | None = None
    token: str | None = None


@dataclass
class SharedDocument:
    """Un document listé sur la page 'documents-partages'."""

    section: str
    title: str
    url: str


@dataclass
class DocumentFile:
    """Un vrai fichier joint d'une fiche document."""

    filename: str
    url: str
    meta: str = ""
    file_type: str = ""
    size: str = ""


@dataclass
class DownloadedFile:
    filename: str
    url: str
    data: bytes
    mime_type: str
    sha256: str


# --------------------------------------------------------------------- utils


def normalize_session(value: str) -> str:
    """Accepte un slug ou une URL et retourne le slug de session."""
    raw = (value or "").strip()
    if not raw:
        raise CesiError("Le paramètre `session` est vide.")
    if raw.startswith(("http://", "https://")):
        parts = [part for part in urlparse(raw).path.split("/") if part]
        if not parts:
            raise CesiError(f"URL de session invalide : {value}")
        raw = parts[0]
    session = raw.strip("/").split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
        raise CesiError(f"Slug de session invalide : {session!r}")
    return session


def documents_url(session: str) -> str:
    return f"https://ent.cesi.fr/{quote(session, safe='')}/documents-partages"


def assert_cesi_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CesiError(f"URL non HTTP(S) refusée : {url}")
    host = (parsed.hostname or "").lower()
    if not (host == "cesi.fr" or host.endswith(ALLOWED_SUFFIXES)):
        raise CesiError(f"Hôte non autorisé (attendu : *.cesi.fr) : {host or url}")


def sanitize_kv_part(value: str, max_length: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._-")
    return (safe or "document")[:max_length]


def build_kv_key(session: str, title: str, filename: str, sha256: str) -> str:
    """Clé KV ASCII stable et lisible (le daemon valide les clés ASCII)."""
    return "/".join(
        [
            "documents",
            sanitize_kv_part(session),
            f"{sha256[:16]}-{sanitize_kv_part(title, 60)}-{sanitize_kv_part(filename, 80)}",
        ]
    )


def _filename_from_headers(headers: Any, url: str) -> str:
    content_disposition = ""
    try:
        content_disposition = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    except Exception:
        pass
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip().strip('"')
    match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip()
    name = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    return name or "fichier"


def _content_type(headers: Any) -> str:
    try:
        return str(headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    except Exception:
        return "application/octet-stream"


def _ensure_extension(filename: str, mime_type: str, url: str) -> str:
    """Ajoute une extension quand l'ENT fournit un nom sans suffixe."""
    if re.search(r"\.[A-Za-z0-9]{1,10}$", filename):
        return filename
    url_suffix = Path(urlparse(url).path).suffix
    if url_suffix:
        return filename + url_suffix
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    return filename + guessed


# ------------------------------------------------------------------- parsing


def extract_documents(html_content: str | bytes) -> list[SharedDocument]:
    """Extrait les documents depuis la page 'documents-partages'.

    Structure observée :
      ul.liste__document > li.liste__document--element
        > .liste__document--titre  (section)
        > li.liste__ged-item > a   (document)
    """
    if isinstance(html_content, (bytes, bytearray)):
        html_content = bytes(html_content).decode("utf-8", errors="replace")

    page = Adaptor(html_content)
    documents: list[SharedDocument] = []
    for section_el in page.css("ul.liste__document > li.liste__document--element"):
        titles = section_el.css(".liste__document--titre")
        section_name = titles[0].text.strip() if titles else ""
        for anchor in section_el.css("li.liste__ged-item a"):
            title = (anchor.text or "").strip()
            href = (anchor.attrib.get("href") or "").strip()
            if not title or not href:
                continue
            documents.append(
                SharedDocument(
                    section=section_name,
                    title=title,
                    url=href if href.startswith("http") else urljoin("https://ent.cesi.fr", href),
                )
            )
    return documents


def extract_document_files(html_content: str | bytes) -> list[DocumentFile]:
    """Extrait les vrais fichiers joints d'une fiche document."""
    if isinstance(html_content, (bytes, bytearray)):
        html_content = bytes(html_content).decode("utf-8", errors="replace")

    page = Adaptor(html_content)
    files: list[DocumentFile] = []
    for anchor in page.css("ul.fichiers-joints li a"):
        href = (anchor.attrib.get("href") or "").strip()
        if not href:
            continue
        absolute = href if href.startswith("http") else urljoin("https://ent.cesi.fr", href)

        spans = anchor.css(".fichiers-joints__type-poids")
        meta = spans[0].text.strip() if spans else ""
        full_text = (anchor.text or "").strip()
        filename = full_text.replace(meta, "").strip() if meta and meta in full_text else full_text
        if not filename:
            filename = unquote(urlparse(absolute).path.rstrip("/").split("/")[-1])

        file_type, size = "", ""
        match = re.match(r"\(\s*([^,]+)\s*,\s*(.+?)\s*\)", meta)
        if match:
            file_type, size = match.group(1).strip(), match.group(2).strip()
        else:
            classes = (anchor.attrib.get("class") or "").split()
            if classes:
                file_type = classes[0].upper()

        files.append(DocumentFile(filename=filename, url=absolute, meta=meta, file_type=file_type, size=size))
    return files


# -------------------------------------------------------------------- scraper


class CesiScraper:
    def __init__(self) -> None:
        default_cookie = Path(os.getenv("CESI_WORK_DIR", "/data")) / "cesi-cookies.json"
        self._cookie_store_path = Path(os.getenv("CESI_COOKIES_FILE", str(default_cookie)))
        self._headless = os.getenv("CESI_HEADLESS", "true").strip().lower() != "false"
        self._lock = asyncio.Lock()
        self._cookies: dict[str, dict[str, str]] = self._load_cookie_store()
        self._login_count = 0

    # ------------------------------------------------------------- cookies

    def _load_cookie_store(self) -> dict[str, dict[str, str]]:
        try:
            if not self._cookie_store_path.exists():
                return {}
            data = json.loads(self._cookie_store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    str(key): {str(name): str(value) for name, value in cookies.items()}
                    for key, cookies in data.items()
                    if isinstance(cookies, dict)
                }
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("Cookies CESI illisibles (%s) : %s", self._cookie_store_path, exc)
        return {}

    def _save_cookie_store(self) -> None:
        try:
            self._cookie_store_path.parent.mkdir(parents=True, exist_ok=True)
            self._cookie_store_path.write_text(
                json.dumps(self._cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            LOGGER.warning("Impossible de sauvegarder les cookies CESI : %s", exc)

    @staticmethod
    def _cookie_key(credentials: Credentials) -> str:
        return hashlib.sha256(credentials.username.strip().lower().encode("utf-8")).hexdigest()[:16]

    # -------------------------------------------------------------- login

    async def ensure_login(self, credentials: Credentials, proxy: ProxyInfo) -> dict[str, str]:
        """Retourne des cookies valides, en réutilisant le cache si possible."""
        async with self._lock:
            cache_key = self._cookie_key(credentials)
            cached = self._cookies.get(cache_key)
            if cached and await self._test_login(cached, proxy):
                return cached

            LOGGER.info("Cookies CESI absents/expirés : login navigateur pour %s", credentials.username)
            cookies = await self._browser_login(credentials, proxy)
            self._cookies[cache_key] = cookies
            self._save_cookie_store()
            self._login_count += 1
            LOGGER.info("Login CESI réussi (%d cookies)", len(cookies))
            return cookies

    async def _test_login(self, cookies: dict[str, str], proxy: ProxyInfo) -> bool:
        try:
            response = await AsyncFetcher.get(
                ENT_ACCUEIL,
                cookies=cookies,
                timeout=30,
                **self._proxy_kwargs(proxy),
            )
            return int(response.status) == 200 and "accueil-apprenant" in str(response.url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Test de session CESI échoué : %s", exc)
            return False

    async def _browser_login(self, credentials: Credentials, proxy: ProxyInfo) -> dict[str, str]:
        proxy_config: dict[str, str] | None = None
        if proxy.url:
            proxy_config = {"server": proxy.url}
            if proxy.token:
                proxy_config["username"] = proxy.token
                proxy_config["password"] = ""

        session = AsyncStealthySession(
            headless=self._headless,
            timeout=45_000,
            proxy=proxy_config,
            google_search=False,
            solve_cloudflare=False,
            # Docker : /dev/shm est limité à 64 Mo par défaut et le sandbox
            # Chromium peut être bloqué par seccomp selon l'hôte.
            extra_flags=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        await session.start()
        try:
            await session.fetch(ENT_WAYF, page_action=lambda page: self._login_action(page, credentials))
            raw_cookies = await session.context.cookies()
        finally:
            try:
                await session.close()
            except Exception:
                pass

        cookies = {
            str(cookie["name"]): str(cookie["value"])
            for cookie in raw_cookies
            if cookie.get("name") and cookie.get("value") is not None
        }
        if not cookies:
            raise CesiError("Login CESI : aucun cookie de session obtenu.")
        if not await self._test_login(cookies, proxy):
            raise CesiError(
                "Login CESI effectué mais la session reste invalide "
                "(identifiants incorrects, MFA ou page de login modifiée ?)."
            )
        return cookies

    async def _login_action(self, page: Page, credentials: Credentials) -> None:
        page.set_default_timeout(15_000)
        await self._dismiss_cookies(page)
        try:
            await page.wait_for_selector("#login, #passwordInput, #userNameInput", timeout=8_000)
        except Exception:
            LOGGER.warning("Aucun champ de login CESI détecté sur %s", page.url)

        # --- WAYF : identifiant ---
        if not self._on_sts(page):
            login_input = page.locator("#login")
            if await login_input.count() > 0:
                for attempt in range(2):
                    await self._fill_verified(login_input, credentials.username)
                    button = page.locator("button.submit")
                    if await button.count() == 0:
                        button = page.locator('button[type="submit"], input[type="submit"]').first
                    try:
                        await button.click(timeout=5_000)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_url(
                            lambda url: "sts." in str(url)
                            or ("ent.cesi.fr" in str(url) and "/identification/wayf" not in str(url)),
                            timeout=10_000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_load_state("domcontentloaded")
                    if not await self._still_on_wayf(page):
                        break
                    LOGGER.warning("WAYF toujours affiché après soumission (tentative %d)", attempt + 1)
                    await page.wait_for_timeout(600)

        # --- ADFS : mot de passe ---
        if self._on_sts(page):
            await self._adfs_login(page, credentials)

        if self._on_sts(page):
            raise CesiError(f"Échec de l'authentification ADFS : toujours sur {page.url}")

        if "ent.cesi.fr" not in page.url:
            await page.goto(ENT_HOME, wait_until="domcontentloaded", timeout=20_000)
        await self._dismiss_cookies(page)
        await page.goto(ENT_ACCUEIL, wait_until="domcontentloaded", timeout=20_000)

    async def _adfs_login(self, page: Page, credentials: Credentials) -> None:
        await page.wait_for_selector("#userNameInput, #passwordInput", timeout=8_000)
        user_input = page.locator("#userNameInput")
        if await user_input.count() > 0:
            await self._fill_verified(user_input, credentials.username)

        password_input = page.locator("#passwordInput")
        if await password_input.count() == 0:
            password_input = page.locator('input[type="password"]').first
        if await password_input.count() > 0:
            await self._fill_verified(password_input, credentials.password)

        submit = page.locator("#submitButton")
        if await submit.count() > 0:
            try:
                await submit.click(timeout=5_000)
            except Exception:
                pass
        else:
            fallback = page.locator('span.submit, button[type="submit"], input[type="submit"]').first
            try:
                await fallback.click(timeout=5_000)
            except Exception:
                pass

        try:
            await page.wait_for_url(lambda url: "ent.cesi.fr" in str(url), timeout=10_000)
        except Exception:
            pass
        await page.wait_for_load_state("domcontentloaded")

        if self._on_sts(page):
            LOGGER.warning("ADFS toujours affiché après le submit, tentative JS")
            try:
                await page.evaluate(
                    """() => {
                        const win = window;
                        if (win.Login && typeof win.Login.submitLoginRequest === "function") {
                            return win.Login.submitLoginRequest();
                        }
                        const form = document.getElementById('loginForm');
                        if (form) { form.submit(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                pass
            try:
                await page.wait_for_url(lambda url: "ent.cesi.fr" in str(url), timeout=8_000)
            except Exception:
                pass

    @staticmethod
    def _on_sts(page: Page) -> bool:
        return "sts." in str(page.url)

    @staticmethod
    async def _still_on_wayf(page: Page) -> bool:
        if "/identification/wayf" not in str(page.url):
            return False
        return await page.locator("#login").count() > 0

    async def _fill_verified(self, locator: Any, value: str) -> bool:
        page = locator.page
        for _ in range(3):
            try:
                await locator.click(timeout=1_500)
            except Exception:
                pass
            try:
                await locator.fill(value, timeout=5_000)
            except Exception:
                try:
                    await locator.evaluate(
                        """(el, v) => {
                            el.focus();
                            el.value = v;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }""",
                        value,
                    )
                except Exception:
                    pass
            await page.wait_for_timeout(150)
            try:
                current = (await locator.input_value(timeout=1_000)).strip()
                if current == value.strip():
                    return True
            except Exception:
                pass
        LOGGER.warning("Impossible de confirmer la saisie d'un champ CESI")
        return False

    @staticmethod
    async def _dismiss_cookies(page: Page) -> None:
        selectors = (
            'button:has-text("Tout accepter")',
            'button:has-text("Accepter")',
            ".orejime-Button--save",
            "#tarteaucitronPersonalize2",
            ".didomi-continue-without-agreeing",
        )
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    await locator.click(timeout=800)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------- requêtes

    @staticmethod
    def _proxy_kwargs(proxy: ProxyInfo) -> dict[str, Any]:
        if not proxy.url:
            return {}
        kwargs: dict[str, Any] = {"proxy": proxy.url}
        if proxy.token:
            kwargs["proxy_auth"] = (proxy.token, "")
        return kwargs

    async def _fetch_bytes(
        self,
        url: str,
        cookies: dict[str, str],
        proxy: ProxyInfo,
        referer: str | None = None,
        timeout: int = 90,
    ) -> tuple[bytes, Any]:
        assert_cesi_url(url)
        headers = {"Referer": referer} if referer else {}
        response = await AsyncFetcher.get(
            url,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            **self._proxy_kwargs(proxy),
        )
        if int(response.status) != 200:
            raise CesiError(f"HTTP {response.status} pour {url} (final: {response.url})")
        final_url = str(response.url)
        if "/identification/wayf" in final_url or "sts.viacesi.fr" in final_url:
            raise CesiError(
                f"Session CESI expirée : redirection vers le login ({final_url})"
            )
        body = response.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        return bytes(body or b""), response.headers

    async def fetch_html(
        self,
        url: str,
        cookies: dict[str, str],
        proxy: ProxyInfo,
        referer: str | None = None,
    ) -> str:
        data, _ = await self._fetch_bytes(url, cookies, proxy, referer=referer)
        return data.decode("utf-8", errors="replace")

    async def list_shared_documents(
        self,
        session: str,
        cookies: dict[str, str],
        proxy: ProxyInfo,
    ) -> tuple[str, list[SharedDocument]]:
        url = documents_url(session)
        html = await self.fetch_html(url, cookies, proxy)
        return url, extract_documents(html)

    async def list_document_files(
        self,
        document_url: str,
        cookies: dict[str, str],
        proxy: ProxyInfo,
    ) -> list[DocumentFile]:
        assert_cesi_url(document_url)
        html = await self.fetch_html(document_url, cookies, proxy)
        return extract_document_files(html)

    async def download_file(
        self,
        file_url: str,
        cookies: dict[str, str],
        proxy: ProxyInfo,
        referer: str | None = None,
        max_size: int = 100 * 1024 * 1024,
        filename: str | None = None,
    ) -> DownloadedFile:
        assert_cesi_url(file_url)
        data, headers = await self._fetch_bytes(file_url, cookies, proxy, referer=referer, timeout=180)
        if len(data) > max_size:
            raise CesiError(
                f"Fichier trop volumineux : {len(data)} octets > limite {max_size} octets ({file_url})"
            )
        if not data:
            raise CesiError(f"Fichier vide : {file_url}")
        mime_type = _content_type(headers)
        if mime_type.startswith("text/html") and "/medias/fichier/" in file_url:
            raise CesiError(
                "Réponse HTML au lieu du fichier attendu : session CESI probablement expirée."
            )
        resolved_name = filename or _filename_from_headers(headers, file_url)
        return DownloadedFile(
            filename=_ensure_extension(resolved_name, mime_type, file_url),
            url=file_url,
            data=data,
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )
