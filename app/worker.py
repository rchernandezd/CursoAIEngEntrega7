"""Worker: saca jobs de la cola de Redis, ejecuta el grafo y deja el estado en Redis (PENDING/RUNNING/.../DONE/FAILED).

    python -m app.worker        # proceso aparte (recomendado; escala con más procesos)
    WORKER_EMBEDDED=true        # o dentro del proceso de la API (desarrollo)

Reglas de oro:
  - NINGUNA excepción puede escapar de `procesar_job` sin actualizar el estado: si el agente falla, el job queda FAILED
    con el error (si no, el cliente haría polling para siempre).
  - Nada bloquea el event loop: el grafo es async de punta a punta (ainvoke/astream), Redis es redis.asyncio y el
    exportador de trazas corre en su propio hilo.
  - Si el worker muere, el job queda en `processing` sin latido y otro worker lo recupera
    (JobStore.recuperar_huerfanos).
"""

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from app.jobs import JobStore
from app.observability import init_observability, trazar_job
from app.runtime import Runtime, abrir_runtime
from app.schemas import JobRecord, JobResult, ahora
from app.settings import ApiSettings
from config import Settings
from logging_config import configurar_logging, forzar_utf8_en_consola
from state import estado_inicial

logger = logging.getLogger(__name__)

ESPERA_COLA_S = 2  # cuánto bloquea cada consumidor esperando un job (acota el tiempo de apagado)
GRACIA_APAGADO_S = 10.0  # al apagar, los jobs en curso tienen este tiempo para terminar antes de devolverse a la cola


def _solicitud_de_aprobacion(snapshot: Any) -> dict[str, Any]:
    """Payload que el nodo HITL pasó a `interrupt(...)`: lo que el humano debe revisar."""
    for tarea in snapshot.tasks:
        for pendiente in tarea.interrupts:
            valor = pendiente.value
            return dict(valor) if isinstance(valor, dict) else {"detalle": str(valor)}
    return {"detalle": "El grafo quedó pausado sin payload de aprobación."}


def _resultado(values: dict[str, Any]) -> JobResult:
    return JobResult(
        respuesta=values.get("respuesta_final") or "",
        estado_final=values.get("estado_final"),
        motivo_escalamiento=values.get("motivo_escalamiento"),
        pasos=values.get("pasos", 0),
        intentos=values.get("intentos", {}),
        accion=values.get("accion_resultado"),
    )


async def _ejecutar_grafo(rt: Runtime, job: JobRecord) -> None:
    """Corre (o reanuda) el grafo. Termina el job en DONE o lo deja en WAITING_APPROVAL si el grafo se pausó."""
    store, config = rt.store, rt.run_config(job.job_id)
    # Reanudación tras la aprobación humana: Command(resume=...) sobre el MISMO thread_id (estado leído de Redis).
    entrada: Any = Command(resume=job.resume) if job.resume is not None else estado_inicial(job.query)
    progreso = list(job.progress)
    with trazar_job(job.job_id, job.query) as registrar_salida:
        async for cambio in rt.graph.astream(entrada, config, stream_mode="updates"):
            nuevos = [nodo for nodo in cambio if nodo != "__interrupt__"]
            if nuevos:
                progreso.extend(nuevos)
                await store.transicion(job.job_id, {"RUNNING"}, {"progress": list(progreso)})
        snapshot = await rt.graph.aget_state(config)
        if snapshot.next:  # el grafo se pausó en un interrupt(): hay que esperar a un humano
            solicitud = _solicitud_de_aprobacion(snapshot)
            await store.transicion(
                job.job_id,
                {"RUNNING"},
                {"status": "WAITING_APPROVAL", "approval_request": solicitud, "resume": None, "progress": progreso},
            )
            registrar_salida("(pausado: esperando aprobación humana)")
            return
        resultado = _resultado(snapshot.values)
        guardado = await store.transicion(
            job.job_id,
            {"RUNNING"},
            {"status": "DONE", "result": resultado, "resume": None, "progress": progreso, "finished_at": ahora()},
        )
        if guardado is None:
            logger.warning("job %s terminó pero ya no estaba RUNNING (¿expiró?); resultado descartado", job.job_id)
        registrar_salida(resultado.respuesta)


async def _latido(store: JobStore, job_id: str, cada_s: float) -> None:
    while True:
        await asyncio.sleep(cada_s)
        await store.renovar_latido(job_id)


async def procesar_job(rt: Runtime, job_id: str) -> None:
    """Ejecuta UN job de punta a punta. Nunca lanza excepciones (salvo la cancelación por apagado)."""
    store = rt.store
    job = await store.transicion(
        job_id,
        {"PENDING"},
        lambda j: {"status": "RUNNING", "attempts": j.attempts + 1, "started_at": ahora(), "error": None},
    )
    if job is None:  # duplicado, expirado o ya procesado: no hay nada que hacer
        logger.warning("job %s ignorado: no está PENDING", job_id)
        await store.confirmar(job_id)
        return
    latido = asyncio.create_task(_latido(store, job_id, rt.api.lease_ttl_s / 3))
    try:
        async with asyncio.timeout(rt.api.job_timeout_s):
            await _ejecutar_grafo(rt, job)
    except asyncio.CancelledError:
        # Apagado del worker: no es un fallo del job. Se devuelve a la cola para que otro worker lo retome.
        await asyncio.shield(_devolver(store, job_id))
        raise
    except Exception as exc:  # incluye TimeoutError, GraphRecursionError, fallos del LLM y de Redis
        logger.exception("job %s falló", job_id)
        detalle = (
            f"Timeout: el job superó JOB_TIMEOUT_S={rt.api.job_timeout_s:g}s"
            if isinstance(exc, TimeoutError)
            else f"{type(exc).__name__}: {exc}"
        )
        await _marcar_fallido(store, job_id, detalle)
    finally:
        latido.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await latido
    await store.confirmar(job_id)


async def _marcar_fallido(store: JobStore, job_id: str, detalle: str) -> None:
    try:
        await store.transicion(
            job_id,
            {"RUNNING", "PENDING"},
            {"status": "FAILED", "error": detalle, "resume": None, "finished_at": ahora()},
        )
    except Exception:  # Redis caído: lo recogerá recuperar_huerfanos cuando el latido expire
        logger.exception("no se pudo marcar FAILED el job %s", job_id)


async def _devolver(store: JobStore, job_id: str) -> None:
    await store.transicion(job_id, {"RUNNING"}, {"status": "PENDING", "started_at": None})
    await store.devolver_a_la_cola(job_id)


async def consumidor(rt: Runtime, parar: asyncio.Event) -> None:
    while not parar.is_set():
        try:
            job_id = await rt.store.tomar(ESPERA_COLA_S)
        except Exception:  # Redis caído o reiniciándose: reintentar con pausa, sin matar al consumidor
            logger.exception("error al tomar un job de la cola")
            await asyncio.sleep(1)
            continue
        if job_id is not None:
            await procesar_job(rt, job_id)


async def recuperador(rt: Runtime, parar: asyncio.Event) -> None:
    """Al arrancar y luego periódicamente: reencola los jobs de workers que murieron."""
    while not parar.is_set():
        try:
            recuperados = await rt.store.recuperar_huerfanos(rt.api.max_attempts)
            if recuperados:
                logger.warning("jobs huérfanos recuperados: %s", recuperados)
        except Exception:
            logger.exception("falló la recuperación de huérfanos")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(parar.wait(), timeout=rt.api.lease_ttl_s)


@dataclass(slots=True)
class WorkerHandle:
    tareas: list[asyncio.Task[None]]
    parar: asyncio.Event


def iniciar_worker(rt: Runtime) -> WorkerHandle:
    parar = asyncio.Event()
    tareas = [
        asyncio.create_task(consumidor(rt, parar), name=f"consumidor-{i}") for i in range(rt.api.worker_concurrency)
    ]
    tareas.append(asyncio.create_task(recuperador(rt, parar), name="recuperador"))
    logger.info("Worker iniciado: %d consumidores", rt.api.worker_concurrency)
    return WorkerHandle(tareas, parar)


async def detener_worker(handle: WorkerHandle, *, gracia_s: float = GRACIA_APAGADO_S) -> None:
    handle.parar.set()
    _, pendientes = await asyncio.wait(handle.tareas, timeout=gracia_s)
    for tarea in pendientes:  # los jobs que no terminaron vuelven a la cola (ver procesar_job)
        tarea.cancel()
    await asyncio.gather(*pendientes, return_exceptions=True)
    logger.info("Worker detenido")


async def _principal() -> None:
    core, api = Settings.from_env(), ApiSettings.from_env()
    configurar_logging(core.log_level)
    apagar_trazas = init_observability(api)
    try:
        async with abrir_runtime(core, api) as rt:
            handle = iniciar_worker(rt)
            detener = asyncio.Event()
            loop = asyncio.get_running_loop()
            for senal in (signal.SIGINT, signal.SIGTERM):
                # Windows no implementa add_signal_handler: allí Ctrl+C llega como KeyboardInterrupt
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(senal, detener.set)
            try:
                await detener.wait()
            finally:
                await detener_worker(handle)
    finally:
        apagar_trazas()


if __name__ == "__main__":
    forzar_utf8_en_consola()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_principal())
