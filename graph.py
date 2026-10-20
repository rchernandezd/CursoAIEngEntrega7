"""Grafo principal: StateGraph con Supervisor, 2 especialistas, Validador y nodos terminales."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.analyst_agent import crear_agente_analista, crear_nodo_analista
from agents.research_agent import crear_agente_investigador, crear_nodo_investigador
from agents.supervisor import crear_nodo_supervisor, enrutar
from agents.synthesis import crear_nodo_revision_humana, crear_nodo_sintesis
from agents.validator import crear_nodo_validador
from config import Settings
from faults import InyectorFallas
from llm import build_llm
from state import AgentState
from tools.retrieval import Retriever, crear_retriever


@dataclass(frozen=True, slots=True)
class Dependencias:
    """Todo lo que el grafo necesita, inyectado (los tests pasan fakes por rol)."""

    settings: Settings
    llm_supervisor: BaseChatModel
    llm_investigador: BaseChatModel
    llm_analista: BaseChatModel
    llm_sintesis: BaseChatModel
    retriever: Retriever
    inyector: InyectorFallas = field(default_factory=InyectorFallas)


def construir_dependencias(
    settings: Settings,
    *,
    llm: BaseChatModel | None = None,
    retriever: Retriever | None = None,
    inyector: InyectorFallas | None = None,
) -> Dependencias:
    base = llm or build_llm(settings)
    supervisor = llm or (build_llm(settings, modelo=settings.supervisor_model) if settings.supervisor_model else base)
    return Dependencias(
        settings=settings,
        llm_supervisor=supervisor,
        llm_investigador=base,
        llm_analista=base,
        llm_sintesis=base,
        retriever=retriever or crear_retriever(settings),
        inyector=inyector or InyectorFallas(),
    )


def _agregar(
    grafo: StateGraph[AgentState], nombre: str, nodo: Callable[[AgentState], Awaitable[dict[str, Any]]]
) -> None:
    # Los stubs de LangGraph aceptan `async def` directos pero no valores tipados como Callable (cast solo de tipado).
    grafo.add_node(nombre, cast(Any, nodo))


type Extensor = Callable[[StateGraph[AgentState]], None]


def build_graph(deps: Dependencias, *, extender: Extensor | None = None) -> StateGraph[AgentState]:
    """Grafo del orquestador. `extender` (Entrega 7) conecta la salida de `sintesis` con nodos adicionales (HITL);
    sin él, `sintesis` termina en END (comportamiento de la Entrega 6, intacto)."""
    s = deps.settings
    grafo = StateGraph(AgentState)
    _agregar(grafo, "supervisor", crear_nodo_supervisor(deps.llm_supervisor, s))
    _agregar(
        grafo,
        "investigador",
        crear_nodo_investigador(
            crear_agente_investigador(deps.llm_investigador, deps.retriever, k=s.top_k),
            inyector=deps.inyector,
            timeout_s=s.especialista_timeout_s,
        ),
    )
    _agregar(
        grafo,
        "analista",
        crear_nodo_analista(
            crear_agente_analista(deps.llm_analista), inyector=deps.inyector, timeout_s=s.especialista_timeout_s
        ),
    )
    _agregar(grafo, "validador", crear_nodo_validador(s))
    _agregar(grafo, "sintesis", crear_nodo_sintesis(deps.llm_sintesis))
    _agregar(grafo, "revision_humana", crear_nodo_revision_humana())

    grafo.add_edge(START, "supervisor")
    grafo.add_conditional_edges("supervisor", enrutar)  # destinos inferidos del Literal de `enrutar`
    grafo.add_edge("investigador", "validador")
    grafo.add_edge("analista", "validador")
    grafo.add_edge("validador", "supervisor")  # el especialista nunca habla con otro: todo vuelve al supervisor
    if extender is None:
        grafo.add_edge("sintesis", END)
    else:
        extender(grafo)  # debe conectar "sintesis" con lo que sigue y terminar en END
    grafo.add_edge("revision_humana", END)
    return grafo


def compilar(
    deps: Dependencias,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    extender: Extensor | None = None,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    return build_graph(deps, extender=extender).compile(checkpointer=checkpointer)


def configuracion(settings: Settings) -> RunnableConfig:
    """RunnableConfig de CADA invocación: el techo duro de LangGraph es la última red de seguridad."""
    return RunnableConfig(recursion_limit=settings.recursion_limit)
