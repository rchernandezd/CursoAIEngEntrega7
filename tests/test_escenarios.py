from langchain_core.messages import AIMessage

from config import Settings
from escenarios import ESCENARIOS, Escenario, ejecutar_escenario
from graph import Dependencias
from tests.conftest import (
    FakeToolCallingModel,
    decision,
    guion_analista,
    guion_investigador,
    tool_call,
)
from tools.retrieval import LocalBM25Retriever
from tracing import EscenarioTraza, TrazaEjecucion, guardar_traza, render_escenario

CITA_TELE = "El esquema estándar es de 3 días de trabajo remoto y 2 días presenciales por semana"


def _inv(hallazgos: list[dict], consulta: str = "consulta") -> list[AIMessage]:
    return [
        tool_call("buscar_en_politicas_internas", {"pregunta": consulta}, "b1"),
        tool_call(
            "ResearchArtifact",
            {"consulta": consulta, "estado": "completo", "hallazgos": hallazgos, "confidence_score": 0.9},
            "b2",
        ),
    ]


def _h(valor: float, cita: str, fuente: str = "politica_vacaciones.txt") -> dict:
    return {"afirmacion": f"dato {valor:g}", "valor": valor, "unidad": "días", "fuente": fuente, "cita": cita}


SIN_EVIDENCIA = [
    tool_call("buscar_en_politicas_internas", {"pregunta": "bono de desempeño"}, "b1"),
    tool_call(
        "ResearchArtifact",
        {"consulta": "bono de desempeño", "estado": "sin_evidencia", "hallazgos": [], "confidence_score": 0.8},
        "b2",
    ),
]

# id → (decisiones del supervisor, guion investigador, guion analista, texto de síntesis)
GUIONES = {
    "E1": (
        [
            decision("investigador", "Buscar días de vacaciones con más de 10 años"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
        ],
        guion_investigador(),
        guion_analista(),
        "Le corresponden 28 días por año; en 3 años suma 84 (28*3).",
    ),
    "E2": (
        [decision("analista", "Calcular 5*14 + 5*21 + 2*28"), decision("FINISH", ok=True)],
        None,
        guion_analista("5*14 + 5*21 + 2*28", 231),
        "Acumuló 231 días.",
    ),
    "E3": (
        [decision("investigador", "Buscar el esquema estándar de teletrabajo"), decision("FINISH", ok=True)],
        _inv(
            [_h(3, CITA_TELE, "politica_teletrabajo.txt"), _h(2, CITA_TELE, "politica_teletrabajo.txt")],
            "esquema teletrabajo",
        ),
        None,
        "3 días remotos y 2 presenciales por semana.",
    ),
    "E4": (
        [
            decision("investigador", "Buscar días de vacaciones por tramo"),
            decision("analista", "Calcular 14+21+28"),
            decision("FINISH", ok=True),
        ],
        _inv(
            [
                _h(14, "Los empleados con menos de 5 años de antigüedad acceden a 14 días corridos de vacaciones"),
                _h(21, "Los empleados con 5 a 10 años acceden a 21 días"),
                _h(28, "Los empleados con más de 10 años acceden a 28 días"),
            ],
            "vacaciones por antigüedad",
        ),
        guion_analista("14+21+28", 63),
        "Suman 63 días entre los tres.",
    ),
    "E5": (
        [
            decision("investigador", "Buscar"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
            decision("FINISH", ok=True),
        ],
        guion_investigador(),
        guion_analista() + guion_analista(),
        "28 por año; 84 en 3 años.",
    ),
    "E6": (
        [decision("investigador", "Buscar el bono de desempeño"), decision("FINISH", ok=True)],
        SIN_EVIDENCIA,
        None,
        "No encontré información sobre el bono en las políticas.",
    ),
    "E7": (
        [
            decision("investigador", "Buscar"),
            decision("investigador", "Buscar"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
        ],
        guion_investigador(),
        guion_analista(),
        "28 por año; 84 en 3 años.",
    ),
    "E8": (
        [decision("investigador", "Buscar"), decision("analista", "Calcular 28*3")]
        + [decision("analista", "Calcular 28*3")] * 5,
        guion_investigador(),
        guion_analista() * 4,
        "no se usa",
    ),
}


def deps_fake(esc: Escenario, settings: Settings) -> Dependencias:
    sup, inv, ana, sint = GUIONES[esc.id]
    return Dependencias(
        settings=settings,
        llm_supervisor=FakeToolCallingModel(decisiones=sup),
        llm_investigador=FakeToolCallingModel(guion=inv or guion_investigador()),
        llm_analista=FakeToolCallingModel(guion=ana or guion_analista()),
        llm_sintesis=FakeToolCallingModel(guion=[AIMessage(content=sint)]),
        retriever=LocalBM25Retriever(),
        inyector=esc.inyector(),
    )


def test_hay_8_escenarios_con_ids_unicos() -> None:
    assert [e.id for e in ESCENARIOS] == [f"E{i}" for i in range(1, 9)]
    assert ESCENARIOS[4].analisis_corrupto == 1 and ESCENARIOS[6].excepcion_especialista == {"investigador": 1}
    assert ESCENARIOS[7].analisis_corrupto == 99


async def test_los_8_escenarios_pasan_todas_sus_verificaciones_con_fakes(settings: Settings) -> None:
    for esc in ESCENARIOS:
        traza = await ejecutar_escenario(esc, settings, deps_factory=deps_fake)
        fallidas = [v.descripcion for v in traza.verificaciones if not v.ok]
        assert not fallidas, f"{esc.id}: {fallidas}"


async def test_una_verificacion_falla_cuando_el_flujo_es_incorrecto(settings: Settings) -> None:
    # E1 pero el supervisor manda directo al analista (sin investigar): debe fallar la verificación de orden
    esc = ESCENARIOS[0]
    _, _, ana, sint = GUIONES["E1"]
    mal = {**GUIONES, "E1": ([decision("analista", "Calcular 28*3"), decision("FINISH", ok=True)], None, ana, sint)}

    def fabrica(e: Escenario, s: Settings) -> Dependencias:
        d = deps_fake(e, s)
        return Dependencias(
            **{
                **{f: getattr(d, f) for f in d.__slots__},
                "llm_supervisor": FakeToolCallingModel(decisiones=mal["E1"][0]),
            }
        )

    traza = await ejecutar_escenario(esc, settings, deps_factory=fabrica)
    ok = {v.descripcion: v.ok for v in traza.verificaciones}
    assert ok["el supervisor delega primero al investigador y luego al analista"] is False


async def test_render_y_round_trip_de_la_traza(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    t1 = await ejecutar_escenario(ESCENARIOS[0], settings, deps_factory=deps_fake)
    texto = render_escenario(t1)
    for esperado in [
        "=== E1 · Flujo completo",
        "[1] supervisor decide → investigador (llm)",
        "razón:",
        "instrucción:",
        "rúbrica:",
        "herramienta: buscar_en_politicas_internas(",
        "validador ✔ investigador aprobado",
        "herramienta: calculadora(",
        'Respuesta final: "Le corresponden 28 días',
        "(COMPLETADO ·",
        "✔ el analista usó la calculadora",
    ]:
        assert esperado in texto, esperado
    traza = TrazaEjecucion(metadata={"proveedor": "fake"}, escenarios=[t1], resumen={"escenarios": 1})
    rj, rl = guardar_traza(traza, tmp_path / "traces")
    assert TrazaEjecucion.model_validate_json(rj.read_text(encoding="utf-8")).escenarios[0].id == "E1"
    assert "Le corresponden 28 días" in rl.read_text(encoding="utf-8")  # UTF-8 correcto
    print(texto)
    assert isinstance(t1, EscenarioTraza)
