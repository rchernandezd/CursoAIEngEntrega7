"""Settings inmutables leídos del entorno (.env). Las API keys NO se validan aquí (ver llm.py)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from dotenv import load_dotenv

PROVEEDORES: tuple[str, ...] = ("openai", "anthropic", "google")
BACKENDS: tuple[str, ...] = ("local", "pinecone")


@dataclass(frozen=True, slots=True)
class Settings:
    llm_provider: str
    openai_api_key: str = field(repr=False)
    anthropic_api_key: str = field(repr=False)
    google_api_key: str = field(repr=False)
    pinecone_api_key: str = field(repr=False)
    openai_model: str
    anthropic_model: str
    gemini_model: str
    supervisor_model: str  # "" = mismo modelo que los demás roles
    llm_temperature: float
    llm_timeout_s: float
    llm_max_retries: int
    max_pasos: int  # tope de invocaciones a especialistas por consulta
    max_reparaciones: int  # reintentos por agente tras una validación fallida
    recursion_limit: int  # techo duro de LangGraph (red de seguridad)
    especialista_timeout_s: float
    retriever_backend: str
    top_k: int
    pinecone_index_name: str
    pinecone_namespace: str
    pinecone_embedding_model: str
    operandos_estrictos: bool  # True: OPERANDO_SIN_ORIGEN bloquea; False: solo advierte
    traces_dir: Path
    log_level: str

    def __post_init__(self) -> None:
        if self.llm_provider not in PROVEEDORES:
            raise ValueError(f"LLM_PROVIDER debe ser uno de {PROVEEDORES}")
        if self.retriever_backend not in BACKENDS:
            raise ValueError(f"RETRIEVER_BACKEND debe ser uno de {BACKENDS}")
        if not 0.0 <= self.llm_temperature <= 1.0:
            raise ValueError("LLM_TEMPERATURE debe estar entre 0 y 1 (se recomienda 0)")
        if not 1 <= self.llm_timeout_s <= 300:
            raise ValueError("LLM_TIMEOUT_S debe estar entre 1 y 300")
        if not 0 <= self.llm_max_retries <= 6:
            raise ValueError("LLM_MAX_RETRIES debe estar entre 0 y 6")
        if not 2 <= self.max_pasos <= 12:
            raise ValueError("MAX_PASOS debe estar entre 2 y 12")
        if not 0 <= self.max_reparaciones <= 3:
            raise ValueError("MAX_REPARACIONES debe estar entre 0 y 3")
        minimo = 3 * self.max_pasos + 4
        if not minimo <= self.recursion_limit <= 100:
            raise ValueError(f"RECURSION_LIMIT debe estar entre {minimo} (3*MAX_PASOS+4) y 100")
        if not 1 <= self.especialista_timeout_s <= 300:
            raise ValueError("ESPECIALISTA_TIMEOUT_S debe estar entre 1 y 300")
        if not 1 <= self.top_k <= 10:
            raise ValueError("TOP_K debe estar entre 1 y 10")

    @property
    def modelo_activo(self) -> str:
        return {
            "openai": self.openai_model,
            "anthropic": self.anthropic_model,
            "google": self.gemini_model,
        }[self.llm_provider]

    @property
    def api_key_activa(self) -> str:
        return {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
        }[self.llm_provider]

    @classmethod
    def from_env(cls, **overrides: object) -> Self:
        load_dotenv()
        env = os.getenv
        values: dict[str, object] = dict(
            llm_provider=env("LLM_PROVIDER", "openai").strip().lower(),
            openai_api_key=env("OPENAI_API_KEY", ""),
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            google_api_key=env("GOOGLE_API_KEY", ""),
            pinecone_api_key=env("PINECONE_API_KEY", ""),
            openai_model=env("OPENAI_MODEL", "gpt-4o-mini"),
            anthropic_model=env("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            gemini_model=env("GEMINI_MODEL", "gemini-flash-latest"),
            supervisor_model=env("SUPERVISOR_MODEL", ""),
            llm_temperature=float(env("LLM_TEMPERATURE", "0")),
            llm_timeout_s=float(env("LLM_TIMEOUT_S", "60")),
            llm_max_retries=int(env("LLM_MAX_RETRIES", "3")),
            max_pasos=int(env("MAX_PASOS", "6")),
            max_reparaciones=int(env("MAX_REPARACIONES", "2")),
            recursion_limit=int(env("RECURSION_LIMIT", "30")),
            especialista_timeout_s=float(env("ESPECIALISTA_TIMEOUT_S", "60")),
            retriever_backend=env("RETRIEVER_BACKEND", "local").strip().lower(),
            top_k=int(env("TOP_K", "5")),
            pinecone_index_name=env("PINECONE_INDEX_NAME", "techcorp-rag-hibrido"),
            pinecone_namespace=env("PINECONE_NAMESPACE", "politicas-internas"),
            pinecone_embedding_model=env("PINECONE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
            operandos_estrictos=env("OPERANDOS_ESTRICTOS", "false").strip().lower() in ("1", "true", "yes"),
            traces_dir=Path(env("TRACES_DIR", "traces")),
            log_level=env("LOG_LEVEL", "INFO").upper(),
        )
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]  # dict[str, object] → kwargs; validado en __post_init__
