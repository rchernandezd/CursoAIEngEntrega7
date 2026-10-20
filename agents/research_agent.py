"""Especialista 1 — Investigador: consulta las políticas internas (RAG) y devuelve un ResearchArtifact."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from agents._common import armar_prompt_especialista, evidencia_desde_mensajes, llamadas_a_herramientas
from faults import InyectorFallas
from prompts import INVESTIGADOR_PROMPT
from schemas import Contribucion, ResearchArtifact
from state import AgentState
from tools.retrieval import Retriever, crear_herramienta_busqueda

logger = logging.getLogger(__name__)

NOMBRE_HERRAMIENTA = "buscar_en_politicas_internas"


def crear_agente_investigador(
    llm: BaseChatModel, retriever: Retriever, *, k: int
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Sub-agente ReAct con UNA herramienta acotada y salida estructurada validada por Pydantic."""
    return create_agent(
        model=llm,
        tools=[crear_herramienta_busqueda(retriever, k=k)],
        system_prompt=INVESTIGADOR_PROMPT,
        response_format=ToolStrategy(ResearchArtifact),
        name="investigador",
    )


def crear_nodo_investigador(
    agente: CompiledStateGraph[Any, Any, Any, Any], *, inyector: InyectorFallas, timeout_s: float
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def nodo_investigador(state: AgentState) -> dict[str, Any]:
        intento = state["intentos"].get("investigador", 0) + 1
        base: dict[str, Any] = {"pasos": 1, "intentos": {"investigador": 1}}
        try:
            inyector.disparar_excepcion("investigador")
            prompt = armar_prompt_especialista(state, incluir_datos=False)
            async with asyncio.timeout(timeout_s):
                resultado = await agente.ainvoke({"messages": [HumanMessage(content=prompt)]})
            artefacto = resultado.get("structured_response")
            if not isinstance(artefacto, ResearchArtifact):
                artefacto = None
            mensajes = resultado["messages"]
            evidencia = evidencia_desde_mensajes(mensajes, NOMBRE_HERRAMIENTA)
            llamadas = llamadas_a_herramientas(mensajes, frozenset({"ResearchArtifact"}))
            resumen = (
                f"estado={artefacto.estado}, {len(artefacto.hallazgos)} hallazgo(s), "
                f"confianza={artefacto.confidence_score:.2f}"
                if artefacto
                else "sin artefacto estructurado"
            )
            return base | {
                "research_output": artefacto,
                "research_evidence": evidencia,
                "research_error": None,
                "contribuciones": [
                    Contribucion(
                        agente="investigador",
                        intento=intento,
                        ok=None,
                        resumen=resumen,
                        detalle={"herramientas": llamadas},
                    )
                ],
                "messages": [AIMessage(content=resumen, name="investigador")],
            }
        except Exception as exc:  # un nodo que falla sin capturarse deja el sistema bloqueado
            logger.warning("investigador falló (intento %d): %s", intento, exc)
            msg = f"{type(exc).__name__}: {exc}"
            return base | {
                "research_output": None,
                "research_evidence": [],
                "research_error": msg,
                "contribuciones": [Contribucion(agente="investigador", intento=intento, ok=False, resumen=msg)],
                "messages": [AIMessage(content=f"[ERROR] {msg}", name="investigador")],
            }

    return nodo_investigador
