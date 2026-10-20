from typing import Any

from config import Settings
from faults import InyectorFallas
from graph import compilar, configuracion
from state import estado_inicial
from tests.conftest import (
    FabricaDeps,
    FakeToolCallingModel,
    decision,
    guion_analista,
    guion_investigador,
    tool_call,
)

PREGUNTA = "Un empleado lleva 12 años en TechCorp. ¿Cuántos días de vacaciones tiene por año y cuántos suma en 3 años?"


async def correr(deps: Any, pregunta: str = PREGUNTA) -> tuple[dict[str, Any], list[str]]:
    app = compilar(deps)
    orden: list[str] = []
    final: dict[str, Any] = {}
    async for modo, dato in app.astream(
        estado_inicial(pregunta), configuracion(deps.settings), stream_mode=["updates", "values"]
    ):
        if modo == "updates":
            orden.extend(dato.keys())
        else:
            final = dato
    return final, orden


async def test_e1_flujo_completo(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[
            decision("investigador", "Buscar vacaciones >10 años"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
        ]
    )
    est, orden = await correr(deps)
    assert orden == [
        "supervisor",
        "investigador",
        "validador",
        "supervisor",
        "analista",
        "validador",
        "supervisor",
        "sintesis",
    ]
    assert est["estado_final"] == "COMPLETADO" and est["aprobaciones"] == {"investigador": True, "analista": True}
    assert est["pasos"] == 2 and est["analysis_output"].resultado == 84
    assert [d.siguiente for d in est["decisiones"]] == ["investigador", "analista", "FINISH"]
    assert est["respuesta_final"] == "Respuesta final de prueba."


async def test_e2_solo_computo_omite_al_investigador(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[decision("analista", "Calcular 5*14 + 5*21 + 2*28"), decision("FINISH", ok=True)],
        analista=guion_analista("5*14 + 5*21 + 2*28", 231),
    )
    est, orden = await correr(deps, "Tuvo 14 días 5 años, 21 los siguientes 5 y 28 ahora. ¿Total en 12 años?")
    assert (
        "investigador" not in orden and est["estado_final"] == "COMPLETADO" and est["analysis_output"].resultado == 231
    )


async def test_e3_solo_investigacion(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(supervisor=[decision("investigador", "Buscar vacaciones"), decision("FINISH", ok=True)])
    est, orden = await correr(deps)
    assert "analista" not in orden and est["estado_final"] == "COMPLETADO"


async def test_e5_reparacion_por_resultado_inconsistente(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[
            decision("investigador", "Buscar"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
            decision("FINISH", ok=True),
        ],
        analista=guion_analista() + guion_analista(),
        inyector=InyectorFallas(analisis_corrupto=1),
    )
    est, _ = await correr(deps)
    # el LLM quiso cerrar (FINISH) con un resultado rechazado: el guardrail lo forzó a reparar
    assert est["decisiones"][2].origen == "guardrail" and est["decisiones"][2].sobrescrituras == [
        "REPARACION_FORZADA:analista"
    ]
    assert est["decisiones"][2].siguiente == "analista" and "[REPARACIÓN]" in est["decisiones"][2].instruccion
    assert "RESULTADO_INCONSISTENTE" in est["decisiones"][2].instruccion
    assert est["fallos_validacion"] == {"analista": 1} and est["intentos"]["analista"] == 2
    assert est["aprobaciones"]["analista"] is True and est["estado_final"] == "COMPLETADO"
    assert est["analysis_output"].resultado == 84


async def test_e6_sin_evidencia_cierra_honestamente(fabricar_deps: FabricaDeps) -> None:
    sin = [
        tool_call("buscar_en_politicas_internas", {"pregunta": "bono de desempeño"}, "b1"),
        tool_call(
            "ResearchArtifact",
            {"consulta": "bono de desempeño", "estado": "sin_evidencia", "hallazgos": [], "confidence_score": 0.8},
            "b2",
        ),
    ]
    deps = fabricar_deps(
        supervisor=[decision("investigador", "Buscar bono"), decision("FINISH", ok=True)], investigador=sin
    )
    est, orden = await correr(deps, "¿Cuál es el bono de desempeño?")
    assert (
        "analista" not in orden and est["estado_final"] == "INCOMPLETO" and est["aprobaciones"]["investigador"] is True
    )


async def test_e7_excepcion_del_especialista_se_recupera(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[
            decision("investigador", "Buscar"),
            decision("investigador", "Buscar"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
        ],
        inyector=InyectorFallas(excepcion_especialista={"investigador": 1}),
    )
    est, _ = await correr(deps)
    assert est["fallos_validacion"] == {"investigador": 1} and est["intentos"]["investigador"] == 2
    assert est["contribuciones"][0].ok is False and "ErrorInyectado" in est["contribuciones"][0].resumen
    assert est["estado_final"] == "COMPLETADO"


async def test_e8_validacion_irresoluble_escala_a_revision_humana(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[decision("investigador", "Buscar"), decision("analista", "Calcular 28*3")]
        + [decision("analista", "Calcular 28*3")] * 5,
        analista=guion_analista() * 4,
        inyector=InyectorFallas(analisis_corrupto=99),
    )
    est, orden = await correr(deps)
    assert orden[-1] == "revision_humana" and "sintesis" not in orden
    assert (
        est["estado_final"] == "REQUIERE_REVISION_HUMANA"
        and est["motivo_escalamiento"] == "VALIDACION_IRRESOLUBLE:analista"
    )
    assert est["intentos"]["analista"] == 3 and est["fallos_validacion"]["analista"] == 3
    assert "revisión humana" in est["respuesta_final"] and "RESULTADO_INCONSISTENTE" in est["respuesta_final"]


async def test_tope_de_pasos_corta_el_supervisor_infinito(fabricar_deps: FabricaDeps, settings: Settings) -> None:
    chico = Settings.from_env(
        **{
            **{k: getattr(settings, k) for k in ("traces_dir", "retriever_backend")},
            "openai_api_key": "",
            "max_pasos": 3,
            "recursion_limit": 20,
        }
    )
    distintas = [decision("investigador", f"Buscar vacaciones variante {i}") for i in range(20)]
    deps = fabricar_deps(supervisor=distintas, investigador=guion_investigador() * 10, settings_=chico)
    est, orden = await correr(deps)  # no lanza GraphRecursionError
    assert est["estado_final"] == "REQUIERE_REVISION_HUMANA" and est["motivo_escalamiento"] == "LIMITE_DE_PASOS"
    assert est["pasos"] == 3 and orden[-1] == "revision_humana"


async def test_repeticion_sin_progreso_cierra(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[decision("investigador", "Buscar vacaciones"), decision("investigador", "buscar  VACACIONES!")]
    )
    est, _ = await correr(deps)
    assert (
        est["decisiones"][1].sobrescrituras == ["REPETICION_SIN_PROGRESO"]
        and est["decisiones"][1].siguiente == "FINISH"
    )
    assert est["estado_final"] == "INCOMPLETO"  # rúbrica del LLM en False


async def test_finish_sin_trabajo_se_redirige_al_investigador(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(supervisor=[decision("FINISH", ok=True), decision("FINISH", ok=True)])
    est, orden = await correr(deps)
    assert est["decisiones"][0].sobrescrituras == ["FINISH_SIN_TRABAJO"] and orden[1] == "investigador"
    assert est["decisiones"][0].instruccion == PREGUNTA


async def test_reparacion_del_investigador_por_cita_inventada(fabricar_deps: FabricaDeps) -> None:
    mala = guion_investigador(cita="Los empleados tienen 30 días de vacaciones", valor=30)
    deps = fabricar_deps(
        supervisor=[
            decision("investigador", "Buscar"),
            decision("investigador", "Buscar"),
            decision("FINISH", ok=True),
        ],
        investigador=mala + guion_investigador(),
    )
    est, _ = await correr(deps)
    assert est["intentos"]["investigador"] == 2 and est["fallos_validacion"] == {"investigador": 1}
    assert "CITA_NO_ENCONTRADA" in est["decisiones"][1].instruccion and est["estado_final"] == "COMPLETADO"


async def test_supervisor_llm_caido_escala_sin_romper(fabricar_deps: FabricaDeps) -> None:
    class SupervisorRoto(FakeToolCallingModel):
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
            def boom(_: Any) -> Any:
                raise RuntimeError("503 proveedor caído")

            from langchain_core.runnables import RunnableLambda

            return RunnableLambda(boom)

    deps = fabricar_deps(supervisor=[decision("FINISH")])
    deps = type(deps)(**{**{f: getattr(deps, f) for f in deps.__slots__}, "llm_supervisor": SupervisorRoto()})
    est, _ = await correr(deps)
    assert est["estado_final"] == "REQUIERE_REVISION_HUMANA" and est["motivo_escalamiento"] == "ERROR_SUPERVISOR"


async def test_contexto_acotado_el_analista_no_ve_la_conversacion(fabricar_deps: FabricaDeps) -> None:
    deps = fabricar_deps(
        supervisor=[
            decision("investigador", "Buscar vacaciones"),
            decision("analista", "Calcular 28*3"),
            decision("FINISH", ok=True),
        ]
    )
    await correr(deps)
    primera = deps.llm_analista.recibidos[0]  # type: ignore[attr-defined]
    humanos = [m for m in primera if m.type == "human"]
    assert len(humanos) == 1
    texto = str(humanos[0].content)
    assert texto.startswith("Instrucción: Calcular 28*3") and "Datos validados disponibles" in texto
    assert PREGUNTA not in texto  # no recibe la pregunta original ni el historial
    inv = deps.llm_investigador.recibidos[0]  # type: ignore[attr-defined]
    assert PREGUNTA not in str([m.content for m in inv if m.type == "human"])


def test_el_grafo_dibuja_la_topologia(fabricar_deps: FabricaDeps) -> None:
    mmd = compilar(fabricar_deps(supervisor=[decision("FINISH")])).get_graph().draw_mermaid()
    for arista in (
        "__start__ --> supervisor",
        "investigador --> validador",
        "analista --> validador",
        "validador --> supervisor",
        "sintesis --> __end__",
        "revision_humana --> __end__",
    ):
        assert arista in mmd, arista
    for destino in ("investigador", "analista", "sintesis", "revision_humana"):
        assert f"supervisor -.-> {destino}" in mmd, destino
