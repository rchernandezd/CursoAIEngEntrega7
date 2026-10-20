"""Factory del LLM por proveedor (misma idea que la Pre-entrega 5). Valida la key antes de cualquier llamada."""

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from config import Settings
from exceptions import ConfigError, MissingApiKeyError, ProveedorNoDisponibleError

_NOMBRE_KEY = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY"}


def build_llm(settings: Settings, *, modelo: str | None = None) -> BaseChatModel:
    """Construye el chat model del proveedor activo. `modelo` pisa el modelo por defecto (p. ej. supervisor)."""
    key = settings.api_key_activa
    if not key.strip():
        raise MissingApiKeyError(
            f"Definí {_NOMBRE_KEY[settings.llm_provider]} en .env (LLM_PROVIDER={settings.llm_provider}) "
            "o cambiá LLM_PROVIDER."
        )
    nombre = modelo or settings.modelo_activo
    match settings.llm_provider:
        case "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=nombre,
                temperature=settings.llm_temperature,
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
                api_key=SecretStr(key),
            )
        case "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=nombre,
                temperature=settings.llm_temperature,
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
                api_key=SecretStr(key),
                stop=None,
            )
        case "google":
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
            except ImportError as exc:
                raise ProveedorNoDisponibleError(
                    "LLM_PROVIDER=google requiere: pip install langchain-google-genai"
                ) from exc
            gemini: BaseChatModel = ChatGoogleGenerativeAI(
                model=nombre,
                temperature=settings.llm_temperature,
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
                google_api_key=SecretStr(key),
            )
            return gemini
        case _:
            raise ConfigError(f"Proveedor no soportado: {settings.llm_provider}")
