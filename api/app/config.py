"""Configuración por variables de entorno (Railway)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _lista(nombre: str, defecto: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(nombre, defecto).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # Base de datos: cadena de conexión de Supabase (pooler en modo sesión o directa).
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:54322/postgres")
    # Supabase Auth: proyecto nuevo -> JWKS (ES256/RS256); proyecto viejo -> secreto HS256.
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_jwt_secret: str = os.environ.get("SUPABASE_JWT_SECRET", "")
    # Llave de administración, solo para invitar usuarios. Recomendada: la "secret key" nueva (sb_secret_...).
    # Se acepta también la service_role antigua (JWT), que Supabase retira a fines de 2026.
    supabase_service_key: str = os.environ.get("SUPABASE_SECRET_KEY", "") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    jwt_audience: str = os.environ.get("JWT_AUDIENCE", "authenticated")
    cors_origins: list[str] = field(default_factory=lambda: _lista("CORS_ORIGINS", "http://localhost:3000"))
    app_url: str = os.environ.get("APP_URL", "http://localhost:3000")
    # Correo (Resend)
    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")
    correo_remitente: str = os.environ.get("CORREO_REMITENTE", "Planilla Costa Rica <planilla@auromtec.com>")
    # Tareas programadas (pg_cron / Railway cron llaman con este secreto)
    cron_secret: str = os.environ.get("CRON_SECRET", "")
    # Intervalo mínimo y tope diario de cada tarea (app/servicios/tareas.py). Solo las pruebas lo apagan.
    cron_limites: bool = os.environ.get("CRON_LIMITES", "si").lower() not in ("no", "0", "false")
    # Telegram
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    telegram_chat_duena: str = os.environ.get("TELEGRAM_CHAT_DUENA", "")
    telegram_bot_username: str = os.environ.get("TELEGRAM_BOT_USERNAME", "PlanillaCostaRicaBot")  # sin @
    # Tilopay
    tilopay_api_url: str = os.environ.get("TILOPAY_API_URL", "https://app.tilopay.com/api/v1")
    tilopay_api_key: str = os.environ.get("TILOPAY_API_KEY", "")
    tilopay_api_user: str = os.environ.get("TILOPAY_API_USER", "")
    tilopay_api_password: str = os.environ.get("TILOPAY_API_PASSWORD", "")
    tilopay_webhook_secret: str = os.environ.get("TILOPAY_WEBHOOK_SECRET", "")
    # URL pública de esta API (Railway): retorno de Tilopay y webhooks que se registran allá.
    api_url: str = os.environ.get("API_URL", "http://localhost:8000")
    # Callback opcional de Hacienda (POST con el resultado). Vacío = solo consulta periódica.
    hacienda_callback_url: str = os.environ.get("HACIENDA_CALLBACK_URL", "")
    # Hacienda (facturación electrónica 4.4)
    hacienda_ambiente: str = os.environ.get("HACIENDA_AMBIENTE", "pruebas")  # pruebas | produccion
    hacienda_usuario: str = os.environ.get("HACIENDA_USUARIO", "")
    hacienda_clave: str = os.environ.get("HACIENDA_CLAVE", "")
    hacienda_p12_base64: str = os.environ.get("HACIENDA_P12_BASE64", "")
    hacienda_p12_pin: str = os.environ.get("HACIENDA_P12_PIN", "")
    entorno: str = os.environ.get("ENTORNO", "desarrollo")
    # --- Seguridad ---
    # MFA (segundo factor) obligatoria para aprobar, cerrar, autorizar salarios, administrar y plataforma.
    mfa_obligatoria: bool = os.environ.get("MFA_OBLIGATORIA", "si" if os.environ.get("ENTORNO") == "produccion" else "no") == "si"
    # Llave AES-256 en base64 (32 bytes) para cifrar datos sensibles guardados (tokens de tarjeta).
    clave_cifrado: str = os.environ.get("CLAVE_CIFRADO", "")
    db_statement_timeout_ms: int = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "20000"))
    # Conexiones a Supabase POR PROCESO (uvicorn --workers). Total = workers × DB_POOL_MAX: con 2 workers
    # y 5, son 10 como máximo, dentro del límite del plan. Las inactivas se cierran a los 5 minutos.
    # DB_POOL_MIN = 0: sin uso no queda ninguna conexión abierta, así Railway (Serverless) puede dormir
    # la API; la primera petición después abre una conexión nueva (unos milisegundos más).
    db_pool_min: int = int(os.environ.get("DB_POOL_MIN", "0"))
    db_pool_max: int = int(os.environ.get("DB_POOL_MAX", "5"))
    max_cuerpo_bytes: int = int(os.environ.get("MAX_CUERPO_BYTES", str(8 * 1024 * 1024)))
    limite_por_minuto: int = int(os.environ.get("LIMITE_POR_MINUTO", "300"))
    limite_sensible_por_minuto: int = int(os.environ.get("LIMITE_SENSIBLE_POR_MINUTO", "30"))
    hosts_permitidos: list[str] = field(default_factory=lambda: _lista("HOSTS_PERMITIDOS", ""))
    documentacion_publica: bool = os.environ.get("DOCUMENTACION_PUBLICA", "no" if os.environ.get("ENTORNO") == "produccion" else "si") == "si"


settings = Settings()


def cabeceras_admin_supabase() -> dict[str, str]:
    """Cabeceras para la API de administración de Supabase Auth (invitaciones).

    La llave nueva (sb_secret_...) va solo en `apikey`: no es un JWT y Supabase pide no enviarla en
    `Authorization: Bearer`. La service_role antigua es un JWT y se envía en ambas, como siempre.
    """
    llave = settings.supabase_service_key
    if llave.startswith("sb_secret_"):
        return {"apikey": llave}
    return {"apikey": llave, "Authorization": f"Bearer {llave}"}
