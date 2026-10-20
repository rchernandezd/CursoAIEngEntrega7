"""Observabilidad: metadata de trazas, selección del backend y spans de OpenTelemetry (sin red)."""

import pytest

from app.observability import config_de_traza, init_observability, trazar_job
from app.settings import ApiSettings
from exceptions import ConfigError
from prompts import PROMPT_VERSIONES


def test_la_config_de_traza_lleva_job_sesion_y_version_de_prompts() -> None:
    cfg = config_de_traza("job-123")
    meta = cfg["metadata"]
    assert meta["job_id"] == meta["thread_id"] == meta["session_id"] == "job-123"
    assert all(f"{nombre}={version}" in meta["prompt_versions"] for nombre, version in PROMPT_VERSIONES.items())
    assert "entrega-7" in cfg["tags"] and set(PROMPT_VERSIONES.values()) <= set(cfg["tags"])


def test_sin_backend_no_hay_instrumentacion() -> None:
    apagar = init_observability(ApiSettings.from_env(observability="none"))
    apagar()  # no debe fallar
    with trazar_job("j", "pregunta") as registrar:
        registrar("respuesta")


def test_langsmith_exige_api_key() -> None:
    with pytest.raises(ConfigError, match="LANGSMITH_API_KEY"):
        init_observability(ApiSettings.from_env(observability="langsmith", langsmith_api_key=""))


def test_phoenix_registra_el_span_raiz_del_job() -> None:
    """Con un exportador en memoria comprobamos que `trazar_job` emite el span 'tarea_multiagente' con la sesión."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import app.observability as obs

    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    trace.set_tracer_provider(proveedor)
    anterior, obs._backend = obs._backend, "phoenix"
    try:
        with trazar_job("job-9", "¿cuántos días?") as registrar:
            registrar("28 días")
    finally:
        obs._backend = anterior
    (span,) = exportador.get_finished_spans()
    assert span.name == "tarea_multiagente"
    assert span.attributes is not None
    assert span.attributes["session.id"] == "job-9"
    assert span.attributes["input.value"] == "¿cuántos días?" and span.attributes["output.value"] == "28 días"
