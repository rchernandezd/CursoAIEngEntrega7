"""Supervisor: router inteligente + controlador de flujo.

El LLM decide (salida estructurada con `Literal`); las reglas deterministas (guardrails) garantizan que el
sistema SIEMPRE termine y que nunca se cierre con un resultado que no pasó la validación.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings
from prompts import SUPERVISOR_PROMPT
from schemas import DecisionSupervisor, EstadoFinal, RegistroDecision, Rubrica, SiguienteEfectivo
from state import AgentState
from tools.retrieval import normalizar_texto

logger = logging.getLogger(__name__)

MARCA_REPARACION = "\n\n[REPARACIÓN]"


@dataclass(frozen=True, slots=True)
class DecisionFinal:
    """Decisión ya filtrada por los guardrails."""

    siguiente: SiguienteEfectivo
    instruccion: str
    razon: str
    origen: Literal["llm", "guardrail"]
    rubrica: Rubrica | None = None
    sobrescrituras: tuple[str, ...] = ()
    motivo_escalamiento: str | None = None


def instruccion_base(instruccion: str | None) -> str:
    """La instrucción sin el bloque [REPARACIÓN] (para no acumular reparaciones ni contaminar operandos)."""
    return (instruccion or "").split(MARCA_REPARACION)[0].strip()


def resumir_estado(state: AgentState, max_pasos: int) -> str:
    """Resumen COMPACTO para el LLM supervisor (no se le pasa todo el historial de mensajes)."""
    lineas = [f"Pasos usados: {state['pasos']}/{max_pasos}"]
    inv, ana = state["research_output"], state["analysis_output"]
    ap = state["aprobaciones"]
    if inv is None:
        lineas.append(
            "- investigador: no invocado"
            if not state["intentos"].get("investigador")
            else "- investigador: sin artefacto"
        )
    else:
        lineas.append(
            f"- investigador: intentos={state['intentos'].get('investigador', 0)}, "
            f"aprobado={ap.get('investigador', False)}, estado={inv.estado}"
        )
        for h in inv.hallazgos:
            valor = f" valor={h.valor:g}" if h.valor is not None else ""
            lineas.append(f"    · {h.afirmacion}{valor} [fuente: {h.fuente}]")
    if ana is None:
        lineas.append(
            "- analista: no invocado" if not state["intentos"].get("analista") else "- analista: sin artefacto"
        )
    else:
        lineas.append(
            f"- analista: intentos={state['intentos'].get('analista', 0)}, aprobado={ap.get('analista', False)}, "
            f"{ana.expresion} = {ana.resultado:g}"
        )
    v = state["validacion"]
    if v is not None and not v.ok:
        lineas.append(f"- ÚLTIMA VALIDACIÓN FALLÓ ({v.agente}): " + " | ".join(h.mensaje for h in v.errores))
    return "\n".join(lineas)


def construir_instruccion_reparacion(base: str, agente: str, state: AgentState) -> str:
    """Bucle de reparación: reenvía la salida fallida + el error ESPECÍFICO del validador (no un 'intentá de nuevo')."""
    v = state["validacion"]
    artefacto = state["research_output"] if agente == "investigador" else state["analysis_output"]
    previo = artefacto.model_dump_json()[:1500] if artefacto is not None else "(no hubo artefacto)"
    errores = "\n".join(f"- [{h.codigo}] {h.mensaje}" for h in (v.errores if v else []))
    return (
        f"{base}{MARCA_REPARACION} Tu salida anterior fue rechazada por el validador determinístico.\n"
        f"Salida anterior: {previo}\nErrores específicos:\n{errores}\n"
        "Corregí SOLO eso y devolvé el artefacto completo otra vez."
    )


def calcular_estado_final(state: AgentState, rubrica: Rubrica | None) -> EstadoFinal:
    inv = state["research_output"]
    if inv is not None and inv.estado == "sin_evidencia" and not state["aprobaciones"].get("analista"):
        return "INCOMPLETO"
    if rubrica is not None and not rubrica.aprobada:
        return "INCOMPLETO"
    return "COMPLETADO"


def aplicar_guardrails(decision: DecisionSupervisor | None, state: AgentState, settings: Settings) -> DecisionFinal:
    """Reglas deterministas aplicadas SOBRE la decisión del LLM. Función pura: se testea sin LLM."""
    # G1 - tope de pasos (criterio de parada estricto contra el "Supervisor Infinito")
    if state["pasos"] >= settings.max_pasos:
        return DecisionFinal(
            "ESCALAR",
            "",
            f"Se alcanzó el tope de {settings.max_pasos} pasos.",
            "guardrail",
            sobrescrituras=("LIMITE_DE_PASOS",),
            motivo_escalamiento="LIMITE_DE_PASOS",
        )
    if decision is None:
        return DecisionFinal(
            "ESCALAR",
            "",
            "El supervisor no pudo decidir.",
            "guardrail",
            sobrescrituras=("SIN_DECISION",),
            motivo_escalamiento="ERROR_SUPERVISOR",
        )

    siguiente: SiguienteEfectivo = decision.next
    instruccion, razon = decision.instruccion.strip(), decision.razon
    sobrescrituras: list[str] = []
    origen: Literal["llm", "guardrail"] = "llm"

    # G2 - validación fallida: reparar al mismo agente con el error específico, o escalar si ya se agotaron los intentos
    v = state["validacion"]
    if v is not None and not v.ok:
        if state["fallos_validacion"].get(v.agente, 0) > settings.max_reparaciones:
            return DecisionFinal(
                "ESCALAR",
                "",
                f"La validación de '{v.agente}' sigue fallando tras {settings.max_reparaciones} reparaciones.",
                "guardrail",
                decision.rubrica,
                ("VALIDACION_IRRESOLUBLE",),
                f"VALIDACION_IRRESOLUBLE:{v.agente}",
            )
        base = instruccion_base(instruccion if siguiente == v.agente else state["instruccion"])
        if siguiente != v.agente:
            sobrescrituras.append(f"REPARACION_FORZADA:{v.agente}")
            origen = "guardrail"
        siguiente, instruccion = v.agente, construir_instruccion_reparacion(base, v.agente, state)
        return DecisionFinal(siguiente, instruccion, razon, origen, decision.rubrica, tuple(sobrescrituras))

    # G3 - repetición sin progreso: mismo especialista, misma instrucción, ya aprobado → cerrar
    if siguiente in ("investigador", "analista") and state["aprobaciones"].get(siguiente):
        anteriores = [d for d in state["decisiones"] if d.siguiente == siguiente and not d.sobrescrituras]
        if anteriores and normalizar_texto(anteriores[-1].instruccion) == normalizar_texto(instruccion):
            return DecisionFinal(
                "FINISH",
                "",
                "Repetición sin progreso: el especialista ya cumplió esa instrucción.",
                "guardrail",
                decision.rubrica,
                ("REPETICION_SIN_PROGRESO",),
            )

    # G4 - no se puede cerrar sin que ningún especialista haya trabajado
    if siguiente == "FINISH" and not state["contribuciones"]:
        return DecisionFinal(
            "investigador",
            state["pregunta"],
            "No se puede finalizar sin evidencia: se consulta al investigador.",
            "guardrail",
            decision.rubrica,
            ("FINISH_SIN_TRABAJO",),
        )
    if siguiente == "FINISH":
        instruccion = ""
        if not (decision.rubrica.aprobada):
            sobrescrituras.append("FINISH_CON_RUBRICA_INCOMPLETA")  # se cierra, pero se marca INCOMPLETO
    elif not instruccion:
        sobrescrituras.append("INSTRUCCION_VACIA")
        instruccion, origen = state["pregunta"], "guardrail"
    return DecisionFinal(siguiente, instruccion, razon, origen, decision.rubrica, tuple(sobrescrituras))


def crear_nodo_supervisor(llm: BaseChatModel, settings: Settings) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    estructurado = llm.with_structured_output(DecisionSupervisor)

    async def nodo_supervisor(state: AgentState) -> dict[str, Any]:
        decision: DecisionSupervisor | None = None
        if state["pasos"] < settings.max_pasos:  # si ya se agotó el tope, ni se llama al LLM
            try:
                bruto = await estructurado.ainvoke(
                    [
                        SystemMessage(content=SUPERVISOR_PROMPT),
                        HumanMessage(
                            content=f"Pregunta original: {state['pregunta']}\n\n"
                            f"Estado del equipo:\n{resumir_estado(state, settings.max_pasos)}"
                        ),
                    ]
                )
                decision = bruto if isinstance(bruto, DecisionSupervisor) else DecisionSupervisor.model_validate(bruto)
            except Exception as exc:  # el supervisor es el cuello de botella: si falla, se escala, no se cae
                logger.warning("El LLM del supervisor falló: %s", exc)
        final = aplicar_guardrails(decision, state, settings)
        terminado = final.siguiente in ("FINISH", "ESCALAR")
        update: dict[str, Any] = {
            "next_agent": final.siguiente,
            "instruccion": final.instruccion or None,
            "task_completed": terminado,
            "motivo_escalamiento": final.motivo_escalamiento,
            "decisiones": [
                RegistroDecision(
                    paso=len(state["decisiones"]) + 1,
                    origen=final.origen,
                    siguiente=final.siguiente,
                    razon=final.razon,
                    instruccion=final.instruccion,
                    rubrica=final.rubrica,
                    sobrescrituras=list(final.sobrescrituras),
                )
            ],
        }
        if final.siguiente == "FINISH":
            update["estado_final"] = calcular_estado_final(state, final.rubrica)
        elif final.siguiente == "ESCALAR":
            update["estado_final"] = "REQUIERE_REVISION_HUMANA"
        return update

    return nodo_supervisor


def enrutar(state: AgentState) -> Literal["investigador", "analista", "sintesis", "revision_humana"]:
    """Arista condicional: mapea la decisión del supervisor al nombre del nodo siguiente."""
    match state["next_agent"]:
        case "investigador":
            return "investigador"
        case "analista":
            return "analista"
        case "FINISH":
            return "sintesis"
        case "ESCALAR":
            return "revision_humana"
        case other:
            raise ValueError(f"next_agent inválido: {other!r}")
