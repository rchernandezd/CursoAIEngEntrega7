"""Especialista 2 — Analista: cálculos con una calculadora segura; devuelve un AnalysisArtifact."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from agents._common import armar_prompt_especialista, llamadas_a_herramientas, salidas_de_herramienta
from faults import InyectorFallas
from prompts import ANALISTA_PROMPT
from schemas import AnalysisArtifact, Contribucion
from state import AgentState
from tools.calculator import crear_calculadora

logger = logging.getLogger(__name__)

NOMBRE_HERRAMIENTA = "calculadora"


def crear_agente_analista(llm: BaseChatModel) -> CompiledStateGraph[Any, Any, Any, Any]:
    return create_agent(
        model=llm,
        tools=[crear_calculadora()],
        system_prompt=ANALISTA_PROMPT,
        response_format=ToolStrategy(AnalysisArtifact),
        name="analista",
    )


def crear_nodo_analista(
    agente: CompiledStateGraph[Any, Any, Any, Any], *, inyector: InyectorFallas, timeout_s: float
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def nodo_analista(state: AgentState) -> dict[str, Any]:
        intento = state["intentos"].get("analista", 0) + 1
        base: dict[str, Any] = {"pasos": 1, "intentos": {"analista": 1}}
        try:
            inyector.disparar_excepcion("analista")
            prompt = armar_prompt_especialista(state, incluir_datos=True)
            async with asyncio.timeout(timeout_s):
                resultado = await agente.ainvoke({"messages": [HumanMessage(content=prompt)]})
            artefacto = resultado.get("structured_response")
            # el inyector es un no-op salvo en los escenarios de falla de la demo
            artefacto = inyector.corromper_analisis(artefacto) if isinstance(artefacto, AnalysisArtifact) else None
            mensajes = resultado["messages"]
            calculos = salidas_de_herramienta(mensajes, NOMBRE_HERRAMIENTA)
            llamadas = llamadas_a_herramientas(mensajes, frozenset({"AnalysisArtifact"}))
            resumen = (
                f"{artefacto.expresion} = {artefacto.resultado:g} (confianza={artefacto.confidence_score:.2f})"
                if artefacto
                else "sin artefacto estructurado"
            )
            return base | {
                "analysis_output": artefacto,
                "analysis_tool_results": calculos,
                "analysis_error": None,
                "contribuciones": [
                    Contribucion(
                        agente="analista", intento=intento, ok=None, resumen=resumen, detalle={"herramientas": llamadas}
                    )
                ],
                "messages": [AIMessage(content=resumen, name="analista")],
            }
        except Exception as exc:
            logger.warning("analista falló (intento %d): %s", intento, exc)
            msg = f"{type(exc).__name__}: {exc}"
            return base | {
                "analysis_output": None,
                "analysis_tool_results": [],
                "analysis_error": msg,
                "contribuciones": [Contribucion(agente="analista", intento=intento, ok=False, resumen=msg)],
                "messages": [AIMessage(content=f"[ERROR] {msg}", name="analista")],
            }

    return nodo_analista
