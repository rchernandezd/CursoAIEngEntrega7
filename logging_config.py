"""Logging por consola. Nunca se loguean API keys."""

import io
import logging
import sys


def forzar_utf8_en_consola() -> None:
    """Windows redirige stdout/stderr con cp1252 y los símbolos del log (✔ ✘ →) lanzarían UnicodeEncodeError."""
    for flujo in (sys.stdout, sys.stderr):
        if isinstance(flujo, io.TextIOWrapper):
            flujo.reconfigure(encoding="utf-8", errors="replace")


def configurar_logging(nivel: str = "INFO") -> None:
    logger = logging.getLogger()
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(nivel)
    for ruidoso in ("httpx", "httpcore", "openai", "anthropic", "langchain", "langgraph", "pinecone", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
