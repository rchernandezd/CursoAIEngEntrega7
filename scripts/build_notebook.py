"""Genera demo_delegacion.ipynb (celdas definidas aquí, sin salidas).

    python scripts/build_notebook.py                 # crea el .ipynb vacío
    python scripts/build_notebook.py --execute       # además lo ejecuta (requiere API key) y guarda las salidas

El notebook NO instala paquetes ni pide claves por getpass: usa el mismo .env y los mismos módulos que el repo.
"""

import argparse
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "demo_delegacion.ipynb"

CELDAS = [
    new_markdown_cell(
        "# Demo — Orquestador multi-agente (Pre-entrega 6)\n\n"
        "Muestra el **flujo de delegación**: el Supervisor decide quién interviene, el Investigador consulta las "
        "políticas, el Analista calcula, el Validador comprueba cada salida con código determinístico y la Síntesis "
        "cierra. Ejecutá las celdas en orden (requiere `.env` con la API key; ver README)."
    ),
    new_code_cell(
        "from IPython.display import Image, Markdown, display\n\n"
        "from config import Settings\n"
        "from escenarios import ESCENARIOS, ejecutar_escenario\n"
        "from graph import compilar, construir_dependencias\n"
        "from tracing import render_escenario\n\n"
        "settings = Settings.from_env()\n"
        'print(f"Proveedor: {settings.llm_provider} · modelo: {settings.modelo_activo}")\n'
        'print(f"Retriever: {settings.retriever_backend}")\n'
        'print(f"Topes: MAX_PASOS={settings.max_pasos} · MAX_REPARACIONES={settings.max_reparaciones}")\n'
        'print(f"recursion_limit={settings.recursion_limit}")'
    ),
    new_markdown_cell("## 1. Topología del grafo"),
    new_code_cell(
        "app = compilar(construir_dependencias(settings))\n"
        "grafo = app.get_graph()\n"
        "try:\n"
        "    display(Image(grafo.draw_mermaid_png()))  # usa mermaid.ink (necesita red)\n"
        "except Exception:\n"
        '    display(Markdown("```mermaid\\n" + grafo.draw_mermaid() + "\\n```"))'
    ),
    new_markdown_cell(
        "## 2. Flujo completo (E1): Supervisor → Investigador → Analista → Síntesis\n\n"
        "Cada línea `[n]` es un nodo del grafo; el supervisor explica *por qué* delega."
    ),
    new_code_cell(
        "e1 = ESCENARIOS[0]\n"
        'print(f"Usuario: {e1.pregunta}\\n")\n'
        't1 = await ejecutar_escenario(e1, settings, on_evento=lambda e: print(f"[{e.orden}] {e.titulo}"))\n'
        'print("\\nRespuesta:", t1.respuesta)'
    ),
    new_code_cell("print(render_escenario(t1))"),
    new_markdown_cell(
        "## 3. Estado compartido: quién aportó qué\n\n"
        "Cada especialista escribe solo en su parcela (`research_output`, `analysis_output`); el validador marca "
        "`aprobaciones`; `decisiones` guarda el log del supervisor."
    ),
    new_code_cell(
        'print("Investigador:", t1.research_output)\n'
        'print("Analista:    ", t1.analysis_output)\n'
        'print("Aprobaciones:", t1.aprobaciones, "· intentos:", t1.intentos)\n'
        "for d in t1.decisiones:\n"
        "    print(f\"  decisión {d['paso']} [{d['origen']}] → {d['siguiente']}: {d['razon']}\")"
    ),
    new_markdown_cell(
        "## 4. Ruteo dinámico (E2 y E3)\n\n"
        "No hay una ruta fija: si la pregunta ya trae los números va directo al analista (E2); si solo pide un dato, "
        "solo investiga (E3)."
    ),
    new_code_cell(
        "for esc in (ESCENARIOS[1], ESCENARIOS[2]):\n"
        "    t = await ejecutar_escenario(esc, settings)\n"
        "    print(f\"{esc.id}: {' → '.join(t.orden_nodos)}\\n   {t.respuesta[:200]}\\n\")"
    ),
    new_markdown_cell(
        "## 5. Camino de fallo (E5, E7, E8)\n\n"
        "E5: el validador rechaza un cálculo inconsistente y el supervisor reenvía el error específico. "
        "E7: un especialista lanza una excepción y se recupera. E8: la validación no se resuelve y se escala a "
        "revisión humana (sin bucle infinito)."
    ),
    new_code_cell(
        "for esc in (ESCENARIOS[4], ESCENARIOS[6], ESCENARIOS[7]):\n"
        "    t = await ejecutar_escenario(esc, settings)\n"
        "    print(render_escenario(t))\n"
        '    print("-" * 80)'
    ),
    new_markdown_cell(
        "## 6. Resumen\n\n"
        "Las verificaciones automáticas de cada escenario están en `traces/traza_ejecucion.json` "
        "(generado con `python demo.py`)."
    ),
]


def construir() -> nbformat.NotebookNode:
    nb = new_notebook(cells=CELDAS)
    nb.metadata["kernelspec"] = {"display_name": "Python 3", "language": "python", "name": "python3"}
    nbformat.validate(nb)
    return nb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="ejecuta el notebook y guarda las salidas")
    args = parser.parse_args()
    nbformat.write(construir(), DESTINO)
    print(f"Notebook creado: {DESTINO}")
    if args.execute:
        from nbconvert.preprocessors import ExecutePreprocessor

        nb = nbformat.read(DESTINO, as_version=4)
        ExecutePreprocessor(timeout=600, kernel_name="python3").preprocess(nb, {"metadata": {"path": str(RAIZ)}})
        nbformat.write(nb, DESTINO)
        print("Notebook ejecutado y guardado con salidas.")


if __name__ == "__main__":
    main()
