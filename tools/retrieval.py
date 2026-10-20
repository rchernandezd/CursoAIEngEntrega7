"""Herramienta del Investigador: búsqueda en las políticas internas de TechCorp.

Dos backends con la misma interfaz (`Retriever`):
- LocalBM25Retriever: en memoria, sin red ni claves. Lo usan los tests y la demo offline.
- PineconeHybridRetriever: BM25 + Pinecone (el RAGSystem de la Pre-entrega 4 que reconecta el notebook).
"""

import asyncio
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from typing import Any, Protocol

from langchain_core.tools import BaseTool, tool

from config import Settings
from exceptions import ProveedorNoDisponibleError
from schemas import Fragmento

# Mismos 4 documentos que el notebook (Pre-entrega 3/4). Las cifras de las pruebas salen de aquí.
CORPUS: dict[str, str] = {
    "politica_vacaciones.txt": (
        "Política de Vacaciones - TechCorp\n\nLos empleados con menos de 5 años de antigüedad acceden a "
        "14 días corridos de vacaciones. Los empleados con 5 a 10 años acceden a 21 días. Los empleados "
        "con más de 10 años acceden a 28 días."
    ),
    "politica_teletrabajo.txt": (
        "Política de Teletrabajo - TechCorp\n\nEl esquema estándar es de 3 días de trabajo remoto y 2 días "
        "presenciales por semana. Soporte Técnico Nivel 1 y Recepción son 100% presenciales."
    ),
    "politica_seguridad_informatica.txt": (
        "Política de Seguridad Informática - TechCorp\n\nLas contraseñas deben renovarse cada 90 días y "
        "usar 2FA obligatorio en todos los sistemas internos."
    ),
    "onboarding_nuevos_empleados.txt": (
        "Proceso de Onboarding - TechCorp\n\nEl proceso dura 4 semanas: inducción general, capacitación "
        "del rol, y cierre con Recursos Humanos."
    ),
}

SIN_RESULTADOS = "No se encontró información relevante en las políticas."


def normalizar_texto(texto: str) -> str:
    """casefold + sin tildes + solo alfanuméricos separados por un espacio."""
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto.casefold()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes).split())


class Retriever(Protocol):
    async def buscar(self, consulta: str, k: int) -> list[Fragmento]: ...


# ---------------------------------------------------------------- backend local (BM25 propio)
_STOPWORDS = frozenset(
    [
        "a",
        "al",
        "ante",
        "con",
        "cual",
        "cuales",
        "cuando",
        "cuanto",
        "cuantos",
        "de",
        "del",
        "desde",
        "donde",
        "el",
        "ella",
        "en",
        "entre",
        "es",
        "esa",
        "ese",
        "esta",
        "este",
        "ha",
        "han",
        "hay",
        "la",
        "las",
        "le",
        "les",
        "lo",
        "los",
        "me",
        "mi",
        "mis",
        "no",
        "o",
        "para",
        "pero",
        "por",
        "que",
        "se",
        "si",
        "sin",
        "su",
        "sus",
        "tiene",
        "tienen",
        "un",
        "una",
        "uno",
        "unos",
        "unas",
        "y",
        "ya",
        "como",
        "empresa",
        "techcorp",
    ]
)


def tokenizar(texto: str) -> list[str]:
    """Tokens normalizados, sin stopwords y con un stemming mínimo (plural → singular)."""
    tokens: list[str] = []
    for t in normalizar_texto(texto).split():
        if t in _STOPWORDS:
            continue
        if len(t) > 4 and t.endswith("es"):
            t = t[:-2]
        elif len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        tokens.append(t)
    return tokens


class LocalBM25Retriever:
    """BM25 (k1=1.5, b=0.75) con IDF suavizado: funciona bien aunque el corpus tenga solo 4 documentos."""

    def __init__(self, corpus: Mapping[str, str] = CORPUS, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1, self._b = k1, b
        self._docs = [(fuente, texto, Counter(tokenizar(texto))) for fuente, texto in corpus.items()]
        self._largos = [sum(c.values()) for _, _, c in self._docs]
        self._promedio = (sum(self._largos) / len(self._largos)) if self._largos else 0.0
        n = len(self._docs)
        df: Counter[str] = Counter()
        for _, _, conteo in self._docs:
            df.update(conteo.keys())
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def _puntaje(self, consulta: list[str], indice: int) -> float:
        _, _, conteo = self._docs[indice]
        largo = self._largos[indice]
        total = 0.0
        for t in set(consulta):
            f = conteo.get(t, 0)
            if f == 0:
                continue
            denominador = f + self._k1 * (1 - self._b + self._b * largo / (self._promedio or 1.0))
            total += self._idf.get(t, 0.0) * f * (self._k1 + 1) / denominador
        return total

    async def buscar(self, consulta: str, k: int) -> list[Fragmento]:
        tokens = tokenizar(consulta)
        puntajes = [(self._puntaje(tokens, i), i) for i in range(len(self._docs))]
        mejores = sorted((p for p in puntajes if p[0] > 0), key=lambda p: (-p[0], p[1]))[:k]
        return [Fragmento(fuente=self._docs[i][0], contenido=self._docs[i][1]) for _, i in mejores]


# ---------------------------------------------------------------- backend Pinecone (código del notebook)
class PineconeHybridRetriever:
    """Envuelve el EnsembleRetriever (BM25 + Pinecone) del notebook. Su `invoke` es síncrono → to_thread."""

    def __init__(self, retriever: Any) -> None:
        self._retriever = retriever

    async def buscar(self, consulta: str, k: int) -> list[Fragmento]:
        docs = await asyncio.to_thread(self._retriever.invoke, consulta)
        return [Fragmento(fuente=str(d.metadata.get("source", "?")), contenido=d.page_content) for d in docs[:k]]


def crear_retriever_pinecone(settings: Settings, corpus: Mapping[str, str] = CORPUS) -> PineconeHybridRetriever:
    """Reconecta (o crea e ingesta si está vacío) el índice híbrido. Réplica de la celda 8 del notebook."""
    try:
        from langchain_classic.retrievers import EnsembleRetriever
        from langchain_community.retrievers import BM25Retriever
        from langchain_core.documents import Document
        from langchain_huggingface import HuggingFaceEmbeddings
        from langchain_pinecone import PineconeVectorStore
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from pinecone import Pinecone, ServerlessSpec
    except ImportError as exc:  # pragma: no cover - depende de los extras opcionales
        raise ProveedorNoDisponibleError(
            "RETRIEVER_BACKEND=pinecone requiere: pip install -r requirements-pinecone.txt"
        ) from exc
    if not settings.pinecone_api_key.strip():
        from exceptions import MissingApiKeyError

        raise MissingApiKeyError("Definí PINECONE_API_KEY en .env o usá RETRIEVER_BACKEND=local.")

    documentos = [Document(page_content=t, metadata={"source": n}) for n, t in corpus.items()]
    chunks = RecursiveCharacterTextSplitter.from_tiktoken_encoder(chunk_size=600, chunk_overlap=100).split_documents(
        documentos
    )
    for i, c in enumerate(chunks):
        c.metadata["chunk_id"] = i

    embeddings = HuggingFaceEmbeddings(model_name=settings.pinecone_embedding_model)
    pc = Pinecone(api_key=settings.pinecone_api_key)
    nombre, namespace = settings.pinecone_index_name, settings.pinecone_namespace
    if nombre not in [i["name"] for i in pc.list_indexes()]:
        pc.create_index(
            name=nombre, dimension=384, metric="cosine", spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
    vectorstore = PineconeVectorStore(index_name=nombre, embedding=embeddings, namespace=namespace)
    if pc.Index(nombre).describe_index_stats()["namespaces"].get(namespace, {}).get("vector_count", 0) == 0:
        vectorstore.add_documents(chunks)  # primera vez: ingesta; si ya hay datos no se reingesta

    bm25 = BM25Retriever.from_documents(chunks)
    bm25.k = settings.top_k
    vectorial = vectorstore.as_retriever(search_kwargs={"k": settings.top_k, "namespace": namespace})
    return PineconeHybridRetriever(EnsembleRetriever(retrievers=[bm25, vectorial], weights=[0.5, 0.5]))


def crear_retriever(settings: Settings) -> Retriever:
    if settings.retriever_backend == "pinecone":
        return crear_retriever_pinecone(settings)
    return LocalBM25Retriever()


# ---------------------------------------------------------------- herramienta
def crear_herramienta_busqueda(retriever: Retriever, *, k: int) -> BaseTool:
    @tool
    async def buscar_en_politicas_internas(pregunta: str) -> str:
        """Busca información en las políticas internas de TechCorp (vacaciones, teletrabajo, seguridad
        informática, onboarding). Usar para cualquier pregunta sobre reglas o números concretos de la
        empresa, con una consulta corta y específica (ej. 'días de vacaciones por antigüedad'). Devuelve
        fragmentos con el formato '[Fuente: archivo.txt] texto'; si no hay nada relevante devuelve
        'No se encontró información relevante en las políticas.' (no inventar datos en ese caso)."""
        resultados = await retriever.buscar(pregunta, k)
        if not resultados:
            return SIN_RESULTADOS
        return "\n\n".join(f"[Fuente: {r['fuente']}] {r['contenido']}" for r in resultados)

    return buscar_en_politicas_internas


_BLOQUE = re.compile(r"\[Fuente: ([^\]]+)\] (.*?)(?=\n\n\[Fuente: |\Z)", re.DOTALL)


def parsear_evidencia(salida_herramienta: str) -> list[Fragmento]:
    """Inversa de la herramienta: '[Fuente: x] texto' → fragmentos."""
    return [Fragmento(fuente=f.strip(), contenido=c.strip()) for f, c in _BLOQUE.findall(salida_herramienta)]
