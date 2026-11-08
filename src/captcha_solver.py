"""
Resolución de captcha para el portal de consulta de la DIAN.

El portal (catalogo-vpfe.dian.gov.co) protege la búsqueda con un captcha
(Cloudflare Turnstile en la versión actual; versiones anteriores usaron
Google reCAPTCHA). Este módulo detecta qué tipo hay en la página y lo
resuelve con una estrategia por capas, de la más barata a la más costosa:

  1. AUTO     -> Turnstile en modo "managed" suele validar solo cuando el
                 navegador es real (Chromium con interfaz, perfil persistente).
                 Se espera a que el widget deje el token en el input oculto.
  2. SERVICIO -> Si hay CAPTCHA_API_KEY, se envía sitekey + URL a un
                 servicio de resolución (API compatible 2Captcha) y se
                 inyecta el token devuelto en la página.
  3. MANUAL   -> Si nada de lo anterior funciona y la ejecución es
                 interactiva, se pide a la persona que lo resuelva en la
                 ventana y el robot continúa solo (human-in-the-loop).

Cada intento queda registrado con el método usado y el tiempo que tomó,
para las métricas del proceso.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

import requests
from playwright.sync_api import Page

log = logging.getLogger("captcha")

TOKEN_SELECTORS = [
    "input[name='cf-turnstile-response']",
    "textarea[name='g-recaptcha-response']",
    "#g-recaptcha-response",
]


@dataclass
class CaptchaInfo:
    tipo: str            # turnstile | recaptcha_v2 | recaptcha_v3 | ninguno
    sitekey: str | None
    action: str | None = None
    cdata: str | None = None


@dataclass
class CaptchaResult:
    resuelto: bool
    metodo: str          # auto | servicio | manual | no_requerido | fallido
    segundos: float


# --------------------------------------------------------------------------- #
# Detección
# --------------------------------------------------------------------------- #
def detectar(page: Page) -> CaptchaInfo:
    """Identifica el tipo de captcha y su sitekey leyendo el DOM."""
    info = page.evaluate(
        """() => {
        const q = s => document.querySelector(s);
        const ts = q('.cf-turnstile, [data-sitekey][class*="turnstile"]');
        if (ts || q('iframe[src*="challenges.cloudflare.com"]') ||
            q("input[name='cf-turnstile-response']")) {
            const el = ts || q('[data-sitekey]');
            return {tipo: 'turnstile',
                    sitekey: el ? el.getAttribute('data-sitekey') : null,
                    action: el ? el.getAttribute('data-action') : null,
                    cdata: el ? el.getAttribute('data-cdata') : null};
        }
        const rc = q('.g-recaptcha, [data-sitekey]');
        if (rc || q('iframe[src*="recaptcha"]')) {
            return {tipo: 'recaptcha_v2',
                    sitekey: rc ? rc.getAttribute('data-sitekey') : null};
        }
        const s = [...document.scripts].map(x => x.src).find(x => x.includes('recaptcha/api.js?render='));
        if (s) {
            return {tipo: 'recaptcha_v3', sitekey: new URL(s).searchParams.get('render')};
        }
        return {tipo: 'ninguno', sitekey: null};
    }"""
    )
    return CaptchaInfo(**info)


def token_actual(page: Page) -> str:
    for sel in TOKEN_SELECTORS:
        el = page.query_selector(sel)
        if el:
            val = el.input_value() if el.evaluate("e => 'value' in e") else ""
            if val:
                return val
    return ""


def _esperar_token(page: Page, timeout_s: float) -> bool:
    fin = time.time() + timeout_s
    while time.time() < fin:
        if token_actual(page):
            return True
        page.wait_for_timeout(500)
    return False


# --------------------------------------------------------------------------- #
# Capa 2: servicio externo (API compatible 2Captcha / anti-captcha proxy)
# --------------------------------------------------------------------------- #
def _resolver_con_servicio(info: CaptchaInfo, url: str, api_key: str,
                           base: str, timeout_s: int = 180) -> str | None:
    params = {"key": api_key, "pageurl": url, "json": 1}
    if info.tipo == "turnstile":
        params.update(method="turnstile", sitekey=info.sitekey)
        if info.action:
            params["action"] = info.action
        if info.cdata:
            params["data"] = info.cdata
    elif info.tipo == "recaptcha_v2":
        params.update(method="userrecaptcha", googlekey=info.sitekey)
    elif info.tipo == "recaptcha_v3":
        params.update(method="userrecaptcha", version="v3",
                      googlekey=info.sitekey, action="submit", min_score=0.5)
    else:
        return None

    r = requests.post(f"{base}/in.php", data=params, timeout=30).json()
    if r.get("status") != 1:
        log.warning("Servicio rechazó la tarea: %s", r)
        return None
    task_id = r["request"]
    fin = time.time() + timeout_s
    time.sleep(10)
    while time.time() < fin:
        r = requests.get(f"{base}/res.php", timeout=30, params={
            "key": api_key, "action": "get", "id": task_id, "json": 1}).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") != "CAPCHA_NOT_READY":
            log.warning("Servicio devolvió error: %s", r)
            return None
        time.sleep(5)
    return None


def _inyectar_token(page: Page, token: str) -> None:
    """Escribe el token en los campos ocultos y dispara el callback del widget."""
    page.evaluate(
        """(token) => {
        for (const n of ['cf-turnstile-response', 'g-recaptcha-response']) {
            document.querySelectorAll(`[name="${n}"]`).forEach(e => { e.value = token; });
        }
        const w = document.querySelector('[data-callback]');
        const cb = w && w.getAttribute('data-callback');
        if (cb && typeof window[cb] === 'function') { window[cb](token); }
    }""",
        token,
    )


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #
def resolver(page: Page, auto_timeout_s: float = 25,
             manual_timeout_s: float = 180) -> CaptchaResult:
    t0 = time.perf_counter()
    info = detectar(page)
    log.info("Captcha detectado: %s (sitekey=%s)", info.tipo, info.sitekey)

    if info.tipo == "ninguno":
        return CaptchaResult(True, "no_requerido", time.perf_counter() - t0)

    # Capa 1: validación automática del navegador
    if _esperar_token(page, auto_timeout_s):
        return CaptchaResult(True, "auto", time.perf_counter() - t0)

    # Capa 2: servicio de resolución
    api_key = os.getenv("CAPTCHA_API_KEY")
    if api_key and info.sitekey:
        base = os.getenv("CAPTCHA_API_BASE", "https://2captcha.com")
        log.info("Enviando captcha al servicio %s", base)
        token = _resolver_con_servicio(info, page.url, api_key, base)
        if token:
            _inyectar_token(page, token)
            return CaptchaResult(True, "servicio", time.perf_counter() - t0)

    # Capa 3: humano en el ciclo (solo si hay terminal y ventana visible)
    if sys.stdin.isatty() and os.getenv("HEADLESS", "0") != "1":
        print("\n>>> Resuelve el captcha en la ventana del navegador; "
              "el robot continuará solo cuando detecte el token.")
        if _esperar_token(page, manual_timeout_s):
            return CaptchaResult(True, "manual", time.perf_counter() - t0)

    return CaptchaResult(False, "fallido", time.perf_counter() - t0)
