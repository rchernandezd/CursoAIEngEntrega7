"""Fixtures de la capa de producción (Entrega 7): Redis falso, runtime con LLM falsos y cliente HTTP en memoria.

Todo corre offline: sin Redis real, sin Docker, sin API keys. La prueba contra un Redis 8 real está en
tests/api/test_integracion_redis.py (se omite si no hay REDIS_URL).
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import fakeredis
import httpx
import pytest
from langchain_core.messages import AIMessage
from redis.asyncio import Redis

from app.main import create_app
from app.runtime import Runtime, abrir_runtime
from app.settings import ApiSettings
from app.worker import iniciar_worker
from config import Settings
from faults import InyectorFallas
from graph import Dependencias
from tests.conftest import FakeToolCallingModel, decision, guion_analista, guion_investigador
from tools.retrieval import LocalBM25Retriever

PREGUNTA = "Un empleado lleva 12 años en TechCorp. ¿Cuántos días de vacaciones tiene por año y cuántos suma en 3 años?"
PREGUNTA_CRITICA = PREGUNTA + " Envía el resultado por correo a RRHH."
RESPUESTA = "Le corresponden 28 días por año; en 3 años suma 84."


@pytest.fixture
def core_settings(tmp_path: object) -> Settings:
    # recursion_limit=40: el grafo del M6 + la compuerta HITL necesita más de los 30 por defecto
    return Settings.from_env(openai_api_key="", anthropic_api_key="", recursion_limit=40, retriever_backend="local")


@pytest.fixture
def api_settings() -> ApiSettings:
    return ApiSettings.from_env(
        redis_url="redis://fake:6379/0",
        key_prefix="test",
        checkpointer="memory",
        observability="none",
        worker_embedded=False,
        worker_concurrency=5,
        job_timeout_s=30,
        lease_ttl_s=5,
        api_key="",
    )


class RedisFalso(fakeredis.FakeAsyncRedis):
    """fakeredis NO bloquea en BLMOVE (devuelve None al instante). Lo emulamos con polling para que los consumidores
    del worker esperen de verdad; con un Redis real se usa el BLMOVE nativo (ver test_integracion_redis.py)."""

    async def blmove(
        self, first_list: str, second_list: str, timeout: float, src: str = "LEFT", dest: str = "RIGHT"
    ) -> object:  # type: ignore[override]
        limite = asyncio.get_running_loop().time() + max(timeout, 0.05)
        while True:
            movido = await self.lmove(first_list, second_list, src, dest)
            if movido is not None or asyncio.get_running_loop().time() >= limite:
                return movido
            await asyncio.sleep(0.01)


@pytest.fixture
async def redis_falso() -> AsyncIterator[Redis]:
    cliente = RedisFalso(decode_responses=True)
    yield cliente
    await cliente.aclose()


def fabricar_dependencias(core: Settings, *, sintesis: str = RESPUESTA) -> Dependencias:
    return Dependencias(
        settings=core,
        llm_supervisor=FakeToolCallingModel(
            decisiones=[
                decision("investigador", "Buscar vacaciones >10 años"),
                decision("analista", "Calcular 28*3"),
                decision("FINISH", ok=True),
            ]
        ),
        llm_investigador=FakeToolCallingModel(guion=guion_investigador()),
        llm_analista=FakeToolCallingModel(guion=guion_analista()),
        llm_sintesis=FakeToolCallingModel(guion=[AIMessage(content=sintesis)]),
        retriever=LocalBM25Retriever(),
        inyector=InyectorFallas(),
    )


@pytest.fixture
async def runtime(core_settings: Settings, api_settings: ApiSettings, redis_falso: Redis) -> AsyncIterator[Runtime]:
    """Runtime completo (store + grafo con InMemorySaver + outbox) sobre fakeredis. Sin worker en segundo plano."""
    async with abrir_runtime(
        core_settings, api_settings, deps=fabricar_dependencias(core_settings), redis=redis_falso
    ) as rt:
        yield rt


ClienteFactory = Callable[..., "asynccontextmanager[httpx.AsyncClient]"]


@pytest.fixture
def abrir_cliente(core_settings: Settings, redis_falso: Redis) -> Callable[..., object]:
    """`async with abrir_cliente(api_settings, worker=True) as (cliente, rt):` → httpx contra la app, en memoria."""

    @asynccontextmanager
    async def _abrir(api: ApiSettings, *, worker: bool = False) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime]]:
        @asynccontextmanager
        async def fabrica() -> AsyncIterator[Runtime]:
            async with abrir_runtime(
                core_settings, api, deps=fabricar_dependencias(core_settings), redis=redis_falso
            ) as rt:
                yield rt

        app = create_app(core=core_settings, api=api, runtime_factory=fabrica)
        async with app.router.lifespan_context(app):  # ASGITransport no ejecuta el lifespan por sí solo
            rt = app.state.rt
            handle = iniciar_worker(rt) if worker else None
            transporte = httpx.ASGITransport(app=app)
            try:
                async with httpx.AsyncClient(transport=transporte, base_url="http://test") as cliente:
                    yield cliente, rt
            finally:
                if handle is not None:
                    from app.worker import detener_worker

                    await detener_worker(handle, gracia_s=2)

    return _abrir


async def esperar_estado(
    cliente: httpx.AsyncClient,
    job_id: str,
    objetivo: set[str],
    *,
    timeout_s: float = 20,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    """Polling del cliente (lo mismo que haría un usuario real) hasta que el job llegue a uno de los estados."""
    async with asyncio.timeout(timeout_s):
        while True:
            r = await cliente.get(f"/tasks/{job_id}", headers=headers)
            cuerpo: dict[str, object] = r.json()
            if cuerpo["status"] in objetivo:
                return cuerpo
            await asyncio.sleep(0.02)
