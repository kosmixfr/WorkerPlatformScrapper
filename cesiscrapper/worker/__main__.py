"""Point d'entrée du worker CESI.

En Docker, l'hôte injecte `WORKER_ID`, `PLATFORM_GRPC_ADDRESS` et
`PLATFORM_WORKER_KEY`. En local, un fichier `.env` peut fournir
`CESI_USERNAME` / `CESI_PASSWORD` (jamais copié dans l'image).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from .functions import build_worker
from .platform_worker import run_worker


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="[cesi-worker] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if "--check" in sys.argv:
        worker = build_worker()
        names = ", ".join(sorted(worker._functions))  # noqa: SLF001 - diagnostic local
        print(f"OK : worker {worker.worker_id} prêt ({names})")
        return 0

    worker = build_worker()
    try:
        asyncio.run(run_worker(worker))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
