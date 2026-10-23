"""Client Python du KV daemon de la Warm Scraper Platform.

Le daemon KV est la seule voie vers la persistance clé/valeur de la plateforme.
Le container s'y connecte en gRPC (Unix Domain Socket ou TCP) et s'authentifie
avec son `run_token` injecté par l'hôte dans `PLATFORM_KV_RUN_TOKEN`.

Ce module reproduit le comportement minimal du SDK TypeScript
(`packages/container-sdk/src/kv.ts`) : Default KV de la Run (`store=""`),
Named KV Stores, bascule automatique sur les RPC streaming pour les gros
objets (PDF) et génération d'URLs publiques signées.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable

import grpc

from .generated import kv_pb2, kv_pb2_grpc

# Le SDK TypeScript utilise 3,5 Mo comme seuil de bascule vers GetStream.
SINGLE_MESSAGE_LIMIT_BYTES = int(os.getenv("PLATFORM_KV_SINGLE_MESSAGE_LIMIT_BYTES", str(3_500_000)))
STREAM_CHUNK_BYTES = 4 * 1024 * 1024
DEFAULT_GRPC_MAX_MESSAGE_BYTES = int(os.getenv("PLATFORM_KV_MAX_MESSAGE_BYTES", str(256 * 1024 * 1024)))


class KvError(RuntimeError):
    """Erreur renvoyée par le daemon KV ou par le transport gRPC."""


def _normalize_address(raw: str) -> str:
    """grpc n'accepte que ``host:port``, ``dns:///...`` ou ``unix:///...``."""
    if raw.startswith("tcp://"):
        return raw[len("tcp://") :]
    return raw


class KvApi:
    """Accès KV lié à la Run courante du container."""

    def __init__(
        self,
        address: str | None = None,
        run_token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        raw_address = address or os.getenv("PLATFORM_KV_ADDRESS") or "unix:///var/run/platform/kv.sock"
        self.address = _normalize_address(raw_address)
        self.run_token = run_token if run_token is not None else (os.getenv("PLATFORM_KV_RUN_TOKEN") or "")
        self.timeout = timeout if timeout is not None else float(os.getenv("PLATFORM_KV_TIMEOUT_MS", "60000")) / 1000
        self.stream_timeout = float(os.getenv("PLATFORM_KV_STREAM_TIMEOUT_MS", "600000")) / 1000
        self._channel: grpc.aio.Channel | None = None
        self._stub: kv_pb2_grpc.KvDaemonStub | None = None

    @property
    def configured(self) -> bool:
        """True si le container est rattaché à une Run (run_token présent)."""
        return bool(self.run_token)

    # ------------------------------------------------------------------ gRPC

    @property
    def stub(self) -> kv_pb2_grpc.KvDaemonStub:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                self.address,
                options=[
                    ("grpc.max_send_message_length", DEFAULT_GRPC_MAX_MESSAGE_BYTES),
                    ("grpc.max_receive_message_length", DEFAULT_GRPC_MAX_MESSAGE_BYTES),
                ],
            )
            self._stub = kv_pb2_grpc.KvDaemonStub(self._channel)
        return self._stub

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        if not self.run_token:
            raise KvError(
                "KV indisponible : PLATFORM_KV_RUN_TOKEN absent "
                "(le container n'est pas rattaché à une Run)."
            )
        return (("authorization", f"Bearer {self.run_token}"),)

    async def _reset(self) -> None:
        channel = self._channel
        self._channel = None
        self._stub = None
        if channel is not None:
            try:
                await channel.close(grace=None)
            except Exception:
                pass

    async def _unary(self, method_name: str, request: Any, timeout: float | None = None) -> Any:
        """Appel unary/unary avec une reconnexion automatique."""
        self._metadata()  # valide la présence du token
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                method = getattr(self.stub, method_name)
                response = await method(request, metadata=self._metadata(), timeout=timeout or self.timeout)
                error = getattr(response, "error", "")
                if error:
                    raise KvError(f"KV {method_name} : {error}")
                return response
            except grpc.aio.AioRpcError as exc:
                last_error = exc
                await self._reset()
                if attempt == 1:
                    raise KvError(f"KV {method_name} : {exc.details() or exc.code().name}") from exc
            except KvError:
                raise
        raise KvError(f"KV {method_name} : échec inconnu") from last_error

    # --------------------------------------------------------------- lecture

    async def get_bytes(self, key: str, store: str = "") -> tuple[bytes, str] | None:
        """Lit une valeur brute. Retourne ``(octets, content_type)`` ou None."""
        response = await self._unary("Get", kv_pb2.GetRequest(store=store, key=key))
        if not response.found:
            return None
        size = int(response.size or 0)
        content_type = response.content_type or ""
        if size <= SINGLE_MESSAGE_LIMIT_BYTES:
            return bytes(response.value or b""), content_type
        return await self._get_stream(key, store)

    async def _get_stream(self, key: str, store: str) -> tuple[bytes, str] | None:
        self._metadata()
        chunks: list[bytes] = []
        content_type = ""
        found = False
        try:
            call = self.stub.GetStream(
                kv_pb2.GetStreamRequest(store=store, key=key),
                metadata=self._metadata(),
                timeout=self.stream_timeout,
            )
            async for chunk in call:
                if chunk.error:
                    raise KvError(f"KV GetStream : {chunk.error}")
                content_type = content_type or chunk.content_type or ""
                found = bool(chunk.found)
                if chunk.data:
                    chunks.append(bytes(chunk.data))
                if chunk.last:
                    break
        except grpc.aio.AioRpcError as exc:
            await self._reset()
            raise KvError(f"KV GetStream : {exc.details() or exc.code().name}") from exc
        if not found:
            return None
        return b"".join(chunks), content_type

    async def get_value(self, key: str, store: str = "") -> Any:
        raw = await self.get_bytes(key, store)
        if raw is None:
            return None
        data, content_type = raw
        return _decode(data, content_type)

    # --------------------------------------------------------------- écriture

    async def set_bytes(self, key: str, data: bytes, store: str = "", content_type: str = "application/octet-stream") -> None:
        """Écrit une valeur brute, en streaming au-delà du seuil gRPC."""
        if len(data) <= SINGLE_MESSAGE_LIMIT_BYTES:
            await self._unary(
                "Set",
                kv_pb2.SetRequest(store=store, key=key, value=data, content_type=content_type),
            )
            return
        await self._set_stream(key, data, store, content_type)

    async def _set_stream(self, key: str, data: bytes, store: str, content_type: str) -> None:
        self._metadata()
        total = len(data)
        if total == 0:
            chunks = [b""]
        else:
            chunks = [data[offset : offset + STREAM_CHUNK_BYTES] for offset in range(0, total, STREAM_CHUNK_BYTES)]

        async def requests() -> AsyncIterator[kv_pb2.SetStreamRequest]:
            for index, chunk in enumerate(chunks):
                yield kv_pb2.SetStreamRequest(
                    store=store,
                    key=key,
                    data=chunk,
                    content_type=content_type,
                    last=index == len(chunks) - 1,
                    total_size=total if index == 0 else 0,
                )

        try:
            response = await self.stub.SetStream(
                requests(),
                metadata=self._metadata(),
                timeout=self.stream_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            await self._reset()
            raise KvError(f"KV SetStream : {exc.details() or exc.code().name}") from exc
        if response.error:
            raise KvError(f"KV SetStream : {response.error}")

    async def set_value(self, key: str, value: Any, store: str = "", content_type: str | None = None) -> None:
        if value is None:
            await self.delete_value(key, store)
            return
        data, inferred = _encode(value, content_type)
        await self.set_bytes(key, data, store, inferred)

    async def set_value_stream(
        self,
        key: str,
        source: Iterable[bytes] | AsyncIterator[bytes],
        store: str = "",
        content_type: str = "application/octet-stream",
    ) -> None:
        """Écrit une valeur depuis un itérable de morceaux (sans tout concaténer)."""
        self._metadata()
        if isinstance(source, (bytes, bytearray)):
            await self.set_bytes(key, bytes(source), store, content_type)
            return
        total = 0
        first = True

        async def requests() -> AsyncIterator[kv_pb2.SetStreamRequest]:
            nonlocal total, first
            if hasattr(source, "__aiter__"):
                async for chunk in source:  # type: ignore[union-attr]
                    chunk = bytes(chunk)
                    total += len(chunk)
                    yield kv_pb2.SetStreamRequest(
                        store=store,
                        key=key,
                        data=chunk,
                        content_type=content_type,
                        last=False,
                        total_size=total if first else 0,
                    )
                    first = False
            else:
                for chunk in source:  # type: ignore[union-attr]
                    chunk = bytes(chunk)
                    total += len(chunk)
                    yield kv_pb2.SetStreamRequest(
                        store=store,
                        key=key,
                        data=chunk,
                        content_type=content_type,
                        last=False,
                        total_size=total if first else 0,
                    )
                    first = False
            yield kv_pb2.SetStreamRequest(
                store=store,
                key=key,
                data=b"",
                content_type=content_type,
                last=True,
                total_size=total,
            )

        try:
            response = await self.stub.SetStream(
                requests(),
                metadata=self._metadata(),
                timeout=self.stream_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            await self._reset()
            raise KvError(f"KV SetStream : {exc.details() or exc.code().name}") from exc
        if response.error:
            raise KvError(f"KV SetStream : {response.error}")

    # ------------------------------------------------------------- opérations

    async def delete_value(self, key: str, store: str = "") -> bool:
        response = await self._unary("Delete", kv_pb2.DeleteRequest(store=store, key=key))
        return bool(response.deleted)

    async def exists(self, key: str, store: str = "") -> bool:
        response = await self._unary("Exists", kv_pb2.ExistsRequest(store=store, key=key))
        return bool(response.found)

    async def list_keys(self, store: str = "", prefix: str = "", cursor: str = "", limit: int = 100) -> dict[str, Any]:
        response = await self._unary(
            "ListKeys",
            kv_pb2.ListKeysRequest(store=store, prefix=prefix, cursor=cursor, limit=limit),
        )
        return {
            "items": [
                {
                    "key": item.key,
                    "size": int(item.size),
                    "contentType": item.content_type,
                    "updatedAt": item.updated_at,
                }
                for item in response.items
            ],
            "nextCursor": response.next_cursor or None,
        }

    async def flush(self) -> None:
        await self._unary("Flush", kv_pb2.FlushRequest())

    async def create_store(self, name: str) -> str:
        response = await self._unary("StoreCreate", kv_pb2.StoreCreateRequest(name=name))
        return response.store_id

    async def list_stores(self) -> list[dict[str, str]]:
        response = await self._unary("StoreList", kv_pb2.StoreListRequest())
        return [{"id": s.id, "name": s.name, "type": s.type} for s in response.stores]

    async def get_public_url(self, key: str, store: str = "", expires_in: int = 3600) -> dict[str, str]:
        response = await self._unary(
            "PublicUrl",
            kv_pb2.PublicUrlRequest(store=store, key=key, expires_in=expires_in),
            timeout=max(self.timeout, 30),
        )
        if not response.url:
            raise KvError("URL publique indisponible")
        return {"url": response.url, "expiresAt": response.expires_at or ""}

    async def close(self) -> None:
        await self._reset()


# --------------------------------------------------------------------- codec


def _encode(value: Any, content_type: str | None = None) -> tuple[bytes, str]:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value), content_type or "application/octet-stream"
    if isinstance(value, str):
        return value.encode("utf-8"), content_type or "text/plain; charset=utf-8"
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        content_type or "application/json",
    )


def _decode(data: bytes, content_type: str) -> Any:
    lowered = (content_type or "").lower()
    if "json" in lowered:
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return data
    if lowered.startswith("text/") or any(token in lowered for token in ("xml", "html", "svg")):
        return data.decode("utf-8", errors="replace")
    return data
