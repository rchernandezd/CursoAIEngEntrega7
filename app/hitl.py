"""Human-in-the-loop: tareas críticas, nodo de aprobación (interrupt), acción con efectos y endpoint /approve.

Flujo (después de la síntesis):

    sintesis → evaluar_criticidad ─┬─ no crítica ───────────────────────────────→ END
                                   └─ crítica → aprobacion_humana (interrupt) ─┬─ aprobada → ejecutar_accion → END
                                                                               └─ rechazada ───────────────────→ END

Regla de oro (Clase 7): al reanudar, el nodo con `interrupt()` se vuelve a ejecutar desde el principio. Por eso
`aprobacion_humana` no hace nada con efectos antes del interrupt: el efecto vive en el nodo SIGUIENTE
(`ejecutar_accion`), que además es idempotente (HSETNX con una clave determinística).
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from redis.asyncio import Redis

from app.deps import AutenticadoDep, RuntimeDep
from app.schemas import ApproveBody, TaskAccepted, ahora
from schemas import AccionPropuesta, DecisionHumana, ResultadoAccion
from state import AgentState
from tools.retrieval import normalizar_texto

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 1. ¿la tarea es crítica?
# Criterio determinístico (no un LLM): la pregunta pide una ACCIÓN con efectos en el mundo (enviar/notificar) hacia
# un destino concreto (RRHH, correo). Es conservador a propósito: ante la duda se pide aprobación.
_VERBO_ACCION = re.compile(r"\b(?:envi\w*|mand\w*|notific\w*|avis\w*|comunic\w*)\b")
_DESTINO = re.compile(r"\b(?:rrhh|rr hh|recursos humanos|correo|e mail|email|mail)\b")


def detectar_accion_critica(pregunta: str) -> bool:
    texto = normalizar_texto(pregunta)
    return bool(_VERBO_ACCION.search(texto) and _DESTINO.search(texto))


def proponer_accion(state: AgentState, *, destinatario: str) -> AccionPropuesta | None:
    """None si la tarea no es crítica; si lo es, la acción exacta que se ejecutaría tras la aprobación."""
    if not detectar_accion_critica(state["pregunta"]):
        return None
    cuerpo = state["respuesta_final"] or ""
    return AccionPropuesta(
        tipo="notificar_rrhh",
        motivos=["EFECTO_SECUNDARIO"],
        resumen=f"Enviar el resultado por correo a {destinatario}",
        destinatario=destinatario,
        asunto=f"Resultado de consulta: {state['pregunta'][:80]}",
        cuerpo=cuerpo,
    )


# ---------------------------------------------------------------- 2. actuador (el efecto secundario)
class Actuador(Protocol):
    async def ejecutar(self, accion: AccionPropuesta, accion_id: str) -> str: ...


class RedisOutbox:
    """Efecto secundario SIMULADO: deja el 'correo' en un hash de Redis (outbox). Idempotente por `accion_id`."""

    def __init__(self, redis: Redis, *, clave: str) -> None:
        self._redis = redis
        self._clave = clave

    async def ejecutar(self, accion: AccionPropuesta, accion_id: str) -> str:
        registro = {**accion.model_dump(), "accion_id": accion_id, "enviado_en": ahora().isoformat()}
        # HSETNX es atómico: si el worker se cae y reintenta, el "correo" no se envía dos veces.
        nuevo = await self._redis.hsetnx(self._clave, accion_id, json.dumps(registro, ensure_ascii=False))
        if nuevo:
            return f"correo enviado a {accion.destinatario} (id {accion_id})"
        return f"el correo ya había sido enviado (id {accion_id}); no se duplicó"

    async def listar(self) -> list[dict[str, Any]]:
        crudo = cast(dict[str, str], await self._redis.hgetall(self._clave))
        return [json.loads(v) for v in crudo.values()]


# ---------------------------------------------------------------- 3. nodos del grafo
Nodo = Callable[..., Awaitable[dict[str, Any]]]


def crear_nodo_evaluar_criticidad(*, destinatario: str) -> Nodo:
    async def evaluar_criticidad(state: AgentState) -> dict[str, Any]:
        accion = proponer_accion(state, destinatario=destinatario)
        if accion is not None:
            logger.info("Tarea crítica (%s): requiere aprobación humana", ",".join(accion.motivos))
        return {"accion": accion}

    return evaluar_criticidad


def crear_nodo_aprobacion_humana() -> Nodo:
    async def aprobacion_humana(state: AgentState) -> dict[str, Any]:
        accion = state["accion"]
        if accion is None:  # defensa: la arista condicional no debería mandar aquí una tarea no crítica
            return {}
        # --- PAUSA: el grafo se detiene, el estado queda en el checkpointer (Redis) y `interrupt` devuelve este
        # --- payload a quien invocó el grafo. Al reanudar con Command(resume=...), `interrupt` devuelve el resume.
        bruto = interrupt(
            {
                "tipo": "aprobacion_requerida",
                "pregunta": state["pregunta"],
                "accion": accion.model_dump(),
                "respuesta_propuesta": state["respuesta_final"],
            }
        )
        decision = DecisionHumana.model_validate(bruto)
        if decision.aprobado:
            return {"decision_humana": decision}
        motivo = f": {decision.comentario}" if decision.comentario else ""
        texto = f"{state['respuesta_final'] or ''}\n\n✘ Acción rechazada por el revisor{motivo}. No se envió nada."
        return {
            "decision_humana": decision,
            "accion_resultado": ResultadoAccion(
                estado="RECHAZADA", detalle=f"Rechazada por {decision.revisor or 'el revisor'}{motivo}"
            ),
            "respuesta_final": texto.strip(),
            "messages": [AIMessage(content=texto.strip(), name="supervisor")],
        }

    return aprobacion_humana


def crear_nodo_ejecutar_accion(actuador: Actuador) -> Nodo:
    async def ejecutar_accion(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        accion = state["accion"]
        if accion is None:
            return {}
        accion_id = f"{config['configurable']['thread_id']}:{accion.tipo}"  # determinístico → idempotente
        detalle = await actuador.ejecutar(accion, accion_id)
        texto = f"{state['respuesta_final'] or ''}\n\n✔ Acción ejecutada tras aprobación humana: {detalle}."
        return {
            "accion_resultado": ResultadoAccion(estado="EJECUTADA", detalle=detalle, accion_id=accion_id),
            "respuesta_final": texto.strip(),
            "messages": [AIMessage(content=texto.strip(), name="supervisor")],
        }

    return ejecutar_accion


def enrutar_criticidad(state: AgentState) -> Literal["aprobacion_humana", "__end__"]:
    return "aprobacion_humana" if state["accion"] is not None else "__end__"


def enrutar_aprobacion(state: AgentState) -> Literal["ejecutar_accion", "__end__"]:
    decision = state["decision_humana"]
    return "ejecutar_accion" if decision is not None and decision.aprobado else "__end__"


def crear_extensor_hitl(actuador: Actuador, *, destinatario: str) -> Callable[[StateGraph[AgentState]], None]:
    """Conecta `sintesis` con la compuerta HITL (se pasa como `extender=` a graph.build_graph / compilar)."""

    def extender(grafo: StateGraph[AgentState]) -> None:
        grafo.add_node("evaluar_criticidad", crear_nodo_evaluar_criticidad(destinatario=destinatario))
        grafo.add_node("aprobacion_humana", crear_nodo_aprobacion_humana())
        grafo.add_node("ejecutar_accion", crear_nodo_ejecutar_accion(actuador))
        grafo.add_edge("sintesis", "evaluar_criticidad")
        grafo.add_conditional_edges("evaluar_criticidad", enrutar_criticidad)
        grafo.add_conditional_edges("aprobacion_humana", enrutar_aprobacion)
        grafo.add_edge("ejecutar_accion", END)

    return extender


# ---------------------------------------------------------------- 4. endpoint de aprobación
router = APIRouter(dependencies=[AutenticadoDep])


@router.post("/tasks/{job_id}/approve", status_code=202, response_model=TaskAccepted, tags=["HITL"])
async def aprobar_tarea(job_id: UUID, body: ApproveBody, rt: RuntimeDep) -> TaskAccepted:
    """Entrega la decisión humana de un job en WAITING_APPROVAL. No ejecuta nada aquí: re-encola el job y un worker
    reanuda el grafo (el endpoint responde en milisegundos, igual que POST /tasks)."""
    jid = str(job_id)
    decision = DecisionHumana(aprobado=body.aprobado, revisor=body.revisor, comentario=body.comentario)
    job = await rt.store.transicion(
        jid,
        {"WAITING_APPROVAL"},
        {"status": "PENDING", "resume": decision.model_dump(), "approval_request": None, "attempts": 0},
    )
    if job is None:  # no existe, o no estaba esperando (p. ej. ya fue aprobado: la 2.ª llamada cae acá)
        actual = await rt.store.obtener(jid)
        if actual is None:
            raise HTTPException(status_code=404, detail=f"No existe el job {jid}.")
        raise HTTPException(status_code=409, detail=f"El job está en estado {actual.status}, no en WAITING_APPROVAL.")
    await rt.store.encolar(jid)
    return TaskAccepted(job_id=jid, status=job.status)


@router.get("/outbox", tags=["HITL"])
async def ver_outbox(rt: RuntimeDep) -> list[dict[str, Any]]:
    """Demo: los 'correos' que la acción simulada dejó en Redis (para comprobar que un rechazo NO envía nada)."""
    return await rt.outbox.listar()
