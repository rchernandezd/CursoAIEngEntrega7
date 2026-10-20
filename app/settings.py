"""Settings de la capa de producción (API + worker + observabilidad). Los del orquestador siguen en config.py."""

import os
from dataclasses import dataclass, field
from typing import Literal, Self, get_args

from dotenv import load_dotenv

Checkpointer = Literal["redis", "memory"]
Observabilidad = Literal["phoenix", "langsmith", "none"]


@dataclass(frozen=True, slots=True)
class ApiSettings:
    redis_url: str
    key_prefix: str  # prefijo de TODAS las claves de Redis de la app (permite compartir una instancia)
    job_ttl_s: int  # los registros de jobs expiran (se refresca en cada escritura)
    queue_max_len: int  # contrapresión: más pendientes que esto → 429
    worker_concurrency: int  # jobs simultáneos por proceso worker
    worker_embedded: bool  # True: el worker corre dentro del proceso de la API (dev); False: proceso aparte
    job_timeout_s: float  # tope de duración de UN job (si se pasa → FAILED)
    lease_ttl_s: int  # latido del worker: sin latido, el job se considera huérfano y se reencola
    max_attempts: int  # veces que se reintenta un job huérfano antes de marcarlo FAILED
    checkpointer: Checkpointer
    checkpoint_ttl_min: int  # expiración de los checkpoints de LangGraph en Redis (minutos)
    observability: Observabilidad
    phoenix_endpoint: str  # base del colector de Phoenix (sin /v1/traces)
    phoenix_project: str
    langsmith_project: str
    langsmith_api_key: str = field(repr=False)
    api_key: str = field(repr=False)  # "" = API sin autenticación (solo desarrollo)
    rrhh_destinatario: str  # destinatario de la acción simulada notificar_rrhh

    def __post_init__(self) -> None:
        if self.checkpointer not in get_args(Checkpointer):
            raise ValueError(f"CHECKPOINTER debe ser uno de {get_args(Checkpointer)}")
        if self.observability not in get_args(Observabilidad):
            raise ValueError(f"OBSERVABILITY debe ser uno de {get_args(Observabilidad)}")
        if not self.redis_url.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL debe empezar con redis:// , rediss:// o unix://")
        if not 1 <= self.worker_concurrency <= 50:
            raise ValueError("WORKER_CONCURRENCY debe estar entre 1 y 50")
        if not 1 <= self.job_timeout_s <= 3600:
            raise ValueError("JOB_TIMEOUT_S debe estar entre 1 y 3600")
        if not 5 <= self.lease_ttl_s <= 600:
            raise ValueError("LEASE_TTL_S debe estar entre 5 y 600")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("MAX_ATTEMPTS debe estar entre 1 y 10")
        if self.queue_max_len < 1 or self.job_ttl_s < 60 or self.checkpoint_ttl_min < 1:
            raise ValueError("QUEUE_MAX_LEN >= 1, JOB_TTL_S >= 60 y CHECKPOINT_TTL_MIN >= 1")

    @classmethod
    def from_env(cls, **overrides: object) -> Self:
        load_dotenv()
        env = os.getenv
        values: dict[str, object] = dict(
            redis_url=env("REDIS_URL", "redis://localhost:6379/0"),
            key_prefix=env("KEY_PREFIX", "agentapi"),
            job_ttl_s=int(env("JOB_TTL_S", "86400")),
            queue_max_len=int(env("QUEUE_MAX_LEN", "1000")),
            worker_concurrency=int(env("WORKER_CONCURRENCY", "5")),
            worker_embedded=env("WORKER_EMBEDDED", "true").strip().lower() in ("1", "true", "yes"),
            job_timeout_s=float(env("JOB_TIMEOUT_S", "300")),
            lease_ttl_s=int(env("LEASE_TTL_S", "30")),
            max_attempts=int(env("MAX_ATTEMPTS", "3")),
            checkpointer=env("CHECKPOINTER", "redis").strip().lower(),
            checkpoint_ttl_min=int(env("CHECKPOINT_TTL_MIN", "1440")),
            observability=env("OBSERVABILITY", "phoenix").strip().lower(),
            phoenix_endpoint=env("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").rstrip("/"),
            phoenix_project=env("PHOENIX_PROJECT_NAME", "agente-m7"),
            langsmith_project=env("LANGSMITH_PROJECT", "agente-m7"),
            langsmith_api_key=env("LANGSMITH_API_KEY", ""),
            api_key=env("API_KEY", ""),
            rrhh_destinatario=env("RRHH_DESTINATARIO", "rrhh@techcorp.example"),
        )
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]  # dict[str, object] → kwargs; validado en __post_init__
