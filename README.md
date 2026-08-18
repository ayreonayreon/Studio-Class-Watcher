# Navy Class Watcher

Revisa cada 5 minutos si se liberó lugar en la clase de **5:45 AM** entre
semana en Navy El Refugio, y avisa por Telegram apenas aparezca.

> ⚠️ Este repo es **público** para poder usar minutos ilimitados de GitHub
> Actions gratis. El código no contiene tus credenciales — esas viven
> encriptadas en "Secrets" de GitHub y nadie (ni siquiera tú) puede
> volver a leerlas una vez guardadas.

## 1. Crear tu bot de Telegram (2 min)

1. Abre Telegram y busca **@BotFather**.
2. Mándale `/newbot`, ponle un nombre (ej. `navy-watcher-bot`).
3. Te va a dar un **token** tipo `123456789:AAExxxxxxxxxxxxxxxxxxxxxxx` — cópialo.
4. Mándale un mensaje cualquiera a tu bot recién creado (para "activar" el chat).
5. Abre en el navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
   Ahí vas a ver un campo `"chat":{"id": 123456789, ...}` — ese número es tu
   `TELEGRAM_CHAT_ID`.

## 2. Configurar los Secrets en GitHub

En tu repo: **Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor |
|---|---|
| `EZFIT_EMAIL` | tu correo de EZfit |
| `EZFIT_PASSWORD` | tu contraseña de EZfit |
| `TELEGRAM_BOT_TOKEN` | el token de BotFather |
| `TELEGRAM_CHAT_ID` | el chat id que sacaste arriba |

## 3. Subir el proyecto

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/<tu-usuario>/navy-class-watcher.git
git push -u origin main
```

## 4. Probarlo manualmente

Ve a la pestaña **Actions** de tu repo → selecciona el workflow
**Check class availability** → **Run workflow**. Revisa los logs.

## Estado actual del proyecto (para retomar la conversación)

El script (`check_availability.py`) tiene un esqueleto funcional de
login + navegación, pero la parte de **leer qué clases tienen cupo**
todavía es un placeholder — falta afinar los selectores exactos del DOM
de la página de reservas (nombres de clases, cómo se ve "lleno" vs
"disponible", formato exacto de fecha/hora). El siguiente paso es
inspeccionar una tarjeta de clase específica en el navegador para
confirmar esos selectores.
