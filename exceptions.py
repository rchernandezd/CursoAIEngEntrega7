"""Excepciones del proyecto. Cada mensaje dice qué hacer para resolver el problema."""


class OrquestadorError(RuntimeError):
    """Base de los errores propios del proyecto."""


class ConfigError(OrquestadorError):
    """Configuración inválida o incompleta."""


class MissingApiKeyError(ConfigError):
    """Falta la API key del proveedor activo."""


class ProveedorNoDisponibleError(ConfigError):
    """El proveedor o backend pedido requiere un paquete que no está instalado."""


class ExpresionNoPermitidaError(ValueError):
    """La expresión matemática usa algo fuera de la aritmética permitida."""


class ErrorInyectado(OrquestadorError):
    """Falla simulada por el inyector de fallas (solo demo y tests)."""
