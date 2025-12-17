from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import Babel, _
import datetime
import qrcode
from io import BytesIO
import os
from datetime import timezone

# --- KONFIGURACJA ŚCIEŻEK DLA RENDER ---
basedir = os.path.abspath(os.path.dirname(__file__))
DB_NAME = 'warehouse.db'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'twoj_super_tajny_klucz_dla_aplikacji_wms')
# Używamy ścieżki absolutnej do pliku bazy - kluczowe na Render.com
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(basedir, DB_NAME)}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- KONFIGURACJA FLASK-BABEL ---
app.config['BABEL_DEFAULT_LOCALE'] = 'pl'
app.config['LANGUAGES'] = {'pl': 'Polski', 'en': 'English', 'nl': 'Nederlands'}

def get_locale():
    return session.get('lang', request.accept_languages.best_match(app.config['LANGUAGES'].keys()))

babel = Babel(app, locale_selector=get_locale)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- DODANIE NAGŁÓWKÓW DLA KAMERY (WAŻNE DLA RENDER) ---
@app.after_request
def add_security_headers(response):
    response.headers['Permissions-Policy'] = 'camera=(self)'
    return response

# --- MODELE BAZY DANYCH ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    full_name = db.Column(db.String(100))

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=5)
    location_id = db.Column(db.Integer, db.ForeignKey('location.id'))

class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.datetime.now(timezone.utc))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action_type = db.Column(db.String(20)) # 'receive' lub 'issue'
    amount = db.Column(db.Integer)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- TRASY (ROUTES) ---

@app.route('/')
@login_required
def index():
    return render_template('index.html', 
        welcome_title=_("Panel Magazyniera"),
        tab_inventory=_("Stan Magazynowy"),
        tab_audit=_("Historia Operacji"),
        action_receive=_("Przyjmij Towar"),
        action_issue=_("Wydaj Towar"),
        action_new_product=_("Nowy Produkt"),
        logout_link=_("Wyloguj")
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        return render_template('login.html', error=_("Błędny login lub hasło"))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/set_language/<lang>')
def set_language(lang):
    if lang in app.config['LANGUAGES']:
        session['lang'] = lang
    return redirect(request.referrer or url_for('index'))

# --- API ---

@app.route('/api/products', methods=['GET'])
@login_required
def get_products():
    products = Product.query.all()
    data = []
    for p in products:
        loc = Location.query.get(p.location_id)
        data.append({
            'id': p.id,
            'item_number': p.item_number,
            'name': p.name,
            'current_stock': p.current_stock,
            'location_name': loc.name if loc else '---'
        })
    return jsonify(data=data)

@app.route('/api/stock/receive', methods=['POST'])
@login_required
def receive_stock():
    data = request.get_json()
    product = Product.query.get(data['product_id'])
    amount = int(data['amount'])
    product.current_stock += amount
    db.session.add(AuditLog(product_id=product.id, user_id=current_user.id, action_type='receive', amount=amount))
    db.session.commit()
    return jsonify(message=_("Pomyślnie przyjęto towar."))

@app.route('/api/stock/issue', methods=['POST'])
@login_required
def issue_stock():
    data = request.get_json()
    product = Product.query.get(data['product_id'])
    amount = int(data['amount'])
    if product.current_stock < amount:
        return jsonify(message=_("Błąd: Niewystarczająca ilość na stanie.")), 400
    product.current_stock -= amount
    db.session.add(AuditLog(product_id=product.id, user_id=current_user.id, action_type='issue', amount=amount))
    db.session.commit()
    return jsonify(message=_("Pomyślnie wydano towar."))

@app.route('/api/auth/qr_login', methods=['POST'])
def qr_login():
    data = request.get_json()
    token = data.get('token')
    user = User.query.filter_by(username=token).first()
    if user:
        login_user(user)
        return jsonify(success=True)
    return jsonify(success=False, message=_("Nieprawidłowy kod QR")), 401

@app.route('/generate_qr/<data>')
@login_required
def generate_qr(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    return send_file(buffer, mimetype='image/png')

# --- INICJALIZACJA ---

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Tworzenie testowego użytkownika i lokalizacji jeśli puste
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(username='admin', password=generate_password_hash('admin123'), full_name='Administrator'))
        if not Location.query.first():
            db.session.add(Location(name='Magazyn Główny'))
        db.session.commit()
    app.run()