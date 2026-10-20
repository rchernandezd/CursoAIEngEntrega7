"""Nodos terminales: síntesis (respuesta final con aportes VALIDADOS) y revisión humana (escalamiento)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents._common import extraer_texto
from prompts import SINTESIS_PROMPT
from state import AgentState

logger = logging.getLogger(__name__)


def aportes_validados(state: AgentState) -> list[str]:
    """Solo lo aprobado por el validador entra en la respuesta final."""
    lineas: list[str] = []
    inv, ana = state["research_output"], state["analysis_output"]
    if inv is not None and state["aprobaciones"].get("investigador"):
        if inv.estado == "sin_evidencia":
            lineas.append(f"[investigador] SIN EVIDENCIA para: {inv.consulta}")
        for h in inv.hallazgos:
            valor = f" (valor={h.valor:g}{' ' + h.unidad if h.unidad else ''})" if h.valor is not None else ""
            lineas.append(f"[investigador] {h.afirmacion}{valor} — fuente: {h.fuente}")
    if ana is not None and state["aprobaciones"].get("analista"):
        lineas.append(f"[analista] {ana.expresion} = {ana.resultado:g}. {ana.explicacion}")
        lineas.extend(f"[analista] supuesto: {s}" for s in ana.supuestos)
    return lineas


def advertencias_vigentes(state: AgentState) -> list[str]:
    v = state["validacion"]
    return [f"[{h.codigo}] {h.mensaje}" for h in v.advertencias] if v is not None else []


def crear_nodo_sintesis(llm: BaseChatModel) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def nodo_sintesis(state: AgentState) -> dict[str, Any]:
        aportes = aportes_validados(state)
        advertencias = advertencias_vigentes(state)
        prompt = (
            f"Pregunta original: {state['pregunta']}\n"
            f"Estado de la tarea: {state['estado_final']}\n\n"
            "Aportes validados:\n"
            + ("\n".join(aportes) or "(ninguno)")
            + ("\n\nAdvertencias:\n" + "\n".join(advertencias) if advertencias else "")
        )
        try:
            respuesta = await llm.ainvoke([SystemMessage(content=SINTESIS_PROMPT), HumanMessage(content=prompt)])
            texto = extraer_texto(respuesta.content).strip()
        except Exception as exc:  # degradación elegante: nunca se pierde lo ya validado
            logger.warning("La síntesis con LLM falló (%s); se devuelve un resumen determinístico", exc)
            texto = "No pude redactar la síntesis; estos son los aportes validados:\n" + "\n".join(aportes)
        return {"respuesta_final": texto, "messages": [AIMessage(content=texto, name="supervisor")]}

    return nodo_sintesis


def crear_nodo_revision_humana() -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def nodo_revision_humana(state: AgentState) -> dict[str, Any]:
        v = state["validacion"]
        pendientes = [f"[{h.codigo}] {h.mensaje}" for h in v.errores] if v is not None and not v.ok else []
        validados = aportes_validados(state)
        texto = (
            f"Esta consulta requiere revisión humana (motivo: {state['motivo_escalamiento']}). "
            "No entrego un resultado definitivo porque no superó la validación automática."
        )
        if pendientes:
            texto += "\nErrores sin resolver:\n" + "\n".join(f"- {p}" for p in pendientes)
        if validados:
            texto += "\nAportes que sí fueron validados:\n" + "\n".join(f"- {a}" for a in validados)
        return {"respuesta_final": texto, "messages": [AIMessage(content=texto, name="supervisor")]}

    return nodo_revision_humana
