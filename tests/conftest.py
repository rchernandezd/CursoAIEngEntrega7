"""Fakes y fixtures compartidos (suite offline: sin red ni API keys)."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from config import Settings
from faults import InyectorFallas
from graph import Dependencias
from schemas import DecisionSupervisor, Rubrica
from tools.retrieval import LocalBM25Retriever


class FakeToolCallingModel(BaseChatModel):
    """LLM guionado. Sirve como especialista (AIMessage con tool_calls), síntesis (texto) o supervisor (decisiones)."""

    guion: list[AIMessage] = Field(default_factory=list)
    decisiones: list[DecisionSupervisor] = Field(default_factory=list)
    llamadas: int = 0
    recibidos: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FakeToolCallingModel":
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.recibidos.append(list(messages))
        mensaje = self.guion[min(self.llamadas, len(self.guion) - 1)]
        self.llamadas += 1
        # copia profunda: si se devolviera la MISMA instancia, add_messages la reemplazaría en vez de anexarla
        return ChatResult(generations=[ChatGeneration(message=mensaje.model_copy(deep=True))])

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # supervisor
        def siguiente(entrada: Any) -> DecisionSupervisor:
            self.recibidos.append(list(entrada))
            d = self.decisiones[min(self.llamadas, len(self.decisiones) - 1)]
            self.llamadas += 1
            return d.model_copy(deep=True)

        return RunnableLambda(siguiente)


def tool_call(nombre: str, args: dict[str, Any], id_: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": id_, "name": nombre, "args": args}])


def decision(next_: str, instruccion: str = "", razon: str = "test", *, ok: bool = False) -> DecisionSupervisor:
    return DecisionSupervisor(
        next=next_,  # type: ignore[arg-type]
        instruccion=instruccion,
        razon=razon,
        rubrica=Rubrica(completitud=ok, respaldo_en_evidencia=ok, consistencia=ok),
    )


CITA_VACACIONES = "Los empleados con más de 10 años acceden a 28 días"


def guion_investigador(
    cita: str = CITA_VACACIONES, valor: float | None = 28, fuente: str = "politica_vacaciones.txt"
) -> list[AIMessage]:
    return [
        tool_call("buscar_en_politicas_internas", {"pregunta": "días de vacaciones por antigüedad"}, "b1"),
        tool_call(
            "ResearchArtifact",
            {
                "consulta": "días de vacaciones por antigüedad",
                "estado": "completo",
                "hallazgos": [
                    {
                        "afirmacion": "Más de 10 años: 28 días de vacaciones",
                        "valor": valor,
                        "unidad": "días corridos",
                        "fuente": fuente,
                        "cita": cita,
                    }
                ],
                "confidence_score": 0.95,
            },
            "b2",
        ),
    ]


def guion_analista(expresion: str = "28*3", resultado: float = 84) -> list[AIMessage]:
    return [
        tool_call("calculadora", {"expresion": expresion}, "c1"),
        tool_call(
            "AnalysisArtifact",
            {
                "expresion": expresion,
                "resultado": resultado,
                "explicacion": "Multiplicación directa.",
                "supuestos": [],
                "confidence_score": 0.9,
            },
            "c2",
        ),
    ]


@pytest.fixture(autouse=True)
def _sin_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite hermética: ni el .env del desarrollador (p. ej. RECURSION_LIMIT=40) ni sus claves cambian los resultados.
    Los tests pasan sus valores explícitos; `Settings.from_env`/`ApiSettings.from_env` ya no leen el archivo."""
    monkeypatch.setattr("config.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("app.settings.load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        openai_api_key="",
        anthropic_api_key="",
        google_api_key="",
        pinecone_api_key="",
        retriever_backend="local",
        traces_dir=tmp_path / "traces",
    )


FabricaDeps = Callable[..., Dependencias]


@pytest.fixture
def fabricar_deps(settings: Settings) -> FabricaDeps:
    def _fabricar(
        *,
        supervisor: list[DecisionSupervisor],
        investigador: list[AIMessage] | None = None,
        analista: list[AIMessage] | None = None,
        sintesis: str = "Respuesta final de prueba.",
        inyector: InyectorFallas | None = None,
        settings_: Settings | None = None,
    ) -> Dependencias:
        return Dependencias(
            settings=settings_ or settings,
            llm_supervisor=FakeToolCallingModel(decisiones=supervisor),
            llm_investigador=FakeToolCallingModel(guion=investigador or guion_investigador()),
            llm_analista=FakeToolCallingModel(guion=analista or guion_analista()),
            llm_sintesis=FakeToolCallingModel(guion=[AIMessage(content=sintesis)]),
            retriever=LocalBM25Retriever(),
            inyector=inyector or InyectorFallas(),
        )

    return _fabricar
