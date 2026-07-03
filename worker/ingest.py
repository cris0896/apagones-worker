#!/usr/bin/env python3
"""
Worker de ingesta — corre gratis en GitHub Actions cada 5 min.

Flujo: preview público de Telegram (sin credenciales) → parser por reglas
(cubre lo formulaico, costo $0) → fallback a Claude Haiku solo si el mensaje
es ambiguo y hay ANTHROPIC_API_KEY → upsert a Supabase.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, ANTHROPIC_API_KEY (opcional)
"""

import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

CHANNEL = "EmpresaElectricaDeLaHabana"
PROVINCE = "La Habana"
PREFIX = "eelh"
HAVANA_TZ = timezone(timedelta(hours=-4))

MUNICIPIOS = [
    "Playa", "Plaza", "Centro Habana", "Habana Vieja", "Regla", "Habana del Este",
    "Guanabacoa", "San Miguel del Padrón", "Diez de Octubre", "Cerro", "Marianao",
    "La Lisa", "Lisa", "Boyeros", "Arroyo Naranjo", "Cotorro",
]

# ---------------------------------------------------------------- scraping

def fetch_messages():
    """Extrae mensajes del preview público t.me/s/<canal>. Sin API ni cuenta."""
    r = requests.get(f"https://t.me/s/{CHANNEL}", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    blocks = re.split(r'class="tgme_widget_message_wrap', r.text)[1:]
    for b in blocks:
        m_id = re.search(rf'data-post="{CHANNEL}/(\d+)"', b)
        m_time = re.search(r'datetime="([^"]+)"', b)
        m_text = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        has_photo = 'tgme_widget_message_photo' in b
        if not m_id or not m_time:
            continue
        text = ""
        if m_text:
            text = re.sub(r"<br/?>", "\n", m_text.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        out.append({
            "id": int(m_id.group(1)),
            "published_at": m_time.group(1),
            "text": text,
            "photo": has_photo,
        })
    return out

# ---------------------------------------------------------------- parser por reglas

def _items(text):
    """Ítems tras 👉, o segmentos separados por coma."""
    items = re.findall(r"👉\s*([^\n👉]+)", text)
    return [i.strip().rstrip(".") for i in items if i.strip()]


def _munis(text):
    found = []
    for m in MUNICIPIOS:
        if re.search(rf"\b{re.escape(m)}\b", text, re.I):
            name = "La Lisa" if m == "Lisa" else m
            if name not in found:
                found.append(name)
    return found


def parse_rules(text):
    """Devuelve dict parseado o None si el mensaje no encaja en ninguna regla."""
    t = text.strip()
    if not t:
        return None
    low = t.lower()

    def aff(**kw):
        base = {"municipality": None, "zones": [], "circuits": [], "block": None, "streets": None}
        base.update(kw)
        return base

    # Restablecimiento
    if "restablecido el servicio" in low:
        cause = "daf" if "daf" in low or "frecuencia" in low else "desconocida"
        return {"event_type": "corte_fin", "status": "restablecido", "cause": cause,
                "affected": [], "schedule": [], "confidence": 0.9, "needs_review": False}
    if "proceso de restablecimiento" in low:
        return {"event_type": "corte_fin", "status": "en_proceso", "cause": "desconocida",
                "affected": [aff(circuits=_items(t))], "schedule": [],
                "confidence": 0.85, "needs_review": False}

    # Avería localizada
    if "avería secundaria" in low or "averia secundaria" in low:
        munis = _munis(t)
        streets = None
        ms = re.search(r"calles?\s+(.+?)(?:\.|🚨|$)", t, re.S)
        if ms:
            streets = ms.group(1).strip()
        return {"event_type": "averia", "status": "activo", "cause": "averia",
                "affected": [aff(municipality=munis[0] if munis else None, streets=streets)],
                "schedule": [], "confidence": 0.9, "needs_review": not munis}

    # Corte por déficit de generación
    if "generación nacional" in low or "generacion nacional" in low:
        items = _items(t)
        is_circuits = "circuito" in low
        munis = _munis(t)
        affected = []
        if is_circuits:
            affected = [aff(circuits=items)]
        else:
            for it in items:
                mm = re.search(r"\(([^)]+)\)", it)
                affected.append(aff(municipality=mm.group(1) if mm else (munis[0] if munis else None),
                                    zones=[re.sub(r"\s*\([^)]*\)", "", it).strip()]))
        return {"event_type": "corte_inicio", "status": "activo", "cause": "deficit_generacion",
                "affected": affected, "schedule": [],
                "confidence": 0.9 if affected else 0.4, "needs_review": not affected}

    # DAF
    if "daf" in low or "disparo automático por frecuencia" in low:
        items = _items(t)
        affected = []
        for it in items:
            mm = re.match(r"([^:]+):\s*(.+)", it)
            if mm and mm.group(1).strip() in MUNICIPIOS:
                affected.append(aff(municipality=mm.group(1).strip(),
                                    zones=[z.strip() for z in mm.group(2).split(",")]))
        vague = not affected
        return {"event_type": "corte_inicio", "status": "activo", "cause": "daf",
                "affected": affected, "schedule": [],
                "confidence": 0.9 if affected else 0.4, "needs_review": vague}

    # Trabajos operativos / disparo en subestación
    if "trabajos operativos" in low or "disparo en la se" in low:
        munis = _munis(t)
        items = _items(t)
        cause = "trabajo_operativo" if "operativos" in low else "averia"
        zones = items if items else None
        if not zones:
            mz = re.search(r"afecta el servicio eléctrico en (.+?)(?:‼️|$)", t, re.S)
            zones = [z.strip() for z in mz.group(1).replace(" y ", ",").split(",")] if mz else []
        return {"event_type": "corte_inicio", "status": "activo", "cause": cause,
                "affected": [aff(municipality=munis[0] if munis else None,
                                 circuits=zones if "circuito" in low else [],
                                 zones=[] if "circuito" in low else zones)],
                "schedule": [], "confidence": 0.8, "needs_review": not munis}

    return None  # no encaja: fallback LLM

# ---------------------------------------------------------------- fallback LLM

def parse_llm(text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic()
    system = open(os.path.join(os.path.dirname(__file__), "prompt_parser.txt"), encoding="utf-8").read()
    resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1500,
                                  system=system, messages=[{"role": "user", "content": text}])
    out = resp.content[0].text.strip()
    if out.startswith("```"):
        out = out.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None

# ------------------------------------------------- mapeo zona -> bloque

BLOCK_RE = re.compile(r"bloque\s*(?:no\.?\s*)?(\d{1,2})\b", re.I)


def _norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def zone_block_rows(events):
    """Cuando un parte menciona 'bloque No. X' y lista zonas, aprende los pares
    zona->bloque. Ese mapeo acumulado permite a la app deducir el bloque del
    usuario a partir de su dirección."""
    rows = {}
    for e in events:
        m = BLOCK_RE.search(e.get("raw_text") or "")
        if not m:
            continue
        block = int(m.group(1))
        zones = []
        for a in e.get("affected") or []:
            zones += (a.get("zones") or []) + (a.get("circuits") or [])
        if not zones:
            zones = _items(e.get("raw_text") or "")
        for z in zones:
            for part in z.split(","):
                part = part.strip().rstrip(".")
                if len(part) < 4 or len(part) > 80:
                    continue
                rows[(_norm(part), block)] = {
                    "zone": part,
                    "zone_norm": _norm(part),
                    "block": block,
                    "last_seen": e["published_at"],
                }
    return list(rows.values())


def upsert_zone_blocks(rows):
    if not rows:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/zone_blocks"
    headers = {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    r = requests.post(url, headers=headers, json=rows, timeout=30)
    r.raise_for_status()

# ---------------------------------------------------------------- supabase

def upsert(events):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/events"
    headers = {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    r = requests.post(url, headers=headers, json=events, timeout=30)
    r.raise_for_status()

# ---------------------------------------------------------------- main

def main():
    msgs = fetch_messages()
    print(f"{len(msgs)} mensajes en el preview")
    events = []
    for m in msgs:
        if m["photo"] and not m["text"]:
            ev_body = {"event_type": "programacion", "status": "programado",
                       "cause": "deficit_generacion", "affected": [], "schedule": [],
                       "confidence": 0.0, "needs_review": True}
            ctype = "photo"
        else:
            ev_body = parse_rules(m["text"]) or parse_llm(m["text"])
            ctype = "text"
            if ev_body is None:
                ev_body = {"event_type": "info", "status": "activo", "cause": "desconocida",
                           "affected": [], "schedule": [], "confidence": 0.0, "needs_review": True}
        events.append({
            "event_id": f"{PREFIX}-{m['id']}",
            "message_id": m["id"],
            "channel": CHANNEL,
            "published_at": m["published_at"],
            "content_type": ctype,
            "province": PROVINCE,
            "raw_text": m["text"][:2000],
            **ev_body,
        })
    if events:
        upsert(events)
        by_rule = sum(1 for e in events if e["confidence"] > 0)
        print(f"Upsert de {len(events)} eventos ({by_rule} parseados con confianza)")
        zb = zone_block_rows(events)
        upsert_zone_blocks(zb)
        print(f"Mapeo zona->bloque: {len(zb)} pares aprendidos")


if __name__ == "__main__":
    if "--dry" in sys.argv:  # prueba local sin Supabase
        for m in fetch_messages():
            parsed = parse_rules(m["text"]) if m["text"] else None
            tag = parsed["event_type"] if parsed else ("FOTO" if m["photo"] else "SIN_REGLA")
            print(f"[{m['id']}] {tag}: {m['text'][:80]!r}")
    else:
        main()
