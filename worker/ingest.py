#!/usr/bin/env python3
"""
Worker de ingesta multi-fuente — corre gratis en GitHub Actions cada ~5 min.

Fuentes: los 15 canales provinciales oficiales de la UNE + Empresa Eléctrica
de La Habana + agregador nacional (filtrado por palabras eléctricas).
Los canales sin preview web se saltan solos y quedan registrados en el log.

Flujo: previews públicos de Telegram → parser por reglas (costo $0) →
fallback a Claude Haiku SOLO para mensajes nuevos y ambiguos (con tope por
corrida) → upsert a Supabase. Además aprende pares zona→bloque por provincia
y extrae el déficit/afectación en MW para alimentar la predicción.

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

HAVANA_TZ = timezone(timedelta(hours=-4))  # Cuba, horario de verano (America/Havana)

MAX_LLM_POR_CORRIDA = 40  # tope de llamadas a la API por ejecución

CHANNELS = [
    {"u": "EmpresaElectricaDeLaHabana", "prov": "La Habana", "prefix": "eelh"},
    {"u": "ceelh", "prov": "La Habana"},
    {"u": "elecpinar", "prov": "Pinar del Río"},
    {"u": "EEArtemisa", "prov": "Artemisa"},
    {"u": "electricamayabeque", "prov": "Mayabeque"},
    {"u": "EmpresaElectricaMatanzas", "prov": "Matanzas"},
    {"u": "empresaelectricacienfuegos1", "prov": "Cienfuegos"},
    {"u": "electrico1895", "prov": "Villa Clara"},
    {"u": "informateessp", "prov": "Sancti Spíritus"},
    {"u": "eecav", "prov": "Ciego de Ávila"},
    {"u": "empresa_electrica", "prov": "Camagüey"},
    {"u": "eleclastunas", "prov": "Las Tunas"},
    {"u": "elecholguin", "prov": "Holguín"},
    {"u": "UNE_EEG", "prov": "Granma"},
    {"u": "electricastgo", "prov": "Santiago de Cuba"},
    {"u": "elecguantanamo", "prov": "Guantánamo"},
    # agregador nacional: solo mensajes con palabras eléctricas
    {"u": "apagonencubainfo", "prov": "Cuba", "filtro": True},
]

PALABRAS_ELECTRICAS = [
    "apag", "electr", "eléctr", " mw", "sen ", " sen", "bloque", "circuito",
    "avería", "averia", "restablec", "déficit", "deficit", "generaci",
]

MUNICIPIOS = [
    "Playa", "Plaza", "Centro Habana", "Habana Vieja", "Regla", "Habana del Este",
    "Guanabacoa", "San Miguel del Padrón", "Diez de Octubre", "Cerro", "Marianao",
    "La Lisa", "Lisa", "Boyeros", "Arroyo Naranjo", "Cotorro",
]

# ---------------------------------------------------------------- scraping

def fetch_messages(username):
    """Extrae mensajes del preview público t.me/s/<canal>. Sin API ni cuenta.
    Devuelve [] si el canal tiene el preview desactivado."""
    r = requests.get(f"https://t.me/s/{username}", timeout=25,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    blocks = re.split(r'class="tgme_widget_message_wrap', r.text)[1:]
    for b in blocks:
        m_id = re.search(rf'data-post="{re.escape(username)}/(\d+)"', b)
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
    if "proceso de restablecimiento" in low or "inicia de forma gradual el restablecimiento" in low:
        return {"event_type": "corte_fin", "status": "en_proceso", "cause": "desconocida",
                "affected": [aff(circuits=_items(t))], "schedule": [],
                "confidence": 0.85, "needs_review": False}

    # Avería localizada
    if "avería" in low or "averia" in low:
        munis = _munis(t)
        streets = None
        ms = re.search(r"(?:calles?|direcci[oó]n)\s*:?\s+(.+?)(?:\.|🚨|🛑|$)", t, re.S)
        if ms:
            streets = ms.group(1).strip()
        return {"event_type": "averia", "status": "activo", "cause": "averia",
                "affected": [aff(municipality=munis[0] if munis else None, streets=streets)],
                "schedule": [], "confidence": 0.9, "needs_review": not munis}

    # Corte por déficit de generación
    if "generación nacional" in low or "generacion nacional" in low or "déficit de generación" in low:
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
MW_RE = re.compile(r"(\d{3,4})\s*mw", re.I)


def _norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def zone_block_rows(events):
    """Aprende pares zona->bloque (por provincia) de los partes que mencionan
    'bloque No. X' junto a listas de zonas. Cuenta `votes`: cuántas veces se ha
    visto ese par en el corpus reciente — la app usa ese peso para desempatar
    cuando una misma zona aparece en más de un bloque."""
    rows = {}
    for e in events:
        m = BLOCK_RE.search(e.get("raw_text") or "")
        if not m:
            continue
        block = int(m.group(1))
        prov = e.get("province") or "La Habana"
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
                k = (prov, _norm(part), block)
                if k in rows:
                    rows[k]["votes"] += 1
                    if (e["published_at"] or "") > (rows[k]["last_seen"] or ""):
                        rows[k]["last_seen"] = e["published_at"]
                else:
                    rows[k] = {
                        "province": prov,
                        "zone": part,
                        "zone_norm": _norm(part),
                        "block": block,
                        "last_seen": e["published_at"],
                        "votes": 1,
                    }
    return list(rows.values())


# ------------------------------------------------- histórico de cortes (outages)

_BUCKETS = {0: "madrugada", 1: "manana", 2: "tarde", 3: "noche"}


def _hora_local(iso):
    """Devuelve (hora_local_0_23, etiqueta_franja) para un timestamp ISO, en
    horario de La Habana. (None, None) si no se puede parsear."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HAVANA_TZ)
        h = dt.astimezone(HAVANA_TZ).hour
        return h, _BUCKETS[h // 6]
    except Exception:
        return None, None


def outage_rows(events):
    """EL HISTÓRICO. Vincula cada corte_inicio (o programación) con su
    corte_fin por (provincia, bloque) y produce un corte cerrado con duración
    real y franja horaria de inicio. `outage_id` es estable, así que reejecutar
    el worker no duplica: los cortes ya cerrados se vuelven a upsert idénticos y
    la tabla acumula histórico aunque la ventana de eventos se desplace."""
    orden = sorted((e for e in events if e.get("published_at")),
                   key=lambda e: e["published_at"])
    abiertos = {}   # (prov, block) -> (inicio_iso, cause)
    out = {}
    for e in orden:
        m = BLOCK_RE.search(e.get("raw_text") or "")
        if not m:
            continue
        block = int(m.group(1))
        prov = e.get("province") or "La Habana"
        key = (prov, block)
        et = e.get("event_type")
        ts = e["published_at"]
        if et in ("corte_inicio", "programacion"):
            abiertos[key] = (ts, e.get("cause") or "desconocida")
        elif et == "corte_fin" and key in abiertos:
            inicio_iso, cause = abiertos.pop(key)
            try:
                dur = (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                       - datetime.fromisoformat(inicio_iso.replace("Z", "+00:00"))
                       ).total_seconds() / 3600
            except Exception:
                continue
            if not (0.2 < dur < 30):   # descarta ruido y cortes absurdamente largos
                continue
            _, bucket = _hora_local(inicio_iso)
            oid = f"{prov}-b{block}-{inicio_iso}"
            out[oid] = {
                "outage_id": oid,
                "province": prov,
                "block": block,
                "start_at": inicio_iso,
                "end_at": ts,
                "duration_h": round(dur, 3),
                "hour_bucket": bucket,
                "cause": cause,
            }
    return list(out.values())


def grid_rows(events):
    """Extrae la afectación/déficit en MW por día y provincia — la señal
    nacional que hace más certera la predicción."""
    rows = {}
    for e in events:
        txt = e.get("raw_text") or ""
        mws = [int(x) for x in MW_RE.findall(txt)]
        if not mws:
            continue
        day = (e.get("published_at") or "")[:10]
        if not day:
            continue
        prov = e.get("province") or "Cuba"
        key = (day, prov)
        mw = max(mws)
        if key not in rows or mw > rows[key]["mw_max"]:
            rows[key] = {"day": day, "province": prov, "mw_max": mw,
                         "last_seen": e["published_at"]}
    return list(rows.values())

# ---------------------------------------------------------------- supabase

def _headers():
    return {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def _url(tabla):
    return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + tabla


def upsert(tabla, rows):
    if not rows:
        return
    r = requests.post(_url(tabla), headers=_headers(), json=rows, timeout=40)
    r.raise_for_status()


def fetch_existing_ids(limit=4000):
    """IDs ya guardados: evita re-parsear (y re-pagar LLM) lo ya conocido."""
    r = requests.get(_url("events") + f"?select=event_id&order=published_at.desc&limit={limit}",
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    return {x["event_id"] for x in r.json()}


def fetch_stored_events(limit=1200):
    """Ventana de eventos ya guardados para recomputar mapa de bloques e
    histórico. Incluye event_type/cause porque el histórico los necesita."""
    r = requests.get(_url("events") +
                     f"?select=event_id,raw_text,affected,published_at,province,event_type,cause"
                     f"&order=published_at.desc&limit={limit}",
                     headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------- main

def procesar_canal(ch, existing, llm_restantes):
    prefix = ch.get("prefix", ch["u"])
    try:
        msgs = fetch_messages(ch["u"])
    except Exception as e:
        print(f"  [{ch['u']}] inaccesible: {e}")
        return [], llm_restantes
    if not msgs:
        print(f"  [{ch['u']}] sin preview web (0 mensajes) — saltado")
        return [], llm_restantes
    eventos = []
    nuevos = 0
    for m in msgs:
        eid = f"{prefix}-{m['id']}"
        if eid in existing:
            continue
        low = (m["text"] or "").lower()
        if ch.get("filtro") and not any(k in low for k in PALABRAS_ELECTRICAS):
            continue
        nuevos += 1
        if m["photo"] and not m["text"]:
            body = {"event_type": "programacion", "status": "programado",
                    "cause": "deficit_generacion", "affected": [], "schedule": [],
                    "confidence": 0.0, "needs_review": True}
            ctype = "photo"
        else:
            body = parse_rules(m["text"])
            ctype = "text"
            if body is None and llm_restantes > 0:
                body = parse_llm(m["text"])
                llm_restantes -= 1
            if body is None:
                body = {"event_type": "info", "status": "activo", "cause": "desconocida",
                        "affected": [], "schedule": [], "confidence": 0.0, "needs_review": True}
        eventos.append({
            "event_id": eid,
            "message_id": m["id"],
            "channel": ch["u"],
            "published_at": m["published_at"],
            "content_type": ctype,
            "province": ch["prov"],
            "raw_text": (m["text"] or "")[:2000],
            **body,
        })
    print(f"  [{ch['u']}] {len(msgs)} en preview, {nuevos} nuevos ({ch['prov']})")
    return eventos, llm_restantes


def main():
    existing = fetch_existing_ids()
    print(f"{len(existing)} eventos ya conocidos en BD")
    todos = []
    llm_restantes = MAX_LLM_POR_CORRIDA
    for ch in CHANNELS:
        evs, llm_restantes = procesar_canal(ch, existing, llm_restantes)
        todos += evs
    if todos:
        upsert("events", todos)
    print(f"Upsert de {len(todos)} eventos nuevos")
    base = todos + fetch_stored_events()
    zb = zone_block_rows(base)
    upsert("zone_blocks", zb)
    print(f"Mapeo zona->bloque: {len(zb)} pares")
    gr = grid_rows(base)
    upsert("grid_status", gr)
    print(f"Déficit MW: {len(gr)} registros día/provincia")
    ob = outage_rows(base)
    upsert("outages", ob)
    print(f"Histórico de cortes: {len(ob)} cortes cerrados (bloque+franja)")


if __name__ == "__main__":
    if "--dry" in sys.argv:  # prueba local sin Supabase
        for ch in CHANNELS:
            try:
                msgs = fetch_messages(ch["u"])
                print(f"[{ch['u']}] {len(msgs)} mensajes")
            except Exception as e:
                print(f"[{ch['u']}] ERROR {e}")
    else:
        main()
