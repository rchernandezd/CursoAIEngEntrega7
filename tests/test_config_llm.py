import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from agents.research_agent import crear_agente_investigador
from agents.supervisor import crear_nodo_supervisor
from config import Settings
from exceptions import MissingApiKeyError, ProveedorNoDisponibleError
from llm import build_llm
from schemas import DecisionSupervisor
from tools.retrieval import LocalBM25Retriever, crear_retriever


def _s(**kw: object) -> Settings:
    base: dict[str, object] = dict(openai_api_key="", anthropic_api_key="", google_api_key="", pinecone_api_key="")
    return Settings.from_env(**{**base, **kw})


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "cohere"},
        {"retriever_backend": "chroma"},
        {"llm_temperature": 1.5},
        {"max_pasos": 1},
        {"max_pasos": 13},
        {"max_reparaciones": 4},
        {"max_pasos": 6, "recursion_limit": 21},
        {"recursion_limit": 101},
        {"especialista_timeout_s": 0},
        {"top_k": 0},
    ],
)
def test_settings_rechaza_valores_invalidos(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _s(**overrides)


def test_settings_defaults_y_no_expone_keys() -> None:
    s = _s(openai_api_key="sk-secreto")
    assert (s.max_pasos, s.max_reparaciones, s.recursion_limit, s.retriever_backend) == (6, 2, 30, "local")
    assert "sk-secreto" not in repr(s) and s.api_key_activa == "sk-secreto" and s.modelo_activo == "gpt-4o-mini"


def test_build_llm_valida_la_key_y_construye_sin_red() -> None:
    with pytest.raises(MissingApiKeyError, match="OPENAI_API_KEY"):
        build_llm(_s())
    assert isinstance(build_llm(_s(openai_api_key="sk-x")), ChatOpenAI)
    assert isinstance(build_llm(_s(llm_provider="anthropic", anthropic_api_key="sk-ant-x")), ChatAnthropic)
    assert build_llm(_s(openai_api_key="sk-x"), modelo="gpt-4o").model_name == "gpt-4o"  # type: ignore[attr-defined]
    with pytest.raises(ProveedorNoDisponibleError):
        build_llm(_s(llm_provider="google", google_api_key="g-x"))


def test_los_esquemas_son_compatibles_con_el_cliente_real_sin_llamar_a_la_red() -> None:
    llm = build_llm(_s(openai_api_key="sk-x"))
    assert llm.with_structured_output(DecisionSupervisor) is not None  # esquema aceptado por langchain-openai
    assert crear_agente_investigador(llm, LocalBM25Retriever(), k=5) is not None  # ToolStrategy(ResearchArtifact)
    assert crear_nodo_supervisor(llm, _s()) is not None


def test_backend_pinecone_exige_extras_y_key() -> None:
    with pytest.raises((ProveedorNoDisponibleError, MissingApiKeyError)):
        crear_retriever(_s(retriever_backend="pinecone"))
