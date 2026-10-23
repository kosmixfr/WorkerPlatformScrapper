"""Worker CESI autonome pour la Warm Scraper Platform (PlatformScrapper).

Le worker parle le contrat gRPC `warmplatform.ContainerGateway` (stream
bidirectionnel `Connect`) et expose des fonctions de scraping des documents
partagés de l'ENT CESI.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
