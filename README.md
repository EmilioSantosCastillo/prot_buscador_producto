# 🔍 Bot Buscador de Productos — Telegram

Bot de Telegram que recibe una foto de cualquier producto, la analiza con **Google Lens vía SerpAPI**, y responde con el nombre del producto, imagen de referencia, precios y links de compra en tiendas online.

---

## 📦 Alcance

- El usuario envía una foto de un producto desde Telegram
- El bot sube la imagen temporalmente a **ImgBB** para obtener una URL pública
- Esa URL se envía a **Google Lens (SerpAPI)** para identificar el producto
- El bot responde con:
  - 🖼️ Imagen del mejor resultado encontrado
  - 🔍 Nombre del producto identificado
  - 🛒 Hasta 5 resultados con precio, tienda y link de compra
  - 🔗 Si no hay precios, muestra productos visualmente similares
- Si la imagen del resultado no está disponible, responde solo con texto

---

## 🛠️ Tecnologías

| Componente | Tecnología |
|---|---|
| Bot | Python 3.11 + python-telegram-bot 21.6 |
| Búsqueda visual | Google Lens vía SerpAPI |
| Hosting de imagen temporal | ImgBB API |
| Contenedor | Docker + Docker Compose |
| Servidor | VPS Ubuntu 22.04 |

---

## 📁 Estructura del proyecto

```
bot-lens/
├── bot.py              # Código de producción (sin dotenv)
├── bot_local.py        # Código para pruebas locales (con dotenv)
├── requirements.txt    # Dependencias Python
├── Dockerfile          # Imagen Docker
├── docker-compose.yml  # Configuración del contenedor
├── .env                # Variables de entorno (NO subir a git)
├── .env.example        # Ejemplo de variables de entorno
├── .dockerignore       # Archivos excluidos del build
└── README.md           # Este archivo
```

---

## 🔑 Variables de entorno

Crear archivo `.env` en la raíz del proyecto:

```env
TELEGRAM_TOKEN=tu_token_de_botfather
SERPAPI_KEY=tu_key_de_serpapi
IMGBB_KEY=tu_key_de_imgbb
```

| Variable | Dónde obtenerla |
|---|---|
| `TELEGRAM_TOKEN` | [@BotFather](https://t.me/botfather) en Telegram |
| `SERPAPI_KEY` | [serpapi.com](https://serpapi.com) — 100 búsquedas/mes gratis |
| `IMGBB_KEY` | [api.imgbb.com](https://api.imgbb.com) — gratis |

---

## 🚀 Despliegue en VPS

### Requisitos previos
- VPS con Ubuntu 22.04
- Docker instalado (`curl -fsSL https://get.docker.com | sh`)
- Usuario con permisos de Docker

### Primera vez

**1. Crear carpeta en la VPS:**
```powershell
ssh emilio@170.239.87.142 -p 60784 "mkdir -p ~/bot-lens"
```

**2. Subir archivos desde tu PC:**
```powershell
scp -P 60784 bot.py Dockerfile docker-compose.yml requirements.txt .env emilio@170.239.87.142:~/bot-lens/
```

**3. Conectarse a la VPS:**
```powershell
ssh emilio@170.239.87.142 -p 60784
```

**4. Levantar el contenedor:**
```bash
cd ~/bot-lens
docker compose up -d --build
```

**5. Verificar que esté corriendo:**
```bash
docker compose logs -f
```
Deberías ver `Bot iniciado`. Presiona `Ctrl+C` para salir de los logs sin detener el bot.

---

## 🔄 Actualizar el bot en la VPS

Cuando hagas cambios en el código:

**1. Prueba localmente:**
```powershell
python bot_local.py
```

**2. Copia los cambios a `bot.py`** (sin las líneas de dotenv)

**3. Sube el archivo actualizado:**
```powershell
scp -P 60784 bot.py emilio@170.239.87.142:~/bot-lens/
```

**4. Reconstruye el contenedor en la VPS:**
```bash
cd ~/bot-lens
docker compose up -d --build
```

---

## 🧪 Pruebas locales

Usar `bot_local.py` que carga las variables desde `.env` automáticamente:

```powershell
# Activar entorno virtual
venv\Scripts\activate

# Ejecutar bot local
python bot_local.py
```

---

## 🐳 Comandos Docker útiles

```bash
# Ver estado del contenedor
docker compose ps

# Ver logs en tiempo real
docker compose logs -f

# Detener el bot
docker compose down

# Reiniciar el bot
docker compose restart

# Reconstruir y levantar
docker compose up -d --build
```

---

## ⚠️ Notas importantes

- El archivo `.env` **nunca debe subirse a git** — agrégalo al `.gitignore`
- Las imágenes en ImgBB tienen expiración de 5 minutos (suficiente para la búsqueda)
- SerpAPI tiene un límite de **100 búsquedas/mes** en el plan gratuito
- Si la imagen del resultado falla, el bot responde con solo texto (fallback automático)
- El contenedor tiene `restart: always` — se reinicia automáticamente si la VPS se reinicia
