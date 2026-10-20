"""Capturas (PNG) del dashboard de Arize Phoenix para la carpeta `screenshots/` (evidencia de observabilidad).

    pip install playwright && playwright install chromium        # una sola vez
    python -m scripts.capturar_dashboard --proyecto agente-m7

Genera, para el proyecto indicado:
    01_traces.png               lista de trazas (una por job) con latencia, tokens y costo
    02_metrics_p95_costo.png    pestaña Metrics: latencia p50/p95/p99, costo en USD y tokens por modelo
    03_sessions.png             sesiones (una por job; la reanudación tras la aprobación HITL queda en la misma)
    04_detalle_de_traza.png     árbol de spans de la primera traza (supervisor, investigador, analista, ...)

Antes de capturar, en la UI de Phoenix ajustá el selector de tiempo (arriba a la derecha) a "Last 15 Mins" para que las
gráficas muestren SOLO la corrida de las 5 peticiones concurrentes. Si preferís no instalar Playwright, hacé las mismas
capturas a mano (herramienta de recortes). Con LangSmith las capturas son manuales (Project → Monitor).
"""

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

type Point = tuple[float, float]


def id_de_proyecto(base_url: str, nombre: str) -> str:
    proyectos = httpx.get(f"{base_url}/v1/projects", timeout=15).json()["data"]
    for p in proyectos:
        if p["name"] == nombre:
            return str(p["id"])
    raise SystemExit(f"No existe el proyecto '{nombre}' en Phoenix. Existentes: {[p['name'] for p in proyectos]}")


def capturar(
    base_url: str,
    proyecto: str,
    salida: Path,
    espera_s: float,
    chromium: str | None,
    rango: str,
    tooltips: bool = False,
) -> list[Path]:
    from playwright.sync_api import FloatRect, Page, sync_playwright

    def centro_del_dato(pagina: Page, titulo: str, selector: str, filtro: Callable[[FloatRect], bool]) -> Point | None:
        """Centro del elemento SVG (punto / barra) con datos dentro de la tarjeta `titulo`. Se busca en el DOM y no por
        coordenadas fijas porque el eje de tiempo se desplaza cada minuto."""
        tarjeta = (
            pagina.locator("div", has=pagina.get_by_text(titulo, exact=True)).filter(has=pagina.locator("svg")).last
        )
        elementos = tarjeta.locator(selector)
        cajas = [c for i in range(elementos.count()) if (c := elementos.nth(i).bounding_box()) and filtro(c)]
        if not cajas:
            return None
        c = max(cajas, key=lambda caja: caja["height"])
        return c["x"] + c["width"] / 2, c["y"] + c["height"] / 2

    def tooltips_de_metricas(pagina: Page, foto: Callable[[str], None]) -> None:
        """Con un solo intervalo de tiempo con datos, P95 y costo son un punto/barra sin valor legible: se pasa el
        cursor para que el tooltip muestre p50/p95/p99 y el costo/tokens (02a y 02b)."""
        objetivos = (
            ("Trace latency", "circle", lambda c: 400 < c["y"] < 580 and c["x"] > 300, "02a_latencia_p95.png"),
            (
                "Cost",
                "path, rect",
                lambda c: c["width"] < 30 and c["height"] > 20 and 640 < c["y"] < 880,
                "02b_costo_por_ejecucion.png",
            ),
        )
        for titulo, selector, filtro, archivo in objetivos:
            punto = centro_del_dato(pagina, titulo, selector, filtro)
            if punto is None:
                print(f"✘ no encontré el dato de '{titulo}' en el DOM; hacé esa captura a mano")
                continue
            pagina.mouse.move(*punto)
            time.sleep(1.5)
            foto(archivo)

    def fijar_rango(pagina: Page) -> None:
        """El selector de tiempo arranca en 'Last 7 Days': con ese rango la corrida es un punto en los gráficos."""
        boton = pagina.get_by_role("button", name=re.compile(r"Last (\d+ \w+|Hour|Day|Month)"))
        if rango.lower() in boton.first.inner_text().lower():
            return
        boton.first.click()
        pagina.get_by_role("option", name=rango, exact=True).click()
        time.sleep(espera_s)

    def compactar_columnas(pagina: Page) -> None:
        """Quita columnas anchas sin datos para que latencia, tokens y COSTO POR TRAZA entren en la captura."""
        pagina.get_by_role("button", name="Columns").click()
        for columna in ("output_value", "error", "userId", "annotations", "traceAnnotations"):
            # force: el checkbox de Phoenix es un <svg> superpuesto al <input>; Playwright igual verifica el cambio
            pagina.locator(f'input[type="checkbox"][name="{columna}"]').uncheck(force=True)
        pagina.keyboard.press("Escape")

    salida.mkdir(parents=True, exist_ok=True)
    url = f"{base_url}/projects/{id_de_proyecto(base_url, proyecto)}"
    generadas: list[Path] = []
    with sync_playwright() as p:
        navegador = p.chromium.launch(executable_path=chromium) if chromium else p.chromium.launch()
        pagina = navegador.new_page(viewport={"width": 1700, "height": 1300})

        def foto(nombre: str) -> None:
            time.sleep(espera_s)  # los gráficos de Phoenix se pintan después de cargar
            destino = salida / nombre
            pagina.screenshot(path=str(destino), full_page=True)
            generadas.append(destino)
            print(f"✔ {destino}")

        pagina.goto(url, wait_until="networkidle")
        for pestana, archivo in (
            ("Traces", "01_traces.png"),
            ("Metrics", "02_metrics_p95_costo.png"),
            ("Sessions", "03_sessions.png"),
        ):
            pagina.get_by_role("tab", name=pestana).click()
            fijar_rango(pagina)
            if pestana == "Traces":
                compactar_columnas(pagina)
            foto(archivo)
            if pestana == "Metrics" and tooltips:
                tooltips_de_metricas(pagina, foto)
        pagina.get_by_role("tab", name="Traces").click()
        fijar_rango(pagina)
        time.sleep(espera_s)
        pagina.locator("a", has_text="tarea_multiagente").first.click()  # abre el árbol de spans de la primera traza
        foto("04_detalle_de_traza.png")
        navegador.close()
    return generadas


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"))
    p.add_argument("--proyecto", default=os.getenv("PHOENIX_PROJECT_NAME", "agente-m7"))
    p.add_argument("--salida", default="screenshots")
    p.add_argument("--espera", type=float, default=3.0, help="segundos de espera antes de cada captura")
    p.add_argument("--chromium", default=os.getenv("CHROMIUM_PATH"), help="ruta a un Chromium ya instalado (opcional)")
    p.add_argument(
        "--rango", default="Last 15 Min", help="rango de tiempo de Phoenix (Last 15 Min | Last Hour | Last Day ...)"
    )
    p.add_argument(
        "--tooltips",
        action="store_true",
        help="además genera 02a_latencia_p95.png y 02b_costo_por_ejecucion.png con el cursor sobre el dato (tooltip)",
    )
    args = p.parse_args()
    try:
        capturar(
            args.url.rstrip("/"),
            args.proyecto,
            Path(args.salida),
            args.espera,
            args.chromium,
            args.rango,
            args.tooltips,
        )
    except ImportError:
        print("Falta Playwright: pip install playwright && playwright install chromium")
        return 2
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
