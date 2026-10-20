"""Contratos de la API (entrada/salida HTTP) y registro del job que se guarda en Redis."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas import ResultadoAccion

JobStatus = Literal["PENDING", "RUNNING", "WAITING_APPROVAL", "DONE", "FAILED"]
ESTADOS_TERMINALES: frozenset[str] = frozenset({"DONE", "FAILED"})


def ahora() -> datetime:
    return datetime.now(UTC)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=3, max_length=2000, description="Pregunta para el orquestador multi-agente.")


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    aprobado: bool
    revisor: str | None = Field(default=None, max_length=80)
    comentario: str | None = Field(default=None, max_length=500)


class JobResult(BaseModel):
    """Resultado final de un job (lo escribe el worker al terminar el grafo)."""

    respuesta: str
    estado_final: str | None
    motivo_escalamiento: str | None
    pasos: int
    intentos: dict[str, int]
    accion: ResultadoAccion | None = None


class JobRecord(BaseModel):
    """Lo que se persiste en Redis por cada job. `resume` es interno (decisión humana pendiente de reanudar)."""

    job_id: str
    status: JobStatus
    query: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = 0
    progress: list[str] = Field(default_factory=list)  # nodos del grafo ya ejecutados
    approval_request: dict[str, Any] | None = None  # qué debe aprobar el humano (solo en WAITING_APPROVAL)
    resume: dict[str, Any] | None = None  # decisión humana que el worker usará para reanudar el grafo
    result: JobResult | None = None
    error: str | None = None


class TaskAccepted(BaseModel):
    job_id: str
    status: JobStatus


class TaskView(BaseModel):
    """Vista pública de un job (sin campos internos)."""

    job_id: str
    status: JobStatus
    query: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: list[str]
    approval_request: dict[str, Any] | None
    result: JobResult | None
    error: str | None

    @classmethod
    def desde(cls, job: JobRecord) -> "TaskView":
        return cls(**job.model_dump(exclude={"resume", "attempts"}))
