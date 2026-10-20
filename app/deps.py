"""Dependencias de FastAPI compartidas por los routers (acceso al Runtime y autenticación opcional)."""

import secrets
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, Request

if TYPE_CHECKING:  # solo para tipado: evita un import circular (runtime → graph → hitl → deps)
    from app.runtime import Runtime


def get_runtime(request: Request) -> "Runtime":
    rt: Runtime = request.app.state.rt
    return rt


RuntimeDep = Annotated["Runtime", Depends(get_runtime)]


def verificar_api_key(rt: RuntimeDep, x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Si API_KEY está definida, exige el header X-API-Key (comparación en tiempo constante)."""
    esperada = rt.api.api_key
    # bytes: compare_digest(str, str) lanza TypeError con caracteres no ASCII (un header hostil daría un 500, no un 401)
    if esperada and not secrets.compare_digest((x_api_key or "").encode(), esperada.encode()):
        raise HTTPException(status_code=401, detail="API key inválida o ausente (header X-API-Key).")


AutenticadoDep = Depends(verificar_api_key)
