"""
Portal "Mis Cobros" — Streamlit
--------------------------------------------------------------
- Bloqueo por IP (misma lógica que el Portal de Capacitación).
- Login por operador con contraseña individual en secrets.toml.
- Cada operador ve SOLO sus cobros (leídos del Google Sheet del form).
- Editar = reenviar el Google Form prellenado con la misma factura y
  oportunidad; el Apps Script de deduplicación sobrescribe la fila.
- Vista administrador: ve todos los operadores.
"""
import hmac
import ipaddress
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_javascript import st_javascript

# ==========================================
# CONFIGURACIÓN
# ==========================================
st.set_page_config(page_title="Mis Cobros", page_icon="💶", layout="wide")

TZ = ZoneInfo("America/Bogota")
TODOS = "(Todos los operadores)"
MAX_INTENTOS = 5

# Encabezados EXACTOS de la hoja de respuestas (se buscan por nombre, no por letra)
COLUMNAS = {
    "fecha": "Marca temporal",
    "operador": "Operador",
    "oportunidad": "Id Oportunidad",
    "factura": "Número de factura",
    "llamada": "ID de la llamada",
    "importe": "Importe cobrado",
    "tipo": "Tipos de deuda",
}


def ahora_col() -> datetime:
    return datetime.now(TZ)


# ==========================================
# VALIDACIÓN DE IP (igual que el otro portal)
# ==========================================
def obtener_ip_cliente_javascript():
    js_code = "await fetch('https://api.ipify.org?format=json').then(r => r.json()).then(d => d.ip)"
    return st_javascript(js_code)


def validar_ip(ip_usuario: str) -> bool:
    if not ip_usuario or not isinstance(ip_usuario, str):
        return False
    rangos = st.secrets.get("network", {}).get("ips_permitidas", ["127.0.0.1"])
    try:
        ip_obj = ipaddress.ip_address(ip_usuario.strip())
        return any(ip_obj in ipaddress.ip_network(r, strict=False) for r in rangos)
    except ValueError:
        return False


# ==========================================
# LECTURA DEL GOOGLE SHEET (vía Apps Script Web App + token)
# ==========================================

def parsear_importe(valor):
    """'12,29€' -> 12.29 | '1.234,56' -> 1234.56 | '12.29' -> 12.29 | '' -> None"""
    txt = re.sub(r"[^0-9,.]", "", str(valor or ""))
    num = re.sub(r"[^0-9]", "", txt)
    if not num:
        return None
    m = re.search(r"[.,](\d{1,2})$", txt)
    dec = len(m.group(1)) if m else 0
    return int(num) / (10 ** dec)


@st.cache_data(ttl=60, show_spinner="Cargando cobros...")
def cargar_cobros() -> pd.DataFrame:
    cfg = st.secrets["sheet"]
    resp = requests.get(cfg["api_url"], params={"token": cfg["api_token"]}, timeout=30)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            "La API no devolvió JSON. Revisa que la aplicación web esté implementada "
            "con acceso 'Cualquier usuario' y que la URL termine en /exec."
        )
    if not data.get("ok"):
        raise RuntimeError(f"La API respondió: {data.get('error', 'error desconocido')}")

    valores = data.get("valores", [])
    if len(valores) < 2:
        return pd.DataFrame(columns=list(COLUMNAS) + ["fila", "importe_num"])

    encabezados = [h.strip().lower() for h in valores[0]]
    idx = {}
    for clave, nombre in COLUMNAS.items():
        if nombre.lower() not in encabezados:
            raise KeyError(f'No se encontró la columna "{nombre}" en la hoja.')
        idx[clave] = encabezados.index(nombre.lower())

    filas = []
    for n, fila in enumerate(valores[1:], start=2):
        fila = fila + [""] * (len(encabezados) - len(fila))
        reg = {k: fila[j].strip() for k, j in idx.items()}
        if not reg["factura"]:
            continue
        reg["fila"] = n
        filas.append(reg)

    df = pd.DataFrame(filas)
    df["fecha"] = pd.to_datetime(df["fecha"], dayfirst=True, errors="coerce")
    df["importe_num"] = df["importe"].map(parsear_importe)
    return df.sort_values("fecha", ascending=False).reset_index(drop=True)


# ==========================================
# GOOGLE FORM (prellenado / embebido)
# ==========================================
def url_form(prefill: dict | None = None, embebido: bool = False) -> str:
    f = st.secrets["form"]
    base = f["url"].split("?")[0]
    params = {}
    if prefill:
        params["usp"] = "pp_url"
        for clave, valor in prefill.items():
            entry = str(f.get(f"entry_{clave}", "")).strip()
            if entry and valor:
                params[f"entry.{entry}"] = valor
    if embebido:
        params["embedded"] = "true"
    return base + ("?" + urllib.parse.urlencode(params) if params else "")


def mostrar_form(prefill: dict | None, key: str):
    st.link_button(
        "↗️ Abrir el formulario en una pestaña nueva",
        url_form(prefill),
        use_container_width=True,
    )
    components.iframe(url_form(prefill, embebido=True), height=1150, scrolling=True)
    if st.button("🔄 Ya envié el formulario: actualizar mis datos", key=key, type="primary"):
        cargar_cobros.clear()
        st.rerun()


# ==========================================
# AUTENTICACIÓN
# ==========================================
def verificar(password_esperada, password_ingresada: str) -> bool:
    return password_esperada is not None and hmac.compare_digest(
        str(password_esperada).encode(), password_ingresada.encode()
    )


for k, v in {"autenticado": False, "usuario": None, "es_admin": False, "intentos": 0}.items():
    st.session_state.setdefault(k, v)

# ==========================================
# CORTAFUEGOS POR IP
# ==========================================
ip_detectada = obtener_ip_cliente_javascript()
if ip_detectada is None or ip_detectada == 0:
    st.info("⏳ Validando conexión con la red corporativa...")
    st.stop()

if not validar_ip(str(ip_detectada)):
    st.error("🚫 **ACCESO NO AUTORIZADO**")
    st.warning(
        f"No puedes ingresar a este portal desde tu red actual.\n\n"
        f"**IP detectada:** `{ip_detectada}`\n\n"
        f"La plataforma solo está disponible desde la red corporativa autorizada."
    )
    st.stop()

# ==========================================
# LOGIN
# ==========================================
if not st.session_state.autenticado:
    st.title("💶 Mis Cobros")
    st.caption(f"🌐 IP autorizada: `{ip_detectada}`")

    if st.session_state.intentos >= MAX_INTENTOS:
        st.error("🔒 Demasiados intentos fallidos. Cierra el navegador y vuelve a intentarlo más tarde.")
        st.stop()

    operadores = sorted(st.secrets["operadores"].keys())
    with st.form("login"):
        usuario = st.selectbox("Operador", ["Administrador"] + operadores, index=None,
                               placeholder="Selecciona tu nombre")
        password = st.text_input("Contraseña", type="password")
        enviar = st.form_submit_button("Ingresar", use_container_width=True)

    if enviar:
        if not usuario or not password:
            st.error("⚠️ Selecciona tu nombre y escribe la contraseña.")
        else:
            if usuario == "Administrador":
                ok = verificar(st.secrets["passwords"].get("admin_password"), password)
            else:
                ok = verificar(st.secrets["operadores"].get(usuario), password)
            if ok:
                st.session_state.update(autenticado=True, usuario=usuario,
                                        es_admin=(usuario == "Administrador"), intentos=0)
                st.rerun()
            else:
                st.session_state.intentos += 1
                restantes = MAX_INTENTOS - st.session_state.intentos
                st.error(f"❌ Contraseña incorrecta. Intentos restantes: {restantes}")
    st.stop()

# ==========================================
# APLICACIÓN
# ==========================================
usuario = st.session_state.usuario
es_admin = st.session_state.es_admin

with st.sidebar:
    st.title("💶 Mis Cobros")
    st.success(f"👤 **{usuario}**" + (" (admin)" if es_admin else ""))
    st.caption(f"🌐 IP: `{ip_detectada}`")
    if st.button("🔄 Actualizar datos", use_container_width=True):
        cargar_cobros.clear()
        st.rerun()
    if st.button("Cerrar sesión", use_container_width=True):
        for k in ("autenticado", "usuario", "es_admin"):
            st.session_state[k] = False if k != "usuario" else None
        st.rerun()

try:
    df_total = cargar_cobros()
except Exception as e:  # noqa: BLE001
    st.error(f"❌ No se pudo leer la hoja de cobros: {e}")
    st.stop()

# --- Filtro de seguridad: el operador SOLO ve lo suyo ---
if es_admin:
    opciones = [TODOS] + sorted(df_total["operador"].dropna().unique())
    ver = st.sidebar.selectbox("Ver operador", opciones)
    df = df_total if ver == TODOS else df_total[df_total["operador"] == ver]
else:
    df = df_total[df_total["operador"] == usuario]

umbral = float(st.secrets["form"].get("umbral_alerta", 5000))

st.title("💶 Mis cobros registrados" if not es_admin else "💶 Cobros registrados")

tab_lista, tab_editar, tab_nuevo = st.tabs(["📋 Mis cobros", "✏️ Editar un cobro", "➕ Nuevo cobro"])

# ------------------------------------------
# TAB 1: LISTADO
# ------------------------------------------
with tab_lista:
    if df.empty:
        st.info("Aún no tienes cobros registrados.")
    else:
        c1, c2 = st.columns(2)
        fechas_validas = df["fecha"].dropna()
        f_min, f_max = fechas_validas.min().date(), fechas_validas.max().date()
        rango = c1.date_input("Rango de fechas", (f_min, f_max), min_value=f_min, max_value=f_max)
        tipos = sorted(t for t in df["tipo"].unique() if t)
        tipos_sel = c2.multiselect("Tipo de deuda", tipos, default=tipos)

        vista = df.copy()
        if isinstance(rango, tuple) and len(rango) == 2:
            vista = vista[(vista["fecha"].dt.date >= rango[0]) & (vista["fecha"].dt.date <= rango[1])]
        if tipos_sel:
            vista = vista[vista["tipo"].isin(tipos_sel) | (vista["tipo"] == "")]

        hoy = ahora_col().date()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total cobrado", f"{vista['importe_num'].sum():,.2f} €".replace(",", "X").replace(".", ",").replace("X", "."))
        m2.metric("Facturas", len(vista))
        m3.metric("Cobrado hoy", f"{vista.loc[vista['fecha'].dt.date == hoy, 'importe_num'].sum():,.2f} €"
                  .replace(",", "X").replace(".", ",").replace("X", "."))
        alertas = vista["importe_num"].isna() | (vista["importe_num"] > umbral)
        m4.metric("⚠️ Por revisar", int(alertas.sum()),
                  help=f"Sin importe válido o importe mayor a {umbral:,.0f} €")

        tabla = pd.DataFrame({
            "Fecha": vista["fecha"].dt.strftime("%d/%m/%Y %H:%M"),
            **({"Operador": vista["operador"]} if es_admin else {}),
            "Factura": vista["factura"],
            "Id Oportunidad": vista["oportunidad"],
            "ID llamada": vista["llamada"],
            "Importe (€)": vista["importe_num"],
            "Importe escrito": vista["importe"],
            "Tipo de deuda": vista["tipo"],
            "Estado": alertas.map({True: "⚠️ Revisar", False: "✅"}),
        })
        st.dataframe(
            tabla, use_container_width=True, hide_index=True,
            column_config={"Importe (€)": st.column_config.NumberColumn(format="%.2f €")},
        )
        st.download_button(
            "📥 Descargar CSV",
            tabla.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"cobros_{usuario}_{ahora_col():%Y%m%d_%H%M}.csv".replace(" ", "_"),
            mime="text/csv",
        )

# ------------------------------------------
# TAB 2: EDITAR (reenviar el form prellenado)
# ------------------------------------------
with tab_editar:
    if df.empty:
        st.info("No hay cobros para editar.")
    else:
        def etiqueta(i):
            r = df.loc[i]
            imp = f"{r['importe_num']:.2f} €" if pd.notna(r["importe_num"]) else "sin importe"
            fecha = r["fecha"].strftime("%d/%m %H:%M") if pd.notna(r["fecha"]) else "—"
            pref = f"{r['operador']} · " if es_admin else ""
            return f"{pref}{r['factura']} · {imp} · {r['tipo'] or 'sin tipo'} · {fecha}"

        sel = st.selectbox("Selecciona el cobro a corregir", df.index, format_func=etiqueta)
        reg = df.loc[sel]

        st.warning(
            "**No cambies el Número de factura ni el Id Oportunidad.** "
            "Con esos dos datos el sistema reconoce el registro y lo reemplaza. "
            "Si los cambias, se crea un cobro nuevo duplicado."
        )
        mostrar_form(
            {
                "operador": reg["operador"],
                "factura": reg["factura"],
                "oportunidad": reg["oportunidad"],
                "llamada": reg["llamada"],
                "importe": reg["importe"],
                "tipo": reg["tipo"],
            },
            key="refresh_editar",
        )

# ------------------------------------------
# TAB 3: NUEVO COBRO
# ------------------------------------------
with tab_nuevo:
    st.caption("El formulario se abre con tu nombre ya seleccionado.")
    mostrar_form({"operador": usuario} if not es_admin else None, key="refresh_nuevo")