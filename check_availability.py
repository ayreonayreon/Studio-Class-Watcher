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
import time
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuracion (viene de variables de entorno / GitHub Secrets)
# ---------------------------------------------------------------------------
EZFIT_LOGIN_URL = "https://api.ezfit.io/embed/login?gid=MzQ0&branch_id=NzY2"
BOOKING_URL = "https://www.navytrainingcenter.com/el-refugio#/booking"

EZFIT_EMAIL = os.environ.get("EZFIT_EMAIL")
EZFIT_PASSWORD = os.environ.get("EZFIT_PASSWORD")
EZFIT_COOKIES_JSON = os.environ.get("EZFIT_COOKIES_JSON")
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


def get_booking_frame(page, timeout_ms: int = 20000):
    """
    El calendario de reservas vive dentro de un <iframe> (confirmado por la
    URL de login que dice '/embed/login'). Playwright no busca automatico
    dentro de iframes, asi que hay que ubicarlo explicitamente y operar
    sobre ese frame en vez de sobre 'page' directamente.
    """
    page.wait_for_selector("iframe", timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for f in page.frames:
            if "ezfit" in f.url:
                return f
        page.wait_for_timeout(300)
    raise RuntimeError(
        "No se encontro un iframe de ezfit.io en la pagina. "
        "Revisar debug_*.png para ver que se estaba mostrando."
    )


def extract_stable(frame, page, max_wait_seconds: float = 10.0) -> list[dict]:
    """
    Lee las tarjetas de 5:45 AM y espera hasta que DOS lecturas seguidas
    (separadas por 500ms) coincidan exactamente en sus class_id's. Esto
    evita leer el DOM a medias mientras la pagina esta reemplazando las
    tarjetas de una semana por las de otra (causa confirmada de resultados
    inconsistentes: 3, luego 0, luego 1 tarjetas en corridas distintas).
    """
    deadline = time.time() + max_wait_seconds
    previous_ids = None

    while time.time() < deadline:
        found = frame.evaluate(EXTRACT_JS)
        current_ids = frozenset(c.get("class_id") for c in found)

        if previous_ids is not None and current_ids == previous_ids:
            return found

        previous_ids = current_ids
        page.wait_for_timeout(500)

    # Se acabo el tiempo sin estabilizar -- regresamos la ultima lectura,
    # pero avisamos que puede no ser confiable.
    print("[warn] La lectura de tarjetas no se estabilizo a tiempo; el resultado puede ser incompleto.")
    return frame.evaluate(EXTRACT_JS)


def scrape_classes(page) -> dict:
    """
    Recorre el calendario visible (avanzando semanas con el boton '>') y
    regresa un dict {class_id: {..., available: bool}} con todas las
    clases de 5:45:00 AM encontradas, usando data-classdate/data-classtime
    como filtro exacto (confirmado en el DOM real del sitio).

    Todo esto ocurre dentro del <iframe> de ezfit.io, no en la pagina
    principal (ver get_booking_frame).
    """
    all_classes: dict[str, dict] = {}

    frame = get_booking_frame(page)
    frame.wait_for_selector("text=Filter By Location", timeout=20000)

    # Verificacion de seguridad: confirmar que la ubicacion "Studio Refugio"
    # sigue seleccionada (deberia venir asi desde la sesion guardada). No
    # forzamos el cambio porque aun no tenemos el selector exacto del
    # dropdown -- si esto llegara a fallar algun dia, es la primera pista
    # a revisar (screenshot debug_wrong_location.png).
    if frame.locator("text=Studio Refugio").count() == 0:
        page.screenshot(path="debug_wrong_location.png", full_page=True)
        print(
            "[warn] No se detecto 'Studio Refugio' seleccionado en el filtro "
            "de ubicacion. Revisar debug_wrong_location.png -- puede que "
            "las clases mostradas no sean las correctas."
        )

    weeks_to_check = int(os.environ.get("WEEKS_AHEAD", "2"))

    for week_index in range(weeks_to_check):
        page.wait_for_timeout(1000)  # deja asentar el render tras el cambio de semana

        try:
            week_range_text = frame.locator("#weekRange").inner_text()
        except Exception:
            week_range_text = f"(desconocido, semana {week_index})"
        print(f"[info] Revisando semana {week_index}: {week_range_text}")

        total_cards = frame.evaluate("document.querySelectorAll('div.exercise').length")
        print(f"[debug] Semana {week_index}: {total_cards} tarjeta(s) totales en la pagina (todas las horas).")

        found = extract_stable(frame, page)
        print(f"[debug] Semana {week_index}: {len(found)} tarjeta(s) con data-classtime=05:45:00 encontradas (lectura estable).")
        for card in found:
            print(
                f"[debug]   class_id={card.get('class_id')} "
                f"class_date={card.get('class_date')} "
                f"spots_text={card.get('spots_text')!r} "
                f"onclick={(card.get('onclick') or '')[:40]!r}"
            )

        if len(found) == 0:
            # Diagnostico: si el filtro exacto no encontro nada, volcamos
            # TODOS los valores de data-classtime y data-classdate que
            # existen en la pagina en este momento, para ver que hay
            # realmente en vez de seguir adivinando.
            all_times = frame.evaluate(
                "Array.from(document.querySelectorAll('div.exercise')).map(e => "
                "({t: e.getAttribute('data-classtime'), d: e.getAttribute('data-classdate')}))"
            )
            unique_pairs = sorted(set((c["t"], c["d"]) for c in all_times if c["t"]))
            print(f"[debug] Valores unicos (hora, fecha) encontrados esta semana ({len(unique_pairs)}):")
            for t, d in unique_pairs[:30]:
                print(f"[debug]   hora={t!r} fecha={d!r}")
            if not card.get("class_id"):
                continue
            all_classes[card["class_id"]] = {
                **card,
                "available": parse_availability(card),
            }

        # Screenshot de CADA semana (no solo la primera), para poder
        # confirmar visualmente que el avance de semana si esta ocurriendo.
        page.screenshot(path=f"debug_week_{week_index}.png", full_page=True)

        # Avanzar a la siguiente semana usando el boton con id="nextWeek",
        # confirmado en el DOM real del sitio. En vez de solo esperar un
        # tiempo fijo, esperamos a que el texto del rango de fechas
        # realmente cambie -- asi no seguimos de largo antes de que la
        # nueva semana termine de cargar via AJAX.
        if week_index < weeks_to_check - 1:
            try:
                frame.locator("#nextWeek").click(timeout=5000)
                deadline = time.time() + 8
                while time.time() < deadline:
                    new_range_text = frame.locator("#weekRange").inner_text()
                    if new_range_text != week_range_text:
                        # El texto del rango cambia al instante, pero los
                        # datos de las clases (AJAX) tardan un poco mas en
                        # llegar -- le damos un respiro extra antes de
                        # seguir a la siguiente iteracion del bucle.
                        page.wait_for_timeout(2500)
                        break
                    page.wait_for_timeout(300)
                else:
                    print(
                        f"[warn] El rango de fechas no cambio tras dar click "
                        f"en 'siguiente semana' (seguia diciendo '{week_range_text}')."
                    )
            except Exception as e:
                print(f"[warn] No se pudo hacer click en 'siguiente semana': {e}")

    return all_classes


SAME_SITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def convert_cookie_editor_json_to_playwright(raw_json: str) -> list[dict]:
    """
    Cookie-Editor exporta cookies con nombres de campo distintos a los que
    Playwright espera (ej. 'expirationDate' en vez de 'expires', y valores
    de 'sameSite' en formato de Chrome en vez de Playwright). Esta funcion
    hace esa conversion.
    """
    raw_cookies = json.loads(raw_json)
    converted = []
    for c in raw_cookies:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
        }
        if c.get("expirationDate"):
            cookie["expires"] = c["expirationDate"]
        same_site_raw = str(c.get("sameSite", "unspecified")).lower()
        cookie["sameSite"] = SAME_SITE_MAP.get(same_site_raw, "Lax")
        converted.append(cookie)
    return converted


def login_via_cookies(context, page) -> bool:
    """
    Carga la sesion guardada en vez de llenar el formulario de login
    (evita por completo el reCAPTCHA, que no se debe automatizar).
    Regresa True si la sesion parece valida, False si parece haber expirado.
    """
    cookies = convert_cookie_editor_json_to_playwright(EZFIT_COOKIES_JSON)
    context.add_cookies(cookies)

    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Tras cargar cookies validas, puede seguir apareciendo el panel
    # transparente que descubrimos antes -- se quita con un clic.
    page.mouse.click(10, 10)
    page.wait_for_timeout(1000)
    page.screenshot(path="debug_after_cookie_login.png", full_page=True)

    # Senal de exito: si el calendario cargo, "Filter By Location" deberia
    # aparecer dentro del iframe en pocos segundos. Si la sesion expiro,
    # esto nunca aparece (aunque a veces exista un campo de password oculto
    # en el DOM que no sirve como señal confiable por si solo).
    try:
        frame = get_booking_frame(page, timeout_ms=8000)
        frame.wait_for_selector("text=Filter By Location", timeout=8000)
        return True
    except Exception:
        return False


def login_and_get_page(playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(20000)  # 20s por accion, nunca se cuelga indefinido

    if EZFIT_COOKIES_JSON:
        session_ok = login_via_cookies(context, page)
        if not session_ok:
            page.screenshot(path="debug_session_expired.png", full_page=True)
            send_telegram_message(
                "⚠️ <b>La sesion guardada expiro.</b>\n"
                "Necesitas volver a exportar las cookies de EZfit y "
                "actualizar el Secret EZFIT_COOKIES_JSON en GitHub."
            )
            browser.close()
            sys.exit(1)
        return browser, page

    # --- Fallback: login con formulario (solo si no hay cookies guardadas) ---
    if not EZFIT_EMAIL or not EZFIT_PASSWORD:
        raise RuntimeError(
            "No hay EZFIT_COOKIES_JSON ni EZFIT_EMAIL/EZFIT_PASSWORD configurados."
        )

    page.goto(EZFIT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

    page.fill("input[type='email'], input[name='email']", EZFIT_EMAIL)
    page.fill("input[type='password'], input[name='password']", EZFIT_PASSWORD)

    robot_checkbox = page.locator("text=I am not a robot").first
    if robot_checkbox.count() > 0:
        robot_checkbox.click()

    page.click("button:has-text('Log In'), button:has-text('Iniciar')")
    page.wait_for_timeout(3000)
    page.screenshot(path="debug_after_login.png", full_page=True)

    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
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
