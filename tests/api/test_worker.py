"""Worker: ejecución feliz, fallos → FAILED, timeout, apagado ordenado, concurrencia y recuperación de caídas."""

import asyncio
import dataclasses
import time
from typing import Any

import pytest
from redis.asyncio import Redis

from app.runtime import Runtime
from app.settings import ApiSettings
from app.worker import detener_worker, iniciar_worker, procesar_job
from tests.api.conftest import PREGUNTA, RESPUESTA
from tests.conftest import FakeToolCallingModel


async def test_procesar_job_feliz(runtime: Runtime) -> None:
    job = await runtime.store.crear(PREGUNTA)
    assert await runtime.store.tomar(1) == job.job_id
    await procesar_job(runtime, job.job_id)
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.status == "DONE" and final.attempts == 1
    assert final.result is not None and RESPUESTA in final.result.respuesta
    assert final.result.accion is None  # la pregunta no es crítica
    assert await runtime.store.largo_procesando() == 0


async def test_si_el_agente_falla_el_job_queda_failed_con_el_error(runtime: Runtime) -> None:
    async def roto(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("el proveedor del LLM devolvió 500")
        yield {}  # pragma: no cover  (lo hace un generador async)

    runtime.graph.astream = roto  # type: ignore[method-assign,assignment]
    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)  # NO debe lanzar: el error queda en el job
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.status == "FAILED"
    assert final.error is not None and "RuntimeError" in final.error and "500" in final.error
    assert final.finished_at is not None
    assert await runtime.store.largo_procesando() == 0


async def test_si_el_llm_de_sintesis_falla_el_orquestador_degrada_y_el_job_termina(runtime: Runtime) -> None:
    """Resiliencia del M6 que la API hereda: la síntesis cae a un resumen determinístico, el job NO falla."""

    class Roto(FakeToolCallingModel):
        def _generate(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("el proveedor del LLM devolvió 500")

    from app.graph import construir_grafo_produccion
    from tests.api.conftest import fabricar_dependencias

    deps = dataclasses.replace(fabricar_dependencias(runtime.core), llm_sintesis=Roto())
    runtime.graph = construir_grafo_produccion(deps, runtime.graph.checkpointer, runtime.outbox, runtime.api)  # type: ignore[arg-type]
    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.status == "DONE"


async def test_timeout_marca_failed(runtime: Runtime) -> None:
    corto = dataclasses.replace(runtime.api, job_timeout_s=1)
    runtime.api = corto
    original = runtime.graph.astream

    async def lento(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(30)
        async for x in original(*args, **kwargs):  # pragma: no cover
            yield x

    runtime.graph.astream = lento  # type: ignore[method-assign,assignment]
    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    t0 = time.perf_counter()
    await procesar_job(runtime, job.job_id)
    assert time.perf_counter() - t0 < 5
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.status == "FAILED" and "Timeout" in (final.error or "")


async def test_job_duplicado_o_ya_procesado_se_ignora(runtime: Runtime) -> None:
    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)
    await runtime.store.encolar(job.job_id)  # entrega duplicada de la cola
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)  # ya no está PENDING: no lo ejecuta de nuevo
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.attempts == 1
    assert await runtime.store.largo_procesando() == 0


async def test_cancelacion_devuelve_el_job_a_la_cola(runtime: Runtime) -> None:
    entro = asyncio.Event()

    async def colgado(*args: Any, **kwargs: Any) -> Any:
        entro.set()
        await asyncio.sleep(60)
        yield {}  # pragma: no cover

    runtime.graph.astream = colgado  # type: ignore[method-assign,assignment]
    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    tarea = asyncio.create_task(procesar_job(runtime, job.job_id))
    await entro.wait()
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    final = await runtime.store.obtener(job.job_id)
    assert final is not None and final.status == "PENDING"
    assert await runtime.store.largo_cola() == 1 and await runtime.store.largo_procesando() == 0


async def test_cinco_jobs_concurrentes_terminan_todos(runtime: Runtime) -> None:
    handle = iniciar_worker(runtime)
    try:
        jobs = [await runtime.store.crear(f"{PREGUNTA} (consulta {i})") for i in range(5)]
        async with asyncio.timeout(30):
            while True:
                estados = [(await runtime.store.obtener(j.job_id)) for j in jobs]  # type: ignore[union-attr]
                if all(e is not None and e.status in {"DONE", "FAILED"} for e in estados):
                    break
                await asyncio.sleep(0.05)
    finally:
        await detener_worker(handle, gracia_s=2)
    assert [e.status for e in estados if e] == ["DONE"] * 5
    assert await runtime.store.largo_procesando() == 0


async def test_worker_recupera_un_job_cuyo_worker_murio(runtime: Runtime, redis_falso: Redis) -> None:
    """Un worker muere a mitad de un job (queda en `processing`, sin latido). Otro worker lo termina."""
    from datetime import timedelta

    from app.schemas import ahora

    job = await runtime.store.crear(PREGUNTA)
    await runtime.store.tomar(1)
    await runtime.store.transicion(job.job_id, {"PENDING"}, {"status": "RUNNING", "attempts": 1})
    await redis_falso.delete(runtime.store.clave("lease", job.job_id))
    actual = await runtime.store.obtener(job.job_id)
    assert actual is not None
    viejo = actual.model_copy(update={"updated_at": ahora() - timedelta(seconds=120)})
    await redis_falso.set(runtime.store.clave("job", job.job_id), viejo.model_dump_json())

    handle = iniciar_worker(runtime)  # el recuperador corre al arrancar
    try:
        async with asyncio.timeout(30):
            while (final := await runtime.store.obtener(job.job_id)) and final.status != "DONE":
                await asyncio.sleep(0.05)
    finally:
        await detener_worker(handle, gracia_s=2)
    assert final is not None and final.status == "DONE" and final.attempts == 2


def test_ajustes_de_worker_validan_rangos() -> None:
    with pytest.raises(ValueError, match="WORKER_CONCURRENCY"):
        ApiSettings.from_env(worker_concurrency=0)
    with pytest.raises(ValueError, match="CHECKPOINTER"):
        ApiSettings.from_env(checkpointer="sqlite")
