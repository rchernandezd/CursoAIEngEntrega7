"""Calculadora segura del Analista.

Evaluar con eval() texto generado por un LLM permite ejecutar código arbitrario. Aquí se parsea con `ast`
y solo se aceptan números y operaciones aritméticas: sin nombres, llamadas, atributos ni builtins.
"""

import ast
import operator as op
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool

from exceptions import ExpresionNoPermitidaError

MAX_LARGO_EXPRESION = 200
MAX_EXPONENTE = 100

type Numero = int | float

_BINARIOS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
}
_UNARIOS: dict[type[ast.unaryop], Callable[[Any], Any]] = {ast.USub: op.neg, ast.UAdd: op.pos}


def _evaluar(nodo: ast.AST) -> Numero:
    match nodo:
        case ast.Constant(value=valor) if isinstance(valor, int | float) and not isinstance(valor, bool):
            return valor
        case ast.BinOp(left=izq, op=operador, right=der) if type(operador) in _BINARIOS:
            a, b = _evaluar(izq), _evaluar(der)
            if isinstance(operador, ast.Pow) and abs(b) > MAX_EXPONENTE:
                raise ExpresionNoPermitidaError(f"Exponente demasiado grande (máximo {MAX_EXPONENTE})")
            resultado: Numero = _BINARIOS[type(operador)](a, b)
            return resultado
        case ast.UnaryOp(op=operador, operand=operando) if type(operador) in _UNARIOS:
            negado: Numero = _UNARIOS[type(operador)](_evaluar(operando))
            return negado
        case _:
            raise ExpresionNoPermitidaError("Expresión no permitida: solo números, + - * / // % ** y paréntesis")


def _parsear(expresion: str) -> ast.Expression:
    if not expresion.strip():
        raise ExpresionNoPermitidaError("Expresión vacía")
    if len(expresion) > MAX_LARGO_EXPRESION:
        raise ExpresionNoPermitidaError(f"Expresión demasiado larga (máximo {MAX_LARGO_EXPRESION} caracteres)")
    try:
        return ast.parse(expresion.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpresionNoPermitidaError(f"Sintaxis inválida: {exc.msg}") from exc


def evaluar_expresion(expresion: str) -> Numero:
    """Evalúa aritmética pura. Lanza ExpresionNoPermitidaError o ZeroDivisionError/OverflowError."""
    return _evaluar(_parsear(expresion).body)


def literales_numericos(expresion: str) -> list[float]:
    """Todos los números literales de la expresión (para comprobar de dónde salió cada operando)."""
    return [
        float(n.value)
        for n in ast.walk(_parsear(expresion))
        if isinstance(n, ast.Constant) and isinstance(n.value, int | float) and not isinstance(n.value, bool)
    ]


def formatear_numero(valor: Numero) -> str:
    """84.0 → '84'; 3.14159 → '3.14159'. Sin notación científica para magnitudes razonables."""
    if isinstance(valor, float) and valor.is_integer() and abs(valor) < 1e15:
        return str(int(valor))
    return f"{valor:.10g}" if isinstance(valor, float) else str(valor)


def crear_calculadora() -> BaseTool:
    @tool
    def calculadora(expresion: str) -> str:
        """Evalúa una expresión aritmética (+, -, *, /, //, %, ** y paréntesis) y devuelve el resultado.
        Usar punto como separador decimal y NO usar separador de miles. Ejemplo: '5*14 + 5*21 + 2*28'.
        Usar para CUALQUIER cálculo; no calcular de memoria. Devuelve 'Resultado: <número>' o un mensaje
        'Error al calcular ...' si la expresión no es válida (en ese caso corregirla y reintentar)."""
        try:
            return f"Resultado: {formatear_numero(evaluar_expresion(expresion))}"
        except (ExpresionNoPermitidaError, ArithmeticError) as exc:
            return f"Error al calcular '{expresion}': {exc}"

    return calculadora
