"""
Navy El Refugio - Watcher de disponibilidad de clases (5:45 AM, Lun-Vie)
--------------------------------------------------------------------------
Inicia sesion en la plataforma EZfit, revisa si hay lugares disponibles
en la clase de las 5:45 AM entre semana, y si encuentra un espacio NUEVO
(que no existia en la ultima corrida), manda una notificacion a Telegram.

Guarda el estado anterior en state.json y lo actualiza/commitea de vuelta
al repo (ver workflow de GitHub Actions) para no notificar dos veces lo
mismo.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuracion (viene de variables de entorno / GitHub Secrets)
# ---------------------------------------------------------------------------
EZFIT_LOGIN_URL = "https://api.ezfit.io/embed/login?gid=MzQ0&branch_id=NzY2"
BOOKING_URL = "https://www.navytrainingcenter.com/el-refugio#/booking"

EZFIT_EMAIL = os.environ["EZFIT_EMAIL"]
EZFIT_PASSWORD = os.environ["EZFIT_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TARGET_TIME = "5:45 AM"
# No filtramos por dia de la semana de forma explicita: en el sitio, las
# unicas clases de 5:45 AM que existen ya son entre semana (Sabado/Domingo
# tienen otros horarios, segun lo que vimos: 7:50 AM y 7:10 AM). Si esto
# cambiara, se puede afinar aqui.

STATE_FILE = "state.json"
DEBUG_SCREENSHOT = "debug_screenshot.png"


def load_previous_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


EXTRACT_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('div.exercise[data-classtime="05:45:00"]'));
  return cards
    .map(card => {
      const nameEl = card.querySelector('.class-type.class_name');
      const spotsEl = card.querySelector('.spots-info-text');
      return {
        class_id: card.getAttribute('data-class_id'),
        room_id: card.getAttribute('data-room_id'),
        class_date: card.getAttribute('data-classdate'),
        class_time: card.getAttribute('data-classtime'),
        autobook: card.getAttribute('data-autobook'),
        onclick: card.getAttribute('onclick') || '',
        name: nameEl ? nameEl.innerText.trim() : null,
        spots_text: spotsEl ? spotsEl.innerText.trim() : null,
        spots_class: spotsEl ? spotsEl.className : null,
      };
    });
}
"""


def parse_availability(card: dict) -> bool:
    """
    Determina si una tarjeta tiene cupo disponible, confirmado con dos
    ejemplos reales del sitio:

    DISPONIBLE:
      onclick="showspot(...)"
      <span class="spots-info-text text-success">Quedan: 48</span>

    LLENA (con lista de espera):
      onclick="addwaitlist(...)"  data-autobook="1"
      <span class="spots-info-text text-warning">Quedan: 0</span>
    """
    spots_class = (card.get("spots_class") or "").lower()
    onclick = (card.get("onclick") or "").lower()
    spots_text = (card.get("spots_text") or "").lower()

    if "addwaitlist" in onclick:
        return False
    if "text-success" in spots_class:
        return True
    if "text-warning" in spots_class or "text-danger" in spots_class:
        return False

    # Fallback: intenta sacar un numero del texto ("Quedan: 48" -> 48)
    import re
    match = re.search(r"(\d+)", spots_text)
    if match:
        return int(match.group(1)) > 0

    return False


def scrape_classes(page) -> dict:
    """
    Recorre el calendario visible (avanzando semanas con el boton '>') y
    regresa un dict {class_id: {..., available: bool}} con todas las
    clases de 5:45:00 AM encontradas, usando data-classdate/data-classtime
    como filtro exacto (confirmado en el DOM real del sitio).
    """
    all_classes: dict[str, dict] = {}

    page.wait_for_selector("text=Filter By Location", timeout=20000)

    weeks_to_check = int(os.environ.get("WEEKS_AHEAD", "2"))

    for week_index in range(weeks_to_check):
        page.wait_for_timeout(1000)  # deja asentar el render tras el cambio de semana

        found = page.evaluate(EXTRACT_JS)
        for card in found:
            if not card.get("class_id"):
                continue
            all_classes[card["class_id"]] = {
                **card,
                "available": parse_availability(card),
            }

        if week_index == 0:
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)

        # Avanzar a la siguiente semana usando la flecha '>' junto al rango
        # de fechas (ej. "2026-08-18 - 2026-08-24").
        # PENDIENTE DE CONFIRMAR: este selector es una primera aproximacion.
        # Si falla, revisar debug_screenshot.png (se guarda como artifact del
        # workflow) e inspeccionar la flecha '>' con clic derecho -> Inspeccionar,
        # igual que hicimos con la tarjeta de la clase.
        try:
            page.locator("xpath=//*[self::button or self::div][.//svg]").last.click(timeout=3000)
        except Exception as e:
            print(f"[warn] No se pudo hacer click en 'siguiente semana': {e}")

    return all_classes


def login_and_get_page(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(20000)  # 20s por accion, nunca se cuelga indefinido

    # 'networkidle' puede no llegar nunca porque el sitio manda trafico de
    # analitica (Microsoft Clarity) de forma continua. 'domcontentloaded' es
    # mas confiable aqui: espera a que el HTML este listo, no a que la red
    # este en silencio total.
    page.goto(EZFIT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

    page.fill("input[type='email'], input[name='email']", EZFIT_EMAIL)
    page.fill("input[type='password'], input[name='password']", EZFIT_PASSWORD)

    # Checkbox "I am not a robot" (simple, sin reto de imagenes segun lo confirmado)
    robot_checkbox = page.locator("text=I am not a robot").first
    if robot_checkbox.count() > 0:
        robot_checkbox.click()

    page.click("button:has-text('Log In'), button:has-text('Iniciar')")
    page.wait_for_timeout(3000)
    page.screenshot(path="debug_after_login.png", full_page=True)

    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    page.screenshot(path="debug_before_dismiss.png", full_page=True)

    # Tras el login aparece un panel transparente que tapa el calendario y
    # que se quita con un clic en cualquier parte de la pantalla (sin texto
    # ni boton visible, confirmado por el usuario). Le damos clic a una
    # esquina "segura" para no toparnos por accidente con una tarjeta de
    # clase real.
    page.mouse.click(10, 10)
    page.wait_for_timeout(1000)
    page.screenshot(path="debug_after_navigate.png", full_page=True)
    return browser, page


def main():
    previous_state = load_previous_state()

    with sync_playwright() as p:
        browser, page = login_and_get_page(p)
        try:
            current_state = scrape_classes(page)
        except Exception:
            # Dejamos evidencia visual del estado exacto de la pagina en el
            # momento del error, para poder diagnosticar sin adivinar.
            try:
                page.screenshot(path="debug_on_error.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            browser.close()

    new_findings = []
    for class_id, info in current_state.items():
        was_available = previous_state.get(class_id, {}).get("available", False)
        is_available = info.get("available", False)

        if is_available and not was_available:
            new_findings.append(info)

    if not current_state:
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] "
            "ADVERTENCIA: no se encontro ninguna clase de 5:45 AM. "
            "Revisar debug_screenshot.png -- puede que el selector de "
            "'siguiente semana' o el de las tarjetas necesite ajuste."
        )

    if new_findings:
        lines = ["🚨 <b>¡Nuevo cupo disponible!</b>"]
        for f in new_findings:
            fecha = f.get("class_date", "")
            lines.append(f"• {fecha} 5:45 AM — {f.get('name', 'Clase')} ({f.get('spots_text', '')})")
        lines.append(f"\nEntra ya: {BOOKING_URL}")
        send_telegram_message("\n".join(lines))
        print(f"[{datetime.now(timezone.utc).isoformat()}] Notificacion enviada: {len(new_findings)} clase(s).")
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sin cupos nuevos. Total clases vistas: {len(current_state)}.")

    save_state(current_state)


if __name__ == "__main__":
    sys.exit(main())
