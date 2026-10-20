"""Observabilidad: trazas de cada ejecución hacia Arize Phoenix (OpenInference) o LangSmith.

OBSERVABILITY=phoenix    registra el TracerProvider de OpenTelemetry y activa el instrumentor de LangChain/LangGraph:
                         cada nodo, llamada al LLM y herramienta es un span (con tokens, modelo y costo).
OBSERVABILITY=langsmith  basta con las variables de entorno LANGSMITH_*: LangGraph ya se traza solo.
OBSERVABILITY=none       sin trazas (tests).

Además cada job abre un span raíz propio ("tarea_multiagente") con la pregunta y la respuesta, y la RunnableConfig lleva
`thread_id`/`session_id` y las versiones de los prompts como metadata: así las trazas de una misma tarea (incluida su
reanudación tras la aprobación humana) quedan agrupadas en una sesión y se puede filtrar por versión de prompt.
"""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext

from langchain_core.runnables import RunnableConfig

from app.settings import ApiSettings
from exceptions import ConfigError
from prompts import PROMPT_VERSIONES

logger = logging.getLogger(__name__)

_backend: str = "none"


def init_observability(api: ApiSettings) -> Callable[[], None]:
    """Activa la instrumentación. Devuelve la función que vacía los buffers al apagar la app."""
    global _backend
    _backend = api.observability
    match api.observability:
        case "phoenix":
            from openinference.instrumentation.langchain import LangChainInstrumentor
            from phoenix.otel import register

            provider = register(
                project_name=api.phoenix_project,
                endpoint=f"{api.phoenix_endpoint}/v1/traces",  # OTLP/HTTP: el mismo puerto de la UI (6006)
                protocol="http/protobuf",
                batch=True,  # exporta en un hilo aparte: no bloquea el event loop
                auto_instrument=False,
                verbose=False,
            )
            instrumentor = LangChainInstrumentor()
            instrumentor.instrument(tracer_provider=provider)
            logger.info("Phoenix activo: proyecto=%s endpoint=%s", api.phoenix_project, api.phoenix_endpoint)

            def apagar() -> None:
                instrumentor.uninstrument()
                provider.force_flush()
                provider.shutdown()

            return apagar
        case "langsmith":
            if not api.langsmith_api_key.strip():
                raise ConfigError("OBSERVABILITY=langsmith requiere LANGSMITH_API_KEY en .env")
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGSMITH_API_KEY"] = api.langsmith_api_key
            os.environ["LANGSMITH_PROJECT"] = api.langsmith_project
            logger.info("LangSmith activo: proyecto=%s", api.langsmith_project)

            def apagar() -> None:
                from langchain_core.tracers.langchain import wait_for_all_tracers

                wait_for_all_tracers()  # espera a que se envíen las trazas pendientes

            return apagar
        case _:
            return lambda: None


def config_de_traza(job_id: str) -> RunnableConfig:
    """Campos de RunnableConfig que ven ambas plataformas (nombre del run, tags y metadata)."""
    versiones = ",".join(f"{k}={v}" for k, v in sorted(PROMPT_VERSIONES.items()))
    return RunnableConfig(
        run_name="grafo_multiagente",
        tags=["entrega-7", *sorted(PROMPT_VERSIONES.values())],
        metadata={"job_id": job_id, "thread_id": job_id, "session_id": job_id, "prompt_versions": versiones},
    )


@contextmanager
def trazar_job(job_id: str, query: str) -> Iterator[Callable[[str], None]]:
    """Span/run raíz del job. Entrega una función para registrar la salida (la respuesta final)."""
    meta = config_de_traza(job_id)
    metadata, tags = meta.get("metadata", {}), meta.get("tags", [])
    if _backend == "phoenix":
        from opentelemetry import trace

        tracer = trace.get_tracer("app.worker")
        with tracer.start_as_current_span(
            "tarea_multiagente",
            attributes={
                "openinference.span.kind": "CHAIN",
                "session.id": job_id,
                "input.value": query,
                "metadata": str(metadata),
            },
        ) as span:
            yield lambda salida: span.set_attribute("output.value", salida)
    elif _backend == "langsmith":
        from langsmith import trace as ls_trace

        with ls_trace(
            name="tarea_multiagente",
            run_type="chain",
            inputs={"query": query},
            metadata=metadata,
            tags=tags,
        ) as run:
            yield lambda salida: run.add_outputs({"respuesta": salida})
    else:
        with nullcontext():
            yield lambda salida: None
