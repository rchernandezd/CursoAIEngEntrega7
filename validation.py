"""Validación determinística de los artefactos (código, no juicio del modelo).

Son funciones puras: reciben el artefacto y la evidencia, devuelven un ResultadoValidacion. Los mensajes de
error son específicos y accionables porque se reenvían tal cual al especialista en el bucle de reparación.
"""

import itertools
import math
import re
from collections.abc import Iterable, Sequence

from exceptions import ExpresionNoPermitidaError
from schemas import (
    AnalysisArtifact,
    CodigoValidacion,
    Fragmento,
    HallazgoValidacion,
    ResearchArtifact,
    ResultadoValidacion,
    Severidad,
)
from tools.calculator import evaluar_expresion, literales_numericos
from tools.retrieval import normalizar_texto

_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
_MILES_PUNTO = re.compile(r"\d{1,3}(?:\.\d{3})+")
CONFIANZA_MINIMA = 0.3


def extraer_numeros(texto: str) -> set[float]:
    """Números de un texto, tolerando '14.500' (miles) y '3,5' (decimal). Devuelve todas las lecturas posibles."""
    numeros: set[float] = set()
    for token in _NUMERO.findall(texto):
        if _MILES_PUNTO.fullmatch(token):
            numeros.add(float(token.replace(".", "")))  # 14.500 → 14500
        partes = re.split(r"[.,]", token)
        if len(partes) == 1:
            numeros.add(float(token))  # entero
        elif len(partes) == 2:
            numeros.add(float(f"{partes[0]}.{partes[1]}"))  # 3,5 → 3.5 (y 14.500 → 14.5, lectura alternativa)
    return numeros


def es_derivable(n: float, conocidos: Iterable[float]) -> bool:
    """¿Se obtiene `n` combinando hasta 3 números conocidos con +, -, *, /? (p. ej. 2 = 12 - 5 - 5)."""
    base = sorted(set(conocidos))

    def cerca(x: float) -> bool:
        return math.isclose(x, n, rel_tol=1e-9, abs_tol=1e-9)

    if any(cerca(a) for a in base):
        return True
    for a, b in itertools.product(base, repeat=2):
        if cerca(a + b) or cerca(a - b) or cerca(a * b) or (b != 0 and cerca(a / b)):
            return True
    for a, b, c in itertools.product(base, repeat=3):
        if cerca(a - b - c) or cerca(a + b - c) or cerca(a + b + c) or cerca(a * b - c) or cerca(a * b + c):
            return True
    return False


def _h(codigo: CodigoValidacion, severidad: Severidad, mensaje: str) -> HallazgoValidacion:
    return HallazgoValidacion(codigo=codigo, severidad=severidad, mensaje=mensaje)


def verificar_investigacion(
    artefacto: ResearchArtifact | None,
    evidencia: Sequence[Fragmento],
    error_ejecucion: str | None,
) -> ResultadoValidacion:
    """Reglas: el investigador solo puede afirmar lo que la herramienta devolvió (fuente, cita y cifra)."""
    r = ResultadoValidacion(agente="investigador")
    if error_ejecucion:
        r.hallazgos.append(_h("ERROR_ESPECIALISTA", "error", f"El investigador falló al ejecutarse: {error_ejecucion}"))
        return r
    if artefacto is None:
        r.hallazgos.append(_h("SIN_ARTEFACTO", "error", "No devolvió el artefacto estructurado (ResearchArtifact)."))
        return r

    if artefacto.estado == "sin_evidencia":
        if artefacto.hallazgos:
            r.hallazgos.append(
                _h("ESTADO_INCONSISTENTE", "error", "estado='sin_evidencia' pero incluye hallazgos; vaciá la lista.")
            )
        else:
            r.hallazgos.append(_h("SIN_EVIDENCIA", "advertencia", "La búsqueda no encontró información relevante."))
        return r

    if not artefacto.hallazgos:
        r.hallazgos.append(_h("HALLAZGOS_VACIOS", "error", f"estado='{artefacto.estado}' pero 'hallazgos' está vacío."))
        return r
    if not evidencia:
        r.hallazgos.append(
            _h(
                "EVIDENCIA_AUSENTE",
                "error",
                "No hay evidencia recuperada: usá la herramienta buscar_en_politicas_internas antes de afirmar datos.",
            )
        )
        return r

    texto_por_fuente: dict[str, str] = {}
    for f in evidencia:
        texto_por_fuente[f["fuente"]] = (texto_por_fuente.get(f["fuente"], "") + " " + f["contenido"]).strip()

    for i, h in enumerate(artefacto.hallazgos, start=1):
        if h.fuente not in texto_por_fuente:
            r.hallazgos.append(
                _h(
                    "FUENTE_NO_RECUPERADA",
                    "error",
                    f"Hallazgo {i}: la fuente '{h.fuente}' no está entre las recuperadas "
                    f"({', '.join(sorted(texto_por_fuente))}).",
                )
            )
            continue
        if normalizar_texto(h.cita) not in normalizar_texto(texto_por_fuente[h.fuente]):
            r.hallazgos.append(
                _h(
                    "CITA_NO_ENCONTRADA",
                    "error",
                    f"Hallazgo {i}: la cita '{h.cita[:80]}' no aparece textualmente en '{h.fuente}'. "
                    "Copiá el fragmento tal cual, sin parafrasear ni usar puntos suspensivos.",
                )
            )
            continue
        if h.valor is not None and not any(math.isclose(h.valor, n, abs_tol=1e-9) for n in extraer_numeros(h.cita)):
            r.hallazgos.append(
                _h(
                    "VALOR_NO_RESPALDADO",
                    "error",
                    f"Hallazgo {i}: el valor {h.valor:g} no figura en la cita '{h.cita[:80]}'.",
                )
            )
    if artefacto.confidence_score < CONFIANZA_MINIMA:
        r.hallazgos.append(
            _h(
                "BAJA_CONFIANZA",
                "advertencia",
                f"confidence_score={artefacto.confidence_score:.2f} < {CONFIANZA_MINIMA}",
            )
        )
    return r


def verificar_analisis(
    artefacto: AnalysisArtifact | None,
    *,
    calculos_tool: Sequence[str],
    numeros_permitidos: set[float],
    error_ejecucion: str | None,
    operandos_estrictos: bool,
) -> ResultadoValidacion:
    """Reglas: usó la calculadora, expresión válida, resultado coincide al recalcular y operandos con origen."""
    r = ResultadoValidacion(agente="analista")
    if error_ejecucion:
        r.hallazgos.append(_h("ERROR_ESPECIALISTA", "error", f"El analista falló al ejecutarse: {error_ejecucion}"))
        return r
    if artefacto is None:
        r.hallazgos.append(_h("SIN_ARTEFACTO", "error", "No devolvió el artefacto estructurado (AnalysisArtifact)."))
        return r
    if not calculos_tool:
        r.hallazgos.append(
            _h(
                "SIN_USO_DE_HERRAMIENTA",
                "error",
                "No llamó a la herramienta 'calculadora': no se aceptan cálculos de memoria.",
            )
        )
        return r

    try:
        recalculado = float(evaluar_expresion(artefacto.expresion))
    except (ExpresionNoPermitidaError, ArithmeticError) as exc:
        r.hallazgos.append(
            _h("EXPRESION_INVALIDA", "error", f"La expresión '{artefacto.expresion}' no es válida: {exc}")
        )
        return r
    if not math.isfinite(recalculado) or not math.isfinite(artefacto.resultado):
        r.hallazgos.append(_h("RESULTADO_NO_FINITO", "error", "El resultado no es un número finito."))
        return r
    if not math.isclose(recalculado, artefacto.resultado, rel_tol=1e-9, abs_tol=1e-9):
        r.hallazgos.append(
            _h(
                "RESULTADO_INCONSISTENTE",
                "error",
                f"La expresión '{artefacto.expresion}' da {recalculado:g}, pero reportaste resultado="
                f"{artefacto.resultado:g}. Reportá exactamente el valor que devuelve la calculadora.",
            )
        )

    sev: Severidad = "error" if operandos_estrictos else "advertencia"
    for n in sorted(set(literales_numericos(artefacto.expresion))):
        if any(math.isclose(n, p, abs_tol=1e-9) for p in numeros_permitidos):
            continue
        if es_derivable(n, numeros_permitidos):
            r.hallazgos.append(
                _h(
                    "OPERANDO_DERIVADO",
                    "advertencia",
                    f"El operando {n:g} no figura literalmente, pero se deriva de otros datos.",
                )
            )
        else:
            r.hallazgos.append(
                _h(
                    "OPERANDO_SIN_ORIGEN",
                    sev,
                    f"El operando {n:g} no aparece en la pregunta, la instrucción ni los hallazgos validados.",
                )
            )
    if artefacto.confidence_score < CONFIANZA_MINIMA:
        r.hallazgos.append(
            _h(
                "BAJA_CONFIANZA",
                "advertencia",
                f"confidence_score={artefacto.confidence_score:.2f} < {CONFIANZA_MINIMA}",
            )
        )
    return r
