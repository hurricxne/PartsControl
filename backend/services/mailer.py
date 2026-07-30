"""Envío de correo de la app (best-effort). Usa la config SMTP de settings/.env.
Reutiliza la casilla no-reply@bigcode.cl (mail.bigcode.cl). Si no hay SMTP configurado
o falla el envío, NO rompe el flujo del request (devuelve False y loguea)."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from config import settings

logger = logging.getLogger("mailer")


def enviar_correo(asunto: str, cuerpo: str, destino: str | None = None) -> bool:
    host = getattr(settings, "SMTP_HOST", "") or ""
    if not host:
        logger.info("[mailer] sin SMTP configurado; correo omitido: %s", asunto)
        return False
    to = destino or getattr(settings, "MAIL_SOPORTE", "") or ""
    if not to:
        logger.info("[mailer] sin destinatario; correo omitido")
        return False
    m = EmailMessage()
    m["Subject"] = asunto
    m["From"] = getattr(settings, "MAIL_FROM", "") or getattr(settings, "SMTP_USER", "")
    m["To"] = to
    m.set_content(cuerpo)
    try:
        port = int(getattr(settings, "SMTP_PORT", 587) or 587)
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            user = getattr(settings, "SMTP_USER", "") or ""
            if user:
                s.login(user, getattr(settings, "SMTP_PASS", "") or "")
            s.send_message(m)
        logger.info("[mailer] correo enviado a %s: %s", to, asunto)
        return True
    except Exception as e:  # best-effort: nunca tumba el request
        logger.warning("[mailer] error enviando correo: %s", e)
        return False
