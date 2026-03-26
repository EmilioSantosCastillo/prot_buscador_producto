# -*- coding: utf-8 -*-
"""
Bot de búsqueda de productos por imagen
Stack : Telegram + SerpAPI Google Lens + DeepSeek (agente) + ImgBB
Flujo : imagen → Lens → agente refina → Lens con query → respuesta
Memoria: context.user_data (por usuario, RAM, solo sesión activa)
Formato: HTML para resultados (robusto con URLs y caracteres especiales)
"""

import base64
import glob
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
os.environ["PYTHONIOENCODING"] = "utf-8"

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DEEPSEEK_KEY   = os.environ.get("DEEPSEEK_KEY", "")
SERPAPI_KEY    = os.environ.get("SERPAPI_KEY", "")
IMGBB_KEY      = os.environ.get("IMGBB_KEY", "")

MAX_PREGUNTAS = 3      # máximo de preguntas antes de forzar búsqueda
GUARDAR_JSON  = True   # False para desactivar guardado de debug

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    encoding="utf-8",
)
log = logging.getLogger(__name__)

# ─── CLIENTE DEEPSEEK ─────────────────────────────────────────────────────────
deepseek = OpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com",
)

# ─── PALABRAS QUE RESETEAN LA CONVERSACIÓN ────────────────────────────────────
PALABRAS_RESET = {
    "olvida", "cancelar", "cancel", "reset",
    "reiniciar", "da igual", "no importa",
}


# ══════════════════════════════════════════════════════════════════════════════
#  UTILIDADES
# ══════════════════════════════════════════════════════════════════════════════

def esc_md(text: str) -> str:
    """Escapa caracteres especiales para Telegram MarkdownV2.
    Usado solo para mensajes del agente (texto controlado, sin URLs externas).
    """
    if not text:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def esc_html(text: str) -> str:
    """Escapa caracteres especiales para HTML de Telegram.
    Usado para resultados de APIs externas (títulos, precios, fuentes).
    """
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def reset_contexto(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    log.info("Contexto reseteado")


# ══════════════════════════════════════════════════════════════════════════════
#  IMGBB
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_imgbb(image_bytes: bytes, retries: int = 3) -> str:
    """Sube imagen a ImgBB con retry exponencial. Retorna URL pública."""
    for attempt in range(retries):
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            r = requests.post(
                "https://api.imgbb.com/1/upload",
                data={"key": IMGBB_KEY, "image": b64, "expiration": 600},
                timeout=20,
            )
            r.raise_for_status()
            url = r.json()["data"]["url"]
            log.info(f"ImgBB OK: {url[:60]}...")
            return url
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            log.warning(f"ImgBB intento {attempt + 1} fallido: {e}. Reintento en {wait}s")
            time.sleep(wait)


# ══════════════════════════════════════════════════════════════════════════════
#  SERPAPI — GOOGLE LENS
# ══════════════════════════════════════════════════════════════════════════════

def serpapi_lens(image_url: str, query_ctx: str = "") -> dict:
    """Busca por imagen con Google Lens vía SerpAPI."""
    params = {
        "api_key": SERPAPI_KEY,
        "engine":  "google_lens",
        "url":     image_url,
        "hl":      "es",
        "gl":      "cl",
    }
    if query_ctx:
        params["q"] = query_ctx

    log.info(f"SerpAPI Lens — query: '{query_ctx or 'sin texto'}'")
    try:
        r = requests.get("https://serpapi.com/search", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Error SerpAPI: {e}")
        return {"error": str(e)}


def extraer_contexto_lens(lens_data: dict) -> str:
    """
    Extrae un resumen estructurado del JSON de Lens para el agente.
    Retorna string JSON compacto con solo la info útil.
    """
    if "error" in lens_data:
        return json.dumps({"error": "Lens no respondió"}, ensure_ascii=False)

    try:
        visual   = lens_data.get("visual_matches", [])
        shopping = lens_data.get("shopping_results", [])

        # Títulos únicos (máx 10)
        titulos = []
        seen    = set()
        for item in (visual + shopping)[:15]:
            t = item.get("title", "").strip()
            if t and t not in seen:
                seen.add(t)
                titulos.append(t[:80])
            if len(titulos) >= 10:
                break

        # Marcas detectadas
        marcas_conocidas = [
            "apple", "samsung", "huawei", "xiaomi", "sony", "lg", "canon",
            "epson", "hp", "dell", "lenovo", "asus", "acer", "msi",
            "nike", "adidas", "puma", "reebok", "anker", "ugreen", "baseus",
        ]
        texto_plano = " ".join(titulos).lower()
        marcas = [m.title() for m in marcas_conocidas if m in texto_plano]

        # Precios
        precios = []
        for item in (visual + shopping)[:8]:
            p = item.get("price")
            if isinstance(p, dict) and p.get("extracted_value"):
                moneda = p.get("currency", "")
                precios.append(f"{moneda}{p['extracted_value']}")
            elif isinstance(p, str) and p:
                precios.append(p)

        info = {
            "total_resultados":   len(visual) + len(shopping),
            "titulos_encontrados": titulos,
            "marcas_detectadas":   marcas[:5],
            "precios_referencia":  precios[:5],
        }
        return json.dumps(info, ensure_ascii=False)

    except Exception as e:
        log.error(f"extraer_contexto_lens error: {e}")
        return json.dumps({"total_resultados": 0, "titulos_encontrados": []}, ensure_ascii=False)


def analizar_estadisticas_lens(lens_data: dict) -> dict:
    """Estadísticas básicas del resultado de Lens para logs y /stats."""
    stats = {
        "total_resultados": 0,
        "visual_matches": 0,
        "shopping_results": 0,
        "marcas_detectadas": [],
        "rango_precios": "",
        "tiendas_encontradas": [],
        "categorias_principales": [],
    }
    try:
        visual   = lens_data.get("visual_matches", [])
        shopping = lens_data.get("shopping_results", [])
        stats["visual_matches"]   = len(visual)
        stats["shopping_results"] = len(shopping)
        stats["total_resultados"] = len(visual) + len(shopping)

        texto_completo = " ".join(
            (item.get("title", "") + " " + item.get("source", "")).lower()
            for item in (visual + shopping)[:10]
        )

        for marca in ["nike", "adidas", "apple", "samsung", "canon", "epson",
                      "hp", "anker", "ugreen", "huawei", "xiaomi", "sony", "lg",
                      "lenovo", "dell"]:
            if marca in texto_completo:
                stats["marcas_detectadas"].append(marca.title())

        precios = []
        for item in (visual + shopping)[:8]:
            p = item.get("price")
            if isinstance(p, dict) and p.get("extracted_value"):
                precios.append(float(p["extracted_value"]))
        if precios:
            stats["rango_precios"] = f"${min(precios):.2f} - ${max(precios):.2f}"

        tiendas = {item.get("source") for item in (visual + shopping)[:10] if item.get("source")}
        stats["tiendas_encontradas"] = list(tiendas)[:5]

        categorias_kw = {
            "electrónica":  ["usb", "hub", "cable", "adaptador", "cargador", "hdmi"],
            "informática":  ["laptop", "portátil", "pc", "computador", "tablet", "monitor"],
            "telefonía":    ["teléfono", "smartphone", "iphone", "galaxy"],
            "calzado":      ["zapato", "zapatilla", "tenis", "sneaker"],
            "ropa":         ["camiseta", "polera", "pantalón", "jeans", "chaqueta"],
            "impresoras":   ["impresora", "printer", "toner", "cartucho", "megatank"],
        }
        for cat, kws in categorias_kw.items():
            if any(k in texto_completo for k in kws):
                stats["categorias_principales"].append(cat)

    except Exception as e:
        log.error(f"analizar_estadisticas_lens error: {e}")
    return stats


def guardar_json_debug(lens_data: dict, user_id: int, tag: str = "") -> None:
    """Guarda el JSON completo de Lens en disco para debugging."""
    if not GUARDAR_JSON:
        return
    try:
        os.makedirs("lens_json", exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_tag = re.sub(r"[^\w\-]", "_", tag)[:30]
        path     = f"lens_json/lens_{user_id}_{ts}_{safe_tag}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lens_data, f, ensure_ascii=False, indent=2, default=str)
        log.info(f"JSON debug guardado: {path}")
    except Exception as e:
        log.error(f"Error guardando JSON debug: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  DEEPSEEK — AGENTE DE COMPRA
# ══════════════════════════════════════════════════════════════════════════════

def deepseek_agente(
    lens_info: str,
    history: list,
    user_name: str,
    preguntas_hechas: int,
) -> dict:
    """
    Agente conversacional enfocado ÚNICAMENTE en compra.
    Evalúa la calidad de cada respuesta del usuario para decidir
    si preguntar de nuevo o lanzar la búsqueda final.
    Retorna dict: { action, message, query }
    """
    forzar       = preguntas_hechas >= MAX_PREGUNTAS
    primer_turno = len(history) == 0

    system = f"""Eres un asistente especializado ÚNICAMENTE en buscar productos para comprar online.

RESULTADOS DE GOOGLE LENS (búsqueda por imagen):
{lens_info}

CONVERSANDO CON: {user_name}

TU ÚNICO OBJETIVO: construir el mejor query de búsqueda para encontrar este producto en tiendas online con precio y link de compra.

{"⚠️ Ya hiciste suficientes preguntas. DEBES construir el query y buscar AHORA sin excepción." if forzar else f"Preguntas hechas hasta ahora: {preguntas_hechas}/{MAX_PREGUNTAS}."}

RESPONDE SIEMPRE CON ESTE JSON EXACTO (sin markdown, sin texto extra):
{{
  "action": "preguntar" o "buscar",
  "message": "texto natural y breve para el usuario",
  "query": "keywords de búsqueda en español (obligatorio si action es buscar, vacío si es preguntar)"
}}

REGLAS ESTRICTAS — NUNCA las violes:
1. NUNCA preguntes sobre soporte técnico, configuración, instalación, drivers ni uso del producto
2. NUNCA ofrezcas ayuda con problemas o fallas del producto
3. {"Estás en el PRIMER turno — SIEMPRE haz UNA pregunta para afinar la compra antes de buscar." if primer_turno else ""}
4. En turnos siguientes evalúa la calidad de la última respuesta del usuario:
   - Si fue CLARA y específica (ej: "nuevo", "menos de 50 mil", "talla 42", nombre de tienda) → BUSCA YA
   - Si fue AMBIGUA o incompleta (ej: "algo barato", "no sé", "el normal") → haz UNA pregunta más para aclarar
   - Si el usuario dice que no importa, da igual, cualquiera → BUSCA con lo que tengas
5. TUS PREGUNTAS son SOLO para afinar la compra:
   - modelo específico si Lens detectó varios
   - nuevo, reacondicionado o segunda mano
   - presupuesto máximo o rango de precio
   - tienda o país preferido
6. El query debe ser concreto: "Canon MegaTank G3272 impresora nueva" no "impresora"
7. Máximo UNA pregunta por turno, breve y directa
8. Responde siempre en español
"""

    messages = [{"role": "system", "content": system}] + history

    log.info(f"DeepSeek agente — preguntas={preguntas_hechas}, primer_turno={primer_turno}, forzar={forzar}")

    resp = deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        max_tokens=400,
        response_format={"type": "json_object"},
    )

    raw = resp.choices[0].message.content.strip()
    log.info(f"DeepSeek raw: {raw}")

    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"JSON parse error: {e} — raw: {raw}")
        return {"action": "buscar", "message": "Buscando productos similares...", "query": ""}


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATEADOR DE RESULTADOS — HTML
# ══════════════════════════════════════════════════════════════════════════════

def format_resultados(data: dict, query_usado: str) -> str:
    """
    Formatea resultados para Telegram usando HTML.
    HTML es más robusto que MarkdownV2 con URLs y caracteres especiales
    provenientes de APIs externas.
    """
    if "error" in data:
        return f"❌ Error en la búsqueda: {esc_html(data['error'])}"

    visual     = data.get("visual_matches", [])
    shopping   = data.get("shopping_results", [])
    con_precio = [m for m in visual if m.get("price")]

    # Prioridad: visual con precio → shopping → visual sin precio
    todos = []
    seen_links = set()
    for item in (con_precio + shopping + visual):
        link = item.get("link", "")
        key  = link or item.get("title", "")
        if key and key not in seen_links:
            seen_links.add(key)
            todos.append(item)

    if not todos:
        return "😕 No encontré productos similares. Intenta con otra foto más clara."

    lines = []
    if query_usado:
        lines.append(f"🔍 <b>Búsqueda:</b> <i>{esc_html(query_usado)}</i>\n")

    lines.append(f"🛒 <b>{min(len(todos), 5)} resultados encontrados:</b>\n")

    for i, item in enumerate(todos[:5], 1):
        titulo = esc_html(str(item.get("title", "Producto"))[:70])
        link   = item.get("link", "")
        fuente = esc_html(item.get("source", ""))

        precio_raw = item.get("price", {})
        if isinstance(precio_raw, dict):
            valor  = precio_raw.get("extracted_value", "")
            moneda = precio_raw.get("currency", "")
            precio = f"{moneda}{valor}" if valor else ""
        else:
            precio = esc_html(str(precio_raw)) if precio_raw else ""

        # Título con o sin link
        if link:
            lines.append(f'{i}. <a href="{link}">{titulo}</a>')
        else:
            lines.append(f"{i}. {titulo}")

        # Detalles: precio y fuente
        detalles = []
        if precio:
            detalles.append(f"💰 {esc_html(precio)}")
        if fuente:
            detalles.append(f"🏪 <i>{fuente}</i>")
        if detalles:
            lines.append("   " + " · ".join(detalles))

        lines.append("")

    return "\n".join(lines)


def get_thumbnail(data: dict) -> str | None:
    """Retorna la mejor URL de imagen de los resultados."""
    for section in ["visual_matches", "shopping_results"]:
        for item in data.get(section, [])[:5]:
            url = item.get("image") or item.get("thumbnail")
            if url and url.startswith("http"):
                return url
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

async def procesar_nueva_foto(
    image_bytes: bytes,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Flujo completo para foto nueva:
    1. Subir a ImgBB → URL pública
    2. Buscar en Google Lens (búsqueda inicial)
    3. Extraer contexto de resultados para el agente
    4. Agente decide si preguntar o buscar directamente
    """
    user = update.effective_user
    msg  = await update.message.reply_text("⏳ Procesando imagen...")

    try:
        # 1. ImgBB
        await msg.edit_text("📤 Subiendo imagen...")
        image_url = upload_to_imgbb(image_bytes)

        # 2. Google Lens — búsqueda inicial
        await msg.edit_text("🔍 Buscando en Google Lens...")
        lens_raw = serpapi_lens(image_url)

        if "error" in lens_raw:
            await msg.edit_text(f"❌ Error en Google Lens: {lens_raw['error']}")
            return

        guardar_json_debug(lens_raw, user.id, "inicial")
        stats = analizar_estadisticas_lens(lens_raw)
        log.info(f"Estadísticas Lens: {stats}")

        # 3. Extraer contexto estructurado para el agente
        lens_info = extraer_contexto_lens(lens_raw)
        log.info(f"Contexto para agente: {lens_info[:200]}...")

        # 4. Inicializar memoria del usuario
        reset_contexto(context)
        context.user_data.update({
            "image_url":        image_url,
            "lens_raw":         lens_raw,
            "lens_info":        lens_info,
            "stats":            stats,
            "history":          [],
            "preguntas_hechas": 0,
        })

        # 5. Primera decisión del agente
        await msg.edit_text("🤔 Analizando resultados...")
        decision = deepseek_agente(
            lens_info,
            context.user_data["history"],
            user.first_name,
            context.user_data["preguntas_hechas"],
        )

        await msg.delete()
        await ejecutar_decision(decision, update, context)

    except Exception as e:
        log.exception(f"procesar_nueva_foto error: {e}")
        try:
            await msg.edit_text("😕 Ocurrió un problema. Intenta con otra foto.")
        except Exception:
            await update.message.reply_text("😕 Ocurrió un problema. Intenta con otra foto.")


async def procesar_respuesta_usuario(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Maneja texto del usuario cuando hay una conversación activa."""
    user  = update.effective_user
    texto = update.message.text.strip()

    # Detectar palabras de reset
    if any(p in texto.lower() for p in PALABRAS_RESET):
        reset_contexto(context)
        await update.message.reply_text(
            "🔄 Contexto limpiado. Manda una nueva foto cuando quieras."
        )
        return

    # Agregar turno al historial
    context.user_data["history"].append({"role": "user", "content": texto})
    msg = await update.message.reply_text("🤔 Pensando...")

    try:
        decision = deepseek_agente(
            context.user_data["lens_info"],
            context.user_data["history"],
            user.first_name,
            context.user_data.get("preguntas_hechas", 0),
        )
        await msg.delete()
        await ejecutar_decision(decision, update, context)

    except Exception as e:
        log.exception(f"procesar_respuesta_usuario error: {e}")
        # Usar reply_text — msg puede ya estar borrado
        try:
            await msg.delete()
        except Exception:
            pass
        await update.message.reply_text("😕 Ocurrió un problema. Intenta de nuevo.")


async def ejecutar_decision(
    decision: dict,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Ejecuta la decisión del agente: preguntar o lanzar búsqueda final."""
    action  = decision.get("action", "buscar")
    message = decision.get("message", "")
    query   = decision.get("query", "")

    log.info(f"ejecutar_decision — action={action}, query='{query}'")

    # ── PREGUNTAR ─────────────────────────────────────────────────────────────
    if action == "preguntar":
        context.user_data["preguntas_hechas"] = (
            context.user_data.get("preguntas_hechas", 0) + 1
        )
        context.user_data["history"].append({"role": "assistant", "content": message})
        log.info(f"Agente pregunta #{context.user_data['preguntas_hechas']}: {message}")

        # Mensaje del agente: texto plano (sin parse_mode) para evitar problemas
        # con caracteres especiales que DeepSeek pueda generar
        await update.message.reply_text(message)
        return

    # ── BUSCAR ────────────────────────────────────────────────────────────────
    buscando = await update.message.reply_text("🔎 Buscando productos...")

    try:
        user = update.effective_user

        if query:
            log.info(f"Segunda búsqueda Lens con query: '{query}'")
            data = serpapi_lens(context.user_data["image_url"], query)
            guardar_json_debug(data, user.id, query)
        else:
            log.info("Usando resultados iniciales de Lens")
            data = context.user_data["lens_raw"]

        texto = format_resultados(data, query)
        thumb = get_thumbnail(data)

        log.info(
            f"Resultados finales — visual: {len(data.get('visual_matches', []))}, "
            f"shopping: {len(data.get('shopping_results', []))}"
        )

        await buscando.delete()

        # Mensaje previo del agente si es informativo (texto plano)
        msg_lower = message.lower().strip()
        if message and msg_lower not in ("buscando...", "buscar", ""):
            await update.message.reply_text(message)

        # Enviar resultados en HTML
        if thumb:
            try:
                await update.message.reply_photo(
                    photo=thumb,
                    caption=texto,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning(f"reply_photo falló: {e}")
                await update.message.reply_text(
                    texto,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        else:
            await update.message.reply_text(
                texto,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        reset_contexto(context)

    except Exception as e:
        log.exception(f"ejecutar_decision (buscar) error: {e}")
        # reply_text nuevo — buscando puede ya estar borrado
        try:
            await buscando.delete()
        except Exception:
            pass
        await update.message.reply_text("😕 Error al buscar. Intenta de nuevo.")


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS DE TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        photo       = update.message.photo[-1]
        file        = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        await procesar_nueva_foto(image_bytes, update, context)
    except Exception as e:
        log.exception(f"handle_photo error: {e}")
        await update.message.reply_text("😕 Error al procesar la foto.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        try:
            file        = await context.bot.get_file(doc.file_id)
            image_bytes = bytes(await file.download_as_bytearray())
            await procesar_nueva_foto(image_bytes, update, context)
        except Exception as e:
            log.exception(f"handle_document error: {e}")
            await update.message.reply_text("😕 Error al procesar el archivo.")
    else:
        await update.message.reply_text("Por favor envía una imagen (JPG, PNG, WEBP).")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("image_url"):
        await procesar_respuesta_usuario(update, context)
    else:
        await update.message.reply_text(
            "📸 Mándame una foto del producto que quieres buscar."
        )


# ─── COMANDOS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Hola {nombre}! Soy tu buscador de productos.\n\n"
        "📸 Mándame una foto y te ayudo a encontrar el producto "
        "con precio y link de compra.\n\n"
        "💬 Te haré un par de preguntas para afinar la búsqueda.\n\n"
        "Comandos disponibles:\n"
        "/reset — limpiar conversación activa\n"
        "/status — ver estado de tu sesión\n"
        "/jsons — archivos de debug guardados\n\n"
        "Escribe cancelar en cualquier momento para reiniciar."
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_contexto(context)
    await update.message.reply_text(
        "🔄 Contexto limpiado. Manda una nueva foto cuando quieras."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug: muestra estado de memoria del usuario activo."""
    if not context.user_data:
        await update.message.reply_text("Sin sesión activa.")
        return

    preguntas = context.user_data.get("preguntas_hechas", 0)
    turnos    = len(context.user_data.get("history", []))
    tiene_img = bool(context.user_data.get("image_url"))
    stats     = context.user_data.get("stats", {})

    lineas = [
        f"imagen cargada: {'sí' if tiene_img else 'no'}",
        f"preguntas: {preguntas}/{MAX_PREGUNTAS}",
        f"turnos en historial: {turnos}",
    ]
    if stats.get("total_resultados"):
        lineas.append(f"resultados Lens iniciales: {stats['total_resultados']}")
    if stats.get("marcas_detectadas"):
        lineas.append(f"marcas detectadas: {', '.join(stats['marcas_detectadas'])}")

    await update.message.reply_text("\n".join(lineas))


async def cmd_jsons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra info de los archivos JSON de debug guardados."""
    archivos = glob.glob("lens_json/*.json")
    if not archivos:
        await update.message.reply_text("📁 No hay archivos JSON guardados aún.")
        return

    total_kb = sum(os.path.getsize(f) for f in archivos) / 1024
    await update.message.reply_text(
        f"📊 JSON debug:\n"
        f"• Archivos: {len(archivos)}\n"
        f"• Espacio: {total_kb:.1f} KB\n"
        f"• Directorio: lens_json/"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Validar variables de entorno
    required = ["TELEGRAM_TOKEN", "DEEPSEEK_KEY", "SERPAPI_KEY", "IMGBB_KEY"]
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        print("❌ Faltan variables de entorno:")
        for k in missing:
            print(f"   {k}")
        print("\nCrea un archivo .env con esas variables y vuelve a intentar.")
        sys.exit(1)

    os.makedirs("lens_json", exist_ok=True)

    log.info("Iniciando bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("jsons",  cmd_jsons))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE,          handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot corriendo. Ctrl+C para detener.")
    app.run_polling(drop_pending_updates=True)