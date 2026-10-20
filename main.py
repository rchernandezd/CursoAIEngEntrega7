"""CLI: una pregunta → flujo de delegación en vivo → respuesta.

python main.py "¿Cuántos días de vacaciones le tocan a alguien con 12 años y cuántos suma en 3 años?"
python main.py "..." --backend pinecone --verbose
python main.py --export-graph          # escribe docs/grafo.mmd (y docs/grafo.png si hay red)
"""

import argparse
import asyncio
import sys
from pathlib import Path

from config import Settings
from escenarios import Escenario, ejecutar_escenario
from exceptions import OrquestadorError
from graph import compilar, construir_dependencias
from logging_config import configurar_logging, forzar_utf8_en_consola
from tracing import EventoTraza, render_escenario


def exportar_grafo(settings: Settings, destino: Path) -> None:
    """Mermaid del grafo compilado (no necesita API key: se usa un LLM de relleno)."""
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    relleno = ChatOpenAI(model="gpt-4o-mini", api_key=SecretStr("solo-para-dibujar"))  # no se hace ninguna llamada
    app = compilar(construir_dependencias(settings, llm=relleno))
    destino.mkdir(parents=True, exist_ok=True)
    grafo = app.get_graph()
    (destino / "grafo.mmd").write_text(grafo.draw_mermaid(), encoding="utf-8")
    try:
        (destino / "grafo.png").write_bytes(grafo.draw_mermaid_png())  # usa mermaid.ink: requiere red
    except Exception as exc:  # sin red no es un error del proyecto
        print(f"(PNG no generado: {type(exc).__name__}; el .mmd sí está en {destino})")
    print(f"Grafo exportado en {destino}")


async def _correr(pregunta: str, settings: Settings, verbose: bool) -> int:
    escenario = Escenario("Q", "Consulta puntual", "-", pregunta, lambda t: [])
    en_vivo = (lambda e: print(f"[{e.orden}] {e.titulo}")) if verbose else None
    traza = await ejecutar_escenario(escenario, settings, on_evento=_imprimir if verbose else en_vivo)
    print("\n" + traza.respuesta)
    print(f"\n({traza.estado_final} · {traza.pasos} paso(s) · {traza.duracion_ms / 1000:.1f} s)")
    if verbose:
        print("\n" + render_escenario(traza))
    return 0 if traza.estado_final != "REQUIERE_REVISION_HUMANA" else 3


def _imprimir(evento: EventoTraza) -> None:
    print(f"[{evento.orden}] {evento.titulo}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pregunta", nargs="?")
    parser.add_argument("--backend", choices=("local", "pinecone"), default=None, help="pisa RETRIEVER_BACKEND")
    parser.add_argument("--verbose", action="store_true", help="muestra el log completo de decisiones")
    parser.add_argument("--export-graph", action="store_true")
    args = parser.parse_args(argv)
    forzar_utf8_en_consola()
    overrides = {"retriever_backend": args.backend} if args.backend else {}
    settings = Settings.from_env(**overrides)
    configurar_logging(settings.log_level)
    try:
        if args.export_graph:
            exportar_grafo(settings, Path("docs"))
            return 0
        if not args.pregunta:
            parser.error("falta la pregunta (o usá --export-graph)")
        return asyncio.run(_correr(args.pregunta, settings, args.verbose))
    except OrquestadorError as exc:  # config incompleta: mensaje claro, sin traceback
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
