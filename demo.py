"""Corre los 8 escenarios, imprime el flujo de delegación en vivo y escribe traces/traza_ejecucion.{json,log}.

python demo.py                       # los 8 escenarios
python demo.py --escenarios E1,E5    # subconjunto
python demo.py --backend pinecone    # RAG real del notebook (requiere requirements-pinecone.txt y PINECONE_API_KEY)
python demo.py --strict              # exit code 1 si alguna verificación falla
"""

import argparse
import asyncio
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from config import Settings
from escenarios import ESCENARIOS, ejecutar_escenario
from exceptions import OrquestadorError
from logging_config import configurar_logging, forzar_utf8_en_consola
from prompts import PROMPT_VERSIONES
from tracing import EscenarioTraza, EventoTraza, TrazaEjecucion, guardar_traza, render_escenario


async def _correr(ids: set[str], settings: Settings, salida: Path) -> TrazaEjecucion:
    trazas: list[EscenarioTraza] = []
    for esc in (e for e in ESCENARIOS if e.id in ids):
        print(f"\n=== {esc.id} · {esc.titulo} ===")
        print(f'Usuario: "{esc.pregunta}"')
        trazas.append(await ejecutar_escenario(esc, settings, on_evento=_en_vivo))
        print("\n".join(f"  {'✔' if v.ok else '✘'} {v.descripcion}" for v in trazas[-1].verificaciones))
    verificaciones = [v for t in trazas for v in t.verificaciones]
    tokens = sum(t.tokens.get("total_tokens", 0) for t in trazas)
    return TrazaEjecucion(
        metadata={
            "generado_en": datetime.now(UTC).isoformat(timespec="seconds"),
            "proveedor": settings.llm_provider,
            "modelo": settings.modelo_activo,
            "modelo_supervisor": settings.supervisor_model or settings.modelo_activo,
            "retriever_backend": settings.retriever_backend,
            "max_pasos": settings.max_pasos,
            "max_reparaciones": settings.max_reparaciones,
            "recursion_limit": settings.recursion_limit,
            "prompts": PROMPT_VERSIONES,
            "python": platform.python_version(),
            "langgraph": version("langgraph"),
            "langchain": version("langchain"),
        },
        escenarios=trazas,
        resumen={
            "escenarios": len(trazas),
            "verificaciones_ok": sum(v.ok for v in verificaciones),
            "verificaciones_total": len(verificaciones),
            "tokens": tokens,
            "duracion_s": round(sum(t.duracion_ms for t in trazas) / 1000, 1),
        },
    )


def _en_vivo(evento: EventoTraza) -> None:
    print(f"[{evento.orden}] {evento.titulo}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--escenarios", default=",".join(e.id for e in ESCENARIOS))
    parser.add_argument("--backend", choices=("local", "pinecone"), default=None)
    parser.add_argument("--salida", type=Path, default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    forzar_utf8_en_consola()
    ids = {i.strip().upper() for i in args.escenarios.split(",") if i.strip()}
    desconocidos = ids - {e.id for e in ESCENARIOS}
    if desconocidos:
        parser.error(f"escenarios desconocidos: {sorted(desconocidos)}")
    settings = Settings.from_env(**({"retriever_backend": args.backend} if args.backend else {}))
    configurar_logging(settings.log_level)
    try:
        traza = asyncio.run(_correr(ids, settings, args.salida or settings.traces_dir))
    except OrquestadorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    ruta_json, ruta_log = guardar_traza(traza, args.salida or settings.traces_dir)
    print("\n" + "\n\n".join(render_escenario(t) for t in traza.escenarios[-1:]))
    r = traza.resumen
    ok, total = r["verificaciones_ok"], r["verificaciones_total"]
    print(f"\nVerificaciones: {ok}/{total} · {r['tokens']} tokens · {r['duracion_s']} s")
    print(f"Traza: {ruta_json} · {ruta_log}")
    return 1 if args.strict and r["verificaciones_ok"] != r["verificaciones_total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
