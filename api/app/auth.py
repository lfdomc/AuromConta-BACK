"""Validación del JWT de Supabase y contexto del usuario."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterator
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Path, Request, status

from . import db
from .config import settings
from .db import Conexion


@dataclass
class Usuario:
    id: str
    email: str | None
    claims: dict[str, Any]


@lru_cache
def _jwks_client() -> jwt.PyJWKClient | None:
    if not settings.supabase_url:
        return None
    return jwt.PyJWKClient(f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json", cache_keys=True)


_OPCIONES = {"require": ["exp", "sub", "aud"]}


def _emisor() -> str | None:
    """Supabase firma con iss = <proyecto>/auth/v1. Sin SUPABASE_URL (pruebas) no se exige."""
    return f"{settings.supabase_url.rstrip('/')}/auth/v1" if settings.supabase_url else None


def verificar_token(token: str) -> dict[str, Any]:
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
        if alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise jwt.InvalidTokenError("HS256 no configurado")
            return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"],
                              audience=settings.jwt_audience, issuer=_emisor(), options=_OPCIONES)
        cliente = _jwks_client()
        if cliente is None:
            raise jwt.InvalidTokenError("JWKS no configurado")
        clave = cliente.get_signing_key_from_jwt(token)
        return jwt.decode(token, clave.key, algorithms=["ES256", "RS256", "EdDSA"], audience=settings.jwt_audience,
                          issuer=_emisor(), options=_OPCIONES)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Sesión inválida: {exc}") from exc


def usuario_actual(authorization: str = Header(default="")) -> Usuario:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de sesión")
    claims = verificar_token(authorization.split(" ", 1)[1].strip())
    if not claims.get("sub"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token sin usuario")
    return Usuario(id=claims["sub"], email=claims.get("email"), claims=claims)


def conexion(u: Usuario = Depends(usuario_actual)) -> Iterator[Conexion]:
    """Conexión de la petición, con RLS del usuario. Hace commit al terminar."""
    with db.como_usuario(u.claims) as conn:
        yield conn


ROLES_OPERAR = {"admin", "aprobador", "operador", "contador_operacion", "plataforma"}
ROLES_APROBAR = {"admin", "aprobador", "plataforma"}
ROLES_ADMIN = {"admin", "plataforma"}


@dataclass
class Contexto:
    usuario: Usuario
    conn: Conexion
    empresa_id: UUID
    rol: str

    @property
    def puede_operar(self) -> bool:
        return self.rol in ROLES_OPERAR

    @property
    def puede_aprobar(self) -> bool:
        return self.rol in ROLES_APROBAR

    def exigir(self, roles: set[str], accion: str = "esta acción") -> None:
        if self.rol not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Su rol ({self.rol}) no permite {accion}")


def contexto_empresa(empresa_id: UUID = Path(...), u: Usuario = Depends(usuario_actual),
                     conn: Conexion = Depends(conexion)) -> Contexto:
    fila = conn.execute("select public.rol_en_empresa(%s) as rol", (empresa_id,)).fetchone()
    if not fila or not fila["rol"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Empresa no encontrada")
    return Contexto(u, conn, empresa_id, fila["rol"])


MFA_ENCABEZADO = {"X-Aurom-Error": "mfa_requerida"}


def exigir_mfa(u: Usuario) -> None:
    """Acciones críticas: la sesión debe tener segundo factor verificado (Supabase: aal2)."""
    if settings.mfa_obligatoria and u.claims.get("aal") != "aal2":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Esta acción requiere verificación en dos pasos (MFA)",
                            headers=MFA_ENCABEZADO)


def operador(ctx: Contexto = Depends(contexto_empresa)) -> Contexto:
    ctx.exigir(ROLES_OPERAR, "modificar datos")
    return ctx


def aprobador(ctx: Contexto = Depends(contexto_empresa)) -> Contexto:
    ctx.exigir(ROLES_APROBAR, "aprobar")
    exigir_mfa(ctx.usuario)
    return ctx


def admin_empresa(ctx: Contexto = Depends(contexto_empresa)) -> Contexto:
    ctx.exigir(ROLES_ADMIN, "administrar la empresa")
    exigir_mfa(ctx.usuario)
    return ctx


MODULO_INACTIVO = {"X-Aurom-Error": "modulo_inactivo"}
NOMBRES_MODULO = {"planilla": "Planilla", "contabilidad": "Contabilidad"}


def requiere_modulo(modulo: str):
    """Dependencia de router: con el módulo apagado se puede consultar y exportar lo que
    ya existe, pero no registrar nada nuevo. La base aplica la misma regla (RLS)."""
    def _dep(request: Request, empresa_id: UUID = Path(...), conn: Conexion = Depends(conexion)) -> None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        ok = conn.execute("select public.modulo_activo(%s, %s) as ok", (empresa_id, modulo)).fetchone()["ok"]
        if not ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"El módulo {NOMBRES_MODULO.get(modulo, modulo)} no está activo para esta empresa. "
                                "El administrador lo puede activar en Suscripción.", headers=MODULO_INACTIVO)
    return _dep


def admin_plataforma(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> Conexion:
    if not conn.execute("select public.es_admin_plataforma() as ok").fetchone()["ok"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo la administración de A.U.R.O.M.")
    exigir_mfa(u)
    return conn
