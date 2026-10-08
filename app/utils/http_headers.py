"""Construcción segura de cabeceras HTTP."""

import unicodedata
from urllib.parse import quote


def _ascii_fallback(filename: str) -> str:
    """Versión ASCII latin-1-safe del nombre (para el parámetro ``filename=``)."""
    folded = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(c for c in folded if c not in '"\\\r\n')
    return cleaned.strip() or "archivo"


def build_content_disposition(filename: str | None, disposition: str = "attachment") -> str:
    """Cabecera ``Content-Disposition`` segura para cualquier nombre.

    Normaliza a NFC (los navegadores de macOS envían NFD: ``ó`` como
    ``o`` + U+0301) y emite el fallback ASCII latin-1 más el parámetro
    RFC 5987 ``filename*=UTF-8''...`` con el nombre real.
    """
    nfc = unicodedata.normalize("NFC", filename or "archivo")
    ascii_name = _ascii_fallback(nfc)
    encoded = quote(nfc, safe="")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'