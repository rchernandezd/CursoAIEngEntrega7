"""Escenarios de prueba (8) con verificaciones automáticas, y el runner que genera la traza."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.callbacks import get_usage_metadata_callback

from config import Settings
from faults import InyectorFallas
from graph import Dependencias, compilar, configuracion, construir_dependencias
from state import estado_inicial
from tools.retrieval import normalizar_texto
from tracing import EscenarioTraza, EventoTraza, Verificacion, evento_desde_update
from validation import extraer_numeros

PREGUNTA_VACACIONES = (
    "Un empleado lleva 12 años en TechCorp. Según la política de vacaciones, ¿cuántos días de vacaciones le "
    "corresponden por año y cuántos días suma en 3 años consecutivos manteniendo ese ritmo?"
)
PREGUNTA_NOTEBOOK = (
    "Un empleado va a cumplir 12 años en TechCorp. Tuvo 14 días de vacaciones los primeros "
    "5 años, 21 los siguientes 5, y ahora tiene derecho a 28. ¿Cuántos días de vacaciones "
    "acumuló en total en esos 12 años?"
)


@dataclass(frozen=True, slots=True)
class Escenario:
    id: str
    titulo: str
    criterio: str
    pregunta: str
    verificar: Callable[[EscenarioTraza], list[Verificacion]]
    excepcion_especialista: Mapping[str, int] = field(default_factory=dict)
    analisis_corrupto: int = 0

    def inyector(self) -> InyectorFallas:
        return InyectorFallas(
            excepcion_especialista=dict(self.excepcion_especialista), analisis_corrupto=self.analisis_corrupto
        )


# ---------------------------------------------------------------- helpers de verificación (puros)
def _v(descripcion: str, ok: bool, detalle: str = "") -> Verificacion:
    return Verificacion(descripcion=descripcion, ok=ok, detalle="" if ok else detalle)


def contiene(t: EscenarioTraza, *textos: str) -> bool:
    resp = normalizar_texto(t.respuesta)
    return all(normalizar_texto(x) in resp for x in textos)


def _indice(t: EscenarioTraza, nodo: str) -> int | None:
    return t.orden_nodos.index(nodo) if nodo in t.orden_nodos else None


def delego_en_orden(t: EscenarioTraza, primero: str, segundo: str) -> bool:
    a, b = _indice(t, primero), _indice(t, segundo)
    return a is not None and b is not None and a < b


def usaron_herramienta(t: EscenarioTraza, nodo: str, herramienta: str) -> bool:
    return any(
        h["nombre"] == herramienta for e in t.eventos if e.nodo == nodo for h in e.detalle.get("herramientas", [])
    )


def _base(t: EscenarioTraza, estado: str) -> list[Verificacion]:
    return [
        _v(f"estado_final == {estado}", t.estado_final == estado, f"fue {t.estado_final}"),
        _v("el flujo termina en síntesis o revisión humana", t.orden_nodos[-1] in ("sintesis", "revision_humana")),
    ]


def verificar_e1(t: EscenarioTraza) -> list[Verificacion]:
    return [
        *_base(t, "COMPLETADO"),
        _v(
            "el supervisor delega primero al investigador y luego al analista",
            delego_en_orden(t, "investigador", "analista"),
            f"orden: {t.orden_nodos}",
        ),
        _v(
            "el investigador usó buscar_en_politicas_internas",
            usaron_herramienta(t, "investigador", "buscar_en_politicas_internas"),
        ),
        _v("el analista usó la calculadora", usaron_herramienta(t, "analista", "calculadora")),
        _v(
            "ambos artefactos fueron aprobados por el validador",
            t.aprobaciones == {"investigador": True, "analista": True},
        ),
        _v("la respuesta contiene 28 (por año) y 84 (en 3 años)", contiene(t, "28", "84"), t.respuesta[:120]),
    ]


def verificar_e2(t: EscenarioTraza) -> list[Verificacion]:
    return [
        *_base(t, "COMPLETADO"),
        _v(
            "ruteo dinámico: va directo al analista, sin investigar",
            "investigador" not in t.orden_nodos and "analista" in t.orden_nodos,
            f"orden: {t.orden_nodos}",
        ),
        _v("la respuesta contiene 231", contiene(t, "231"), t.respuesta[:120]),
    ]


def verificar_e3(t: EscenarioTraza) -> list[Verificacion]:
    return [
        *_base(t, "COMPLETADO"),
        _v(
            "ruteo dinámico: solo investiga, sin analista",
            "analista" not in t.orden_nodos and "investigador" in t.orden_nodos,
        ),
        _v(
            "la respuesta contiene 3 días remotos y 2 presenciales",
            contiene(t, "3", "remoto", "2", "presencial"),
            t.respuesta[:120],
        ),
    ]


def verificar_e4(t: EscenarioTraza) -> list[Verificacion]:
    valores = {h["valor"] for h in (t.research_output or {}).get("hallazgos", []) if h.get("valor") is not None}
    return [
        *_base(t, "COMPLETADO"),
        _v("investigador → analista", delego_en_orden(t, "investigador", "analista"), f"orden: {t.orden_nodos}"),
        _v(
            "el investigador respaldó los tres tramos (14, 21 y 28)",
            {14.0, 21.0, 28.0} <= valores,
            f"valores: {sorted(valores)}",
        ),
        _v("la respuesta contiene 63", contiene(t, "63"), t.respuesta[:120]),
    ]


def verificar_e5(t: EscenarioTraza) -> list[Verificacion]:
    reparo = any("[REPARACIÓN]" in (d.get("instruccion") or "") for d in t.decisiones)
    return [
        *_base(t, "COMPLETADO"),
        _v(
            "el validador rechazó un resultado del analista (fallos_validacion.analista == 1)",
            t.fallos_validacion.get("analista") == 1,
            f"fallos: {t.fallos_validacion}",
        ),
        _v("el supervisor reenvió el error específico (bucle de reparación)", reparo),
        _v(
            "el analista se invocó 2 veces y quedó aprobado",
            t.intentos.get("analista") == 2 and t.aprobaciones.get("analista") is True,
        ),
        _v("la respuesta final contiene 84", contiene(t, "84"), t.respuesta[:120]),
    ]


def verificar_e6(t: EscenarioTraza) -> list[Verificacion]:
    ajenos = extraer_numeros(t.respuesta) - extraer_numeros(t.pregunta)
    return [
        *_base(t, "INCOMPLETO"),
        _v(
            "no se invocó al analista con datos inexistentes",
            "analista" not in t.orden_nodos,
            f"orden: {t.orden_nodos}",
        ),
        _v("la respuesta no inventa cifras", not ajenos, f"cifras ajenas: {sorted(ajenos)}"),
    ]


def verificar_e7(t: EscenarioTraza) -> list[Verificacion]:
    primero = t.eventos[1]  # el primer evento tras la decisión del supervisor
    return [
        *_base(t, "COMPLETADO"),
        _v(
            "el investigador falló con excepción en el primer intento",
            primero.nodo == "investigador" and "ErrorInyectado" in primero.titulo,
            primero.titulo,
        ),
        _v(
            "el validador la registró como ERROR_ESPECIALISTA y el sistema no se rompió",
            t.fallos_validacion.get("investigador") == 1,
        ),
        _v(
            "el investigador se recuperó en el segundo intento",
            t.intentos.get("investigador") == 2 and t.aprobaciones.get("investigador") is True,
        ),
        _v("la respuesta final contiene 84", contiene(t, "84"), t.respuesta[:120]),
    ]


def verificar_e8(t: EscenarioTraza) -> list[Verificacion]:
    return [
        *_base(t, "REQUIERE_REVISION_HUMANA"),
        _v("termina en revision_humana (no en síntesis)", t.orden_nodos[-1] == "revision_humana"),
        _v(
            "motivo VALIDACION_IRRESOLUBLE:analista",
            t.motivo_escalamiento == "VALIDACION_IRRESOLUBLE:analista",
            str(t.motivo_escalamiento),
        ),
        _v(
            "el analista agotó sus intentos (3) sin bucle infinito",
            t.intentos.get("analista") == 3,
            f"intentos: {t.intentos}",
        ),
        _v(
            "la respuesta pide revisión humana y no presenta el cálculo rechazado como válido",
            contiene(t, "revision humana") and "[analista]" not in t.respuesta,
        ),
    ]


ESCENARIOS: tuple[Escenario, ...] = (
    Escenario(
        "E1",
        "Flujo completo: Investigador → Analista → Síntesis",
        "Topología jerárquica con 2 dominios + síntesis",
        PREGUNTA_VACACIONES,
        verificar_e1,
    ),
    Escenario(
        "E2",
        "Solo cómputo: el supervisor omite al investigador",
        "Ruteo dinámico (pregunta del notebook)",
        PREGUNTA_NOTEBOOK,
        verificar_e2,
    ),
    Escenario(
        "E3",
        "Solo investigación: el supervisor omite al analista",
        "Ruteo dinámico",
        "¿Cuántos días de trabajo remoto y cuántos presenciales por semana tiene el esquema estándar de teletrabajo?",
        verificar_e3,
    ),
    Escenario(
        "E4",
        "Tres tramos de antigüedad en un solo cálculo",
        "Estado compartido: datos validados fluyen al analista",
        "Si en un mismo año tomaran vacaciones tres empleados con 3, 7 y 12 años de antigüedad, "
        "¿cuántos días de vacaciones suman entre los tres?",
        verificar_e4,
    ),
    Escenario(
        "E5",
        "Reparación: el validador rechaza un cálculo inconsistente",
        "Validación + bucle de reparación",
        PREGUNTA_VACACIONES,
        verificar_e5,
        analisis_corrupto=1,
    ),
    Escenario(
        "E6",
        "Sin evidencia: cierre honesto, sin inventar",
        "Camino de fallo: dato inexistente",
        "¿Cuál es el valor del bono de desempeño por trimestre y cuánto suman dos bonos?",
        verificar_e6,
    ),
    Escenario(
        "E7",
        "Un especialista lanza una excepción y se recupera",
        "Camino de fallo: el supervisor no se rompe",
        PREGUNTA_VACACIONES,
        verificar_e7,
        excepcion_especialista={"investigador": 1},
    ),
    Escenario(
        "E8",
        "Validación irresoluble → revisión humana",
        "Condición de parada estricta (anti Supervisor Infinito)",
        PREGUNTA_VACACIONES,
        verificar_e8,
        analisis_corrupto=99,
    ),
)


# ---------------------------------------------------------------- runner
DepsFactory = Callable[[Escenario, Settings], Dependencias]


def deps_reales(escenario: Escenario, settings: Settings) -> Dependencias:
    return construir_dependencias(settings, inyector=escenario.inyector())


async def ejecutar_escenario(
    escenario: Escenario,
    settings: Settings,
    *,
    deps_factory: DepsFactory = deps_reales,
    on_evento: Callable[[EventoTraza], None] | None = None,
) -> EscenarioTraza:
    deps = deps_factory(escenario, settings)
    app = compilar(deps)
    eventos: list[EventoTraza] = []
    final: dict[str, Any] = {}
    inicio = ultimo = time.perf_counter()
    with get_usage_metadata_callback() as cb:
        async for modo, dato in app.astream(
            estado_inicial(escenario.pregunta), configuracion(settings), stream_mode=["updates", "values"]
        ):
            parte = cast(dict[str, Any], dato)  # 'values' → estado completo; 'updates' → {nodo: actualización}
            if modo == "values":
                final = parte
                continue
            for nodo, update in parte.items():
                ahora = time.perf_counter()
                ev = evento_desde_update(nodo, update, len(eventos) + 1, int((ahora - ultimo) * 1000))
                ultimo = ahora
                eventos.append(ev)
                if on_evento:
                    on_evento(ev)
    tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for uso in cb.usage_metadata.values():
        tokens["input_tokens"] += uso.get("input_tokens", 0)
        tokens["output_tokens"] += uso.get("output_tokens", 0)
        tokens["total_tokens"] += uso.get("total_tokens", 0)
    traza = EscenarioTraza(
        id=escenario.id,
        titulo=escenario.titulo,
        criterio=escenario.criterio,
        pregunta=escenario.pregunta,
        eventos=eventos,
        respuesta=final["respuesta_final"] or "",
        estado_final=final["estado_final"],
        motivo_escalamiento=final["motivo_escalamiento"],
        pasos=final["pasos"],
        intentos=final["intentos"],
        fallos_validacion=final["fallos_validacion"],
        aprobaciones=final["aprobaciones"],
        research_output=final["research_output"].model_dump() if final["research_output"] else None,
        analysis_output=final["analysis_output"].model_dump() if final["analysis_output"] else None,
        decisiones=[d.model_dump() for d in final["decisiones"]],
        duracion_ms=int((time.perf_counter() - inicio) * 1000),
        tokens=tokens if tokens["total_tokens"] else {},
    )
    traza.verificaciones = escenario.verificar(traza)
    return traza
