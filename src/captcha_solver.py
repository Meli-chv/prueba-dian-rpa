"""
Resolución del captcha (Cloudflare Turnstile) del catálogo de la DIAN.

Cómo se comporta el portal:
- El widget Turnstile vive en un iframe de challenges.cloudflare.com dentro de
  un shadow DOM cerrado: document.querySelector no lo ve, pero page.frames sí.
- Puede aparecer en el buscador y otra vez en la página de detalle.
- A veces se valida solo; otras veces exige un clic en la casilla.
- Cuando se valida, deja el token en el campo oculto [name=cf-turnstile-response].

Estrategia por capas, de la más barata a la más costosa (igual que una
escalación en el NOC: primero lo automático, luego el apoyo, y al final la persona):

  1. AUTO     -> esperar a que Turnstile valide el navegador; si no lo hace,
                 mover el mouse de forma gradual y hacer clic en la casilla.
  2. SERVICIO -> si hay CAPTCHA_API_KEY, enviar sitekey + URL a una API
                 compatible con 2Captcha e inyectar el token devuelto.
  3. MANUAL   -> si la ejecución es interactiva, una persona lo resuelve en la
                 ventana y el robot continúa solo al detectar el token.
"""
from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass

import requests
from playwright.sync_api import Error as PWError, Page

log = logging.getLogger("captcha")


@dataclass
class CaptchaResult:
    resuelto: bool
    metodo: str          # auto | clic | servicio | manual | no_requerido | fallido
    segundos: float


# --------------------------------------------------------------------------- #
# Lectura segura del DOM (el portal navega mientras evaluamos)
# --------------------------------------------------------------------------- #
def _eval(page: Page, js: str, arg=None, intentos: int = 4):
    for i in range(intentos):
        try:
            return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except PWError as e:
            transitorio = "context was destroyed" in str(e).lower() or "navigation" in str(e).lower()
            if not transitorio or i == intentos - 1:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWError:
                time.sleep(1)


def _frame_turnstile(page: Page):
    return next((f for f in page.frames if "challenges.cloudflare.com" in f.url), None)


def hay_turnstile(page: Page) -> bool:
    return bool(_frame_turnstile(page)) or bool(_eval(
        page, "() => !!document.querySelector('.cf-turnstile, [name=\"cf-turnstile-response\"]')"))


def token_actual(page: Page) -> str:
    return _eval(page, """() => {
        const el = document.querySelector('[name="cf-turnstile-response"]');
        return el ? (el.value || '') : '';
    }""") or ""


def sitekey(page: Page) -> str | None:
    key = _eval(page, "() => { const e = document.querySelector('[data-sitekey]'); "
                      "return e ? e.getAttribute('data-sitekey') : null; }")
    if key:
        return key
    f = _frame_turnstile(page)   # la URL del iframe lleva la sitekey: .../0x4AAAA.../
    m = re.search(r"/(0x[0-9A-Za-z_-]{10,})/", f.url) if f else None
    return m.group(1) if m else None


def _esperar_token(page: Page, segundos: float) -> bool:
    fin = time.time() + segundos
    while time.time() < fin:
        if token_actual(page):
            return True
        page.wait_for_timeout(500)
    return False


# --------------------------------------------------------------------------- #
# Capa 1b: clic "humano" en la casilla
# --------------------------------------------------------------------------- #
def _clic_casilla(page: Page) -> None:
    f = _frame_turnstile(page)
    if not f:
        return
    caja = f.frame_element().bounding_box()
    if not caja:
        return
    x = caja["x"] + random.uniform(24, 34)                 # la casilla está a la izquierda
    y = caja["y"] + caja["height"] / 2 + random.uniform(-3, 3)
    page.mouse.move(x - random.randint(80, 200), y - random.randint(40, 120), steps=random.randint(8, 14))
    page.mouse.move(x, y, steps=random.randint(10, 18))
    page.wait_for_timeout(random.randint(200, 500))
    page.mouse.click(x, y)
    log.info("Clic en la casilla de Turnstile (%.0f, %.0f)", x, y)


# --------------------------------------------------------------------------- #
# Capa 2: servicio externo (API compatible 2Captcha)
# --------------------------------------------------------------------------- #
def _resolver_con_servicio(key: str, url: str, api_key: str, base: str, timeout_s: int = 180) -> str | None:
    r = requests.post(f"{base}/in.php", timeout=30, data={
        "key": api_key, "method": "turnstile", "sitekey": key, "pageurl": url, "json": 1}).json()
    if r.get("status") != 1:
        log.warning("El servicio rechazó la tarea: %s", r)
        return None
    fin, task = time.time() + timeout_s, r["request"]
    time.sleep(10)
    while time.time() < fin:
        r = requests.get(f"{base}/res.php", timeout=30,
                         params={"key": api_key, "action": "get", "id": task, "json": 1}).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") != "CAPCHA_NOT_READY":
            log.warning("El servicio devolvió error: %s", r)
            return None
        time.sleep(5)
    return None


def _inyectar_token(page: Page, token: str) -> None:
    _eval(page, """(t) => {
        document.querySelectorAll('[name="cf-turnstile-response"]').forEach(e => e.value = t);
        const w = document.querySelector('[data-callback]');
        const cb = w && w.getAttribute('data-callback');
        if (cb && typeof window[cb] === 'function') window[cb](t);
    }""", token)


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #
def resolver(page: Page, detectar_s: float = 8, auto_s: float = 12,
             clics: int = 4, manual_s: float = 180) -> CaptchaResult:
    t0 = time.perf_counter()

    # ¿hay captcha? (el widget tarda unos segundos en inyectarse)
    fin = time.time() + detectar_s
    while not hay_turnstile(page):
        if time.time() > fin:
            return CaptchaResult(True, "no_requerido", time.perf_counter() - t0)
        page.wait_for_timeout(500)
    if token_actual(page):
        return CaptchaResult(True, "auto", time.perf_counter() - t0)

    # Capa 1: validación automática y, si no, clic en la casilla
    if _esperar_token(page, auto_s):
        return CaptchaResult(True, "auto", time.perf_counter() - t0)
    for _ in range(clics):
        _clic_casilla(page)
        if _esperar_token(page, 8):
            return CaptchaResult(True, "clic", time.perf_counter() - t0)

    # Capa 2: servicio de resolución
    api_key, key = os.getenv("CAPTCHA_API_KEY"), sitekey(page)
    if api_key and key:
        log.info("Escalando el captcha al servicio externo")
        token = _resolver_con_servicio(key, page.url, api_key, os.getenv("CAPTCHA_API_BASE", "https://2captcha.com"))
        if token:
            _inyectar_token(page, token)
            return CaptchaResult(True, "servicio", time.perf_counter() - t0)

    # Capa 3: persona en el ciclo
    if sys.stdin.isatty() and os.getenv("HEADLESS", "0") != "1":
        print("\n>>> Resuelve el captcha en la ventana del navegador; el robot sigue solo al detectarlo.")
        if _esperar_token(page, manual_s):
            return CaptchaResult(True, "manual", time.perf_counter() - t0)

    return CaptchaResult(False, "fallido", time.perf_counter() - t0)
