"""Estado compartido del orquestador.

Reglas de diseño (Clase 6, "Aislamiento de escritura"):
- Hereda de MessagesState (reducer add_messages para `messages`).
- Cada parcela tiene UN solo nodo escritor; el resto solo lee.
- Los campos acumulativos usan reducers (operator.add / fusionar): nada se pisa, todo queda auditado.
"""

import operator
from typing import Annotated

from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState

from schemas import (
    AccionPropuesta,
    AnalysisArtifact,
    Contribucion,
    DecisionHumana,
    EstadoFinal,
    Fragmento,
    RegistroDecision,
    ResearchArtifact,
    ResultadoAccion,
    ResultadoValidacion,
    SiguienteEfectivo,
)


def sumar_contadores(actual: dict[str, int], nuevo: dict[str, int]) -> dict[str, int]:
    """Reducer: suma por clave ({'analista': 1} + {'analista': 1} = {'analista': 2})."""
    resultado = dict(actual)
    for clave, valor in nuevo.items():
        resultado[clave] = resultado.get(clave, 0) + valor
    return resultado


def fusionar(actual: dict[str, bool], nuevo: dict[str, bool]) -> dict[str, bool]:
    """Reducer: el valor más nuevo de cada clave gana; las demás claves se conservan."""
    return {**actual, **nuevo}


class AgentState(MessagesState):
    """`messages` viene de MessagesState. Escritor de cada campo entre paréntesis."""

    pregunta: str  # (entrada) pregunta original del usuario

    # --- control del flujo (supervisor) ---
    next_agent: SiguienteEfectivo | None
    instruccion: str | None  # instrucción puntual para el especialista elegido
    task_completed: bool
    estado_final: EstadoFinal | None
    motivo_escalamiento: str | None
    pasos: Annotated[int, operator.add]  # invocaciones a especialistas (lo suma cada especialista)
    decisiones: Annotated[list[RegistroDecision], operator.add]  # (supervisor) quién decidió qué y por qué

    # --- parcela del investigador ---
    research_output: ResearchArtifact | None
    research_evidence: list[Fragmento]  # fragmentos que devolvió la herramienta de búsqueda
    research_error: str | None

    # --- parcela del analista ---
    analysis_output: AnalysisArtifact | None
    analysis_tool_results: list[str]  # salidas textuales de la calculadora
    analysis_error: str | None

    # --- parcela del validador ---
    validacion: ResultadoValidacion | None  # última validación
    aprobaciones: Annotated[dict[str, bool], fusionar]  # agente → ¿su último artefacto fue aprobado?
    fallos_validacion: Annotated[dict[str, int], sumar_contadores]

    # --- bitácora compartida (append-only; la escriben los especialistas y el validador) ---
    contribuciones: Annotated[list[Contribucion], operator.add]
    intentos: Annotated[dict[str, int], sumar_contadores]  # agente → veces invocado

    # --- salida (síntesis / revisión humana) ---
    respuesta_final: str | None

    # --- Human-in-the-loop (Entrega 7): lo escriben evaluar_criticidad / aprobacion_humana / ejecutar_accion ---
    accion: AccionPropuesta | None  # acción con efectos secundarios detectada (None = tarea no crítica)
    decision_humana: DecisionHumana | None
    accion_resultado: ResultadoAccion | None


def estado_inicial(pregunta: str) -> AgentState:
    """Estado de arranque completo (todas las claves presentes)."""
    return AgentState(
        messages=[HumanMessage(content=pregunta)],
        pregunta=pregunta,
        next_agent=None,
        instruccion=None,
        task_completed=False,
        estado_final=None,
        motivo_escalamiento=None,
        pasos=0,
        decisiones=[],
        research_output=None,
        research_evidence=[],
        research_error=None,
        analysis_output=None,
        analysis_tool_results=[],
        analysis_error=None,
        validacion=None,
        aprobaciones={},
        fallos_validacion={},
        contribuciones=[],
        intentos={},
        respuesta_final=None,
        accion=None,
        decision_humana=None,
        accion_resultado=None,
    )
