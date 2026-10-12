"""Logo de la empresa para los PDFs."""
from uuid import UUID

from ..db import Conexion


def logo(conn: Conexion, empresa_id: UUID | str) -> bytes | None:
    """Bytes del logo (PNG) o None. Usa logo_empresa(), que también sirve desde el portal del colaborador."""
    fila = conn.execute("select datos from public.logo_empresa(%s)", (empresa_id,)).fetchone()
    return bytes(fila["datos"]) if fila else None
