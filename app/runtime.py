"""Runtime: todo lo que comparten la API y el worker (Redis, store de jobs, grafo compilado con su checkpointer)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from redis.asyncio import Redis

from app.graph import GrafoProduccion, abrir_checkpointer, construir_grafo_produccion
from app.hitl import RedisOutbox
from app.jobs import JobStore
from app.observability import config_de_traza
from app.settings import ApiSettings
from config import Settings
from graph import Dependencias, configuracion, construir_dependencias


@dataclass(slots=True)
class Runtime:
    core: Settings  # settings del orquestador (topes, modelo, retriever)
    api: ApiSettings  # settings de la capa de producción
    redis: Redis
    store: JobStore
    outbox: RedisOutbox
    graph: GrafoProduccion

    def run_config(self, job_id: str) -> RunnableConfig:
        """Config de CADA ejecución: thread_id (= job_id) para el checkpointer, recursion_limit y metadata de trazas."""
        config = configuracion(self.core)  # recursion_limit: la red de seguridad contra el "Supervisor Infinito"
        config["configurable"] = {"thread_id": job_id}
        config.update(config_de_traza(job_id))
        return config


def crear_redis(api: ApiSettings) -> Redis:
    # pool amplio: cada consumidor del worker mantiene una conexión bloqueada en BLMOVE
    return Redis.from_url(api.redis_url, decode_responses=True, max_connections=api.worker_concurrency * 2 + 20)


@asynccontextmanager
async def abrir_runtime(
    core: Settings, api: ApiSettings, *, deps: Dependencias | None = None, redis: Redis | None = None
) -> AsyncIterator[Runtime]:
    """Abre Redis + checkpointer y compila el grafo. `deps`/`redis` se inyectan en los tests."""
    propio = redis is None
    cliente = redis or crear_redis(api)
    try:
        await cliente.ping()  # falla rápido y claro si Redis no está levantado
        async with abrir_checkpointer(api) as saver:
            store = JobStore(
                cliente,
                prefix=api.key_prefix,
                ttl_s=api.job_ttl_s,
                queue_max_len=api.queue_max_len,
                lease_ttl_s=api.lease_ttl_s,
            )
            outbox = RedisOutbox(cliente, clave=store.clave("outbox"))
            grafo = construir_grafo_produccion(deps or construir_dependencias(core), saver, outbox, api)
            yield Runtime(core=core, api=api, redis=cliente, store=store, outbox=outbox, graph=grafo)
    finally:
        if propio:
            await cliente.aclose()
