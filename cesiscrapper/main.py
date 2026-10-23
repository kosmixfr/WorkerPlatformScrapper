from pathlib import Path
import asyncio
import json
from dataclasses import dataclass
from patchright.async_api import Locator, Page
from scrapling.fetchers import AsyncStealthySession, AsyncFetcher
from scrapling.parser import Adaptor
import os
from dotenv import load_dotenv



 

load_dotenv()


 

LATEST_DOCUMENT_URL = "https://ent.cesi.fr/cpi-a2-info-26-27-nanterre-nr6dp201"
DOCUMENTS_URL = f"{LATEST_DOCUMENT_URL}/documents-partages"
COOKIES_FILE = Path("./cookies.json")
CESI_USERNAME = os.getenv("CESI_USERNAME")
CESI_PASSWORD = os.getenv("CESI_PASSWORD")


 

@dataclass
class SharedDocument:
    """Un document listé sur la page 'documents-partages'."""
    section: str  # ex: "Documents importants à lire absolument", "plannings"
    title: str    # ex: "planning S43"
    url: str      # URL absolue vers la fiche document (page HTML, PAS le fichier)


 

@dataclass
class DocumentFile:
    """Un vrai fichier joint d'une fiche document (document.html)."""
    filename: str  # ex: "CalendrierExamens 2026-2027 CPI A2 INFO.pdf"
    url: str       # ex: "https://ent.cesi.fr/medias/fichier/....-pdf"
    meta: str = ""      # brut: "(PDF, 179 Ko)"
    file_type: str = "" # ex: "PDF"
    size: str = ""      # ex: "179 Ko"

 

def normalize_cookies(cookies) -> dict | None:
    """Convert Playwright-style cookies (list/tuple of dicts) to dict for AsyncFetcher."""
    if not cookies:
        return None if cookies is None else {}
    if isinstance(cookies, dict):
        return cookies
    if isinstance(cookies, (list, tuple)):
        if len(cookies) == 0:
            return {}
        first = cookies[0]
        # Playwright format: [{'name': ..., 'value': ...}, ...]
        if isinstance(first, dict) and "name" in first:
            return {c["name"]: c["value"] for c in cookies}
        # curl format: [('name', 'value'), ...]
        if isinstance(first, (list, tuple)) and len(first) == 2:
            return dict(cookies)
    # CookieJar or unknown: let curl_cffi try, else return None
    return cookies


 

def save_cookies(cookies) -> dict:
    """Normalise + sauvegarde les cookies dans COOKIES_FILE. Retourne le dict sauvé."""
    cookies_dict = normalize_cookies(cookies) or {}
    COOKIES_FILE.write_text(json.dumps(cookies_dict, indent=2), encoding="utf-8")
    return cookies_dict


 

def load_cookies() -> dict | None:
    """Charge les cookies sauvés, ou None si absent/invalide."""
    if not COOKIES_FILE.exists():
        return None
    try:
        data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    return data


 

async def test_login(fetcher: AsyncFetcher | Page | None = None, cookies=None) -> bool:
    url = "https://ent.cesi.fr/accueil-apprenant"
    if isinstance(fetcher, Page):
        playwright_cookies = await fetcher.context.cookies()
        cookies_dict = normalize_cookies(playwright_cookies)
        return await test_login(None, cookies_dict)
    cookies_dict = normalize_cookies(cookies)
    res = await AsyncFetcher.get(url, cookies=cookies_dict)
    print("Got status code", res.status, res.url)
    return res.status == 200 and url == res.url
    
    

 

async def cesi_login(page: Page):
    if await test_login(page):
        print("Already Connected")
        return
    input_login: Locator = page.locator('//*[@id="login"]')
    submit_btn: Locator = page.locator('//*[@id="container"]/div/div/div[2]/form/div/button')


 

    await input_login.wait_for(timeout=10_000)
    await submit_btn.wait_for(timeout=10_000)

 

    await input_login.focus()
    await input_login.fill(CESI_USERNAME)
    await submit_btn.click()



 

    await page.wait_for_load_state("domcontentloaded")
    print("Got domcontent loaded", page.url)
    try:
        await page.wait_for_url(lambda u: "sts.viacesi.fr" in u, timeout=10_000)
        password_input : Locator = page.locator('//*[@id="passwordInput"]')
        submit_btn : Locator = page.locator('//*[@id="submitButton"]')
        await password_input.wait_for(timeout=10_000)
        await submit_btn.wait_for(timeout=10_000)
        await password_input.focus()
        print('Fill Password input')
        await password_input.fill(CESI_PASSWORD)
        await page.wait_for_timeout(1000)
        await submit_btn.click()

 

        await page.wait_for_load_state("load")
    except Exception as e:
        print(f"On arrive sur la page sans login ? ({e})")

 

    return await page.context.cookies()


 

async def do_stealth_login() -> dict:
    """Fait le login navigateur complet et sauvegarde les cookies. Retourne le dict de cookies."""
    if not CESI_USERNAME or not CESI_PASSWORD:
        raise Exception("CESI_USERNAME or CESI_PASSWORD is empty")
    async with AsyncStealthySession(
        headless=True,  # Afficher le navigateur
        timeout=30000,
    ) as session:
        response = await session.fetch(
            "https://ent.cesi.fr/",
            page_action=cesi_login,
        )
        print("Stealth fetch:", response.status, response.url)
        cookies_dict = save_cookies(response.cookies)
        ok = await test_login(None, cookies_dict)
        print("Login OK ?", ok)
        if not ok:
            raise RuntimeError("Login navigateur effectué mais test_login échoue")
        return cookies_dict


 

async def ensure_login() -> dict:
    """Retourne des cookies valides sans logger si possible.

 

    1. Charge cookies.json -> test_login (1 seul test au démarrage).
    2. Si valides, on les réutilise (pas de navigateur).
    3. Sinon, login navigateur + save.
    """
    saved = load_cookies()
    if saved:
        print("Cookies trouvés, test...")
        if await test_login(None, saved):
            print("Cookies valides, pas de login")
            return saved
        print("Cookies expirés/invalides")
    else:
        print("Pas de cookies sauvés")
    print("Login requis...")
    return await do_stealth_login()


 

async def fetch_documents_page(cookies=None) -> str:
    """Fetch la page 'documents-partages' et retourne le HTML (str).

 

    :param cookies: cookies de session (dict, liste Playwright, etc.)
    :raises RuntimeError: si le fetch échoue (statut != 200).
    """
    res = await AsyncFetcher.get(DOCUMENTS_URL, cookies=normalize_cookies(cookies))
    print("Got status code", res.status, res.url)
    if res.status != 200:
        raise RuntimeError(f"Fetch documents échoué: status={res.status} url={res.url}")
    # res.body est en bytes, on décode en str
    body = res.body
    return body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)


 

def extract_latest_documents(html_content: str | bytes) -> list[SharedDocument]:
    """Extrait les documents depuis le HTML de la page 'documents-partages'.

 

    Basé sur la structure observée dans data.html :
      ul.liste__document
        > li.liste__document--element
          > .liste__document--titre  (nom de la section)
          > li.liste__ged-item > a   (un document)

 

    :param html_content: HTML brut (str ou bytes), ex: contenu de data.html
    :return: liste plate de SharedDocument, dans l'ordre de la page
             (les derniers de chaque section sont donc en fin de sous-liste).
    """
    if isinstance(html_content, (bytes, bytearray)):
        html_content = bytes(html_content).decode("utf-8", errors="replace")

 

    page = Adaptor(html_content)
    documents: list[SharedDocument] = []

 

    sections = page.css("ul.liste__document > li.liste__document--element")
    for section_el in sections:
        titre_els = section_el.css(".liste__document--titre")
        section_name = titre_els[0].text.strip() if titre_els else ""

 

        for a in section_el.css("li.liste__ged-item a"):
            title = (a.text or "").strip()
            href = (a.attrib.get("href") or "").strip()
            if not title or not href:
                continue
            # href déjà absolu sur l'ENT, sinon on résout en absolu
            if href.startswith("/"):
                href = f"https://ent.cesi.fr{href}"
            documents.append(SharedDocument(section=section_name, title=title, url=href))

 

    return documents


 

async def fetch_document_page(doc_url: str, cookies=None) -> str:
    """Fetch une fiche document (ex: .../documents-partages/planning-s43) et retourne le HTML.

 

    C'est la page intermédiaire : l'url absolue d'un SharedDocument n'est PAS
    le fichier, il faut ensuite appeler extract_document_files().
    """
    res = await AsyncFetcher.get(doc_url, cookies=normalize_cookies(cookies))
    print("Got status code", res.status, res.url)
    if res.status != 200:
        raise RuntimeError(f"Fetch fiche échoué: status={res.status} url={res.url}")
    body = res.body
    return body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)


 

def extract_document_files(html_content: str | bytes) -> list[DocumentFile]:
    """Extrait les vrais fichiers joints depuis le HTML d'une fiche document.

 

    Basé sur document.html :
      ul.fichiers-joints > li > a.fichiers-joints__lien[href*="/medias/fichier/"]
        + span.fichiers-joints__type-poids "(PDF, 179 Ko)"

 

    :return: liste de DocumentFile (souvent 1 seul, parfois plusieurs).
    """
    import re

 

    if isinstance(html_content, (bytes, bytearray)):
        html_content = bytes(html_content).decode("utf-8", errors="replace")

 

    page = Adaptor(html_content)
    files: list[DocumentFile] = []

 

    for a in page.css("ul.fichiers-joints li a"):
        href = (a.attrib.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://ent.cesi.fr{href}"

 

        # Nom du fichier : texte du lien sans le span "(PDF, 179 Ko)"
        spans = a.css(".fichiers-joints__type-poids")
        meta = spans[0].text.strip() if spans else ""
        full_text = (a.text or "").strip()
        # Adaptor.text inclut parfois déjà sans le span, sinon on le retire
        filename = full_text.replace(meta, "").strip() if meta and meta in full_text else full_text
        if not filename:
            # fallback : basename de l'URL
            filename = href.rstrip("/").split("/")[-1]

 

        file_type, size = "", ""
        m = re.match(r"\(\s*([^,]+)\s*,\s*(.+?)\s*\)", meta)
        if m:
            file_type, size = m.group(1).strip(), m.group(2).strip()
        else:
            # fallback : 1re classe CSS = extension (ex: "pdf fichiers-joints__lien")
            cls = (a.attrib.get("class") or "").split()
            if cls:
                file_type = cls[0].upper()

 

        files.append(DocumentFile(filename=filename, url=href, meta=meta, file_type=file_type, size=size))

 

    return files


 

async def download_file(file_url: str, cookies=None, dest: str | Path | None = None) -> Path:
    """Download un fichier joint (ex: /medias/fichier/....-pdf) avec les cookies de session.

 

    :param file_url: URL absolue du fichier (issue de extract_document_files).
    :param dest: chemin fichier ou dossier. Si None/dossier, le nom est déduit de l'URL.
    :return: Path du fichier écrit.
    """
    res = await AsyncFetcher.get(file_url, cookies=normalize_cookies(cookies))
    print("Got status code", res.status, res.url)
    if res.status != 200:
        raise RuntimeError(f"Download échoué: status={res.status} url={res.url}")

 

    dest_path = Path(dest) if dest else Path("downloads")
    if str(dest_path) == "downloads" or (dest and Path(dest).is_dir()):
        dest_path.mkdir(parents=True, exist_ok=True)
        # nom déduit de l'URL si dossier
        name = file_url.rstrip("/").split("/")[-1]
        if not Path(name).suffix and res.headers:
            ctype = str(res.headers.get("content-type", ""))
            if "pdf" in ctype.lower() and not name.lower().endswith(".pdf"):
                name += ".pdf"
        dest_path = dest_path / name
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

 

    dest_path.write_bytes(res.body)
    print(f"Sauvé: {dest_path} ({len(res.body)} octets)")
    return dest_path


 

async def main():
    cookies = await ensure_login()

 

    try:
        html = await fetch_documents_page(cookies)
    except RuntimeError:
        # Session expirée entre le test et le fetch -> un seul re-login puis retry
        print("Fetch échoué, re-login + retry...")
        cookies = await do_stealth_login()
        html = await fetch_documents_page(cookies)


 

    documents = extract_latest_documents(html)
    print(f"{len(documents)} documents trouvés :")
    for doc in documents:
        print(f" - [{doc.section}] {doc.title} -> {doc.url}")


 

# ---------------- mode interactif (python main.py sans args) ----------------

 

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix} : ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    return value or default


 

async def interactive():
    """Menu interactif : liste les fiches, explore les fichiers joints, télécharge."""
    cookies = await ensure_login()

 

    async def get_index() -> list[SharedDocument]:
        nonlocal cookies
        try:
            html = await fetch_documents_page(cookies)
        except RuntimeError:
            print("Session expirée, re-login...")
            cookies = await do_stealth_login()
            html = await fetch_documents_page(cookies)
        return extract_latest_documents(html)

 

    documents = await get_index()
    if not documents:
        print("Aucun document trouvé.")
        return

 

    files_cache: dict[str, list[DocumentFile]] = {}
    out_dir = Path("downloads")

 

    async def get_files(doc: SharedDocument) -> list[DocumentFile]:
        if doc.url not in files_cache:
            html = await fetch_document_page(doc.url, cookies)
            files_cache[doc.url] = extract_document_files(html)
        return files_cache[doc.url]

 

    while True:
        print("\n=== Documents partagés ===")
        for i, doc in enumerate(documents, 1):
            print(f" {i:2d}. [{doc.section}] {doc.title}")
        print("  r. Refresh la liste")
        print("  q. Quitter")

 

        choice = _ask("Choix")
        if choice.lower() in ("q", "quit", "exit", "0"):
            print("Bye !")
            return
        if choice.lower() in ("r", "refresh"):
            documents = await get_index()
            files_cache.clear()
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(documents)):
            print("Choix invalide, donne un numéro, r ou q.")
            continue

 

        doc = documents[int(choice) - 1]
        print(f"\n-- {doc.title}\n   fiche: {doc.url}")
        try:
            files = await get_files(doc)
        except RuntimeError as e:
            print(f"Erreur fetch fiche : {e}")
            continue
        if not files:
            print("   (aucun fichier joint)")
            continue
        for j, f in enumerate(files, 1):
            print(f"   {j}. {f.filename} [{f.meta}]")

 

        sub = _ask("Télécharger (numéro, 'all', b=retour)", default="b")
        if sub.lower() in ("b", "back", ""):
            continue
        targets = files if sub.lower() == "all" else (
            [files[int(sub) - 1]] if sub.isdigit() and 1 <= int(sub) <= len(files) else None
        )
        if targets is None:
            print("Choix invalide.")
            continue
        dest = _ask("Dossier de sortie", default=str(out_dir))
        for f in targets:
            try:
                await download_file(f.url, cookies, Path(dest) / f.filename)
            except RuntimeError as e:
                print(f"Échec {f.filename} : {e}")


 

# ---------------- petit CLI ----------------

 

async def cmd_list(save: str | None = "data.html"):
    cookies = await ensure_login()
    html = await fetch_documents_page(cookies)
    if save:
        Path(save).write_text(html, encoding="utf-8")
    for doc in extract_latest_documents(html):
        print(f"[{doc.section}] {doc.title}\n  fiche: {doc.url}")


 

async def cmd_files(doc_url: str | None = None, all_docs: bool = False, save: str | None = None):
    cookies = await ensure_login()
    urls: list[str] = []
    if all_docs:
        html = await fetch_documents_page(cookies)
        urls = [d.url for d in extract_latest_documents(html)]
    elif doc_url:
        urls = [doc_url]
    else:
        raise ValueError("Donne une URL de fiche ou --all")
    for u in urls:
        print(f"# {u}")
        html = await fetch_document_page(u, cookies)
        if save and len(urls) == 1:
            Path(save).write_text(html, encoding="utf-8")
        files = extract_document_files(html)
        if not files:
            print("  (aucun fichier joint trouvé)")
        for f in files:
            print(f"  - {f.filename} [{f.meta}] -> {f.url}")


 

async def cmd_download(target: str, out: str | None = None, is_doc_page: bool = False):
    """Download un fichier direct, ou tous les joints d'une fiche (--doc)."""
    cookies = await ensure_login()
    if is_doc_page:
        html = await fetch_document_page(target, cookies)
        files = extract_document_files(html)
        if not files:
            print("Aucun fichier joint sur cette fiche")
            return
        dest_dir = Path(out) if out else Path("downloads")
        for f in files:
            await download_file(f.url, cookies, dest_dir / f.filename)
    else:
        await download_file(target, cookies, out or "downloads")


 

def cli():
    import argparse
    import sys

 

    # Sans args -> mode interactif
    if len(sys.argv) == 1:
        asyncio.run(interactive())
        return

 

    p = argparse.ArgumentParser(prog="cesi-docs", description="Liste et télécharge les documents CESI")
    sub = p.add_subparsers(dest="cmd")

 

    p_list = sub.add_parser("list", help="Lister les fiches documents")
    p_list.add_argument("--save", default="data.html", help="Sauver l'index HTML (vide pour ne pas sauver)")

 

    p_files = sub.add_parser("files", help="Lister les fichiers joints d'une fiche")
    p_files.add_argument("doc_url", nargs="?", help="URL de la fiche document")
    p_files.add_argument("--all", action="store_true", help="Toutes les fiches de l'index")
    p_files.add_argument("--save", default=None, help="Sauver la fiche HTML (1 seule URL)")

 

    p_dl = sub.add_parser("download", help="Télécharger un fichier ou tous ceux d'une fiche")
    p_dl.add_argument("target", help="URL du fichier (/medias/fichier/...) ou de la fiche avec --doc")
    p_dl.add_argument("-o", "--out", default=None, help="Fichier ou dossier de sortie (défaut: downloads/)")
    p_dl.add_argument("--doc", action="store_true", help="Target est une fiche document, pas un fichier direct")

 

    args = p.parse_args()
    if not args.cmd:
        asyncio.run(main())  # compat : sans args = ancien comportement
    elif args.cmd == "list":
        asyncio.run(cmd_list(save=args.save or None))
    elif args.cmd == "files":
        asyncio.run(cmd_files(doc_url=args.doc_url, all_docs=args.all, save=args.save))
    elif args.cmd == "download":
        asyncio.run(cmd_download(args.target, out=args.out, is_doc_page=args.doc))



 

if __name__ == "__main__":
    cli()