"""Endpoints HTTP: 202 inmediato, polling, 404, validación, autenticación, contrapresión y /health."""

import dataclasses
import time
from collections.abc import Callable
from typing import Any

from app.settings import ApiSettings
from tests.api.conftest import PREGUNTA, RESPUESTA, esperar_estado

Abrir = Callable[..., Any]


async def test_post_tasks_responde_202_al_instante_y_no_ejecuta_el_agente(
    abrir_cliente: Abrir, api_settings: ApiSettings
) -> None:
    async with abrir_cliente(api_settings, worker=False) as (cliente, rt):
        t0 = time.perf_counter()
        r = await cliente.post("/tasks", json={"query": PREGUNTA})
        assert time.perf_counter() - t0 < 1.0
        assert r.status_code == 202
        cuerpo = r.json()
        assert cuerpo["status"] == "PENDING" and r.headers["location"] == f"/tasks/{cuerpo['job_id']}"
        # sin worker nadie lo toma: sigue PENDING y en la cola
        assert (await cliente.get(f"/tasks/{cuerpo['job_id']}")).json()["status"] == "PENDING"
        assert await rt.store.largo_cola() == 1


async def test_flujo_completo_pending_a_done(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, _):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA})).json()["job_id"]
        final = await esperar_estado(cliente, job_id, {"DONE", "FAILED"})
    assert final["status"] == "DONE"
    resultado: Any = final["result"]
    assert RESPUESTA in resultado["respuesta"]
    assert final["progress"][0] == "supervisor" and "sintesis" in final["progress"]
    assert final["started_at"] is not None and final["finished_at"] is not None
    assert "resume" not in final and "attempts" not in final  # campos internos fuera de la vista pública


async def test_job_inexistente_devuelve_404(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings) as (cliente, _):
        r = await cliente.get("/tasks/no-existe")
    assert r.status_code == 404


async def test_body_invalido_devuelve_422(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings) as (cliente, _):
        assert (await cliente.post("/tasks", json={})).status_code == 422
        assert (await cliente.post("/tasks", json={"query": "ab"})).status_code == 422
        assert (await cliente.post("/tasks", json={"query": "x" * 2001})).status_code == 422
        assert (await cliente.post("/tasks", json={"query": "pregunta válida", "extra": 1})).status_code == 422


async def test_api_key_requerida_si_esta_configurada(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    con_clave = dataclasses.replace(api_settings, api_key="secreta")
    async with abrir_cliente(con_clave) as (cliente, _):
        assert (await cliente.post("/tasks", json={"query": PREGUNTA})).status_code == 401
        mala = await cliente.post("/tasks", json={"query": PREGUNTA}, headers={"X-API-Key": "otra"})
        assert mala.status_code == 401
        buena = await cliente.post("/tasks", json={"query": PREGUNTA}, headers={"X-API-Key": "secreta"})
        assert buena.status_code == 202
        assert (await cliente.get("/health")).status_code == 200  # /health no exige clave (healthcheck de Docker)


async def test_api_key_con_caracteres_no_ascii_devuelve_401_y_no_500(
    abrir_cliente: Abrir, api_settings: ApiSettings
) -> None:
    """secrets.compare_digest(str, str) lanza TypeError con no-ASCII: un header hostil no puede producir un 500."""
    con_clave = dataclasses.replace(api_settings, api_key="secreta")
    async with abrir_cliente(con_clave) as (cliente, _):
        r = await cliente.post(
            "/tasks", json={"query": PREGUNTA}, headers={"X-API-Key": "contraseña".encode("latin-1")}
        )
    assert r.status_code == 401


async def test_cola_llena_devuelve_429(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    chica = dataclasses.replace(api_settings, queue_max_len=1)
    async with abrir_cliente(chica) as (cliente, _):
        assert (await cliente.post("/tasks", json={"query": PREGUNTA})).status_code == 202
        r = await cliente.post("/tasks", json={"query": PREGUNTA})
    assert r.status_code == 429 and r.headers["retry-after"] == "10"


async def test_health_reporta_redis_y_cola(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings) as (cliente, _):
        await cliente.post("/tasks", json={"query": PREGUNTA})
        r = await cliente.get("/health")
    assert r.status_code == 200
    assert r.json() | {"cola": 1} == r.json() and r.json()["redis"] == "ok" and r.json()["status"] == "ok"


async def test_health_degradado_si_redis_falla(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings) as (cliente, rt):

        async def caido() -> bool:
            raise ConnectionError("redis caído")

        rt.redis.ping = caido  # type: ignore[method-assign]
        r = await cliente.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded"


async def test_openapi_documenta_los_endpoints(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings) as (cliente, _):
        rutas = (await cliente.get("/openapi.json")).json()["paths"]
    assert {"/tasks", "/tasks/{job_id}", "/tasks/{job_id}/approve", "/health"} <= set(rutas)
