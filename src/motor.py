"""
Motor de automatización del navegador.

Usa patchright si está instalado: es Playwright con parches que eliminan las huellas
de automatización que Cloudflare Turnstile detecta (fuga de Runtime.enable por CDP,
--enable-automation, navigator.webdriver, etc.). Si no está, usa Playwright normal.
Todo el proyecto importa desde aquí para que las clases de error coincidan con el motor.
"""
try:
    from patchright.sync_api import (BrowserContext, BrowserType, Error, Page, TimeoutError,  # noqa: F401
                                     sync_playwright)
    MOTOR = "patchright"
except ImportError:  # pragma: no cover
    from playwright.sync_api import (BrowserContext, BrowserType, Error, Page, TimeoutError,  # noqa: F401
                                     sync_playwright)
    MOTOR = "playwright"
