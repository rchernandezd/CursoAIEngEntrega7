"""Estado de los jobs en Redis + cola confiable.

Claves (todas con el prefijo KEY_PREFIX):
  job:{id}      JSON del JobRecord (con TTL; se refresca en cada escritura)
  queue         lista de job_id pendientes (RPUSH para encolar, BLMOVE para tomar)
  processing    job_id TOMADOS por algún worker (cola "confiable": un job no se pierde si el worker muere)
  lease:{id}    latido del worker que ejecuta el job (SET con TTL corto, se renueva mientras trabaja)

Reglas que evitan los errores típicos (Clase 7, "Errores comunes"):
  - El estado PENDING se escribe ANTES de encolar: el cliente nunca recibe un 404 por una carrera con el worker.
  - Toda transición de estado es atómica (WATCH/MULTI): dos aprobaciones simultáneas no reanudan el grafo 2 veces.
  - Un worker que muere deja el job en `processing` sin latido: `recuperar_huerfanos` lo reencola o lo marca FAILED,
    así el cliente nunca queda haciendo polling para siempre.
"""

import uuid
from collections.abc import Callable, Collection
from datetime import timedelta
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.schemas import ESTADOS_TERMINALES, JobRecord, ahora

type Cambios = dict[str, Any] | Callable[[JobRecord], dict[str, Any]]


class ColaLlenaError(RuntimeError):
    """Hay demasiados jobs pendientes (contrapresión)."""


class JobStore:
    def __init__(self, redis: Redis, *, prefix: str, ttl_s: int, queue_max_len: int, lease_ttl_s: int) -> None:
        self.redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._queue_max_len = queue_max_len
        self._lease_ttl_s = lease_ttl_s

    # ------------------------------------------------------------------ claves
    def clave(self, *partes: str) -> str:
        return ":".join((self._prefix, *partes))

    @property
    def cola(self) -> str:
        return self.clave("queue")

    @property
    def procesando(self) -> str:
        return self.clave("processing")

    # ------------------------------------------------------------------ CRUD
    async def _guardar(self, job: JobRecord) -> None:
        await self.redis.set(self.clave("job", job.job_id), job.model_dump_json(), ex=self._ttl_s)

    async def crear(self, query: str) -> JobRecord:
        """Registra el job como PENDING y recién entonces lo encola."""
        if await self.redis.llen(self.cola) >= self._queue_max_len:
            raise ColaLlenaError(f"Hay {self._queue_max_len} jobs pendientes; reintentá más tarde.")
        t = ahora()
        job = JobRecord(job_id=str(uuid.uuid4()), status="PENDING", query=query, created_at=t, updated_at=t)
        await self._guardar(job)  # 1) el estado existe antes de que el worker pueda verlo
        await self.redis.rpush(self.cola, job.job_id)  # 2) recién ahora se encola
        return job

    async def obtener(self, job_id: str) -> JobRecord | None:
        raw = await self.redis.get(self.clave("job", job_id))
        return JobRecord.model_validate_json(raw) if raw else None

    async def transicion(self, job_id: str, desde: Collection[str], cambios: Cambios) -> JobRecord | None:
        """Cambia el job SOLO si su estado actual está en `desde` (compare-and-set atómico).

        Devuelve el job nuevo, o None si no existe o su estado no es el esperado. `cambios` puede ser una función
        del job actual (p. ej. `lambda j: {"attempts": j.attempts + 1}`).
        """
        clave = self.clave("job", job_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(clave)
                    raw = await pipe.get(clave)
                    if raw is None:
                        await pipe.unwatch()  # type: ignore[no-untyped-call]
                        return None
                    actual = JobRecord.model_validate_json(raw)
                    if actual.status not in desde:
                        await pipe.unwatch()  # type: ignore[no-untyped-call]
                        return None
                    delta = cambios(actual) if callable(cambios) else cambios
                    nuevo = JobRecord.model_validate({**actual.model_dump(), **delta, "updated_at": ahora()})
                    pipe.multi()  # type: ignore[no-untyped-call]
                    pipe.set(clave, nuevo.model_dump_json(), ex=self._ttl_s)
                    await pipe.execute()
                    return nuevo
                except WatchError:  # alguien modificó el job entre el GET y el EXEC: reintentar con el estado nuevo
                    continue

    # ------------------------------------------------------------------ cola
    async def encolar(self, job_id: str) -> None:
        await self.redis.rpush(self.cola, job_id)

    async def tomar(self, timeout_s: int) -> str | None:
        """Saca el próximo job de la cola y lo pasa a `processing` en un solo paso atómico (BLMOVE)."""
        job_id = cast(str | None, await self.redis.blmove(self.cola, self.procesando, timeout_s, "LEFT", "RIGHT"))
        if job_id is not None:
            await self.renovar_latido(job_id)
        return job_id

    async def renovar_latido(self, job_id: str) -> None:
        await self.redis.set(self.clave("lease", job_id), "1", ex=self._lease_ttl_s)

    async def confirmar(self, job_id: str) -> None:
        """El worker terminó con el job (cualquiera sea el resultado): sale de `processing`."""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrem(self.procesando, 0, job_id)
            pipe.delete(self.clave("lease", job_id))
            await pipe.execute()

    async def devolver_a_la_cola(self, job_id: str) -> None:
        """Mueve el job de `processing` otra vez a la cola (apagado ordenado del worker)."""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrem(self.procesando, 0, job_id)
            pipe.delete(self.clave("lease", job_id))
            pipe.rpush(self.cola, job_id)
            await pipe.execute()

    async def largo_cola(self) -> int:
        return int(await self.redis.llen(self.cola))

    async def largo_procesando(self) -> int:
        return int(await self.redis.llen(self.procesando))

    async def recuperar_huerfanos(self, max_attempts: int) -> list[str]:
        """Jobs tomados por un worker que dejó de dar señales de vida: reencola o marca FAILED. Devuelve los ids."""
        recuperados: list[str] = []
        ids = cast(list[str], await self.redis.lrange(self.procesando, 0, -1))
        for job_id in dict.fromkeys(ids):
            if await self.redis.exists(self.clave("lease", job_id)):
                continue  # su worker sigue vivo
            job = await self.obtener(job_id)
            if job is None or job.status in ESTADOS_TERMINALES or job.status == "WAITING_APPROVAL":
                await self.confirmar(job_id)  # nada que recuperar
                continue
            if ahora() - job.updated_at < timedelta(seconds=self._lease_ttl_s):
                continue  # recién tomado: dar tiempo a que el worker escriba su primer latido
            if job.attempts >= max_attempts:
                await self.transicion(
                    job_id,
                    {"PENDING", "RUNNING"},
                    {
                        "status": "FAILED",
                        "error": f"WorkerInterrumpido: el worker murió {job.attempts} veces ejecutando este job.",
                        "finished_at": ahora(),
                    },
                )
                await self.confirmar(job_id)
            elif await self.transicion(job_id, {"PENDING", "RUNNING"}, {"status": "PENDING", "started_at": None}):
                await self.devolver_a_la_cola(job_id)
            else:
                continue
            recuperados.append(job_id)
        return recuperados
