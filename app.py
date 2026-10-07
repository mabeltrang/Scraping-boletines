import html
import io
import random
import re
import time

import pandas as pd
import streamlit as st

import scraper_corpoboyaca as sc

st.set_page_config(page_title="Seguimiento CORPOBOYACÁ", page_icon="🌳", layout="wide")
ss = st.session_state
ss.setdefault("actos", [])          # filas con texto original
ss.setdefault("boletines", [])
ss.setdefault("revision", {})       # idx -> DataFrame de revisión
ss.setdefault("muestra", [])

OPC_REV = ["correcto", "incorrecto", "faltó", "n/a"]

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Opciones")
    anonimizar = st.toggle("Anonimizar personas naturales", value=True,
                           help="Reemplaza nombre y cédula por un código. Recomendado si publicas los datos.")
    solo_empresas = st.toggle("Mostrar solo empresas", value=False)
    if st.button("Borrar resultados"):
        ss.actos, ss.revision, ss.muestra = [], {}, []
        st.rerun()

st.title("🌳 Boletines legales CORPOBOYACÁ")
tab_ext, tab_datos, tab_ver = st.tabs(["1 · Extraer", "2 · Datos y descarga", "3 · Verificar"])

# ----------------------------------------------------------------- extraer
with tab_ext:
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Desde la web")
        if st.button("Buscar boletines publicados"):
            with st.spinner("Consultando el sitio…"):
                try:
                    ss.boletines, fuente = sc.listar_boletines()
                    st.success(f"{len(ss.boletines)} PDF encontrados (vía {fuente})")
                except Exception as e:
                    st.error(f"No se pudo listar: {e}")
        if ss.boletines:
            titulos = [f"{b['fecha_publicacion']}  {b['titulo']}".strip() for b in ss.boletines]
            elegidos = st.multiselect("Boletines a procesar", range(len(titulos)),
                                      format_func=lambda i: titulos[i], default=list(range(min(2, len(titulos)))))
            if st.button("Procesar seleccionados", type="primary"):
                ya = {a["url_boletin"] for a in ss.actos}
                barra = st.progress(0.0)
                for k, i in enumerate(elegidos):
                    b = ss.boletines[i]
                    barra.progress((k + 1) / len(elegidos), text=b["titulo"])
                    if b["url"] in ya:
                        continue
                    try:
                        filas, esc = sc.procesar_pdf(sc.descargar(b["url"]), b["titulo"], b["url"], anonimizar)
                        ss.actos += filas
                        st.write(f"✓ {b['titulo']}: {len(filas)} actos" + ("  ⚠ parece escaneado (OCR)" if esc else ""))
                    except Exception as e:
                        st.write(f"✗ {b['titulo']}: {e}")
                    time.sleep(sc.PAUSA_SEG)
    with col_b:
        st.subheader("O subir PDF a mano")
        subidos = st.file_uploader("Boletines en PDF", type="pdf", accept_multiple_files=True)
        if subidos and st.button("Procesar subidos"):
            for f in subidos:
                filas, esc = sc.procesar_pdf(f.getvalue(), f.name, "", anonimizar)
                ss.actos += filas
                st.write(f"✓ {f.name}: {len(filas)} actos" + ("  ⚠ parece escaneado (OCR)" if esc else ""))

# --------------------------------------------------------- datos / descarga
def tabla_actos():
    df = pd.DataFrame(ss.actos)
    if df.empty:
        return df
    return df[df["tipo_persona"] == "empresa"] if solo_empresas else df


with tab_datos:
    df = tabla_actos()
    if df.empty:
        st.info("Todavía no hay actos procesados.")
    else:
        vista = df.drop(columns="texto")
        res = sc.resumen_por_expediente(vista)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Actos", len(vista))
        c2.metric("Expedientes", len(res))
        c3.metric("Actos con alertas", int((vista["alertas"] != "").sum()))
        c4.metric("Revisados a mano", len(ss.revision))

        f1, f2, f3 = st.columns(3)
        tram = f1.multiselect("Trámite", sorted(vista["tramite"].dropna().unique()))
        dec = f2.multiselect("Decisión", sorted(vista["decision"].dropna().unique()))
        texto_busq = f3.text_input("Buscar titular / expediente")
        filt = vista
        if tram: filt = filt[filt["tramite"].isin(tram)]
        if dec: filt = filt[filt["decision"].isin(dec)]
        if texto_busq:
            m = filt["titular"].fillna("").str.contains(texto_busq, case=False) | \
                filt["expediente"].fillna("").str.contains(texto_busq, case=False)
            filt = filt[m]

        st.subheader("Actos")
        st.dataframe(filt, use_container_width=True, hide_index=True,
                     column_config={"url_boletin": st.column_config.LinkColumn("PDF")})
        st.subheader("Resumen por expediente")
        st.dataframe(res, use_container_width=True, hide_index=True)

        # hoja de revisión
        rev = [r.assign(acto=f"{ss.actos[i]['tipo']} {ss.actos[i]['numero']}", idx=i)
               for i, r in ss.revision.items()]
        rev_df = pd.concat(rev) if rev else pd.DataFrame()

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            vista.to_excel(xw, sheet_name="actos", index=False)
            res.to_excel(xw, sheet_name="expedientes", index=False)
            if not rev_df.empty:
                rev_df.to_excel(xw, sheet_name="revision", index=False)
        d1, d2 = st.columns(2)
        d1.download_button("⬇️ Excel (actos + expedientes + revisión)", buf.getvalue(),
                           "corpoboyaca_seguimiento.xlsx", type="primary")
        d2.download_button("⬇️ CSV de actos", vista.to_csv(index=False).encode("utf-8-sig"),
                           "corpoboyaca_actos.csv")

# ---------------------------------------------------------------- verificar
def resaltar(texto, valores):
    t = html.escape(texto[:6000])
    for v in sorted({str(v) for v in valores if v and len(str(v)) > 2}, key=len, reverse=True):
        t = re.sub(re.escape(html.escape(v)), lambda m: f"<mark>{m.group(0)}</mark>", t, flags=re.I)
    return (f"<div style='height:520px;overflow-y:auto;white-space:pre-wrap;font-size:0.85rem;"
            f"border:1px solid #ccc;border-radius:6px;padding:10px'>{t}</div>")


with tab_ver:
    if not ss.actos:
        st.info("Procesa algún boletín primero.")
    else:
        st.markdown("Toma una **muestra aleatoria**, compara cada campo con el texto original "
                    "y marca si quedó bien. Así sabes qué tan confiable es cada columna.")
        cA, cB, cC = st.columns([1, 1, 2])
        n = cA.number_input("Tamaño de muestra", 5, 200, min(20, len(ss.actos)))
        priorizar = cB.checkbox("Incluir todos los que tienen alertas")
        if cC.button("Tomar muestra"):
            idxs = list(range(len(ss.actos)))
            con_alerta = [i for i in idxs if ss.actos[i]["alertas"]] if priorizar else []
            resto = [i for i in idxs if i not in con_alerta]
            ss.muestra = con_alerta + random.sample(resto, min(int(n), len(resto)))

        if ss.muestra:
            pos = st.selectbox("Acto", range(len(ss.muestra)), format_func=lambda p:
                               f"{p+1}/{len(ss.muestra)} · {ss.actos[ss.muestra[p]]['tipo']} "
                               f"{ss.actos[ss.muestra[p]]['numero']} · {ss.actos[ss.muestra[p]]['boletin']}"
                               + ("  ✓" if ss.muestra[p] in ss.revision else ""))
            idx = ss.muestra[pos]
            a = ss.actos[idx]
            izq, der = st.columns([3, 2])
            with izq:
                if a["url_boletin"] and a["pagina"]:
                    st.markdown(f"[Abrir PDF en la página {a['pagina']}]({a['url_boletin']}#page={a['pagina']})")
                elif a["pagina"]:
                    st.caption(f"Página {a['pagina']} del archivo {a['boletin']}")
                if a["alertas"]:
                    st.warning(a["alertas"])
                st.markdown(resaltar(a["texto"], [a[c] for c in sc.CAMPOS]), unsafe_allow_html=True)
            with der:
                previa = ss.revision.get(idx)
                if previa is None:
                    previa = pd.DataFrame({"campo": sc.CAMPOS,
                                           "valor_extraido": [str(a[c]) if a[c] is not None else "" for c in sc.CAMPOS],
                                           "evaluacion": [None] * len(sc.CAMPOS),
                                           "valor_correcto": [""] * len(sc.CAMPOS)})
                editada = st.data_editor(previa, key=f"ed_{idx}", hide_index=True, height=560,
                                         disabled=["campo", "valor_extraido"],
                                         column_config={"evaluacion": st.column_config.SelectboxColumn(
                                             "evaluación", options=OPC_REV)})
                if st.button("Guardar revisión", type="primary"):
                    if editada["evaluacion"].isna().any():
                        st.error("Evalúa todos los campos (usa n/a si no aplica).")
                    else:
                        ss.revision[idx] = editada
                        st.success("Guardado")

        if ss.revision:
            st.subheader("Confiabilidad por campo")
            todo = pd.concat(ss.revision.values())
            todo = todo[todo["evaluacion"] != "n/a"]
            t = todo.groupby("campo")["evaluacion"].value_counts().unstack(fill_value=0)
            for c in ["correcto", "incorrecto", "faltó"]:
                t[c] = t.get(c, 0)
            t["% acierto"] = (100 * t["correcto"] / t[["correcto", "incorrecto", "faltó"]].sum(axis=1)).round(1)
            st.dataframe(t[["correcto", "incorrecto", "faltó", "% acierto"]].sort_values("% acierto"),
                         use_container_width=True)
            st.caption(f"Basado en {len(ss.revision)} actos revisados. 'faltó' = el dato estaba en el texto y no se extrajo.")
