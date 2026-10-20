import pytest

from exceptions import ExpresionNoPermitidaError
from tools.calculator import crear_calculadora, evaluar_expresion, formatear_numero, literales_numericos


def test_evalua_aritmetica_del_notebook() -> None:
    assert evaluar_expresion("5*14 + 5*21 + 2*28") == 231
    assert evaluar_expresion("(12-5-5)*28") == 56
    assert evaluar_expresion("-3 + 10 / 4") == -0.5
    assert evaluar_expresion("7 // 2 + 7 % 2 + 2**3") == 12


@pytest.mark.parametrize(
    "malicioso",
    [
        "__import__('os').system('ls')",
        "abs(3)",
        "a + 1",
        "'a' * 3",
        "True + 1",
        "9**9**9",
        "[1, 2]",
        "(lambda: 1)()",
        "",
    ],
)
def test_rechaza_todo_lo_que_no_es_aritmetica(malicioso: str) -> None:
    with pytest.raises(ExpresionNoPermitidaError):
        evaluar_expresion(malicioso)


def test_herramienta_devuelve_texto_y_no_lanza() -> None:
    calc = crear_calculadora()
    assert calc.invoke({"expresion": "28*3"}) == "Resultado: 84"
    assert calc.invoke({"expresion": "10/4"}) == "Resultado: 2.5"
    assert calc.invoke({"expresion": "1/0"}).startswith("Error al calcular '1/0'")
    assert calc.invoke({"expresion": "os.system('x')"}).startswith("Error al calcular")
    assert "Evalúa una expresión aritmética" in (calc.description or "")


def test_literales_y_formato() -> None:
    assert literales_numericos("5*14 + 2*28") == [5.0, 14.0, 2.0, 28.0]
    assert formatear_numero(84.0) == "84"
    assert formatear_numero(2.5) == "2.5"
