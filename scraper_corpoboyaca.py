"""
Motor de extracción de Boletines Legales de CORPOBOYACÁ.
Lo usa app.py (Streamlit), y también se puede correr solo:
    python scraper_corpoboyaca.py 2
"""
import bisect
import hashlib
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import pymupdf
import requests
from bs4 import BeautifulSoup

SITIO = "https://www.corpoboyaca.gov.co"
URL_LISTADO = f"{SITIO}/normatividad/boletines-legales/"
URL_API_MEDIOS = f"{SITIO}/cms/wp-json/wp/v2/media"
PAUSA_SEG = 2

session = requests.Session()
session.headers.update({"User-Agent": "Seguimiento-CARs/1.0 (uso investigativo; contacto: TU_CORREO)"})

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
         "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
         "noviembre": 11, "diciembre": 12}
RX_MES = "|".join(MESES)
RX_FECHA = rf"\d{{1,2}}\s+(?:de\s+)?(?:{RX_MES})\s+(?:de\s+|del\s+)?\d{{4}}"


# =================================================================== listado
def listar_boletines_api(busqueda="boletin", max_paginas=20):
    """Lista PDF desde la API de medios de WordPress. Devuelve [] si está cerrada."""
    out = []
    for pag in range(1, max_paginas + 1):
        r = session.get(URL_API_MEDIOS, timeout=30, params={
            "mime_type": "application/pdf", "search": busqueda,
            "per_page": 100, "page": pag, "orderby": "date", "order": "desc"})
        if r.status_code != 200:
            break
        datos = r.json()
        if not datos:
            break
        for m in datos:
            out.append({"titulo": BeautifulSoup(m["title"]["rendered"], "html.parser").get_text(),
                        "url": m["source_url"], "fecha_publicacion": m.get("date", "")[:10]})
        if pag >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
        time.sleep(PAUSA_SEG)
    return out


def listar_boletines_pagina(url=URL_LISTADO, profundidad=1, _visitadas=None):
    """Raspa los enlaces a PDF de la página (y un nivel de subpáginas)."""
    _visitadas = set() if _visitadas is None else _visitadas
    if url in _visitadas:
        return []
    _visitadas.add(url)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        href = urljoin(url, a["href"]).split("#")[0]
        if urlparse(href).path.lower().endswith(".pdf") or "/download/" in href.lower():
            out.append({"titulo": a.get_text(" ", strip=True), "url": href, "fecha_publicacion": ""})
        elif profundidad > 0 and href.startswith(URL_LISTADO) and href not in _visitadas:
            time.sleep(PAUSA_SEG)
            out += listar_boletines_pagina(href, profundidad - 1, _visitadas)
    vistos, unicos = set(), []
    for e in out:
        if e["url"] not in vistos:
            vistos.add(e["url"])
            unicos.append(e)
    return unicos


def listar_boletines():
    """Intenta la API; si no responde, raspa la página."""
    try:
        res = listar_boletines_api()
        if res:
            return res, "API WordPress"
    except Exception:
        pass
    return listar_boletines_pagina(), "página HTML"


# ============================================================ PDF en memoria
def descargar(url):
    r = session.get(url, timeout=180)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"no es PDF ({r.headers.get('Content-Type')})")
    return r.content


def leer_pdf_bytes(datos):
    """Texto completo + posición donde arranca cada página (para ubicar actos)."""
    with pymupdf.open(stream=datos, filetype="pdf") as doc:
        paginas = [p.get_text("text") for p in doc]
    inicios, pos = [], 0
    for t in paginas:
        inicios.append(pos)
        pos += len(t) + 1
    escaneado = sum(len(t.strip()) < 50 for t in paginas) > 0.5 * max(len(paginas), 1)
    return "\n".join(paginas), inicios, escaneado


# ============================================================== segmentación
RE_ENCABEZADO = re.compile(
    rf"(?m)^[ \t]*(?P<tipo>AUTO|RESOLUCI[OÓ]N)[ \t]+(?:No\.?|N[°º]\.?|NÚMERO)?[ \t]*"
    rf"(?P<numero>\d{{1,5}})(?i:[^\n]*\n?[^\n]{{0,60}}?(?P<fecha>{RX_FECHA}))?"
)


def segmentar(texto, inicios=None):
    ms = list(RE_ENCABEZADO.finditer(texto))
    actos = []
    for i, m in enumerate(ms):
        fin = ms[i + 1].start() if i + 1 < len(ms) else len(texto)
        pagina = bisect.bisect_right(inicios, m.start()) if inicios else None
        actos.append({"tipo": m["tipo"].upper().replace("Ó", "O"),
                      "numero": m["numero"].lstrip("0") or "0",
                      "fecha_acto": m["fecha"], "pagina": pagina,
                      "texto": texto[m.start():fin]})
    return actos


# ================================================================ extracción
RE_EXPEDIENTE = re.compile(r"\b([A-Z]{3,5})[ \t]*[-–][ \t]*(\d{2,5})[ \t]*[-/–][ \t]*(\d{2,4})\b")
RE_RADICADO = re.compile(r"(?i:radicad[oa])\s+(?i:No\.?|N[°º]\.?|número)?\s*(\d[\dA-Z\-\.]{3,}\d)")
RE_NIT = re.compile(r"(?i)\bNIT\.?\s*(?:No\.?)?\s*(\d[\d\.\-]{4,}\d)")
RE_CC = re.compile(r"(?i)c[ée]dula\s+de\s+ciudadan[ií]a\s+(?:No\.?|N[°º]\.?|número)?\s*(\d[\d\.]{3,}\d)")
RE_TITULAR = re.compile(
    r"(?i:a\s+favor\s+de|presentad[ao]\s+por|a\s+nombre\s+de|solicitud\s+de|la\s+sociedad|la\s+empresa)\s+"
    r"(?i:(?:la\s+(?:sociedad|empresa)|el\s+señor|la\s+señora|los\s+señores)\s+)?"
    r"(?P<nombre>[A-ZÁÉÍÓÚÑ0-9][A-ZÁÉÍÓÚÑ0-9\.\s&\-]{3,120}?)\s*,?\s*"
    r"(?i:identificad|con\s+NIT|con\s+c[ée]dula|NIT)")
RE_REPRESENTANTE = re.compile(
    r"(?i:representante\s+legal|apoderad[oa])\s*,?\s*(?i:(?:el|la)\s+)?(?i:señor[a]?\s+)?"
    r"(?P<nombre>[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s\.]{5,80}?)\s*,?\s*(?i:identificad|con\s+c[ée]dula|mayor)")
RE_MUNICIPIO = re.compile(r"(?i:municipio\s+de)\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+)?)")
RE_ASUNTO = re.compile(r"(?is)por\s+medio\s+de[l]?\s+(?:la\s+)?cual\s+(.{10,400}?)(?:\n\s*\n|\bEL\s+(?:DIRECTOR|SUBDIRECTOR|JEFE)|\bLA\s+(?:DIRECTORA|SUBDIRECTORA|JEFE))")
RE_VISITA = re.compile(rf"(?is)visita\s+t[eé]cnica.{{0,120}}?({RX_FECHA})")
RE_FECHA_RAD = re.compile(rf"(?is)radicad[oa].{{0,80}}?({RX_FECHA})")

TRAMITES = {
    "aprovechamiento forestal": r"aprovechamiento\s+forestal|[aá]rboles\s+aislados",
    "ocupación de cauce": r"ocupaci[oó]n\s+de\s+cauce",
    "concesión de aguas": r"concesi[oó]n\s+de\s+aguas",
    "vertimientos": r"vertimiento",
    "emisiones": r"emisiones\s+atmosf",
    "licencia ambiental": r"licencia\s+ambiental",
    "sancionatorio": r"sancionatori|formula\s+cargos|medida\s+preventiva",
}
DECISIONES = [  # el orden importa
    ("desistimiento", r"desist"), ("prórroga", r"pr[oó]rroga"), ("archivo", r"archiv"),
    ("inicio", r"(?:da|dar)\s+inicio|admite|inicia\s+(?:un\s+)?tr[aá]mite|avoca"),
    ("niega", r"\bniega"), ("otorga", r"otorga|autoriza|concede"),
    ("modifica", r"modific"), ("requerimiento", r"requier|requerimiento"),
]
SOCIEDAD = r"\b(S\.?\s?A\.?\s?S|S\.?\s?A\b|LTDA|E\.?\s?S\.?\s?P|S\.?\s?EN\s+C|CORPORACI[OÓ]N|FUNDACI[OÓ]N|ASOCIACI[OÓ]N|MUNICIPIO|EMPRESA)"

CAMPOS = ["tipo", "numero", "fecha_acto", "expediente", "tramite", "decision", "titular",
          "tipo_persona", "documento", "es_esp", "representante_apoderado", "radicado",
          "fecha_radicado", "fecha_visita", "municipio"]


def _primero(rx, texto, grupo=1):
    m = rx.search(texto)
    return " ".join(m.group(grupo).split()) if m else None


def _hash(s):
    return "PN-" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def nit_valido(nit):
    """Valida el dígito de verificación DIAN. None si el NIT no trae DV."""
    if not nit or "-" not in nit:
        return None
    base, dv = nit.replace(".", "").split("-")[:2]
    if not (base.isdigit() and dv.isdigit()):
        return None
    pesos = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]
    s = sum(int(d) * p for d, p in zip(reversed(base), pesos)) % 11
    return int(dv) == (s if s < 2 else 11 - s)


def a_fecha(s):
    if not isinstance(s, str):
        return pd.NaT
    m = re.search(rf"(\d{{1,2}})\s+(?:de\s+)?({RX_MES})\s+(?:de\s+|del\s+)?(\d{{4}})", s, re.I)
    try:
        return datetime(int(m[3]), MESES[m[2].lower()], int(m[1])) if m else pd.NaT
    except ValueError:
        return pd.NaT


def alertas(f):
    a = []
    if not f["expediente"]: a.append("sin expediente")
    if pd.isna(a_fecha(f["fecha_acto"])): a.append("fecha del acto no leída")
    if not f["decision"]: a.append("decisión no clasificada")
    if not f["titular"]: a.append("sin titular")
    if f["tipo_persona"] == "indeterminado": a.append("tipo de persona indeterminado")
    if f["tipo_persona"] == "empresa" and nit_valido(f["documento"]) is False:
        a.append("NIT con dígito de verificación inválido")
    fa, fr, fv = a_fecha(f["fecha_acto"]), a_fecha(f["fecha_radicado"]), a_fecha(f["fecha_visita"])
    if pd.notna(fr) and pd.notna(fa) and fr > fa: a.append("radicado posterior al acto")
    if pd.notna(fv) and pd.notna(fa) and fv > fa: a.append("visita posterior al acto")
    return "; ".join(a)


def extraer_campos(acto, anonimizar=True):
    t = acto["texto"]
    cabeza = t[:3000]
    asunto = _primero(RE_ASUNTO, cabeza) or " ".join(cabeza[:400].split())
    exp = RE_EXPEDIENTE.search(t)
    titular = _primero(RE_TITULAR, cabeza, "nombre")
    nit, cc = _primero(RE_NIT, cabeza), _primero(RE_CC, cabeza)

    if nit or (titular and re.search(SOCIEDAD, titular)):
        tipo_persona = "empresa"
    elif cc:
        tipo_persona = "persona natural"
    else:
        tipo_persona = "indeterminado"
    documento = nit if tipo_persona == "empresa" else cc
    if tipo_persona == "persona natural" and anonimizar:
        titular = _hash(titular) if titular else None
        documento = _hash(documento) if documento else None

    f = {
        "tipo": acto["tipo"], "numero": acto["numero"], "fecha_acto": acto["fecha_acto"],
        "expediente": "-".join(exp.groups()) if exp else None,
        "tramite": next((k for k, rx in TRAMITES.items() if re.search(rx, t, re.I)), None),
        "decision": next((k for k, rx in DECISIONES if re.search(rx, asunto.lower())), None),
        "titular": titular, "tipo_persona": tipo_persona, "documento": documento,
        "es_esp": bool(titular and re.search(r"E\.?\s?S\.?\s?P", titular)),
        "representante_apoderado": _primero(RE_REPRESENTANTE, cabeza, "nombre"),
        "radicado": _primero(RE_RADICADO, cabeza),
        "fecha_radicado": _primero(RE_FECHA_RAD, cabeza),
        "fecha_visita": _primero(RE_VISITA, t),
        "municipio": _primero(RE_MUNICIPIO, t),
        "asunto": asunto[:300], "pagina": acto.get("pagina"),
    }
    f["alertas"] = alertas(f)
    f["texto"] = t  # solo para revisión en la app; no se exporta
    return f


def procesar_pdf(datos, nombre, url="", anonimizar=True):
    texto, inicios, escaneado = leer_pdf_bytes(datos)
    filas = []
    for a in segmentar(texto, inicios):
        f = extraer_campos(a, anonimizar)
        f.update(boletin=nombre, url_boletin=url)
        filas.append(f)
    return filas, escaneado


# ======================================================== resumen expediente
def resumen_por_expediente(df):
    df = df.dropna(subset=["expediente"]).copy()
    if df.empty:
        return pd.DataFrame()
    df["f"] = df["fecha_acto"].map(a_fecha)
    filas = []
    for exp, g in df.groupby("expediente"):
        g = g.sort_values("f")
        ini = g.loc[g["decision"] == "inicio", "f"].min()
        fin = g.loc[g["decision"].isin(["otorga", "niega", "desistimiento", "archivo"]), "f"].max()
        primero = lambda c: g[c].dropna().iloc[0] if g[c].notna().any() else None
        dias = (fin - ini).days if pd.notna(ini) and pd.notna(fin) else None
        filas.append({
            "expediente": exp, "titular": primero("titular"), "tipo_persona": primero("tipo_persona"),
            "es_esp": bool(g["es_esp"].any()), "tramite": primero("tramite"),
            "municipio": primero("municipio"), "radicado": primero("radicado"),
            "fecha_auto_inicio": ini, "fecha_decision_final": fin,
            "decision_final": g.loc[g["f"] == fin, "decision"].iloc[0] if pd.notna(fin) else None,
            "dias_inicio_a_decision": dias, "n_prorrogas": int((g["decision"] == "prórroga").sum()),
            "n_actos": len(g),
            "alerta": "decisión antes del inicio" if dias is not None and dias < 0 else "",
        })
    return pd.DataFrame(filas)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    bols, fuente = listar_boletines()
    print(f"{len(bols)} boletines vía {fuente}")
    filas = []
    for b in bols[:n] if n else bols:
        print("→", b["titulo"])
        fs, esc = procesar_pdf(descargar(b["url"]), b["titulo"], b["url"])
        filas += fs
        time.sleep(PAUSA_SEG)
    df = pd.DataFrame(filas).drop(columns="texto")
    df.to_csv("corpoboyaca_actos.csv", index=False, encoding="utf-8-sig")
    resumen_por_expediente(df).to_csv("corpoboyaca_expedientes.csv", index=False, encoding="utf-8-sig")
