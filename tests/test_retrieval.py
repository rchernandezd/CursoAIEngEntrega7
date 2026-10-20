from schemas import Fragmento
from tools.retrieval import (
    CORPUS,
    LocalBM25Retriever,
    crear_herramienta_busqueda,
    normalizar_texto,
    parsear_evidencia,
    tokenizar,
)


async def test_local_recupera_la_politica_correcta() -> None:
    r = LocalBM25Retriever()
    casos = {
        "días de vacaciones por antigüedad": "politica_vacaciones.txt",
        "cuántos días de trabajo remoto y presencial por semana": "politica_teletrabajo.txt",
        "cada cuánto se renuevan las contraseñas": "politica_seguridad_informatica.txt",
        "cuánto dura el onboarding": "onboarding_nuevos_empleados.txt",
    }
    for consulta, esperado in casos.items():
        assert (await r.buscar(consulta, 3))[0]["fuente"] == esperado, consulta


async def test_local_sin_coincidencias_devuelve_vacio() -> None:
    assert await LocalBM25Retriever().buscar("valor del bono de desempeño pagado por la empresa", 5) == []


async def test_herramienta_formato_y_parseo() -> None:
    tool = crear_herramienta_busqueda(LocalBM25Retriever(), k=2)
    salida = await tool.ainvoke({"pregunta": "días de vacaciones por antigüedad"})
    assert salida.startswith("[Fuente: politica_vacaciones.txt] Política de Vacaciones")
    frag = parsear_evidencia(salida)
    assert frag[0] == Fragmento(fuente="politica_vacaciones.txt", contenido=CORPUS["politica_vacaciones.txt"])
    assert (
        await tool.ainvoke({"pregunta": "bono de desempeño trimestral"})
        == "No se encontró información relevante en las políticas."
    )
    assert "(no inventar datos" in (tool.description or "")


def test_normalizacion_y_tokens() -> None:
    assert normalizar_texto("  ¡Más de 10 AÑOS!  ") == "mas de 10 anos"
    assert tokenizar("Los empleados de TechCorp") == ["empleado"]
