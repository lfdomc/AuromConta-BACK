"""Fase 8 · Autogestión: soporte (bot de ayuda + tickets), Telegram, artículos, alertas,
normativa, monitoreo de salud y tareas programadas.

Los cobros con Tilopay (/empresas/{e}/suscripcion*, /webhooks/tilopay, /cron/cobros,
/cron/facturas) viven en otro módulo.

Seguridad:
  * Todo lo que hace un usuario corre con su conexión (RLS).
  * `db.sistema()` solo en /webhooks/telegram y /cron/*. El webhook usa la conexión de sistema
    únicamente para la bitácora de eventos y la vinculación del chat; los comandos del bot se
    ejecutan con `db.como_usuario()` del usuario dueño del chat, así que el RLS aplica igual.
  * /cron/* exige el encabezado X-Cron-Secret; el webhook exige X-Telegram-Bot-Api-Secret-Token.
"""
from __future__ import annotations

import calendar
import hmac
import html
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import httpx
import psycopg
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from .. import crud, db
from ..auth import (
    Contexto,
    Usuario,
    admin_plataforma,
    conexion,
    contexto_empresa,
    usuario_actual,
)
from ..config import settings
from ..db import Conexion
from ..fechas import hoy_cr
from ..servicios import alertas, correo, telegram
from ..servicios.telegram import esc
from .saas import _vigente_json, insertar_parametros, validar_parametros

log = logging.getLogger("aurom.autogestion")

r = APIRouter(tags=["autogestion"])
tickets_empresa = APIRouter(prefix="/empresas/{empresa_id}/tickets", tags=["soporte"])
plataforma = APIRouter(prefix="/plataforma", tags=["plataforma"])
cron = APIRouter(prefix="/cron", tags=["cron"])
routers = [r, tickets_empresa, plataforma, cron]

CR = timezone(timedelta(hours=-6))   # Costa Rica no usa horario de verano
SOPORTE = {"contador_operacion", "contador_lectura", "plataforma"}
UMBRAL_RESPUESTA = 0.3               # rango mínimo (ts_rank_cd normalizado) para sugerir una respuesta
ESTADOS_ABIERTOS = ("abierto", "en_proceso", "escalado")



class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _corto(uid) -> str:
    return str(uid)[:8]


# ================================================================== Ayuda ====
def buscar(conn: Conexion, q: str, limite: int = 5) -> dict:
    """Busca en articulos_ayuda (full-text en español con ranking) y arma una respuesta sugerida."""
    q = (q or "").strip()[:500]
    filas = conn.execute("select * from public.buscar_ayuda(%s, %s)", (q, limite)).fetchall() if q else []
    articulos = [{"id": f["id"], "titulo": f["titulo"], "categoria": f["categoria"], "rango": round(float(f["rango"]), 4),
                  "fragmento": f["fragmento"]} for f in filas]
    sugerida = None
    if filas and float(filas[0]["rango"]) >= UMBRAL_RESPUESTA:
        sugerida = {"articulo_id": filas[0]["id"], "titulo": filas[0]["titulo"], "texto": filas[0]["contenido"],
                    "resumen": filas[0]["fragmento"]}
    return {"consulta": q, "articulos": articulos, "respuesta_sugerida": sugerida,
            "confianza": articulos[0]["rango"] if articulos else 0.0}


@r.get("/ayuda/buscar", summary="Buscar en la ayuda: artículos y respuesta sugerida")
def ayuda_buscar(q: str = Query(..., min_length=2, max_length=500), limite: int = Query(5, ge=1, le=20),
                 conn: Conexion = Depends(conexion)) -> dict:
    return buscar(conn, q, limite)


# ================================================================ Tickets ====
class TicketIn(_Base):
    asunto: str = Field(min_length=3, max_length=200)
    mensaje: str | None = Field(None, max_length=10000)
    prioridad: Literal["baja", "normal", "alta", "urgente"] = "normal"


class TicketUpd(_Base):
    asunto: str | None = Field(None, min_length=3, max_length=200)
    estado: Literal["abierto", "en_proceso", "resuelto", "cerrado"] | None = None
    prioridad: Literal["baja", "normal", "alta", "urgente"] | None = None
    minutos_invertidos: int | None = Field(None, ge=0, le=100000)


class MensajeIn(_Base):
    texto: str = Field(min_length=1, max_length=10000)


class EscalarIn(_Base):
    motivo: str | None = Field(None, max_length=2000)


def _autor(rol: str) -> str:
    if rol.startswith("contador"):
        return "contador"
    return "plataforma" if rol == "plataforma" else "cliente"


def _nombre_empresa(conn, empresa_id) -> str:
    fila = conn.execute("select nombre from empresas where id = %s", (empresa_id,)).fetchone()
    return fila["nombre"] if fila else ""


def _avisar_soporte(conn: Conexion, t: dict, titulo: str, texto: str = "") -> None:
    """Avisa al contador asignado (Telegram) y, si el ticket es de la plataforma o urgente, a la dueña."""
    empresa = _nombre_empresa(conn, t["empresa_id"])
    cuerpo = (f"🎫 <b>{esc(titulo)}</b> · #{_corto(t['id'])}\n<b>{esc(empresa)}</b>: {esc(t['asunto'])}"
              + (f"\n\n{esc(texto[:800])}" if texto else "") + f"\n\n{settings.app_url}/e/{t['empresa_id']}/soporte/{t['id']}")
    if t["nivel"] == "contador" and t["asignado_socio_id"]:
        telegram.notificar_socio(conn, t["asignado_socio_id"], cuerpo)
    if t["nivel"] == "plataforma" or t["prioridad"] == "urgente":
        sev = "critica" if t["prioridad"] == "urgente" else "aviso"
        alertas.alertar_ticket(conn, t["id"], f"{titulo} #{_corto(t['id'])} · {empresa}: {t['asunto']}"
                               + (f" — {texto[:300]}" if texto else ""), sev)


def abrir_ticket(conn: Conexion, empresa_id, asunto: str, mensaje: str | None, prioridad: str = "normal",
                 canal: str = "web", autor: str = "cliente") -> tuple[dict, dict]:
    """Crea el ticket (el trigger lo asigna al contador de soporte principal o a la plataforma),
    agrega el mensaje inicial y una respuesta del bot si encuentra artículos relacionados."""
    t = conn.execute("insert into tickets (empresa_id, asunto, prioridad, canal) values (%s,%s,%s,%s) returning *",
                     (empresa_id, asunto, prioridad, canal)).fetchone()
    if mensaje:
        conn.execute("insert into mensajes_ticket (empresa_id, ticket_id, autor, texto) values (%s,%s,%s,%s)",
                     (empresa_id, t["id"], autor, mensaje))
    ayuda = buscar(conn, f"{asunto} {mensaje or ''}", 3)
    relevantes = [a for a in ayuda["articulos"] if a["rango"] >= UMBRAL_RESPUESTA / 2]
    if relevantes:
        texto = "Mientras le atienden, estos artículos podrían ayudarle:\n" + "\n".join(
            f"• {a['titulo']} ({settings.app_url}/ayuda/{a['id']})" for a in relevantes)
        conn.execute("insert into mensajes_ticket (empresa_id, ticket_id, autor, usuario_id, texto) values (%s,%s,'bot',null,%s)",
                     (empresa_id, t["id"], texto))
    _avisar_soporte(conn, t, "Ticket nuevo", mensaje or "")
    return t, ayuda


_TICKETS_SQL = """
    select t.*, e.nombre as empresa_nombre, s.nombre as asignado_nombre,
           (select count(*)::int from mensajes_ticket m where m.ticket_id = t.id) as mensajes,
           (select max(m.creado_en) from mensajes_ticket m where m.ticket_id = t.id) as ultimo_mensaje_en
      from tickets t join empresas e on e.id = t.empresa_id
      left join socios s on s.id = t.asignado_socio_id"""


@tickets_empresa.get("", summary="Tickets de soporte de la empresa")
def listar_tickets(ctx: Contexto = Depends(contexto_empresa), estado: str | None = None) -> list[dict]:
    q, p = sql.SQL(_TICKETS_SQL) + sql.SQL(" where t.empresa_id = %s"), [ctx.empresa_id]
    if estado == "abiertos":
        q += sql.SQL(" and t.estado = any(%s)"); p.append(list(ESTADOS_ABIERTOS))
    elif estado:
        q += sql.SQL(" and t.estado = %s"); p.append(estado)
    return ctx.conn.execute(q + sql.SQL(" order by t.actualizado_en desc"), p).fetchall()


@tickets_empresa.post("", status_code=201, summary="Abrir un ticket (va primero al contador asignado)")
def crear_ticket(datos: TicketIn, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    t, ayuda = abrir_ticket(ctx.conn, ctx.empresa_id, datos.asunto, datos.mensaje, datos.prioridad, "web", _autor(ctx.rol))
    return {**ver_ticket(t["id"], ctx), "ayuda": ayuda}


def _ticket(ctx: Contexto, id: UUID) -> dict:
    t = ctx.conn.execute(sql.SQL(_TICKETS_SQL) + sql.SQL(" where t.id = %s and t.empresa_id = %s"), (id, ctx.empresa_id)).fetchone()
    if not t:
        raise HTTPException(404, "Ticket no encontrado")
    return t


@tickets_empresa.get("/{id}", summary="Ticket con su conversación")
def ver_ticket(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    t = _ticket(ctx, id)
    t["conversacion"] = ctx.conn.execute(
        "select id, autor, usuario_id, texto, adjuntos, creado_en from mensajes_ticket where ticket_id = %s "
        "order by creado_en, id", (id,)).fetchall()
    t["mi_rol"] = ctx.rol
    return t


@tickets_empresa.patch("/{id}", summary="Actualizar estado, prioridad o tiempo invertido")
def editar_ticket(id: UUID, datos: TicketUpd, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    t = _ticket(ctx, id)
    cambios = datos.model_dump(exclude_unset=True, exclude_none=True)
    soporte = ctx.rol in SOPORTE
    if not soporte:
        if "minutos_invertidos" in cambios:
            raise HTTPException(403, "Solo el soporte registra el tiempo invertido")
        if cambios.get("estado") not in (None, "abierto", "cerrado"):
            raise HTTPException(403, "Puede cerrar o reabrir el ticket; los demás estados los maneja el soporte")
    if cambios.get("estado") == "resuelto":
        cambios["resuelto_por"] = _autor(ctx.rol) if soporte else None
        if not cambios["resuelto_por"]:
            del cambios["resuelto_por"]
    if cambios.get("estado") == "abierto" and t["estado"] in ("resuelto", "cerrado"):
        cambios["resuelto_por"] = None
    if cambios:
        crud.actualizar(ctx.conn, "tickets", id, cambios, ctx.empresa_id)
    if cambios.get("prioridad") == "urgente" and t["prioridad"] != "urgente":
        _avisar_soporte(ctx.conn, {**t, **cambios}, "Ticket marcado urgente")
    if soporte and cambios.get("estado") == "resuelto":
        telegram.notificar_usuario_empresa(ctx.conn, ctx.empresa_id, t["usuario_id"],
                                           f"✅ Su ticket #{_corto(id)} «{esc(t['asunto'])}» quedó resuelto.")
    return ver_ticket(id, ctx)


@tickets_empresa.post("/{id}/mensajes", status_code=201, summary="Responder en el ticket")
def responder(id: UUID, datos: MensajeIn, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    t = _ticket(ctx, id)
    autor = _autor(ctx.rol)
    m = ctx.conn.execute("insert into mensajes_ticket (empresa_id, ticket_id, autor, texto) values (%s,%s,%s,%s) returning *",
                         (ctx.empresa_id, id, autor, datos.texto)).fetchone()
    if autor == "cliente":
        if t["estado"] in ("resuelto", "cerrado"):
            ctx.conn.execute("update tickets set estado = 'abierto', resuelto_por = null where id = %s", (id,))
        else:
            ctx.conn.execute("update tickets set actualizado_en = now() where id = %s", (id,))
        _avisar_soporte(ctx.conn, t, "Nuevo mensaje del cliente", datos.texto)
    else:
        if t["estado"] == "abierto":
            ctx.conn.execute("update tickets set estado = 'en_proceso' where id = %s", (id,))
        else:
            ctx.conn.execute("update tickets set actualizado_en = now() where id = %s", (id,))
        telegram.notificar_usuario_empresa(ctx.conn, ctx.empresa_id, t["usuario_id"],
                                           f"💬 Respuesta en su ticket #{_corto(id)} «{esc(t['asunto'])}»:\n\n"
                                           f"{esc(datos.texto[:1500])}")
    return m


@tickets_empresa.post("/{id}/escalar", summary="Escalar el ticket a la plataforma (segundo nivel)")
def escalar(id: UUID, datos: EscalarIn | None = None, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    if ctx.rol not in {"contador_operacion", "contador_lectura", "admin"}:
        raise HTTPException(403, "Solo el contador asignado o el administrador de la empresa pueden escalar")
    t = _ticket(ctx, id)
    if t["nivel"] == "plataforma":
        raise HTTPException(409, "El ticket ya está con la plataforma")
    if t["estado"] in ("resuelto", "cerrado"):
        raise HTTPException(409, "El ticket está resuelto o cerrado; reábralo primero")
    motivo = (datos.motivo if datos else None) or "Sin motivo indicado"
    ctx.conn.execute("update tickets set nivel = 'plataforma', estado = 'escalado' where id = %s", (id,))
    ctx.conn.execute("insert into mensajes_ticket (empresa_id, ticket_id, autor, texto) values (%s,%s,%s,%s)",
                     (ctx.empresa_id, id, _autor(ctx.rol), f"Escalado a A.U.R.O.M.: {motivo}"))
    _avisar_soporte(ctx.conn, {**t, "nivel": "plataforma", "estado": "escalado"}, "Ticket escalado", motivo)
    return ver_ticket(id, ctx)


def tickets_soporte(conn: Conexion, solo_socio: bool = False, estado: str | None = None,
                    nivel: str | None = None) -> list[dict]:
    """Tickets que atiende el usuario: el socio, los de sus empresas asignadas; la plataforma, todos."""
    es_plat = conn.execute("select public.es_admin_plataforma() as ok").fetchone()["ok"]
    socio = conn.execute("select public.mi_socio_id() as id").fetchone()["id"]
    if solo_socio or not es_plat:
        if not socio:
            raise HTTPException(403, "Solo para contadores, vendedores o la plataforma")
        q = sql.SQL(_TICKETS_SQL) + sql.SQL(""" where exists (select 1 from asignaciones_contador a where a.empresa_id = t.empresa_id
                                              and a.socio_id = %s and a.revocado_en is null)""")
        p: list[Any] = [socio]
    else:
        q, p = sql.SQL(_TICKETS_SQL) + sql.SQL(" where true"), []
    if estado == "abiertos" or estado is None:
        q += sql.SQL(" and t.estado = any(%s)"); p.append(list(ESTADOS_ABIERTOS))
    elif estado != "todos":
        q += sql.SQL(" and t.estado = %s"); p.append(estado)
    if nivel:
        q += sql.SQL(" and t.nivel = %s"); p.append(nivel)
    orden = sql.SQL(""" order by (t.prioridad = 'urgente') desc, (t.estado = 'escalado') desc, (t.nivel = 'plataforma') desc,
                t.actualizado_en desc limit 500""")
    return conn.execute(q + orden, p).fetchall()


@r.get("/soporte/tickets", tags=["soporte"],
       summary="Bandeja de soporte (contador: sus clientes; dueña: escalados primero y todos)")
def soporte_tickets(conn: Conexion = Depends(conexion),
                    estado: str | None = Query(None, description="abiertos (defecto) | todos | abierto | en_proceso | escalado | resuelto | cerrado"),
                    nivel: Literal["contador", "plataforma", "bot"] | None = None) -> list[dict]:
    return tickets_soporte(conn, estado=estado, nivel=nivel)


# =============================================================== Telegram ====
class VincularIn(_Base):
    avisos: dict | None = None


def _rol_telegram(conn) -> str:
    if conn.execute("select public.es_admin_plataforma() as ok").fetchone()["ok"]:
        return "duena"
    if conn.execute("select public.mi_socio_id() as id").fetchone()["id"]:
        return "socio"
    return "cliente"


@r.post("/telegram/vincular", tags=["telegram"], summary="Código y enlace para vincular Telegram (vence en 24 h)")
def telegram_vincular(datos: VincularIn | None = None, u: Usuario = Depends(usuario_actual),
                      conn: Conexion = Depends(conexion)) -> dict:
    rol = _rol_telegram(conn)
    conn.execute("delete from telegram_vinculos where usuario_id = %s and not activo", (u.id,))
    avisos = {"boletas": True, "recordatorios": True, **((datos.avisos if datos else None) or {})}
    v = conn.execute("insert into telegram_vinculos (usuario_id, rol, avisos) values (%s, %s, %s) returning *",
                     (u.id, rol, json.dumps({k: bool(x) for k, x in avisos.items()}))).fetchone()
    return {"codigo": v["codigo_vinculacion"], "enlace": telegram.enlace_vinculacion(v["codigo_vinculacion"]),
            "rol": rol, "vence_en": v["creado_en"] + timedelta(hours=24)}


@r.post("/webhooks/telegram", tags=["telegram"], summary="Webhook del bot de Telegram")
def webhook_telegram(update: dict = Body(...),
                     x_telegram_bot_api_secret_token: str = Header(default="")) -> dict:
    secreto = settings.telegram_webhook_secret
    if not secreto or not hmac.compare_digest(x_telegram_bot_api_secret_token.encode(), secreto.encode()):
        raise HTTPException(401, "Firma del webhook inválida")
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(422, "Falta update_id")
    tipo = next((k for k in update if k != "update_id"), None)
    with db.sistema() as conn:
        ev = conn.execute("""insert into negocio.eventos_webhook (origen, tipo, contenido, firma_valida)
                             values ('telegram', %s, %s, true)
                             on conflict ((contenido ->> 'update_id')) where origen = 'telegram' do nothing
                             returning id""", (tipo, json.dumps(update))).fetchone()
        if not ev:
            return {"ok": True, "duplicado": True}
        error = None
        try:
            with conn.transaction():
                Bot(conn).procesar(update)
        except Exception as exc:  # noqa: BLE001 - Telegram no debe reintentar en bucle
            log.exception("Error procesando update %s de Telegram", update_id)
            error = str(exc)[:500]
        conn.execute("update negocio.eventos_webhook set procesado_en = now(), error = %s where id = %s",
                     (error, ev["id"]))
    return {"ok": True}


MENU = {
    "cliente": "/ayuda <pregunta> · buscar en la ayuda\n/ticket <asunto> · abrir un ticket de soporte\n"
               "/estado · última planilla de sus empresas\n/desvincular · dejar de recibir avisos",
    "socio": "/clientes · sus empresas asignadas\n/tickets · tickets abiertos de sus clientes\n"
             "/ayuda <pregunta> · buscar en la ayuda\n/estado · última planilla de sus clientes\n/desvincular",
    "duena": "/resumen · indicadores de la plataforma\n/alertas · alertas sin atender\n/clientes · clientes por estado\n"
             "/tickets · tickets escalados y urgentes\n/estado · planillas recientes\n/desvincular",
}


class Bot:
    """Comandos del bot. `sis` es la conexión de sistema (solo vínculos); las consultas de datos
    se hacen con la conexión RLS del usuario vinculado."""

    def __init__(self, sis: Conexion):
        self.sis = sis
        self.chat_id: str = ""

    def responder(self, texto: str) -> None:
        telegram.enviar_mensaje(self.chat_id, texto)

    def procesar(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or not isinstance(msg.get("text"), str):
            return
        self.chat_id = str(msg["chat"]["id"])
        if msg["chat"].get("type", "private") != "private":
            self.responder("Por seguridad use el bot de Planilla Costa Rica en un chat privado.")
            return
        texto = msg["text"].strip()
        if texto.startswith("/"):
            cmd, _, arg = texto.partition(" ")
            cmd = cmd.split("@")[0].lower()
        else:
            cmd, arg = "/ayuda", texto
        arg = arg.strip()
        if cmd == "/start" and arg:
            self.vincular(arg)
            return
        v = self.sis.execute("""select * from public.telegram_vinculos where chat_id = %s and activo
                                order by creado_en desc limit 1""", (self.chat_id,)).fetchone()
        if not v:
            self.responder("👋 Hola. Para usar el bot vincule su cuenta: en Planilla Costa Rica vaya a "
                           "<b>Perfil › Vincular Telegram</b> y abra el enlace que se genera.")
            return
        if cmd == "/desvincular":
            self.sis.execute("update public.telegram_vinculos set activo = false where id = %s", (v["id"],))
            self.sis.execute("update public.socios set telegram_chat_id = null where usuario_id = %s and telegram_chat_id = %s",
                             (v["usuario_id"], self.chat_id))
            self.responder("Listo, este chat ya no recibirá avisos de Planilla Costa Rica.")
            return
        with db.como_usuario({"sub": str(v["usuario_id"]), "role": "authenticated", "aud": "authenticated"}) as uc:
            rol = _rol_telegram(uc)
            manejador = {"/start": self.menu, "/menu": self.menu, "/help": self.menu, "/ayuda": self.ayuda,
                         "/ticket": self.ticket, "/estado": self.estado, "/resumen": self.resumen,
                         "/alertas": self.alertas, "/clientes": self.clientes, "/tickets": self.tickets}.get(cmd)
            if not manejador:
                self.menu(uc, rol, "")
                return
            manejador(uc, rol, arg)

    # ------------------------------------------------------------ vínculo --
    def vincular(self, codigo: str) -> None:
        v = self.sis.execute("""select * from public.telegram_vinculos
                                 where codigo_vinculacion = %s and not activo and creado_en > now() - interval '24 hours'""",
                             (codigo.strip().upper()[:32],)).fetchone()
        if not v:
            self.responder("El código no es válido o ya venció. Genere uno nuevo en Planilla Costa Rica "
                           "(<b>Perfil › Vincular Telegram</b>).")
            return
        # Un chat corresponde a una sola cuenta: se desactivan vínculos anteriores de este chat.
        self.sis.execute("update public.telegram_vinculos set activo = false where chat_id = %s and id <> %s",
                         (self.chat_id, v["id"]))
        self.sis.execute("update public.telegram_vinculos set chat_id = %s, activo = true where id = %s",
                         (self.chat_id, v["id"]))
        self.sis.execute("update public.socios set telegram_chat_id = %s where usuario_id = %s",
                         (self.chat_id, v["usuario_id"]))
        extra = ""
        if v["rol"] == "duena":
            extra = (f"\n\nPara recibir las alertas de la plataforma configure TELEGRAM_CHAT_DUENA={self.chat_id} "
                     "en Railway (si aún no lo hizo).")
        self.responder(f"✅ Cuenta vinculada. Desde ahora recibirá avisos aquí.\n\n{MENU[v['rol']]}{extra}")

    def menu(self, uc, rol: str, arg: str) -> None:
        self.responder(f"<b>Planilla Costa Rica</b>\n\n{MENU[rol]}\n\nTambién puede escribir su pregunta directamente.")

    # ------------------------------------------------------------- ayuda --
    def _empresa_para_ticket(self, uc, arg: str) -> tuple[dict | None, str]:
        empresas = uc.execute("select * from public.mis_empresas()").fetchall()
        propias = [e for e in empresas if e["via"] == "usuario"] or empresas
        partes = arg.split(" ", 1)
        if len(propias) > 1 and partes[0].isdigit() and 1 <= int(partes[0]) <= len(propias):
            return propias[int(partes[0]) - 1], (partes[1] if len(partes) > 1 else "")
        if len(propias) == 1:
            return propias[0], arg
        if not propias:
            self.responder("Su cuenta no tiene empresas asociadas, así que no puedo abrir un ticket.")
        else:
            lista = "\n".join(f"{i}. {esc(e['nombre'])}" for i, e in enumerate(propias, 1))
            self.responder(f"¿De cuál empresa es la consulta? Escriba por ejemplo <code>/ticket 1 {esc(arg[:40]) or 'asunto'}</code>\n\n{lista}")
        return None, arg

    def ayuda(self, uc, rol: str, arg: str) -> None:
        if len(arg) < 3:
            self.menu(uc, rol, arg)
            return
        res = buscar(uc, arg, 3)
        sug = res["respuesta_sugerida"]
        if sug:
            otros = "\n".join(f"• {esc(a['titulo'])}" for a in res["articulos"][1:])
            self.responder(f"📘 <b>{esc(sug['titulo'])}</b>\n\n{esc(sug['texto'][:1500])}"
                           + (f"\n\nRelacionados:\n{otros}" if otros else "")
                           + "\n\n¿No resolvió su duda? Escriba <code>/ticket</code> y su consulta.")
            return
        # El bot no encontró respuesta: abre un ticket para el contador (o la plataforma).
        if rol == "duena":
            self.responder("No encontré artículos sobre eso.")
            return
        empresa, consulta = self._empresa_para_ticket(uc, arg)
        if not empresa:
            return
        t, _ = abrir_ticket(uc, empresa["id"], consulta[:200], consulta, "normal", "telegram")
        self._confirmar_ticket(uc, t, empresa, "No encontré la respuesta en la ayuda, así que abrí un ticket")

    def ticket(self, uc, rol: str, arg: str) -> None:
        empresa, asunto = self._empresa_para_ticket(uc, arg)
        if not empresa:
            return
        if len(asunto) < 3:
            self.responder("Escriba el asunto después del comando, por ejemplo: <code>/ticket No me calcula la renta</code>")
            return
        t, _ = abrir_ticket(uc, empresa["id"], asunto[:200], asunto if len(asunto) > 200 else None, "normal", "telegram")
        self._confirmar_ticket(uc, t, empresa, "Abrí su ticket")

    def _confirmar_ticket(self, uc, t: dict, empresa: dict, inicio: str) -> None:
        quien = "A.U.R.O.M."
        if t["nivel"] == "contador" and t["asignado_socio_id"]:
            s = uc.execute("select nombre from socios where id = %s", (t["asignado_socio_id"],)).fetchone()
            quien = f"su contador {s['nombre']}" if s else "su contador"
        self.responder(f"🎫 {inicio} <b>#{_corto(t['id'])}</b> para {esc(empresa['nombre'])}. "
                       f"Lo atenderá {esc(quien)}; le avisaremos aquí cuando respondan.")

    # ------------------------------------------------------------ estado --
    def estado(self, uc, rol: str, arg: str) -> None:
        filas = uc.execute("""select e.nombre, p.tipo, p.desde, p.hasta, p.estado, p.fecha_pago
                                from public.mis_empresas() e
                                left join lateral (select * from planillas p where p.empresa_id = e.id
                                                    order by p.hasta desc, p.creado_en desc limit 1) p on true
                               order by e.nombre limit 30""").fetchall()
        if not filas:
            self.responder("No tiene empresas asociadas.")
            return
        lineas = []
        for f in filas:
            if f["estado"]:
                lineas.append(f"• <b>{esc(f['nombre'])}</b>: {f['tipo']} {f['desde']:%d/%m}–{f['hasta']:%d/%m/%Y} · "
                              f"<i>{f['estado']}</i>" + (f" · pago {f['fecha_pago']:%d/%m}" if f["fecha_pago"] else ""))
            else:
                lineas.append(f"• <b>{esc(f['nombre'])}</b>: sin planillas todavía")
        self.responder("📊 <b>Última planilla</b>\n" + "\n".join(lineas))

    # ------------------------------------------------------------ dueña ---
    def resumen(self, uc, rol: str, arg: str) -> None:
        if rol != "duena":
            self.menu(uc, rol, arg)
            return
        from .saas import resumen as _resumen
        d = _resumen(uc)
        estados = ", ".join(f"{k}: {v}" for k, v in sorted(d["clientes"]["por_estado"].items())) or "sin clientes"
        self.responder(f"📈 <b>Resumen de la plataforma</b>\nClientes: {d['clientes']['total']} ({estados})\n"
                       f"Empleados activos: {d['empleados_activos']}\nMRR: ₡{d['mrr']:,.2f}\n"
                       f"Tickets abiertos: {d['tickets']['abiertos']} (plataforma {d['tickets']['plataforma']}, "
                       f"urgentes {d['tickets']['urgentes']})\nAlertas sin atender: {d['alertas_pendientes']}")

    def alertas(self, uc, rol: str, arg: str) -> None:
        if rol != "duena":
            self.menu(uc, rol, arg)
            return
        filas = uc.execute("select * from negocio.alertas where not atendida order by creado_en desc limit 10").fetchall()
        if not filas:
            self.responder("✅ No hay alertas pendientes.")
            return
        self.responder("🔔 <b>Alertas sin atender</b>\n" + "\n".join(
            f"{alertas.ICONO.get(a['severidad'], '')} {a['creado_en']:%d/%m %H:%M} <b>{a['tipo']}</b>: {esc(a['mensaje'][:200])}"
            for a in filas))

    def clientes(self, uc, rol: str, arg: str) -> None:
        if rol == "duena":
            est = uc.execute("select estado_servicio, count(*)::int n from empresas group by 1 order by 2 desc").fetchall()
            nuevos = uc.execute("select nombre, creado_en from empresas order by creado_en desc limit 5").fetchall()
            self.responder("🏢 <b>Clientes</b>\n" + "\n".join(f"• {e['estado_servicio']}: {e['n']}" for e in est)
                           + "\n\nÚltimos registros:\n" + "\n".join(f"• {esc(n['nombre'])} ({n['creado_en']:%d/%m})" for n in nuevos))
            return
        if rol == "socio":
            from .socios import clientes_de_socio
            filas = clientes_de_socio(uc)
            if not filas:
                self.responder("Aún no tiene empresas asignadas.")
                return
            self.responder("🏢 <b>Sus clientes</b>\n" + "\n".join(
                f"• {esc(c['nombre'])} · {c['estado_servicio']} · {c['empleados']} empleados"
                + (f" · {c['tickets_abiertos']} tickets" if c["tickets_abiertos"] else "") for c in filas[:40]))
            return
        self.menu(uc, rol, arg)

    def tickets(self, uc, rol: str, arg: str) -> None:
        if rol == "cliente":
            filas = uc.execute(sql.SQL(_TICKETS_SQL) + sql.SQL(" where t.estado = any(%s) order by t.actualizado_en desc limit 15"),
                               (list(ESTADOS_ABIERTOS),)).fetchall()
        else:
            filas = tickets_soporte(uc, solo_socio=(rol == "socio"),
                                    nivel=None)[:15]
        if not filas:
            self.responder("✅ No hay tickets abiertos.")
            return
        self.responder("🎫 <b>Tickets abiertos</b>\n" + "\n".join(
            f"• #{_corto(t['id'])} {esc(t['empresa_nombre'])}: {esc(t['asunto'][:80])} · {t['estado']}"
            + (" · URGENTE" if t["prioridad"] == "urgente" else "") for t in filas))


# ============================================================== Artículos ====
class ArticuloIn(_Base):
    titulo: str = Field(min_length=3, max_length=200)
    contenido: str = Field(min_length=10)
    categoria: str | None = None
    palabras_clave: list[str] = Field(default_factory=list)
    publicado: bool = True


class ArticuloUpd(_Base):
    titulo: str | None = Field(None, min_length=3, max_length=200)
    contenido: str | None = Field(None, min_length=10)
    categoria: str | None = None
    palabras_clave: list[str] | None = None
    publicado: bool | None = None


_ART = sql.SQL("id, titulo, contenido, categoria, palabras_clave, publicado, version, actualizado_en, actualizado_por")
# Columnas que se pueden editar de un artículo (lista cerrada para sql.Identifier).
_ART_EDITABLES = frozenset({"titulo", "contenido", "categoria", "palabras_clave", "publicado"})


@plataforma.get("/articulos", summary="Artículos de ayuda (incluye no publicados)")
def articulos(conn: Conexion = Depends(admin_plataforma), categoria: str | None = None,
              buscar_texto: str | None = Query(None, alias="buscar")) -> list[dict]:
    conds, p = [sql.SQL("true")], []
    if categoria:
        conds.append(sql.SQL("categoria = %s")); p.append(categoria)
    if buscar_texto:
        conds.append(sql.SQL("(titulo ilike %s or contenido ilike %s)")); p += [f"%{buscar_texto}%"] * 2
    q = sql.SQL("select {} from articulos_ayuda where {} order by categoria nulls last, titulo").format(
        _ART, sql.SQL(" and ").join(conds))
    return conn.execute(q, p).fetchall()


@plataforma.post("/articulos", status_code=201)
def crear_articulo(datos: ArticuloIn, conn: Conexion = Depends(admin_plataforma)) -> dict:
    return conn.execute(sql.SQL("""insert into articulos_ayuda (titulo, contenido, categoria, palabras_clave, publicado)
                            values (%s,%s,%s,%s,%s) returning {}""").format(_ART),
                        (datos.titulo, datos.contenido, datos.categoria, datos.palabras_clave, datos.publicado)).fetchone()


@plataforma.get("/articulos/{id}")
def ver_articulo(id: UUID, conn: Conexion = Depends(admin_plataforma)) -> dict:
    fila = conn.execute(sql.SQL("select {} from articulos_ayuda where id = %s").format(_ART), (id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Artículo no encontrado")
    return fila


@plataforma.patch("/articulos/{id}")
def editar_articulo(id: UUID, datos: ArticuloUpd, conn: Conexion = Depends(admin_plataforma)) -> dict:
    cambios = {k: v for k, v in datos.model_dump(exclude_unset=True).items() if k in _ART_EDITABLES}
    if not cambios:
        return ver_articulo(id, conn)
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in cambios)
    fila = conn.execute(sql.SQL("update articulos_ayuda set {}, version = version + 1 where id = %s returning {}")
                        .format(sets, _ART), [*cambios.values(), id]).fetchone()
    if not fila:
        raise HTTPException(404, "Artículo no encontrado")
    return fila


@plataforma.delete("/articulos/{id}", status_code=204)
def borrar_articulo(id: UUID, conn: Conexion = Depends(admin_plataforma)) -> None:
    if not conn.execute("delete from articulos_ayuda where id = %s", (id,)).rowcount:
        raise HTTPException(404, "Artículo no encontrado")


# ================================================================ Alertas ====
@plataforma.get("/alertas", summary="Centro de alertas de la plataforma")
def listar_alertas(conn: Conexion = Depends(admin_plataforma), atendida: bool | None = False,
                   tipo: str | None = None, limite: int = Query(100, ge=1, le=1000)) -> list[dict]:
    q, p = sql.SQL("select * from negocio.alertas where true"), []
    if atendida is not None:
        q += sql.SQL(" and atendida = %s"); p.append(atendida)
    if tipo:
        q += sql.SQL(" and tipo = %s"); p.append(tipo)
    return conn.execute(q + sql.SQL(" order by creado_en desc limit %s"), p + [limite]).fetchall()


@plataforma.post("/alertas/{id}/atender")
def atender_alerta(id: UUID, conn: Conexion = Depends(admin_plataforma)) -> dict:
    fila = conn.execute("update negocio.alertas set atendida = true where id = %s returning *", (id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Alerta no encontrada")
    return fila


# ============================================================== Normativa ====
class NormativaIn(_Base):
    fuente: str = Field(min_length=2)
    enlace: str | None = None
    publicado: date | None = None
    resumen: str = Field(min_length=5)
    parametros_propuestos: dict | None = None
    vigente_desde: date | None = None


class AprobarNormativaIn(_Base):
    parametros_propuestos: dict | None = None
    vigente_desde: date | None = None


class RechazarIn(_Base):
    motivo: str | None = None


def _resultado(fila: dict) -> dict:
    try:
        fila["resultado_pruebas_json"] = json.loads(fila["resultado_pruebas"]) if fila.get("resultado_pruebas") else None
    except ValueError:
        fila["resultado_pruebas_json"] = None
    return fila


@plataforma.get("/normativa", summary="Cambios normativos detectados o registrados")
def normativa(conn: Conexion = Depends(admin_plataforma), estado: str | None = None) -> list[dict]:
    q, p = sql.SQL("select * from negocio.cambios_normativos where true"), []
    if estado:
        q += sql.SQL(" and estado = %s"); p.append(estado)
    return [_resultado(f) for f in conn.execute(q + sql.SQL(" order by creado_en desc"), p).fetchall()]


@plataforma.post("/normativa", status_code=201, summary="Registrar un cambio normativo (propuesta de parámetros)")
def registrar_normativa(datos: NormativaIn, conn: Conexion = Depends(admin_plataforma)) -> dict:
    fila = crud.insertar(conn, "negocio.cambios_normativos", {
        **datos.model_dump(exclude={"parametros_propuestos"}),
        "parametros_propuestos": json.dumps(datos.parametros_propuestos) if datos.parametros_propuestos is not None else None})
    return _resultado(fila)


@plataforma.post("/normativa/{id}/aprobar",
                 summary="Aprobar: valida con el motor y crea el juego de parámetros legales")
def aprobar_normativa(id: UUID, datos: AprobarNormativaIn | None = None, u: Usuario = Depends(usuario_actual),
                      conn: Conexion = Depends(admin_plataforma)):
    n = conn.execute("select * from negocio.cambios_normativos where id = %s for update", (id,)).fetchone()
    if not n:
        raise HTTPException(404, "Cambio normativo no encontrado")
    if n["estado"] in ("aplicado", "rechazado"):
        raise HTTPException(409, f"El cambio ya está {n['estado']}")
    propuestos = (datos.parametros_propuestos if datos and datos.parametros_propuestos is not None
                  else n["parametros_propuestos"])
    vigente = (datos.vigente_desde if datos and datos.vigente_desde else None) or n["vigente_desde"]
    if not propuestos or not vigente:
        raise HTTPException(422, "Faltan los parámetros propuestos o la fecha de vigencia")
    v = validar_parametros(vigente, propuestos, _vigente_json(conn, vigente))
    resumen_v = {"ok": v["ok"], "errores": v["errores"], "advertencias": v["advertencias"], "casos": v["casos"],
                 "fecha": datetime.now(CR).isoformat(timespec="seconds")}
    conn.execute("""update negocio.cambios_normativos set resultado_pruebas = %s, parametros_propuestos = %s,
                    vigente_desde = %s where id = %s""", (json.dumps(resumen_v), json.dumps(propuestos), vigente, id))
    if not v["ok"]:
        # Se responde sin lanzar excepción para que el resultado de las pruebas quede guardado.
        return JSONResponse(status_code=409, content={
            "detail": "Los parámetros no pasaron la validación: " + "; ".join(v["errores"][:5]),
            "resultado_pruebas": json.loads(json.dumps(resumen_v, default=str))})
    try:
        pl = insertar_parametros(conn, vigente, v["datos"], f"{n['fuente']}" + (f" · {n['enlace']}" if n["enlace"] else ""), u.id)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "resultado_pruebas": resumen_v})
    fila = conn.execute("update negocio.cambios_normativos set estado = 'aplicado' where id = %s returning *", (id,)).fetchone()
    return {"normativa": _resultado(fila), "parametros": pl, "validacion": resumen_v}


@plataforma.post("/normativa/{id}/rechazar")
def rechazar_normativa(id: UUID, datos: RechazarIn | None = None, conn: Conexion = Depends(admin_plataforma)) -> dict:
    n = conn.execute("select * from negocio.cambios_normativos where id = %s", (id,)).fetchone()
    if not n:
        raise HTTPException(404, "Cambio normativo no encontrado")
    if n["estado"] == "aplicado":
        raise HTTPException(409, "El cambio ya se aplicó; registre un nuevo juego de parámetros para revertirlo")
    previo = _resultado(dict(n))["resultado_pruebas_json"]
    res = json.dumps({"motivo_rechazo": (datos.motivo if datos else None) or "", "validacion_previa": previo})
    fila = conn.execute("update negocio.cambios_normativos set estado = 'rechazado', resultado_pruebas = %s "
                        "where id = %s returning *", (res, id)).fetchone()
    return _resultado(fila)


# ================================================================== Salud ====
@plataforma.get("/salud", summary="Estado de los servicios y su historial")
def salud(conn: Conexion = Depends(admin_plataforma), horas: int = Query(24, ge=1, le=24 * 30)) -> dict:
    ultimos = conn.execute("""select distinct on (servicio) * from negocio.chequeos_salud
                               order by servicio, fecha desc""").fetchall()
    historial = conn.execute("""select * from negocio.chequeos_salud where fecha > now() - make_interval(hours => %s)
                                 order by fecha desc limit 2000""", (horas,)).fetchall()
    disponibilidad = conn.execute("""select servicio, round(100.0 * avg(ok::int), 2) as porcentaje,
                                            round(avg(latencia_ms)) as latencia_promedio_ms
                                       from negocio.chequeos_salud where fecha > now() - make_interval(hours => %s)
                                      group by servicio""", (horas,)).fetchall()
    return {"ok": all(u["ok"] for u in ultimos) if ultimos else None, "servicios": ultimos,
            "disponibilidad": disponibilidad, "historial": historial}


def cron_autorizado(x_cron_secret: str = Header(default="")) -> None:
    if not settings.cron_secret or not hmac.compare_digest(x_cron_secret.encode(), settings.cron_secret.encode()):
        raise HTTPException(401, "Secreto de tarea programada inválido")


def _http(metodo: str, url: str, *, headers: dict | None = None, ok=lambda resp: resp.status_code < 500) -> tuple[bool, int | None, str]:
    t0 = time.monotonic()
    try:
        resp = httpx.request(metodo, url, headers=headers, timeout=10, follow_redirects=True)
        ms = int((time.monotonic() - t0) * 1000)
        bien = ok(resp)
        return bien, ms, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, None, f"{type(exc).__name__}: {str(exc)[:150]}"


def chequear_servicios(conn: Conexion) -> list[dict]:
    resultados: list[dict] = []
    t0 = time.monotonic()
    try:
        with conn.transaction():
            conn.execute("select 1")
        resultados.append({"servicio": "base_datos", "ok": True, "latencia_ms": int((time.monotonic() - t0) * 1000), "detalle": ""})
    except psycopg.Error as exc:
        resultados.append({"servicio": "base_datos", "ok": False, "latencia_ms": None, "detalle": str(exc)[:200]})
    if settings.supabase_url:
        h = {"apikey": settings.supabase_service_key} if settings.supabase_service_key else None
        ok, ms, det = _http("GET", f"{settings.supabase_url.rstrip('/')}/auth/v1/health", headers=h)
        resultados.append({"servicio": "supabase", "ok": ok, "latencia_ms": ms, "detalle": det})
    if settings.app_url:
        ok, ms, det = _http("HEAD", settings.app_url)
        resultados.append({"servicio": "frontend", "ok": ok, "latencia_ms": ms, "detalle": det})
    if settings.telegram_bot_token:
        ok, ms, det = telegram.get_me()
        resultados.append({"servicio": "telegram", "ok": ok, "latencia_ms": ms, "detalle": det})
    if settings.resend_api_key:
        ok, ms, det = _http("GET", "https://api.resend.com/domains",
                            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                            ok=lambda resp: resp.status_code < 400)
        resultados.append({"servicio": "resend", "ok": ok, "latencia_ms": ms, "detalle": det})
    return resultados


@cron.post("/salud", dependencies=[Depends(cron_autorizado)], summary="Chequeo de servicios (cada 5-15 min)")
def cron_salud() -> dict:
    with db.sistema() as conn:
        resultados = chequear_servicios(conn)
        for res in resultados:
            previo = conn.execute("select ok from negocio.chequeos_salud where servicio = %s order by fecha desc limit 1",
                                  (res["servicio"],)).fetchone()
            conn.execute("insert into negocio.chequeos_salud (servicio, ok, latencia_ms, detalle) values (%s,%s,%s,%s)",
                         (res["servicio"], res["ok"], res["latencia_ms"], res["detalle"]))
            # Solo se alerta en el cambio de estado para no repetir el aviso cada chequeo.
            if not res["ok"] and (previo is None or previo["ok"]):
                alertas.alertar(conn, "caida", f"{res['servicio']} no responde ({res['detalle']})", "critica")
            elif res["ok"] and previo is not None and not previo["ok"]:
                alertas.alertar(conn, "caida", f"{res['servicio']} se recuperó ({res['latencia_ms']} ms)", "info")
    return {"ok": all(x["ok"] for x in resultados), "servicios": resultados}


# ======================================================== Tarea diaria =======
HITOS_VENCIMIENTO = (30, 15, 7, 3, 1, 0)   # días antes del vencimiento en que se avisa


def _mesiversario(anio: int, mes: int, dia: int) -> date:
    return date(anio, mes, min(dia, calendar.monthrange(anio, mes)[1]))


def meses_cumplidos(ingreso: date, desde: date, hasta: date) -> int:
    """Cantidad de "mesiversarios" (mismo día del mes que la fecha de ingreso; si el mes es más corto,
    el último día) en el intervalo (desde, hasta]. Port de `_meses_desde` de calc_vacation_accrual
    (models/rate_helper.py del módulo Odoo planilla_cr, método 'monthly', el predeterminado)."""
    if hasta <= desde:
        return 0
    idx = desde.year * 12 + desde.month - 1
    cand = _mesiversario(idx // 12, idx % 12 + 1, ingreso.day)
    if cand <= desde:
        idx += 1
        cand = _mesiversario(idx // 12, idx % 12 + 1, ingreso.day)
    n = 0
    while cand <= hasta:
        n += 1
        idx += 1
        cand = _mesiversario(idx // 12, idx % 12 + 1, ingreso.day)
    return n


def aplicar_cambios_salariales(conn, hoy: date) -> list[dict]:
    """Aplica a la ficha los cambios salariales autorizados con fecha efectiva de hoy (o de los 3 días
    anteriores, por si la tarea no corrió). Los que se autorizan ya vigentes los aplica la propia
    decisión en routers/empleados.py."""
    filas = conn.execute("""
        select distinct on (h.empleado_id) h.id, h.empleado_id, h.empresa_id, h.salario_nuevo, h.fecha_efectiva,
               e.salario_mensual as actual, e.nombre, e.apellidos
          from historial_salarial h
          join empleados e on e.id = h.empleado_id and e.estado = 'activo'
          join empresas em on em.id = h.empresa_id and em.estado_servicio <> 'baja'
         where h.estado = 'autorizado' and h.fecha_efectiva between %s and %s
         order by h.empleado_id, h.fecha_efectiva desc, h.autorizado_en desc nulls last""",
                         (hoy - timedelta(days=3), hoy)).fetchall()
    aplicados = []
    for f in filas:
        if Decimal(f["actual"]) == Decimal(f["salario_nuevo"]):
            continue
        conn.execute("update empleados set salario_mensual = %s where id = %s", (f["salario_nuevo"], f["empleado_id"]))
        aplicados.append({**f, "anterior": f["actual"]})
    return aplicados


def dias_incapacidad_vacaciones(conn, empleado_id, desde: date, hasta: date) -> int:
    """Días de incapacidad que cuentan para vacaciones (sin maternidad, Art. 95 CT) en (desde, hasta]."""
    return int(conn.execute("""select coalesce(sum(least(hasta, %(h)s) - greatest(desde, %(d)s + 1) + 1), 0) as dias
                                 from incapacidades where empleado_id = %(e)s and estado = 'confirmada' and tipo <> 'maternity'
                                  and desde <= %(h)s and hasta > %(d)s""",
                              {"e": empleado_id, "d": desde, "h": hasta}).fetchone()["dias"])


def acumular_vacaciones(conn, hoy: date, empleado_id=None) -> list[dict]:
    """Art. 153 CT: 12 días hábiles de vacaciones por cada 50 semanas. Igual que el módulo Odoo
    (calc_vacation_accrual, método mensual): 1 día por cada mes cumplido desde la fecha de ingreso.
    El saldo de la ficha es el punto de partida en la fecha en que el empleado se registró (o se
    importó) en el sistema; desde ahí se acredita cada mesiversario. La bitácora
    public.vacaciones_acumuladas guarda cada acreditación y hace la tarea idempotente."""
    filas = conn.execute("""
        select e.id, e.empresa_id, e.fecha_ingreso, e.saldo_vacaciones, (e.creado_en at time zone 'America/Costa_Rica')::date as creado,
               e.vacaciones_corte, (select max(fecha) from vacaciones_acumuladas va where va.empleado_id = e.id) as ultima
          from empleados e join empresas em on em.id = e.empresa_id and em.estado_servicio <> 'baja'
         where e.estado = 'activo' and e.fecha_ingreso <= %s and (%s::uuid is null or e.id = %s)""",
                         (hoy, empleado_id, empleado_id)).fetchall()
    acreditados = []
    configs: dict = {}
    for f in filas:
        # Con saldo inicial (pre-implementación) se acumula desde su fecha de corte, como en Odoo;
        # si no, desde que el colaborador entró al sistema.
        inicio = f["vacaciones_corte"] or f["creado"]
        corte = max(d for d in (f["ultima"], inicio, f["fecha_ingreso"]) if d is not None)
        n = meses_cumplidos(f["fecha_ingreso"], corte, hoy)
        if n <= 0:
            continue
        if f["empresa_id"] not in configs:
            configs[f["empresa_id"]] = conn.execute(
                """select vacaciones_excluir_incapacidad, vacaciones_dias_aniversario from configuracion_planilla
                    where empresa_id = %s""", (f["empresa_id"],)).fetchone() or {}
        cfg = configs[f["empresa_id"]]
        dias = Decimal(n)
        detalle = [f"{n} mes(es) cumplido(s) desde {corte:%d/%m/%Y} (Art. 153 CT)"]
        if cfg.get("vacaciones_excluir_incapacidad"):
            # Igual que el módulo Odoo: 1 día de vacación menos por cada 29 días de incapacidad acumulados
            # desde la base (corte del saldo o ingreso), sin maternidad. Se descuenta lo que se completó en este tramo.
            base = f["vacaciones_corte"] or f["fecha_ingreso"]
            antes = dias_incapacidad_vacaciones(conn, f["id"], base, corte) if corte > base else 0
            ahora = dias_incapacidad_vacaciones(conn, f["id"], base, hoy)
            menos = Decimal(ahora // 29 - antes // 29)
            if menos > 0:
                dias -= menos
                detalle.append(f"menos {menos} por incapacidad ({ahora} día(s) acumulados, 1 por cada 29)")
        if cfg.get("vacaciones_dias_aniversario"):
            # Beneficio por aniversario: el mes del aniversario suma hasta el total configurado
            # (el mes normal ya aporta 1 día). Solo desde el primer año cumplido.
            extra = Decimal(cfg["vacaciones_dias_aniversario"]) - 1
            ing = f["fecha_ingreso"]
            for anio in range(corte.year, hoy.year + 1):
                try:
                    aniv = ing.replace(year=anio)
                except ValueError:                  # 29 de febrero
                    aniv = date(anio, 2, 28)
                if anio > ing.year and corte < aniv <= hoy and extra > 0:
                    dias += extra
                    detalle.append(f"más {extra} por aniversario {anio - ing.year}")
        if dias <= 0:
            continue
        saldo = Decimal(f["saldo_vacaciones"]) + dias
        ins = conn.execute("""insert into vacaciones_acumuladas (empresa_id, empleado_id, fecha, dias, saldo_resultante, detalle)
                              values (%s,%s,%s,%s,%s,%s) on conflict (empleado_id, fecha) do nothing returning id""",
                           (f["empresa_id"], f["id"], hoy, dias, saldo, "; ".join(detalle))).fetchone()
        if ins:
            conn.execute("update empleados set saldo_vacaciones = saldo_vacaciones + %s where id = %s", (dias, f["id"]))
            acreditados.append({"empleado_id": f["id"], "empresa_id": f["empresa_id"], "dias": dias, "saldo": saldo})
    return acreditados


def rebase_vacaciones(conn, empleado_id, hoy: date) -> None:
    """Saldo inicial de vacaciones (pre-implementación, como en Odoo): el saldo vuelve a partir del
    dato de la ficha a la fecha de corte, menos lo disfrutado después del corte, y se re-acredita
    lo ganado desde entonces."""
    f = conn.execute("select vacaciones_saldo_inicial, vacaciones_corte from empleados where id = %s",
                     (empleado_id,)).fetchone()
    if not f or f["vacaciones_saldo_inicial"] is None or not f["vacaciones_corte"]:
        return
    tomados = conn.execute("""select coalesce(sum(dias), 0) as d from vacaciones
                               where empleado_id = %s and boleta_id is not null and desde > %s""",
                           (empleado_id, f["vacaciones_corte"])).fetchone()["d"]
    conn.execute("delete from vacaciones_acumuladas where empleado_id = %s and fecha > %s",
                 (empleado_id, f["vacaciones_corte"]))
    conn.execute("update empleados set saldo_vacaciones = %s where id = %s",
                 (Decimal(f["vacaciones_saldo_inicial"]) - Decimal(tomados), empleado_id))
    acumular_vacaciones(conn, hoy, empleado_id)


def resumen_vacaciones(conn, empleado_id) -> dict:
    """Panel «Saldo de vacaciones» de la ficha: de dónde sale el saldo actual."""
    e = conn.execute("""select fecha_ingreso, saldo_vacaciones, vacaciones_saldo_inicial, vacaciones_corte,
                               (creado_en at time zone 'America/Costa_Rica')::date as creado from empleados where id = %s""",
                     (empleado_id,)).fetchone()
    desde = e["vacaciones_corte"] or e["creado"]
    acum = conn.execute("""select fecha, dias, saldo_resultante, detalle from vacaciones_acumuladas
                            where empleado_id = %s and fecha > %s order by fecha""", (empleado_id, desde)).fetchall()
    tomados = conn.execute("""select coalesce(sum(dias), 0) as d from vacaciones
                               where empleado_id = %s and boleta_id is not null and desde > %s""",
                           (empleado_id, desde)).fetchone()["d"]
    return {"fecha_ingreso": e["fecha_ingreso"], "saldo_inicial": e["vacaciones_saldo_inicial"],
            "corte": e["vacaciones_corte"], "desde": desde,
            "acumulado": sum((Decimal(a["dias"]) for a in acum), Decimal(0)), "tomados": Decimal(tomados),
            "saldo": e["saldo_vacaciones"], "acreditaciones": acum}


def bonos_antiguedad(conn, hoy: date) -> list[dict]:
    """Como cron_bono_antiguedad de Odoo: el día del aniversario laboral crea un bono puntual (ese mes)
    según la tabla Configuración › Bono de antigüedad de la empresa. Es salarial: cotiza CCSS (Art. 162 CT)."""
    import calendar as _cal
    creados = []
    filas = conn.execute("""
        select e.id, e.empresa_id, e.nombre, e.apellidos, e.salario_mensual,
               extract(year from age(%(h)s::date, e.fecha_ingreso))::int as anios
          from empleados e join empresas em on em.id = e.empresa_id and em.estado_servicio in ('prueba','activo','gracia')
         where e.estado = 'activo' and e.fecha_ingreso < %(h)s
           and extract(month from e.fecha_ingreso) = extract(month from %(h)s::date)
           and extract(day from e.fecha_ingreso) = extract(day from %(h)s::date)
           and exists (select 1 from bono_antiguedad b where b.empresa_id = e.empresa_id and b.activo)""",
                         {"h": hoy}).fetchall()
    for f in filas:
        t = conn.execute("""select * from bono_antiguedad where empresa_id = %s and activo and desde_anios <= %s
                             and (hasta_anios is null or hasta_anios >= %s) order by desde_anios desc limit 1""",
                         (f["empresa_id"], f["anios"], f["anios"])).fetchone()
        if not t:
            continue
        monto = (Decimal(f["salario_mensual"]) * Decimal(t["porcentaje"]) / 100 if t["tipo"] == "porcentaje"
                 else Decimal(t["monto"])).quantize(Decimal("0.01"))
        if monto <= 0:
            continue
        nombre = f"Bono de antigüedad — {f['anios']} año(s)"
        if conn.execute("select 1 from bonos where empleado_id = %s and nombre = %s and desde >= %s",
                        (f["id"], nombre, date(hoy.year, 1, 1))).fetchone():
            continue
        fin = date(hoy.year, hoy.month, _cal.monthrange(hoy.year, hoy.month)[1])
        conn.execute("""insert into bonos (empresa_id, empleado_id, nombre, monto, afecto_ccss, afecto_salario_base,
                                           recurrente, desde, hasta)
                        values (%s, %s, %s, %s, true, true, false, %s, %s)""",
                     (f["empresa_id"], f["id"], nombre, monto, hoy, fin))
        creados.append({**f, "monto": monto, "nombre_bono": nombre})
    return creados


def cerrar_prestamos(conn) -> list[dict]:
    return conn.execute("""update prestamos p set estado = 'pagado' from empleados e
                            where e.id = p.empleado_id and p.estado in ('activo','pausado') and p.saldo <= 0
                        returning p.id, p.empresa_id, p.descripcion, e.nombre, e.apellidos""").fetchall()


def _nombre(f: dict) -> str:
    return f"{f['nombre']} {f.get('apellidos') or ''}".strip()


def avisos_empresas(conn, hoy: date) -> dict[Any, list[str]]:
    """Avisos para los administradores de cada empresa (portados de scheduled_actions.py)."""
    avisos: dict[Any, list[str]] = {}

    def add(eid, texto):
        avisos.setdefault(eid, []).append(texto)

    activas = sql.SQL("em.estado_servicio in ('prueba','activo','gracia','solo_lectura')")
    # Aniversarios laborales (cron_check_anniversaries). 29/02 se celebra el 28/02 en años no bisiestos.
    for f in conn.execute(sql.SQL("""
        select e.empresa_id, e.nombre, e.apellidos, e.fecha_ingreso from empleados e join empresas em on em.id = e.empresa_id
         where e.estado = 'activo' and {activas} and extract(year from e.fecha_ingreso) < %s
           and extract(month from e.fecha_ingreso) = %s
           and (extract(day from e.fecha_ingreso) = %s
                or (%s and extract(month from e.fecha_ingreso) = 2 and extract(day from e.fecha_ingreso) = 29))""").format(
                              activas=activas),
                          (hoy.year, hoy.month, hoy.day, hoy.month == 2 and hoy.day == 28 and not calendar.isleap(hoy.year))).fetchall():
        add(f["empresa_id"], f"🎉 {_nombre(f)} cumple {hoy.year - f['fecha_ingreso'].year} año(s) en la empresa.")
    # Embargos y pensiones por vencer (cron_alert_embargo_expiry, Art. 172 CT).
    for tabla, etiqueta in (("embargos", "Embargo"), ("pensiones_alimentarias", "Pensión alimentaria")):
        for f in conn.execute(sql.SQL("""
            select x.empresa_id, x.expediente, x.juzgado, x.hasta, e.nombre, e.apellidos
              from {tabla} x join empleados e on e.id = x.empleado_id join empresas em on em.id = x.empresa_id
             where x.estado = 'activo' and x.hasta is not null and {activas} and (x.hasta - %s) = any(%s)""").format(
                                  tabla=sql.Identifier(tabla), activas=activas),
                              (hoy, list(HITOS_VENCIMIENTO))).fetchall():
            dias = (f["hasta"] - hoy).days
            add(f["empresa_id"], f"⚖️ {etiqueta} de {_nombre(f)} (expediente {f['expediente']}"
                                 + (f", {f['juzgado']}" if f["juzgado"] else "") + ") "
                                 + ("vence hoy" if dias == 0 else f"vence el {f['hasta']:%d/%m/%Y} ({dias} días)")
                                 + ". Coordine con el juzgado.")
    # Documentos del empleado por vencer y, los lunes, los ya vencidos (cron_alert_document_expiry).
    for f in conn.execute(sql.SQL("""
        select d.empresa_id, d.tipo, d.numero, d.vence, e.nombre, e.apellidos
          from documentos_empleado d join empleados e on e.id = d.empleado_id and e.estado = 'activo'
          join empresas em on em.id = d.empresa_id
         where d.vence is not null and {activas}
           and ((d.vence - %s) = any(%s) or (%s and d.vence < %s and d.vence >= %s - 365))
         order by d.vence""").format(activas=activas), (hoy, list(HITOS_VENCIMIENTO), hoy.weekday() == 0, hoy, hoy)).fetchall():
        dias = (f["vence"] - hoy).days
        estado = (f"venció el {f['vence']:%d/%m/%Y}" if dias < 0 else "vence hoy" if dias == 0
                  else f"vence el {f['vence']:%d/%m/%Y} ({dias} días)")
        add(f["empresa_id"], f"📄 {f['tipo']} de {_nombre(f)}" + (f" ({f['numero']})" if f["numero"] else "") + f" {estado}.")
    # Saldos de vacaciones negativos: los lunes (cron_alert_negative_vacations).
    if hoy.weekday() == 0:
        for f in conn.execute(sql.SQL("""
            select e.empresa_id, e.nombre, e.apellidos, e.saldo_vacaciones from empleados e join empresas em on em.id = e.empresa_id
             where e.estado = 'activo' and e.saldo_vacaciones < 0 and {activas}""").format(
                activas=activas)).fetchall():
            add(f["empresa_id"], f"🏖️ {_nombre(f)} tiene saldo de vacaciones negativo ({f['saldo_vacaciones']} días).")
    # Vacaciones por prescribir: el día 1 de cada mes (cron_alert_prescribing_vacations, Art. 156 CT:
    # prescriben a los 2 años; se avisa desde los 22 meses sin disfrutarlas).
    if hoy.day == 1:
        for f in conn.execute(sql.SQL("""
            select e.empresa_id, e.nombre, e.apellidos, e.saldo_vacaciones,
                   coalesce((select max(v.desde) from vacaciones v where v.empleado_id = e.id and v.tipo = 'disfrute'
                              and v.estado in ('aprobada','pagada')), e.fecha_ingreso) as referencia
              from empleados e join empresas em on em.id = e.empresa_id
             where e.estado = 'activo' and e.saldo_vacaciones > 0 and {activas}""").format(
                activas=activas)).fetchall():
            meses = (hoy - f["referencia"]).days / 30
            if meses >= 22:
                add(f["empresa_id"], f"⏳ {_nombre(f)} tiene {f['saldo_vacaciones']} días de vacaciones sin disfrutar "
                                     f"desde hace ~{meses:.0f} meses: pueden prescribir (Art. 156 CT).")
    return avisos


def recordatorios(conn, hoy: date) -> dict[Any, list[str]]:
    """Recordatorios opcionales por Telegram (avisos.recordatorios) para administradores de clientes:
    planillas por pagar sin aprobar, presentación de planillas a la CCSS (SICERE) y al INS (RT-Virtual) y
    aguinaldo. Los días se configuran en negocio.configuracion clave 'recordatorios'."""
    cfg_fila = conn.execute("select valor from negocio.configuracion where clave = 'recordatorios'").fetchone()
    cfg = {"sicere_dia": 1, "ins_dia": 1, "aguinaldo_dias": [1, 15], **((cfg_fila or {}).get("valor") or {})}
    generales = []
    mes_ant = (hoy.replace(day=1) - timedelta(days=1))
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
    if hoy.day == int(cfg["sicere_dia"]):
        generales.append(f"🏥 Recuerde reportar en SICERE (CCSS) la planilla de {meses[mes_ant.month - 1]}. "
                         "En Reportes › Exportaciones descargue el archivo SICERE.")
    if hoy.day == int(cfg["ins_dia"]):
        generales.append(f"🦺 Recuerde reportar al INS (RT-Virtual) la planilla de {meses[mes_ant.month - 1]}. "
                         "En Reportes › Exportaciones descargue el archivo del INS.")
    if hoy.month == 12 and hoy.day in [int(d) for d in cfg["aguinaldo_dias"]]:
        generales.append("🎁 El aguinaldo debe pagarse dentro de los primeros 20 días de diciembre. "
                         "Calcúlelo en Aguinaldo y cree la planilla.")
    salida: dict[Any, list[str]] = {}
    empresas = conn.execute("select id from empresas where estado_servicio in ('prueba','activo','gracia')").fetchall()
    for e in empresas:
        if generales:
            salida[e["id"]] = list(generales)
    for p in conn.execute("""select p.empresa_id, p.desde, p.hasta, p.fecha_pago, p.estado from planillas p
                               join empresas em on em.id = p.empresa_id and em.estado_servicio in ('prueba','activo','gracia')
                              where p.estado in ('borrador','calculada') and p.fecha_pago between %s and %s""",
                          (hoy, hoy + timedelta(days=2))).fetchall():
        salida.setdefault(p["empresa_id"], []).append(
            f"🗓️ La planilla del {p['desde']:%d/%m} al {p['hasta']:%d/%m} se paga el {p['fecha_pago']:%d/%m} "
            f"y está en <i>{p['estado']}</i>: falta calcularla o aprobarla.")
    return salida


def avisar_boletas_listas(conn) -> int:
    """Avisa por Telegram (avisos.boletas) a los empleados con portal cuando su boleta de una planilla
    cerrada en los últimos 3 días está disponible. envios_boleta (canal telegram) evita repetir."""
    filas = conn.execute("""
        select b.id as boleta_id, b.empresa_id, v.chat_id, e.nombre, p.desde, p.hasta, em.nombre as empresa
          from boletas b join planillas p on p.id = b.planilla_id and p.estado = 'cerrada'
                                       and p.cerrada_en > now() - interval '3 days'
          join empleados e on e.id = b.empleado_id and e.usuario_id is not null
          join empresas em on em.id = b.empresa_id
          join lateral (select chat_id from telegram_vinculos v where v.usuario_id = e.usuario_id and v.activo
                          and v.chat_id is not null and coalesce((v.avisos ->> 'boletas')::boolean, true)
                        order by v.creado_en desc limit 1) v on true
         where not exists (select 1 from envios_boleta x where x.boleta_id = b.id and x.canal = 'telegram')""").fetchall()
    n = 0
    for f in filas:
        ok = telegram.enviar_mensaje(f["chat_id"], f"🧾 Hola {esc(f['nombre'])}, su boleta del {f['desde']:%d/%m} al "
                                                   f"{f['hasta']:%d/%m/%Y} de {esc(f['empresa'])} ya está disponible:\n"
                                                   f"{settings.app_url}/portal")
        conn.execute("insert into envios_boleta (empresa_id, boleta_id, canal, destino, estado) values (%s,%s,'telegram',%s,%s)",
                     (f["empresa_id"], f["boleta_id"], f["chat_id"], "enviado" if ok else "error"))
        n += ok
    return n


def actualizar_prospectos(conn, hoy: date) -> dict:
    convertidos = conn.execute("""
        update prospectos p set estado = 'convertido', empresa_id = em.id
          from empresas em
         where p.estado = 'abierto' and em.referido_por = p.socio_id
           and public.sin_tildes(trim(em.nombre)) = public.sin_tildes(trim(p.empresa_nombre))
        returning p.id""").rowcount
    vencidos = conn.execute("update prospectos set estado = 'vencido' where estado = 'abierto' and vence_atribucion < %s",
                            (hoy,)).rowcount
    return {"convertidos": convertidos, "vencidos": vencidos}


def _enviar_a_empresa(conn, empresa_id, textos: list[str], *, por_correo: bool, aviso: str) -> dict:
    empresa = conn.execute("select nombre, correo from empresas where id = %s", (empresa_id,)).fetchone()
    res = {"correo": False, "telegram": 0}
    if por_correo:
        destinos = {f["email"] for f in conn.execute("""
            select u.email from usuarios_empresa ue join auth.users u on u.id = ue.usuario_id
             where ue.empresa_id = %s and ue.activo and ue.rol = 'admin' and u.email is not null""", (empresa_id,)).fetchall()}
        if empresa["correo"]:
            destinos.add(empresa["correo"])
        if destinos:
            cuerpo = "".join(f"<li>{esc(t)}</li>" for t in textos)
            try:
                correo.enviar(sorted(destinos), f"Avisos de planilla · {empresa['nombre']}",
                              f"<p>Avisos de hoy para <b>{esc(empresa['nombre'])}</b>:</p><ul>{cuerpo}</ul>"
                              f"<p><a href=\"{settings.app_url}\">Abrir Planilla Costa Rica</a></p>")
                res["correo"] = True
            except Exception as exc:  # noqa: BLE001 - un correo fallido no detiene la tarea
                log.warning("Correo de avisos a %s no enviado: %s", empresa["nombre"], exc)
    texto_tg = f"<b>{esc(empresa['nombre'])}</b>\n" + "\n".join(t if "<i>" in t else esc(t) for t in textos)
    for chat in telegram.chats_admins_empresa(conn, empresa_id, aviso):
        res["telegram"] += telegram.enviar_mensaje(chat, texto_tg)
    return res


def alertas_documentos(conn, hoy: date) -> int:
    """Configuración › Planilla › «Alertas de documentos» (como el módulo Odoo): envía a los correos
    configurados la lista de documentos de colaboradores vencidos o por vencer, con un Excel adjunto.
    Semanal (cada 7 días) o mensual (una vez por mes). Devuelve cuántas empresas recibieron aviso."""
    import io as _io

    from openpyxl import Workbook
    from openpyxl.styles import Font

    enviados = 0
    for c in conn.execute("""select c.empresa_id, c.alertas_documentos_correos, c.alertas_documentos_frecuencia,
                                    c.alertas_documentos_dias, c.alertas_documentos_ultima, em.nombre
                               from configuracion_planilla c join empresas em on em.id = c.empresa_id
                              where coalesce(trim(c.alertas_documentos_correos), '') <> ''
                                and em.estado_servicio in ('prueba','activo','gracia')""").fetchall():
        u = c["alertas_documentos_ultima"]
        if u and (((hoy - u).days < 7) if c["alertas_documentos_frecuencia"] == "semanal"
                  else (u.year, u.month) == (hoy.year, hoy.month)):
            continue
        # Días de aviso: los del tipo de documento (Configuración › Tipos de documento) o los generales.
        docs = conn.execute("""select d.tipo, d.numero, d.vence, e.nombre, e.apellidos, e.identificacion
                                 from documentos_empleado d join empleados e on e.id = d.empleado_id and e.estado = 'activo'
                                 left join tipos_documento t on t.empresa_id = d.empresa_id and lower(t.nombre) = lower(d.tipo)
                                where d.empresa_id = %s and d.vence is not null
                                  and d.vence <= %s::date + coalesce(t.dias_alerta, %s)
                                order by d.vence, e.apellidos""",
                            (c["empresa_id"], hoy, int(c["alertas_documentos_dias"]))).fetchall()
        conn.execute("update configuracion_planilla set alertas_documentos_ultima = %s where empresa_id = %s",
                     (hoy, c["empresa_id"]))
        if not docs:
            continue
        wb = Workbook()
        ws = wb.active
        ws.title = "Documentos"
        ws.append(["Colaborador", "Identificación", "Documento", "Número", "Vence", "Estado"])
        for cel in ws[1]:
            cel.font = Font(bold=True)
        filas_html = []
        for d in docs:
            estado = "Vencido" if d["vence"] < hoy else f"Vence en {(d['vence'] - hoy).days} día(s)"
            nombre = f"{d['nombre']} {d['apellidos'] or ''}".strip()
            ws.append([nombre, d["identificacion"], d["tipo"], d["numero"] or "", d["vence"], estado])
            filas_html.append(f"<tr><td>{html.escape(nombre)}</td><td>{html.escape(d['tipo'])}</td>"
                              f"<td>{d['vence']:%d/%m/%Y}</td><td>{estado}</td></tr>")
        buf = _io.BytesIO()
        wb.save(buf)
        destinos = [x.strip() for x in c["alertas_documentos_correos"].split(",") if x.strip()]
        correo.enviar(destinos, f"Documentos por vencer · {c['nombre']}",
                      f"<p>Documentos de colaboradores vencidos o que vencen en los próximos "
                      f"{c['alertas_documentos_dias']} días:</p><table border='1' cellpadding='4' cellspacing='0'>"
                      "<tr><th>Colaborador</th><th>Documento</th><th>Vence</th><th>Estado</th></tr>"
                      + "".join(filas_html) + "</table>",
                      [(f"documentos-por-vencer-{hoy}.xlsx", buf.getvalue())])
        enviados += 1
    return enviados


def _ya_corrio(conn, hoy: date) -> bool:
    fila = conn.execute("select valor from negocio.configuracion where clave = 'cron_diario'").fetchone()
    return bool(fila and (fila["valor"] or {}).get("fecha") == hoy.isoformat())


@cron.post("/diario", dependencies=[Depends(cron_autorizado)], summary="Tareas diarias (7:00 a. m. hora de Costa Rica)")
def cron_diario(fecha: date | None = Query(None, description="Solo para pruebas o reprocesos; por defecto hoy en Costa Rica"),
                forzar_avisos: bool = False) -> dict:
    hoy = fecha or hoy_cr()
    resultado: dict[str, Any] = {"fecha": hoy.isoformat(), "errores": []}
    with db.sistema() as conn:
        avisos_repetidos = _ya_corrio(conn, hoy) and not forzar_avisos

        def tarea(nombre, fn, *args, **kw):
            try:
                with conn.transaction():
                    return fn(*args, **kw)
            except Exception as exc:  # noqa: BLE001 - una tarea fallida no detiene las demás
                log.exception("Tarea diaria %s falló", nombre)
                resultado["errores"].append(f"{nombre}: {exc}")
                return None

        cambios = tarea("cambios_salariales", aplicar_cambios_salariales, conn, hoy) or []
        acum = tarea("vacaciones", acumular_vacaciones, conn, hoy) or []
        prestamos = tarea("prestamos", cerrar_prestamos, conn) or []
        antiguedad = tarea("bono_antiguedad", bonos_antiguedad, conn, hoy) or []
        prospectos = tarea("prospectos", actualizar_prospectos, conn, hoy) or {}
        resultado.update({
            "cambios_salariales": [{"empleado_id": c["empleado_id"], "salario_anterior": c["anterior"],
                                    "salario_nuevo": c["salario_nuevo"]} for c in cambios],
            "vacaciones": {"empleados": len(acum), "dias": sum(a["dias"] for a in acum)},
            "prestamos_cerrados": len(prestamos), "prospectos": prospectos, "bonos_antiguedad": len(antiguedad),
        })

        enviados = {"empresas": 0, "correos": 0, "telegram": 0, "boletas_telegram": 0}
        if not avisos_repetidos:
            avisos = tarea("avisos", avisos_empresas, conn, hoy) or {}
            for c in cambios:
                avisos.setdefault(c["empresa_id"], []).append(
                    f"💼 Se aplicó el nuevo salario de {_nombre(c)}: ₡{Decimal(c['salario_nuevo']):,.2f} "
                    f"(desde {c['fecha_efectiva']:%d/%m/%Y}).")
            for b in antiguedad:
                avisos.setdefault(b["empresa_id"], []).append(
                    f"🎉 {_nombre(b)} cumple {b['anios']} año(s): se agregó su bono de antigüedad de ₡{b['monto']:,.2f}.")
            for p in prestamos:
                avisos.setdefault(p["empresa_id"], []).append(f"✅ El préstamo «{p['descripcion']}» de {_nombre(p)} quedó pagado.")
            for eid, textos in avisos.items():
                res = tarea("envio_avisos", _enviar_a_empresa, conn, eid, textos[:60], por_correo=True, aviso="recordatorios")
                if res:
                    enviados["empresas"] += 1
                    enviados["correos"] += int(res["correo"])
                    enviados["telegram"] += res["telegram"]
            for eid, textos in (tarea("recordatorios", recordatorios, conn, hoy) or {}).items():
                res = tarea("envio_recordatorios", _enviar_a_empresa, conn, eid, textos, por_correo=False, aviso="recordatorios")
                if res:
                    enviados["telegram"] += res["telegram"]
            enviados["boletas_telegram"] = tarea("boletas_telegram", avisar_boletas_listas, conn) or 0
            enviados["alertas_documentos"] = tarea("alertas_documentos", alertas_documentos, conn, hoy) or 0
        resultado["avisos"] = enviados
        resultado["avisos_omitidos"] = avisos_repetidos

        # Resumen diario para la dueña.
        if not avisos_repetidos:
            def _resumen():
                est = conn.execute("select estado_servicio, count(*)::int n from empresas group by 1 order by 1").fetchall()
                nuevos = conn.execute("select count(*)::int n from empresas where creado_en > now() - interval '1 day'").fetchone()["n"]
                tk = conn.execute("""select count(*) filter (where estado in ('abierto','en_proceso','escalado'))::int abiertos,
                                            count(*) filter (where nivel = 'plataforma' and estado in ('abierto','en_proceso','escalado'))::int plataforma,
                                            count(*) filter (where prioridad = 'urgente' and estado in ('abierto','en_proceso','escalado'))::int urgentes
                                       from tickets""").fetchone()
                pend = conn.execute("select count(*)::int n from negocio.alertas where not atendida").fetchone()["n"]
                clientes = ", ".join(f"{e['estado_servicio']} {e['n']}" for e in est) or "ninguno"
                texto = (f"Resumen {hoy:%d/%m/%Y}\n"
                         f"Clientes: {clientes} · nuevos 24 h: {nuevos}\n"
                         f"Tickets abiertos: {tk['abiertos']} (plataforma {tk['plataforma']}, urgentes {tk['urgentes']})\n"
                         f"Alertas sin atender: {pend}\n"
                         f"Tareas: {len(cambios)} cambios salariales, {resultado['vacaciones']['empleados']} empleados con "
                         f"vacaciones acreditadas ({resultado['vacaciones']['dias']} días), {len(prestamos)} préstamos cerrados, "
                         f"avisos a {enviados['empresas']} empresas")
                if resultado["errores"]:
                    texto += f"\nErrores: {len(resultado['errores'])}"
                alertas.alertar(conn, "resumen", texto, "aviso" if resultado["errores"] else "info")
                return texto
            resultado["resumen"] = tarea("resumen", _resumen)
            conn.execute("""insert into negocio.configuracion (clave, valor) values ('cron_diario', %s)
                            on conflict (clave) do update set valor = excluded.valor""",
                         (json.dumps({"fecha": hoy.isoformat(), "errores": len(resultado["errores"])}),))
    return json.loads(json.dumps(resultado, default=str))


@cron.post("/ancla-auditoria", dependencies=[Depends(cron_autorizado)],
           summary="Verifica la bitácora encadenada y envía su huella fuera de la base (diario)")
def ancla_auditoria() -> dict:
    """La huella final de la cadena se manda por Telegram a la dueña: si alguien con acceso
    total a la base reescribiera la bitácora, la huella dejaría de coincidir con las enviadas."""
    with db.sistema() as conn:
        r = conn.execute("select * from public.verificar_auditoria()").fetchone()
        if r["id_alterado"] is not None:
            alertas.alertar(conn, "error", f"La bitácora de auditoría fue alterada (registro {r['id_alterado']}).", "critica")
            return {"integra": False, "id_alterado": r["id_alterado"]}
        huella = (r["hash_final"] or "")[:16]
        alertas.alertar(conn, "resumen", f"Bitácora íntegra: {r['revisados']} registros, huella {huella}.", "info")
    return {"integra": True, "revisados": r["revisados"], "huella": huella}
