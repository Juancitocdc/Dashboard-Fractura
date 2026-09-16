#### APP APB — Tablero Operativo (rediseño visual)
#
# Nota: los cálculos (descarga, procesamiento, cuadros) son los mismos que en la versión
# anterior. Lo que cambia es únicamente la presentación: tema, tarjetas, tablas y gráficos.


import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytz
from datetime import datetime
import io
import requests
import google.auth.transport.requests
from google.oauth2 import service_account
import json
import html as _html
from string import Template

# ==========================================
# CONFIGURACIÓN DE LA PÁGINA
# ==========================================
st.set_page_config(page_title="Dashboard Operativo", page_icon="⏱️", layout="wide", initial_sidebar_state="expanded")

PIN_OPERATIVO = "FRACTURA2026" # <--- CAMBIÁ EL PIN ACÁ

# ==========================================
# PARÁMETROS STD Y URLs
# ==========================================
PARAMETROS_STD = {
    "FORTIN DE PIEDRA": {"Etapas_Dia_STD": 7.6, "Setupf_STD_min": 21.0, "Ramp_STD_min": 8.0, "Frac_STD_min": 122.0},
    "LOS TOLDOS ESTE": {"Etapas_Dia_STD": 8.7, "Setupf_STD_min": 17.0, "Ramp_STD_min": 5.0, "Frac_STD_min": 119.0},
    "Default": {"Etapas_Dia_STD": 7.0, "Setupf_STD_min": 15.0, "Ramp_STD_min": 10.0, "Frac_STD_min": 120.0}
}

# Tolerancia para considerar bombeo continuo: la etapa nueva tiene que arrancar entre
# TOLERANCIA_CP_MIN antes y TOLERANCIA_CP_MIN después del fin de la etapa anterior.
TOLERANCIA_CP_MIN = 5
# Presiones (P3m / ISIP) menores o iguales a este valor se toman como "no cargadas" al evaluar el CP.
PRESION_MINIMA_PSI = 0

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

    # Etapa anterior = la que arrancó antes y cuyo fin es el último dentro de (inicio + tolerancia).
    # No es la fila de arriba: si se salió de un pozo y se volvió a entrar, la anterior es la que terminó justo antes.
    _inicios = df_continuo['fecha_hora_inicio'].values
    _fines = df_continuo['fecha_hora_fin'].values
    _tol = np.timedelta64(int(TOLERANCIA_CP_MIN * 60), 's')
    _idx_anterior = np.full(len(df_continuo), -1)
    for _i in range(len(df_continuo)):
        if pd.isna(_inicios[_i]):
            continue
        _cand = np.where((_inicios < _inicios[_i]) & (_fines <= _inicios[_i] + _tol))[0]
        if len(_cand):
            _idx_anterior[_i] = _cand[np.argmax(_fines[_cand])]
    _tiene_anterior = _idx_anterior >= 0
    _ant = df_continuo.iloc[np.where(_tiene_anterior, _idx_anterior, 0)]
    df_continuo['pozo_etapa_anterior'] = np.where(_tiene_anterior, _ant['pozo_etapa_actual'].values, 'Inicio Operaciones')
    df_continuo['transicion_cp'] = np.where(df_continuo['es_cp'], df_continuo['pozo_etapa_anterior'] + " -> " + df_continuo['pozo_etapa_actual'], "")

    # --- INICIO NUEVA LÓGICA DE CP (CON PRESIONES) ---
    # 1. Blindaje: Asegurar que las columnas existan por si suben un Excel viejo
    if 'presion_final_3m [psi]' not in df_continuo.columns:
        df_continuo['presion_final_3m [psi]'] = np.nan
    if 'isip_post_frac [psi]' not in df_continuo.columns:
        df_continuo['isip_post_frac [psi]'] = np.nan

    # 2. Calcula la diferencia de tiempo contra la etapa anterior (la que terminó justo antes)
    df_continuo['fecha_hora_fin_anterior'] = pd.to_datetime(np.where(_tiene_anterior, _ant['fecha_hora_fin'].values, np.datetime64('NaT')))
    df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'] = (df_continuo['fecha_hora_inicio'] - df_continuo['fecha_hora_fin_anterior']).dt.total_seconds() / 60

    # 3. Traemos las presiones de esa etapa anterior
    df_continuo['presion_3m_anterior'] = np.where(_tiene_anterior, _ant['presion_final_3m [psi]'].values, np.nan)
    df_continuo['isip_anterior'] = np.where(_tiene_anterior, _ant['isip_post_frac [psi]'].values, np.nan)

    # 4. Limpiamos las presiones para ver si realmente están vacías
    no_hay_p3m = pd.to_numeric(df_continuo['presion_3m_anterior'], errors='coerce').fillna(0) <= PRESION_MINIMA_PSI
    no_hay_isip = pd.to_numeric(df_continuo['isip_anterior'], errors='coerce').fillna(0) <= PRESION_MINIMA_PSI

    # 5. Lógica dura combinada: Tiempo <= 5 min Y NO hay P3m Y NO hay ISIP
    condicion_tiempo = df_continuo['Tiempo_entre_fin_e_inicio_de_nueva_fractura'].between(-TOLERANCIA_CP_MIN, TOLERANCIA_CP_MIN)
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
# SISTEMA VISUAL: TEMAS, CSS Y ESTILO DE GRÁFICOS
# ==========================================
# Dos paletas (oscura y clara) con los mismos "roles" de color. Todo el tablero
# se dibuja a partir de estos tokens, así cambiar un color se hace en un solo lugar.
TEMAS = {
    "oscuro": dict(
        base="dark",
        bg="#0d1117", surface="#151b23", surface2="#1c242e",
        border="rgba(255,255,255,0.08)", border_strong="rgba(255,255,255,0.16)",
        text="#e6edf3", text2="#9aa5b1", muted="#6b7683",
        accent="#3987e5", accent_soft="rgba(57,135,229,0.16)", accent_2="#6fb1ff",
        good="#0ca30c", good_text="#3ccf5a", good_soft="rgba(12,163,12,0.16)",
        bad="#d03b3b", bad_text="#f07272", bad_soft="rgba(208,59,59,0.16)",
        warn="#fab219", warn_soft="rgba(250,178,25,0.16)",
        grid="rgba(255,255,255,0.07)", axis="rgba(255,255,255,0.18)",
        setupf="#3987e5", ramp="#d95926", frac="#199e70", otros="#9085e9",
        cp="#0ca30c", sin_cp="#d03b3b", etapas="#55708f",
    ),
    "claro": dict(
        base="light",
        bg="#f3f5f7", surface="#ffffff", surface2="#f4f6f9",
        border="rgba(11,11,11,0.10)", border_strong="rgba(11,11,11,0.18)",
        text="#111827", text2="#52514e", muted="#898781",
        accent="#2a78d6", accent_soft="rgba(42,120,214,0.12)", accent_2="#5598e7",
        good="#0ca30c", good_text="#006300", good_soft="rgba(12,163,12,0.12)",
        bad="#d03b3b", bad_text="#a82626", bad_soft="rgba(208,59,59,0.10)",
        warn="#eda100", warn_soft="rgba(237,161,0,0.14)",
        grid="#e1e0d9", axis="#c3c2b7",
        setupf="#2a78d6", ramp="#eb6834", frac="#1baf7a", otros="#4a3aa7",
        cp="#0ca30c", sin_cp="#d03b3b", etapas="#8fa3b8",
    ),
}


def _tema_nativo(modo):
    # Le pide a Streamlit que sus propios widgets (selectores, fechas, botones) usen la
    # misma paleta. Es una API interna: si en alguna versión no está, seguimos con el CSS solo.
    t = TEMAS[modo]
    try:
        from streamlit import config as _cfg
        _cfg.set_option("theme.base", t["base"])
        _cfg.set_option("theme.primaryColor", t["accent"])
        _cfg.set_option("theme.backgroundColor", t["bg"])
        _cfg.set_option("theme.secondaryBackgroundColor", t["surface"])
        _cfg.set_option("theme.textColor", t["text"])
    except Exception:
        pass


if "modo_tema" not in st.session_state:
    # Arranca en oscuro salvo que el tema activo de Streamlit ya sea claro
    modo_inicial = "oscuro"
    try:
        from streamlit import config as _cfg
        if str(_cfg.get_option("theme.base")).lower() == "light":
            modo_inicial = "claro"
    except Exception:
        pass
    st.session_state["modo_tema"] = modo_inicial

MODO = st.session_state["modo_tema"]
TEMA = TEMAS[MODO]

# Si el repo no tiene .streamlit/config.toml, igual alineamos los widgets nativos con la paleta
try:
    from streamlit import config as _cfg
    if not _cfg.get_option("theme.base"):
        _tema_nativo(MODO)
except Exception:
    pass
FUENTE = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif"

CSS = Template("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root {
  --bg:$bg; --surface:$surface; --surface2:$surface2; --border:$border; --border-strong:$border_strong;
  --text:$text; --text2:$text2; --muted:$muted; --accent:$accent; --accent-soft:$accent_soft; --accent2:$accent_2;
  --good:$good; --good-text:$good_text; --good-soft:$good_soft; --bad:$bad; --bad-text:$bad_text; --bad-soft:$bad_soft;
  --warn:$warn; --warn-soft:$warn_soft;
}
/* ---- superficies base de Streamlit ---- */
.stApp, [data-testid="stAppViewContainer"] { background: var(--bg) !important; color: var(--text); }
[data-testid="stHeader"] { background: transparent !important; }
[data-testid="stSidebar"] { background: var(--surface) !important; border-right: 1px solid var(--border); }
[data-testid="stSidebar"] > div:first-child { padding-top: 0.6rem; }
.block-container { padding-top: 1.4rem !important; padding-bottom: 3rem; max-width: 1560px; }
.block-container [data-testid="stVerticalBlock"] { gap: 0.8rem; }
.stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp li, .stApp input, .stApp button,
.stApp div[data-testid="stMarkdownContainer"], .stApp [data-baseweb="select"] div { font-family: $fuente; }
.stApp p, .stApp label, .stApp li { color: var(--text); }
.stApp h1, .stApp h2, .stApp h3 { color: var(--text); letter-spacing: -0.01em; }
.stApp hr { border-color: var(--border); margin: 0.9rem 0; }
.stApp [data-testid="stWidgetLabel"] p, .stApp [data-testid="stWidgetLabel"] label { color: var(--text2); font-size: 12.5px; font-weight: 600; }
/* ---- pestañas ---- */
[data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid var(--border); }
[data-testid="stTabs"] button[role="tab"] { background: transparent; padding: 10px 14px; border-radius: 8px 8px 0 0; }
[data-testid="stTabs"] button[role="tab"] p { color: var(--text2); font-weight: 600; font-size: 14px; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] p { color: var(--text); }
[data-testid="stTabs"] button[role="tab"]:hover { background: var(--surface2); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background: var(--accent); height: 3px; border-radius: 3px 3px 0 0; }
[data-testid="stTabs"] [data-baseweb="tab-border"] { background: var(--border); }
/* ---- navegación lateral (radio convertido en lista) ---- */
[data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] { gap: 4px; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] {
  width: 100%; margin: 0; padding: 9px 12px; border-radius: 10px; cursor: pointer;
  border: 1px solid transparent; background: transparent; transition: background .15s;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:hover { background: var(--surface2); }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) { background: var(--accent-soft); border-color: rgba(57,135,229,0.35); }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-of-type { display: none; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] p { font-weight: 600; color: var(--text2); font-size: 14px; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) p { color: var(--text); }
/* ---- widgets ---- */
.stApp [data-baseweb="select"] > div { background: var(--surface) !important; border-color: var(--border-strong) !important; border-radius: 10px !important; color: var(--text); }
.stApp [data-baseweb="select"] div, .stApp [data-baseweb="select"] input { color: var(--text) !important; }
.stApp [data-baseweb="select"] svg { fill: var(--text2); }
.stApp [data-testid="stDateInput"] [data-baseweb="input"], .stApp [data-testid="stDateInput"] [data-baseweb="base-input"] { background: var(--surface) !important; border-color: var(--border-strong) !important; border-radius: 10px !important; }
.stApp [data-testid="stDateInput"] input { color: var(--text) !important; }
.stApp [data-testid="stTextInput"] [data-baseweb="input"], .stApp [data-testid="stTextInput"] [data-baseweb="base-input"] { background: var(--surface) !important; border-color: var(--border-strong) !important; border-radius: 10px !important; }
.stApp [data-testid="stTextInput"] input { color: var(--text) !important; }
[data-baseweb="popover"] ul, [data-baseweb="popover"] li, [data-baseweb="popover"] [data-baseweb="calendar"], [data-baseweb="popover"] div[role="listbox"] { background: var(--surface) !important; color: var(--text) !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] { background: var(--accent-soft) !important; }
[data-baseweb="tag"] { background: var(--accent-soft) !important; }
[data-baseweb="tag"] span { color: var(--text) !important; }
[data-testid="stSegmentedControl"] button, [data-testid="stSegmentedControl"] div[role="radiogroup"] > label { border-radius: 8px !important; }
[data-testid="stSegmentedControl"] p { font-weight: 600; }
.stApp [data-testid="stBaseButton-secondary"], .stApp button[kind="secondary"] {
  border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface2); color: var(--text); font-weight: 600;
}
.stApp [data-testid="stBaseButton-secondary"]:hover, .stApp button[kind="secondary"]:hover { border-color: var(--accent); color: var(--accent); }
[data-testid="stSidebar"] [data-testid="stButton"] button { width: 100%; }
[data-testid="stDownloadButton"] button { width: 100%; border-radius: 10px; background: var(--accent) !important; border: none !important; color: #fff !important; font-weight: 600; }
[data-testid="stDownloadButton"] button p { color: #fff !important; }
[data-testid="stDownloadButton"] button:hover { filter: brightness(1.08); }
.stApp [data-testid="stBaseButton-primary"], .stApp button[kind="primary"] { border-radius: 10px; background: var(--accent); border: none; font-weight: 600; }
.stApp [data-testid="stBaseButton-primary"] p, .stApp button[kind="primary"] p { color: #fff !important; }
[data-testid="stToggle"] p, [data-testid="stCheckbox"] p { color: var(--text2); font-weight: 600; font-size: 13px; }
/* ---- marca en la barra lateral ---- */
.brand { display:flex; gap:12px; align-items:center; padding: 4px 2px 14px; border-bottom: 1px solid var(--border); margin-bottom: 8px; }
.brand-mark { width:40px; height:40px; border-radius:11px; background: linear-gradient(135deg, var(--accent), var(--accent2)); display:flex; align-items:center; justify-content:center; font-weight:800; color:#fff; font-size: 15px; letter-spacing:-.02em; flex-shrink:0; }
.brand-name { font-weight: 700; font-size: 15px; color: var(--text); line-height:1.15; }
.brand-sub { font-size: 11.5px; color: var(--muted); margin-top:3px; }
.side-label { font-size: 11px; letter-spacing:.1em; text-transform: uppercase; color: var(--muted); font-weight: 700; margin: 16px 0 4px; }
.status-card { border:1px solid var(--border); background: var(--bg); border-radius: 12px; padding: 10px 12px; margin: 6px 0 8px; }
.status-card .k { font-size: 11px; text-transform: uppercase; letter-spacing:.08em; color: var(--muted); font-weight:700; }
.status-card .v { font-size: 14px; font-weight: 600; color: var(--text); margin-top: 3px; }
.status-card .ok { display:inline-flex; align-items:center; gap:6px; color: var(--good-text); font-weight:600; font-size: 12.5px; margin-top:6px; }
.status-card .ok i { width:8px; height:8px; border-radius:50%; background: var(--good); display:inline-block; }
/* ---- encabezado de sección ---- */
.hdr { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; flex-wrap:wrap; margin: 2px 0 14px; }
.hdr-eyebrow { font-size: 11.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--accent); font-weight: 700; }
.hdr-title { font-size: 30px; font-weight: 800; letter-spacing: -0.02em; color: var(--text); line-height: 1.15; margin: 4px 0 2px; }
.hdr-sub { color: var(--text2); font-size: 14px; }
.chips { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.chip { display:inline-flex; align-items:center; gap:6px; padding: 5px 11px; border-radius: 999px; border:1px solid var(--border); background: var(--surface); font-size: 12.5px; color: var(--text2); font-weight:500; white-space:nowrap; }
.chip b { color: var(--text); font-weight: 600; }
.chip.accent { border-color: transparent; background: var(--accent-soft); color: var(--accent); font-weight:600; }
.chip .dot { width:8px; height:8px; border-radius:50%; background: var(--good); display:inline-block; }
.sec { display:flex; align-items:center; gap:10px; margin: 26px 0 10px; }
.sec-bar { width:4px; height:18px; border-radius:2px; background: var(--accent); }
.sec-title { font-size: 16.5px; font-weight: 700; color: var(--text); }
.sec-sub { font-size: 13px; color: var(--muted); }
.sec.small { margin: 14px 0 8px; }
.sec.small .sec-title { font-size: 14px; }
.leyenda { display:flex; gap:14px; flex-wrap:wrap; margin: -4px 0 10px; font-size: 12px; color: var(--text2); }
.leyenda i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:5px; vertical-align:-1px; }
/* ---- tarjetas KPI ---- */
.kpis { display:grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin: 6px 0 4px; }
.kpi { background: var(--surface); border:1px solid var(--border); border-radius: 14px; padding: 14px 16px 13px; position:relative; overflow:hidden; min-width:0; }
.kpi::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background: var(--kpi-accent, var(--accent)); }
.kpi-label { font-size: 11.5px; color: var(--text2); font-weight: 600; text-transform: uppercase; letter-spacing: .06em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.kpi-value { font-size: 27px; font-weight: 700; color: var(--text); margin-top: 6px; line-height:1.1; letter-spacing:-0.01em; }
.kpi-unit { font-size: 13px; color: var(--muted); font-weight: 500; margin-left: 3px; }
.kpi-delta { margin-top: 8px; font-size: 12px; font-weight: 600; display:inline-flex; gap:4px; align-items:center; padding: 2px 8px; border-radius: 999px; }
.kpi-delta.good { color: var(--good-text); background: var(--good-soft); }
.kpi-delta.bad { color: var(--bad-text); background: var(--bad-soft); }
.kpi-delta.neutral { color: var(--text2); background: var(--surface2); }
.kpi-hint { margin-top: 7px; font-size: 12px; color: var(--muted); }
/* ---- tablas ---- */
.tbl-wrap { border:1px solid var(--border); border-radius: 14px; overflow:auto; background: var(--surface); }
.tbl { width:100%; border-collapse: separate; border-spacing: 0; font-size: 13px; font-variant-numeric: tabular-nums; }
.tbl thead th { position: sticky; top:0; z-index:2; background: var(--surface2); color: var(--text2); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; padding: 10px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; height: 38px; box-sizing: border-box; }
.tbl thead tr.grp th { background: var(--surface); color: var(--text); text-transform: none; letter-spacing: 0; font-size: 12.5px; font-weight: 700; text-align:center; height: 36px; padding: 6px 12px; }
.tbl thead tr.grp + tr th { top: 36px; }
.grp-note { display:inline-block; margin-left: 8px; font-weight: 500; color: var(--muted); font-size: 11px; background: var(--surface2); padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); }
.tbl tbody td { padding: 9px 12px; border-bottom: 1px solid var(--border); color: var(--text); white-space: nowrap; }
.tbl tbody tr:last-child td { border-bottom: none; }
.tbl.compacta { font-size: 12.5px; }
.tbl.compacta thead th { padding: 8px 9px; font-size: 11px; }
.tbl.compacta tbody td { padding: 8px 9px; }
.tbl tbody tr:hover td { background: var(--surface2); }
.tbl .num { text-align: right; }
.tbl .txt { text-align: left; }
.tbl .ctr { text-align: center; }
.tbl td.dim { color: var(--text2); }
.tbl td.strong { font-weight: 600; }
.tbl th.grp-first, .tbl td.grp-first { border-left: 1px solid var(--border); }
.tbl td.good { background: var(--good-soft); color: var(--good-text); font-weight: 600; }
.tbl td.bad { background: var(--bad-soft); color: var(--bad-text); font-weight: 600; }
.tbl td.warn { background: var(--warn-soft); font-weight: 500; }
.tbl tbody tr.total td { background: var(--surface2); font-weight: 700; border-top: 1px solid var(--border-strong); }
.barra { display:inline-block; height:6px; border-radius:3px; background: var(--barra-color, var(--accent)); vertical-align: middle; margin-right: 8px; opacity:.9; }
.pill { display:inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; }
.pill.good { background: var(--good-soft); color: var(--good-text); }
.pill.bad { background: var(--bad-soft); color: var(--bad-text); }
.pill.neutral { background: var(--surface2); color: var(--text2); }
.tbl-empty { padding: 28px; text-align:center; color: var(--muted); font-size: 13.5px; }
.tbl-foot { padding: 8px 12px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); }
/* ---- pantalla de PIN ---- */
.pin-card { max-width: 440px; margin: 6vh auto 4px; background: var(--surface); border:1px solid var(--border); border-radius: 18px; padding: 30px 28px 24px; text-align:center; }
.pin-icon { width:56px; height:56px; border-radius:16px; background: var(--accent-soft); color: var(--accent); display:flex; align-items:center; justify-content:center; margin: 0 auto 14px; font-size: 24px; }
.pin-title { font-size: 22px; font-weight: 800; color: var(--text); letter-spacing:-0.01em; }
.pin-sub { color: var(--text2); font-size: 14px; margin-top: 6px; }
.aviso { border:1px solid var(--border); border-left: 3px solid var(--warn); background: var(--surface); border-radius: 10px; padding: 10px 14px; color: var(--text2); font-size: 13px; margin: 8px 0; }
.aviso.error { border-left-color: var(--bad); }
</style>
""").substitute(fuente=FUENTE, **TEMA)



def html_out(contenido):
    # st.html (Streamlit >= 1.33) no pasa por Markdown ni agrega márgenes; si no existe, usa markdown
    if hasattr(st, "html"):
        st.html(contenido)
    else:
        st.markdown(contenido, unsafe_allow_html=True)


html_out(CSS)


# ==========================================
# COMPONENTES DE PRESENTACIÓN (HTML)
# ==========================================
def esc(texto):
    # Escapa texto para HTML y evita que Markdown interprete _ o * como formato
    s = _html.escape(str(texto))
    return s.replace("_", "&#95;").replace("*", "&#42;").replace("~", "&#126;")


def fmt_num(v, dec=2):
    # Mismo criterio que mostraba la tabla anterior: enteros sin decimales, el resto con hasta 2
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
            return "–"
        v = float(v)
    except Exception:
        return esc(v)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}"
    return f"{v:.{dec}f}".rstrip("0").rstrip(".")


def encabezado(eyebrow, titulo, subtitulo="", chips=None):
    chips_html = ""
    if chips:
        chips_html = '<div class="chips">' + "".join(chips) + "</div>"
    html_out(
        f'<div class="hdr"><div><div class="hdr-eyebrow">{esc(eyebrow)}</div>'
        f'<div class="hdr-title">{esc(titulo)}</div>'
        + (f'<div class="hdr-sub">{esc(subtitulo)}</div>' if subtitulo else "")
        + f"</div>{chips_html}</div>"
    )


def chip(texto, valor=None, tipo="", punto=False):
    inner = (('<span class="dot"></span>' if punto else "") + esc(texto)
             + (f" <b>{esc(valor)}</b>" if valor is not None else ""))
    return f'<span class="chip {tipo}">{inner}</span>'


def seccion(titulo, sub="", chica=False):
    html_out(
        f'<div class="sec{" small" if chica else ""}"><div class="sec-bar"></div>'
        f'<div class="sec-title">{esc(titulo)}</div>'
        + (f'<div class="sec-sub">{esc(sub)}</div>' if sub else "") + "</div>"
    )


def leyenda(items):
    # items: lista de (texto, color)
    html_out('<div class="leyenda">' + "".join(
        f'<span><i style="background:{c}"></i>{esc(t)}</span>' for t, c in items) + "</div>")


def kpis(tarjetas):
    """tarjetas: lista de dicts con label, value, unit, delta, delta_tipo (good/bad/neutral), hint, color"""
    partes = []
    for t in tarjetas:
        color = t.get("color") or TEMA["accent"]
        h = f'<div class="kpi" style="--kpi-accent:{color}"><div class="kpi-label">{esc(t["label"])}</div>'
        h += f'<div class="kpi-value">{esc(t["value"])}'
        if t.get("unit"):
            h += f'<span class="kpi-unit">{esc(t["unit"])}</span>'
        h += "</div>"
        if t.get("delta"):
            h += f'<div class="kpi-delta {t.get("delta_tipo", "neutral")}">{esc(t["delta"])}</div>'
        if t.get("hint"):
            h += f'<div class="kpi-hint">{esc(t["hint"])}</div>'
        h += "</div>"
        partes.append(h)
    html_out('<div class="kpis">' + "".join(partes) + "</div>")


def tabla(df, columnas, grupos=None, clases=None, alto_max=None, pie=None, fila_total=None, compacta=False):
    """
    Dibuja un DataFrame ya calculado como tabla HTML. No recalcula nada.
      columnas: lista de (columna_del_df, etiqueta, tipo) con tipo en {"txt", "num", "pct", "ctr", "raw", "rawtxt"}
                (raw/rawtxt = la celda ya viene como HTML, alineada a la derecha / izquierda)
      grupos:   lista de (etiqueta, cantidad_de_columnas, nota) para la fila superior del encabezado
      clases:   función (fila_dict, columna) -> clase css extra ("good", "bad", "dim", ...) o None
      fila_total: dict opcional con una fila resumen al final
    """
    if df is None or len(df) == 0:
        html_out('<div class="tbl-wrap"><div class="tbl-empty">Sin datos para el rango seleccionado.</div></div>')
        return

    # columnas que abren un grupo (para dibujar el separador vertical)
    inicio_grupo = set()
    if grupos:
        pos = 0
        for _, n, _ in grupos:
            if pos > 0:
                inicio_grupo.add(pos)
            pos += n

    def clase_col(i, tipo):
        c = {"num": "num", "pct": "num", "txt": "txt", "ctr": "ctr", "raw": "num", "rawtxt": "txt"}.get(tipo, "txt")
        if i in inicio_grupo:
            c += " grp-first"
        return c

    thead = ""
    if grupos:
        thead += '<tr class="grp">'
        pos = 0
        for etiqueta, n, nota in grupos:
            extra = " grp-first" if pos > 0 else ""
            nota_html = f'<span class="grp-note">{esc(nota)}</span>' if nota else ""
            thead += f'<th colspan="{n}" class="{extra.strip()}">{esc(etiqueta)}{nota_html}</th>'
            pos += n
        thead += "</tr>"
    thead += "<tr>" + "".join(
        f'<th class="{clase_col(i, tipo)}">{esc(etq)}</th>' for i, (_, etq, tipo) in enumerate(columnas)) + "</tr>"

    def celda(fila, i, col, tipo):
        v = fila.get(col)
        if tipo in ("raw", "rawtxt"):
            txt = "" if v is None else str(v)
        elif tipo in ("num",):
            txt = fmt_num(v)
        else:
            txt = "–" if (v is None or (isinstance(v, float) and np.isnan(v))) else esc(v)
        extra = clases(fila, col) if clases else None
        return f'<td class="{clase_col(i, tipo)}{(" " + extra) if extra else ""}">{txt}</td>'

    filas = df.to_dict("records")
    tbody = "".join(
        "<tr>" + "".join(celda(f, i, col, tipo) for i, (col, _, tipo) in enumerate(columnas)) + "</tr>"
        for f in filas)
    if fila_total:
        tbody += '<tr class="total">' + "".join(
            celda(fila_total, i, col, tipo) for i, (col, _, tipo) in enumerate(columnas)) + "</tr>"

    estilo = f' style="max-height:{alto_max}px"' if alto_max else ""
    pie_html = f'<div class="tbl-foot">{esc(pie)}</div>' if pie else ""
    html_out(f'<div class="tbl-wrap"{estilo}><table class="tbl{" compacta" if compacta else ""}"><thead>{thead}</thead><tbody>{tbody}</tbody></table>{pie_html}</div>')


def barra_html(valor, maximo, color=None, ancho_max=70):
    # Barrita proporcional para mostrar magnitud dentro de una celda (solo visual)
    try:
        v = float(valor)
    except Exception:
        return fmt_num(valor)
    if maximo is None or maximo <= 0 or np.isnan(v):
        return fmt_num(valor)
    w = max(2, int(round(ancho_max * v / maximo))) if v > 0 else 0
    c = color or TEMA["accent"]
    return f'<span class="barra" style="width:{w}px;--barra-color:{c}"></span>{fmt_num(v)}'


def pildora(texto, tipo="neutral"):
    return f'<span class="pill {tipo}">{esc(texto)}</span>'


def delta_vs_std(valor, std, unidad="min", menor_es_mejor=True):
    # Texto y tipo para la diferencia contra el estándar. Solo formato, sin lógica nueva.
    try:
        v, s = float(valor), float(std)
    except Exception:
        return None, "neutral"
    d = v - s
    if abs(d) < 1e-9:
        return f"= STD ({fmt_num(s)} {unidad})", "neutral"
    flecha = "▼" if d < 0 else "▲"
    bueno = (d < 0) if menor_es_mejor else (d > 0)
    u = f" {unidad}" if unidad else ""
    return f"{flecha} {fmt_num(abs(d))}{u} vs STD {fmt_num(s)}", ("good" if bueno else "bad")


# ---------- gráficos ----------
def estilo_fig(fig, alto=300, leyenda=True, hover="x unified"):
    t = TEMA
    fig.update_layout(
        height=alto,
        margin=dict(l=6, r=10, t=34 if leyenda else 14, b=6),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FUENTE, size=12, color=t["text2"]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="",
                    font=dict(size=12, color=t["text2"]), bgcolor="rgba(0,0,0,0)"),
        showlegend=leyenda,
        hovermode=hover,
        hoverlabel=dict(bgcolor=t["surface2"], bordercolor=t["border_strong"],
                        font=dict(family=FUENTE, size=12, color=t["text"])),
        bargap=0.35,
    )
    # automargin: Plotly agranda el margen solo para que las etiquetas de los ejes no se corten
    fig.update_xaxes(showgrid=False, zeroline=False, showline=True, linecolor=t["axis"], linewidth=1,
                     tickfont=dict(color=t["muted"], size=11), title_text="", automargin=True)
    fig.update_yaxes(showgrid=True, gridcolor=t["grid"], gridwidth=1, zeroline=False, showline=False,
                     tickfont=dict(color=t["muted"], size=11), title_text="", automargin=True)
    try:
        fig.update_layout(barcornerradius=4)
    except Exception:
        pass
    return fig


def mostrar_fig(fig):
    st.plotly_chart(fig, use_container_width=True, theme=None, config={"displayModeBar": False})


def linea_std(fig, y, texto, fila=None, col=None):
    # Línea horizontal de referencia (STD) con etiqueta, sin gridlines extra
    kw = dict(row=fila, col=col) if fila else {}
    fig.add_hline(y=y, line=dict(color=TEMA["text2"], width=1.5), opacity=0.9,
                  annotation=dict(text=texto, font=dict(size=11, color=TEMA["text2"]),
                                  bgcolor=TEMA["surface"], bordercolor=TEMA["border_strong"], borderwidth=1, borderpad=3),
                  annotation_position="top right", **kw)


def a_fecha(serie_texto):
    return pd.to_datetime(serie_texto, format="%d/%m/%Y", errors="coerce")


def selector_pildoras(etiqueta, opciones, defecto, key):
    # Usa el control segmentado si la versión de Streamlit lo tiene; si no, un radio horizontal
    if hasattr(st, "segmented_control"):
        val = st.segmented_control(etiqueta, opciones, default=defecto, key=key)
        return val or defecto
    return st.radio(etiqueta, opciones, index=opciones.index(defecto), horizontal=True, key=key)


def aviso(texto, tipo=""):
    html_out(f'<div class="aviso {tipo}">{esc(texto)}</div>')


# ==========================================
# ACCESO CON PIN (misma lógica, otra presentación)
# ==========================================
if "acceso_concedido" not in st.session_state:
    st.session_state["acceso_concedido"] = False

if not st.session_state["acceso_concedido"]:
    col1, col2, col3 = st.columns([1, 1.15, 1])
    with col2:
        html_out('<div class="pin-card"><div class="pin-icon">🔒</div>'
                 '<div class="pin-title">Tablero Operativo</div>'
                 '<div class="pin-sub">Ingresá el código de autorización para ver los datos.</div></div>')
        pin_ingresado = st.text_input("PIN de Acceso:", type="password", label_visibility="collapsed", placeholder="PIN de acceso")
        if st.button("Desbloquear tablero", use_container_width=True, type="primary"):
            if pin_ingresado == PIN_OPERATIVO:
                st.session_state["acceso_concedido"] = True
                st.rerun()
            else:
                aviso("PIN incorrecto. Acceso denegado.", "error")
    st.stop()


# ==========================================
# BARRA LATERAL
# ==========================================
with st.sidebar:
    html_out('<div class="brand"><div class="brand-mark">APB</div><div>'
             '<div class="brand-name">Tablero Operativo</div>'
             '<div class="brand-sub">Fractura · Tiempos y Continuous Pumping</div></div></div>')

    html_out('<div class="side-label">Navegación</div>')
    seccion_sel = st.radio("Navegación Principal", ["Tiempos y NPT", "Continuous Pumping"], label_visibility="collapsed")

    html_out('<div class="side-label">Vista</div>')
    _toggle = getattr(st, "toggle", st.checkbox)
    modo_claro = _toggle("Modo claro", value=(MODO == "claro"), key="toggle_modo_claro")
    nuevo_modo = "claro" if modo_claro else "oscuro"
    if nuevo_modo != MODO:
        st.session_state["modo_tema"] = nuevo_modo
        _tema_nativo(nuevo_modo)
        st.rerun()

    html_out('<div class="side-label">Datos</div>')
    if st.button("🔄 Actualizar datos (Drive)"):
        st.cache_data.clear()
        st.rerun()
    estado_slot = st.empty()

    html_out('<div class="side-label">Exportar</div>')
    descarga_slot = st.empty()

try:
    with st.spinner("Procesando datos en vivo desde Google Drive..."):
        df_h2, df_h8, df_h9, archivo_maestro_bytes = descargar_y_procesar(URL_TIEMPOS, URL_CONTINUO)

    # Botón para descargar el Excel procesado final
    descarga_slot.download_button(
        label="📥 Descargar Excel Maestro (Full)",
        data=archivo_maestro_bytes,
        file_name=f"Reporte_Maestro_Fractura_{datetime.now().strftime('%d_%m_%Y')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    zona_ar = pytz.timezone('America/Argentina/Buenos_Aires')
    hora_actual = datetime.now(zona_ar).strftime("%d/%m/%Y %H:%M hs")
    if 'fecha_fin' in df_h2.columns:
        ultima_op = pd.to_datetime(df_h2['fecha_fin'], errors='coerce').max()
        hora_op = ultima_op.strftime("%d/%m/%Y %H:%M hs") if pd.notnull(ultima_op) else "Sin datos"
    else:
        hora_op = "Sin datos"

    with estado_slot:
        html_out(f'<div class="status-card"><div class="k">Última OP en pozo</div><div class="v">{esc(hora_op)}</div>'
                 f'<div class="ok"><i></i>Base actualizada · {esc(hora_actual)}</div></div>')

    # ==========================================
    # DASHBOARD: SECCIÓN 1 (TIEMPOS)
    # ==========================================
    if seccion_sel == "Tiempos y NPT":
        encabezado("Sección 1", "Control Operativo de Tiempos y NPT",
                   "Tiempos netos por fase, promedios por etapa y desglose de NPT contra el estándar del yacimiento.")

        col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
        with col_f1: toggle_npt = selector_pildoras("Inclusión de NPT", ["Sin NPT", "Con NPT"], "Sin NPT", "sel_npt")
        with col_f2: toggle_unidad = selector_pildoras("Unidad de medida", ["min", "hrs"], "min", "sel_unidad")

        tab1, tab2 = st.tabs(["📊 Detalle diario", "📋 Resumen global"])

        with tab1:
            col_s1, col_s2, col_s3 = st.columns([1, 1, 1])

            # --- Lógica del PAD por defecto ---
            if 'fecha reporte' in df_h8.columns and not df_h8.empty:
                idx_max_h8 = pd.to_datetime(df_h8['fecha reporte'], errors='coerce').idxmax()
                yac_def_t = df_h8.loc[idx_max_h8, 'Yacimiento'] if pd.notna(idx_max_h8) else "S/D"
                pad_def_t = df_h8.loc[idx_max_h8, 'PAD'] if pd.notna(idx_max_h8) else "S/D"
            else:
                yac_def_t, pad_def_t = "S/D", "S/D"

            yacimientos_disp = df_h8['Yacimiento'].dropna().unique().tolist() if 'Yacimiento' in df_h8.columns else ["S/D"]
            if not yacimientos_disp: yacimientos_disp = ["S/D"]

            yac_idx_t = yacimientos_disp.index(yac_def_t) if yac_def_t in yacimientos_disp else 0
            with col_s1: sel_yac_t1 = st.selectbox("Yacimiento", yacimientos_disp, index=yac_idx_t, key="yac_t1")

            pads_disp = df_h8[df_h8['Yacimiento'] == sel_yac_t1]['PAD'].dropna().unique().tolist() if 'PAD' in df_h8.columns else ["S/D"]
            if not pads_disp: pads_disp = ["S/D"]

            pad_idx_t = pads_disp.index(pad_def_t) if (sel_yac_t1 == yac_def_t and pad_def_t in pads_disp) else 0
            with col_s2: sel_pad_t1 = st.selectbox("PAD", pads_disp, index=pad_idx_t, key="pad_t1")

            col_fecha_h8 = 'fecha reporte' if 'fecha reporte' in df_h8.columns else 'fecha_reporte'
            df_t1 = df_h8[(df_h8['Yacimiento'] == sel_yac_t1) & (df_h8['PAD'] == sel_pad_t1)].sort_values(col_fecha_h8).copy()

            # --- Selector de fechas ---
            if not df_t1.empty:
                min_date_t1 = pd.to_datetime(df_t1[col_fecha_h8]).min().date()
                max_date_t1 = pd.to_datetime(df_t1[col_fecha_h8]).max().date()
            else:
                min_date_t1, max_date_t1 = datetime.today().date(), datetime.today().date()

            with col_s3:
                fechas_t1 = st.date_input("Rango de fechas", [min_date_t1, max_date_t1], min_value=min_date_t1, max_value=max_date_t1, key="fechas_t1")

            # Manejo si el usuario selecciona solo un día
            if len(fechas_t1) == 2:
                f_inicio_t1, f_fin_t1 = fechas_t1
            else:
                f_inicio_t1, f_fin_t1 = fechas_t1[0], fechas_t1[0]

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

            std_t1 = PARAMETROS_STD.get(sel_yac_t1, PARAMETROS_STD["Default"])
            std_setupf_u = std_t1["Setupf_STD_min"] / factor_div
            std_ramp_u = std_t1["Ramp_STD_min"] / factor_div
            std_frac_u = std_t1["Frac_STD_min"] / factor_div

            # ---- Cuadro 1 (mismos cálculos que antes) ----
            cuadro1_filtrado = pd.DataFrame()
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
                cuadro1['Fecha_Date'] = pd.to_datetime(cuadro1["Fecha de Reporte"], format='%d/%m/%Y').dt.date
                cuadro1_filtrado = cuadro1[(cuadro1['Fecha_Date'] >= f_inicio_t1) & (cuadro1['Fecha_Date'] <= f_fin_t1)].drop(columns=['Fecha_Date'])
            except Exception as e:
                aviso(f"Hubo un problema al armar el Cuadro 1: {e}", "error")

            # ---- Cuadro 2 (mismos cálculos que antes) ----
            cuadro2_filtrado = pd.DataFrame()
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
                cuadro2['Fecha_Date'] = pd.to_datetime(cuadro2["Fecha de Reporte"], format='%d/%m/%Y').dt.date
                cuadro2_filtrado = cuadro2[(cuadro2['Fecha_Date'] >= f_inicio_t1) & (cuadro2['Fecha_Date'] <= f_fin_t1)].drop(columns=['Fecha_Date'])
            except Exception:
                pass

            # ---- Tarjetas KPI (valores tomados de los cuadros de arriba) ----
            if not cuadro1_filtrado.empty:
                ult = cuadro1_filtrado.iloc[-1]
                total_etapas = int(cuadro1_filtrado["Cant. Etapas"].sum())
                d_set, k_set = delta_vs_std(ult["Setupf Prom PAD"], std_setupf_u, suf_und)
                d_ramp, k_ramp = delta_vs_std(ult["Ramp Prom PAD"], std_ramp_u, suf_und)
                d_frac, k_frac = delta_vs_std(ult["Frac Prom PAD"], std_frac_u, suf_und)
                npt_total_rango = float(cuadro2_filtrado["NPT Total"].sum()) if not cuadro2_filtrado.empty else 0.0
                npt_prom_pad = cuadro2_filtrado["NPT Prom PAD"].iloc[-1] if not cuadro2_filtrado.empty else 0

                kpis([
                    dict(label="Etapas en el período", value=fmt_num(total_etapas),
                         hint=f"{len(cuadro1_filtrado)} días · acumulado PAD al {ult['Fecha de Reporte']}", color=TEMA["accent"]),
                    dict(label="Setupf prom. PAD", value=fmt_num(ult["Setupf Prom PAD"]), unit=suf_und, delta=d_set, delta_tipo=k_set, color=TEMA["setupf"]),
                    dict(label="Ramp prom. PAD", value=fmt_num(ult["Ramp Prom PAD"]), unit=suf_und, delta=d_ramp, delta_tipo=k_ramp, color=TEMA["ramp"]),
                    dict(label="Frac prom. PAD", value=fmt_num(ult["Frac Prom PAD"]), unit=suf_und, delta=d_frac, delta_tipo=k_frac, color=TEMA["frac"]),
                    dict(label="NPT en el período", value=fmt_num(npt_total_rango), unit=suf_und,
                         hint=f"{fmt_num(npt_prom_pad)} {suf_und} por etapa (prom. PAD)", color=TEMA["bad"]),
                ])

                # ---- Gráficos ----
                seccion("Evolución diaria", f"{sel_pad_t1} · {suf_npt} · {suf_und}")
                fechas_x = a_fecha(cuadro1_filtrado["Fecha de Reporte"])
                g1, g2 = st.columns([1, 1])
                with g1:
                    fig = go.Figure()
                    fig.add_bar(x=fechas_x, y=cuadro1_filtrado["Cant. Etapas"], name="Etapas por día",
                                marker=dict(color=TEMA["accent"], line_width=0),
                                hovertemplate="%{y} etapas<extra></extra>")
                    linea_std(fig, std_t1["Etapas_Dia_STD"], f"STD {fmt_num(std_t1['Etapas_Dia_STD'])} etapas/día")
                    estilo_fig(fig, alto=280, leyenda=False)
                    fig.update_layout(title=dict(text="Etapas por día", font=dict(size=13, color=TEMA["text"]), x=0, xanchor="left"),
                                      margin=dict(t=40))
                    fig.update_xaxes(tickformat="%d/%m")
                    mostrar_fig(fig)
                with g2:
                    df_t1_rango = df_t1[(pd.to_datetime(df_t1[col_fecha_h8]).dt.date >= f_inicio_t1) & (pd.to_datetime(df_t1[col_fecha_h8]).dt.date <= f_fin_t1)]
                    fig = go.Figure()
                    for fase, col_npt, color in [("Setupf", "SETUPF NPT min", TEMA["setupf"]), ("Ramp", "RAMP NPT min", TEMA["ramp"]),
                                                 ("Frac", "FRAC NPT min", TEMA["frac"]), ("Otros", "OTROS NPT min", TEMA["otros"])]:
                        if col_npt in df_t1_rango.columns:
                            fig.add_bar(x=pd.to_datetime(df_t1_rango[col_fecha_h8]), y=(df_t1_rango[col_npt].fillna(0) / factor_div).round(2),
                                        name=fase, marker=dict(color=color, line=dict(color=TEMA["surface"], width=1)),
                                        hovertemplate="%{y} " + suf_und + "<extra>" + fase + "</extra>")
                    estilo_fig(fig, alto=280, leyenda=True)
                    fig.update_layout(barmode="stack", title=dict(text=f"NPT por día y fase ({suf_und})", font=dict(size=13, color=TEMA["text"]), x=0, xanchor="left"),
                                      legend=dict(y=1.0, x=1, xanchor="right"), margin=dict(t=40))
                    fig.update_xaxes(tickformat="%d/%m")
                    mostrar_fig(fig)

                seccion("Promedio por etapa (24 hs) vs STD", f"Cada panel compara el promedio diario por etapa con el estándar de {sel_yac_t1}", chica=True)
                fig = make_subplots(rows=1, cols=3, subplot_titles=["Setupf", "Ramp", "Frac"], horizontal_spacing=0.05)
                paneles = [("Setupf Prom 24hs", TEMA["setupf"], std_setupf_u), ("Ramp Prom 24hs", TEMA["ramp"], std_ramp_u), ("Frac Prom 24hs", TEMA["frac"], std_frac_u)]
                for i, (col_p, color, std_u) in enumerate(paneles, start=1):
                    fig.add_bar(x=fechas_x, y=cuadro1_filtrado[col_p], marker=dict(color=color, line_width=0), name=col_p,
                                hovertemplate="%{y} " + suf_und + "<extra></extra>", row=1, col=i)
                    linea_std(fig, std_u, f"STD {fmt_num(std_u)} {suf_und}", fila=1, col=i)
                estilo_fig(fig, alto=260, leyenda=False)
                fig.update_annotations(font=dict(size=12, color=TEMA["text2"]))
                fig.update_xaxes(tickformat="%d/%m")
                mostrar_fig(fig)

            # ---- Cuadro 1 ----
            seccion("Cuadro 1 · Tiempos operativos", f"{sel_pad_t1} · {suf_npt} · en {suf_und}")
            leyenda([("Por debajo del STD", TEMA["good"]), ("Por encima del STD", TEMA["bad"])])

            def clases_c1(f, col):
                # Se colorea el promedio del día contra el STD; el promedio PAD va en negrita sin color
                ref = {"Setupf Prom 24hs": std_setupf_u, "Ramp Prom 24hs": std_ramp_u, "Frac Prom 24hs": std_frac_u}.get(col)
                if ref is None:
                    if col == "Cant. Etapas":
                        return "dim"
                    return "strong" if col.endswith("Prom PAD") else None
                try:
                    return "good" if float(f[col]) <= ref else "bad"
                except Exception:
                    return None

            tabla(cuadro1_filtrado,
                  columnas=[("Fecha de Reporte", "Fecha", "txt"), ("Cant. Etapas", "Etapas", "num"),
                            ("Setupf", "Día", "num"), ("Setupf Prom 24hs", "Prom 24hs", "num"), ("Setupf Prom PAD", "Prom PAD", "num"),
                            ("Ramp", "Día", "num"), ("Ramp Prom 24hs", "Prom 24hs", "num"), ("Ramp Prom PAD", "Prom PAD", "num"),
                            ("Frac", "Día", "num"), ("Frac Prom 24hs", "Prom 24hs", "num"), ("Frac Prom PAD", "Prom PAD", "num")],
                  grupos=[("", 2, None),
                          ("Setupf", 3, f"STD {fmt_num(std_setupf_u)} {suf_und}"),
                          ("Ramp", 3, f"STD {fmt_num(std_ramp_u)} {suf_und}"),
                          ("Frac", 3, f"STD {fmt_num(std_frac_u)} {suf_und}")],
                  clases=clases_c1, alto_max=560, compacta=True)

            # ---- Cuadro 2 ----
            seccion("Cuadro 2 · Desglose de NPT", f"{sel_pad_t1} · en {suf_und}")
            if not cuadro2_filtrado.empty:
                c2_vista = cuadro2_filtrado.copy()
                max_npt = float(c2_vista["NPT Total"].max()) if len(c2_vista) else 0
                c2_vista["NPT Total"] = [barra_html(v, max_npt, TEMA["bad"]) for v in c2_vista["NPT Total"]]

                def clases_c2(f, col):
                    if col == "Cant. Etapas":
                        return "dim"
                    if col in ("NPT Setupf", "NPT Ramp", "NPT Frac"):
                        try:
                            return "warn" if float(f[col]) > 0 else "dim"
                        except Exception:
                            return None
                    return "strong" if col.endswith("Prom PAD") else None

                tabla(c2_vista,
                      columnas=[("Fecha de Reporte", "Fecha", "txt"), ("Cant. Etapas", "Etapas", "num"),
                                ("NPT Total", "Total", "raw"), ("NPT Prom 24hs", "Prom 24hs", "num"), ("NPT Prom PAD", "Prom PAD", "num"),
                                ("NPT Setupf", "Día", "num"), ("NPT Setupf Prom 24hs", "Prom 24hs", "num"), ("NPT Setupf Prom PAD", "Prom PAD", "num"),
                                ("NPT Ramp", "Día", "num"), ("NPT Ramp Prom 24hs", "Prom 24hs", "num"), ("NPT Ramp Prom PAD", "Prom PAD", "num"),
                                ("NPT Frac", "Día", "num"), ("NPT Frac Prom 24hs", "Prom 24hs", "num"), ("NPT Frac Prom PAD", "Prom PAD", "num")],
                      grupos=[("", 2, None), ("NPT total", 3, None), ("NPT Setupf", 3, None), ("NPT Ramp", 3, None), ("NPT Frac", 3, None)],
                      clases=clases_c2, alto_max=560, compacta=True)
            else:
                tabla(pd.DataFrame(), [])

        with tab2:
            col_m1, col_m2 = st.columns(2)
            with col_m1: sel_yac_t2 = st.multiselect("Yacimiento(s)", yacimientos_disp, default=yacimientos_disp, key="yac_t2")
            pads_disp_t2 = df_h8[df_h8['Yacimiento'].isin(sel_yac_t2)]['PAD'].dropna().unique().tolist()
            with col_m2: sel_pad_t2 = st.multiselect("PAD(s)", pads_disp_t2, default=pads_disp_t2, key="pad_t2")

            df_t2 = df_h8[(df_h8['Yacimiento'].isin(sel_yac_t2)) & (df_h8['PAD'].isin(sel_pad_t2))]

            # ---- Comparativa macro por PAD (mismos cálculos que antes) ----
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
            df_macro = pd.DataFrame(resumen_macro)

            seccion("Cuadro 1 · Comparativa macro vs STD por PAD", "Tiempos netos (sin NPT), en minutos")
            leyenda([("Por debajo del STD", TEMA["good"]), ("Por encima del STD", TEMA["bad"])])

            def clases_macro(f, col):
                pares = {"Setupf Prom PAD (min)": "Setupf STD (min)", "Ramp Prom PAD (min)": "Ramp STD (min)", "Frac Prom PAD (min)": "Frac STD (min)"}
                if col in pares:
                    try:
                        return "good" if float(f[col]) <= float(f[pares[col]]) else "bad"
                    except Exception:
                        return None
                if col.endswith("STD (min)") or col == "Cantidad Etapas STD":
                    return "dim"
                return None

            tabla(df_macro,
                  columnas=[("Yacimiento", "Yacimiento", "txt"), ("PAD", "PAD", "txt"),
                            ("Cantidad Etapas", "Total", "num"), ("Cantidad Etapas STD", "STD por día", "num"),
                            ("Setupf Prom PAD (min)", "Prom PAD", "num"), ("Setupf STD (min)", "STD", "num"),
                            ("Ramp Prom PAD (min)", "Prom PAD", "num"), ("Ramp STD (min)", "STD", "num"),
                            ("Frac Prom PAD (min)", "Prom PAD", "num"), ("Frac STD (min)", "STD", "num"),
                            ("NPT Total PAD (min)", "Total PAD", "num"), ("NPT Prom PAD (min)", "Prom / etapa", "num")],
                  grupos=[("", 2, None), ("Etapas", 2, None), ("Setupf (min)", 2, None), ("Ramp (min)", 2, None), ("Frac (min)", 2, None), ("NPT (min)", 2, None)],
                  clases=clases_macro)

            if not df_macro.empty:
                seccion("Desvío del promedio PAD respecto al STD", "En minutos por etapa · negativo = mejor que el estándar", chica=True)
                fig = go.Figure()
                for fase, col_p, col_s, color in [("Setupf", "Setupf Prom PAD (min)", "Setupf STD (min)", TEMA["setupf"]),
                                                  ("Ramp", "Ramp Prom PAD (min)", "Ramp STD (min)", TEMA["ramp"]),
                                                  ("Frac", "Frac Prom PAD (min)", "Frac STD (min)", TEMA["frac"])]:
                    desvio = (pd.to_numeric(df_macro[col_p], errors="coerce") - pd.to_numeric(df_macro[col_s], errors="coerce")).round(2)
                    fig.add_bar(x=df_macro["PAD"], y=desvio, name=fase, marker=dict(color=color, line_width=0),
                                hovertemplate="%{y:+.2f} min vs STD<extra>" + fase + "</extra>")
                estilo_fig(fig, alto=280, leyenda=True, hover="closest")
                fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
                fig.update_yaxes(zeroline=True, zerolinecolor=TEMA["axis"], zerolinewidth=1.5)
                mostrar_fig(fig)

    # ==========================================
    # DASHBOARD: SECCIÓN 2 (CONTINUOUS PUMPING)
    # ==========================================
    elif seccion_sel == "Continuous Pumping":
        encabezado("Sección 2", "Continuous Pumping",
                   "Transiciones entre etapas sin corte de bombeo, tiempo de bombeo continuo y comparativa por PAD y yacimiento.")

        # --- LÓGICA 1: CORTADOR DE GALLETAS (Limpieza Visual y de Tiempos) ---
        def generar_fragmentos_visuales(df_raw, df_npt):
            df_cl = df_raw.dropna(subset=['fecha_hora_inicio', 'fecha_hora_fin']).copy()
            if df_cl.empty: return pd.DataFrame()

            # 1. Pumping vs Pumping (Saltos intermedios / Zipper Frac real)
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
                            new_segments.append((s_ini, s_fin))
                        else:
                            # Parte la etapa a la mitad (Permite los retornos / cuadrados violetas)
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

            # 2. Pumping vs NPT (Crea los huecos en blanco)
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

        # --- LÓGICA 2: AUDITORÍA DE NPT EN VENTANA ---
        def has_npt_in_gap(p_end, c_start, df_npts):
            if pd.isna(p_end) or pd.isna(c_start): return False
            if df_npts is None or df_npts.empty: return False

            df_n = df_npts.dropna(subset=['fecha_inicio', 'fecha_fin']).copy()
            df_n['fecha_inicio'] = pd.to_datetime(df_n['fecha_inicio'])
            df_n['fecha_fin'] = pd.to_datetime(df_n['fecha_fin'])

            mask_overlap = (df_n['fecha_inicio'] < c_start) & (df_n['fecha_fin'] > p_end)
            mask_touch_end = (df_n['fecha_fin'] == c_start)
            return (mask_overlap | mask_touch_end).any()

        # --- LÓGICA 3: MOTOR CENTRAL (INDEPENDIZADO DE LA BASE CRUDA) ---
        def procesar_pad_cp(df_h9_pad, df_h2_pad):
            # 1. Base Original
            df_p = df_h9_pad.copy().sort_values('fecha_hora_inicio')
            if df_p.empty: return df_p, pd.DataFrame()

            df_p['stage_id'] = df_p['nombre_pozo'].astype(str) + "_" + df_p['nro_etapa'].astype(str)
            df_p = df_p.drop_duplicates(subset=['stage_id'], keep='first')

            df_npt = df_h2_pad[df_h2_pad['es_npt'] == True].copy() if 'es_npt' in df_h2_pad.columns else pd.DataFrame()

            # 2. Fragmentación
            df_frag = generar_fragmentos_visuales(df_p, df_npt)

            if df_frag.empty:
                df_p['es_cp_final'] = False
                return df_p, df_frag

            real_starts = df_frag.groupby('stage_id')['fecha_hora_inicio'].min()
            real_ends = df_frag.groupby('stage_id')['fecha_hora_fin'].max()

            # 3. DETECCIÓN DE ETAPAS ABANDONADAS
            abandoned_stages = set()
            for i in range(len(df_frag) - 1):
                curr_frag = df_frag.iloc[i]
                next_frag = df_frag.iloc[i+1]
                if curr_frag['fecha_hora_fin'] < real_ends[curr_frag['stage_id']]:
                    if curr_frag['stage_id'] != next_frag['stage_id']:
                        abandoned_stages.add(curr_frag['stage_id'])

            # 4. CRONOLOGÍA ESTRICTA EN VIVO (Sin mirar la base cruda)
            # Horas originales (sin recortar) y presiones por etapa, para aplicar la misma regla que la hoja 9:
            # ventana de +-TOLERANCIA_CP_MIN contra el fin real de la anterior y sin P3m/ISIP cargados en ella.
            inicio_real = df_p.set_index('stage_id')['fecha_hora_inicio']
            fin_real = df_p.set_index('stage_id')['fecha_hora_fin']
            p3m_etapa = pd.to_numeric(df_p.set_index('stage_id').get('presion_final_3m [psi]'), errors='coerce').fillna(0)
            isip_etapa = pd.to_numeric(df_p.set_index('stage_id').get('isip_post_frac [psi]'), errors='coerce').fillna(0)
            sin_presion = (p3m_etapa <= PRESION_MINIMA_PSI) & (isip_etapa <= PRESION_MINIMA_PSI)
            colores = []
            true_prev_stage = {}
            breaks_physical_block = []
            stage_status = {}

            for i in range(len(df_frag)):
                frag = df_frag.iloc[i]
                stage_id = frag['stage_id']
                is_frag_start = (frag['fecha_hora_inicio'] == real_starts[stage_id])

                if i == 0:
                    color = False # El primer bloque del pad en la historia no viene de nada, arranca rojo/gris.
                    colores.append(color)
                    if is_frag_start: stage_status[stage_id] = color
                    breaks_physical_block.append(False)
                    continue

                prev_frag = df_frag.iloc[i-1]
                gap_mins = (frag['fecha_hora_inicio'] - prev_frag['fecha_hora_fin']).total_seconds() / 60.0
                has_npt = has_npt_in_gap(prev_frag['fecha_hora_fin'], frag['fecha_hora_inicio'], df_npt)

                # --- BLOQUE FÍSICO (Para sumar las horas) ---
                if gap_mins > 5 or has_npt:
                    breaks_physical_block.append(True)
                else:
                    breaks_physical_block.append(False)

                # --- COLOR VISUAL Y CP LOGRADO ---
                if stage_id in abandoned_stages:
                    color = False
                else:
                    is_same_stage = (stage_id == prev_frag['stage_id'])
                    if is_same_stage:
                        color = stage_status.get(stage_id, False)
                    elif gap_mins > 5 or has_npt:
                        color = False
                    else:
                        is_prev_final = (prev_frag['fecha_hora_fin'] == real_ends[prev_frag['stage_id']])
                        if not is_prev_final:
                            color = False
                        else:
                            prev_id = prev_frag['stage_id']
                            gap_real = (inicio_real[stage_id] - fin_real[prev_id]).total_seconds() / 60.0
                            if is_frag_start and -TOLERANCIA_CP_MIN <= gap_real <= TOLERANCIA_CP_MIN and bool(sin_presion.get(prev_id, True)):
                                # Es CP: la etapa nueva arrancó dentro de la tolerancia del fin real de la anterior y la anterior no tiene presiones cargadas
                                color = True
                                if stage_id not in true_prev_stage:
                                    true_prev_stage[stage_id] = prev_frag['stage_id']
                            else:
                                color = False if is_frag_start else stage_status.get(stage_id, False)

                colores.append(color)
                if is_frag_start and stage_id not in stage_status:
                    stage_status[stage_id] = color

            df_frag['es_cp_final'] = colores
            df_frag['break_block'] = breaks_physical_block
            df_frag['bloque_id'] = df_frag['break_block'].cumsum()

            # El Tren de Bombeo Máximo se convalida si tiene al menos un fragmento Verde
            df_frag['es_bloque_cp'] = df_frag.groupby('bloque_id')['es_cp_final'].transform('any')

            # 5. Impactar resultado en la base original
            cp_por_etapa = df_frag.groupby('stage_id')['es_cp_final'].any()
            df_p['es_cp_final'] = df_p['stage_id'].map(cp_por_etapa).fillna(False)

            df_p['prev_stage_id'] = df_p['stage_id'].map(true_prev_stage)
            stage_text_map = dict(zip(df_p['stage_id'], df_p['nombre_pozo'].astype(str) + " Etapa " + df_p['nro_etapa'].astype(str)))
            df_p['pozo_etapa_actual'] = df_p['stage_id'].map(stage_text_map)
            df_p['pozo_etapa_anterior'] = df_p['prev_stage_id'].map(stage_text_map).fillna("Inicio / NPT")

            return df_p, df_frag
        # -----------------------------------------------------------

        if 'fecha_reporte' in df_h9.columns and not df_h9.empty:
            idx_max_h9 = pd.to_datetime(df_h9['fecha_reporte'], errors='coerce').idxmax()
            yac_def_c = df_h9.loc[idx_max_h9, 'Yacimiento'] if pd.notna(idx_max_h9) else "S/D"
            pad_def_c = df_h9.loc[idx_max_h9, 'PAD'] if pd.notna(idx_max_h9) else "S/D"
        else:
            yac_def_c, pad_def_c = "S/D", "S/D"

        tab3, tab4 = st.tabs(["📊 Diario por PAD", "📋 Resumen gerencial"])

        with tab3:
            col_c1, col_c2, col_c3 = st.columns([1, 1, 1])
            yacimientos_disp = df_h9['Yacimiento'].dropna().unique().tolist() if 'Yacimiento' in df_h9.columns else ["S/D"]
            if not yacimientos_disp: yacimientos_disp = ["S/D"]

            yac_idx_c = yacimientos_disp.index(yac_def_c) if yac_def_c in yacimientos_disp else 0
            with col_c1: sel_yac_c1 = st.selectbox("Yacimiento", yacimientos_disp, index=yac_idx_c, key="yac_c1")

            pads_disp = df_h9[df_h9['Yacimiento'] == sel_yac_c1]['PAD'].dropna().unique().tolist() if 'PAD' in df_h9.columns else ["S/D"]
            if not pads_disp: pads_disp = ["S/D"]

            pad_idx_c = pads_disp.index(pad_def_c) if (sel_yac_c1 == yac_def_c and pad_def_c in pads_disp) else 0
            with col_c2: sel_pad_c1 = st.selectbox("PAD", pads_disp, index=pad_idx_c, key="pad_c1")

            df_pad_raw = df_h9[(df_h9['Yacimiento'] == sel_yac_c1) & (df_h9['PAD'] == sel_pad_c1)].copy()
            df_pad_h2 = df_h2[(df_h2['Yacimiento'] == sel_yac_c1) & (df_h2['PAD'] == sel_pad_c1)].copy()

            df_c1_h9, df_frag = procesar_pad_cp(df_pad_raw, df_pad_h2)

            if not df_c1_h9.empty:
                min_date_c1 = pd.to_datetime(df_c1_h9['fecha_reporte']).min().date()
                max_date_c1 = pd.to_datetime(df_c1_h9['fecha_reporte']).max().date()
            else:
                min_date_c1, max_date_c1 = datetime.today().date(), datetime.today().date()

            with col_c3:
                fechas_c1 = st.date_input("Rango de fechas", [min_date_c1, max_date_c1], min_value=min_date_c1, max_value=max_date_c1, key="fechas_c1")

            if len(fechas_c1) == 2:
                f_inicio_c1, f_fin_c1 = fechas_c1
            else:
                f_inicio_c1, f_fin_c1 = fechas_c1[0], fechas_c1[0]

            df_c1_h9['fecha_reporte'] = pd.to_datetime(df_c1_h9['fecha_reporte']).dt.date
            df_c1_h9['fecha_reporte_cp'] = pd.to_datetime(df_c1_h9['fecha_reporte_cp']).dt.date
            df_pad_h2['fecha_reporte'] = pd.to_datetime(df_pad_h2['fecha_reporte']).dt.date

            todas_las_fechas = set(df_c1_h9['fecha_reporte'].dropna()) | set(df_c1_h9['fecha_reporte_cp'].dropna()) | set(df_pad_h2['fecha_reporte'].dropna())
            fechas_pad = sorted([f for f in todas_las_fechas if pd.notna(f)])

            std_yac = PARAMETROS_STD.get(sel_yac_c1, PARAMETROS_STD["Default"])["Etapas_Dia_STD"]

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

            # ---- Tarjetas KPI (valores tomados del cuadro de evolución) ----
            if not df_cuadro1_filtrado.empty:
                ult = df_cuadro1_filtrado.iloc[-1]
                cp_rango = int(pd.to_numeric(df_cuadro1_filtrado["CP Logrados"], errors="coerce").fillna(0).sum())
                etapas_rango = int(pd.to_numeric(df_cuadro1_filtrado["Etapas Día"], errors="coerce").fillna(0).sum())
                hrs_cp_rango = float(pd.to_numeric(df_cuadro1_filtrado["Tiempo Total de Bombeo Continuo (hr)"], errors="coerce").fillna(0).sum())
                hrs_bombeo_rango = float(pd.to_numeric(df_cuadro1_filtrado["Tiempo de Bombeo (hr)"], errors="coerce").fillna(0).sum())
                max_cp_rango = float(pd.to_numeric(df_cuadro1_filtrado["Maximo Tiempo de Bombeo Continuo Diario (hr)"], errors="coerce").fillna(0).max())
                d_etd, k_etd = delta_vs_std(ult["Etapas/Día (Real)"], std_yac, "", menor_es_mejor=False)

                kpis([
                    dict(label="Etapas acumuladas", value=fmt_num(ult["Etapas Acum."]), hint=f"{etapas_rango} en el período · al {ult['Fecha Reporte']}", color=TEMA["accent"]),
                    dict(label="CP logrados", value=fmt_num(cp_rango), hint=f"de {fmt_num(ult['Etapas Posibles CP (Acum-4)'])} posibles (acum. − 4)", color=TEMA["cp"]),
                    dict(label="% CP del PAD", value=str(ult["% CP (PAD)"]), hint=f"último día: {ult['% CP (Día)']}", color=TEMA["cp"]),
                    dict(label="Etapas / día (real)", value=fmt_num(ult["Etapas/Día (Real)"]), delta=d_etd, delta_tipo=k_etd, color=TEMA["accent"]),
                    dict(label="Bombeo continuo", value=fmt_num(hrs_cp_rango), unit="hr", hint=f"sobre {fmt_num(hrs_bombeo_rango)} hr de bombeo en el período", color=TEMA["frac"]),
                    dict(label="Máx. tren continuo", value=fmt_num(max_cp_rango), unit="hr", hint="mayor bloque diario del período", color=TEMA["frac"]),
                ])

                # ---- Gráficos ----
                seccion("Evolución diaria del CP", f"{sel_pad_c1}")
                fechas_x = a_fecha(df_cuadro1_filtrado["Fecha Reporte"])
                g1, g2 = st.columns([1, 1])
                with g1:
                    fig = go.Figure()
                    fig.add_bar(x=fechas_x, y=pd.to_numeric(df_cuadro1_filtrado["Etapas Día"], errors="coerce"), name="Etapas del día",
                                marker=dict(color=TEMA["etapas"], line_width=0), hovertemplate="%{y} etapas<extra>Etapas</extra>")
                    fig.add_bar(x=fechas_x, y=pd.to_numeric(df_cuadro1_filtrado["CP Logrados"], errors="coerce"), name="CP logrados",
                                marker=dict(color=TEMA["cp"], line_width=0), hovertemplate="%{y} CP<extra>CP</extra>")
                    linea_std(fig, std_yac, f"STD {fmt_num(std_yac)} etapas/día")
                    estilo_fig(fig, alto=280, leyenda=True)
                    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.06,
                                      title=dict(text="Etapas y CP por día", font=dict(size=13, color=TEMA["text"]), x=0, xanchor="left"),
                                      legend=dict(y=1.0, x=1, xanchor="right"), margin=dict(t=40))
                    fig.update_xaxes(tickformat="%d/%m")
                    mostrar_fig(fig)
                with g2:
                    pct_pad = pd.to_numeric(df_cuadro1_filtrado["% CP (PAD)"].astype(str).str.rstrip("%"), errors="coerce")
                    pct_dia = pd.to_numeric(df_cuadro1_filtrado["% CP (Día)"].astype(str).str.rstrip("%"), errors="coerce")
                    fig = go.Figure()
                    fig.add_bar(x=fechas_x, y=pct_dia, name="% CP del día", marker=dict(color=TEMA["accent_soft"], line=dict(color=TEMA["accent"], width=1)),
                                hovertemplate="%{y:.1f}%<extra>Día</extra>")
                    fig.add_scatter(x=fechas_x, y=pct_pad, name="% CP acumulado PAD", mode="lines+markers",
                                    line=dict(color=TEMA["cp"], width=2.5), marker=dict(size=8, color=TEMA["cp"], line=dict(color=TEMA["surface"], width=2)),
                                    hovertemplate="%{y:.1f}%<extra>PAD acum.</extra>")
                    estilo_fig(fig, alto=280, leyenda=True)
                    fig.update_layout(title=dict(text="% de CP: día y acumulado del PAD", font=dict(size=13, color=TEMA["text"]), x=0, xanchor="left"),
                                      legend=dict(y=1.0, x=1, xanchor="right"), margin=dict(t=40))
                    fig.update_yaxes(range=[0, 105], ticksuffix="%")
                    fig.update_xaxes(tickformat="%d/%m")
                    mostrar_fig(fig)

            # ---- Gantt ----
            seccion("Línea de tiempo · FRAC y CP", "Cada barra es un tramo de bombeo; en verde las etapas que arrancaron con bombeo continuo")
            leyenda([("FRAC con CP", TEMA["cp"]), ("FRAC sin CP", TEMA["sin_cp"])])

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
                                      color_discrete_map={"FRAC (Con CP)": TEMA["cp"], "FRAC (Sin CP)": TEMA["sin_cp"]},
                                      category_orders={"Tipo_FRAC": ["FRAC (Con CP)", "FRAC (Sin CP)"]},
                                      custom_data=['nombre_pozo', 'Etapa Nro', 'Inicio_str', 'Fin_str'])

                    fig.update_traces(hovertemplate="<b>%{customdata[0]} - Etapa %{customdata[1]}</b><br>Inicio = %{customdata[2]}<br>Fin = %{customdata[3]}<extra></extra>",
                                      marker=dict(line=dict(color=TEMA["surface"], width=1)))
                    n_pozos = int(df_gantt['nombre_pozo'].nunique())
                    estilo_fig(fig, alto=max(300, 120 + 48 * n_pozos), leyenda=False, hover="closest")
                    fig.update_layout(barmode='overlay', bargap=0.25)
                    fig.update_yaxes(autorange="reversed", type='category', showgrid=True, gridcolor=TEMA["grid"], tickfont=dict(color=TEMA["text"], size=12))
                    fig.update_xaxes(showgrid=True, gridcolor=TEMA["grid"], tickformat="%d/%m\n%H:%M")
                    mostrar_fig(fig)
                else:
                    aviso("No hay datos de bombeo para el rango seleccionado.")
            else:
                aviso("No hay datos procesados disponibles.")

            # ---- Cuadro 1: evolución CP ----
            seccion("Cuadro 1 · Evolución CP", f"{sel_pad_c1} · un renglón por día de reporte")

            def clases_cp(f, col):
                if col in ("Etapas STD", "Etapas Posibles CP (Acum-4)", "Etapas Acum."):
                    return "dim"
                if col == "Etapas/Día (Real)":
                    try:
                        return "good" if float(f[col]) >= float(f["Etapas STD"]) else "bad"
                    except Exception:
                        return None
                if col == "CP Logrados":
                    try:
                        return "strong" if float(f[col]) > 0 else "dim"
                    except Exception:
                        return None
                return None

            tabla(df_cuadro1_filtrado,
                  columnas=[("Fecha Reporte", "Fecha", "txt"), ("Etapas Acum.", "Acum.", "num"), ("Etapas Día", "Día", "num"),
                            ("Etapas Posibles CP (Acum-4)", "Posibles CP", "num"),
                            ("CP Logrados", "Logrados", "num"), ("% CP (Día)", "% Día", "pct"), ("% CP (PAD)", "% PAD", "pct"),
                            ("Etapas STD", "STD", "num"), ("Etapas/Día (Real)", "Real", "num"),
                            ("Tiempo de Bombeo (hr)", "Bombeo", "num"), ("Tiempo Total de Bombeo Continuo (hr)", "Continuo", "num"),
                            ("Maximo Tiempo de Bombeo Continuo Diario (hr)", "Máx. tren", "num")],
                  grupos=[("", 1, None), ("Etapas", 3, None), ("Continuous Pumping", 3, None), ("Etapas / día", 2, None), ("Horas de bombeo", 3, None)],
                  clases=clases_cp, alto_max=520, compacta=True)

            col_t1, col_t2 = st.columns(2)

            with col_t1:
                seccion("Listado de transiciones", chica=True)
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
                if not tabla1.empty:
                    tabla1 = tabla1.copy()
                    tabla1["Transición (Pozo/Etapa)"] = [esc(v).replace("-&gt;", "<span style='color:var(--muted)'>→</span>") for v in tabla1["Transición (Pozo/Etapa)"]]
                tabla(tabla1, columnas=[("Fecha de Reporte", "Fecha", "txt"), ("Transición (Pozo/Etapa)", "Transición (pozo / etapa)", "rawtxt")], alto_max=420,
                      pie=f"{len(tabla1)} transiciones con CP en el rango" if not tabla1.empty else None)

            with col_t2:
                seccion("Etapas con Continuous Pumping", chica=True)
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
                tabla(tabla2, columnas=[("Fecha de Reporte", "Fecha", "txt"), ("Pozo", "Pozo", "txt"), ("Etapa Nro", "Etapa", "num"), ("Secuencia Diaria", "Sec. diaria", "num")],
                      alto_max=420, pie=f"{len(tabla2)} etapas con CP en el rango" if not tabla2.empty else None)

        with tab4:
            col_m3, col_m4 = st.columns(2)

            def_yacs = [yac_def_c] if yac_def_c != "S/D" else yacimientos_disp
            with col_m3: sel_yac_c2 = st.multiselect("Yacimiento(s)", yacimientos_disp, default=def_yacs, key="yac_c2")

            pads_disp_c2 = df_h9[df_h9['Yacimiento'].isin(sel_yac_c2)]['PAD'].dropna().unique().tolist()
            def_pads = [pad_def_c] if pad_def_c in pads_disp_c2 else pads_disp_c2
            with col_m4: sel_pad_c2 = st.multiselect("PAD(s)", pads_disp_c2, default=def_pads, key="pad_c2")

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

                            max_dia = df_bloques_hoy.groupby('bloque_id')['minutos_en_ventana'].sum().max() / 60 if not df_bloques_hoy.empty else 0
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
            df_resumen_cp = pd.DataFrame(resumen_cp) if resumen_cp else pd.DataFrame(columns=columnas_cuadro1)

            seccion("Cuadro 1 · Foto final por PAD")

            def clases_res(f, col):
                if col in ("Etapas STD", "Posibles CP (Total - 4)", "Última Fecha"):
                    return "dim"
                if col == "Etapas/Día (Real)":
                    try:
                        return "good" if float(f[col]) >= float(f["Etapas STD"]) else "bad"
                    except Exception:
                        return None
                if col == "% CP Final (PAD)":
                    return "strong"
                return None

            tabla(df_resumen_cp,
                  columnas=[("Yacimiento", "Yacimiento", "txt"), ("PAD", "PAD", "txt"), ("Última Fecha", "Último día", "txt"),
                            ("Etapas Acum.", "Acum.", "num"), ("Posibles CP (Total - 4)", "Posibles CP", "num"),
                            ("Total CP Logrados", "Logrados", "num"), ("% CP Final (PAD)", "% CP final", "pct"),
                            ("Etapas STD", "STD", "num"), ("Etapas/Día (Real)", "Real", "num"),
                            ("Tiempo de Bombeo (hr)", "Bombeo", "num"), ("Tiempo Total de Bombeo Continuo (hr)", "Continuo", "num"), ("Máx. Diario CP (hr)", "Máx. diario", "num")],
                  grupos=[("", 3, None), ("Etapas", 2, None), ("Continuous Pumping", 2, None), ("Etapas / día", 2, None), ("Horas de bombeo", 3, None)],
                  clases=clases_res)

            if not df_resumen_cp.empty:
                seccion("% de CP final por PAD", chica=True)
                pct_final = pd.to_numeric(df_resumen_cp["% CP Final (PAD)"].astype(str).str.rstrip("%"), errors="coerce")
                fig = go.Figure()
                fig.add_bar(x=pct_final, y=df_resumen_cp["PAD"], orientation="h", marker=dict(color=TEMA["cp"], line_width=0),
                            text=[f"{v:.1f}%" for v in pct_final], textposition="outside", textfont=dict(color=TEMA["text"], size=12),
                            hovertemplate="%{x:.1f}% de CP<extra>%{y}</extra>")
                estilo_fig(fig, alto=max(200, 70 + 40 * len(df_resumen_cp)), leyenda=False, hover="closest")
                fig.update_xaxes(range=[0, 115], ticksuffix="%", showgrid=True, gridcolor=TEMA["grid"])
                fig.update_yaxes(showgrid=False, autorange="reversed", tickfont=dict(color=TEMA["text"], size=12))
                fig.update_layout(bargap=0.4)
                mostrar_fig(fig)

            seccion("Cuadro 2 · Resumen por yacimiento")
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
            df_resumen_yac = pd.DataFrame(resumen_yac) if resumen_yac else pd.DataFrame(columns=columnas_cuadro2)

            def clases_yac(f, col):
                if col == "Etapas STD":
                    return "dim"
                if col == "Etapas/Día (Yacimiento)":
                    try:
                        return "good" if float(f[col]) >= float(f["Etapas STD"]) else "bad"
                    except Exception:
                        return None
                if col == "% CP Final (Yacimiento)":
                    return "strong"
                return None

            tabla(df_resumen_yac,
                  columnas=[("Yacimiento", "Yacimiento", "txt"), ("Etapas Acumuladas", "Acum.", "num"),
                            ("Total CP Realizados", "Logrados", "num"), ("% CP Final (Yacimiento)", "% CP final", "pct"),
                            ("Etapas STD", "STD", "num"), ("Etapas/Día (Yacimiento)", "Real", "num"),
                            ("Tiempo de Bombeo (hr)", "Bombeo", "num"), ("Tiempo Total de Bombeo Continuo (hr)", "Continuo", "num"), ("Máx. Diario CP (hr)", "Máx. diario", "num")],
                  grupos=[("", 1, None), ("Etapas", 1, None), ("Continuous Pumping", 2, None), ("Etapas / día", 2, None), ("Horas de bombeo", 3, None)],
                  clases=clases_yac)

except Exception as e:
    aviso(f"Ocurrió un error al procesar el tablero. Detalles: {e}", "error")
