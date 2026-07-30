##### Codigo Web


import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import pytz
from datetime import datetime
import io
import requests
import google.auth.transport.requests
from google.oauth2 import service_account
import json

# ==========================================
# CONFIGURACIÓN DE LA PÁGINA Y SEGURIDAD
# ==========================================
st.set_page_config(page_title="Dashboard de Fractura", layout="wide", initial_sidebar_state="expanded")

# --- SISTEMA DE ACCESO POR PIN ---
PIN_OPERATIVO = "FRAC2026" # <--- CAMBIÁ EL PIN ACÁ

if "acceso_concedido" not in st.session_state:
    st.session_state["acceso_concedido"] = False

if not st.session_state["acceso_concedido"]:
    st.markdown("<h1 style='text-align: center;'>🔒 Tablero Operativo Restringido</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center;'>Ingrese el código de autorización para visualizar los datos.</p>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        pin_ingresado = st.text_input("PIN de Acceso:", type="password")
        if st.button("Desbloquear Dashboard", use_container_width=True):
            if pin_ingresado == PIN_OPERATIVO:
                st.session_state["acceso_concedido"] = True
                st.rerun()
            else:
                st.error("❌ PIN incorrecto. Acceso denegado.")
    st.stop() 

# ==========================================
# PARÁMETROS STD
# ==========================================
PARAMETROS_STD = {
    "Yacimiento_A": {"Etapas_Dia_STD": 8.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0},
    "Yacimiento_B": {"Etapas_Dia_STD": 7.5, "Setupf_STD_min": 20.0, "Ramp_STD_min": 12.0},
    "Default": {"Etapas_Dia_STD": 7.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0}
}

# ==========================================
# DESCARGA SEGURA Y PROCESAMIENTO
# ==========================================
URL_DEL_EXCEL = "https://docs.google.com/spreadsheets/d/1ExsBgW_v9w5k_Yve19650zqBAdPA4jUH/export?format=xlsx"

@st.cache_data(ttl=600) 
def cargar_datos_desde_google(url):
    credenciales_dict = json.loads(st.secrets["gcp_json"])
    credentials = service_account.Credentials.from_service_account_info(
        credenciales_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    )
    
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    token = credentials.token
    
    headers = {"Authorization": f"Bearer {token}"}
    respuesta = requests.get(url, headers=headers)
    
    if respuesta.status_code != 200:
        st.error(f"Error descargando el archivo. Código: {respuesta.status_code}")
        st.stop()
        
    archivo_excel = io.BytesIO(respuesta.content)
    
    # 1. Cargamos las hojas eliminando filas totalmente vacías
    h2 = pd.read_excel(archivo_excel, sheet_name='2_Base_Mapeada_Tiempos').dropna(how='all')
    h4 = pd.read_excel(archivo_excel, sheet_name='4_Detalle_Tiempos_Bombeo').dropna(how='all')
    h8 = pd.read_excel(archivo_excel, sheet_name='8_Comparativa_y_NPTs').dropna(how='all')
    h9 = pd.read_excel(archivo_excel, sheet_name='9_Revision_Continuous_Pumping').dropna(how='all')
    
    h2.columns = h2.columns.str.strip()
    h4.columns = h4.columns.str.strip()
    h8.columns = h8.columns.str.strip()
    h9.columns = h9.columns.str.strip()

    # Función robusta para parsear fechas de Excel
    def parse_date_robust(serie):
        if serie is None or serie.empty:
            return pd.Series(pd.NaT, index=serie.index if hasattr(serie, 'index') else None)
        res = pd.to_datetime(serie, errors='coerce', dayfirst=True)
        mask_nat = res.isna() & serie.notna()
        if mask_nat.any():
            try:
                num_val = pd.to_numeric(serie[mask_nat], errors='coerce')
                res[mask_nat] = pd.to_datetime(num_val, unit='D', origin='1899-12-30', errors='coerce')
            except:
                pass
        return res.dt.normalize()

    # 2. Limpieza de fechas
    if 'fecha_reporte' in h2.columns: h2['fecha_reporte'] = parse_date_robust(h2['fecha_reporte'])
    if 'fecha_fin' in h2.columns: h2['fecha_fin'] = pd.to_datetime(h2['fecha_fin'], errors='coerce', dayfirst=True)
    
    if 'fecha_reporte' in h4.columns: h4['fecha_reporte'] = parse_date_robust(h4['fecha_reporte'])
    if 'fecha reporte' in h8.columns: h8['fecha reporte'] = parse_date_robust(h8['fecha reporte'])
    
    if 'fecha_reporte' in h9.columns: h9['fecha_reporte'] = parse_date_robust(h9['fecha_reporte'])
    if 'fecha_hora_inicio' in h9.columns: h9['fecha_hora_inicio'] = pd.to_datetime(h9['fecha_hora_inicio'], errors='coerce', dayfirst=True)
    if 'fecha_hora_fin' in h9.columns: h9['fecha_hora_fin'] = pd.to_datetime(h9['fecha_hora_fin'], errors='coerce', dayfirst=True)

    # 3. Mapeo de Yacimiento y PAD desde h2
    h2.rename(columns={'yacimiento': 'Yacimiento', 'nombre_pad': 'PAD'}, inplace=True)
    if 'fecha_reporte' in h2.columns and not h2['fecha_reporte'].dropna().empty:
        mapa_ubicacion = h2.dropna(subset=['fecha_reporte']).groupby('fecha_reporte')[['Yacimiento', 'PAD']].first().reset_index()
        h8 = pd.merge(h8, mapa_ubicacion, left_on='fecha reporte', right_on='fecha_reporte', how='left')
        h9 = pd.merge(h9, mapa_ubicacion, on='fecha_reporte', how='left')
    
    yac_dominante = h2['Yacimiento'].dropna().mode()[0] if ('Yacimiento' in h2.columns and not h2['Yacimiento'].dropna().empty) else "Yacimiento_A"
    pad_dominante = h2['PAD'].dropna().mode()[0] if ('PAD' in h2.columns and not h2['PAD'].dropna().empty) else "PAD_Default"

    for df_target in [h8, h9]:
        if 'Yacimiento' in df_target.columns: df_target['Yacimiento'] = df_target['Yacimiento'].fillna(yac_dominante)
        else: df_target['Yacimiento'] = yac_dominante
        if 'PAD' in df_target.columns: df_target['PAD'] = df_target['PAD'].fillna(pad_dominante)
        else: df_target['PAD'] = pad_dominante

    # 4. Normalizar secuencias numéricas
    if 'secuencia_diaria' in h4.columns:
        h4['secuencia_diaria'] = pd.to_numeric(h4['secuencia_diaria'], errors='coerce').fillna(-1).astype(int)
    if 'secuencia_diaria' in h9.columns:
        h9['secuencia_diaria'] = pd.to_numeric(h9['secuencia_diaria'], errors='coerce').fillna(-1).astype(int)

    # 5. Cruce robusto con la Hoja 4 para traer Pozo y Etapa reales
    if 'secuencia_diaria' in h9.columns and 'secuencia_diaria' in h4.columns:
        h4_subset = h4[['fecha_reporte', 'secuencia_diaria', 'nombre_pozo', 'nro_etapa']].copy()
        h9 = pd.merge(h9, h4_subset, on=['fecha_reporte', 'secuencia_diaria'], how='left', suffixes=('', '_h4'))
        
        missing_pozo = h9['nombre_pozo'].isna() if 'nombre_pozo' in h9.columns else pd.Series(True, index=h9.index)
        if missing_pozo.any():
            h4_sec_only = h4_subset.drop(columns=['fecha_reporte']).drop_duplicates(subset=['secuencia_diaria'])
            h9 = pd.merge(h9, h4_sec_only, on='secuencia_diaria', how='left', suffixes=('', '_sec'))
            if 'nombre_pozo_sec' in h9.columns and 'nombre_pozo' in h9.columns:
                h9['nombre_pozo'] = h9['nombre_pozo'].fillna(h9['nombre_pozo_sec'])
            if 'nro_etapa_sec' in h9.columns and 'nro_etapa' in h9.columns:
                h9['nro_etapa'] = h9['nro_etapa'].fillna(h9['nro_etapa_sec'])

    if 'nombre_pozo' not in h9.columns: h9['nombre_pozo'] = "Pozo S/D"
    else: h9['nombre_pozo'] = h9['nombre_pozo'].fillna("Pozo S/D")

    if 'nro_etapa' not in h9.columns: h9['nro_etapa'] = 0
    else: h9['nro_etapa'] = h9['nro_etapa'].fillna(0)

    # ---> FILTRO ESTRICTO: Descartamos filas sin fechas o tiempos reales (Cero inventos) <---
    h9.dropna(subset=['fecha_reporte', 'fecha_hora_inicio', 'fecha_hora_fin'], inplace=True)

    h9['fecha_reporte_cp'] = (pd.to_datetime(h9['fecha_hora_inicio']) - pd.Timedelta(hours=6) + pd.Timedelta(days=1)).dt.date
    
    return h2, h8, h9

# ==========================================
# INTERFAZ PRINCIPAL
# ==========================================
st.sidebar.title("⚙️ Panel de Control")

if st.sidebar.button("🔄 Forzar Actualización Ahora"):
    st.cache_data.clear()
    st.rerun()

try:
    with st.spinner('Conectando a base de datos segura...'):
        df_h2, df_h8, df_h9 = cargar_datos_desde_google(URL_DEL_EXCEL)
        
    zona_ar = pytz.timezone('America/Argentina/Buenos_Aires')
    hora_actual = datetime.now(zona_ar).strftime("%d/%m/%Y %H:%M hs")
    ultima_op = pd.to_datetime(df_h2['fecha_fin'], errors='coerce').max()
    hora_op = ultima_op.strftime("%d/%m/%Y %H:%M hs") if pd.notnull(ultima_op) else "Sin datos"
    
    st.sidebar.success(f"✅ Sincronizado:\n{hora_actual}")
    st.sidebar.info(f"⏱️ Última OP en pozo:\n{hora_op}")
    st.sidebar.divider()
    
    seccion = st.sidebar.radio("Navegación Principal", ["⏳ Sección 1: Tiempos", "🔄 Sección 2: Continuous Pumping"])
    
    # ==========================================
    # SECCIÓN 1: TIEMPOS
    # ==========================================
    if seccion == "⏳ Sección 1: Tiempos":
        st.title("⏳ Control Operativo de Tiempos y NPT")
        
        st.markdown("### Filtros de Visualización")
        col_f1, col_f2 = st.columns(2)
        with col_f1: toggle_npt = st.radio("Inclusión de NPT:", ["Sin NPT", "Con NPT"], horizontal=True)
        with col_f2: toggle_unidad = st.radio("Unidad de Medida:", ["min", "hrs"], horizontal=True)
        
        tab1, tab2 = st.tabs(["📊 Pestaña 1 (Detalle Diario)", "📋 Pestaña 2 (Resumen Global)"])
        
        with tab1:
            col_s1, col_s2 = st.columns(2)
            yacimientos_disp = df_h8['Yacimiento'].dropna().unique().tolist()
            if not yacimientos_disp: yacimientos_disp = ["S/D"]
            with col_s1: sel_yac_t1 = st.selectbox("Seleccionar Yacimiento (P1):", yacimientos_disp)
            
            pads_disp = df_h8[df_h8['Yacimiento'] == sel_yac_t1]['PAD'].dropna().unique().tolist()
            if not pads_disp: pads_disp = ["S/D"]
            with col_s2: sel_pad_t1 = st.selectbox("Seleccionar PAD (P1):", pads_disp)
            
            df_t1 = df_h8[(df_h8['Yacimiento'] == sel_yac_t1) & (df_h8['PAD'] == sel_pad_t1)].sort_values('fecha reporte').copy()
            
            suf_npt = "Con NPT" if toggle_npt == "Con NPT" else "Sin NPT"
            suf_und = toggle_unidad 
            factor_div = 60 if toggle_unidad == "hrs" else 1 
            
            acum_etapas = df_t1['cantidad etapas'].cumsum()
            prom_pad_setupf = df_t1[f'SETUPF {suf_npt} min'].cumsum() / acum_etapas
            prom_pad_ramp = df_t1[f'RAMP {suf_npt} min'].cumsum() / acum_etapas
            prom_pad_frac = df_t1[f'FRAC {suf_npt} min'].cumsum() / acum_etapas
            
            prom_pad_npt_total = df_t1['NPT Total min'].cumsum() / acum_etapas
            prom_pad_npt_setupf = df_t1['SETUPF NPT min'].cumsum() / acum_etapas
            prom_pad_npt_ramp = df_t1['RAMP NPT min'].cumsum() / acum_etapas
            prom_pad_npt_frac = df_t1['FRAC NPT min'].cumsum() / acum_etapas
            
            st.subheader(f"Cuadro 1 - Tiempos Operativos ({suf_und}) - {sel_pad_t1}")
            try:
                cuadro1 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_t1['fecha reporte']).dt.strftime('%d/%m/%Y'),
                    "Cant. Etapas": df_t1['cantidad etapas'].fillna(0).astype(int),
                    "Setupf": df_t1[f'SETUPF {suf_npt} {suf_und}'].fillna(0).round(2),
                    "Setupf Prom 24hs": (df_t1[f'Promedio SETUPF {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Setupf Prom PAD": (prom_pad_setupf.fillna(0) / factor_div).round(2),
                    "Ramp": df_t1[f'RAMP {suf_npt} {suf_und}'].fillna(0).round(2),
                    "Ramp Prom 24hs": (df_t1[f'Promedio RAMP {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Ramp Prom PAD": (prom_pad_ramp.fillna(0) / factor_div).round(2),
                    "Frac": df_t1[f'FRAC {suf_npt} {suf_und}'].fillna(0).round(2),
                    "Frac Prom 24hs": (df_t1[f'Promedio FRAC {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Frac Prom PAD": (prom_pad_frac.fillna(0) / factor_div).round(2)
                })
                st.dataframe(cuadro1, use_container_width=True, hide_index=True)
            except KeyError as e:
                st.warning(f"⚠️ Hubo un problema encontrando la columna: {e}")

            st.subheader(f"Cuadro 2 - Desglose de NPT ({sel_pad_t1})")
            cuadro2 = pd.DataFrame({
                "Fecha de Reporte": pd.to_datetime(df_t1['fecha reporte']).dt.strftime('%d/%m/%Y'),
                "Cant. Etapas": df_t1['cantidad etapas'].fillna(0).astype(int),
                "NPT Total": (df_t1['NPT Total min'] / factor_div).fillna(0).round(2),
                "NPT Prom 24hs": (df_t1['NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                "NPT Prom PAD": (prom_pad_npt_total.fillna(0) / factor_div).round(2),
                "NPT Setupf": (df_t1['SETUPF NPT min'] / factor_div).fillna(0).round(2),
                "NPT Setupf Prom 24hs": (df_t1['SETUPF NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                "NPT Setupf Prom PAD": (prom_pad_npt_setupf.fillna(0) / factor_div).round(2),
                "NPT Ramp": (df_t1['RAMP NPT min'] / factor_div).fillna(0).round(2),
                "NPT Ramp Prom 24hs": (df_t1['RAMP NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                "NPT Ramp Prom PAD": (prom_pad_npt_ramp.fillna(0) / factor_div).round(2),
                "NPT Frac": (df_t1['FRAC NPT min'] / factor_div).fillna(0).round(2),
                "NPT Frac Prom 24hs": (df_t1['FRAC NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                "NPT Frac Prom PAD": (prom_pad_npt_frac.fillna(0) / factor_div).round(2)
            })
            st.dataframe(cuadro2, use_container_width=True, hide_index=True)

        with tab2:
            col_m1, col_m2 = st.columns(2)
            with col_m1: sel_yac_t2 = st.multiselect("Seleccionar Yacimiento(s) (P2):", yacimientos_disp, default=yacimientos_disp)
            pads_disp_t2 = df_h8[df_h8['Yacimiento'].isin(sel_yac_t2)]['PAD'].dropna().unique().tolist()
            with col_m2: sel_pad_t2 = st.multiselect("Seleccionar PAD(s) (P2):", pads_disp_t2, default=pads_disp_t2)
            
            df_t2 = df_h8[(df_h8['Yacimiento'].isin(sel_yac_t2)) & (df_h8['PAD'].isin(sel_pad_t2))]
            
            st.subheader("Cuadro 1 - Comparativa Macro vs. STD")
            resumen_macro = []
            for pad in sel_pad_t2:
                df_pad = df_t2[df_t2['PAD'] == pad]
                if df_pad.empty: continue
                yac = df_pad['Yacimiento'].iloc[0]
                std = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])
                
                etapas_totales = df_pad['cantidad etapas'].sum()
                setup_prom = df_pad['SETUPF Sin NPT min'].sum() / etapas_totales if etapas_totales > 0 else 0
                ramp_prom = df_pad['RAMP Sin NPT min'].sum() / etapas_totales if etapas_totales > 0 else 0
                
                resumen_macro.append({
                    "Yacimiento": yac, "PAD": pad,
                    "Cant. Etapas": etapas_totales,
                    "Cant. Etapas STD": std["Etapas_Dia_STD"],
                    "Setupf Prom PAD (min)": round(setup_prom, 2),
                    "Setupf STD (min)": std["Setupf_STD_min"],
                    "Ramp Prom PAD (min)": round(ramp_prom, 2),
                    "Ramp STD (min)": std["Ramp_STD_min"]
                })
            st.dataframe(pd.DataFrame(resumen_macro), use_container_width=True, hide_index=True)

    # ==========================================
    # SECCIÓN 2: CONTINUOUS PUMPING
    # ==========================================
    elif seccion == "🔄 Sección 2: Continuous Pumping":
        st.title("🔄 Auditoría de Continuous Pumping")
        
        tab3, tab4 = st.tabs(["📊 Pestaña 1 (Diario por PAD)", "📋 Pestaña 2 (Resumen Gerencial)"])
        
        with tab3:
            col_c1, col_c2 = st.columns(2)
            yacimientos_disp = df_h9['Yacimiento'].dropna().unique().tolist()
            if not yacimientos_disp: yacimientos_disp = ["S/D"]
            with col_c1: sel_yac_c1 = st.selectbox("Seleccionar Yacimiento (C1):", yacimientos_disp)
            
            pads_disp = df_h9[df_h9['Yacimiento'] == sel_yac_c1]['PAD'].dropna().unique().tolist()
            if not pads_disp: pads_disp = ["S/D"]
            with col_c2: sel_pad_c1 = st.selectbox("Seleccionar PAD (C1):", pads_disp)
            
            df_c1_h9 = df_h9[(df_h9['Yacimiento'] == sel_yac_c1) & (df_h9['PAD'] == sel_pad_c1)].sort_values('fecha_hora_inicio').copy()
            df_c1_h2 = df_h2[(df_h2['Yacimiento'] == sel_yac_c1) & (df_h2['PAD'] == sel_pad_c1)].copy()
            
            df_c1_h9['fecha_reporte'] = pd.to_datetime(df_c1_h9['fecha_reporte']).dt.date
            df_c1_h9['fecha_reporte_cp'] = pd.to_datetime(df_c1_h9['fecha_reporte_cp']).dt.date
            df_c1_h2['fecha_reporte'] = pd.to_datetime(df_c1_h2['fecha_reporte']).dt.date
            
            st.subheader(f"Cuadro 1 - Evolución CP ({sel_pad_c1})")
            
            todas_las_fechas = set(df_c1_h9['fecha_reporte'].dropna()) | set(df_c1_h9['fecha_reporte_cp'].dropna())
            fechas_pad = sorted([f for f in todas_las_fechas if pd.notna(f)]) 
            
            std_yac = PARAMETROS_STD.get(sel_yac_c1, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]
            
            datos_cp = []
            acum_etapas_fin = 0
            acum_cp = 0
            acum_minutos_totales = 0
            posibles_acum_ayer = 0
            
            for fecha in fechas_pad:
                df_dia_h9_fin = df_c1_h9[df_c1_h9['fecha_reporte'] == fecha]
                df_dia_h2 = df_c1_h2[df_c1_h2['fecha_reporte'] == fecha]
                df_dia_h9_inicio = df_c1_h9[df_c1_h9['fecha_reporte_cp'] == fecha]
                
                etapas_dia = len(df_dia_h9_fin)
                cp_dia = (df_dia_h9_inicio['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5).sum() 
                minutos_dia = df_dia_h2['duracion_minutos'].sum()
                
                acum_etapas_fin += etapas_dia
                acum_cp += cp_dia
                acum_minutos_totales += minutos_dia
                
                posibles_acum_hoy = max(0, acum_etapas_fin - 4)
                posibles_dia = posibles_acum_hoy - posibles_acum_ayer
                
                pct_cp_dia = (cp_dia / posibles_dia * 100) if posibles_dia > 0 else 0
                pct_cp_pad = (acum_cp / posibles_acum_hoy * 100) if posibles_acum_hoy > 0 else 0
                
                dias_reales = acum_minutos_totales / 1440.0
                etapas_por_dia_real = (acum_etapas_fin / dias_reales) if dias_reales > 0 else 0
                
                datos_cp.append({
                    "Fecha Reporte": pd.to_datetime(fecha).strftime('%d/%m/%Y'),
                    "Etapas Acum.": acum_etapas_fin,
                    "Etapas Día": etapas_dia,
                    "CP Logrados": cp_dia,
                    "% CP (Día)": f"{pct_cp_dia:.1f}%",
                    "% CP (PAD)": f"{pct_cp_pad:.1f}%",
                    "Etapas STD": std_yac,
                    "Etapas/Día (Real)": round(etapas_por_dia_real, 2),
                    "Etapas Posibles CP (Acum-4)": posibles_acum_hoy
                })
                
                posibles_acum_ayer = posibles_acum_hoy
                
            columnas_tabla = ["Fecha Reporte", "Etapas Acum.", "Etapas Día", "CP Logrados", "% CP (Día)", "% CP (PAD)", "Etapas STD", "Etapas/Día (Real)", "Etapas Posibles CP (Acum-4)"]
            df_cuadro1 = pd.DataFrame(datos_cp) if datos_cp else pd.DataFrame(columns=columnas_tabla)
            st.dataframe(df_cuadro1, use_container_width=True, hide_index=True)
            
            st.subheader("Línea de Tiempo (Gantt) - FRAC & CP")
            
            df_gantt = df_c1_h9.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin', 'nombre_pozo']).copy()
            df_gantt['Es_CP'] = df_gantt['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5
            df_gantt['Tipo_FRAC'] = np.where(df_gantt['Es_CP'], 'FRAC (Logró CP)', 'FRAC (Sin CP)')
            df_gantt['Etapa Nro'] = df_gantt['nro_etapa'].astype(str)
            
            if not df_gantt.empty:
                fig = px.timeline(df_gantt, x_start="fecha_hora_inicio", x_end="fecha_hora_fin", y="nombre_pozo", 
                                  color="Tipo_FRAC",
                                  color_discrete_map={"FRAC (Logró CP)": "#2ca02c", "FRAC (Sin CP)": "#d62728"},
                                  hover_data=["Etapa Nro"])
                
                fig.update_layout(barmode='overlay', legend_title_text="Telemetría de Bombeo")
                fig.update_yaxes(autorange="reversed", type='category')
                
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning("⚠️ No hay datos de tiempo suficientes para dibujar el diagrama de Gantt en este PAD.")
            
            col_t1, col_t2 = st.columns(2)
            
            with col_t1:
                st.subheader("Listado de Transiciones")
                df_trans = df_c1_h9[df_c1_h9['transicion_cp_con_nueva_logica_chequeo'].notna() & (df_c1_h9['transicion_cp_con_nueva_logica_chequeo'] != "")].copy()
                tabla1 = pd.DataFrame({
                    "Fecha": pd.to_datetime(df_trans['fecha_reporte_cp']).dt.strftime('%d/%m/%Y') if not df_trans.empty else [],
                    "Transición (Pozo/Etapa)": df_trans['transicion_cp_con_nueva_logica_chequeo'] if not df_trans.empty else []
                })
                if tabla1.empty: tabla1 = pd.DataFrame(columns=["Fecha", "Transición (Pozo/Etapa)"])
                st.dataframe(tabla1, use_container_width=True, hide_index=True)

            with col_t2:
                st.subheader("Etapas que lograron CP")
                df_logrados = df_c1_h9[df_c1_h9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5].copy()
                tabla2 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_logrados['fecha_reporte_cp']).dt.strftime('%d/%m/%Y') if not df_logrados.empty else [],
                    "Pozo": df_logrados['nombre_pozo'] if not df_logrados.empty else [],
                    "Etapa Nro": df_logrados['nro_etapa'].fillna(0).astype(int) if not df_logrados.empty else [],
                    "Secuencia Diaria": df_logrados['secuencia_diaria'].fillna(0).astype(int) if not df_logrados.empty else []
                })
                if tabla2.empty: tabla2 = pd.DataFrame(columns=["Fecha de Reporte", "Pozo", "Etapa Nro", "Secuencia Diaria"])
                st.dataframe(tabla2, use_container_width=True, hide_index=True)

        with tab4:
            st.subheader("Cuadro 1 - Foto Final por PAD")
            col_m3, col_m4 = st.columns(2)
            with col_m3: sel_yac_c2 = st.multiselect("Seleccionar Yacimiento(s) (C2):", yacimientos_disp, default=yacimientos_disp)
            pads_disp_c2 = df_h9[df_h9['Yacimiento'].isin(sel_yac_c2)]['PAD'].dropna().unique().tolist()
            with col_m4: sel_pad_c2 = st.multiselect("Seleccionar PAD(s) (C2):", pads_disp_c2, default=pads_disp_c2)
            
            resumen_cp = []
            for pad in sel_pad_c2:
                df_pad_h9 = df_h9[df_h9['PAD'] == pad]
                df_pad_h2 = df_h2[df_h2['PAD'] == pad]
                if df_pad_h9.empty: continue
                
                yac = df_pad_h9['Yacimiento'].iloc[0]
                std = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]
                
                max_fecha = df_pad_h9['fecha_reporte'].max()
                ultima_fecha = pd.to_datetime(max_fecha).strftime('%d/%m/%Y') if pd.notna(max_fecha) else "S/D"
                
                etapas_totales = len(df_pad_h9)
                cp_totales = (df_pad_h9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5).sum()
                minutos_totales = df_pad_h2['duracion_minutos'].sum()
                
                etapas_posibles = max(0, etapas_totales - 4)
                pct_cp_final = (cp_totales / etapas_posibles * 100) if etapas_posibles > 0 else 0
                
                dias_reales = minutos_totales / 1440.0
                etapas_dia_real = (etapas_totales / dias_reales) if dias_reales > 0 else 0
                
                resumen_cp.append({
                    "Yacimiento": yac,
                    "PAD": pad,
                    "Última Fecha": ultima_fecha,
                    "Etapas Acum.": etapas_totales,
                    "Total CP Logrados": cp_totales,
                    "% CP Final (PAD)": f"{pct_cp_final:.1f}%",
                    "Etapas STD": std,
                    "Etapas/Día (Real)": round(etapas_dia_real, 2),
                    "Posibles CP (Total - 4)": etapas_posibles
                })
            
            df_resumen = pd.DataFrame(resumen_cp) if resumen_cp else pd.DataFrame(columns=["Yacimiento", "PAD", "Última Fecha", "Etapas Acum.", "Total CP Logrados", "% CP Final (PAD)", "Etapas STD", "Etapas/Día (Real)", "Posibles CP (Total - 4)"])
            st.dataframe(df_resumen, use_container_width=True, hide_index=True)

except Exception as e:
    st.error(f"❌ Ocurrió un error al procesar el tablero. Detalles: {e}")