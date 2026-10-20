"""Utilidades compartidas por los agentes: normalización de contenido, evidencia y contexto acotado."""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from schemas import Fragmento
from state import AgentState
from tools.retrieval import parsear_evidencia


def extraer_texto(contenido: Any) -> str:
    """str o lista de bloques ([{'type': 'text', 'text': ...}], como devuelven Gemini/Anthropic) → texto plano.

    Ignora la metadata interna (`extras.signature`) que el notebook original dejaba pasar a la respuesta.
    """
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        partes = [
            b["text"] if isinstance(b, dict) and b.get("type") == "text" else (b if isinstance(b, str) else "")
            for b in contenido
        ]
        return "".join(partes)
    return str(contenido)


def evidencia_desde_mensajes(mensajes: Sequence[BaseMessage], herramienta: str) -> list[Fragmento]:
    """Fragmentos devueltos por la herramienta de búsqueda durante la ejecución del especialista."""
    evidencia: list[Fragmento] = []
    for m in mensajes:
        if isinstance(m, ToolMessage) and m.name == herramienta:
            evidencia.extend(parsear_evidencia(extraer_texto(m.content)))
    return evidencia


def salidas_de_herramienta(mensajes: Sequence[BaseMessage], herramienta: str) -> list[str]:
    return [extraer_texto(m.content) for m in mensajes if isinstance(m, ToolMessage) and m.name == herramienta]


def llamadas_a_herramientas(mensajes: Sequence[BaseMessage], ignorar: frozenset[str]) -> list[dict[str, Any]]:
    """[{'nombre', 'args', 'salida'}] de las herramientas reales (sin el pseudo-tool del artefacto estructurado)."""
    salidas = {m.tool_call_id: extraer_texto(m.content) for m in mensajes if isinstance(m, ToolMessage)}
    llamadas: list[dict[str, Any]] = []
    for m in mensajes:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                if tc["name"] not in ignorar:
                    llamadas.append(
                        {"nombre": tc["name"], "args": tc["args"], "salida": salidas.get(tc["id"] or "", "")[:300]}
                    )
    return llamadas


def datos_validados(state: AgentState) -> list[str]:
    """Hallazgos del investigador YA aprobados, en formato compacto (único contexto extra que recibe el analista)."""
    if not state["aprobaciones"].get("investigador") or state["research_output"] is None:
        return []
    return [
        f"- {h.afirmacion}"
        + (f" (valor={h.valor:g}{' ' + h.unidad if h.unidad else ''})" if h.valor is not None else "")
        + f" [fuente: {h.fuente}]"
        for h in state["research_output"].hallazgos
    ]


def armar_prompt_especialista(state: AgentState, *, incluir_datos: bool) -> str:
    """Contexto mínimo: solo la instrucción puntual (+ datos validados si el rol los necesita).

    NO se pasa el historial de mensajes ni la metadata del sistema (evita la contaminación de contexto).
    """
    instruccion = (state["instruccion"] or "").strip()
    if not instruccion:
        raise ValueError("El supervisor no definió una instrucción para el especialista.")
    partes = [f"Instrucción: {instruccion}"]
    if incluir_datos:
        datos = datos_validados(state)
        if datos:
            partes.append("Datos validados disponibles:\n" + "\n".join(datos))
    return "\n\n".join(partes)
