import os
import json
import datetime as dt
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text as sql_text

# ----------------------------
# App Setup
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "dev-secret"

# Obsługa bazy danych (Postgres na Render lub lokalny SQLite)
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"): 
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- MODELE BAZY DANYCH ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(100), default="MAG-1")

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50))
    action = db.Column(db.String(20))
    qty = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(100))
    username = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

# --- AUTOMATYCZNA NAPRAWA BAZY (Dodawanie kolumn) ---
def repair_database():
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        if 'product' in tables:
            p_cols = [c['name'] for c in inspector.get_columns('product')]
            if 'min_stock' not in p_cols:
                with db.engine.begin() as conn:
                    conn.execute(sql_text('ALTER TABLE product ADD COLUMN min_stock INTEGER DEFAULT 0'))

with app.app_context():
    db.create_all()
    repair_database()
    if not User.query.filter_by(username="admin").first():
        db.session.add(User(username="admin", password_hash=generate_password_hash("admin123"), is_admin=True))
        db.session.commit()

# --- TRASY (ROUTES) ---
@app.route("/")
@login_required
def index(): return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username")).first()
        if user and check_password_hash(user.password_hash, request.form.get("password")):
            login_user(user)
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "GET":
        prods = Product.query.all()
        mapped = [{"item_number": p.item_number, "name": p.name, "current_stock": p.current_stock, "min_stock": p.min_stock, "location_name": p.location_name} for p in prods]
        return jsonify({"ok": True, "products": mapped})

    data = request.get_json() or {}
    sku = str(data.get("item_number", "")).strip()
    name = str(data.get("name", "")).strip()
    min_s = int(data.get("min_stock", 0))
    qty = int(data.get("current_stock", 0))

    p = Product.query.filter_by(item_number=sku).first()
    if p:
        p.name, p.current_stock, p.min_stock, p.location_name = name, qty, min_s, data.get("location_name", "MAG-1")
    else:
        p = Product(item_number=sku, name=name, current_stock=qty, min_stock=min_s, location_name=data.get("location_name", "MAG-1"))
        db.session.add(p)
    
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json() or {}
    sku = data.get("item_number", "").strip()
    amt = int(data.get("amount") or 0)
    
    p = Product.query.filter_by(item_number=sku).first()
    if not p: return jsonify({"ok": False, "error": "Product not found"}), 404
    
    if action == "receive": 
        p.current_stock += amt
    else:
        if p.current_stock < amt: return jsonify({"ok": False, "error": "Insufficient stock"}), 400
        p.current_stock -= amt
    
    db.session.add(AuditLog(item_number=sku, action=action.upper(), qty=amt, location_name=p.location_name, username=current_user.username))
    db.session.commit()
    return jsonify({"ok": True, "current_stock": p.current_stock})

@app.route("/api/audit")
@login_required
def api_list_audit():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()
    data = [{"created_at": l.created_at.isoformat(), "type": l.action, "item_number": l.item_number, "quantity": l.qty, "username": l.username} for l in logs]
    return jsonify({"data": data})

if __name__ == "__main__":
    app.run(debug=True)