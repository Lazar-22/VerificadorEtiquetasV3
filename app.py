import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
import xmlrpc.client
import threading
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- NUEVO: CARGAR VARIABLES SECRETAS ---
from dotenv import load_dotenv
load_dotenv() # Esto lee el archivo .env silenciosamente

app = Flask(__name__)
# AHORA LA CLAVE SECRETA DE FLASK TAMBIÉN ESTÁ PROTEGIDA
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback_inseguro_por_si_acaso")

# --- Configuración Odoo (AHORA SEGURA) ---
URL = os.getenv("ODOO_URL")
DB = os.getenv("ODOO_DB")
USER = os.getenv("ODOO_USER")
PASS = os.getenv("ODOO_PASS")

# Validamos que las variables se hayan cargado correctamente
if not all([URL, DB, USER, PASS]):
    raise ValueError("⚠️ Faltan variables de entorno. Revisa tu archivo .env")

odoo_uid = None
odoo_models = None

# ... (EL RESTO DE TU CÓDIGO SIGUE EXACTAMENTE IGUAL) ...


# --- Configuración Google Sheets ---
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
SPREADSHEET_ID = "154IImDC8gsEQR1zZ499JjD9yhqsNFVARpu0yiT05O34" 

# --- NUEVO: BASE DE DATOS DE USUARIOS (NOMBRES Y PIN) ---
USUARIOS_PERMITIDOS = {
    "Jose": "1234",
    "Francisca": "5678",
    "Alejandro": "9012"
}

def get_odoo_connection():
    global odoo_uid, odoo_models
    try:
        if not odoo_uid:
            common = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/common')
            odoo_uid = common.authenticate(DB, USER, PASS, {})
            if odoo_uid:
                odoo_models = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/object')
        return odoo_uid, odoo_models
    except Exception as e:
        odoo_uid = None
        raise e

def format_order_name(name):
    clean_name = name.strip().upper()
    if not clean_name.startswith('SO') and clean_name:
        clean_name = f"SO{clean_name}"
    return clean_name

def log_to_google_sheets(operario, batch_info, total_en_batch, order_name, client_ref, status, message):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cred_path = os.path.join(base_dir, 'credentials.json')
        
        credentials = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
        gc = gspread.authorize(credentials)
        sheet = gc.open_by_key(SPREADSHEET_ID).get_worksheet(0)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        sheet.append_row([now, operario, batch_info, total_en_batch, order_name, client_ref, status, message])
        print(f"✅ Guardado en Sheets: Batch {batch_info} | SO: {order_name}")
    except Exception as e:
        print(f"❌ Error guardando en Google Sheets: {repr(e)}")

# --- RUTAS DE NAVEGACIÓN ---

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        operario = request.form.get('operario')
        pin = request.form.get('pin')
        
        # VALIDACIÓN DE SEGURIDAD
        if operario in USUARIOS_PERMITIDOS and USUARIOS_PERMITIDOS[operario] == pin:
            session['operario'] = operario
            return redirect(url_for('menu'))
        else:
            # Si la clave está mal, mandamos un mensaje de error a la pantalla
            flash("PIN incorrecto. Inténtalo de nuevo.", "error")
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/menu')
def menu():
    if 'operario' not in session:
        return redirect(url_for('login'))
    return render_template('home.html', operario=session['operario'])

@app.route('/falabella')
def falabella():
    if 'operario' not in session:
        return redirect(url_for('login'))
    return render_template('falabella.html')

@app.route('/ripley')
def ripley():
    if 'operario' not in session:
        return redirect(url_for('login'))
    return render_template('ripley.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- LÓGICA DE NEGOCIO Y ODOO ---

@app.route('/verify', methods=['POST'])
def verify():
    global odoo_uid
    data = request.json
    order_name = format_order_name(data.get('name', ''))
    client_ref = data.get('client_ref', '').strip()
    
    status = "error"
    message = "Error desconocido."
    batch_info = "Sin Batch"
    total_batch_orders = 0

    try:
        uid, models = get_odoo_connection()
        if not uid:
            status, message = "error", "Fallo de autenticación en Odoo."
        else:
            domain = [['name', '=', order_name], ['client_order_ref', '=', client_ref]]
            orders = models.execute_kw(DB, uid, PASS, 'sale.order', 'search_read', [domain], {
                'fields': ['partner_id', 'batch_ids'], 
                'limit': 1 
            })

            if orders:
                order = orders[0]
                cliente = order.get('partner_id', [False, 'Desconocido'])[1]
                batch_data = order.get('batch_ids', [])
                
                if batch_data:
                    try:
                        batches = models.execute_kw(DB, uid, PASS, 'stock.picking.batch', 'read', [batch_data], {'fields': ['name']})
                        nombres_batches = [b.get('name', 'Desconocido') for b in batches]
                        batch_info = " | ".join(nombres_batches)
                        
                        total_batch_orders = models.execute_kw(DB, uid, PASS, 'sale.order', 'search_count', [[('batch_ids', 'in', batch_data)]])
                    except Exception as e:
                        print("Error extrayendo info del batch:", e)
                        batch_info = str(batch_data)
                
                status = "success"
                message = f"Cliente: {cliente}. Lote Detectado."
            else:
                status = "error"
                message = f"No se encontró la orden {order_name} con esa referencia."
                
    except Exception as e:
        odoo_uid = None
        status = "error"
        message = f"Error de conexión Odoo: {str(e)}"

    operario = session.get('operario', 'Desconocido')
    
    threading.Thread(target=log_to_google_sheets, args=(operario, batch_info, total_batch_orders, order_name, client_ref, status, message)).start()

    return jsonify({
        "status": status, 
        "message": message,
        "batch": batch_info,
        "total": total_batch_orders
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
