#!/usr/bin/env python3
"""
================================================================================
NATHANIEL RIDGE — RIDGECREST TELEGRAM LISTENER
================================================================================
Servicio independiente de todos los demás. A diferencia de los bots de
análisis y de ridgecrest-pagos/vigilante (que consultan APIs por su cuenta
cada cierto tiempo), este servicio queda "escuchando" Telegram en tiempo
real, usando long-polling (getUpdates), y reacciona a 2 cosas:

  1. (Punto 2) Alguien se une a uno de los 4 canales -> manda un mensaje de
     bienvenida automático al canal, con el link a los Términos y
     Condiciones y una explicación breve de cómo funciona.

  2. (Punto 6) Un cliente le escribe "/estado" al bot en privado -> el bot
     busca su suscripción en Supabase (por su @usuario de Telegram) y le
     responde el estado, los canales que tiene, y cuándo vence — sin que
     el cliente tenga que preguntarle nada al administrador.

No manda señales, no analiza mercados, no toca dinero. Usa el MISMO bot
unificado que administra los 4 canales (el mismo token que usás para
generar las invitaciones de un solo uso).

------------------------------------------------------------------------------
Requisitos:
    pip install requests

Variables de entorno obligatorias:
    export SUPABASE_URL="https://tu-proyecto.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="tu-clave-secreta-de-supabase"
    export TELEGRAM_BOT_TOKEN_CANALES="token-del-bot-unificado-de-los-4-canales"
    export TELEGRAM_CHAT_ID_CRIPTO="..."
    export TELEGRAM_CHAT_ID_FOREX="..."
    export TELEGRAM_CHAT_ID_MATERIAS_PRIMAS="..."
    export TELEGRAM_CHAT_ID_ACCIONES="..."

Variable opcional:
    TERMINOS_URL   Link público a la página de Términos y Condiciones, para
                   incluirlo en el mensaje de bienvenida.

Uso:
    python telegram_listener.py
================================================================================
"""
import os
import sys
import time
from datetime import datetime, timezone
from html import escape as escape_html
from typing import List, Optional

import requests

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_CANALES", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

TERMINOS_URL = os.environ.get("TERMINOS_URL", "").strip()

CHAT_ID_A_BOT = {
    os.environ.get("TELEGRAM_CHAT_ID_CRIPTO", "").strip(): "Cripto",
    os.environ.get("TELEGRAM_CHAT_ID_FOREX", "").strip(): "Forex",
    os.environ.get("TELEGRAM_CHAT_ID_MATERIAS_PRIMAS", "").strip(): "Materias Primas",
    os.environ.get("TELEGRAM_CHAT_ID_ACCIONES", "").strip(): "Acciones",
}
CHAT_ID_A_BOT.pop("", None)  # por si alguna variable no está configurada

BOT_A_COLUMNA = {
    "Cripto": "incluye_cripto",
    "Forex": "incluye_forex",
    "Materias Primas": "incluye_materias_primas",
    "Acciones": "incluye_acciones",
}

# ── Punto agregado a pedido del usuario: auto-atención de ventas ──────────
# Comandos /precios y /comprar, para que un cliente nuevo pueda anotarse
# solo, sin tener que escribirle antes al administrador. El administrador
# sigue dando el visto bueno final ("Confirmar y renovar" en el
# backoffice) — esto solo automatiza el registro del pedido y la entrega
# de la dirección de depósito.
CANALES_VALIDOS = {
    "cripto": "incluye_cripto",
    "forex": "incluye_forex",
    "materias_primas": "incluye_materias_primas",
    "acciones": "incluye_acciones",
}
PRECIO_POR_PLAN = {"2": 15, "4": 30}
DIRECCION_PAGO_USDT = os.environ.get("DIRECCION_PAGO_USDT", "").strip()
RED_PAGO_USDT = os.environ.get("RED_PAGO_USDT", "TRC20").strip()


# ==============================================================================
# 1. SUPABASE — buscar cliente por su usuario de Telegram
# ==============================================================================
def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _normalizar_usuario(u: Optional[str]) -> str:
    return (u or "").strip().lstrip("@").lower()


def buscar_cliente_por_usuario(username: str) -> Optional[dict]:
    """Trae todos los clientes y busca coincidencia por @usuario de Telegram,
    ignorando mayúsculas/minúsculas y el símbolo @. Para el tamaño de
    negocio de Ridgecrest (decenas/pocos cientos de clientes), traer todos
    y comparar en Python es más simple y confiable que armar un filtro
    exacto del lado de Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/clientes"
    resp = requests.get(
        url,
        params={"select": "*,suscripciones(*)"},
        headers=_supabase_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    objetivo = _normalizar_usuario(username)
    for cliente in resp.json():
        if _normalizar_usuario(cliente.get("telegram_usuario")) == objetivo:
            return cliente
    return None


def guardar_telegram_id(cliente_id: str, telegram_id: int) -> None:
    """Guarda el ID numérico de Telegram del cliente la primera vez que
    interactúa con el bot (uniéndose a un canal, o usando /estado). Se
    necesita este número (no alcanza con el @usuario) para poder sacarlo
    de un canal más adelante si corresponde (punto 5.3)."""
    url = f"{SUPABASE_URL}/rest/v1/clientes"
    resp = requests.patch(
        url,
        params={"id": f"eq.{cliente_id}"},
        json={"telegram_id": telegram_id},
        headers=_supabase_headers(),
        timeout=20,
    )
    if resp.status_code >= 300:
        print(f"[Aviso] No se pudo guardar el telegram_id de {cliente_id}: {resp.status_code} {resp.text}")


def crear_cliente(nombre: str, username: str, telegram_id: Optional[int]) -> dict:
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/clientes",
        json={"nombre": nombre, "telegram_usuario": f"@{username}", "telegram_id": telegram_id},
        headers={**_supabase_headers(), "Prefer": "return=representation"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()[0]


def crear_suscripcion_pendiente(cliente_id: str, columnas: dict, monto: float, hoy: str) -> None:
    # Se crea con estado "cancelada" (no "activa") a propósito: es solo un
    # borrador con el plan elegido, todavía no significa que el cliente
    # tenga acceso. El administrador la activa de verdad al confirmar el
    # pago desde el backoffice ("Confirmar y renovar").
    payload = {
        "cliente_id": cliente_id, "precio_usdt": monto,
        "fecha_inicio": hoy, "fecha_vencimiento": hoy, "estado": "cancelada",
        **columnas,
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/suscripciones", json=payload,
        headers={**_supabase_headers(), "Prefer": "return=minimal"}, timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"No se pudo crear la suscripción: {resp.status_code} {resp.text}")


def existe_pago_pendiente(cliente_id: str) -> bool:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/pagos",
        params={"cliente_id": f"eq.{cliente_id}", "estado": "eq.pendiente", "select": "id"},
        headers=_supabase_headers(), timeout=20,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def crear_pago_pendiente(cliente_id: str, monto: float, hoy: str) -> None:
    payload = {"cliente_id": cliente_id, "monto_usdt": monto, "fecha": hoy, "estado": "pendiente"}
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/pagos", json=payload,
        headers={**_supabase_headers(), "Prefer": "return=minimal"}, timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"No se pudo crear el pago: {resp.status_code} {resp.text}")


def texto_precios() -> str:
    lineas = [
        "📊 Planes de Ridgecrest:",
        "",
        "• 2 canales a elección — 15 USDT/mes",
        "• Los 4 canales (Cripto, Forex, Materias Primas, Acciones) — 30 USDT/mes",
        "",
        "Para registrar tu pedido, escribime uno de estos:",
        "/comprar 4",
        "/comprar cripto forex",
        "(opciones para 2 canales: cripto, forex, materias_primas, acciones)",
    ]
    if DIRECCION_PAGO_USDT:
        lineas += ["", f"💰 Dirección de depósito (USDT, red {RED_PAGO_USDT}):", DIRECCION_PAGO_USDT]
    lineas += ["", "Una vez que hagas el depósito, el sistema lo detecta automáticamente "
                    "y el administrador te da acceso a la brevedad."]
    return "\n".join(lineas)


def procesar_comando_comprar(args: List[str], username: Optional[str], telegram_id: Optional[int],
                              nombre: str, chat_id) -> None:
    if not username:
        enviar_mensaje(
            chat_id,
            "Necesito que tengas un @usuario configurado en Telegram para poder registrarte. "
            "Configuralo en Ajustes de Telegram y volvé a intentar.",
        )
        return

    if not args:
        enviar_mensaje(chat_id, texto_precios())
        return

    if len(args) == 1 and args[0] == "4":
        columnas = {c: True for c in CANALES_VALIDOS.values()}
        monto = PRECIO_POR_PLAN["4"]
        etiqueta_plan = "los 4 canales"
    else:
        elegidos = [a.lower() for a in args if a.lower() in CANALES_VALIDOS]
        if len(elegidos) != 2:
            enviar_mensaje(
                chat_id,
                "Para el plan de 2 canales, decime exactamente 2 (por ejemplo: /comprar cripto forex).\n\n"
                + texto_precios(),
            )
            return
        columnas = {columna: (clave in elegidos) for clave, columna in CANALES_VALIDOS.items()}
        monto = PRECIO_POR_PLAN["2"]
        etiqueta_plan = " y ".join(elegidos)

    try:
        cliente = buscar_cliente_por_usuario(username)
        if not cliente:
            cliente = crear_cliente(nombre or f"@{username}", username, telegram_id)
        elif telegram_id and cliente.get("telegram_id") != telegram_id:
            guardar_telegram_id(cliente["id"], telegram_id)

        if existe_pago_pendiente(cliente["id"]):
            enviar_mensaje(
                chat_id,
                "Ya tenés un pedido pendiente de confirmar. Hacé el depósito si todavía no lo hiciste, "
                "y esperá a que el administrador te confirme el acceso.",
            )
            return

        hoy = datetime.now(timezone.utc).date().isoformat()
        crear_suscripcion_pendiente(cliente["id"], columnas, monto, hoy)
        crear_pago_pendiente(cliente["id"], monto, hoy)
    except Exception as exc:
        print(f"[Error] No se pudo registrar el pedido de /comprar (@{username}): {type(exc).__name__}: {exc}")
        enviar_mensaje(chat_id, "Hubo un problema registrando tu pedido. Probá de nuevo en un rato, o contactá al administrador.")
        return

    print(f"[Comprar] Pedido registrado: @{username} -> {etiqueta_plan} ({monto} USDT).")
    lineas = [f"✅ Registré tu pedido: {etiqueta_plan} — {monto} USDT/mes."]
    if DIRECCION_PAGO_USDT:
        lineas += ["", f"💰 Depositá en esta dirección (USDT, red {RED_PAGO_USDT}):", DIRECCION_PAGO_USDT]
    lineas += ["", "Una vez que hagas el depósito, el sistema lo va a detectar automáticamente y "
                    "el administrador te va a dar acceso a la brevedad."]
    enviar_mensaje(chat_id, "\n".join(lineas))


# ==============================================================================
# 2. TELEGRAM — helpers de envío
# ==============================================================================
def enviar_mensaje(chat_id, texto: str, parse_mode: Optional[str] = None) -> None:
    payload = {"chat_id": chat_id, "text": texto}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=15)
        result = resp.json()
        if not result.get("ok"):
            print(f"[Error enviando mensaje a {chat_id}] {result}")
        else:
            print(f"[Mensaje enviado OK] a chat_id={chat_id}")
    except Exception as exc:
        print(f"[Error enviando mensaje a {chat_id}] {type(exc).__name__}: {exc}")


# ==============================================================================
# 3. PUNTO 6 — comando /estado
# ==============================================================================
def responder_estado(username: Optional[str], chat_id_respuesta, telegram_id: Optional[int] = None) -> None:
    if not username:
        enviar_mensaje(
            chat_id_respuesta,
            "No pude identificarte porque tu cuenta de Telegram no tiene un @usuario "
            "configurado. Configurá uno en Ajustes de Telegram y volvé a intentar.",
        )
        return

    try:
        cliente = buscar_cliente_por_usuario(username)
    except Exception as exc:
        print(f"[Error] No se pudo consultar Supabase para /estado: {exc}")
        enviar_mensaje(chat_id_respuesta, "Hubo un problema consultando tu suscripción. Probá de nuevo en un rato.")
        return

    print(f"[Comando /estado] Cliente encontrado para @{username}: {cliente.get('nombre') if cliente else 'NINGUNO'}")

    if not cliente:
        enviar_mensaje(
            chat_id_respuesta,
            "No encontré ninguna suscripción registrada con tu usuario de Telegram. "
            "Si creés que es un error, contactá al administrador.",
        )
        return

    if telegram_id and cliente.get("telegram_id") != telegram_id:
        guardar_telegram_id(cliente["id"], telegram_id)

    suscripciones = cliente.get("suscripciones") or []
    if not suscripciones:
        enviar_mensaje(
            chat_id_respuesta,
            f"Hola {cliente.get('nombre', '')} 👋\n\n"
            f"Estás registrado/a, pero todavía no tenés ninguna suscripción cargada. "
            f"Contactá al administrador.",
        )
        return

    sub = sorted(suscripciones, key=lambda s: s.get("creado_en", ""), reverse=True)[0]
    canales = [nombre for nombre, columna in BOT_A_COLUMNA.items() if sub.get(columna)]

    hoy = datetime.now(timezone.utc).date()
    venc = datetime.strptime(sub["fecha_vencimiento"], "%Y-%m-%d").date()
    dias = (venc - hoy).days

    if dias >= 0 and sub.get("estado") == "activa":
        estado_texto = "✅ Activa"
        dias_texto = "vence hoy" if dias == 0 else f"vence en {dias} día{'s' if dias != 1 else ''}"
    else:
        estado_texto = "🔴 Vencida"
        dias_texto = f"venció hace {abs(dias)} día{'s' if abs(dias) != 1 else ''}"

    texto = (
        f"Hola {cliente.get('nombre', '')} 👋\n\n"
        f"Estado de tu suscripción: {estado_texto}\n"
        f"Canales incluidos: {', '.join(canales) if canales else '(ninguno asignado)'}\n"
        f"Vencimiento: {venc.strftime('%d/%m/%Y')} ({dias_texto})"
    )
    enviar_mensaje(chat_id_respuesta, texto)


def procesar_mensaje(mensaje: dict) -> None:
    chat = mensaje.get("chat", {})
    texto_original = mensaje.get("text") or ""
    username_debug = mensaje.get("from", {}).get("username")
    print(f"[Mensaje recibido] chat_type={chat.get('type')} de=@{username_debug} texto={texto_original!r}")

    if chat.get("type") != "private":
        print("[Mensaje ignorado] no es un chat privado.")
        return  # estos comandos solo funcionan en el chat privado con el bot

    texto = texto_original.strip()
    texto_lower = texto.lower()
    telegram_id = mensaje.get("from", {}).get("id")

    if texto_lower.startswith("/estado"):
        print(f"[Comando /estado] Procesando pedido de @{username_debug}...")
        responder_estado(username_debug, chat["id"], telegram_id)
    elif texto_lower.startswith("/precios"):
        print(f"[Comando /precios] Consulta de @{username_debug}.")
        enviar_mensaje(chat["id"], texto_precios())
    elif texto_lower.startswith("/comprar"):
        print(f"[Comando /comprar] Pedido de @{username_debug}: {texto!r}")
        args = texto.split()[1:]
        nombre = mensaje.get("from", {}).get("first_name") or ""
        procesar_comando_comprar(args, username_debug, telegram_id, nombre, chat["id"])
    else:
        print("[Mensaje ignorado] no es un comando reconocido (/estado, /precios, /comprar).")


# ==============================================================================
# 4. PUNTO 2 — bienvenida automática al unirse a un canal
# ==============================================================================
def procesar_chat_member(evento: dict) -> None:
    old_status = evento.get("old_chat_member", {}).get("status")
    new_status = evento.get("new_chat_member", {}).get("status")

    # Solo nos interesa el momento en que alguien PASA a ser miembro (no
    # cambios de admin, ni gente que ya era miembro y cambió de estado).
    si_se_unio_ahora = old_status in ("left", "kicked", "restricted") and new_status == "member"
    if not si_se_unio_ahora:
        return

    chat_id = str(evento["chat"]["id"])
    bot_nombre = CHAT_ID_A_BOT.get(chat_id, "Ridgecrest")
    usuario = evento["new_chat_member"]["user"]
    nombre = usuario.get("first_name") or usuario.get("username") or "nuevo suscriptor"
    mencion = f'<a href="tg://user?id={usuario["id"]}">{escape_html(nombre)}</a>'

    # Si el @usuario de quien se unió coincide con un cliente ya registrado,
    # aprovechamos para guardar su ID numérico (lo vamos a necesitar más
    # adelante si hace falta sacarlo del canal por falta de pago).
    if usuario.get("username"):
        try:
            cliente = buscar_cliente_por_usuario(usuario["username"])
            if cliente and cliente.get("telegram_id") != usuario["id"]:
                guardar_telegram_id(cliente["id"], usuario["id"])
        except Exception as exc:
            print(f"[Aviso] No se pudo verificar/guardar telegram_id al unirse: {exc}")

    lineas = [
        f"👋 ¡Bienvenido/a {mencion}!",
        "",
        f"Acá vas a recibir las señales del bot de {bot_nombre}: un mensaje de texto "
        f"con el análisis técnico completo, y un audio explicando la misma señal en "
        f"lenguaje simple.",
        "",
        "Escribile \"/estado\" a este bot en un chat privado (no acá en el canal) "
        "en cualquier momento para ver cuándo vence tu suscripción.",
    ]
    if TERMINOS_URL:
        lineas += ["", f"📄 Términos y condiciones: {TERMINOS_URL}"]

    enviar_mensaje(chat_id, "\n".join(lineas), parse_mode="HTML")
    print(f"[Bienvenida] Nuevo miembro en {bot_nombre}: {nombre}")


# ==============================================================================
# 5. CICLO PRINCIPAL — long polling de Telegram
# ==============================================================================
def obtener_offset_inicial() -> Optional[int]:
    """Al arrancar, salta cualquier mensaje viejo que haya quedado sin
    procesar (por ejemplo de mientras el servicio estaba apagado o
    redesplegando), para no reprocesar cosas atrasadas."""
    try:
        resp = requests.get(f"{TELEGRAM_API}/getUpdates", params={"timeout": 0}, timeout=15)
        updates = resp.json().get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception as exc:
        print(f"[Aviso] No se pudo obtener el offset inicial: {exc}")
    return None


def escuchar_telegram() -> None:
    offset = obtener_offset_inicial()
    print("[Listener] Escuchando actualizaciones de Telegram...", flush=True)

    while True:
        try:
            params = {"timeout": 25, "allowed_updates": '["message","chat_member"]'}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
            data = resp.json()

            if not data.get("ok"):
                print(f"[Error] getUpdates devolvió un error: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "message" in update:
                        procesar_mensaje(update["message"])
                    if "chat_member" in update:
                        procesar_chat_member(update["chat_member"])
                except Exception as exc:
                    print(f"[Error] Procesando update {update.get('update_id')}: {exc}")

        except requests.exceptions.RequestException as exc:
            print(f"[Aviso] Error de red consultando Telegram: {exc}. Reintentando en 5s...")
            time.sleep(5)
        except Exception as exc:
            print(f"[Error inesperado en el loop principal] {exc}")
            time.sleep(5)


def main():
    faltantes = [
        nombre for nombre, valor in [
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY),
            ("TELEGRAM_BOT_TOKEN_CANALES", TELEGRAM_BOT_TOKEN),
        ] if not valor
    ]
    if faltantes:
        print(f"Faltan variables de entorno obligatorias: {', '.join(faltantes)}")
        sys.exit(1)

    if not CHAT_ID_A_BOT:
        print("[Aviso] No hay ningún TELEGRAM_CHAT_ID_* configurado — las bienvenidas "
              "automáticas no van a poder identificar de qué bot es cada canal, pero "
              "el comando /estado sigue funcionando igual.")

    escuchar_telegram()


if __name__ == "__main__":
    main()
