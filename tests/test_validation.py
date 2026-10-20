from schemas import AnalysisArtifact, Fragmento, Hallazgo, ResearchArtifact
from tools.retrieval import CORPUS
from validation import es_derivable, extraer_numeros, verificar_analisis, verificar_investigacion

EVID = [Fragmento(fuente="politica_vacaciones.txt", contenido=CORPUS["politica_vacaciones.txt"])]
CITA = "Los empleados con más de 10 años acceden a 28 días"


def research(**kw: object) -> ResearchArtifact:
    h = dict(
        afirmacion="Más de 10 años: 28 días", valor=28.0, unidad="días", fuente="politica_vacaciones.txt", cita=CITA
    )
    h.update({k: v for k, v in kw.items() if k in h})
    return ResearchArtifact(
        consulta="vacaciones",
        estado=str(kw.get("estado", "completo")),  # type: ignore[arg-type]
        hallazgos=kw.get("hallazgos", [Hallazgo(**h)]),  # type: ignore[arg-type]
        confidence_score=float(kw.get("conf", 0.9)),  # type: ignore[arg-type]
    )


def codigos(r: object) -> list[str]:
    return [h.codigo for h in r.hallazgos]  # type: ignore[attr-defined]


def test_investigacion_valida() -> None:
    r = verificar_investigacion(research(), EVID, None)
    assert r.ok and r.hallazgos == []


def test_investigacion_errores_especificos() -> None:
    assert codigos(verificar_investigacion(research(fuente="otra.txt"), EVID, None)) == ["FUENTE_NO_RECUPERADA"]
    assert codigos(
        verificar_investigacion(research(cita="Los empleados tienen 30 días de vacaciones"), EVID, None)
    ) == ["CITA_NO_ENCONTRADA"]
    assert codigos(verificar_investigacion(research(valor=30.0), EVID, None)) == ["VALOR_NO_RESPALDADO"]
    assert codigos(verificar_investigacion(research(), [], None)) == ["EVIDENCIA_AUSENTE"]
    assert codigos(verificar_investigacion(research(estado="completo", hallazgos=[]), EVID, None)) == [
        "HALLAZGOS_VACIOS"
    ]
    assert codigos(verificar_investigacion(None, EVID, None)) == ["SIN_ARTEFACTO"]
    assert codigos(verificar_investigacion(None, EVID, "boom")) == ["ERROR_ESPECIALISTA"]


def test_investigacion_sin_evidencia_es_valida_con_advertencia() -> None:
    r = verificar_investigacion(research(estado="sin_evidencia", hallazgos=[]), [], None)
    assert r.ok and codigos(r) == ["SIN_EVIDENCIA"] and r.advertencias


def test_cita_tolera_tildes_mayusculas_y_signos() -> None:
    r = verificar_investigacion(research(cita="los empleados con MAS de 10 anos acceden a 28 dias!"), EVID, None)
    assert r.ok


def an(expr: str = "28*3", res: float = 84, conf: float = 0.9) -> AnalysisArtifact:
    return AnalysisArtifact(expresion=expr, resultado=res, explicacion="ok ok", supuestos=[], confidence_score=conf)


def test_analisis_valido_y_errores() -> None:
    kw = dict(
        calculos_tool=["Resultado: 84"],
        numeros_permitidos={28.0, 3.0, 12.0},
        error_ejecucion=None,
        operandos_estrictos=False,
    )
    assert verificar_analisis(an(), **kw).ok  # type: ignore[arg-type]
    r = verificar_analisis(an(res=85), **kw)  # type: ignore[arg-type]
    assert codigos(r) == ["RESULTADO_INCONSISTENTE"] and not r.ok
    assert "da 84" in r.errores[0].mensaje and "85" in r.errores[0].mensaje
    assert codigos(verificar_analisis(an(), **{**kw, "calculos_tool": []})) == ["SIN_USO_DE_HERRAMIENTA"]  # type: ignore[arg-type]
    assert codigos(verificar_analisis(an("os.system('x')"), **kw)) == ["EXPRESION_INVALIDA"]  # type: ignore[arg-type]
    assert codigos(verificar_analisis(None, **kw)) == ["SIN_ARTEFACTO"]  # type: ignore[arg-type]
    assert codigos(verificar_analisis(an(), **{**kw, "error_ejecucion": "x"})) == ["ERROR_ESPECIALISTA"]  # type: ignore[arg-type]


def test_operandos_origen_derivado_y_estricto() -> None:
    permitidos = {12.0, 5.0, 14.0, 21.0, 28.0}
    kw = dict(calculos_tool=["Resultado: 231"], error_ejecucion=None)
    notebook = an("5*14 + 5*21 + 2*28", 231)
    r = verificar_analisis(notebook, numeros_permitidos=permitidos, operandos_estrictos=True, **kw)  # type: ignore[arg-type]
    assert r.ok and codigos(r) == ["OPERANDO_DERIVADO"]  # 2 = 12 - 5 - 5
    alucinado = an("25*3", 75)
    blando = verificar_analisis(alucinado, numeros_permitidos={28.0, 3.0}, operandos_estrictos=False, **kw)  # type: ignore[arg-type]
    assert blando.ok  # 25 = 28 - 3 es derivable → solo advierte
    duro = verificar_analisis(an("97*3", 291), numeros_permitidos={28.0, 3.0}, operandos_estrictos=True, **kw)  # type: ignore[arg-type]
    assert not duro.ok and codigos(duro) == ["OPERANDO_SIN_ORIGEN"]
    assert verificar_analisis(an("97*3", 291), numeros_permitidos={28.0, 3.0}, operandos_estrictos=False, **kw).ok  # type: ignore[arg-type]


def test_numeros_y_derivables() -> None:
    assert extraer_numeros("14.500 pedidos, 3,5 días y 28") >= {14500.0, 3.5, 28.0}
    assert es_derivable(2, [12, 5]) and es_derivable(84, [28, 3]) and not es_derivable(97, [28, 3])
