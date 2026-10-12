"""Capas de seguridad HTTP de la API.

  * Cabeceras seguras en toda respuesta (HSTS, CSP, nosniff, sin caché...).
  * Identificador de petición y bitácora sin datos sensibles (nunca tokens ni cuerpos).
  * Límite de tamaño del cuerpo.
  * Límite de peticiones por usuario/IP (general y para acciones sensibles).
  * Idempotencia: con la cabecera `Idempotency-Key`, un POST/PATCH/DELETE repetido
    (doble clic, reintento de red) no se ejecuta dos veces; devuelve la misma respuesta.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import db
from .config import settings

log = logging.getLogger("aurom.http")

CABECERAS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}
RUTAS_DOC = ("/docs", "/redoc", "/openapi.json")

SENSIBLES = re.compile(
    r"/(cerrar|reabrir|aprobar|enviar|emitir|nota-credito|importaciones|usuarios|contadores|liquidaciones|"
    r"aguinaldo|facturas|cobros|pagar|invitar-portal|vincular|parametros|normativa|aumento-masivo|salario)\b")


def ip_cliente(request: Request) -> str:
    """IP real del cliente. Railway agrega la IP que ve al FINAL de X-Forwarded-For; lo que
    viene antes lo escribe el cliente y no es confiable, así que se toma el último salto."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        ultimo = xff.split(",")[-1].strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ultimo):
            return ultimo
    return request.client.host if request.client else "?"


def usuario_verificado(request: Request) -> str | None:
    """`sub` del token SOLO si la firma es válida (si no, None)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    from .auth import verificar_token
    try:
        sub = verificar_token(auth.split(" ", 1)[1].strip()).get("sub")
        return str(uuid.UUID(str(sub)))
    except Exception:  # noqa: BLE001 - token inválido: la ruta responderá 401
        return None


def _sujeto(request: Request) -> str:
    """Clave del límite de tasa: usuario verificado; si no hay, la IP."""
    usuario = usuario_verificado(request)
    return ("u:" + usuario) if usuario else ("ip:" + ip_cliente(request))


class LimitadorTasa:
    """Ventana deslizante en memoria (por proceso). Para varias réplicas, use además el
    límite de Cloudflare/Railway delante de la API."""

    def __init__(self):
        self._eventos: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def permitir(self, clave: str, limite: int, ventana: float = 60.0) -> float:
        """0 si se permite; si no, segundos que faltan para reintentar."""
        ahora = time.monotonic()
        with self._lock:
            q = self._eventos[clave]
            while q and q[0] <= ahora - ventana:
                q.popleft()
            if len(q) >= limite:
                return max(q[0] + ventana - ahora, 1.0)
            q.append(ahora)
            if len(self._eventos) > 50_000:   # evita crecer sin límite
                for k in [k for k, v in self._eventos.items() if not v][:10_000]:
                    del self._eventos[k]
            return 0.0


limitador = LimitadorTasa()


class SeguridadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", rid):
            rid = uuid.uuid4().hex
        inicio = time.perf_counter()
        sujeto = _sujeto(request)

        # Hosts permitidos (evita ataques de cabecera Host)
        if settings.hosts_permitidos:
            host = (request.headers.get("host") or "").split(":")[0]
            if host not in settings.hosts_permitidos:
                return self._final(JSONResponse({"detail": "Host no permitido"}, 400), request, rid, inicio, sujeto)

        # Tamaño del cuerpo
        largo = request.headers.get("content-length")
        if largo and largo.isdigit() and int(largo) > settings.max_cuerpo_bytes:
            return self._final(JSONResponse({"detail": "El archivo o los datos superan el tamaño permitido"}, 413),
                               request, rid, inicio, sujeto)

        # Límite de tasa
        if request.method != "OPTIONS" and not request.url.path.startswith("/salud"):
            espera = limitador.permitir(sujeto, settings.limite_por_minuto)
            if not espera and sujeto.startswith("u:"):
                espera = limitador.permitir("ip:" + ip_cliente(request), settings.limite_por_minuto * 3)
            if not espera and request.method in ("POST", "PUT", "PATCH", "DELETE") and SENSIBLES.search(request.url.path):
                espera = limitador.permitir(sujeto + ":sensible", settings.limite_sensible_por_minuto)
            if espera:
                r = JSONResponse({"detail": "Demasiadas solicitudes. Espere un momento e intente de nuevo."}, 429,
                                 headers={"Retry-After": str(int(espera))})
                return self._final(r, request, rid, inicio, sujeto)

        cuerpo = b""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            cuerpo = await request.body()
            if len(cuerpo) > settings.max_cuerpo_bytes:
                return self._final(JSONResponse({"detail": "El archivo o los datos superan el tamaño permitido"}, 413),
                                   request, rid, inicio, sujeto)

        clave = request.headers.get("idempotency-key")
        if clave and request.method in ("POST", "PUT", "PATCH", "DELETE") and sujeto.startswith("u:"):
            respuesta = await self._idempotente(request, call_next, clave, sujeto[2:], cuerpo)
        else:
            respuesta = await call_next(request)
        return self._final(respuesta, request, rid, inicio, sujeto)

    async def _idempotente(self, request: Request, call_next, clave: str, usuario: str, cuerpo: bytes) -> Response:
        if not re.fullmatch(r"[A-Za-z0-9_\-:.]{8,128}", clave):
            return JSONResponse({"detail": "Idempotency-Key inválida"}, 422)
        # `usuario` ya viene de un token con firma válida (ver _sujeto).
        huella = hashlib.sha256(request.method.encode() + request.url.path.encode() + b"?" +
                                request.url.query.encode() + b"\n" + cuerpo).hexdigest()
        with db.sistema() as c:
            if random.random() < 0.01:
                c.execute("delete from public.idempotencia where creado_en < now() - interval '48 hours'")
            nueva = c.execute("""insert into public.idempotencia (usuario_id, clave, metodo, ruta, huella)
                                 values (%s, %s, %s, %s, %s) on conflict do nothing returning clave""",
                              (usuario, clave, request.method, request.url.path, huella)).fetchone()
            previa = None if nueva else c.execute(
                "select * from public.idempotencia where usuario_id = %s and clave = %s", (usuario, clave)).fetchone()
        if previa:
            if previa["huella"] != huella:
                return JSONResponse({"detail": "Esa Idempotency-Key ya se usó con otra solicitud"}, 422)
            if previa["estado"] == "en_proceso":
                return JSONResponse({"detail": "La misma solicitud todavía se está procesando"}, 409)
            return JSONResponse(previa["respuesta"], previa["codigo_http"], headers={"Idempotent-Replayed": "true"})

        try:
            respuesta = await call_next(request)
            contenido = b"".join([parte async for parte in respuesta.body_iterator])
        except Exception:
            # La operación falló: se libera la clave para que el reintento pueda ejecutarse.
            with db.sistema() as c:
                c.execute("delete from public.idempotencia where usuario_id = %s and clave = %s", (usuario, clave))
            raise
        guardable = (respuesta.status_code < 500 and respuesta.media_type in (None, "application/json")
                     and "json" in respuesta.headers.get("content-type", "json") and len(contenido) < 1_000_000)
        with db.sistema() as c:
            if guardable:
                try:
                    datos = json.loads(contenido) if contenido else None
                except ValueError:
                    datos, guardable = None, False
            if guardable:
                c.execute("""update public.idempotencia set estado = 'listo', codigo_http = %s, respuesta = %s
                             where usuario_id = %s and clave = %s""",
                          (respuesta.status_code, json.dumps(datos), usuario, clave))
            else:
                c.execute("delete from public.idempotencia where usuario_id = %s and clave = %s", (usuario, clave))
        return Response(contenido, respuesta.status_code, headers=dict(respuesta.headers), media_type=respuesta.media_type)

    @staticmethod
    def _final(respuesta: Response, request: Request, rid: str, inicio: float, sujeto: str) -> Response:
        es_doc = request.url.path.startswith(RUTAS_DOC)
        for k, v in CABECERAS.items():
            if es_doc and k == "Content-Security-Policy":
                continue
            respuesta.headers.setdefault(k, v)
        respuesta.headers["X-Request-ID"] = rid
        if "cache-control" not in respuesta.headers:
            respuesta.headers["Cache-Control"] = "no-store"
        ms = int((time.perf_counter() - inicio) * 1000)
        # Bitácora sin datos personales: el usuario va como huella, nunca el token.
        quien = hashlib.sha256(sujeto.encode()).hexdigest()[:12]
        log.info(json.dumps({"rid": rid, "metodo": request.method, "ruta": request.url.path,
                             "estado": respuesta.status_code, "ms": ms, "quien": quien}))
        return respuesta


class LimiteCuerpo:
    """ASGI puro: corta cualquier cuerpo que supere el máximo mientras se recibe, aunque
    no traiga Content-Length (envío por partes). Evita agotar la memoria del servidor."""

    def __init__(self, app, maximo: int | None = None):
        self.app = app
        self.maximo = maximo or settings.max_cuerpo_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        recibido = 0
        excedido = False

        async def recibir():
            nonlocal recibido, excedido
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                recibido += len(mensaje.get("body", b""))
                if recibido > self.maximo:
                    excedido = True
                    raise CuerpoDemasiadoGrande()
            return mensaje

        try:
            await self.app(scope, recibir, send)
        except CuerpoDemasiadoGrande:
            respuesta = JSONResponse({"detail": "El archivo o los datos superan el tamaño permitido"}, 413)
            await respuesta(scope, receive, send)
        except Exception:
            if excedido:
                respuesta = JSONResponse({"detail": "El archivo o los datos superan el tamaño permitido"}, 413)
                await respuesta(scope, receive, send)
            else:
                raise


class CuerpoDemasiadoGrande(Exception):
    pass
