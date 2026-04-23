import os
import xmlrpc.client
import json
from datetime import datetime
import pytz  # <-- IMPORTANTE: Nueva librería para zonas horarias
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Cargar variables de entorno desde el archivo .env
load_dotenv()

app = Flask(__name__)
# Configura una clave secreta para la sesión de Flask
app.secret_key = os.getenv("FLASK_SECRET_KEY", "una-clave-secreta-muy-segura-por-defecto")

# --- Configuración de Odoo (AHORA SEGURA desde .env) ---
URL = os.getenv("ODOO_URL")
DB = os.getenv("ODOO_DB")
USER = os.getenv("ODOO_USER")
PASS = os.getenv("ODOO_PASS")

# --- BASE DE DATOS DE USUARIOS (AHORA SEGURA desde .env) ---
def cargar_usuarios_desde_env():
    users_str = os.getenv("APP_USERS", "")
    if not users_str:
        return {}
    
    user_dict = {}
    for user_pair in users_str.split(','):
        if ':' in user_pair:
            nombre, pin = user_pair.split(':', 1)
            user_dict[nombre] = pin
    return user_dict

USUARIOS_PERMITIDOS = cargar_usuarios_desde_env()
if not USUARIOS_PERMITIDOS:
    print("⚠️ Advertencia: No se cargaron usuarios desde el archivo .env. El login no funcionará.")

def guardar_en_google_sheets(datos_verificacion):
    """Guarda los datos en Google Sheets de forma segura."""
    try:
        SCOPES = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds = None
        google_creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')

        if google_creds_json_str:
            creds_dict = json.loads(google_creds_json_str)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            CREDS_FILE = 'credentials.json'
            if os.path.exists(CREDS_FILE):
                creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
            else:
                print("❌ Error: Credenciales de Google no encontradas.")
                return

        gc = gspread.authorize(creds)
        spreadsheet = gc.open("Prueba API")
        sheet = spreadsheet.sheet1
        
        fila = [
            datos_verificacion.get('operario', ''),
            datos_verificacion.get('timestamp', ''),
            datos_verificacion.get('order_name', ''),
            datos_verificacion.get('client_ref', ''),
            datos_verificacion.get('status', ''),
            datos_verificacion.get('message', ''),
            str(datos_verificacion.get('batch', ''))
        ]
        sheet.append_row(fila)
        print("✅ Datos guardados correctamente en Google Sheets.")

    except Exception as e:
        print(f"❌ Error guardando en Google Sheets: {type(e).__name__}({e})")

# --- RUTAS DE LA APLICACIÓN ---

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('menu'))

    if request.method == 'POST':
        user = request.form.get('user')
        pin = request.form.get('pin')
        if user in USUARIOS_PERMITIDOS and USUARIOS_PERMITIDOS[user] == pin:
            session['user'] = user
            return redirect(url_for('menu'))
        else:
            return render_template('login.html', error="Usuario o PIN incorrecto")
    return render_template('login.html')

@app.route('/menu')
def menu():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('home.html', user=session['user'])

@app.route('/falabella')
def falabella():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('falabella.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/verify', methods=['POST'])
def verify():
    if 'user' not in session:
        return jsonify({'status': 'error', 'message': 'Sesión expirada. Por favor, inicie sesión de nuevo.'}), 401

    data = request.get_json()
    order_name = data.get('name')
    client_ref = data.get('client_ref')
    
    # --- INICIO DE LA LÓGICA DE HORA MEJORADA ---
    santiago_tz = pytz.timezone("America/Santiago")
    timestamp_santiago = datetime.now(santiago_tz)
    timestamp_str = timestamp_santiago.strftime('%Y-%m-%d %H:%M:%S')
    # --- FIN DE LA LÓGICA DE HORA MEJORADA ---

    datos_para_guardar = {
        'operario': session.get('user', 'Desconocido'),
        'timestamp': timestamp_str, # <-- Usamos la hora corregida
        'order_name': order_name,
        'client_ref': client_ref
    }

    try:
        common = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/common')
        uid = common.authenticate(DB, USER, PASS, {})
        models = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/object')

        picking_ids = models.execute_kw(DB, uid, PASS, 'stock.picking', 'search', [[('name', '=', order_name)]])

        if not picking_ids:
            datos_para_guardar.update({'status': 'error', 'message': 'La orden de venta no existe.'})
            guardar_en_google_sheets(datos_para_guardar)
            return jsonify({'status': 'error', 'message': 'La orden de venta no existe.'})

        picking_id = picking_ids[0]
        picking_info = models.execute_kw(DB, uid, PASS, 'stock.picking', 'read', [picking_id], {'fields': ['client_order_ref', 'batch_id']})[0]

        if picking_info.get('client_order_ref') == client_ref:
            batch_name = "Sin Batch"
            total_in_batch = 0
            if picking_info.get('batch_id'):
                batch_id = picking_info['batch_id'][0]
                batch_info = models.execute_kw(DB, uid, PASS, 'stock.picking.batch', 'read', [batch_id], {'fields': ['name', 'picking_ids']})[0]
                batch_name = batch_info.get('name', 'Sin Batch')
                total_in_batch = len(batch_info.get('picking_ids', []))

            result = {'status': 'success', 'message': '¡Válido!', 'batch': batch_name, 'total': total_in_batch}
            datos_para_guardar.update({'status': 'success', 'message': '¡Válido!', 'batch': batch_name})
        else:
            result = {'status': 'error', 'message': 'La Referencia del Cliente no coincide.'}
            datos_para_guardar.update({'status': 'error', 'message': 'La Referencia del Cliente no coincide.'})

        guardar_en_google_sheets(datos_para_guardar)
        return jsonify(result)

    except Exception as e:
        print(f"❌ Error durante la verificación con Odoo: {e}")
        datos_para_guardar.update({'status': 'error', 'message': f'Error de conexión con el servidor: {e}'})
        guardar_en_google_sheets(datos_para_guardar)
        return jsonify({'status': 'error', 'message': 'Error de conexión con el servidor.'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)), debug=False)
