# -*- coding: utf-8 -*-

import logging
import requests
import base64
import sys
import os
import re

os.environ["PYTHONIOENCODING"] = "utf-8"

from serpapi import GoogleSearch
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ─── CONFIGURACIÓN ────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SERPAPI_KEY    = os.environ["SERPAPI_KEY"]
IMGBB_KEY      = os.environ["IMGBB_KEY"]
# ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    encoding="utf-8"
)

# ─── ESCAPE MARKDOWN ──────────────────────────────────────────────
def esc(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([*_`\[])', r'\\\1', str(text))

# ─── SUBIR IMAGEN A IMGBB ─────────────────────────────────────────
def upload_to_imgbb(image_bytes: bytes) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    r = requests.post(
        "https://api.imgbb.com/1/upload",
        data={
            "key":        IMGBB_KEY,
            "image":      image_b64,
            "expiration": 300,
        },
        timeout=20
    )
    r.raise_for_status()
    url = r.json()["data"]["url"]
    logging.info(f"Imagen subida a imgbb: {url}")
    return url

# ─── GOOGLE LENS vía SerpAPI ──────────────────────────────────────
def search_by_image_url(public_url: str) -> dict:
    params = {
        "api_key": SERPAPI_KEY,
        "engine":  "google_lens",
        "url":     public_url,
        "hl":      "es",
        "country": "cl",
    }
    search = GoogleSearch(params)
    return search.get_dict()

# ─── OBTENER MEJOR IMAGEN ─────────────────────────────────────────
def get_best_image_url(data: dict) -> str | None:
    """Obtiene la URL de la mejor imagen del resultado."""
    # Primero intentar knowledge_graph
    knowledge = data.get("knowledge_graph", [])
    if knowledge and knowledge[0].get("images"):
        img = knowledge[0]["images"][0].get("source")
        if img:
            return img

    # Luego visual_matches con precio
    visual_matches = data.get("visual_matches", [])
    with_price = [m for m in visual_matches if m.get("price")]
    candidates = with_price or visual_matches

    for match in candidates[:5]:
        thumbnail = match.get("thumbnail")
        if thumbnail and thumbnail.startswith("http"):
            return thumbnail

    return None

# ─── FORMATEAR RESPUESTA ──────────────────────────────────────────
def format_response(data: dict) -> str:
    lines = []

    if data.get("error"):
        return f"❌ Error de búsqueda: {esc(data['error'])}"

    knowledge      = data.get("knowledge_graph", [])
    titulo_lens    = knowledge[0].get("title", "") if knowledge else ""
    visual_matches = data.get("visual_matches", [])
    with_price     = [m for m in visual_matches if m.get("price")]
    shopping       = data.get("shopping_results", [])

    if titulo_lens:
        lines.append(f"🔍 *Producto:* _{esc(titulo_lens)}_")
    elif with_price:
        lines.append(f"🔍 *Producto:* _{esc(with_price[0].get('title', 'Desconocido'))}_")
    elif visual_matches:
        lines.append(f"🔍 *Producto:* _{esc(visual_matches[0].get('title', 'Desconocido'))}_")
    else:
        return "😕 No encontré productos similares. Intenta con una foto más clara."

    lines.append("")

    if with_price:
        lines.append("🛒 *Coincidencias con precio:*")
        lines.append("")
        for m in with_price[:5]:
            titulo = esc(m.get("title", "Sin nombre")[:55])
            precio = m.get("price", {})
            valor  = precio.get("extracted_value", "") if isinstance(precio, dict) else precio
            moneda = precio.get("currency", "")         if isinstance(precio, dict) else ""
            tienda = esc(m.get("source", "Tienda desconocida"))
            link   = m.get("link", "")
            precio_str = f"{moneda}{valor}" if valor else "Ver precio en tienda"
            if link:
                lines.append(f"*[{titulo}]({link})*")
            else:
                lines.append(f"*{titulo}*")
            lines.append(f"💰 {precio_str} — _{tienda}_")
            lines.append("")

    elif shopping:
        lines.append("🛒 *Resultados en tiendas:*")
        lines.append("")
        for p in shopping[:5]:
            titulo = esc(p.get("title", "Sin nombre")[:55])
            precio = esc(p.get("price", "Ver precio en tienda"))
            tienda = esc(p.get("source", "Tienda desconocida"))
            link   = p.get("link", "")
            if link:
                lines.append(f"*[{titulo}]({link})*")
            else:
                lines.append(f"*{titulo}*")
            lines.append(f"💰 {precio} — _{tienda}_")
            lines.append("")

    elif visual_matches:
        lines.append("🔗 *Productos similares:*")
        lines.append("")
        for m in visual_matches[:5]:
            titulo = esc(m.get("title", "Sin nombre")[:55])
            tienda = esc(m.get("source", ""))
            link   = m.get("link", "")
            if link:
                lines.append(f"• [{titulo}]({link})")
            else:
                lines.append(f"• {titulo}")
            if tienda:
                lines.append(f"  _{tienda}_")
            lines.append("")

    return "\n".join(lines)

# ─── PIPELINE ─────────────────────────────────────────────────────
async def process_image(image_bytes: bytes, update: Update, msg) -> None:
    texto = "😕 No se pudo obtener resultado."
    try:
        await msg.edit_text("📤 Subiendo imagen...")
        public_url = upload_to_imgbb(image_bytes)

        await msg.edit_text("🔍 Buscando con Google Lens...")
        data  = search_by_image_url(public_url)
        texto = format_response(data)

        visual_count   = len(data.get("visual_matches", []))
        shopping_count = len(data.get("shopping_results", []))
        logging.info(f"SerpAPI → visual_matches: {visual_count}, shopping: {shopping_count}")

        # Intentar enviar imagen del resultado
        image_url = get_best_image_url(data)
        await msg.delete()

        if image_url:
            try:
                await update.message.reply_photo(
                    photo=image_url,
                    caption=texto,
                    parse_mode="Markdown"
                )
                return
            except Exception as e:
                logging.warning(f"No se pudo enviar imagen: {e}")

        # Fallback: solo texto
        await update.message.reply_text(
            texto,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        logging.exception(f"Error en process_image: {e}")
        try:
            await msg.edit_text(texto)
        except Exception:
            await msg.edit_text("😕 Ocurrió un problema al mostrar los resultados.")

# ─── HANDLERS ─────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Procesando...")
    try:
        photo       = update.message.photo[-1]
        file        = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        await process_image(image_bytes, update, msg)
    except Exception as e:
        logging.exception(f"Error en handle_photo: {e}")
        await msg.edit_text("😕 Ocurrió un problema. Intenta con otra foto.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        msg = await update.message.reply_text("⏳ Procesando...")
        try:
            file        = await context.bot.get_file(doc.file_id)
            image_bytes = bytes(await file.download_as_bytearray())
            await process_image(image_bytes, update, msg)
        except Exception as e:
            logging.exception(f"Error en handle_document: {e}")
            await msg.edit_text("😕 Ocurrió un problema. Intenta con otra foto.")
    else:
        await update.message.reply_text("Por favor envía una imagen (JPG, PNG, WEBP).")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Envíame una *foto* del producto que quieres buscar.",
        parse_mode="Markdown"
    )

# ─── COMANDOS ─────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu asistente de búsqueda de productos.\n\n"
        "📸 Envíame una foto de cualquier producto y te mostraré:\n"
        "  • Qué producto es\n"
        "  • Precios en tiendas online\n"
        "  • Links de compra directa\n\n"
        "¡Manda una imagen para empezar!"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Comandos:\n\n"
        "/start - Iniciar el bot\n"
        "/help  - Ver esta ayuda\n\n"
        "Envía una foto y el bot buscará el producto y sus precios."
    )

# ─── MAIN ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Bot iniciado. Presiona Ctrl+C para detener.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO,                    handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE,           handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,  handle_text))
    app.run_polling(drop_pending_updates=True)