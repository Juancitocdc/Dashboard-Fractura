import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import pytz
from datetime import datetime

# ==========================================
# CONFIGURACIÓN DE LA PÁGINA
# ==========================================
st.set_page_config(page_title="Dashboard de Fractura", layout="wide", initial_sidebar_state="expanded")

# ==========================================
# 🔗 ENLACE DE GOOGLE DRIVE / GOOGLE SHEETS
# ==========================================
# PEGA AQUÍ TU LINK DE COMPARTIR DE GOOGLE. 
# Asegúrate de que tenga permisos de "Cualquier usuario con el enlace"
LINK_GOOGLE = "https://docs.google.com/spreadsheets/d/1ExsBgW_v9w5k_Yve19650zqBAdPA4jUH/edit?usp=sharing&ouid=110801141837319169026&rtpof=true&sd=true"

def obtener_link_descarga(url):
    """Convierte un link de Google (Drive o Sheets) en un link de descarga directa para Pandas"""
    if "drive.google.com/file/d/" in url:
        # Es un archivo Excel subido a Drive
        file_id = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?id={file_id}&export=download"
    elif "docs.google.com/spreadsheets/d/" in url:
        # Es un Google Sheets nativo
        file_id = url.split("/d/")[1].split("/")[0]
        return f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
    return url

# ==========================================
# PARÁMETROS STD (OBJETIVOS / ESTÁNDARES)
# ==========================================
PARAMETROS_STD = {
    "Yacimiento_A": {"Etapas_Dia_STD": 8.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0},
    "Yacimiento_B": {"Etapas_Dia_STD": 7.5, "Setupf_STD_min": 20.0, "Ramp_STD_min": 12.0},
    "Default": {"Etapas_Dia_STD": 7.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0}
}

# ==========================================
# FUNCIONES DE CARGA Y PROCESAMIENTO
# ==========================================
# Usamos caché para que la app vuele, ttl=600 limpia la memoria cada 10 min
@st.cache_data(ttl=600) 
def cargar_datos(link_directo):
    h2 = pd.read_excel(link_directo, sheet_name='2_Base_Mapeada_Tiempos')
    h4 = pd.read_excel(link_directo, sheet_name='4_Detalle_Tiempos_Bombeo')
    h8 = pd.read_excel(link_directo, sheet_name='8_Comparativa_y_NPTs')
    h9 = pd.read_excel(link_directo, sheet_name='9_Revision_Continuous_Pumping')
    
    h2.rename(columns={'yacimiento': 'Yacimiento', 'nombre_pad': 'PAD'}, inplace=True)
    
    mapa_ubicacion = h2.groupby('fecha_reporte')[['Yacimiento', 'PAD']].first().reset_index()
    
    h8 = pd.merge(h8, mapa_ubicacion, left_on='fecha reporte', right_on='fecha_reporte', how='left')
    h9 = pd.merge(h9, mapa_ubicacion, on='fecha_reporte', how='left')
    
    h9 = pd.merge(h9, h4[['fecha_reporte', 'secuencia_diaria', 'nombre_pozo', 'nro_etapa']], 
                  on=['fecha_reporte', 'secuencia_diaria'], how='left')
                  
    h9['fecha_reporte_cp'] = (pd.to_datetime(h9['fecha_hora_inicio']) - pd.Timedelta(hours=6) + pd.Timedelta(days=1)).dt.date
    
    return h2, h8, h9

# ==========================================
# BARRA LATERAL Y CONEXIÓN
# ==========================================
st.sidebar.title("⚙️ Panel de Control")

if LINK_GOOGLE != "Pega_tu_link_aqui":
    try:
        url_descarga = obtener_link_descarga(LINK_GOOGLE)
        df_h2, df_h8, df_h9 = cargar_datos(url_descarga)
        
        # Botón para forzar la recarga de datos saltando la caché de la página
        if st.sidebar.button("🔄 Actualizar Datos desde Nube"):
            st.cache_data.clear()
            st.rerun()
            
        # --- INDICADORES DE ACTUALIZACIÓN ---
        zona_ar = pytz.timezone('America/Argentina/Buenos_Aires')
        hora_actual = datetime.now(zona_ar).strftime("%d/%m/%Y %H:%M hs")
        
        ultima_op = pd.to_datetime(df_h2['fecha_fin']).max()
        hora_op = ultima_op.strftime("%d/%m/%Y %H:%M hs") if pd.notnull(ultima_op) else "Sin datos"
        
        st.sidebar.success(f"✅ Tablero visualizado:\n{hora_actual}")
        st.sidebar.info(f"⏱️ Última operación en pozo:\n{hora_op}")
        st.sidebar.divider()
        # ------------------------------------
        
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
            
            # --- Pestaña 1: Detalle Diario ---
            with tab1:
                col_s1, col_s2 = st.columns(2)
                yacimientos_disp = df_h8['Yacimiento'].dropna().unique().tolist()
                with col_s1: sel_yac_t1 = st.selectbox("Seleccionar Yacimiento (P1):", yacimientos_disp)
                
                pads_disp = df_h8[df_h8['Yacimiento'] == sel_yac_t1]['PAD'].dropna().unique().tolist()
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

            # --- Pestaña 2: Resumen Global ---
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
            
            # --- Pestaña 1: Diario por PAD ---
            with tab3:
                col_c1, col_c2 = st.columns(2)
                yacimientos_disp = df_h9['Yacimiento'].dropna().unique().tolist()
                with col_c1: sel_yac_c1 = st.selectbox("Seleccionar Yacimiento (C1):", yacimientos_disp)
                
                pads_disp = df_h9[df_h9['Yacimiento'] == sel_yac_c1]['PAD'].dropna().unique().tolist()
                with col_c2: sel_pad_c1 = st.selectbox("Seleccionar PAD (C1):", pads_disp)
                
                df_c1_h9 = df_h9[(df_h9['Yacimiento'] == sel_yac_c1) & (df_h9['PAD'] == sel_pad_c1)].sort_values('fecha_hora_inicio').copy()
                df_c1_h2 = df_h2[(df_h2['Yacimiento'] == sel_yac_c1) & (df_h2['PAD'] == sel_pad_c1)].copy()
                
                df_c1_h9['fecha_reporte'] = pd.to_datetime(df_c1_h9['fecha_reporte']).dt.date
                df_c1_h9['fecha_reporte_cp'] = pd.to_datetime(df_c1_h9['fecha_reporte_cp']).dt.date
                df_c1_h2['fecha_reporte'] = pd.to_datetime(df_c1_h2['fecha_reporte']).dt.date
                
                st.subheader(f"Cuadro 1 - Evolución CP ({sel_pad_c1})")
                
                fechas_pad = sorted(list(set(df_c1_h9['fecha_reporte'].dropna()) | set(df_c1_h9['fecha_reporte_cp'].dropna())))
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
                    
                st.dataframe(pd.DataFrame(datos_cp), use_container_width=True, hide_index=True)
                
                # --- Gráfico de Gantt ---
                st.subheader("Línea de Tiempo (Gantt) - FRAC & CP")
                
                df_gantt = df_c1_h9.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin', 'nombre_pozo']).copy()
                df_gantt['Es_CP'] = df_gantt['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5
                df_gantt['Tipo_FRAC'] = np.where(df_gantt['Es_CP'], 'FRAC (Logró CP)', 'FRAC (Sin CP)')
                df_gantt['Etapa Nro'] = df_gantt['nro_etapa'].astype(str)
                
                fig = px.timeline(df_gantt, x_start="fecha_hora_inicio", x_end="fecha_hora_fin", y="nombre_pozo", 
                                  color="Tipo_FRAC",
                                  color_discrete_map={"FRAC (Logró CP)": "#2ca02c", "FRAC (Sin CP)": "#d62728"},
                                  hover_data=["Etapa Nro"])
                
                fig.update_layout(barmode='overlay', legend_title_text="Telemetría de Bombeo")
                fig.update_yaxes(autorange="reversed", type='category')
                
                st.plotly_chart(fig, use_container_width=True)
                
                # --- Tablas de Transiciones ---
                col_t1, col_t2 = st.columns(2)
                
                with col_t1:
                    st.subheader("Listado de Transiciones")
                    df_trans = df_c1_h9[df_c1_h9['transicion_cp_con_nueva_logica_chequeo'].notna() & (df_c1_h9['transicion_cp_con_nueva_logica_chequeo'] != "")].copy()
                    tabla1 = pd.DataFrame({
                        "Fecha": pd.to_datetime(df_trans['fecha_reporte_cp']).dt.strftime('%d/%m/%Y'),
                        "Transición (Pozo/Etapa)": df_trans['transicion_cp_con_nueva_logica_chequeo']
                    })
                    st.dataframe(tabla1, use_container_width=True, hide_index=True)

                with col_t2:
                    st.subheader("Etapas que lograron CP")
                    df_logrados = df_c1_h9[df_c1_h9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5].copy()
                    tabla2 = pd.DataFrame({
                        "Fecha de Reporte": pd.to_datetime(df_logrados['fecha_reporte_cp']).dt.strftime('%d/%m/%Y'),
                        "Pozo": df_logrados['nombre_pozo'],
                        "Etapa Nro": df_logrados['nro_etapa'].fillna(0).astype(int),
                        "Secuencia Diaria": df_logrados['secuencia_diaria'].fillna(0).astype(int)
                    })
                    st.dataframe(tabla2, use_container_width=True, hide_index=True)

            # --- Pestaña 2: Resumen Gerencial ---
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
                    ultima_fecha = pd.to_datetime(df_pad_h9['fecha_reporte'].max()).strftime('%d/%m/%Y')
                    
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
                
                st.dataframe(pd.DataFrame(resumen_cp), use_container_width=True, hide_index=True)

    except Exception as e:
        st.error(f"❌ Ocurrió un error al cargar los datos desde la nube. Revisa que el enlace sea correcto y público. Detalles: {e}")

else:
    st.info("👈 Por favor, pega el enlace de Google Drive / Google Sheets en el código (variable LINK_GOOGLE) para visualizar los datos.")