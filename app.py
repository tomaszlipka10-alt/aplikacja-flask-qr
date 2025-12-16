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

# Nazwa pliku bazy danych SQLite
DB_NAME = 'warehouse.db'

# Inicjalizacja Aplikacji i Konfiguracja Bazy Danych
app = Flask(__name__)
# Upewnij się, że SECRET_KEY jest unikalny i złożony
app.config['SECRET_KEY'] = 'twoj_super_tajny_klucz_dla_aplikacji_wms'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- KONFIGURACJA FLASK-BABEL ---
app.config['BABEL_DEFAULT_LOCALE'] = 'pl'
app.config['LANGUAGES'] = {
    'pl': 'Polski',
    'en': 'English',
    'nl': 'Nederlands'
}

babel = Babel()

def get_locale():
    # Pobierz język z sesji.
    lang = session.get('lang', app.config['BABEL_DEFAULT_LOCALE'])
    if lang in app.config['LANGUAGES']:
        return lang
    return app.config['BABEL_DEFAULT_LOCALE']

babel.init_app(app, locale_selector=get_locale)
# ---------------------------------------------------

# Trasa do zmiany języka
@app.route('/set_language/<language>')
def set_language(language):
    if language in app.config['LANGUAGES']:
        session['lang'] = language
    return redirect(request.referrer or url_for('index'))

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = _("Proszę się zalogować, aby uzyskać dostęp do tej strony.")


# Definicje Modeli
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    full_name = db.Column(db.String(120))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

class Unit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    min_stock_level = db.Column(db.Integer, default=0)
    
    location_id = db.Column(db.Integer, db.ForeignKey('location.id'))
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))

    location = db.relationship('Location', backref='products')
    unit = db.relationship('Unit', backref='products')
    supplier = db.relationship('Supplier', backref='products')

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Używamy UTC
    timestamp = db.Column(db.DateTime, default=lambda: datetime.datetime.now(timezone.utc))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    transaction_type = db.Column(db.String(20)) # 'RECEIVE', 'ISSUE' lub 'CREATE'
    change_amount = db.Column(db.Integer)

    product = db.relationship('Product', backref='logs')
    user = db.relationship('User', backref='logs')


# Funkcja ładowania użytkownika dla Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# FUNKCJA INICJALIZUJĄCA DANE TESTOWE (POPRAWIONA)
def initialize_db():
    with app.app_context():
        db.create_all()

        # Inicjalizacja Użytkowników
        if not User.query.first():
            print("Inicjalizacja użytkowników...")
            admin = User(username='admin', full_name='Administrator Systemu')
            admin.set_password('admin123')
            db.session.add(admin)
            
            user1 = User(username='magazynier', full_name='Jan Kowalski')
            user1.set_password('magazyn123')
            db.session.add(user1)
        
        # Inicjalizacja Słowników (musimy zdefiniować obiekty loc/unit/sup niezależnie od tego, czy są nowe, czy istniejące)
        
        # LOKALIZACJE
        if not Location.query.first():
            print("Inicjalizacja lokalizacji...")
            locA1 = Location(name='A1 - Półka Wysoka')
            locB2 = Location(name='B2 - Regał Niski')
            locC3 = Location(name='C3 - Zewnętrzny Magazyn')
            db.session.add_all([locA1, locB2, locC3])
        else:
            locA1 = Location.query.filter_by(name='A1 - Półka Wysoka').first()
            locB2 = Location.query.filter_by(name='B2 - Regał Niski').first()
            locC3 = Location.query.filter_by(name='C3 - Zewnętrzny Magazyn').first()

        # JEDNOSTKI
        if not Unit.query.first():
            print("Inicjalizacja jednostek...")
            unitSzt = Unit(name='szt.')
            unitKg = Unit(name='kg')
            unitMb = Unit(name='mb')
            unitL = Unit(name='l')
            db.session.add_all([unitSzt, unitKg, unitMb, unitL])
        else:
            unitSzt = Unit.query.filter_by(name='szt.').first()
            unitKg = Unit.query.filter_by(name='kg').first()
            unitMb = Unit.query.filter_by(name='mb').first()
            unitL = Unit.query.filter_by(name='l').first()

        # DOSTAWCY
        if not Supplier.query.first():
            print("Inicjalizacja dostawców...")
            supABC = Supplier(name='Dostawca Techniczny ABC')
            supMat = Supplier(name='Dostawca Materiałów Budowlanych')
            db.session.add_all([supABC, supMat, Supplier(name='Lokalny Producent Śrub')])
        else:
            supABC = Supplier.query.filter_by(name='Dostawca Techniczny ABC').first()
            supMat = Supplier.query.filter_by(name='Dostawca Materiałów Budowlanych').first()
            # Nie musimy pobierać wszystkich, wystarczy te użyte w produktach testowych

        # Zatwierdzamy wszystkie zmiany dotyczące słowników (lokalizacje, jednostki, dostawcy, użytkownicy)
        try:
            db.session.commit()
        except Exception as e:
            # W przypadku błędu (np. race condition w reloaderze), rollback i kontynuuj
            db.session.rollback()
            print(f"Ostrzeżenie: Nie udało się zatwierdzić wszystkich danych słownikowych: {e}")
            # Ponowne pobranie, aby upewnić się, że obiekty są w aktualnym stanie sesji
            locA1 = Location.query.filter_by(name='A1 - Półka Wysoka').first()
            # itd. - w praktyce wystarczy ponowne uruchomienie
            
        # PRODUKTY (dodawane tylko raz)
        if not Product.query.first():
            print("Inicjalizacja przykładowych produktów...")
            
            # Musimy upewnić się, że pobierzemy admina (potrzebnego do logów)
            admin_user = User.query.filter_by(username='admin').first()
            
            prod_list = [
                Product(item_number='M8-010', name='Śruba M8x10, stal nierdzewna', current_stock=2500, min_stock_level=1500, location=locB2, unit=unitSzt, supplier=supMat),
                Product(item_number='CAB-ETH-10', name='Kabel Ethernet 10m UTP', current_stock=15, min_stock_level=30, location=locA1, unit=unitSzt, supplier=supABC),
                Product(item_number='PLT-P-20', name='Płytka PCB prototypowa 20x20', current_stock=5, min_stock_level=10, location=locA1, unit=unitSzt, supplier=supABC),
                Product(item_number='CON-PVC-100', name='Koncentrat PVC - Biały', current_stock=500, min_stock_level=500, location=locC3, unit=unitKg, supplier=supMat),
                Product(item_number='INS-WIR-2', name='Izolowany przewód 2.5mm', current_stock=1500, min_stock_level=500, location=locB2, unit=unitMb, supplier=supABC),
                Product(item_number='OIL-HYD-5', name='Olej hydrauliczny HLP 46', current_stock=10, min_stock_level=20, location=locC3, unit=unitL, supplier=supMat)
            ]
            db.session.add_all(prod_list)
            db.session.commit()
            
            # Dodawanie logów audytu dla przykładu (działa tylko, jeśli produkty zostały stworzone)
            if admin_user:
                print("Dodawanie logów audytu dla przykładu...")
                for prod in prod_list:
                    if prod.current_stock > 0:
                        log = AuditLog(
                            product_id=prod.id,
                            user_id=admin_user.id,
                            transaction_type='RECEIVE',
                            change_amount=prod.current_stock
                        )
                        db.session.add(log)
                db.session.commit()

        print("Baza danych gotowa.")


# TRASY WIDOKÓW
@app.route('/')
@login_required
def index():
    return render_template('index.html',
                            current_user=current_user,
                            # Globalne elementy
                            welcome_title=_('Magazyn [%s]') % current_user.username,
                            welcome_action_title=_('CO ROBIMY?'),
                            logout_link=_('Wyloguj'),
                            select_lang_text=_('Wybierz język'),
                            
                            # Zakładki/Sidebar
                            tab_inventory=_('Stan Magazynowy'),
                            tab_audit=_('Rejestr Audytu'),
                            tab_search=_('Wyszukiwanie'),
                            
                            # Przyciski Akcji/Tytuły Modali
                            action_receive=_('PRZYJĘCIE MAT.'),
                            action_issue=_('WYDANIE MAT.'),
                            action_picking=_('PICKING LISTS'),
                            action_new_product=_('Dodaj Produkt'),
                            # Tytuły tabeli Stan Magazynowy
                            th_id=_('ID'), th_name=_('Nazwa'), th_item_number=_('Indeks'),
                            th_stock=_('Stan'), th_min_level=_('Min. Poziom'), th_location=_('Lokalizacja'),
                            th_supplier=_('Dostawca'), th_unit=_('Jednostka'),
                            
                            # Tytuły tabeli Rejestr Audytu
                            th_time=_('Czas'), th_product=_('Produkt'), th_type=_('Typ'),
                            th_change_amount=_('Ilość Zmiany'), th_user=_('Użytkownik'),

                            # Etykiety Modali
                            item_number_label=_('Indeks/Numer Produktu (Unikalny)'),
                            name_label=_('Nazwa Produktu'),
                            location_id_label=_('Lokalizacja'),
                            unit_id_label=_('Jednostka'),
                            supplier_id_label=_('Dostawca'),
                            min_level_label=_('Min. Poziom Zapasu'),
                            product_id_label=_('ID Produktu'),
                            amount_label=_('Ilość'),

                            # Przyciski Modali
                            add_to_db_button=_('Dodaj do Bazy'),
                            receive_button=_('Przyjmij'),
                            issue_button=_('Wydaj')
                            )

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('index')) 
        else:
            error = _('Nieprawidłowa nazwa użytkownika lub hasło.')
            
    return render_template('login.html',
                            login_text=_('Logowanie do Systemu Magazynowego'),
                            username_label=_('Nazwa Użytkownika'),
                            password_label=_('Hasło'),
                            login_button=_('Zaloguj'),
                            select_lang_text=_('Wybierz język:'),
                            error=error)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- TRASY API ---

@app.route('/api/products', methods=['GET'])
@login_required
def get_products():
    products = Product.query.all()
    product_list = [{
        'id': p.id,
        'item_number': p.item_number,
        'name': p.name,
        'current_stock': p.current_stock,
        'min_stock_level': p.min_stock_level,
        'location_name': p.location.name if p.location else _('Brak'),
        'unit_name': p.unit.name if p.unit else _('Brak'),
        'supplier_name': p.supplier.name if p.supplier else _('Brak'),
    } for p in products]
    return jsonify(data=product_list)

@app.route('/api/products', methods=['POST'])
@login_required
def create_product():
    data = request.get_json()
    item_number = data.get('item_number')
    name = data.get('name')
    location_id = data.get('location_id')
    unit_id = data.get('unit_id')
    supplier_id = data.get('supplier_id')
    min_stock_level = data.get('min_stock_level', 0)
    initial_stock = data.get('initial_stock', 0)
    
    try:
        initial_stock = int(initial_stock)
        min_stock_level = int(min_stock_level)
    except ValueError:
        return jsonify(message=_("Wartości stanu muszą być liczbami."), success=False), 400

    if not item_number or not name:
        return jsonify(message=_("Brak numeru indeksu lub nazwy produktu."), success=False), 400

    if Product.query.filter_by(item_number=item_number).first():
        return jsonify(message=_("Produkt o podanym numerze indeksu już istnieje."), success=False), 409

    location = Location.query.get(location_id)
    unit = Unit.query.get(unit_id)
    supplier = Supplier.query.get(supplier_id)

    if not location or not unit:
        return jsonify(message=_("Nieprawidłowy ID lokalizacji lub jednostki."), success=False), 400

    new_product = Product(
        item_number=item_number,
        name=name,
        current_stock=initial_stock,
        min_stock_level=min_stock_level,
        location=location,
        unit=unit,
        supplier=supplier
    )
    db.session.add(new_product)
    
    db.session.flush() # Uzyskanie ID produktu
    
    # Tworzenie logu
    transaction_type = 'CREATE'
    if initial_stock > 0:
        transaction_type = 'RECEIVE'
        
    log = AuditLog(
        product_id=new_product.id,
        user_id=current_user.id,
        transaction_type=transaction_type,
        change_amount=initial_stock
    )
    db.session.add(log)
    db.session.commit()

    return jsonify(message=_("Produkt '%(name)s' został pomyślnie dodany do bazy (początkowy stan: %(stock)d).", name=name, stock=initial_stock), success=True), 201


@app.route('/api/product/update_location', methods=['POST'])
@login_required
def update_product_location():
    data = request.get_json()
    product_id = data.get('product_id')
    location_id = data.get('location_id')
    
    product = Product.query.get(product_id)
    location = Location.query.get(location_id)

    if not product:
        return jsonify(message=_("Nie znaleziono produktu."), success=False), 404
    if not location:
        return jsonify(message=_("Nie znaleziono lokalizacji."), success=False), 404

    product.location_id = location_id
    db.session.commit()
    
    return jsonify(message=_("Lokalizacja produktu %(name)s zmieniona na %(loc)s.", name=product.name, loc=location.name), success=True), 200


@app.route('/api/stock/receive', methods=['POST'])
@login_required
def receive_stock():
    data = request.get_json()
    product_id = data.get('product_id')
    amount = data.get('amount')

    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError
    except:
        return jsonify(message=_("Ilość musi być dodatnią liczbą całkowitą."), success=False), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify(message=_("Produkt o podanym ID nie istnieje."), success=False), 404

    try:
        product.current_stock += amount
        
        log = AuditLog(
            product_id=product.id,
            user_id=current_user.id,
            transaction_type='RECEIVE',
            change_amount=amount
        )
        db.session.add(log)
        db.session.commit()
        
        return jsonify(message=_("Przyjęto %(amount)d %(unit)s produktu: %(name)s. Nowy stan: %(stock)d.", amount=amount, unit=product.unit.name, name=product.name, stock=product.current_stock), success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(message=_("Wystąpił błąd podczas przyjmowania towaru: ") + str(e), success=False), 500

@app.route('/api/stock/issue', methods=['POST'])
@login_required
def issue_stock():
    data = request.get_json()
    product_id = data.get('product_id')
    amount = data.get('amount')

    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError
    except:
        return jsonify(message=_("Ilość musi być dodatnią liczbą całkowitą."), success=False), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify(message=_("Produkt o podanym ID nie istnieje."), success=False), 404

    if product.current_stock < amount:
        return jsonify(message=_("Błąd: Próba wydania %(amount)d, dostępny stan to tylko %(stock)d.", amount=amount, stock=product.current_stock), success=False), 400

    try:
        product.current_stock -= amount
        
        log = AuditLog(
            product_id=product.id,
            user_id=current_user.id,
            transaction_type='ISSUE',
            change_amount=-amount
        )
        db.session.add(log)
        db.session.commit()
        
        return jsonify(message=_("Wydano %(amount)d %(unit)s produktu: %(name)s. Nowy stan: %(stock)d.", amount=amount, unit=product.unit.name, name=product.name, stock=product.current_stock), success=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(message=_("Wystąpił błąd podczas wydawania towaru: ") + str(e), success=False), 500

@app.route('/api/audit', methods=['GET'])
@login_required
def get_audit_logs():
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).all()
    type_map = {
        'RECEIVE': _('Przyjęcie'),
        'ISSUE': _('Wydanie'),
        'CREATE': _('Utworzenie Produktu')
    }

    log_list = [{
        'id': l.id,
        'timestamp': l.timestamp.replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S'),
        'product_name': l.product.name if l.product else _('Nieznany Produkt'),
        'transaction_type': type_map.get(l.transaction_type, _('Inne')),
        'change_amount': l.change_amount,
        'user_full_name': l.user.full_name if l.user else _('System')
    } for l in logs]
    return jsonify(data=log_list)

@app.route('/api/generate_qr', methods=['GET'])
@login_required
def generate_qr():
    data = request.args.get('data')
    if not data:
        return jsonify({'message': _('Brak danych do zakodowania.')}), 400

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, 'PNG')
    buffer.seek(0)
    
    return send_file(buffer, mimetype='image/png', as_attachment=False)

# Trasy pomocnicze (lokalizacje, jednostki, dostawcy)
@app.route('/api/locations', methods=['GET'])
@login_required
def get_locations():
    locations = Location.query.all()
    return jsonify(data=[{'id': loc.id, 'name': loc.name} for loc in locations])

@app.route('/api/units', methods=['GET'])
@login_required
def get_units():
    units = Unit.query.all()
    return jsonify(data=[{'id': u.id, 'name': u.name} for u in units])

@app.route('/api/suppliers', methods=['GET'])
@login_required
def get_suppliers():
    suppliers = Supplier.query.all()
    return jsonify(data=[{'id': s.id, 'name': s.name} for s in suppliers])

# Trasa do logowania QR
@app.route('/api/auth/qr_login', methods=['POST'])
def qr_login():
    data = request.get_json()
    qr_code_data = data.get('token')
    
    if not qr_code_data:
        return jsonify(message=_("Brak danych kodu QR."), success=False), 400

    # Założenie: KOD QR zawiera 'username'
    user = User.query.filter_by(username=qr_code_data).first()
    
    if user:
        login_user(user)
        return jsonify(message=_("Zalogowano pomyślnie przez kod QR."), success=True)
    else:
        return jsonify(message=_("Nie znaleziono użytkownika dla podanego kodu QR."), success=False), 401


if __name__ == '__main__':
    # Funkcja initialize_db() teraz bezpiecznie inicjuje dane i obsługuje reloader
    initialize_db()
        
    # Uruchomienie publiczne (dla smartfona)
    app.run(debug=True, host='0.0.0.0')