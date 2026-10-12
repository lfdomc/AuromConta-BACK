"""Planilla Costa Rica · API (FastAPI en Railway)."""
import importlib
from contextlib import asynccontextmanager
import logging

import planilla_motor
import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .seguridad import LimiteCuerpo, SeguridadMiddleware

log = logging.getLogger("aurom")

@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    sincronizar_ajustes_seguridad()
    yield


def sincronizar_ajustes_seguridad() -> None:
    """Activa la MFA también dentro de la base (vale para cualquier vía de acceso)."""
    try:
        with db.sistema() as conn:
            conn.execute("update public.ajustes_seguridad set valor = %s where clave = 'mfa_requerida'",
                         ("si" if settings.mfa_obligatoria else "no",))
    except Exception:  # noqa: BLE001 - sin base al arrancar: /salud lo reportará
        log.warning("No se pudieron sincronizar los ajustes de seguridad")


app = FastAPI(
    lifespan=ciclo_de_vida,
    docs_url="/docs" if settings.documentacion_publica else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.documentacion_publica else None,
    title="Planilla Costa Rica API",
    version="1.0.0",
    description="Planillas de Costa Rica multiempresa. Toda la lógica de montos vive aquí (motor "
                f"planilla_motor {planilla_motor.__version__}). Autenticación: JWT de Supabase en "
                "`Authorization: Bearer`.",
)
# La sesión viaja en la cabecera Authorization (no en cookies): no hace falta allow_credentials.
app.add_middleware(SeguridadMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
                   expose_headers=["X-Request-ID", "X-Aurom-Error", "Retry-After", "Content-Disposition",
                                   "Idempotent-Replayed", "X-Advertencias"], max_age=600)
# Lo último que se agrega es lo primero que corre: el límite de tamaño va por fuera de todo.
app.add_middleware(LimiteCuerpo)


# Routers por fase. Cada módulo expone `r` (APIRouter) y opcionalmente `routers` (lista extra).
MODULOS = [
    "empresas",       # Fase 1: sesión, empresas, usuarios, contadores asignados, catálogos
    "empleados",      # Fase 1
    "planillas",      # Fase 1
    "novedades",      # Fase 2
    "anuales",        # Fase 3: aguinaldo y liquidaciones
    "reportes",       # Fase 4: reportes, exportaciones, asientos, dashboard
    "machote",        # Machote completo de importación (como el del módulo Odoo); antes de /importaciones/{tipo}
    "importaciones",  # Fase 5: importadores Excel y migración desde Odoo
    "saas",           # Fase 6: alta, portal del empleado, panel de plataforma
    "negocio",        # Fase 7: facturación electrónica, cobros, gastos, IVA, renta
    "autogestion",    # Fase 8: Tilopay, Telegram, soporte, normativa, monitoreo, cron
    "socios",         # Fase 8: portal de contadores/vendedores, prospectos, comisiones
    "contabilidad",   # Módulo Contabilidad (C1-C3): catálogo, asientos, periodos, reportes
]
for nombre in MODULOS:
    try:
        mod = importlib.import_module(f".routers.{nombre}", __package__)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(f"routers.{nombre}"):
            log.warning("Módulo %s aún no disponible", nombre)
            continue
        raise
    if hasattr(mod, "r") and mod.r not in getattr(mod, "routers", []):
        app.include_router(mod.r)
    for extra in getattr(mod, "routers", []):
        app.include_router(extra)
    for extra in getattr(mod, "catalogos", []):
        app.include_router(extra)


@app.get("/salud", tags=["sistema"])
def salud() -> dict:
    with db.sistema() as conn:
        conn.execute("select 1")
    return {"ok": True, "motor": planilla_motor.__version__, "entorno": settings.entorno}


@app.exception_handler(psycopg.errors.ForeignKeyViolation)
async def error_referencia(request: Request, exc: Exception):
    return JSONResponse({"detail": "Algún dato hace referencia a un registro que no existe en esta empresa"},
                        status_code=409)


# Mensajes claros para los duplicados de identificación (migración 0018).
DUPLICADOS = {
    "empresas_cedula_unica": "Ya existe una empresa con esa cédula jurídica. Si es la suya, pida acceso a quien la "
                             "administra o escriba a soporte de A.U.R.O.M.",
    "empleados_identificacion_unica": "Ya existe un colaborador con esa identificación en esta empresa.",
    "empleados_empresa_id_identificacion_key": "Ya existe un colaborador con esa identificación en esta empresa.",
    "clientes_identificacion_unica": "Ya existe un cliente con esa identificación.",
}


@app.exception_handler(psycopg.errors.UniqueViolation)
async def error_duplicado(request: Request, exc: Exception):
    restriccion = getattr(getattr(exc, "diag", None), "constraint_name", None) or ""
    return JSONResponse({"detail": DUPLICADOS.get(restriccion, "Ya existe un registro con esos datos")}, status_code=409)


@app.exception_handler(psycopg.errors.CheckViolation)
async def error_regla(request: Request, exc: Exception):
    return JSONResponse({"detail": "Los datos no cumplen una regla de validación"}, status_code=422)


@app.exception_handler(psycopg.errors.RaiseException)
async def error_negocio(request: Request, exc: Exception):
    msg = getattr(getattr(exc, "diag", None), "message_primary", None) or "Operación no permitida"
    if "dos pasos" in msg:
        return JSONResponse({"detail": msg}, status_code=403, headers={"X-Aurom-Error": "mfa_requerida"})
    return JSONResponse({"detail": msg}, status_code=409)


@app.exception_handler(psycopg.errors.InsufficientPrivilege)
async def error_permiso(request: Request, exc: Exception):
    return JSONResponse({"detail": "Su rol no permite esta acción"}, status_code=403)


@app.exception_handler(Exception)
async def error_general(request: Request, exc: Exception):
    log.exception("Error no controlado en %s", request.url.path)
    return JSONResponse({"detail": "Error interno. Ya quedó registrado."}, status_code=500)
