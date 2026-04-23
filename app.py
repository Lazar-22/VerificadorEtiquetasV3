import os
import xmlrpc.client
import json
from datetime import datetime
import pytz
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret-key")

URL = os.getenv("ODOO_URL")
DB = os.getenv("ODOO_DB")
USER = os.getenv("ODOO_USER")
PASS = os.getenv("ODOO_PASS")

def cargar_usuarios():
    users_str = os.getenv("APP_USERS", "")
    if not users_str: return {}
    user_dict = {}
    for user_pair in users_str.split(','):
        if ':' in user_pair:
            nombre, pin = user_pair.split(':', 1)
            user_dict[nombre.strip()] = pin.strip()
    return user_dict

USUARIOS_PERMITIDOS = cargar_usuarios()

# --- FUNCIÓN MODIFICADA ---
def guardar_en_google_sheets(datos_verificacion):
    """Guarda los datos en Google Sheets de forma segura usando la URL."""
    try:
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]
        creds = None
        google_creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if google_creds_json_str:
            creds = Credentials.from_service_account_info(json.loads(google_creds_json_str), scopes=SCOPES)
        else:
            if os.path.exists('credentials.json'):
                creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
            else:
                print("❌ Error: Credenciales de Google no encontradas.")
                return

        gc = gspread.authorize(creds)
        
        # --- ¡CAMBIO IMPORTANTE AQUÍ! ---
        # Pega la URL completa de tu Google Sheet entre las comillas.
        spreadsheet_url = "https://docs.google.com/spreadsheets/d/154IImDC8gsEQR1zZ499JjD9yhqsNFVARpu0yiT05O34/edit"
        
        # Si no pones una URL, no podrá continuar.
        if spreadsheet_url == "https://docs.google.com/spreadsheets/d/154IImDC8gsEQR1zZ499JjD9yhqsNFVARpu0yiT05O34/edit":
            print("❌ Error: https://docs.google.com/spreadsheets/d/154IImDC8gsEQR1zZ499JjD9yhqsNFVARpu0yiT05O34/edit en app.py con la URL real.")
            return

        spreadsheet = gc.open_by_url(spreadsheet_url)
        # --------------------------------

        sheet = spreadsheet.sheet1
        fila = [
            datos_verificacion.get('operario', ''), datos_verificacion.get('timestamp', ''),
            datos_verificacion.get('order_name', ''), datos_verificacion.get('client_ref', ''),
            datos_verificacion.get('status', ''), datos_verificacion.get('message', ''),
            str(datos_verificacion.get('batch', ''))
        ]
        sheet.append_row(fila)
        print("✅ ¡Éxito! Datos guardados correctamente en Google Sheets.")

    except gspread.exceptions.SpreadsheetNotFound:
        print("❌ Error SpreadsheetNotFound: La URL es incorrecta o no has compartido el archivo con el 'client_email' como 'Editor'.")
    except Exception as e:
        print(f"❌ Error guardando en Google Sheets: {type(e).__name__}({e})")

# --- El resto del código se mantiene igual ---
@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user' in session: return redirect(url_for('menu'))
    user_list = list(USUARIOS_PERMITIDOS.keys())
    if request.method == 'POST':
        user = request.form.get('user')
        pin = request.form.get('pin')
        if user in USUARIOS_PERMITIDOS and USUARIOS_PERMITIDOS[user] == pin:
            session['user'] = user
            return redirect(url_for('menu'))
        else:
            return render_template('login.html', error="PIN incorrecto o usuario no seleccionado", users=user_list)
    return render_template('login.html', users=user_list)

@app.route('/menu')
def menu():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('home.html', user=session['user'])

@app.route('/falabella')
def falabella():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('falabella.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/verify', methods=['POST'])
def verify():
    if 'user' not in session: return jsonify({'status': 'error', 'message': 'Sesión expirada.'}), 401
    data = request.get_json()
    order_name, client_ref = data.get('name'), data.get('client_ref')
    timestamp_str = datetime.now(pytz.timezone("America/Santiago")).strftime('%Y-%m-%d %H:%M:%S')
    datos_para_guardar = {'operario': session.get('user', 'N/A'), 'timestamp': timestamp_str, 'order_name': order_name, 'client_ref': client_ref}
    try:
        common = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/common')
        uid = common.authenticate(DB, USER, PASS, {})
        models = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/object')
        picking_ids = models.execute_kw(DB, uid, PASS, 'stock.picking', 'search', [[('name', '=', order_name)]])
        if not picking_ids:
            datos_para_guardar.update({'status': 'error', 'message': 'La orden de venta no existe.'})
            guardar_en_google_sheets(datos_para_guardar)
            return jsonify({'status': 'error', 'message': 'La orden de venta no existe.'})
        picking_info = models.execute_kw(DB, uid, PASS, 'stock.picking', 'read', [picking_ids[0]], {'fields': ['client_order_ref', 'batch_id']})[0]
        if picking_info.get('client_order_ref') == client_ref:
            batch_name, total_in_batch = "Sin Batch", 0
            if picking_info.get('batch_id'):
                batch_info = models.execute_kw(DB, uid, PASS, 'stock.picking.batch', 'read', [picking_info['batch_id'][0]], {'fields': ['name', 'picking_ids']})[0]
                batch_name, total_in_batch = batch_info.get('name', 'N/A'), len(batch_info.get('picking_ids', []))
            result = {'status': 'success', 'message': '¡Válido!', 'batch': batch_name, 'total': total_in_batch}
            datos_para_guardar.update({'status': 'success', 'message': '¡Válido!', 'batch': batch_name})
        else:
            result = {'status': 'error', 'message': 'La Referencia del Cliente no coincide.'}
            datos_para_guardar.update({'status': 'error', 'message': 'La Referencia del Cliente no coincide.'})
        guardar_en_google_sheets(datos_para_guardar)
        return jsonify(result)
    except Exception as e:
        print(f"❌ Error durante la verificación con Odoo: {e}")
        datos_para_guardar.update({'status': 'error', 'message': f'Error de conexión: {e}'})
        guardar_en_google_sheets(datos_para_guardar)
        return jsonify({'status': 'error', 'message': 'Error de conexión con el servidor.'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)), debug=False)
