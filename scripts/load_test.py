"""Prueba de carga: N peticiones concurrentes contra la API + medición de latencia (p50 / p95) desde el cliente.

    python -m scripts.load_test --base-url http://localhost:8000 --n 5
    python -m scripts.load_test --n 5 --critica --aprobar     # la consulta pide un correo a RRHH y se aprueba sola

Qué demuestra (rúbrica "Evidencia de monitoreo"):
  - `POST /tasks` responde 202 en milisegundos aunque el agente tarde segundos (no bloquea el event loop).
  - `GET /health` sigue respondiendo rápido MIENTRAS los jobs se ejecutan.
  - Cada job termina en DONE/FAILED y se mide su latencia total (creación → resultado), con p50 y p95.
Guarda el detalle en `screenshots/carga_<fecha>.json`. Termina con código 1 si algún job FALLÓ o no terminó.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

CONSULTA = "Un empleado lleva 12 años en TechCorp. ¿Cuántos días de vacaciones tiene por año y cuántos suma en 3 años?"
SUFIJO_CRITICA = " Envía el resultado por correo a RRHH."
TERMINALES = {"DONE", "FAILED"}


@dataclass
class Resultado:
    n: int
    job_id: str = ""
    http_status: int = 0
    aceptado_ms: float = 0.0  # tiempo que tardó POST /tasks en responder
    total_s: float = 0.0  # desde el POST hasta el estado terminal
    estado: str = "SIN_TERMINAR"
    pasos: list[str] = field(default_factory=list)
    error: str | None = None


def percentil(valores: list[float], p: float) -> float:
    """Percentil con interpolación lineal (el mismo criterio que numpy.percentile por defecto)."""
    if not valores:
        return float("nan")
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * p / 100
    bajo, alto = int(k), min(int(k) + 1, len(ordenados) - 1)
    return ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (k - bajo)


async def una_tarea(
    cliente: httpx.AsyncClient, n: int, query: str, *, aprobar: bool, poll_s: float, timeout_s: float
) -> Resultado:
    res = Resultado(n=n)
    t0 = time.perf_counter()
    r = await cliente.post("/tasks", json={"query": query})
    res.aceptado_ms = (time.perf_counter() - t0) * 1000
    res.http_status = r.status_code
    if r.status_code != 202:
        res.estado, res.error = "RECHAZADO", f"HTTP {r.status_code}: {r.text[:200]}"
        return res
    res.job_id = r.json()["job_id"]
    aprobado = False
    while time.perf_counter() - t0 < timeout_s:
        vista = (await cliente.get(f"/tasks/{res.job_id}")).json()
        res.estado, res.pasos = vista["status"], vista.get("progress", [])
        if vista["status"] == "WAITING_APPROVAL" and aprobar and not aprobado:
            await cliente.post(f"/tasks/{res.job_id}/approve", json={"aprobado": True, "revisor": "load_test"})
            aprobado = True
        elif vista["status"] in TERMINALES:
            res.error = vista.get("error")
            break
        await asyncio.sleep(poll_s)
    res.total_s = time.perf_counter() - t0
    return res


async def sondear_health(cliente: httpx.AsyncClient, parar: asyncio.Event) -> list[float]:
    """Mientras corren los jobs, mide cuánto tarda GET /health: si el event loop se bloqueara, se dispararía."""
    latencias: list[float] = []
    while not parar.is_set():
        t0 = time.perf_counter()
        try:
            await cliente.get("/health", timeout=10)
            latencias.append((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            latencias.append(float("inf"))
        await asyncio.sleep(0.25)
    return latencias


def guardar(carpeta: Path, contenido: str) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"carga_{datetime.now():%Y%m%d_%H%M%S}.json"
    archivo.write_text(contenido, encoding="utf-8")
    return archivo


async def main(args: argparse.Namespace) -> int:
    query = args.query or (CONSULTA + (SUFIJO_CRITICA if args.critica else ""))
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30) as cliente:
        try:
            salud = await cliente.get("/health")
        except httpx.HTTPError as exc:
            print(f"✘ No se pudo conectar con {args.base_url}: {exc}\n  ¿Está levantada la API? (docker compose up)")
            return 2
        print(f"API: {args.base_url}  health={salud.json()}")
        print(f"Lanzando {args.n} peticiones concurrentes…\n")
        parar = asyncio.Event()
        sonda = asyncio.create_task(sondear_health(cliente, parar))
        t0 = time.perf_counter()
        resultados = await asyncio.gather(
            *(
                una_tarea(cliente, i + 1, query, aprobar=args.aprobar, poll_s=args.poll, timeout_s=args.timeout)
                for i in range(args.n)
            )
        )
        pared = time.perf_counter() - t0
        parar.set()
        health_ms = await sonda

    print(f"{'#':>2}  {'job_id':36}  {'POST→202':>9}  {'total':>8}  estado")
    for r in resultados:
        print(f"{r.n:>2}  {r.job_id or '-':36}  {r.aceptado_ms:7.0f}ms  {r.total_s:7.2f}s  {r.estado}")
    terminados = [r.total_s for r in resultados if r.estado == "DONE"]
    aceptados = [r.aceptado_ms for r in resultados if r.http_status == 202]
    sanos = [h for h in health_ms if h != float("inf")]
    resumen = {
        "n": args.n,
        "tiempo_pared_s": round(pared, 2),
        "done": sum(r.estado == "DONE" for r in resultados),
        "failed": sum(r.estado == "FAILED" for r in resultados),
        "sin_terminar": sum(r.estado not in TERMINALES for r in resultados),
        "latencia_total_p50_s": round(percentil(terminados, 50), 2),
        "latencia_total_p95_s": round(percentil(terminados, 95), 2),
        "latencia_total_media_s": round(statistics.fmean(terminados), 2) if terminados else None,
        "post_202_max_ms": round(max(aceptados), 1) if aceptados else None,
        "health_durante_la_carga_max_ms": round(max(sanos), 1) if sanos else None,
        "health_fallidos": len(health_ms) - len(sanos),
    }
    print("\nResumen:")
    for clave, valor in resumen.items():
        print(f"  {clave:34} {valor}")
    suma = sum(terminados)
    if terminados and pared < suma * 0.8:
        print(f"\n✔ Concurrencia real: {args.n} jobs en {pared:.1f}s de pared (secuencial serían ≈{suma:.1f}s).")

    contenido = json.dumps({"resumen": resumen, "jobs": [asdict(r) for r in resultados]}, ensure_ascii=False, indent=2)
    archivo = await asyncio.to_thread(guardar, Path(args.salida), contenido)
    print(f"\nDetalle guardado en {archivo}")
    return 0 if resumen["done"] == args.n else 1


def parsear() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.getenv("API_URL", "http://localhost:8000"))
    p.add_argument("--n", type=int, default=5, help="peticiones concurrentes (la consigna pide 5)")
    p.add_argument("--query", help="consulta a enviar (por defecto, la de vacaciones)")
    p.add_argument("--critica", action="store_true", help="agrega 'envía el resultado por correo a RRHH' (activa HITL)")
    p.add_argument(
        "--aprobar", action="store_true", help="aprueba automáticamente los jobs que queden en WAITING_APPROVAL"
    )
    p.add_argument("--api-key", default=os.getenv("API_KEY", ""))
    p.add_argument("--poll", type=float, default=0.5, help="segundos entre consultas de estado")
    p.add_argument("--timeout", type=float, default=300, help="segundos máximos por job")
    p.add_argument("--salida", default="screenshots", help="carpeta donde guardar el JSON")
    return p.parse_args()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows: la consola cp1252 no imprime ✔/→
    sys.exit(asyncio.run(main(parsear())))
