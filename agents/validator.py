"""Nodo Validador: puerta de calidad DETERMINÍSTICA entre cada especialista y el supervisor (sin LLM)."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage

from agents.supervisor import instruccion_base
from config import Settings
from schemas import Contribucion, ResultadoValidacion
from state import AgentState
from validation import extraer_numeros, verificar_analisis, verificar_investigacion


def numeros_permitidos(state: AgentState) -> set[float]:
    """Números con origen legítimo para el analista: pregunta + instrucción (sin reparación) + hallazgos aprobados."""
    permitidos = extraer_numeros(state["pregunta"]) | extraer_numeros(instruccion_base(state["instruccion"]))
    if state["aprobaciones"].get("investigador") and state["research_output"] is not None:
        for h in state["research_output"].hallazgos:
            permitidos |= extraer_numeros(h.cita)
            if h.valor is not None:
                permitidos.add(h.valor)
    return permitidos


def crear_nodo_validador(settings: Settings) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def nodo_validador(state: AgentState) -> dict[str, Any]:
        agente = state["next_agent"]
        resultado: ResultadoValidacion
        if agente == "investigador":
            resultado = verificar_investigacion(
                state["research_output"], state["research_evidence"], state["research_error"]
            )
        elif agente == "analista":
            resultado = verificar_analisis(
                state["analysis_output"],
                calculos_tool=state["analysis_tool_results"],
                numeros_permitidos=numeros_permitidos(state),
                error_ejecucion=state["analysis_error"],
                operandos_estrictos=settings.operandos_estrictos,
            )
        else:
            raise ValueError(f"El validador solo corre tras un especialista, no tras {agente!r}")

        estado = "aprobado" if resultado.ok else "RECHAZADO"
        resumen = f"{agente} {estado}"
        if resultado.hallazgos:
            resumen += ": " + " | ".join(f"[{h.codigo}] {h.mensaje}" for h in resultado.hallazgos)
        return {
            "validacion": resultado,
            "aprobaciones": {agente: resultado.ok},
            "fallos_validacion": {} if resultado.ok else {agente: 1},
            "contribuciones": [
                Contribucion(
                    agente="validador",
                    intento=state["intentos"].get(agente, 0),
                    ok=resultado.ok,
                    resumen=resumen,
                    detalle={"para": agente, "hallazgos": [h.model_dump() for h in resultado.hallazgos]},
                )
            ],
            "messages": [AIMessage(content=resumen, name="validador")],
        }

    return nodo_validador
