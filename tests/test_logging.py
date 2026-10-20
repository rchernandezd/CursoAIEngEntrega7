import io
import sys

from logging_config import forzar_utf8_en_consola


def test_forzar_utf8_permite_imprimir_simbolos_aunque_la_consola_sea_cp1252(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    crudo = io.BytesIO()
    flujo = io.TextIOWrapper(crudo, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", flujo)
    forzar_utf8_en_consola()
    print("✔ García → ok")
    flujo.flush()
    assert crudo.getvalue().decode("utf-8").strip() == "✔ García → ok"
