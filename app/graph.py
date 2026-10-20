"""Grafo de producción: el orquestador de la Entrega 6 + compuerta HITL + checkpointer persistente en Redis."""

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

import schemas as modelos_agente
from app.hitl import Actuador, crear_extensor_hitl
from app.settings import ApiSettings
from graph import Dependencias, compilar
from state import AgentState

type GrafoProduccion = CompiledStateGraph[AgentState, None, AgentState, AgentState]


def modelos_del_estado() -> list[tuple[str, str]]:
    """Clases Pydantic del estado del agente (schemas.py), como (módulo, nombre) para la allowlist del serializador."""
    return [
        (clase.__module__, nombre)
        for nombre, clase in inspect.getmembers(modelos_agente, inspect.isclass)
        if issubclass(clase, BaseModel) and clase.__module__ == modelos_agente.__name__
    ]


@asynccontextmanager
async def abrir_checkpointer(api: ApiSettings) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Checkpointer de LangGraph. `redis` (producción) sobrevive a reinicios; `memory` es solo para tests y desarrollo.

    AsyncRedisSaver exige un Redis con los módulos RedisJSON y RediSearch (Redis 8+ o Redis Stack).
    """
    if api.checkpointer == "memory":
        # allowlist explícita: sin ella LangGraph avisa (y luego bloqueará) al deserializar nuestros modelos
        yield InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=modelos_del_estado()))
        return
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

    ttl = {"default_ttl": api.checkpoint_ttl_min, "refresh_on_read": True}  # minutos
    async with AsyncRedisSaver.from_conn_string(api.redis_url, ttl=ttl) as saver:
        # Redis guarda el estado como JSON: sin esta allowlist, tras un reinicio los objetos Pydantic del estado
        # (ResearchArtifact, AccionPropuesta, ...) vuelven como dict y el grafo falla al reanudar.
        saver.serde = JsonPlusRedisSerializer(allowed_json_modules=modelos_del_estado())
        await saver.asetup()  # crea los índices la primera vez (idempotente)
        yield saver


def construir_grafo_produccion(
    deps: Dependencias, checkpointer: BaseCheckpointSaver[Any], actuador: Actuador, api: ApiSettings
) -> GrafoProduccion:
    """Mismo StateGraph de la Entrega 6, con la cola HITL tras la síntesis y el estado persistido por thread_id."""
    extensor = crear_extensor_hitl(actuador, destinatario=api.rrhh_destinatario)
    return compilar(deps, checkpointer=checkpointer, extender=extensor)
