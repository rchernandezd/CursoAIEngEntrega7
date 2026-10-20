"""Contratos Pydantic: artefactos de los especialistas, decisión del supervisor y registros de auditoría.

Todo lo que entra al estado compartido pasa antes por uno de estos modelos (el estado es un contrato,
no un diccionario libre). Los campos son requeridos a propósito: así el esquema JSON que ve el LLM
es el más simple posible y funciona igual con tool calling que con salida estructurada nativa.
"""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

AgenteEspecialista = Literal["investigador", "analista"]
Siguiente = Literal["investigador", "analista", "FINISH"]
SiguienteEfectivo = Literal["investigador", "analista", "FINISH", "ESCALAR"]
EstadoFinal = Literal["COMPLETADO", "INCOMPLETO", "REQUIERE_REVISION_HUMANA"]
Severidad = Literal["error", "advertencia"]
CodigoValidacion = Literal[
    "ERROR_ESPECIALISTA",
    "SIN_ARTEFACTO",
    "EVIDENCIA_AUSENTE",
    "HALLAZGOS_VACIOS",
    "ESTADO_INCONSISTENTE",
    "FUENTE_NO_RECUPERADA",
    "CITA_NO_ENCONTRADA",
    "VALOR_NO_RESPALDADO",
    "SIN_USO_DE_HERRAMIENTA",
    "EXPRESION_INVALIDA",
    "RESULTADO_INCONSISTENTE",
    "RESULTADO_NO_FINITO",
    "OPERANDO_SIN_ORIGEN",
    "OPERANDO_DERIVADO",
    "BAJA_CONFIANZA",
    "SIN_EVIDENCIA",
]


class Fragmento(TypedDict):
    """Un fragmento recuperado de las políticas: nombre de archivo + texto."""

    fuente: str
    contenido: str


# ---------------------------------------------------------------- artefactos de los especialistas
class Hallazgo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    afirmacion: str = Field(min_length=3, max_length=300, description="Hecho encontrado, en una frase.")
    valor: float | None = Field(description="Cifra clave del hecho (ej. 28) o null si no hay cifra.")
    unidad: str | None = Field(description="Unidad de la cifra (ej. 'días corridos por año') o null.")
    fuente: str = Field(description="Nombre EXACTO del archivo, tal como aparece en [Fuente: ...].")
    cita: str = Field(
        min_length=10,
        max_length=400,
        description="Fragmento COPIADO TEXTUALMENTE de la evidencia que respalda el hecho (sin parafrasear).",
    )


class ResearchArtifact(BaseModel):
    """Salida del Investigador. Parcela de escritura: state['research_output']."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    consulta: str = Field(min_length=3, max_length=300, description="Qué se buscó.")
    estado: Literal["completo", "parcial", "sin_evidencia"] = Field(
        description="completo: respondió lo pedido; parcial: faltan datos; sin_evidencia: no hay nada relevante."
    )
    hallazgos: list[Hallazgo] = Field(max_length=8, description="Vacía si estado='sin_evidencia'.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Confianza de 0 a 1.")


class AnalysisArtifact(BaseModel):
    """Salida del Analista. Parcela de escritura: state['analysis_output']."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expresion: str = Field(
        min_length=1, max_length=200, description="Expresión aritmética calculada con la herramienta."
    )
    resultado: float = Field(description="Número devuelto por la calculadora, sin redondear.")
    explicacion: str = Field(min_length=3, max_length=500, description="Cómo se llegó al resultado, en pocas frases.")
    supuestos: list[str] = Field(max_length=5, description="Supuestos hechos; lista vacía si no hubo.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Confianza de 0 a 1.")


# ---------------------------------------------------------------- decisión del supervisor
class Rubrica(BaseModel):
    completitud: bool = Field(description="¿Están respondidas TODAS las partes de la pregunta?")
    respaldo_en_evidencia: bool = Field(description="¿Todo dato de la empresa viene de un hallazgo validado?")
    consistencia: bool = Field(description="¿Los resultados usan los valores de los hallazgos sin contradecirse?")

    @property
    def aprobada(self) -> bool:
        return self.completitud and self.respaldo_en_evidencia and self.consistencia


class DecisionSupervisor(BaseModel):
    """Salida estructurada del LLM supervisor."""

    next: Siguiente = Field(description="Próximo especialista, o FINISH si la tarea está completa.")
    instruccion: str = Field(
        description="Instrucción autocontenida y puntual para el especialista (vacía si next='FINISH')."
    )
    razon: str = Field(description="Justificación breve de la decisión.")
    rubrica: Rubrica


# ---------------------------------------------------------------- validación
class HallazgoValidacion(BaseModel):
    codigo: CodigoValidacion
    severidad: Severidad
    mensaje: str


class ResultadoValidacion(BaseModel):
    agente: AgenteEspecialista
    hallazgos: list[HallazgoValidacion] = Field(default_factory=list)

    @property
    def errores(self) -> list[HallazgoValidacion]:
        return [h for h in self.hallazgos if h.severidad == "error"]

    @property
    def advertencias(self) -> list[HallazgoValidacion]:
        return [h for h in self.hallazgos if h.severidad == "advertencia"]

    @property
    def ok(self) -> bool:
        return not self.errores


# ---------------------------------------------------------------- bitácoras (append-only)
class Contribucion(BaseModel):
    """Quién aportó qué. Se acumula con operator.add: nunca se pisa."""

    agente: Literal["investigador", "analista", "validador"]
    intento: int
    ok: bool | None
    resumen: str
    detalle: dict[str, Any] = Field(default_factory=dict)


class RegistroDecision(BaseModel):
    """Quién decidió qué y por qué (log de auditoría del supervisor)."""

    paso: int
    origen: Literal["llm", "guardrail"]
    siguiente: SiguienteEfectivo
    razon: str
    instruccion: str
    rubrica: Rubrica | None = None
    sobrescrituras: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- Human-in-the-loop (Entrega 7)
TipoAccion = Literal["notificar_rrhh"]
MotivoCriticidad = Literal["EFECTO_SECUNDARIO"]
EstadoAccion = Literal["EJECUTADA", "RECHAZADA"]


class AccionPropuesta(BaseModel):
    """Acción con efectos secundarios que el sistema propone y que NO se ejecuta sin aprobación humana."""

    model_config = ConfigDict(extra="forbid")

    tipo: TipoAccion
    motivos: list[MotivoCriticidad]
    resumen: str = Field(description="Qué se va a hacer, en una frase legible para el revisor.")
    destinatario: str
    asunto: str
    cuerpo: str


class DecisionHumana(BaseModel):
    """Lo que devuelve el revisor al reanudar el grafo (valor de `Command(resume=...)`)."""

    model_config = ConfigDict(extra="forbid")

    aprobado: bool
    revisor: str | None = Field(default=None, max_length=80)
    comentario: str | None = Field(default=None, max_length=500)


class ResultadoAccion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estado: EstadoAccion
    detalle: str
    accion_id: str | None = None
