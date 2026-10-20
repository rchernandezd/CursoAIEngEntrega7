"""Inyección determinística de fallas (solo demo y tests): permite probar el camino de fallo sin depender del azar.

Una instancia por ejecución (se inyecta; no es estado global).
"""

from dataclasses import dataclass, field

from exceptions import ErrorInyectado
from schemas import AnalysisArtifact


@dataclass(slots=True)
class InyectorFallas:
    excepcion_especialista: dict[str, int] = field(default_factory=dict)  # agente → veces que lanza excepción
    analisis_corrupto: int = 0  # veces que el resultado del analista sale alterado (+1)
    registro: list[str] = field(default_factory=list)

    def disparar_excepcion(self, agente: str) -> None:
        restantes = self.excepcion_especialista.get(agente, 0)
        if restantes > 0:
            self.excepcion_especialista[agente] = restantes - 1
            self.registro.append(f"excepcion:{agente}")
            raise ErrorInyectado(f"Falla simulada del especialista '{agente}'")

    def corromper_analisis(self, artefacto: AnalysisArtifact) -> AnalysisArtifact:
        if self.analisis_corrupto > 0:
            self.analisis_corrupto -= 1
            self.registro.append("analisis_corrupto")
            return artefacto.model_copy(update={"resultado": artefacto.resultado + 1})
        return artefacto
