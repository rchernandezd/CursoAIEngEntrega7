"""API REST (FastAPI) del orquestador multi-agente.

    POST /tasks               encola la tarea y devuelve un job_id al instante (202)        → no bloquea el event loop
    GET  /tasks/{id}          estado del job: PENDING | RUNNING | WAITING_APPROVAL | DONE | FAILED
    POST /tasks/{id}/approve  decisión humana para una tarea crítica en WAITING_APPROVAL      (ver app/hitl.py)
    GET  /health              Redis, cola y configuración activa

Arranque:  uvicorn app.main:app --port 8000      (con WORKER_EMBEDDED=true el worker corre en el mismo proceso;
                                                   en producción: WORKER_EMBEDDED=false y `python -m app.worker`)
"""

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse

from app import hitl
from app.deps import AutenticadoDep, RuntimeDep
from app.jobs import ColaLlenaError
from app.observability import init_observability
from app.runtime import Runtime, abrir_runtime
from app.schemas import TaskAccepted, TaskCreate, TaskView
from app.settings import ApiSettings
from app.worker import detener_worker, iniciar_worker
from config import Settings
from exceptions import OrquestadorError
from logging_config import configurar_logging

logger = logging.getLogger(__name__)

RuntimeFactory = Callable[[], AbstractAsyncContextManager[Runtime]]


def create_app(
    *, core: Settings | None = None, api: ApiSettings | None = None, runtime_factory: RuntimeFactory | None = None
) -> FastAPI:
    """Fábrica de la app. Los tests inyectan `runtime_factory` (Redis y LLM falsos); en producción se lee el .env."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c, a = core or Settings.from_env(), api or ApiSettings.from_env()
        configurar_logging(c.log_level)
        apagar_trazas = init_observability(a)
        factory = runtime_factory or (lambda: abrir_runtime(c, a))
        try:
            async with factory() as rt:
                app.state.rt = rt
                worker = iniciar_worker(rt) if rt.api.worker_embedded else None
                try:
                    yield
                finally:
                    if worker is not None:
                        await detener_worker(worker)
        finally:
            await asyncio.to_thread(apagar_trazas)  # vaciar el buffer de trazas sin bloquear el loop

    # Las versiones recientes de FastAPI emiten sus propios spans HTTP (OpenTelemetry). Con Phoenix activo cada
    # GET /tasks/{id} del polling sería una "traza" más y falsearía el P50/P99 del proyecto. Queremos que el dashboard
    # muestre solo el trabajo del agente (una traza por job), así que se apaga el tracing HTTP nativo.
    extra: dict[str, object] = {}
    if "telemetry" in inspect.signature(FastAPI.__init__).parameters:
        extra["telemetry"] = {"tracing": False}

    app = FastAPI(
        title="API del orquestador multi-agente (Pre-entrega 7)",
        version="1.0.0",
        description="Endpoints asíncronos, Redis, observabilidad y Human-in-the-loop sobre el orquestador del M6.",
        lifespan=lifespan,
        **extra,  # type: ignore[arg-type]
    )

    @app.exception_handler(OrquestadorError)
    async def _errores_del_dominio(_: object, exc: OrquestadorError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.post("/tasks", status_code=202, response_model=TaskAccepted, tags=["Tareas"], dependencies=[AutenticadoDep])
    async def crear_tarea(body: TaskCreate, response: Response, rt: RuntimeDep) -> TaskAccepted:
        """Registra la tarea (PENDING), la encola y responde de inmediato. El trabajo lo hace el worker."""
        try:
            job = await rt.store.crear(body.query)
        except ColaLlenaError as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "10"}) from exc
        response.headers["Location"] = f"/tasks/{job.job_id}"
        return TaskAccepted(job_id=job.job_id, status=job.status)

    @app.get("/tasks/{job_id}", response_model=TaskView, tags=["Tareas"], dependencies=[AutenticadoDep])
    async def estado_tarea(job_id: str, rt: RuntimeDep) -> TaskView:
        job = await rt.store.obtener(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No existe el job {job_id} (o ya expiró).")
        return TaskView.desde(job)

    @app.get("/health", tags=["Operación"])
    async def health(rt: RuntimeDep) -> JSONResponse:
        try:
            await rt.redis.ping()
        except Exception as exc:
            return JSONResponse(status_code=503, content={"status": "degraded", "redis": f"{type(exc).__name__}"})
        return JSONResponse(
            content={
                "status": "ok",
                "redis": "ok",
                "cola": await rt.store.largo_cola(),
                "procesando": await rt.store.largo_procesando(),
                "worker_embedded": rt.api.worker_embedded,
                "checkpointer": rt.api.checkpointer,
                "observability": rt.api.observability,
            }
        )

    app.include_router(hitl.router)
    return app


app = create_app()
