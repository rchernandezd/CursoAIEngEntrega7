"""Integración contra un Redis REAL con RedisJSON + RediSearch (Redis 8 o redis-stack-server). Se omite sin REDIS_URL.

    docker compose up -d redis
    REDIS_URL=redis://localhost:6379/15 pytest tests/api/test_integracion_redis.py -v

Demuestra lo que fakeredis no puede: BLMOVE bloqueante real, AsyncRedisSaver (checkpoints en Redis) y, sobre todo,
que un job pausado en WAITING_APPROVAL se puede reanudar desde un RUNTIME NUEVO (= reinicio del servicio).
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.runtime import Runtime, abrir_runtime
from app.settings import ApiSettings
from app.worker import procesar_job
from config import Settings
from tests.api.conftest import PREGUNTA_CRITICA, RESPUESTA, fabricar_dependencias

REDIS_URL = os.getenv("REDIS_URL", "")
pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="define REDIS_URL (Redis 8 / redis-stack) para correr la integración"
)


@pytest.fixture
def settings_redis() -> tuple[Settings, ApiSettings]:
    core = Settings.from_env(openai_api_key="", recursion_limit=40, retriever_backend="local")
    api = ApiSettings.from_env(
        redis_url=REDIS_URL,
        key_prefix=f"it{uuid.uuid4().hex[:8]}",  # aislado: no toca otras claves de la instancia
        checkpointer="redis",
        observability="none",
        worker_embedded=False,
    )
    return core, api


@pytest.fixture
async def limpiar(settings_redis: tuple[Settings, ApiSettings]) -> AsyncIterator[None]:
    yield
    r = Redis.from_url(REDIS_URL, decode_responses=True)
    claves = [k async for k in r.scan_iter(match=f"{settings_redis[1].key_prefix}:*")]
    if claves:
        await r.delete(*claves)
    await r.aclose()


async def test_blmove_real_bloquea_y_entrega(settings_redis: tuple[Settings, ApiSettings], limpiar: None) -> None:
    core, api = settings_redis
    async with abrir_runtime(core, api, deps=fabricar_dependencias(core)) as rt:
        assert await rt.store.tomar(1) is None  # cola vacía: espera 1 s y devuelve None
        job = await rt.store.crear("pregunta de prueba")
        assert await rt.store.tomar(1) == job.job_id
        assert await rt.store.largo_procesando() == 1
        await rt.store.confirmar(job.job_id)


async def _correr(rt: Runtime, job_id: str) -> None:
    await rt.store.tomar(1)
    await procesar_job(rt, job_id)


async def test_pausa_reinicio_y_reanudacion(settings_redis: tuple[Settings, ApiSettings], limpiar: None) -> None:
    core, api = settings_redis
    # --- proceso 1: ejecuta hasta la compuerta HITL y "muere"
    async with abrir_runtime(core, api, deps=fabricar_dependencias(core)) as rt1:
        job = await rt1.store.crear(PREGUNTA_CRITICA)
        await _correr(rt1, job.job_id)
        pausado = await rt1.store.obtener(job.job_id)
        assert pausado is not None and pausado.status == "WAITING_APPROVAL"
        assert await rt1.outbox.listar() == []

    # --- proceso 2: runtime NUEVO (grafo y checkpointer nuevos) sobre el mismo Redis
    async with abrir_runtime(core, api, deps=fabricar_dependencias(core)) as rt2:
        decision = {"aprobado": True, "revisor": "ana", "comentario": None}
        aprobado = await rt2.store.transicion(
            job.job_id,
            {"WAITING_APPROVAL"},
            {"status": "PENDING", "resume": decision, "approval_request": None, "attempts": 0},
        )
        assert aprobado is not None
        await rt2.store.encolar(job.job_id)
        await _correr(rt2, job.job_id)
        final = await rt2.store.obtener(job.job_id)
        assert final is not None and final.status == "DONE", final.error
        assert final.result is not None and final.result.accion is not None
        assert final.result.accion.estado == "EJECUTADA" and RESPUESTA in final.result.respuesta
        assert len(await rt2.outbox.listar()) == 1


async def test_rechazo_tras_reinicio_no_envia_nada(settings_redis: tuple[Settings, ApiSettings], limpiar: None) -> None:
    core, api = settings_redis
    async with abrir_runtime(core, api, deps=fabricar_dependencias(core)) as rt1:
        job = await rt1.store.crear(PREGUNTA_CRITICA)
        await _correr(rt1, job.job_id)
    async with abrir_runtime(core, api, deps=fabricar_dependencias(core)) as rt2:
        decision = {"aprobado": False, "revisor": "ana", "comentario": "no"}
        await rt2.store.transicion(
            job.job_id, {"WAITING_APPROVAL"}, {"status": "PENDING", "resume": decision, "attempts": 0}
        )
        await rt2.store.encolar(job.job_id)
        await _correr(rt2, job.job_id)
        final = await rt2.store.obtener(job.job_id)
        assert final is not None and final.status == "DONE", final.error
        assert (
            final.result is not None and final.result.accion is not None and final.result.accion.estado == "RECHAZADA"
        )
        assert await rt2.outbox.listar() == []
