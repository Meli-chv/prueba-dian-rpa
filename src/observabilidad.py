"""
Observabilidad del proceso, como cualquier automatización del NOC:

- resumen_ejecucion.json : estado consolidado RPA + OCR (lo lee n8n o cualquier orquestador)
- metricas.prom          : métricas en formato Prometheus para el textfile collector de
                           node-exporter -> Prometheus/Mimir -> Grafana
- Aviso a Teams y/o Slack: resumen de la corrida y alertas (captcha fallido, CUFE no
                           encontrado, facturas que requieren revisión humana)
- Código de salida       : 0 = OK, 1 = parcial (hay algo para revisar), 2 = falla total

Nada de esto es obligatorio: si no hay webhooks configurados, solo se escriben los archivos.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("observabilidad")


def construir_resumen(rpa: list | None, salida: Path, inicio: float, incluir_ocr: bool = True) -> dict:
    r: dict = {"fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "duracion_seg": round(time.time() - inicio, 1)}
    if rpa is not None:
        estados = {}
        for x in rpa:
            estados[x.estado] = estados.get(x.estado, 0) + 1
        r["rpa"] = {
            "cufes": len(rpa), "estados": estados,
            "seg_promedio_por_cufe": round(sum(x.seg_total for x in rpa) / len(rpa), 1) if rpa else 0,
            "seg_promedio_captcha": round(sum(x.seg_captcha for x in rpa) / len(rpa), 1) if rpa else 0,
            "problemas": [{"cufe": x.cufe[:16] + "…", "estado": x.estado, "detalle": x.detalle}
                          for x in rpa if x.estado != "descargado"],
        }
    f = salida / "resumen_metricas.json"
    if incluir_ocr and f.exists():
        r["ocr"] = json.loads(f.read_text(encoding="utf-8"))
        met = salida / "metricas_ocr.csv"
        if met.exists():
            import pandas as pd
            m = pd.read_csv(met).fillna("")
            if "requiere_revision" in m:
                r["ocr"]["revision"] = [{"archivo": a[:20], "motivo": mo}
                                        for a, mo in zip(m["archivo"], m["requiere_revision"]) if mo]
    r["estado"] = estado_global(r)
    (salida / "resumen_ejecucion.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    return r


def estado_global(r: dict) -> str:
    rpa, ocr = r.get("rpa"), r.get("ocr")
    if rpa and rpa["estados"].get("descargado", 0) == 0:
        return "FALLA"
    if ocr and ocr.get("procesados_ok", 0) == 0:
        return "FALLA"
    if (rpa and rpa["problemas"]) or (ocr and (ocr.get("revision") or ocr.get("procesados_ok") != ocr.get("archivos"))):
        return "PARCIAL"
    return "OK"


def codigo_salida(r: dict) -> int:
    return {"OK": 0, "PARCIAL": 1, "FALLA": 2}[r["estado"]]


def exportar_prometheus(r: dict, salida: Path) -> Path:
    """Formato textfile collector: copiar o apuntar node-exporter a esta carpeta."""
    L = ["# HELP dian_rpa_cufes_total CUFE procesados por estado en la última corrida",
         "# TYPE dian_rpa_cufes_total gauge"]
    for est, n in r.get("rpa", {}).get("estados", {}).items():
        L.append(f'dian_rpa_cufes_total{{estado="{est}"}} {n}')
    if "rpa" in r:
        L += ["# TYPE dian_rpa_captcha_seg_promedio gauge",
              f'dian_rpa_captcha_seg_promedio {r["rpa"]["seg_promedio_captcha"]}',
              "# TYPE dian_rpa_cufe_seg_promedio gauge",
              f'dian_rpa_cufe_seg_promedio {r["rpa"]["seg_promedio_por_cufe"]}']
    o = r.get("ocr", {})
    for k, nombre in (("tiempo_promedio_por_factura_seg", "dian_ocr_factura_seg_promedio"),
                      ("confianza_media_ocr", "dian_ocr_confianza_media"),
                      ("exactitud_media_vs_capa_texto_pct", "dian_ocr_exactitud_pct"),
                      ("items_totales", "dian_ocr_items_total"),
                      ("facturas_para_revision", "dian_ocr_facturas_revision")):
        if o.get(k) is not None:
            L += [f"# TYPE {nombre} gauge", f"{nombre} {o[k]}"]
    L += ["# TYPE dian_proceso_estado gauge",
          f'dian_proceso_estado{{estado="{r["estado"]}"}} {codigo_salida(r)}',
          "# TYPE dian_proceso_ultima_ejecucion_timestamp gauge",
          f"dian_proceso_ultima_ejecucion_timestamp {int(time.time())}"]
    ruta = salida / "metricas.prom"
    ruta.write_text("\n".join(L) + "\n", encoding="utf-8")
    return ruta


def _texto(r: dict) -> list[str]:
    icono = {"OK": "✅", "PARCIAL": "⚠️", "FALLA": "🔴"}[r["estado"]]
    lineas = [f"{icono} RPA DIAN + OCR — {r['estado']} ({r['fecha']}, {r['duracion_seg']} s)"]
    if "rpa" in r:
        e = r["rpa"]["estados"]
        lineas.append(f"RPA: {e.get('descargado', 0)}/{r['rpa']['cufes']} PDF descargados · "
                      f"captcha prom. {r['rpa']['seg_promedio_captcha']} s")
        lineas += [f"• {p['cufe']} → {p['estado']}" for p in r["rpa"]["problemas"][:10]]
    if "ocr" in r:
        o = r["ocr"]
        lineas.append(f"OCR: {o.get('procesados_ok')}/{o.get('archivos')} facturas · {o.get('items_totales')} ítems · "
                      f"confianza {o.get('confianza_media_ocr')} · exactitud {o.get('exactitud_media_vs_capa_texto_pct')}%")
        lineas += [f"• Revisar {x['archivo']}: {x['motivo']}" for x in o.get("revision", [])[:10]]
    return lineas


def notificar(r: dict) -> None:
    lineas = _texto(r)
    teams, slack = os.getenv("TEAMS_WEBHOOK_URL"), os.getenv("SLACK_WEBHOOK_URL")
    solo_alertas = os.getenv("NOTIFICAR_SOLO_ALERTAS", "0") == "1"
    if solo_alertas and r["estado"] == "OK":
        return
    if teams:
        # Tarjeta adaptable: funciona con webhooks de Workflows (Power Automate) de Teams
        card = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4", "body":
                        [{"type": "TextBlock", "text": lineas[0], "weight": "Bolder", "wrap": True}] +
                        [{"type": "TextBlock", "text": t, "wrap": True, "spacing": "Small"} for t in lineas[1:]]}}]}
        _post(teams, card, "Teams")
    if slack:
        _post(slack, {"text": "\n".join(lineas)}, "Slack")


def _post(url: str, payload: dict, canal: str) -> None:
    try:
        resp = requests.post(url, json=payload, timeout=20)
        log.info("Aviso enviado a %s (HTTP %s)", canal, resp.status_code)
    except Exception as e:   # una falla de notificación no tumba el proceso
        log.warning("No se pudo notificar a %s: %s", canal, e)
