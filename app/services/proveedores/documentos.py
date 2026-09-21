"""PDF generation port and its hand-written implementation (plan section 4).

RF-027 wants "un PDF con numeración correlativa... almacenado, enviado por
correo y publicado en la app". The plan is explicit about how far to go: a
**real minimal PDF 1.4 with text, written by hand, with no new dependency**.
No reportlab, no weasyprint, no headless browser.

That is less exotic than it sounds. A PDF is five objects - a catalogue, a page
tree, a page, a font and a content stream - plus a cross reference table with
the byte offset of each one, and :func:`_ensamblar` is exactly that. The result
opens in any reader, which is what makes RF-027 CA-02 ("puede descargarse en
PDF") a real download and not a text file with the wrong extension.

Two deliberate limits, both fine for a receipt:

* one page and one font (Helvetica, the base-14 font every reader carries, so
  nothing has to be embedded);
* ``WinAnsiEncoding``, which covers Spanish accents and the ``S/`` symbol. A
  character outside it is replaced rather than allowed to break the file.

:meth:`ProveedorDocumentos.reporte` is here for RF-034 (INC-8), which exports
the same way. Both are the same renderer with a different heading.
"""

import logging
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from app.config import settings

logger = logging.getLogger("aqualav.documentos")

MIME_PDF = "application/pdf"

#: A4 in PostScript points, the unit a PDF measures in.
ANCHO_PAGINA = 595
ALTO_PAGINA = 842

#: Where the first line sits and how far apart the lines are.
MARGEN_IZQUIERDO = 56
MARGEN_SUPERIOR = 786
INTERLINEADO = 16
TAMANIO_TITULO = 16
TAMANIO_TEXTO = 11

#: How many lines fit before the rest is dropped. A receipt never gets close.
LINEAS_MAXIMAS = 44

#: Encoding of the base-14 fonts. Anything outside it is replaced.
CODIFICACION = "cp1252"


@runtime_checkable
class ProveedorDocumentos(Protocol):
    """Anything able to turn a heading and some lines into a document."""

    def comprobante(self, titulo: str, lineas: Sequence[str]) -> bytes:
        """The PDF of one receipt (RF-027)."""
        ...

    def reporte(self, titulo: str, lineas: Sequence[str]) -> bytes:
        """The PDF of one report (RF-034)."""
        ...


def _escapar(texto: str) -> bytes:
    """Encode one line the way a PDF string literal needs it."""
    crudo = (texto or "").encode(CODIFICACION, errors="replace").decode(CODIFICACION)
    for original, escapado in (("\\", "\\\\"), ("(", "\\("), (")", "\\)")):
        crudo = crudo.replace(original, escapado)
    return crudo.encode(CODIFICACION, errors="replace")


def _contenido(titulo: str, lineas: Sequence[str]) -> bytes:
    """The page's drawing instructions: one text block, top to bottom."""
    partes: list[bytes] = [
        b"BT",
        b"/F1 %d Tf" % TAMANIO_TITULO,
        b"%d %d Td" % (MARGEN_IZQUIERDO, MARGEN_SUPERIOR),
        b"(" + _escapar(titulo) + b") Tj",
        b"ET",
        b"BT",
        b"/F1 %d Tf" % TAMANIO_TEXTO,
        b"%d %d Td" % (MARGEN_IZQUIERDO, MARGEN_SUPERIOR - INTERLINEADO * 2),
        b"%d TL" % INTERLINEADO,
    ]
    for indice, linea in enumerate(list(lineas)[:LINEAS_MAXIMAS]):
        if indice:
            partes.append(b"T*")
        partes.append(b"(" + _escapar(linea) + b") Tj")
    partes.append(b"ET")
    return b"\n".join(partes)


def _ensamblar(objetos: Iterable[bytes]) -> bytes:
    """Wrap the objects in a header, a cross reference table and a trailer.

    The cross reference table is a list of BYTE OFFSETS, so it can only be
    built while the file is being written. That is the whole reason this
    function exists instead of one long f-string.
    """
    cuerpo = b"%PDF-1.4\n"
    desplazamientos: list[int] = []
    for numero, objeto in enumerate(objetos, start=1):
        desplazamientos.append(len(cuerpo))
        cuerpo += b"%d 0 obj\n" % numero + objeto + b"\nendobj\n"

    inicio_xref = len(cuerpo)
    total = len(desplazamientos) + 1
    cuerpo += b"xref\n0 %d\n" % total
    cuerpo += b"0000000000 65535 f \n"
    for desplazamiento in desplazamientos:
        cuerpo += b"%010d 00000 n \n" % desplazamiento
    cuerpo += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % total
    cuerpo += b"startxref\n%d\n%%%%EOF\n" % inicio_xref
    return cuerpo


def renderizar(titulo: str, lineas: Sequence[str]) -> bytes:
    """A one page PDF 1.4 with ``titulo`` and ``lineas``, built by hand."""
    flujo = _contenido(titulo, lineas)
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>" % (ANCHO_PAGINA, ALTO_PAGINA),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica " b"/Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(flujo) + flujo + b"\nendstream",
    ]
    return _ensamblar(objetos)


class DocumentosSimulados:
    """The hand-written PDF writer. Deterministic and dependency free."""

    def __init__(self, registro: logging.Logger | None = None) -> None:
        self._registro = registro or logger

    def comprobante(self, titulo: str, lineas: Sequence[str]) -> bytes:
        documento = renderizar(titulo, lineas)
        self._registro.info("comprobante generado titulo=%s bytes=%s", titulo, len(documento))
        return documento

    def reporte(self, titulo: str, lineas: Sequence[str]) -> bytes:
        documento = renderizar(titulo, lineas)
        self._registro.info("reporte generado titulo=%s bytes=%s", titulo, len(documento))
        return documento


#: Registry of implementations, keyed by ``settings.documentos_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": DocumentosSimulados}


def proveedor_documentos() -> ProveedorDocumentos:
    """Build the configured generator. Unknown names fall back to the simulation."""
    clase = PROVEEDORES.get(settings.documentos_proveedor, DocumentosSimulados)
    return clase()
