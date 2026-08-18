#### APP APB 


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
st.set_page_config(page_title="Dashboard Operativo", layout="wide", initial_sidebar_state="expanded")

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
# PARÁMETROS STD Y URLs
# ==========================================
PARAMETROS_STD = {
    "FORTIN DE PIEDRA": {"Etapas_Dia_STD": 7.6, "Setupf_STD_min": 21.0, "Ramp_STD_min": 8.0, "Frac_STD_min": 122.0},
    "LOS TOLDOS ESTE": {"Etapas_Dia_STD": 8.7, "Setupf_STD_min": 17.0, "Ramp_STD_min": 5.0, "Frac_STD_min": 119.0},
    "Default": {"Etapas_Dia_STD": 7.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0, "Frac_STD_min": 120.0}
}

URL_TIEMPOS = "https://docs.google.com/spreadsheets/d/171LD-isnq1p9M9_H8sPSZIG8_s9El2WC/export?format=xlsx"
URL_CONTINUO = "https://docs.google.com/spreadsheets/d/1XHPj3S5RW8KRT4IquPREb5Cng3QtEU3C/export?format=xlsx"

# ==========================================
# MOTOR DE DESCARGA Y PROCESAMIENTO APB
# ==========================================
@st.cache_data(ttl=600, show_spinner=False)
def descargar_y_procesar(url_tiempos, url_continuo):
    # --- AUTENTICACIÓN GOOGLE ---
    credenciales_dict = json.loads(st.secrets["gcp_json"])
    credentials = service_account.Credentials.from_service_account_info(
        credenciales_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    token = credentials.token
    headers = {"Authorization": f"Bearer {token}"}
    
    # --- DESCARGA DE ARCHIVOS ---
    resp_t = requests.get(url_tiempos, headers=headers)
    resp_c = requests.get(url_continuo, headers=headers)
    
    if resp_t.status_code != 200 or resp_c.status_code != 200:
        st.error("❌ Error descargando los archivos base desde Google Drive. Verificá que el bot tenga permisos de Lector en ambos archivos.")
        st.stop()
        
    file_tiempos = io.BytesIO(resp_t.content)
    file_continuo = io.BytesIO(resp_c.content)

    # ---------------------------------------------------------
    # BLOQUE 1: PROCESAMIENTO DE LA BITÁCORA OPERATIVA (TIEMPOS)
    # ---------------------------------------------------------
    df_tiempos = pd.read_excel(file_tiempos).dropna(how='all')
    df_tiempos.columns = df_tiempos.columns.str.strip()

    # Blindaje si faltan las columnas de ubicación
    if 'yacimiento' not in df_tiempos.columns: df_tiempos['yacimiento'] = "S/D"
    if 'nombre_pad' not in df_tiempos.columns: df_tiempos['nombre_pad'] = "S/D"

    for col in ['fecha_inicio', 'fecha_fin']:
        df_tiempos[col] = pd.to_datetime(df_tiempos[col], errors='coerce')

    df_tiempos = df_tiempos.sort_values(by=['nombre_pozo', 'fecha_inicio']).reset_index(drop=True)
    df_tiempos['duracion_minutos'] = (df_tiempos['fecha_fin'] - df_tiempos['fecha_inicio']).dt.total_seconds() / 60

    falsos_npt = ['N/A', 'NA', 'NONE', '0', '-', 'FALSO', 'FALSE', 'NO', 'NAN']
    if 'npt_clase' in df_tiempos.columns:
        npt_texto = df_tiempos['npt_clase'].astype(str).str.strip().str.upper()
        df_tiempos['es_npt'] = df_tiempos['npt_clase'].notna() & (npt_texto != '') & (~npt_texto.isin(falsos_npt))
    else:
        df_tiempos['es_npt'] = False

    columnas_posibles = ['fase', 'codigo', 'actividad', 'adicional', 'evento']
    columnas_existentes = [col for col in columnas_posibles if col in df_tiempos.columns]
    texto_global = df_tiempos[columnas_existentes].fillna('').astype(str).agg(' '.join, axis=1).str.upper()

    condiciones = [
        texto_global.str.contains('SETUP', na=False),
        texto_global.str.contains('RAMP', na=False),
        texto_global.str.contains('FRAC', na=False)
    ]
    df_tiempos['fase_asignada'] = np.select(condiciones, ['SETUPF', 'RAMP', 'FRAC'], default='OTROS')

    df_tiempos['fase_limpia'] = np.where(df_tiempos['es_npt'], np.nan, df_tiempos['fase_asignada'])
    fase_limpia_anterior = df_tiempos.groupby('nombre_pozo')['fase_limpia'].ffill().shift(1)

    cambio_pozo = df_tiempos['nombre_pozo'] != df_tiempos['nombre_pozo'].shift(1)
    reinicio_secuencia = ((df_tiempos['fase_asignada'].isin(['SETUPF', 'RAMP'])) & (fase_limpia_anterior == 'FRAC') & (~df_tiempos['es_npt']))

    df_tiempos['inicio_etapa'] = cambio_pozo | reinicio_secuencia
    df_tiempos['nro_etapa_inferido'] = df_tiempos.groupby('nombre_pozo')['inicio_etapa'].cumsum()

    df_ops_netas = df_tiempos[~df_tiempos['es_npt']].copy()
    df_secuencia = df_ops_netas.pivot_table(index=['nombre_pozo', 'nro_etapa_inferido'], columns='fase_asignada', values='duracion_minutos', aggfunc='sum', fill_value=0).reset_index()

    for col in ['SETUPF', 'RAMP', 'FRAC']:
        if col not in df_secuencia.columns: df_secuencia[col] = 0

    if 'nro_etapa' in df_tiempos.columns:
        mapa_etapas = df_tiempos[df_tiempos['nro_etapa'].notna() & (df_tiempos['nro_etapa'].astype(str).str.strip() != '')]
        mapa_etapas = mapa_etapas.groupby(['nombre_pozo', 'nro_etapa_inferido'])['nro_etapa'].last().reset_index()
        df_secuencia = pd.merge(df_secuencia, mapa_etapas, on=['nombre_pozo', 'nro_etapa_inferido'], how='left')
    else:
        df_secuencia['nro_etapa'] = df_secuencia['nro_etapa_inferido']

    df_secuencia = df_secuencia[['nombre_pozo', 'nro_etapa', 'nro_etapa_inferido', 'SETUPF', 'RAMP', 'FRAC']]

    fecha_base_inicio = (df_tiempos['fecha_inicio'] - pd.Timedelta(hours=6) + pd.Timedelta(days=1)).dt.date
    fecha_base_fin = (df_tiempos['fecha_fin'] - pd.Timedelta(hours=6, seconds=1) + pd.Timedelta(days=1)).dt.date
    df_tiempos['fecha_reporte'] = pd.to_datetime(np.where(df_tiempos['fase_asignada'] == 'FRAC', fecha_base_fin, fecha_base_inicio))
    df_tiempos['fase_para_resumen'] = np.where(df_tiempos['es_npt'], 'NPT', df_tiempos['fase_asignada'])

    df_resumen_diario = df_tiempos.pivot_table(index='fecha_reporte', columns='fase_para_resumen', values='duracion_minutos', aggfunc='sum', fill_value=0).reset_index()

    for col in ['SETUPF', 'RAMP', 'FRAC', 'NPT']:
        if col not in df_resumen_diario.columns: df_resumen_diario[col] = 0

    if 'nro_etapa' in df_tiempos.columns:
        etapas_cerradas = df_tiempos[df_tiempos['nro_etapa'].notna() & (df_tiempos['nro_etapa'].astype(str).str.strip() != '')].copy()
        etapas_cerradas['id_cierre'] = etapas_cerradas['nombre_pozo'].astype(str) + "_" + etapas_cerradas['nro_etapa'].astype(str)
        etapas_por_dia = etapas_cerradas.groupby('fecha_reporte')['id_cierre'].nunique().reset_index()
        etapas_por_dia.rename(columns={'id_cierre': 'cantidad_etapas'}, inplace=True)
    else:
        etapas_por_dia = pd.DataFrame(columns=['fecha_reporte', 'cantidad_etapas'])

    df_resumen_diario = pd.merge(df_resumen_diario, etapas_por_dia, on='fecha_reporte', how='left')
    df_resumen_diario['cantidad_etapas'] = df_resumen_diario['cantidad_etapas'].fillna(0).astype(int)

    # # ---------------------------------------------------------
    # # BLOQUE 2: PROCESAMIENTO TÉCNICO Y AUDITORÍA CP (HOJA 9)
    # # ---------------------------------------------------------
    # df_continuo = pd.read_excel(file_continuo).dropna(how='all')
    # df_continuo.columns = df_continuo.columns.str.strip()

    # for col in ['fecha_hora_inicio', 'fecha_hora_caudal_70', 'fecha_hora_fin']:
    #     df_continuo[col] = pd.to_datetime(df_continuo[col], errors='coerce')

    # df_continuo = df_continuo.sort_values(by=['fecha_hora_inicio']).reset_index(drop=True)

    # df_continuo['Inicio_a_70_min'] = (df_continuo['fecha_hora_caudal_70'] - df_continuo['fecha_hora_inicio']).dt.total_seconds() / 60
    # df_continuo['70_a_Fin_min'] = (df_continuo['fecha_hora_fin'] - df_continuo['fecha_hora_caudal_70']).dt.total_seconds() / 60
    # df_continuo['Bombeo_Total_min'] = (df_continuo['fecha_hora_fin'] - df_continuo['fecha_hora_inicio']).dt.total_seconds() / 60

    # df_continuo['fecha_reporte'] = pd.to_datetime((df_continuo['fecha_hora_fin'] - pd.Timedelta(hours=6, seconds=1) + pd.Timedelta(days=1)).dt.date)
    # df_continuo['secuencia_diaria'] = df_continuo.groupby('fecha_reporte').cumcount() + 1

    # def encontrar_columna(df, texto_buscado):
    #     for col in df.columns:
    #         if texto_buscado.lower() in str(col).lower(): return col
    #     return None

    # col_cp = encontrar_columna(df_continuo, 'continuous')
    # col_sweep = encontrar_columna(df_continuo, 'sweep')
    # col_screenout = encontrar_columna(df_continuo, 'screen')
    # cols_operativas = [c for c in [col_cp, col_sweep, col_screenout] if c is not None]

    # if col_cp is not None:
    #     cp_texto = df_continuo[col_cp].astype(str).str.strip().str.upper()
    #     df_continuo['es_cp'] = cp_texto.isin(['1', 'SI', 'YES', 'TRUE', 'V', 'X'])
    # else:
    #     df_continuo['es_cp'] = False
    #     df_continuo['continuous_pumping'] = "N/A"
    #     col_cp = 'continuous_pumping'

    # # --- CREACIÓN DE VARIABLES DE TRANSICIÓN ---
    # df_continuo['pozo_etapa_actual'] = df_continuo['nombre_pozo'].astype(str) + " Etapa " + df_continuo['nro_etapa'].astype(str)
    # df_continuo['pozo_etapa_anterior'] = df_continuo['pozo_etapa_actual'].shift(1).fillna('Inicio Operaciones')
    # df_continuo['transicion_cp'] = np.where(df_continuo['es_cp'], df_continuo['pozo_etapa_anterior'] + " -> " + df_continuo['pozo_etapa_actual'], "")

    # # --- INICIO NUEVA LÓGICA DE CP (CON PRESIONES) ---
    # # 1. Blindaje: Asegurar que las columnas existan por si suben un Excel viejo
    # if 'presion_final_3m [psi]' not in df_continuo.columns:
    #     df_continuo['presion_final_3m [psi]'] = np.nan
    # if 'isip_post_frac [psi]' not in df_continuo.columns:
    #     df_continuo['isip_post_frac [psi]'] = np.nan

    # # 2. Calcula la diferencia de tiempo
    # df_continuo['fecha_hora_fin_anterior'] = df_continuo['fecha_hora_fin'].shift(1)
    # df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] = (df_continuo['fecha_hora_inicio'] - df_continuo['fecha_hora_fin_anterior']).dt.total_seconds() / 60

    # # 3. Traemos las presiones de la etapa ANTERIOR (la que acaba de terminar)
    # df_continuo['presion_3m_anterior'] = df_continuo['presion_final_3m [psi]'].shift(1)
    # df_continuo['isip_anterior'] = df_continuo['isip_post_frac [psi]'].shift(1)

    # # 4. Limpiamos las presiones para ver si realmente están vacías
    # no_hay_p3m = pd.to_numeric(df_continuo['presion_3m_anterior'], errors='coerce').fillna(0) == 0
    # no_hay_isip = pd.to_numeric(df_continuo['isip_anterior'], errors='coerce').fillna(0) == 0

    # # 5. Lógica dura combinada: Tiempo <= 5 min Y NO hay P3m Y NO hay ISIP
    # condicion_tiempo = df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'].notna() & (df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5)
    # df_continuo['es_cp_tecnico'] = condicion_tiempo & no_hay_p3m & no_hay_isip
    
    # # 6. Auditoría: Compara la marca manual de la operadora vs la realidad técnica
    # df_continuo['Esta_cargado_correctamente?'] = np.where(df_continuo['es_cp'] == df_continuo['es_cp_tecnico'], 'Si', 'No')
    # df_continuo.loc[0, 'Esta_cargado_correctamente?'] = 'Si' # La primera etapa no tiene etapa anterior

    # # 7. Genera el texto de la transición solo si fue un CP exitoso
    # df_continuo['transicion_cp_con_nueva_logica_chequeo'] = np.where(df_continuo['es_cp_tecnico'], df_continuo['pozo_etapa_anterior'] + " -> " + df_continuo['pozo_etapa_actual'], "")
    
    # columnas_h9 = ['fecha_reporte', 'secuencia_diaria', 'transicion_cp', 'fecha_hora_inicio', 'fecha_hora_fin', col_cp, 'Tiempo_entre_fin_e_inicio_de_nueva_fractura', 'es_cp_tecnico', 'Esta_cargado_correctamente?', 'transicion_cp_con_nueva_logica_chequeo', 'presion_final_3m [psi]', 'isip_post_frac [psi]']    
    # cols_h9_final = [c for c in columnas_h9 if c in df_continuo.columns] + ['nombre_pozo', 'nro_etapa']
    # df_hoja9 = df_continuo[cols_h9_final].copy()
    # if 'Tiempo_entre_fin_e_inicio_de_nueva_fractura' in df_hoja9.columns:
    #     df_hoja9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] = df_hoja9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'].round(2)

    # df_resumen_cp = df_continuo.groupby('fecha_reporte').agg(etapas_totales=('nro_etapa', 'count'), etapas_continuous_pumping=('es_cp', 'sum')).reset_index()
    
    # ---------------------------------------------------------
    # BLOQUE 2: PROCESAMIENTO TÉCNICO Y AUDITORÍA CP (HOJA 9)
    # ---------------------------------------------------------
    df_continuo = pd.read_excel(file_continuo).dropna(how='all')
    df_continuo.columns = df_continuo.columns.str.strip()

    for col in ['fecha_hora_inicio', 'fecha_hora_caudal_70', 'fecha_hora_fin']:
        df_continuo[col] = pd.to_datetime(df_continuo[col], errors='coerce')

    df_continuo = df_continuo.sort_values(by=['fecha_hora_inicio']).reset_index(drop=True)

    df_continuo['Inicio_a_70_min'] = (df_continuo['fecha_hora_caudal_70'] - df_continuo['fecha_hora_inicio']).dt.total_seconds() / 60
    df_continuo['70_a_Fin_min'] = (df_continuo['fecha_hora_fin'] - df_continuo['fecha_hora_caudal_70']).dt.total_seconds() / 60
    df_continuo['Bombeo_Total_min'] = (df_continuo['fecha_hora_fin'] - df_continuo['fecha_hora_inicio']).dt.total_seconds() / 60

    df_continuo['fecha_reporte'] = pd.to_datetime((df_continuo['fecha_hora_fin'] - pd.Timedelta(hours=6, seconds=1) + pd.Timedelta(days=1)).dt.date)
    df_continuo['secuencia_diaria'] = df_continuo.groupby('fecha_reporte').cumcount() + 1

    def encontrar_columna(df, texto_buscado):
        for col in df.columns:
            if texto_buscado.lower() in str(col).lower(): return col
        return None

    col_cp = encontrar_columna(df_continuo, 'continuous')
    col_sweep = encontrar_columna(df_continuo, 'sweep')
    col_screenout = encontrar_columna(df_continuo, 'screen')
    cols_operativas = [c for c in [col_cp, col_sweep, col_screenout] if c is not None]

    if col_cp is not None:
        cp_texto = df_continuo[col_cp].astype(str).str.strip().str.upper()
        df_continuo['es_cp'] = cp_texto.isin(['1', 'SI', 'YES', 'TRUE', 'V', 'X'])
    else:
        df_continuo['es_cp'] = False
        df_continuo['continuous_pumping'] = "N/A"
        col_cp = 'continuous_pumping'

    # --- CREACIÓN DE VARIABLES DE TRANSICIÓN ---
    df_continuo['pozo_etapa_actual'] = df_continuo['nombre_pozo'].astype(str) + " Etapa " + df_continuo['nro_etapa'].astype(str)
    df_continuo['pozo_etapa_anterior'] = df_continuo['pozo_etapa_actual'].shift(1).fillna('Inicio Operaciones')
    df_continuo['transicion_cp'] = np.where(df_continuo['es_cp'], df_continuo['pozo_etapa_anterior'] + " -> " + df_continuo['pozo_etapa_actual'], "")

    # --- INICIO NUEVA LÓGICA DE CP (CON PRESIONES) ---
    # 1. Blindaje: Asegurar que las columnas existan por si suben un Excel viejo
    if 'presion_final_3m [psi]' not in df_continuo.columns:
        df_continuo['presion_final_3m [psi]'] = np.nan
    if 'isip_post_frac [psi]' not in df_continuo.columns:
        df_continuo['isip_post_frac [psi]'] = np.nan

    # 2. Calcula la diferencia de tiempo
    df_continuo['fecha_hora_fin_anterior'] = df_continuo['fecha_hora_fin'].shift(1)
    df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] = (df_continuo['fecha_hora_inicio'] - df_continuo['fecha_hora_fin_anterior']).dt.total_seconds() / 60

    # 3. Traemos las presiones de la etapa ANTERIOR (la que acaba de terminar)
    df_continuo['presion_3m_anterior'] = df_continuo['presion_final_3m [psi]'].shift(1)
    df_continuo['isip_anterior'] = df_continuo['isip_post_frac [psi]'].shift(1)

    # 4. Limpiamos las presiones para ver si realmente están vacías
    no_hay_p3m = pd.to_numeric(df_continuo['presion_3m_anterior'], errors='coerce').fillna(0) == 0
    no_hay_isip = pd.to_numeric(df_continuo['isip_anterior'], errors='coerce').fillna(0) == 0

    # 5. Lógica dura combinada: Tiempo <= 5 min Y NO hay P3m Y NO hay ISIP
    condicion_tiempo = df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'].notna() & (df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] <= 5)
    df_continuo['es_cp_tecnico'] = condicion_tiempo & no_hay_p3m & no_hay_isip
    
    # 6. Auditoría: Compara la marca manual de la operadora vs la realidad técnica
    df_continuo['Esta_cargado_correctamente?'] = np.where(df_continuo['es_cp'] == df_continuo['es_cp_tecnico'], 'Si', 'No')
    df_continuo.loc[0, 'Esta_cargado_correctamente?'] = 'Si' # La primera etapa no tiene etapa anterior

    # 7. Genera el texto de la transición solo si fue un CP exitoso
    df_continuo['transicion_cp_con_nueva_logica_chequeo'] = np.where(df_continuo['es_cp_tecnico'], df_continuo['pozo_etapa_anterior'] + " -> " + df_continuo['pozo_etapa_actual'], "")
    
    columnas_h9 = ['fecha_reporte', 'secuencia_diaria', 'transicion_cp', 'fecha_hora_inicio', 'fecha_hora_fin', col_cp, 'Tiempo_entre_fin_e_inicio_de_nueva_fractura', 'es_cp_tecnico', 'Esta_cargado_correctamente?', 'transicion_cp_con_nueva_logica_chequeo', 'presion_final_3m [psi]', 'isip_post_frac [psi]']    
    cols_h9_final = [c for c in columnas_h9 if c in df_continuo.columns] + ['nombre_pozo', 'nro_etapa']
    df_hoja9 = df_continuo[cols_h9_final].copy()
    if 'Tiempo_entre_fin_e_inicio_de_nueva_fractura' in df_hoja9.columns:
        df_hoja9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] = df_hoja9['Tiempo_entre_fin_e_inicio_de_nueva_fractura'].round(2)

    df_resumen_cp = df_continuo.groupby('fecha_reporte').agg(etapas_totales=('nro_etapa', 'count'), etapas_continuous_pumping=('es_cp', 'sum')).reset_index()



    # ---------------------------------------------------------
    # BLOQUE 3: BASES MAESTRAS (CRUCE Y QA/QC)
    # ---------------------------------------------------------
    df_secuencia['nro_etapa_str'] = df_secuencia['nro_etapa'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
    df_continuo['nro_etapa_str'] = df_continuo['nro_etapa'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()

    columnas_tecnicas_a_cruzar = ['nombre_pozo', 'nro_etapa_str', 'fecha_reporte', 'secuencia_diaria', 'Inicio_a_70_min', '70_a_Fin_min', 'Bombeo_Total_min', 'transicion_cp'] + cols_operativas

    df_qaqc = pd.merge(df_secuencia[['nombre_pozo', 'nro_etapa_str', 'SETUPF', 'RAMP', 'FRAC']], df_continuo[columnas_tecnicas_a_cruzar], on=['nombre_pozo', 'nro_etapa_str'], how='outer')
    df_qaqc.rename(columns={'nro_etapa_str': 'Etapa', 'SETUPF': 'Tiempos_SETUPF_min', 'RAMP': 'Tiempos_RAMP_min', 'FRAC': 'Tiempos_FRAC_min', 'Inicio_a_70_min': 'Tecnico_Inicio_a_70_min', '70_a_Fin_min': 'Tecnico_70_a_Fin_min'}, inplace=True)

    if 'Tiempos_RAMP_min' in df_qaqc.columns and 'Tecnico_Inicio_a_70_min' in df_qaqc.columns: df_qaqc['Delta_RAMP_min'] = (df_qaqc['Tiempos_RAMP_min'] - df_qaqc['Tecnico_Inicio_a_70_min']).round(2)
    if 'Tiempos_FRAC_min' in df_qaqc.columns and 'Tecnico_70_a_Fin_min' in df_qaqc.columns: df_qaqc['Delta_FRAC_min'] = (df_qaqc['Tiempos_FRAC_min'] - df_qaqc['Tecnico_70_a_Fin_min']).round(2)

    df_master_diario = pd.merge(df_resumen_diario, df_resumen_cp, on='fecha_reporte', how='outer')

    # ---------------------------------------------------------
    # BLOQUE 5: NUEVA ESTRUCTURA HOJA 8 
    # ---------------------------------------------------------
    resumen_pivot = df_tiempos.pivot_table(index='fecha_reporte', columns=['fase_asignada', 'es_npt'], values='duracion_minutos', aggfunc='sum', fill_value=0)
    resumen_pivot.columns = [f"{fase}_{'NPT' if npt else 'Neto'}_min" for fase, npt in resumen_pivot.columns]
    resumen_pivot = resumen_pivot.reset_index()

    for fase in ['SETUPF', 'RAMP', 'FRAC', 'OTROS']:
        if f"{fase}_Neto_min" not in resumen_pivot: resumen_pivot[f"{fase}_Neto_min"] = 0
        if f"{fase}_NPT_min" not in resumen_pivot: resumen_pivot[f"{fase}_NPT_min"] = 0

    df_hoja8 = pd.merge(resumen_pivot, etapas_por_dia, on='fecha_reporte', how='left')
    df_hoja8['cantidad_etapas'] = df_hoja8['cantidad_etapas'].fillna(0).astype(int)

    df_hoja8['NPT Total min'] = (df_hoja8['SETUPF_NPT_min'] + df_hoja8['RAMP_NPT_min'] + df_hoja8['FRAC_NPT_min'] + df_hoja8['OTROS_NPT_min']).round(2)

    for fase in ['SETUPF', 'RAMP', 'FRAC']:
        df_hoja8[f"{fase} Sin NPT min"] = df_hoja8[f"{fase}_Neto_min"].round(2)
        df_hoja8[f"{fase} Con NPT min"] = (df_hoja8[f"{fase}_Neto_min"] + df_hoja8[f"{fase}_NPT_min"]).round(2)
        df_hoja8[f"{fase} Sin NPT hrs"] = (df_hoja8[f"{fase} Sin NPT min"] / 60).round(2)
        df_hoja8[f"{fase} Con NPT hrs"] = (df_hoja8[f"{fase} Con NPT min"] / 60).round(2)
        
        df_hoja8[f"Promedio {fase} Sin NPT min"] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8[f"{fase} Sin NPT min"] / df_hoja8['cantidad_etapas']).round(2), 0)
        df_hoja8[f"Promedio {fase} Con NPT min"] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8[f"{fase} Con NPT min"] / df_hoja8['cantidad_etapas']).round(2), 0)

    df_hoja8['SETUPF NPT min'] = df_hoja8['SETUPF_NPT_min'].round(2)
    df_hoja8['RAMP NPT min'] = df_hoja8['RAMP_NPT_min'].round(2)
    df_hoja8['FRAC NPT min'] = df_hoja8['FRAC_NPT_min'].round(2)
    df_hoja8['OTROS NPT min'] = df_hoja8['OTROS_NPT_min'].round(2)

    df_hoja8['NPT Promedio (min)'] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8['NPT Total min'] / df_hoja8['cantidad_etapas']).round(2), 0)
    df_hoja8['SETUPF NPT Promedio (min)'] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8['SETUPF NPT min'] / df_hoja8['cantidad_etapas']).round(2), 0)
    df_hoja8['RAMP NPT Promedio (min)'] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8['RAMP NPT min'] / df_hoja8['cantidad_etapas']).round(2), 0)
    df_hoja8['FRAC NPT Promedio (min)'] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8['FRAC NPT min'] / df_hoja8['cantidad_etapas']).round(2), 0)
    df_hoja8['OTROS NPT Promedio (min)'] = np.where(df_hoja8['cantidad_etapas'] > 0, (df_hoja8['OTROS NPT min'] / df_hoja8['cantidad_etapas']).round(2), 0)

    df_hoja8['Promedio PAD SETUPF Sin NPT (min)'] = [f"=AVERAGE(G$2:G{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['Promedio PAD SETUPF Con NPT (min)'] = [f"=AVERAGE(H$2:H{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['Promedio PAD RAMP Sin NPT (min)'] = [f"=AVERAGE(M$2:M{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['Promedio PAD RAMP Con NPT (min)'] = [f"=AVERAGE(N$2:N{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['Promedio PAD FRAC Sin NPT (min)'] = [f"=AVERAGE(S$2:S{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['Promedio PAD FRAC Con NPT (min)'] = [f"=AVERAGE(T$2:T{i+2})" for i in range(len(df_hoja8))]

    df_hoja8['NPT Promedio PAD (min)'] = [f"=AVERAGE(AF$2:AF{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['SETUPF NPT Promedio PAD (min)'] = [f"=AVERAGE(AG$2:AG{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['RAMP NPT Promedio PAD (min)'] = [f"=AVERAGE(AH$2:AH{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['FRAC NPT Promedio PAD (min)'] = [f"=AVERAGE(AI$2:AI{i+2})" for i in range(len(df_hoja8))]
    df_hoja8['OTROS NPT Promedio PAD (min)'] = [f"=AVERAGE(AJ$2:AJ{i+2})" for i in range(len(df_hoja8))]

    columnas_h8 = [
        'fecha_reporte', 'cantidad_etapas',
        'SETUPF Sin NPT min', 'SETUPF Con NPT min', 'SETUPF Sin NPT hrs', 'SETUPF Con NPT hrs', 'Promedio SETUPF Sin NPT min', 'Promedio SETUPF Con NPT min',
        'RAMP Sin NPT min', 'RAMP Con NPT min', 'RAMP Sin NPT hrs', 'RAMP Con NPT hrs', 'Promedio RAMP Sin NPT min', 'Promedio RAMP Con NPT min',
        'FRAC Sin NPT min', 'FRAC Con NPT min', 'FRAC Sin NPT hrs', 'FRAC Con NPT hrs', 'Promedio FRAC Sin NPT min', 'Promedio FRAC Con NPT min',
        'NPT Total min', 'SETUPF NPT min', 'RAMP NPT min', 'FRAC NPT min', 'OTROS NPT min',
        'Promedio PAD SETUPF Sin NPT (min)', 'Promedio PAD SETUPF Con NPT (min)',
        'Promedio PAD RAMP Sin NPT (min)', 'Promedio PAD RAMP Con NPT (min)',
        'Promedio PAD FRAC Sin NPT (min)', 'Promedio PAD FRAC Con NPT (min)',
        'NPT Promedio (min)', 'SETUPF NPT Promedio (min)', 'RAMP NPT Promedio (min)', 
        'FRAC NPT Promedio (min)', 'OTROS NPT Promedio (min)',
        'NPT Promedio PAD (min)', 'SETUPF NPT Promedio PAD (min)', 
        'RAMP NPT Promedio PAD (min)', 'FRAC NPT Promedio PAD (min)', 'OTROS NPT Promedio PAD (min)'
    ]
    df_hoja8 = df_hoja8[columnas_h8].sort_values(by='fecha_reporte')
    df_hoja8.rename(columns={'fecha_reporte': 'fecha reporte', 'cantidad_etapas': 'cantidad etapas'}, inplace=True)

    # ---------------------------------------------------------
    # BLOQUE 6: EXPORTACIÓN EN MEMORIA (EXCEL PARA DESCARGA)
    # ---------------------------------------------------------
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_secuencia.drop(columns=['nro_etapa_str'], errors='ignore').to_excel(writer, sheet_name='1_Secuencia_Neta', index=False)
        cols_drop_t = ['fase_limpia', 'inicio_etapa', 'fase_para_resumen']
        df_tiempos.drop(columns=[c for c in cols_drop_t if c in df_tiempos.columns]).to_excel(writer, sheet_name='2_Base_Mapeada_Tiempos', index=False)
        df_resumen_diario.to_excel(writer, sheet_name='3_Resumen_Diario_Tiempos', index=False)
        
        cols_drop_c = ['es_cp', 'pozo_etapa_actual', 'pozo_etapa_anterior', 'nro_etapa_str', 'fecha_hora_fin_anterior', 'Tiempo_entre_fin_e_inicio_de_nueva_fractura', 'es_cp_tecnico', 'Esta_cargado_correctamente?', 'transicion_cp_con_nueva_logica_chequeo']
        df_continuo_clean = df_continuo.drop(columns=[c for c in cols_drop_c if c in df_continuo.columns])
        columnas_frente_4 = ['fecha_reporte', 'secuencia_diaria', 'transicion_cp'] + cols_operativas
        columnas_resto_4 = [c for c in df_continuo_clean.columns if c not in columnas_frente_4]
        
        df_continuo_clean[[c for c in columnas_frente_4 + columnas_resto_4 if c in df_continuo_clean.columns]].to_excel(writer, sheet_name='4_Detalle_Tiempos_Bombeo', index=False)
        df_resumen_cp.to_excel(writer, sheet_name='5_Resumen_CP_Diario', index=False)
        df_qaqc.to_excel(writer, sheet_name='6_QAQC_Tiempos_vs_Tecnico', index=False)
        df_master_diario.to_excel(writer, sheet_name='7_Resumen_Diario_Global', index=False)
        df_hoja8.to_excel(writer, sheet_name='8_Comparativa_y_NPTs', index=False)
        df_hoja9.to_excel(writer, sheet_name='9_Revision_Continuous_Pumping', index=False)
    
    excel_bytes = output.getvalue()

    # ---------------------------------------------------------
    # PREPARACIÓN PARA EL DASHBOARD UI (HOMOLOGACIÓN H2, H8, H9)
    # ---------------------------------------------------------
    h2 = df_tiempos.copy()
    h8 = df_hoja8.copy()
    h9 = df_hoja9.copy()

    if 'yacimiento' in h2.columns: h2.rename(columns={'yacimiento': 'Yacimiento'}, inplace=True)
    if 'nombre_pad' in h2.columns: h2.rename(columns={'nombre_pad': 'PAD'}, inplace=True)
    
    if 'fecha_reporte' in h2.columns and not h2['fecha_reporte'].dropna().empty:
        mapa_ubicacion = h2.dropna(subset=['fecha_reporte']).groupby('fecha_reporte')[['Yacimiento', 'PAD']].first().reset_index()
        if 'fecha reporte' in h8.columns:
            h8 = pd.merge(h8, mapa_ubicacion, left_on='fecha reporte', right_on='fecha_reporte', how='left')
        h9 = pd.merge(h9, mapa_ubicacion, on='fecha_reporte', how='left')
    
    yac_dominante = h2['Yacimiento'].dropna().mode()[0] if ('Yacimiento' in h2.columns and not h2['Yacimiento'].dropna().empty) else "S/D"
    pad_dominante = h2['PAD'].dropna().mode()[0] if ('PAD' in h2.columns and not h2['PAD'].dropna().empty) else "S/D"

    for df_target in [h8, h9]:
        if 'Yacimiento' in df_target.columns: df_target['Yacimiento'] = df_target['Yacimiento'].fillna(yac_dominante)
        else: df_target['Yacimiento'] = yac_dominante
        if 'PAD' in df_target.columns: df_target['PAD'] = df_target['PAD'].fillna(pad_dominante)
        else: df_target['PAD'] = pad_dominante

    h9['nombre_pozo'] = h9['nombre_pozo'].fillna("Pozo S/D")
    if 'nro_etapa' in h9.columns: h9['nro_etapa'] = h9['nro_etapa'].fillna(0)

    if 'fecha_hora_inicio' in h9.columns:
        h9['fecha_reporte_cp'] = (h9['fecha_hora_inicio'] - pd.Timedelta(hours=6) + pd.Timedelta(days=1)).dt.date
        h9['fecha_reporte_cp'] = h9['fecha_reporte_cp'].fillna(h9['fecha_reporte'].dt.date)

    return h2, h8, h9, excel_bytes


# ==========================================
# INTERFAZ PRINCIPAL Y PANELES
# ==========================================
st.sidebar.title("⚙️ Panel de Control")

if st.sidebar.button("🔄 Actualizar Datos Ahora (Drive)"):
    st.cache_data.clear()
    st.rerun()

try:
    with st.spinner('Procesando datos en vivo desde Google Drive...'):
        df_h2, df_h8, df_h9, archivo_maestro_bytes = descargar_y_procesar(URL_TIEMPOS, URL_CONTINUO)
    
    st.sidebar.success("✅ Base de datos actualizada.")
    
    # Botón para descargar el Excel procesado final
    st.sidebar.divider()
    st.sidebar.markdown("### Exportar Resultados")
    st.sidebar.download_button(
        label="📥 Descargar Excel Maestro (Full)",
        data=archivo_maestro_bytes,
        file_name=f"Reporte_Maestro_Fractura_{datetime.now().strftime('%d_%m_%Y')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    st.sidebar.divider()

    zona_ar = pytz.timezone('America/Argentina/Buenos_Aires')
    hora_actual = datetime.now(zona_ar).strftime("%d/%m/%Y %H:%M hs")
    if 'fecha_fin' in df_h2.columns:
        ultima_op = pd.to_datetime(df_h2['fecha_fin'], errors='coerce').max()
        hora_op = ultima_op.strftime("%d/%m/%Y %H:%M hs") if pd.notnull(ultima_op) else "Sin datos"
    else:
        hora_op = "Sin datos"
    
    st.sidebar.info(f"⏱️ Última OP en pozo:\n{hora_op}")
    
    seccion = st.sidebar.radio("Navegación Principal", ["⏳ Sección 1: Tiempos", "🔄 Sección 2: Continuous Pumping"])
    
    # ==========================================
    # DASHBOARD: SECCIÓN 1 (TIEMPOS)
    # ==========================================
    if seccion == "⏳ Sección 1: Tiempos":
        st.title("⏳ Control Operativo de Tiempos y NPT")
        
        col_f1, col_f2 = st.columns(2)
        with col_f1: toggle_npt = st.radio("Inclusión de NPT:", ["Sin NPT", "Con NPT"], horizontal=True)
        with col_f2: toggle_unidad = st.radio("Unidad de Medida:", ["min", "hrs"], horizontal=True)
        
        tab1, tab2 = st.tabs(["📊 Pestaña 1 (Detalle Diario)", "📋 Pestaña 2 (Resumen Global)"])
        
        with tab1:
            # --- NUEVO: 3 Columnas para incluir el filtro de fecha ---
            col_s1, col_s2, col_s3 = st.columns([1, 1, 1])
            
            # --- NUEVO: Lógica del PAD por defecto ---
            if 'fecha reporte' in df_h8.columns and not df_h8.empty:
                idx_max_h8 = pd.to_datetime(df_h8['fecha reporte'], errors='coerce').idxmax()
                yac_def_t = df_h8.loc[idx_max_h8, 'Yacimiento'] if pd.notna(idx_max_h8) else "S/D"
                pad_def_t = df_h8.loc[idx_max_h8, 'PAD'] if pd.notna(idx_max_h8) else "S/D"
            else:
                yac_def_t, pad_def_t = "S/D", "S/D"

            yacimientos_disp = df_h8['Yacimiento'].dropna().unique().tolist() if 'Yacimiento' in df_h8.columns else ["S/D"]
            if not yacimientos_disp: yacimientos_disp = ["S/D"]
            
            yac_idx_t = yacimientos_disp.index(yac_def_t) if yac_def_t in yacimientos_disp else 0
            with col_s1: sel_yac_t1 = st.selectbox("Seleccionar Yacimiento (P1):", yacimientos_disp, index=yac_idx_t)
            
            pads_disp = df_h8[df_h8['Yacimiento'] == sel_yac_t1]['PAD'].dropna().unique().tolist() if 'PAD' in df_h8.columns else ["S/D"]
            if not pads_disp: pads_disp = ["S/D"]
            
            pad_idx_t = pads_disp.index(pad_def_t) if (sel_yac_t1 == yac_def_t and pad_def_t in pads_disp) else 0
            with col_s2: sel_pad_t1 = st.selectbox("Seleccionar PAD (P1):", pads_disp, index=pad_idx_t)
            
            col_fecha_h8 = 'fecha reporte' if 'fecha reporte' in df_h8.columns else 'fecha_reporte'
            df_t1 = df_h8[(df_h8['Yacimiento'] == sel_yac_t1) & (df_h8['PAD'] == sel_pad_t1)].sort_values(col_fecha_h8).copy()
            
            # --- NUEVO: Selector de Fechas interactivo ---
            if not df_t1.empty:
                min_date_t1 = pd.to_datetime(df_t1[col_fecha_h8]).min().date()
                max_date_t1 = pd.to_datetime(df_t1[col_fecha_h8]).max().date()
            else:
                min_date_t1, max_date_t1 = datetime.today().date(), datetime.today().date()
                
            with col_s3: 
                fechas_t1 = st.date_input("Filtrar Fechas (P1):", [min_date_t1, max_date_t1], min_value=min_date_t1, max_value=max_date_t1)
                
            # Manejo de error si el usuario selecciona solo un día
            if len(fechas_t1) == 2:
                f_inicio_t1, f_fin_t1 = fechas_t1
            else:
                f_inicio_t1, f_fin_t1 = fechas_t1[0], fechas_t1[0]
            # -----------------------------------------------
            
            suf_npt = "Con NPT" if toggle_npt == "Con NPT" else "Sin NPT"
            suf_und = toggle_unidad 
            factor_div = 60 if toggle_unidad == "hrs" else 1 
            
            acum_etapas = df_t1['cantidad etapas'].cumsum() if 'cantidad etapas' in df_t1.columns else 1
            prom_pad_setupf = df_t1[f'SETUPF {suf_npt} min'].cumsum() / acum_etapas if f'SETUPF {suf_npt} min' in df_t1.columns else 0
            prom_pad_ramp = df_t1[f'RAMP {suf_npt} min'].cumsum() / acum_etapas if f'RAMP {suf_npt} min' in df_t1.columns else 0
            prom_pad_frac = df_t1[f'FRAC {suf_npt} min'].cumsum() / acum_etapas if f'FRAC {suf_npt} min' in df_t1.columns else 0
            
            prom_pad_npt_total = df_t1['NPT Total min'].cumsum() / acum_etapas if 'NPT Total min' in df_t1.columns else 0
            prom_pad_npt_setupf = df_t1['SETUPF NPT min'].cumsum() / acum_etapas if 'SETUPF NPT min' in df_t1.columns else 0
            prom_pad_npt_ramp = df_t1['RAMP NPT min'].cumsum() / acum_etapas if 'RAMP NPT min' in df_t1.columns else 0
            prom_pad_npt_frac = df_t1['FRAC NPT min'].cumsum() / acum_etapas if 'FRAC NPT min' in df_t1.columns else 0
            
            st.subheader(f"Cuadro 1 - Tiempos Operativos ({suf_und}) - {sel_pad_t1}")
            try:
                cuadro1 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_t1[col_fecha_h8]).dt.strftime('%d/%m/%Y'),
                    "Cant. Etapas": df_t1['cantidad etapas'].fillna(0).astype(int),
                    "Setupf": df_t1[f'SETUPF {suf_npt} {suf_und}'].fillna(0).round(2) if f'SETUPF {suf_npt} {suf_und}' in df_t1.columns else (df_t1[f'SETUPF {suf_npt} min'].fillna(0)/factor_div).round(2),
                    "Setupf Prom 24hs": (df_t1[f'Promedio SETUPF {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Setupf Prom PAD": (prom_pad_setupf.fillna(0) / factor_div).round(2) if isinstance(prom_pad_setupf, pd.Series) else 0,
                    "Ramp": df_t1[f'RAMP {suf_npt} {suf_und}'].fillna(0).round(2) if f'RAMP {suf_npt} {suf_und}' in df_t1.columns else (df_t1[f'RAMP {suf_npt} min'].fillna(0)/factor_div).round(2),
                    "Ramp Prom 24hs": (df_t1[f'Promedio RAMP {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Ramp Prom PAD": (prom_pad_ramp.fillna(0) / factor_div).round(2) if isinstance(prom_pad_ramp, pd.Series) else 0,
                    "Frac": df_t1[f'FRAC {suf_npt} {suf_und}'].fillna(0).round(2) if f'FRAC {suf_npt} {suf_und}' in df_t1.columns else (df_t1[f'FRAC {suf_npt} min'].fillna(0)/factor_div).round(2),
                    "Frac Prom 24hs": (df_t1[f'Promedio FRAC {suf_npt} min'].fillna(0) / factor_div).round(2),
                    "Frac Prom PAD": (prom_pad_frac.fillna(0) / factor_div).round(2) if isinstance(prom_pad_frac, pd.Series) else 0
                })
                # --- NUEVO: Aplicamos el filtro visual al Cuadro 1 ---
                cuadro1['Fecha_Date'] = pd.to_datetime(cuadro1["Fecha de Reporte"], format='%d/%m/%Y').dt.date
                cuadro1_filtrado = cuadro1[(cuadro1['Fecha_Date'] >= f_inicio_t1) & (cuadro1['Fecha_Date'] <= f_fin_t1)].drop(columns=['Fecha_Date'])
                st.dataframe(cuadro1_filtrado, use_container_width=True, hide_index=True)
            except Exception as e:
                st.warning(f"⚠️ Hubo un problema al dibujar el Cuadro 1: {e}")

            st.subheader(f"Cuadro 2 - Desglose de NPT ({sel_pad_t1})")
            try:
                cuadro2 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_t1[col_fecha_h8]).dt.strftime('%d/%m/%Y'),
                    "Cant. Etapas": df_t1['cantidad etapas'].fillna(0).astype(int),
                    "NPT Total": (df_t1['NPT Total min'] / factor_div).fillna(0).round(2),
                    "NPT Prom 24hs": (df_t1['NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                    "NPT Prom PAD": (prom_pad_npt_total.fillna(0) / factor_div).round(2) if isinstance(prom_pad_npt_total, pd.Series) else 0,
                    "NPT Setupf": (df_t1['SETUPF NPT min'] / factor_div).fillna(0).round(2),
                    "NPT Setupf Prom 24hs": (df_t1['SETUPF NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                    "NPT Setupf Prom PAD": (prom_pad_npt_setupf.fillna(0) / factor_div).round(2) if isinstance(prom_pad_npt_setupf, pd.Series) else 0,
                    "NPT Ramp": (df_t1['RAMP NPT min'] / factor_div).fillna(0).round(2),
                    "NPT Ramp Prom 24hs": (df_t1['RAMP NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                    "NPT Ramp Prom PAD": (prom_pad_npt_ramp.fillna(0) / factor_div).round(2) if isinstance(prom_pad_npt_ramp, pd.Series) else 0,
                    "NPT Frac": (df_t1['FRAC NPT min'] / factor_div).fillna(0).round(2),
                    "NPT Frac Prom 24hs": (df_t1['FRAC NPT Promedio (min)'] / factor_div).fillna(0).round(2),
                    "NPT Frac Prom PAD": (prom_pad_npt_frac.fillna(0) / factor_div).round(2) if isinstance(prom_pad_npt_frac, pd.Series) else 0
                })
                # --- NUEVO: Aplicamos el filtro visual al Cuadro 2 ---
                cuadro2['Fecha_Date'] = pd.to_datetime(cuadro2["Fecha de Reporte"], format='%d/%m/%Y').dt.date
                cuadro2_filtrado = cuadro2[(cuadro2['Fecha_Date'] >= f_inicio_t1) & (cuadro2['Fecha_Date'] <= f_fin_t1)].drop(columns=['Fecha_Date'])
                st.dataframe(cuadro2_filtrado, use_container_width=True, hide_index=True)
            except Exception as e:
                pass

        with tab2:
            col_m1, col_m2 = st.columns(2)
            with col_m1: sel_yac_t2 = st.multiselect("Seleccionar Yacimiento(s) (P2):", yacimientos_disp, default=yacimientos_disp)
            pads_disp_t2 = df_h8[df_h8['Yacimiento'].isin(sel_yac_t2)]['PAD'].dropna().unique().tolist()
            with col_m2: sel_pad_t2 = st.multiselect("Seleccionar PAD(s) (P2):", pads_disp_t2, default=pads_disp_t2)
            
            df_t2 = df_h8[(df_h8['Yacimiento'].isin(sel_yac_t2)) & (df_h8['PAD'].isin(sel_pad_t2))]
            
            # ==========================================
            # CUADRO 1: COMPARATIVA MACRO POR PAD
            # ==========================================
            st.subheader("Cuadro 1 - Comparativa Macro vs. STD (por PAD)")
            resumen_macro = []
            for pad in sel_pad_t2:
                df_pad = df_t2[df_t2['PAD'] == pad]
                if df_pad.empty: continue
                yac = df_pad['Yacimiento'].iloc[0]
                std = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])
                
                etapas_totales = df_pad['cantidad etapas'].sum()
                setup_prom = df_pad['SETUPF Sin NPT min'].sum() / etapas_totales if etapas_totales > 0 else 0
                ramp_prom = df_pad['RAMP Sin NPT min'].sum() / etapas_totales if etapas_totales > 0 else 0
                frac_prom = df_pad['FRAC Sin NPT min'].sum() / etapas_totales if etapas_totales > 0 else 0
                npt_total_pad = df_pad['NPT Total min'].sum()
                npt_prom_pad = npt_total_pad / etapas_totales if etapas_totales > 0 else 0
                
                resumen_macro.append({
                    "Yacimiento": yac, 
                    "PAD": pad,
                    "Cantidad Etapas": etapas_totales,
                    "Cantidad Etapas STD": std.get("Etapas_Dia_STD", 0),
                    "Setupf Prom PAD (min)": round(setup_prom, 2),
                    "Setupf STD (min)": std.get("Setupf_STD_min", 0),
                    "Ramp Prom PAD (min)": round(ramp_prom, 2),
                    "Ramp STD (min)": std.get("Ramp_STD_min", 0),
                    "Frac Prom PAD (min)": round(frac_prom, 2),
                    "Frac STD (min)": std.get("Frac_STD_min", "N/A"),
                    "NPT Total PAD (min)": round(npt_total_pad, 2),
                    "NPT Prom PAD (min)": round(npt_prom_pad, 2)
                })
            st.dataframe(pd.DataFrame(resumen_macro), use_container_width=True, hide_index=True)

            # ==========================================
            # CUADRO 2: RESUMEN POR YACIMIENTO
            # ==========================================
            st.subheader("Cuadro 2 - Resumen por Yacimiento")
            resumen_yac_tiempos = []
            
            for yac in sel_yac_t2:
                # Nos aseguramos de sumar solo los PADs que están filtrados en pantalla
                pads_del_yac = [p for p in sel_pad_t2 if p in df_t2[df_t2['Yacimiento'] == yac]['PAD'].unique()]
                if not pads_del_yac: continue
                
                df_yac = df_t2[(df_t2['Yacimiento'] == yac) & (df_t2['PAD'].isin(pads_del_yac))]
                if df_yac.empty: continue
                
                std = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])
                
                etapas_totales_yac = df_yac['cantidad etapas'].sum()
                setup_prom_yac = df_yac['SETUPF Sin NPT min'].sum() / etapas_totales_yac if etapas_totales_yac > 0 else 0
                ramp_prom_yac = df_yac['RAMP Sin NPT min'].sum() / etapas_totales_yac if etapas_totales_yac > 0 else 0
                frac_prom_yac = df_yac['FRAC Sin NPT min'].sum() / etapas_totales_yac if etapas_totales_yac > 0 else 0
                npt_total_yac = df_yac['NPT Total min'].sum()
                npt_prom_yac = npt_total_yac / etapas_totales_yac if etapas_totales_yac > 0 else 0
                
                resumen_yac_tiempos.append({
                    "Yacimiento": yac,
                    "Cantidad Etapas": etapas_totales_yac,
                    "Cantidad Etapas STD": std.get("Etapas_Dia_STD", 0),
                    "Setupf Prom Yac (min)": round(setup_prom_yac, 2),
                    "Setupf STD (min)": std.get("Setupf_STD_min", 0),
                    "Ramp Prom Yac (min)": round(ramp_prom_yac, 2),
                    "Ramp STD (min)": std.get("Ramp_STD_min", 0),
                    "Frac Prom Yac (min)": round(frac_prom_yac, 2),
                    "Frac STD (min)": std.get("Frac_STD_min", "N/A"),
                    "NPT Total Yac (min)": round(npt_total_yac, 2),
                    "NPT Prom Yac (min)": round(npt_prom_yac, 2)
                })
                
            st.dataframe(pd.DataFrame(resumen_yac_tiempos), use_container_width=True, hide_index=True)

    # ==========================================
    # DASHBOARD: SECCIÓN 2 (CONTINUOUS PUMPING)
    # ==========================================
    elif seccion == "🔄 Sección 2: Continuous Pumping":
        st.title("🔄 Continuous Pumping")
        
        # --- LÓGICA 1: CORTADOR DE GALLETAS (Pumping vs Pumping & Pumping vs NPT) ---
        def generar_fragmentos_visuales(df_raw, df_npt):
            df_cl = df_raw.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin']).copy()
            if df_cl.empty: return pd.DataFrame()
            
            # 1. Pumping vs Pumping (Etapas cortas perforan etapas largas = Zipper Frac real)
            df_cl['duration'] = (df_cl['fecha_hora_fin'] - df_cl['fecha_hora_inicio']).dt.total_seconds()
            records = df_cl.sort_values('duration').to_dict('records')
            
            final_segments = []
            for current in records:
                c_ini = current['fecha_hora_inicio']
                c_fin = current['fecha_hora_fin']
                segments_of_current = [(c_ini, c_fin)]
                
                for final_seg in final_segments:
                    f_ini = final_seg['fecha_hora_inicio']
                    f_fin = final_seg['fecha_hora_fin']
                    new_segments = []
                    for s_ini, s_fin in segments_of_current:
                        if f_fin <= s_ini or f_ini >= s_fin:
                            new_segments.append((s_ini, s_fin)) # Sin solapamiento
                        else:
                            # Solapamiento: partimos la etapa larga
                            if s_ini < f_ini: new_segments.append((s_ini, f_ini))
                            if s_fin > f_fin: new_segments.append((f_fin, s_fin))
                    segments_of_current = new_segments
                
                for s_ini, s_fin in segments_of_current:
                    if (s_fin - s_ini).total_seconds() >= 60: # Descartar micro-basura < 1 min
                        seg = current.copy()
                        seg['fecha_hora_inicio'] = s_ini
                        seg['fecha_hora_fin'] = s_fin
                        final_segments.append(seg)
            
            df_frag = pd.DataFrame(final_segments)
            if df_frag.empty: return df_frag
            
            # 2. Pumping vs NPT (NPTs perforan el bombeo creando los huecos en el Gantt)
            if df_npt is not None and not df_npt.empty:
                df_npt_val = df_npt.dropna(subset=['fecha_inicio', 'fecha_fin']).copy()
                df_npt_val['fecha_inicio'] = pd.to_datetime(df_npt_val['fecha_inicio'])
                df_npt_val['fecha_fin'] = pd.to_datetime(df_npt_val['fecha_fin'])
                df_npt_val = df_npt_val[df_npt_val['fecha_fin'] > df_npt_val['fecha_inicio']]
                
                if not df_npt_val.empty:
                    npt_list = df_npt_val[['fecha_inicio', 'fecha_fin']].values.tolist()
                    final_npt_segments = []
                    
                    for _, row in df_frag.iterrows():
                        c_ini = row['fecha_hora_inicio']
                        c_fin = row['fecha_hora_fin']
                        segments = [(c_ini, c_fin)]
                        
                        for n_ini, n_fin in npt_list:
                            new_segments = []
                            for s_ini, s_fin in segments:
                                if n_fin <= s_ini or n_ini >= s_fin:
                                    new_segments.append((s_ini, s_fin))
                                else:
                                    if s_ini < n_ini: new_segments.append((s_ini, n_ini))
                                    if s_fin > n_fin: new_segments.append((n_fin, s_fin))
                            segments = new_segments
                            
                        for s_ini, s_fin in segments:
                            if (s_fin - s_ini).total_seconds() >= 60:
                                seg = row.to_dict()
                                seg['fecha_hora_inicio'] = s_ini
                                seg['fecha_hora_fin'] = s_fin
                                final_npt_segments.append(seg)
                    df_frag = pd.DataFrame(final_npt_segments)
            
            return df_frag.sort_values('fecha_hora_inicio').reset_index(drop=True)

        # --- LÓGICA 2: AUDITORÍA DE NPT EN VENTANA DE TRANSICIÓN ---
        def has_npt_in_gap(p_end, c_start, df_npts):
            if pd.isna(p_end) or pd.isna(c_start): return False
            if df_npts is None or df_npts.empty: return False
            
            df_n = df_npts.dropna(subset=['fecha_inicio', 'fecha_fin']).copy()
            df_n['fecha_inicio'] = pd.to_datetime(df_n['fecha_inicio'])
            df_n['fecha_fin'] = pd.to_datetime(df_n['fecha_fin'])
            
            # Un NPT anula el CP si ocurre en el medio del gap, o si termina en el segundo exacto que arranca la etapa
            mask_overlap = (df_n['fecha_inicio'] < c_start) & (df_n['fecha_fin'] > p_end)
            mask_touch_end = (df_n['fecha_fin'] == c_start)
            return (mask_overlap | mask_touch_end).any()

        # --- LÓGICA 3: MOTOR CENTRAL ---
        def procesar_pad_cp(df_h9_pad, df_h2_pad):
            # 1. Base Original de Etapas
            df_p = df_h9_pad.copy().sort_values('fecha_hora_inicio')
            if df_p.empty: return df_p, pd.DataFrame()
            
            df_p['stage_id'] = df_p['nombre_pozo'].astype(str) + "_" + df_p['nro_etapa'].astype(str)
            df_p = df_p.drop_duplicates(subset=['stage_id'], keep='first')
            
            df_npt = df_h2_pad[df_h2_pad['es_npt'] == True].copy() if 'es_npt' in df_h2_pad.columns else pd.DataFrame()
            
            if 'es_cp_tecnico' not in df_p.columns:
                df_p['es_cp_tecnico'] = False
            
            # 2. Fragmentación visual (Cortador de galletas puro, SIN clamping que borre los retornos)
            df_frag = generar_fragmentos_visuales(df_p, df_npt)
            
            if df_frag.empty:
                df_p['es_cp_final'] = False
                return df_p, df_frag
                
            df_frag = df_frag.sort_values('fecha_hora_inicio').reset_index(drop=True)
            
            # Buscamos los inicios y fines absolutos reales de cada etapa para la regla del cambio intermedio
            real_starts = df_frag.groupby('stage_id')['fecha_hora_inicio'].min()
            real_ends = df_frag.groupby('stage_id')['fecha_hora_fin'].max()
            
            # 3. Lógica Estricta de Color y Validaciones
            colores = []
            true_prev_stage = {}
            
            for i in range(len(df_frag)):
                frag = df_frag.iloc[i]
                if i == 0:
                    es_tecnico = df_p.loc[df_p['stage_id'] == frag['stage_id'], 'es_cp_tecnico'].values
                    colores.append(es_tecnico[0] if len(es_tecnico) > 0 else False)
                    continue
                    
                prev_frag = df_frag.iloc[i-1]
                gap_mins = (frag['fecha_hora_inicio'] - prev_frag['fecha_hora_fin']).total_seconds() / 60.0
                
                # Regla de anulación por NPT en la brecha o Gap > 5
                if gap_mins > 5 or has_npt_in_gap(prev_frag['fecha_hora_fin'], frag['fecha_hora_inicio'], df_npt):
                    colores.append(False)
                else:
                    # REGLA DE ORO (El cambio intermedio): ¿La etapa anterior terminó definitivamente?
                    is_prev_final = (prev_frag['fecha_hora_fin'] == real_ends[prev_frag['stage_id']])
                    
                    if not is_prev_final:
                        # Si no era el final absoluto, es un cambio a mitad de etapa. ROJO.
                        colores.append(False) 
                    else:
                        is_frag_start = (frag['fecha_hora_inicio'] == real_starts[frag['stage_id']])
                        es_tecnico = df_p.loc[df_p['stage_id'] == frag['stage_id'], 'es_cp_tecnico'].values
                        es_valido = es_tecnico[0] if len(es_tecnico) > 0 else False
                        
                        if is_frag_start:
                            # Arranque limpio
                            colores.append(es_valido)
                            if es_valido:
                                true_prev_stage[frag['stage_id']] = prev_frag['stage_id']
                        else:
                            # Es un bloque de 'Retorno' (Los famosos cuadrados violetas)
                            # Hereda el status original de su etapa si la transición fue rápida y sin NPT
                            colores.append(es_valido)
                            
            df_frag['es_cp_final'] = colores
            
            # 4. Actualizar Base Original
            cp_por_etapa = df_frag[df_frag['fecha_hora_inicio'] == df_frag['stage_id'].map(real_starts)].set_index('stage_id')['es_cp_final']
            df_p['es_cp_final'] = df_p['stage_id'].map(cp_por_etapa).fillna(False)
            
            df_p['prev_stage_id'] = df_p['stage_id'].map(true_prev_stage)
            stage_text_map = dict(zip(df_p['stage_id'], df_p['nombre_pozo'].astype(str) + " Etapa " + df_p['nro_etapa'].astype(str)))
            df_p['pozo_etapa_actual'] = df_p['stage_id'].map(stage_text_map)
            df_p['pozo_etapa_anterior'] = df_p['prev_stage_id'].map(stage_text_map).fillna("Inicio / NPT")
            
            # 5. Agrupación para Máximo CP Diario
            df_frag['gap_frag'] = (df_frag['fecha_hora_inicio'] - df_frag['fecha_hora_fin'].shift(1)).dt.total_seconds() / 60
            df_frag['nuevo_bloque'] = (~df_frag['es_cp_final']) | (df_frag['gap_frag'] > 5)
            df_frag['bloque_id'] = df_frag['nuevo_bloque'].cumsum()
            df_frag['es_bloque_cp'] = df_frag['es_cp_final']
            
            return df_p, df_frag
        # -----------------------------------------------------------
        
        if 'fecha_reporte' in df_h9.columns and not df_h9.empty:
            idx_max_h9 = pd.to_datetime(df_h9['fecha_reporte'], errors='coerce').idxmax()
            yac_def_c = df_h9.loc[idx_max_h9, 'Yacimiento'] if pd.notna(idx_max_h9) else "S/D"
            pad_def_c = df_h9.loc[idx_max_h9, 'PAD'] if pd.notna(idx_max_h9) else "S/D"
        else:
            yac_def_c, pad_def_c = "S/D", "S/D"

        tab3, tab4 = st.tabs(["📊 Pestaña 1 (Diario por PAD)", "📋 Pestaña 2 (Resumen Gerencial)"])
        
        with tab3:
            col_c1, col_c2, col_c3 = st.columns([1, 1, 1])
            yacimientos_disp = df_h9['Yacimiento'].dropna().unique().tolist() if 'Yacimiento' in df_h9.columns else ["S/D"]
            if not yacimientos_disp: yacimientos_disp = ["S/D"]
            
            yac_idx_c = yacimientos_disp.index(yac_def_c) if yac_def_c in yacimientos_disp else 0
            with col_c1: sel_yac_c1 = st.selectbox("Seleccionar Yacimiento (C1):", yacimientos_disp, index=yac_idx_c)
            
            pads_disp = df_h9[df_h9['Yacimiento'] == sel_yac_c1]['PAD'].dropna().unique().tolist() if 'PAD' in df_h9.columns else ["S/D"]
            if not pads_disp: pads_disp = ["S/D"]
            
            pad_idx_c = pads_disp.index(pad_def_c) if (sel_yac_c1 == yac_def_c and pad_def_c in pads_disp) else 0
            with col_c2: sel_pad_c1 = st.selectbox("Seleccionar PAD (C1):", pads_disp, index=pad_idx_c)
            
            # --- PREPARACIÓN DE DATOS ---
            df_pad_raw = df_h9[(df_h9['Yacimiento'] == sel_yac_c1) & (df_h9['PAD'] == sel_pad_c1)].copy()
            df_pad_h2 = df_h2[(df_h2['Yacimiento'] == sel_yac_c1) & (df_h2['PAD'] == sel_pad_c1)].copy()
            
            # EJECUTAR MOTOR MAESTRO
            df_c1_h9, df_frag = procesar_pad_cp(df_pad_raw, df_pad_h2)
            
            if not df_c1_h9.empty:
                min_date_c1 = pd.to_datetime(df_c1_h9['fecha_reporte']).min().date()
                max_date_c1 = pd.to_datetime(df_c1_h9['fecha_reporte']).max().date()
            else:
                min_date_c1, max_date_c1 = datetime.today().date(), datetime.today().date()
                
            with col_c3:
                fechas_c1 = st.date_input("Filtrar Fechas (C1):", [min_date_c1, max_date_c1], min_value=min_date_c1, max_value=max_date_c1)
                
            if len(fechas_c1) == 2:
                f_inicio_c1, f_fin_c1 = fechas_c1
            else:
                f_inicio_c1, f_fin_c1 = fechas_c1[0], fechas_c1[0]
            
            df_c1_h9['fecha_reporte'] = pd.to_datetime(df_c1_h9['fecha_reporte']).dt.date
            df_c1_h9['fecha_reporte_cp'] = pd.to_datetime(df_c1_h9['fecha_reporte_cp']).dt.date
            df_pad_h2['fecha_reporte'] = pd.to_datetime(df_pad_h2['fecha_reporte']).dt.date
            
            st.subheader(f"Cuadro 1 - Evolución CP ({sel_pad_c1})")
            
            todas_las_fechas = set(df_c1_h9['fecha_reporte'].dropna()) | set(df_c1_h9['fecha_reporte_cp'].dropna()) | set(df_pad_h2['fecha_reporte'].dropna())
            fechas_pad = sorted([f for f in todas_las_fechas if pd.notna(f)]) 
            
            std_yac = PARAMETROS_STD.get(sel_yac_c1, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]
            df_npt_pad = df_pad_h2[df_pad_h2['es_npt'] == True].copy() if 'es_npt' in df_pad_h2.columns else pd.DataFrame()
            
            datos_cp = []
            acum_etapas_fin = 0
            acum_cp = 0
            acum_minutos_totales = 0
            posibles_acum_ayer = 0
            
            for fecha in fechas_pad:
                df_dia_h9_fin = df_c1_h9[df_c1_h9['fecha_reporte'] == fecha]
                df_dia_h9_inicio = df_c1_h9[df_c1_h9['fecha_reporte_cp'] == fecha]
                df_dia_h2 = df_pad_h2[df_pad_h2['fecha_reporte'] == fecha]
                
                etapas_dia = len(df_dia_h9_fin)
                cp_dia = df_dia_h9_inicio['es_cp_final'].fillna(False).sum() if 'es_cp_final' in df_dia_h9_inicio.columns else 0
                
                if 'duracion_minutos' in df_dia_h2.columns:
                    minutos_dia = pd.to_numeric(df_dia_h2['duracion_minutos'], errors='coerce').sum()
                else:
                    minutos_dia = 0
                
                acum_etapas_fin += etapas_dia
                acum_cp += cp_dia
                acum_minutos_totales += minutos_dia
                
                posibles_acum_hoy = max(0, acum_etapas_fin - 4)
                posibles_dia = posibles_acum_hoy - posibles_acum_ayer
                
                pct_cp_dia = (cp_dia / posibles_dia * 100) if posibles_dia > 0 else 0
                pct_cp_pad = (acum_cp / posibles_acum_hoy * 100) if posibles_acum_hoy > 0 else 0
                
                dias_reales = acum_minutos_totales / 1440.0
                etapas_por_dia_real = (acum_etapas_fin / dias_reales) if dias_reales > 0 else 0
                
                inicio_ventana = pd.to_datetime(fecha) - pd.Timedelta(days=1) + pd.Timedelta(hours=6)
                fin_ventana = pd.to_datetime(fecha) + pd.Timedelta(hours=6)
                
                if not df_frag.empty:
                    df_tiempos = df_frag.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin']).copy()
                    df_tiempos['overlap_inicio'] = np.maximum(df_tiempos['fecha_hora_inicio'], inicio_ventana)
                    df_tiempos['overlap_fin'] = np.minimum(df_tiempos['fecha_hora_fin'], fin_ventana)
                    df_tiempos['minutos_en_ventana'] = (df_tiempos['overlap_fin'] - df_tiempos['overlap_inicio']).dt.total_seconds() / 60
                    
                    df_hoy = df_tiempos[df_tiempos['minutos_en_ventana'] > 0].copy()
                    
                    if not df_hoy.empty:
                        tiempo_bombeo_dia = df_hoy['minutos_en_ventana'].sum() / 60
                        df_cp_hoy = df_hoy[df_hoy['es_bloque_cp'] == True]
                        tiempo_total_cp_dia = df_cp_hoy['minutos_en_ventana'].sum() / 60
                        
                        max_tiempo_cp_dia = df_cp_hoy.groupby('bloque_id')['minutos_en_ventana'].sum().max() / 60 if not df_cp_hoy.empty else 0
                    else:
                        tiempo_bombeo_dia, tiempo_total_cp_dia, max_tiempo_cp_dia = 0, 0, 0
                else:
                    tiempo_bombeo_dia, tiempo_total_cp_dia, max_tiempo_cp_dia = 0, 0, 0
                
                datos_cp.append({
                    "Fecha Reporte": pd.to_datetime(fecha).strftime('%d/%m/%Y'),
                    "Etapas Acum.": acum_etapas_fin,
                    "Etapas Día": etapas_dia,
                    "CP Logrados": cp_dia,
                    "% CP (Día)": f"{pct_cp_dia:.1f}%",
                    "% CP (PAD)": f"{pct_cp_pad:.1f}%",
                    "Etapas STD": std_yac,
                    "Etapas/Día (Real)": round(etapas_por_dia_real, 2),
                    "Tiempo de Bombeo (hr)": round(tiempo_bombeo_dia, 2),
                    "Tiempo Total de Bombeo Continuo (hr)": round(tiempo_total_cp_dia, 2),
                    "Maximo Tiempo de Bombeo Continuo Diario (hr)": round(max_tiempo_cp_dia, 2),
                    "Etapas Posibles CP (Acum-4)": posibles_acum_hoy
                })
                posibles_acum_ayer = posibles_acum_hoy
                
            columnas_tabla = ["Fecha Reporte", "Etapas Acum.", "Etapas Día", "CP Logrados", "% CP (Día)", "% CP (PAD)", "Etapas STD", "Etapas/Día (Real)", "Tiempo de Bombeo (hr)", "Tiempo Total de Bombeo Continuo (hr)", "Maximo Tiempo de Bombeo Continuo Diario (hr)", "Etapas Posibles CP (Acum-4)"]
            df_cuadro1 = pd.DataFrame(datos_cp) if datos_cp else pd.DataFrame(columns=columnas_tabla)
            
            if not df_cuadro1.empty:
                df_cuadro1['Fecha_Date'] = pd.to_datetime(df_cuadro1["Fecha Reporte"], format='%d/%m/%Y').dt.date
                df_cuadro1_filtrado = df_cuadro1[(df_cuadro1['Fecha_Date'] >= f_inicio_c1) & (df_cuadro1['Fecha_Date'] <= f_fin_c1)].drop(columns=['Fecha_Date'])
            else:
                df_cuadro1_filtrado = df_cuadro1
            st.dataframe(df_cuadro1_filtrado, use_container_width=True, hide_index=True)
            
            st.subheader("Línea de Tiempo (Gantt) - FRAC & CP")
            
            if not df_frag.empty:
                df_gantt = df_frag.copy()
                df_gantt['Fecha_Date_Ini'] = pd.to_datetime(df_gantt['fecha_hora_inicio'] - pd.Timedelta(hours=6)).dt.date
                df_gantt = df_gantt[(df_gantt['Fecha_Date_Ini'] >= f_inicio_c1) & (df_gantt['Fecha_Date_Ini'] <= f_fin_c1)]
                
                if not df_gantt.empty:
                    df_gantt['Tipo_FRAC'] = np.where(df_gantt['es_cp_final'], 'FRAC (Con CP)', 'FRAC (Sin CP)')
                    df_gantt['Etapa Nro'] = df_gantt['stage_id'].str.split('_').str[-1]
                    df_gantt['Inicio_str'] = df_gantt['fecha_hora_inicio'].dt.strftime('%d/%m/%Y %H:%M')
                    df_gantt['Fin_str'] = df_gantt['fecha_hora_fin'].dt.strftime('%d/%m/%Y %H:%M')
                    
                    fig = px.timeline(df_gantt, x_start="fecha_hora_inicio", x_end="fecha_hora_fin", y="nombre_pozo", 
                                      color="Tipo_FRAC",
                                      color_discrete_map={"FRAC (Con CP)": "#2ca02c", "FRAC (Sin CP)": "#d62728"},
                                      custom_data=['nombre_pozo', 'Etapa Nro', 'Inicio_str', 'Fin_str'])
                    
                    fig.update_traces(hovertemplate="<b>%{customdata[0]} - Etapa %{customdata[1]}</b><br>Inicio = %{customdata[2]}<br>Fin = %{customdata[3]}<extra></extra>")
                    fig.update_layout(barmode='overlay', legend_title_text="Telemetría de Bombeo")
                    fig.update_yaxes(autorange="reversed", type='category')
                    
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning("⚠️ No hay datos de bombeo para el rango seleccionado.")
            else:
                st.warning("⚠️ No hay datos procesados disponibles.")
            
            col_t1, col_t2 = st.columns(2)
            
            with col_t1:
                st.subheader("Listado de Transiciones")
                if not df_c1_h9.empty:
                    df_trans = df_c1_h9[df_c1_h9['es_cp_final'] == True].copy()
                    df_trans = df_trans[(pd.to_datetime(df_trans['fecha_reporte_cp']).dt.date >= f_inicio_c1) & (pd.to_datetime(df_trans['fecha_reporte_cp']).dt.date <= f_fin_c1)]
                    df_trans['Texto_Transicion'] = df_trans['pozo_etapa_anterior'] + " -> " + df_trans['pozo_etapa_actual']
                else:
                    df_trans = pd.DataFrame()
                
                tabla1 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_trans['fecha_reporte_cp']).dt.strftime('%d/%m/%Y') if not df_trans.empty else [],
                    "Transición (Pozo/Etapa)": df_trans['Texto_Transicion'] if not df_trans.empty else []
                })
                st.dataframe(tabla1 if not tabla1.empty else pd.DataFrame(columns=["Fecha de Reporte", "Transición (Pozo/Etapa)"]), use_container_width=True, hide_index=True)

            with col_t2:
                st.subheader("Etapas con Continuous Pumping")
                if not df_c1_h9.empty:
                    df_logrados = df_c1_h9[df_c1_h9['es_cp_final'] == True].copy()
                    df_logrados = df_logrados[(pd.to_datetime(df_logrados['fecha_reporte_cp']).dt.date >= f_inicio_c1) & (pd.to_datetime(df_logrados['fecha_reporte_cp']).dt.date <= f_fin_c1)]
                else:
                    df_logrados = pd.DataFrame()
                
                tabla2 = pd.DataFrame({
                    "Fecha de Reporte": pd.to_datetime(df_logrados['fecha_reporte_cp']).dt.strftime('%d/%m/%Y') if not df_logrados.empty else [],
                    "Pozo": df_logrados['nombre_pozo'] if not df_logrados.empty else [],
                    "Etapa Nro": df_logrados['nro_etapa'].fillna(0).astype(int) if not df_logrados.empty else [],
                    "Secuencia Diaria": df_logrados['secuencia_diaria'].fillna(0).astype(int) if not df_logrados.empty else []
                })
                st.dataframe(tabla2 if not tabla2.empty else pd.DataFrame(columns=["Fecha de Reporte", "Pozo", "Etapa Nro", "Secuencia Diaria"]), use_container_width=True, hide_index=True)

        with tab4:
            st.subheader("Cuadro 1 - Foto Final por PAD")
            col_m3, col_m4 = st.columns(2)
            
            def_yacs = [yac_def_c] if yac_def_c != "S/D" else yacimientos_disp
            with col_m3: sel_yac_c2 = st.multiselect("Seleccionar Yacimiento(s) (C2):", yacimientos_disp, default=def_yacs)
            
            pads_disp_c2 = df_h9[df_h9['Yacimiento'].isin(sel_yac_c2)]['PAD'].dropna().unique().tolist()
            def_pads = [pad_def_c] if pad_def_c in pads_disp_c2 else pads_disp_c2
            with col_m4: sel_pad_c2 = st.multiselect("Seleccionar PAD(s) (C2):", pads_disp_c2, default=def_pads)
            
            resumen_cp = []
            
            for pad in sel_pad_c2:
                df_pad_raw = df_h9[df_h9['PAD'] == pad].copy()
                if df_pad_raw.empty: continue
                df_pad_h2 = df_h2[df_h2['PAD'] == pad].copy()
                
                df_pad_h9, df_pad_frag = procesar_pad_cp(df_pad_raw, df_pad_h2)
                
                yac = df_pad_h9['Yacimiento'].iloc[0]
                std = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]
                
                max_fecha = df_pad_h9['fecha_reporte'].max()
                ultima_fecha = pd.to_datetime(max_fecha).strftime('%d/%m/%Y') if pd.notna(max_fecha) else "S/D"
                
                etapas_totales = len(df_pad_h9)
                cp_totales = df_pad_h9['es_cp_final'].fillna(False).sum() if 'es_cp_final' in df_pad_h9.columns else 0
                etapas_posibles = max(0, etapas_totales - 4)
                pct_cp_final = (cp_totales / etapas_posibles * 100) if etapas_posibles > 0 else 0
                
                if 'duracion_minutos' in df_pad_h2.columns:
                    minutos_totales = pd.to_numeric(df_pad_h2['duracion_minutos'], errors='coerce').sum()
                else:
                    minutos_totales = 0
                
                dias_reales = minutos_totales / 1440.0
                etapas_dia_real = (etapas_totales / dias_reales) if dias_reales > 0 else 0
                
                fechas_unicas = set(df_pad_h9['fecha_reporte'].dropna())
                
                tiempo_bombeo_pad = 0
                tiempo_total_cp_pad = 0
                max_cp_diario_pad = 0
                
                if not df_pad_frag.empty:
                    for f in fechas_unicas:
                        inv = pd.to_datetime(f) - pd.Timedelta(days=1) + pd.Timedelta(hours=6)
                        fnv = pd.to_datetime(f) + pd.Timedelta(hours=6)
                        
                        df_tiempos = df_pad_frag.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin']).copy()
                        df_tiempos['overlap_inicio'] = np.maximum(df_tiempos['fecha_hora_inicio'], inv)
                        df_tiempos['overlap_fin'] = np.minimum(df_tiempos['fecha_hora_fin'], fnv)
                        df_tiempos['minutos_en_ventana'] = (df_tiempos['overlap_fin'] - df_tiempos['overlap_inicio']).dt.total_seconds() / 60
                        
                        df_hoy_pad = df_tiempos[df_tiempos['minutos_en_ventana'] > 0].copy()
                        
                        if not df_hoy_pad.empty:
                            tiempo_bombeo_pad += df_hoy_pad['minutos_en_ventana'].sum() / 60
                            df_bloques_hoy = df_hoy_pad[df_hoy_pad['es_bloque_cp'] == True]
                            tiempo_total_cp_pad += df_bloques_hoy['minutos_en_ventana'].sum() / 60
                            
                            if not df_bloques_hoy.empty:
                                max_dia = df_bloques_hoy.groupby('bloque_id')['minutos_en_ventana'].sum().max() / 60
                                if max_dia > max_cp_diario_pad:
                                    max_cp_diario_pad = max_dia
                
                resumen_cp.append({
                    "Yacimiento": yac,
                    "PAD": pad,
                    "Etapas Acum.": etapas_totales,
                    "Total CP Logrados": cp_totales,
                    "% CP Final (PAD)": f"{pct_cp_final:.1f}%",
                    "Etapas STD": std,
                    "Etapas/Día (Real)": round(etapas_dia_real, 2),
                    "Tiempo de Bombeo (hr)": round(tiempo_bombeo_pad, 2),
                    "Tiempo Total de Bombeo Continuo (hr)": round(tiempo_total_cp_pad, 2),
                    "Máx. Diario CP (hr)": round(max_cp_diario_pad, 2),
                    "Posibles CP (Total - 4)": etapas_posibles,
                    "Última Fecha": ultima_fecha
                })
            
            columnas_cuadro1 = ["Yacimiento", "PAD", "Etapas Acum.", "Total CP Logrados", "% CP Final (PAD)", "Etapas STD", "Etapas/Día (Real)", "Tiempo de Bombeo (hr)", "Tiempo Total de Bombeo Continuo (hr)", "Máx. Diario CP (hr)", "Posibles CP (Total - 4)", "Última Fecha"]
            st.dataframe(pd.DataFrame(resumen_cp) if resumen_cp else pd.DataFrame(columns=columnas_cuadro1), use_container_width=True, hide_index=True)
            
            st.subheader("Cuadro 2 - Resumen por Yacimiento")
            resumen_yac = []
            
            for yac in sel_yac_c2:
                pads_del_yac = [p for p in sel_pad_c2 if p in df_h9[df_h9['Yacimiento'] == yac]['PAD'].unique()]
                if not pads_del_yac: continue
                
                df_yac_h9 = df_h9[(df_h9['Yacimiento'] == yac) & (df_h9['PAD'].isin(pads_del_yac))].copy()
                if df_yac_h9.empty: continue
                
                tiempo_bombeo_yac = 0
                tiempo_total_cp_yac = 0
                max_cp_diario_yac = 0
                etapas_totales_yac = 0
                cp_totales_yac = 0
                minutos_totales_yac = 0
                posibles_yac = 0
                
                for pad_data in resumen_cp:
                    if pad_data["Yacimiento"] == yac and pad_data["PAD"] in pads_del_yac:
                        tiempo_bombeo_yac += pad_data["Tiempo de Bombeo (hr)"]
                        tiempo_total_cp_yac += pad_data["Tiempo Total de Bombeo Continuo (hr)"]
                        if pad_data["Máx. Diario CP (hr)"] > max_cp_diario_yac:
                            max_cp_diario_yac = pad_data["Máx. Diario CP (hr)"]
                            
                        etapas_totales_yac += pad_data["Etapas Acum."]
                        cp_totales_yac += pad_data["Total CP Logrados"]
                        posibles_yac += pad_data["Posibles CP (Total - 4)"]
                        
                        df_pad_h2 = df_h2[df_h2['PAD'] == pad_data["PAD"]].copy()
                        if 'duracion_minutos' in df_pad_h2.columns:
                            minutos_totales_yac += pd.to_numeric(df_pad_h2['duracion_minutos'], errors='coerce').sum()
                
                pct_cp_yac = (cp_totales_yac / posibles_yac * 100) if posibles_yac > 0 else 0
                std_yac = PARAMETROS_STD.get(yac, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]
                
                dias_reales_yac = minutos_totales_yac / 1440.0
                etapas_dia_real_yac = (etapas_totales_yac / dias_reales_yac) if dias_reales_yac > 0 else 0
                
                resumen_yac.append({
                    "Yacimiento": yac,
                    "Etapas Acumuladas": etapas_totales_yac,
                    "Total CP Realizados": cp_totales_yac,
                    "% CP Final (Yacimiento)": f"{pct_cp_yac:.1f}%",
                    "Etapas STD": std_yac,
                    "Etapas/Día (Yacimiento)": round(etapas_dia_real_yac, 2),
                    "Tiempo de Bombeo (hr)": round(tiempo_bombeo_yac, 2),
                    "Tiempo Total de Bombeo Continuo (hr)": round(tiempo_total_cp_yac, 2),
                    "Máx. Diario CP (hr)": round(max_cp_diario_yac, 2)
                })
                
            columnas_cuadro2 = ["Yacimiento", "Etapas Acumuladas", "Total CP Realizados", "% CP Final (Yacimiento)", "Etapas STD", "Etapas/Día (Yacimiento)", "Tiempo de Bombeo (hr)", "Tiempo Total de Bombeo Continuo (hr)", "Máx. Diario CP (hr)"]
            st.dataframe(pd.DataFrame(resumen_yac) if resumen_yac else pd.DataFrame(columns=columnas_cuadro2), use_container_width=True, hide_index=True)

except Exception as e:
    st.error(f"❌ Ocurrió un error al procesar el tablero. Detalles: {e}")