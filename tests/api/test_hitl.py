"""Human-in-the-loop: detección de tareas críticas, pausa, aprobación/rechazo vía HTTP, idempotencia y reanudación."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from app.hitl import detectar_accion_critica, proponer_accion
from app.runtime import Runtime
from app.settings import ApiSettings
from app.worker import procesar_job
from state import estado_inicial
from tests.api.conftest import PREGUNTA, PREGUNTA_CRITICA, RESPUESTA, esperar_estado

Abrir = Callable[..., Any]


@pytest.mark.parametrize(
    "pregunta",
    [
        "Envía el resultado por correo a RRHH.",
        "Mándale un email a Recursos Humanos con el saldo",
        "notifica a RR HH por mail",
        "ENVIA el calculo al correo de finanzas",
    ],
)
def test_detecta_tareas_criticas(pregunta: str) -> None:
    assert detectar_accion_critica(pregunta)


@pytest.mark.parametrize(
    "pregunta",
    [
        PREGUNTA,
        "¿Cuántos días de vacaciones corresponden a 12 años de antigüedad?",
        "¿Cuál es la política de correo corporativo?",  # menciona correo pero no pide una acción
    ],
)
def test_no_marca_como_critica_una_consulta_normal(pregunta: str) -> None:
    assert not detectar_accion_critica(pregunta)


def test_el_detector_es_conservador_ante_la_duda() -> None:
    """Un falso positivo cuesta un clic de aprobación; un falso negativo, un correo no autorizado."""
    assert detectar_accion_critica("Explícame cómo se envían los reportes de RRHH")


def test_proponer_accion_incluye_destinatario_y_cuerpo() -> None:
    estado = estado_inicial(PREGUNTA_CRITICA)
    estado["respuesta_final"] = RESPUESTA
    accion = proponer_accion(estado, destinatario="rrhh@techcorp.example")
    assert accion is not None
    assert accion.destinatario == "rrhh@techcorp.example" and accion.cuerpo == RESPUESTA
    assert accion.motivos == ["EFECTO_SECUNDARIO"]
    assert proponer_accion(estado_inicial(PREGUNTA), destinatario="x@y.z") is None


async def test_pregunta_no_critica_no_pide_aprobacion(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, rt):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA})).json()["job_id"]
        final = await esperar_estado(cliente, job_id, {"DONE", "FAILED", "WAITING_APPROVAL"})
        assert final["status"] == "DONE"
        assert await rt.outbox.listar() == []


async def test_tarea_critica_se_pausa_y_espera_aprobacion(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, rt):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA_CRITICA})).json()["job_id"]
        pausado = await esperar_estado(cliente, job_id, {"WAITING_APPROVAL", "DONE", "FAILED"})
        assert pausado["status"] == "WAITING_APPROVAL"
        solicitud: Any = pausado["approval_request"]
        assert solicitud["tipo"] == "aprobacion_requerida"
        assert solicitud["accion"]["destinatario"] == "rrhh@techcorp.example"
        assert RESPUESTA in solicitud["respuesta_propuesta"]
        assert pausado["progress"][-1] == "evaluar_criticidad"
        assert await rt.outbox.listar() == []  # NADA se ejecutó antes de la aprobación
        async with asyncio.timeout(5):  # el worker queda libre mientras el humano decide
            while await rt.store.largo_procesando():
                await asyncio.sleep(0.02)


async def test_aprobar_reanuda_ejecuta_la_accion_y_termina(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, _rt):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA_CRITICA})).json()["job_id"]
        await esperar_estado(cliente, job_id, {"WAITING_APPROVAL"})
        r = await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True, "revisor": "ana"})
        assert r.status_code == 202 and r.json()["status"] == "PENDING"
        final = await esperar_estado(cliente, job_id, {"DONE", "FAILED"})
        assert final["status"] == "DONE"
        resultado: Any = final["result"]
        assert resultado["accion"]["estado"] == "EJECUTADA"
        assert "Acción ejecutada tras aprobación humana" in resultado["respuesta"]
        correos = (await cliente.get("/outbox")).json()
        assert len(correos) == 1 and correos[0]["destinatario"] == "rrhh@techcorp.example"
        assert correos[0]["accion_id"] == f"{job_id}:notificar_rrhh"


async def test_rechazar_termina_sin_ejecutar_nada(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, _rt):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA_CRITICA})).json()["job_id"]
        await esperar_estado(cliente, job_id, {"WAITING_APPROVAL"})
        body = {"aprobado": False, "revisor": "ana", "comentario": "no corresponde avisar"}
        assert (await cliente.post(f"/tasks/{job_id}/approve", json=body)).status_code == 202
        final = await esperar_estado(cliente, job_id, {"DONE", "FAILED"})
        assert final["status"] == "DONE"
        resultado: Any = final["result"]
        assert (
            resultado["accion"]["estado"] == "RECHAZADA" and "no corresponde avisar" in resultado["accion"]["detalle"]
        )
        assert "No se envió nada" in resultado["respuesta"]
        assert (await cliente.get("/outbox")).json() == []


async def test_doble_aprobacion_devuelve_409_y_ejecuta_una_sola_vez(
    abrir_cliente: Abrir, api_settings: ApiSettings
) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, _):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA_CRITICA})).json()["job_id"]
        await esperar_estado(cliente, job_id, {"WAITING_APPROVAL"})
        respuestas = await asyncio.gather(
            *(cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True}) for _ in range(5))
        )
        assert sorted(r.status_code for r in respuestas) == [202, 409, 409, 409, 409]
        await esperar_estado(cliente, job_id, {"DONE"})
        assert len((await cliente.get("/outbox")).json()) == 1


async def test_aprobar_un_job_que_no_espera_devuelve_409_y_uno_inexistente_404(
    abrir_cliente: Abrir, api_settings: ApiSettings
) -> None:
    async with abrir_cliente(api_settings, worker=True) as (cliente, _):
        job_id = (await cliente.post("/tasks", json={"query": PREGUNTA})).json()["job_id"]
        await esperar_estado(cliente, job_id, {"DONE"})
        assert (await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True})).status_code == 409
        inexistente = "00000000-0000-4000-8000-000000000000"
        assert (await cliente.post(f"/tasks/{inexistente}/approve", json={"aprobado": True})).status_code == 404
        assert (await cliente.post("/tasks/no-es-uuid/approve", json={"aprobado": True})).status_code == 422


async def test_aprobar_exige_api_key_si_esta_configurada(abrir_cliente: Abrir, api_settings: ApiSettings) -> None:
    import dataclasses

    con_clave = dataclasses.replace(api_settings, api_key="secreta")
    async with abrir_cliente(con_clave) as (cliente, _):
        r = await cliente.post("/tasks/00000000-0000-4000-8000-000000000000/approve", json={"aprobado": True})
    assert r.status_code == 401


async def test_la_accion_es_idempotente_si_el_worker_reintenta(runtime: Runtime) -> None:
    """Si el worker cae justo después de enviar y se reintenta el nodo, el 'correo' no se duplica (HSETNX)."""
    job = await runtime.store.crear(PREGUNTA_CRITICA)
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)
    await runtime.store.transicion(
        job.job_id,
        {"WAITING_APPROVAL"},
        {"status": "PENDING", "resume": {"aprobado": True, "revisor": "ana", "comentario": None}, "attempts": 0},
    )
    await runtime.store.encolar(job.job_id)
    await runtime.store.tomar(1)
    await procesar_job(runtime, job.job_id)
    from schemas import AccionPropuesta

    accion = AccionPropuesta(
        tipo="notificar_rrhh",
        motivos=["EFECTO_SECUNDARIO"],
        resumen="r",
        destinatario="rrhh@techcorp.example",
        asunto="a",
        cuerpo="c",
    )
    detalle = await runtime.outbox.ejecutar(accion, f"{job.job_id}:notificar_rrhh")  # mismo accion_id
    assert "no se duplicó" in detalle
    assert len(await runtime.outbox.listar()) == 1
