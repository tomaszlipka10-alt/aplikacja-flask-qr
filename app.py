import os
import secrets
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------------------------------------------------------------
# App config
# -----------------------------------------------------------------------------
app = Flask(__name__)

# Render: prefer env var, fallback for local dev
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Database (PostgreSQL on Render / SQLite local)
db_url = os.environ.get("DATABASE_URL")
if db_url:
    # Render sometimes uses postgres:// in older URLs
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # local fallback
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# -----------------------------------------------------------------------------
# Extensions
# -----------------------------------------------------------------------------
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)  # what QR will carry for now
    name = db.Column(db.String(120), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(120), default="MAG-1")

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    product_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(20), nullable=False)  # receive / issue / create
    amount = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(80), nullable=True)
    note = db.Column(db.String(255), nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# -----------------------------------------------------------------------------
# DB bootstrap + tiny SQLite migrations (no Alembic yet)
# -----------------------------------------------------------------------------
def _sqlite_column_names(table_name: str) -> set[str]:
    rows = db.session.execute(db.text(f"PRAGMA table_info({table_name})")).fetchall()
    return {r[1] for r in rows}  # second field is name

def ensure_schema():
    """Create tables and patch missing columns for SQLite (common during iteration)."""
    db.create_all()

    # Only do light ALTER TABLE migrations for SQLite.
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        return

    # user table: ensure password/full_name exist
    cols = _sqlite_column_names("user") if db.session.execute(db.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user'"
    )).fetchone() else set()

    if cols:
        if "password" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN password VARCHAR(255)"))
        if "full_name" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN full_name VARCHAR(100)"))
        db.session.commit()

    # product table: ensure core columns exist (schema evolved during iteration)
    cols = _sqlite_column_names("product") if db.session.execute(db.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='product'"
    )).fetchone() else set()

    if cols:
        # very old DBs might miss the main fields
        if "item_number" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN item_number VARCHAR(80)"))
        if "name" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN name VARCHAR(120)"))
        if "current_stock" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN current_stock INTEGER DEFAULT 0"))
        if "location_name" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN location_name VARCHAR(120) DEFAULT 'MAG-1'"))
        db.session.commit()

    # audit_log table: add columns that were introduced later
    cols = _sqlite_column_names("audit_log") if db.session.execute(db.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    )).fetchone() else set()

    if cols:
        if "created_at" not in cols:
            # SQLite supports CURRENT_TIMESTAMP as default
            db.session.execute(db.text("ALTER TABLE audit_log ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP"))
        if "note" not in cols:
            db.session.execute(db.text("ALTER TABLE audit_log ADD COLUMN note VARCHAR(255)"))
        db.session.commit()

    # Ensure admin user exists (safe even with SQLite migrations)
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        db.session.add(User(
            username="admin",
            password=generate_password_hash("admin123"),
            full_name="Administrator"
        ))
        db.session.commit()

with app.app_context():
    ensure_schema()

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
@login_required
def index():
    return render_template("index.html", current_user=current_user)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username or password")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
@app.route("/api/products", methods=["GET"])
@login_required
def api_products_get():
    products = Product.query.order_by(Product.id.desc()).all()
    return jsonify({
        "data": [{
            "id": p.id,
            "item_number": p.item_number,
            "name": p.name,
            "current_stock": p.current_stock,
            "location_name": p.location_name
        } for p in products]
    })

@app.route("/api/products", methods=["POST"])
@login_required
def api_products_post():
    data = request.get_json(silent=True) or {}
    item_number = (data.get("item_number") or "").strip()
    name = (data.get("name") or "").strip()
    location_name = (data.get("location_name") or "MAG-1").strip()
    initial_stock = data.get("initial_stock", 0)

    if not item_number or not name:
        return jsonify({"message": "item_number and name are required"}), 400

    try:
        initial_stock = int(initial_stock)
    except Exception:
        return jsonify({"message": "initial_stock must be an integer"}), 400
    if initial_stock < 0:
        return jsonify({"message": "initial_stock cannot be negative"}), 400

    # Upsert by item_number: if the product exists, update its fields.
    existing = Product.query.filter_by(item_number=item_number).first()
    if existing:
        existing.name = name
        existing.location_name = location_name
        # Only overwrite stock if caller provided it (default is 0 anyway, but keep it explicit)
        existing.current_stock = initial_stock

        db.session.add(AuditLog(
            product_id=existing.id,
            action="update",
            amount=0,
            username=current_user.username,
            note=f"Updated product {item_number} via upsert"
        ))
        db.session.commit()

        return jsonify({"message": "Product already existed — updated", "id": existing.id}), 200

    p = Product(item_number=item_number, name=name, location_name=location_name, current_stock=initial_stock)
    db.session.add(p)
    db.session.commit()

    db.session.add(AuditLog(
        product_id=p.id,
        action="create",
        amount=initial_stock,
        username=current_user.username,
        note=f"Created product {item_number}"
    ))
    db.session.commit()

    return jsonify({"message": "Product created", "id": p.id}), 201

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    amount = data.get("amount")

    try:
        product_id = int(product_id)
    except Exception:
        return jsonify({"message": "Invalid product_id"}), 400

    try:
        amount = int(amount)
    except Exception:
        return jsonify({"message": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"message": "Amount must be > 0"}), 400

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"message": "Product not found"}), 404

    if action == "receive":
        product.current_stock += amount
    elif action == "issue":
        if product.current_stock < amount:
            return jsonify({"message": "Not enough stock"}), 400
        product.current_stock -= amount
    else:
        return jsonify({"message": "Unknown action"}), 400

    db.session.add(AuditLog(
        product_id=product.id,
        action=action,
        amount=amount,
        username=current_user.username
    ))
    db.session.commit()

    return jsonify({"message": "OK"})

@app.route("/api/audit", methods=["GET"])
@login_required
def api_audit():
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    return jsonify({
        "data": [{
            "id": l.id,
            "created_at": (l.created_at.isoformat() if l.created_at else None),
            "product_id": l.product_id,
            "action": l.action,
            "amount": l.amount,
            "username": l.username,
            "note": l.note
        } for l in logs]
    })

# Placeholder (QR login is disabled)
@app.route("/api/auth/qr_login", methods=["POST"])
def qr_login():
    return jsonify({"success": False, "message": "QR login disabled"})
