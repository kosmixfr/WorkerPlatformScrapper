"""Runtime gRPC d'un worker chaud pour la Warm Scraper Platform.

Le worker ouvre un unique stream bidirectionnel `ContainerGateway/Connect`
vers l'hôte, envoie un `Hello` avec ses définitions de fonctions (JSON Schema
draft-07) puis traite les `Invocation`. Il peut appeler les capacités de
l'hôte (`dataset.push`, `proxy.get`) via `HostRequest` et accède au KV daemon
avec `KvApi`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import grpc
from jsonschema import Draft7Validator, FormatChecker

from .generated import platform_pb2, platform_pb2_grpc
from .kv_client import KvApi

LOGGER = logging.getLogger("cesi-worker")

GRPC_MAX_MESSAGE_BYTES = int(os.getenv("PLATFORM_GRPC_MAX_MESSAGE_BYTES", str(256 * 1024 * 1024)))


class WorkerError(RuntimeError):
    """Erreur fonctionnelle renvoyée à l'appelant."""


def _normalize_grpc_target(raw: str) -> str:
    if raw.startswith("tcp://"):
        return raw[len("tcp://") :]
    return raw


@dataclass
class FunctionSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], "PlatformContext"], Awaitable[Mapping[str, Any]]]


class PlatformContext:
    """Capacités exposées au handler d'une fonction."""

    def __init__(self, worker: "WarmWorker", kv: KvApi) -> None:
        self._worker = worker
        self.kv = kv

    async def request(self, kind: str, payload: Any = None) -> Any:
        return await self._worker.request_host(kind, payload if payload is not None else {})

    @property
    def dataset(self) -> "DatasetApi":
        return DatasetApi(self)

    @property
    def proxy(self) -> "ProxyApi":
        return ProxyApi(self)


class DatasetApi:
    def __init__(self, context: PlatformContext) -> None:
        self._context = context

    async def push(self, dataset: str, item: Any) -> dict[str, Any]:
        return await self._context.request("dataset.push", {"dataset": dataset, "item": item})


class ProxyApi:
    def __init__(self, context: PlatformContext) -> None:
        self._context = context

    async def get(self) -> dict[str, str | None]:
        result = await self._context.request("proxy.get", {})
        url = result.get("url") if isinstance(result, dict) else None
        token = os.getenv("PLATFORM_PROXY_CHAIN_TOKEN") if url else None
        return {"url": url, "token": token}


class WarmWorker:
    """Client du stream `ContainerGateway/Connect`."""

    def __init__(
        self,
        worker_id: str,
        grpc_address: str,
        worker_key: str,
        host_request_timeout: float = 15.0,
    ) -> None:
        self.worker_id = worker_id
        self.grpc_address = _normalize_grpc_target(grpc_address)
        self.worker_key = worker_key
        self.host_request_timeout = host_request_timeout

        self._functions: dict[str, FunctionSpec] = {}
        self._validators: dict[str, tuple[Draft7Validator, Draft7Validator]] = {}
        self._stop = asyncio.Event()
        self._fatal = False
        self._task: asyncio.Task[None] | None = None
        self._call: Any = None
        self._outbox: asyncio.Queue[Any] | None = None
        self._connected = False
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._running_invocations: set[asyncio.Task[Any]] = set()
        self.kv = KvApi()

    # ------------------------------------------------------------ définition

    def expose(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], PlatformContext], Awaitable[Mapping[str, Any]]],
    ) -> "WarmWorker":
        if self._task is not None:
            raise WorkerError("Les fonctions doivent être exposées avant start().")
        if name in self._functions:
            raise WorkerError(f"Fonction dupliquée : {name}")
        Draft7Validator.check_schema(input_schema)
        Draft7Validator.check_schema(output_schema)
        self._validators[name] = (
            Draft7Validator(input_schema, format_checker=FormatChecker()),
            Draft7Validator(output_schema, format_checker=FormatChecker()),
        )
        self._functions[name] = FunctionSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            handler=handler,
        )
        return self

    # --------------------------------------------------------------- cycle vie

    async def start(self) -> None:
        if self._task is not None:
            return
        LOGGER.info(
            "Démarrage du worker %s -> %s (%d fonctions)",
            self.worker_id,
            self.grpc_address,
            len(self._functions),
        )
        self._task = asyncio.create_task(self._run(), name="cesi-worker-grpc")

    async def wait_stopped(self) -> None:
        """Attend un arrêt demandé par le worker lui-même (ex. clé invalide)."""
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()
        call = self._call
        if call is not None:
            try:
                call.cancel()
            except Exception:
                pass
        if self._outbox is not None:
            await self._outbox.put(None)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for task in list(self._running_invocations):
            task.cancel()
        if self._running_invocations:
            await asyncio.gather(*self._running_invocations, return_exceptions=True)
            self._running_invocations.clear()
        await self.kv.close()

    # ------------------------------------------------------------- connexion

    async def _run(self) -> None:
        while not self._stop.is_set() and not self._fatal:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("gRPC déconnecté : %s ; reconnexion dans 1 s", exc)
            if self._stop.is_set() or self._fatal:
                break
            await asyncio.sleep(1)

    def _hello(self) -> platform_pb2.Envelope:
        functions = [
            platform_pb2.ExposedFunction(
                name=spec.name,
                description=spec.description,
                input_schema_json=json.dumps(spec.input_schema, ensure_ascii=False, separators=(",", ":")),
                output_schema_json=json.dumps(spec.output_schema, ensure_ascii=False, separators=(",", ":")),
                is_test=False,
            )
            for spec in self._functions.values()
        ]
        return platform_pb2.Envelope(
            hello=platform_pb2.Hello(
                worker_id=self.worker_id,
                api_key=self.worker_key,
                functions=functions,
            )
        )

    async def _connect_once(self) -> None:
        outbox: asyncio.Queue[Any] = asyncio.Queue()
        self._outbox = outbox
        self._pending = {}
        self._connected = True

        channel = grpc.aio.insecure_channel(
            self.grpc_address,
            options=[
                ("grpc.max_send_message_length", GRPC_MAX_MESSAGE_BYTES),
                ("grpc.max_receive_message_length", GRPC_MAX_MESSAGE_BYTES),
                ("grpc.keepalive_time_ms", 30_000),
                ("grpc.keepalive_timeout_ms", 10_000),
                ("grpc.keepalive_permit_without_calls", 1),
            ],
        )
        stub = platform_pb2_grpc.ContainerGatewayStub(channel)
        await outbox.put(self._hello())

        call = stub.Connect(self._request_iterator(outbox))
        self._call = call
        try:
            async for envelope in call:
                kind = envelope.WhichOneof("message")
                if kind == "invocation":
                    task = asyncio.create_task(self._execute(envelope.invocation), name=f"invoke:{envelope.invocation.function_name}")
                    self._running_invocations.add(task)
                    task.add_done_callback(self._running_invocations.discard)
                elif kind == "host_response":
                    self._handle_host_response(envelope.host_response)
        finally:
            self._connected = False
            self._call = None
            self._outbox = None
            self._fail_pending(RuntimeError("Connexion à l'hôte perdue"))
            try:
                await channel.close(grace=None)
            except Exception:
                pass

    async def _request_iterator(self, outbox: asyncio.Queue[Any]):
        while True:
            item = await outbox.get()
            if item is None:
                return
            yield item

    async def _send(self, envelope: platform_pb2.Envelope) -> None:
        if not self._connected or self._outbox is None:
            raise WorkerError("Worker non connecté à l'hôte")
        await self._outbox.put(envelope)

    # ---------------------------------------------------------- invocations

    async def _execute(self, invocation: platform_pb2.Invocation) -> None:
        request_id = invocation.request_id
        try:
            spec = self._functions.get(invocation.function_name)
            if spec is None:
                raise WorkerError(f"Fonction inconnue : {invocation.function_name}")
            try:
                payload = json.loads(invocation.input_json or "{}")
            except json.JSONDecodeError as exc:
                raise WorkerError(f"Entrée JSON invalide : {exc}") from exc
            if not isinstance(payload, dict):
                raise WorkerError("L'entrée doit être un objet JSON")

            input_validator, output_validator = self._validators[spec.name]
            errors = sorted(input_validator.iter_errors(payload), key=lambda e: list(e.path))
            if errors:
                raise WorkerError("Entrée invalide : " + "; ".join(error.message for error in errors))

            context = PlatformContext(self, self.kv)
            result = await spec.handler(payload, context)
            if not isinstance(result, Mapping):
                raise WorkerError("Le handler doit retourner un objet JSON")

            errors = sorted(output_validator.iter_errors(dict(result)), key=lambda e: list(e.path))
            if errors:
                raise WorkerError("Sortie invalide : " + "; ".join(error.message for error in errors))

            await self._send(
                platform_pb2.Envelope(
                    invocation_result=platform_pb2.InvocationResult(
                        request_id=request_id,
                        output_json=json.dumps(result, ensure_ascii=False, default=str),
                        error="",
                    )
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "Échec de %s : %s\n%s",
                invocation.function_name,
                exc,
                traceback.format_exc(),
            )
            try:
                await self._send(
                    platform_pb2.Envelope(
                        invocation_result=platform_pb2.InvocationResult(
                            request_id=request_id,
                            output_json="",
                            error=str(exc) or exc.__class__.__name__,
                        )
                    )
                )
            except Exception:  # noqa: BLE001
                LOGGER.warning("Impossible de renvoyer l'erreur d'invocation (déconnecté).")

    # --------------------------------------------------- capacités de l'hôte

    async def request_host(self, kind: str, payload: Any) -> Any:
        if not self._connected or self._outbox is None:
            raise WorkerError("Worker non connecté à l'hôte")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                platform_pb2.Envelope(
                    host_request=platform_pb2.HostRequest(
                        request_id=request_id,
                        kind=kind,
                        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    )
                )
            )
            return await asyncio.wait_for(future, timeout=self.host_request_timeout)
        except asyncio.TimeoutError as exc:
            raise WorkerError(f"Capacité {kind} : délai dépassé ({self.host_request_timeout:.0f} s)") from exc
        finally:
            self._pending.pop(request_id, None)

    def _handle_host_response(self, response: platform_pb2.HostResponse) -> None:
        if response.request_id == "hello":
            if response.error:
                self._fatal = True
                LOGGER.error("Enregistrement refusé par l'hôte : %s", response.error)
                self.stop_now()
            else:
                LOGGER.info("Worker %s enregistré auprès de l'hôte", self.worker_id)
            return

        future = self._pending.pop(response.request_id, None)
        if future is None or future.done():
            return
        if response.error:
            future.set_exception(WorkerError(response.error))
            return
        try:
            future.set_result(json.loads(response.payload_json) if response.payload_json else {})
        except json.JSONDecodeError:
            future.set_result(response.payload_json)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def stop_now(self) -> None:
        self._stop.set()
        call = self._call
        if call is not None:
            try:
                call.cancel()
            except Exception:
                pass


async def run_worker(worker: WarmWorker) -> None:
    """Démarre le worker et attend un signal d'arrêt."""
    await worker.start()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop(*_: Any) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows : pas de add_signal_handler pour SIGTERM.
            pass

    stop_task = asyncio.create_task(stop.wait(), name="signal-stop")
    closed_task = asyncio.create_task(worker.wait_stopped(), name="worker-stop")
    try:
        await asyncio.wait({stop_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, closed_task):
            task.cancel()
        await worker.stop()
