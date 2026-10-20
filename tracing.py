"""Traza del flujo de delegación: quién decidió qué y por qué (entregable en .json y .log)."""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

type JSONDict = dict[str, Any]

TRUNCAR = 300


class Verificacion(BaseModel):
    descripcion: str
    ok: bool
    detalle: str = ""


class EventoTraza(BaseModel):
    orden: int
    nodo: Literal["supervisor", "investigador", "analista", "validador", "sintesis", "revision_humana"]
    titulo: str
    detalle: JSONDict = Field(default_factory=dict)
    duracion_ms: int


class EscenarioTraza(BaseModel):
    id: str
    titulo: str
    criterio: str
    pregunta: str
    eventos: list[EventoTraza]
    respuesta: str
    estado_final: str | None
    motivo_escalamiento: str | None
    pasos: int
    intentos: dict[str, int]
    fallos_validacion: dict[str, int]
    aprobaciones: dict[str, bool]
    research_output: JSONDict | None
    analysis_output: JSONDict | None
    decisiones: list[JSONDict]
    duracion_ms: int
    tokens: dict[str, int] = Field(default_factory=dict)
    verificaciones: list[Verificacion] = Field(default_factory=list)

    @property
    def orden_nodos(self) -> list[str]:
        return [e.nodo for e in self.eventos]


class TrazaEjecucion(BaseModel):
    metadata: JSONDict
    escenarios: list[EscenarioTraza]
    resumen: JSONDict


def _marca(valor: bool) -> str:
    return "✔" if valor else "✘"


def _recortar(texto: str, n: int = TRUNCAR) -> str:
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def evento_desde_update(nodo: str, update: JSONDict, orden: int, duracion_ms: int) -> EventoTraza:
    """Convierte la actualización de un nodo (stream_mode='updates') en un evento legible."""
    detalle: JSONDict = {}
    match nodo:
        case "supervisor":
            d = update["decisiones"][0]
            titulo = f"supervisor decide → {d.siguiente} ({d.origen})"
            detalle = {
                "razon": d.razon,
                "instruccion": d.instruccion,
                "rubrica": d.rubrica.model_dump() if d.rubrica else None,
                "sobrescrituras": d.sobrescrituras,
                "motivo_escalamiento": update.get("motivo_escalamiento"),
            }
        case "investigador" | "analista":
            c = update["contribuciones"][0]
            titulo = f"{nodo} (intento {c.intento}): {c.resumen}"
            detalle = {"herramientas": c.detalle.get("herramientas", []), "ok_ejecucion": c.ok is not False}
        case "validador":
            c = update["contribuciones"][0]
            titulo = f"validador {'✔' if c.ok else '✘'} {c.resumen}"
            detalle = {"para": c.detalle.get("para"), "hallazgos": c.detalle.get("hallazgos", [])}
        case _:
            titulo = f"{nodo}: respuesta redactada"
            detalle = {"respuesta": update.get("respuesta_final")}
    return EventoTraza(orden=orden, nodo=nodo, titulo=titulo, detalle=detalle, duracion_ms=duracion_ms)


def render_escenario(t: EscenarioTraza) -> str:
    """Formato del log. Una línea por evento + sangría con el detalle (razón, herramientas, errores)."""
    out = [f"=== {t.id} · {t.titulo} ===", f'Usuario: "{t.pregunta}"']
    for e in t.eventos:
        out.append(f"[{e.orden}] {e.titulo}")
        d = e.detalle
        if e.nodo == "supervisor":
            out.append(f"      razón: {_recortar(d['razon'])}")
            if d["instruccion"]:
                out.append(f'      instrucción: "{_recortar(d["instruccion"])}"')
            if d["rubrica"]:
                r = d["rubrica"]
                out.append(
                    f"      rúbrica: completitud {_marca(r['completitud'])}"
                    f" · respaldo {_marca(r['respaldo_en_evidencia'])}"
                    f" · consistencia {_marca(r['consistencia'])}"
                )
            if d["sobrescrituras"]:
                out.append(f"      guardrail: {', '.join(d['sobrescrituras'])}")
        elif e.nodo in ("investigador", "analista"):
            for h in d.get("herramientas", []):
                args = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}" for k, v in h["args"].items())
                out.append(f"      herramienta: {h['nombre']}({_recortar(args, 120)}) → {_recortar(h['salida'], 120)}")
        elif e.nodo == "validador":
            for h in d.get("hallazgos", []):
                out.append(f"      {h['severidad']}: [{h['codigo']}] {_recortar(h['mensaje'], 200)}")
    out.append(f'Respuesta final: "{_recortar(t.respuesta, 600)}"')
    motivo = f" · motivo: {t.motivo_escalamiento}" if t.motivo_escalamiento else ""
    out.append(
        f"({t.estado_final}{motivo} · {t.pasos} paso(s) de especialista · {len(t.eventos)} nodos · "
        f"{t.duracion_ms / 1000:.1f} s" + (f" · {t.tokens.get('total_tokens', 0)} tokens" if t.tokens else "") + ")"
    )
    out.extend(
        f"  {'✔' if v.ok else '✘'} {v.descripcion}" + (f" — {v.detalle}" if v.detalle else "") for v in t.verificaciones
    )
    return "\n".join(out)


def render_log(traza: TrazaEjecucion) -> str:
    cabecera = [f"# {k}: {v}" for k, v in traza.metadata.items()]
    cuerpo = [render_escenario(e) for e in traza.escenarios]
    pie = ["# resumen: " + json.dumps(traza.resumen, ensure_ascii=False)]
    return "\n".join(cabecera) + "\n\n" + "\n\n".join(cuerpo) + "\n\n" + "\n".join(pie) + "\n"


def guardar_traza(traza: TrazaEjecucion, directorio: Path) -> tuple[Path, Path]:
    directorio.mkdir(parents=True, exist_ok=True)
    ruta_json, ruta_log = directorio / "traza_ejecucion.json", directorio / "traza_ejecucion.log"
    ruta_json.write_text(traza.model_dump_json(indent=2), encoding="utf-8")
    ruta_log.write_text(render_log(traza), encoding="utf-8")
    return ruta_json, ruta_log
