"""Reporte de latencia (p50 / p95 / p99), tokens y errores a partir de las trazas guardadas en Arize Phoenix.

    python -m scripts.p95_report --project agente-m7 --last-minutes 60

Para qué sirve: Phoenix (pestaña Metrics → "Trace latency") ya grafica P50/P75/P90/P95/P99. Este script calcula los
mismos percentiles con los datos crudos (API REST de Phoenix: GET /v1/projects/{proyecto}/spans) y sirve para
(a) contrastar el número de la captura, (b) tener la cifra exacta por escrito en el README y (c) versiones de Phoenix
sin ese gráfico.

Qué mide:
  - `tarea_multiagente`  span raíz de cada job (duración del trabajo del agente, sin contar la cola).
  - cada nodo del grafo (supervisor, investigador, analista, validador, sintesis...) y las llamadas a LLM/tools.
La latencia de `POST /tasks` NO está en Phoenix (la API desactiva sus spans HTTP para no ensuciar el P50/P99 del
proyecto): se mide en el cliente con `scripts/load_test.py`.
Costo: Phoenix lo calcula en la UI (Cost) a partir de tokens + nombre del modelo; acá solo se suman los tokens.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from scripts.load_test import percentil


def _fecha(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def traer_spans(endpoint: str, proyecto: str, desde: datetime | None, headers: dict[str, str]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    cursor: str | None = None
    with httpx.Client(base_url=endpoint, headers=headers, timeout=60) as cliente:
        while True:
            params: dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            if desde:
                params["start_time"] = desde.isoformat()
            r = cliente.get(f"/v1/projects/{proyecto}/spans", params=params)
            r.raise_for_status()
            cuerpo = r.json()
            spans.extend(cuerpo["data"])
            cursor = cuerpo.get("next_cursor")
            if not cursor:
                return spans


def duracion_s(span: dict[str, Any]) -> float:
    return (_fecha(span["end_time"]) - _fecha(span["start_time"])).total_seconds()


def resumen(valores: list[float]) -> dict[str, float | int]:
    return {
        "n": len(valores),
        "p50": round(percentil(valores, 50), 3),
        "p95": round(percentil(valores, 95), 3),
        "p99": round(percentil(valores, 99), 3),
        "max": round(max(valores), 3) if valores else float("nan"),
    }


def analizar(spans: list[dict[str, Any]]) -> dict[str, Any]:
    por_nombre: dict[str, list[float]] = defaultdict(list)
    tokens = {"prompt": 0, "completion": 0, "total": 0}
    errores = 0
    for s in spans:
        por_nombre[s["name"]].append(duracion_s(s))
        if s.get("status_code") == "ERROR":
            errores += 1
        if s.get("span_kind") == "LLM":
            a = s.get("attributes", {})
            tokens["prompt"] += int(a.get("llm.token_count.prompt", 0) or 0)
            tokens["completion"] += int(a.get("llm.token_count.completion", 0) or 0)
            tokens["total"] += int(a.get("llm.token_count.total", 0) or 0)
    nodos = {n: resumen(v) for n, v in sorted(por_nombre.items()) if n != "tarea_multiagente"}
    return {
        "spans": len(spans),
        "errores": errores,
        "tarea_multiagente": resumen(por_nombre.get("tarea_multiagente", [])),
        "tokens": tokens,
        "por_nodo": nodos,
    }


def imprimir(rep: dict[str, Any]) -> None:
    def fila(nombre: str, r: dict[str, Any]) -> str:
        cifras = "  ".join(f"{k}={r[k]:>8.3f}s" for k in ("p50", "p95", "p99", "max"))
        return f"  {nombre:34} n={r['n']:<4} {cifras}"

    print(f"Spans analizados: {rep['spans']}   spans con error: {rep['errores']}")
    print("\nLatencia por tarea (span raíz del job):")
    print(fila("tarea_multiagente", rep["tarea_multiagente"]))
    print("\nDesglose por nodo / LLM / herramienta:")
    for nombre, r in rep["por_nodo"].items():
        print(fila(nombre, r))
    t = rep["tokens"]
    print(f"\nTokens (spans LLM): prompt={t['prompt']} completion={t['completion']} total={t['total']}")
    if t["total"] == 0:
        print(
            "  (0 tokens: normal si el LLM es un simulador; con un proveedor real el costo aparece en Phoenix → Cost)"
        )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", default=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    p.add_argument("--project", default=os.getenv("PHOENIX_PROJECT_NAME", "agente-m7"))
    p.add_argument("--last-minutes", type=float, default=0, help="solo spans de los últimos N minutos (0 = todos)")
    p.add_argument("--json", dest="salida_json", help="guardar el reporte en este archivo JSON")
    args = p.parse_args()
    headers = {"authorization": f"Bearer {os.environ['PHOENIX_API_KEY']}"} if os.getenv("PHOENIX_API_KEY") else {}
    desde = datetime.now(UTC) - timedelta(minutes=args.last_minutes) if args.last_minutes else None
    try:
        spans = traer_spans(args.endpoint.rstrip("/"), args.project, desde, headers)
    except httpx.HTTPError as exc:
        print(f"✘ No pude leer las trazas de Phoenix ({args.endpoint}, proyecto {args.project}): {exc}")
        return 2
    if not spans:
        print("No hay spans en ese proyecto/rango. ¿Ejecutaste tareas con OBSERVABILITY=phoenix?")
        return 1
    reporte = analizar(spans)
    imprimir(reporte)
    if args.salida_json:
        with open(args.salida_json, "w", encoding="utf-8") as f:
            json.dump(reporte, f, ensure_ascii=False, indent=2)
        print(f"\nReporte guardado en {args.salida_json}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
