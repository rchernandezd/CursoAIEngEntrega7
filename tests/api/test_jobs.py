"""JobStore: estado en Redis, transiciones atómicas, cola confiable, contrapresión y recuperación de huérfanos."""

import asyncio
from datetime import timedelta

import pytest
from redis.asyncio import Redis

from app.jobs import ColaLlenaError, JobStore
from app.schemas import ahora


def nuevo_store(redis: Redis, *, queue_max_len: int = 100, lease_ttl_s: int = 5) -> JobStore:
    return JobStore(redis, prefix="t", ttl_s=3600, queue_max_len=queue_max_len, lease_ttl_s=lease_ttl_s)


async def test_crear_registra_pending_y_encola(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job = await store.crear("¿cuántos días de vacaciones?")
    assert job.status == "PENDING"
    assert (await store.obtener(job.job_id)) == job  # el estado existe (no hay 404 posible)
    assert await store.largo_cola() == 1
    assert await redis_falso.ttl(store.clave("job", job.job_id)) > 0  # los jobs expiran


async def test_obtener_inexistente_devuelve_none(redis_falso: Redis) -> None:
    assert await nuevo_store(redis_falso).obtener("no-existe") is None


async def test_transicion_solo_desde_el_estado_esperado(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job = await store.crear("pregunta de prueba")
    assert await store.transicion(job.job_id, {"RUNNING"}, {"status": "DONE"}) is None  # estaba PENDING
    pasado = await store.transicion(job.job_id, {"PENDING"}, {"status": "RUNNING"})
    assert pasado is not None and pasado.status == "RUNNING"
    assert pasado.updated_at >= job.updated_at


async def test_transicion_acepta_funcion_del_job_actual(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job = await store.crear("pregunta de prueba")
    nuevo = await store.transicion(job.job_id, {"PENDING"}, lambda j: {"attempts": j.attempts + 1})
    assert nuevo is not None and nuevo.attempts == 1


async def test_dos_transiciones_simultaneas_solo_una_gana(redis_falso: Redis) -> None:
    """Dos aprobaciones al mismo tiempo no pueden reanudar el grafo dos veces (compare-and-set)."""
    store = nuevo_store(redis_falso)
    job = await store.crear("pregunta de prueba")
    await store.transicion(job.job_id, {"PENDING"}, {"status": "WAITING_APPROVAL"})
    resultados = await asyncio.gather(
        *(store.transicion(job.job_id, {"WAITING_APPROVAL"}, {"status": "PENDING"}) for _ in range(10))
    )
    assert sum(r is not None for r in resultados) == 1


async def test_contrapresion_cuando_la_cola_esta_llena(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso, queue_max_len=2)
    await store.crear("primera pregunta")
    await store.crear("segunda pregunta")
    with pytest.raises(ColaLlenaError):
        await store.crear("tercera pregunta")


async def test_tomar_mueve_el_job_a_processing_con_latido(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job = await store.crear("pregunta de prueba")
    assert await store.tomar(1) == job.job_id
    assert await store.largo_cola() == 0
    assert await store.largo_procesando() == 1
    assert await redis_falso.exists(store.clave("lease", job.job_id))
    await store.confirmar(job.job_id)
    assert await store.largo_procesando() == 0
    assert not await redis_falso.exists(store.clave("lease", job.job_id))


async def test_tomar_con_cola_vacia_devuelve_none(redis_falso: Redis) -> None:
    assert await nuevo_store(redis_falso).tomar(1) is None


async def test_orden_fifo(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    a, b = await store.crear("pregunta A"), await store.crear("pregunta B")
    assert await store.tomar(1) == a.job_id
    assert await store.tomar(1) == b.job_id


async def _huerfano(store: JobStore, redis: Redis, *, attempts: int) -> str:
    """Job RUNNING cuyo worker murió: está en `processing`, sin latido y con updated_at viejo."""
    job = await store.crear("pregunta de prueba")
    await store.tomar(1)
    await store.transicion(job.job_id, {"PENDING"}, {"status": "RUNNING", "attempts": attempts})
    await redis.delete(store.clave("lease", job.job_id))
    actual = await store.obtener(job.job_id)
    assert actual is not None
    viejo = actual.model_copy(update={"updated_at": ahora() - timedelta(seconds=60)})
    await redis.set(store.clave("job", job.job_id), viejo.model_dump_json())  # `transicion` siempre pisa updated_at
    return job.job_id


async def test_huerfano_se_reencola(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job_id = await _huerfano(store, redis_falso, attempts=1)
    assert await store.recuperar_huerfanos(max_attempts=3) == [job_id]
    job = await store.obtener(job_id)
    assert job is not None and job.status == "PENDING"
    assert await store.largo_cola() == 1 and await store.largo_procesando() == 0


async def test_huerfano_sin_intentos_restantes_queda_failed(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job_id = await _huerfano(store, redis_falso, attempts=3)
    assert await store.recuperar_huerfanos(max_attempts=3) == [job_id]
    job = await store.obtener(job_id)
    assert job is not None and job.status == "FAILED" and "WorkerInterrumpido" in (job.error or "")
    assert await store.largo_cola() == 0 and await store.largo_procesando() == 0


async def test_job_con_latido_vivo_no_se_recupera(redis_falso: Redis) -> None:
    store = nuevo_store(redis_falso)
    job = await store.crear("pregunta de prueba")
    await store.tomar(1)  # tomar() escribe el latido
    await store.transicion(job.job_id, {"PENDING"}, {"status": "RUNNING"})
    assert await store.recuperar_huerfanos(max_attempts=3) == []
