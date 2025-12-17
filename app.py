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
# Używamy ścieżki absolutnej do pliku bazy
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

# --- MODELE I LOGIKA (Pozostają bez zmian jak w Twoim pliku) ---
# ... (Tutaj reszta Twoich modeli: User, Product, itp.) ...

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- TRASY (ROUTES) ---
@app.route('/')
@login_required
def index():
    return render_template('index.html', welcome_title=_("Panel Magazyniera"))

# ... (Reszta Twoich tras: login, logout, api, itp.) ...

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Inicjalizacja admina jeśli nie istnieje
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(
                username='admin', 
                password=generate_password_hash('admin123'), 
                full_name='Administrator'
            ))
            db.session.commit()
    app.run()