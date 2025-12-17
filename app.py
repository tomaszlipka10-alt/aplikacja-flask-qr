from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import Babel, _
import os

# --- KONFIGURACJA ŚCIEŻEK ---
basedir = os.path.abspath(os.path.dirname(__file__))
DB_NAME = 'warehouse.db'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'twoj_super_tajny_klucz_dla_aplikacji_wms')
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

# --- TRASA DLA RENDER (HEALTH CHECK) ---
@app.route('/health')
def health():
    return "OK", 200

# --- NAGŁÓWKI DLA KAMERY ---
@app.after_request
def add_security_headers(response):
    response.headers['Permissions-Policy'] = 'camera=(self)'
    return response

# --- MODELE BAZY ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    full_name = db.Column(db.String(100))

# ... (Tutaj zachowaj pozostałe modele: Product, Location, AuditLog) ...

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- TRASY (ROUTES) ---
@app.route('/')
@login_required
def index():
    return render_template('index.html', welcome_title=_("Panel Magazyniera"))

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

# --- INICJALIZACJA BAZY ---
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        db.session.add(User(
            username='admin', 
            password=generate_password_hash('admin123'), 
            full_name='Administrator'
        ))
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)