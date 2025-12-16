from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

# Tworzenie obiektu bazy danych
db = SQLAlchemy()

# ----------------- 1. Tabela Użytkowników (Users) -----------------
# UserMixin dodaje funkcje wymagane przez Flask-Login
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    
    # Relacja 1:Wiele do Transakcji
    transactions = db.relationship('Transaction', backref='user', lazy=True)

# ----------------- 2. Tabele Referencyjne (Listy rozwijane) -----------------
# 2a. Dostawcy
class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    products = db.relationship('Product', backref='supplier', lazy=True)

# 2b. Jednostki miary
class Unit(db.Model):
    __tablename__ = 'units'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)
    products = db.relationship('Product', backref='unit', lazy=True)

# 2c. Lokalizacje
class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    products = db.relationship('Product', backref='location', lazy=True)

# ----------------- 3. Tabela Główna (Produkty) -----------------
class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(80), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    min_stock_level = db.Column(db.Integer, default=0)
    
    # Klucze Obce (Foreign Keys) łączące się z tabelami referencyjnymi
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    
    # Relacja 1:Wiele do Transakcji
    transactions = db.relationship('Transaction', backref='product', lazy=True)

# ----------------- 4. Tabela Audytu (Transakcje) -----------------
class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False) 
    change_amount = db.Column(db.Integer, nullable=False) # ujemna dla wydania, dodatnia dla przyjęcia
    transaction_type = db.Column(db.String(10), nullable=False) # 'ISSUE' lub 'RECEIVE'
    
    # Kto i kiedy dokonał zmiany (WYMAGANE DO AUDYTU)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)