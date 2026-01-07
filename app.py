import os
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

from sqlalchemy import inspect, text

# ----------------------------
# App
# ----------------------------
app = Flask(__name__)

# IMPORTANT:
# - On Render, set SECRET_KEY in Environment Variables.
# - This fallback keeps the app from crashing if it's missing locally.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# ----------------------------
# Database (PostgreSQL on Render / SQLite locally)
# ----------------------------
db_url = os.environ.get("DATABASE_URL")
if db_url:
    # Render/Heroku-style URLs sometimes use postgres://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # Render instances are ephemeral. If you don't use a managed DB, store SQLite in a writable path.
    # On Render you can mount a free persistent disk at /var/data (optional). If not present, we fall back to /tmp.
    sqlite_dir = Path("/var/data") if Path("/var/data").exists() else Path("/tmp")
    sqlite_path = sqlite_dir / "warehouse.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ----------------------------
# Extensions
# ----------------------------
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------
# Models
# ----------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(100), default="MAG-1")
    unit = db.Column(db.String(30), default="")
    notes = db.Column(db.String(255), default="")
    qr_product = db.Column(db.String(255), default="")
    qr_location = db.Column(db.String(255), default="")

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer)
    action = db.Column(db.String(20))  # receive | issue
    amount = db.Column(db.Integer)
    username = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def admin_required(fn):
    """Allow only admin user (username == 'admin')."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"message": "Unauthorized"}), 401
        if getattr(current_user, "username", None) != "admin":
            return jsonify({"message": "Forbidden"}), 403
        return fn(*args, **kwargs)

    return wrapper


def _sqlite_add_column(table: str, col_name: str, col_def: str) -> None:
    """Add column in a SQLite-friendly way (no non-constant DEFAULT)."""
    # NOTE: SQLite cannot add column with DEFAULT CURRENT_TIMESTAMP on older versions.
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))


def ensure_schema() -> None:
    """Create tables and apply lightweight 'add missing column' migrations.

    This keeps Render deployments stable without a full migrations stack.
    """
    db.create_all()

    insp = inspect(db.engine)

    def has_col(table: str, col: str) -> bool:
        try:
            return col in {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return False

    # user table
    if insp.has_table("user") and not has_col("user", "password"):
        _sqlite_add_column("user", "password", "VARCHAR(255)")
    if insp.has_table("user") and not has_col("user", "full_name"):
        _sqlite_add_column("user", "full_name", "VARCHAR(100)")

    # product table
    if insp.has_table("product"):
        for col, coldef in [
            ("unit", "VARCHAR(30)"),
            ("notes", "VARCHAR(255)"),
            ("qr_product", "VARCHAR(255)"),
            ("qr_location", "VARCHAR(255)"),
        ]:
            if not has_col("product", col):
                _sqlite_add_column("product", col, coldef)

    # audit_log table
    if insp.has_table("audit_log") and not has_col("audit_log", "created_at"):
        _sqlite_add_column("audit_log", "created_at", "DATETIME")
        # backfill for existing rows
        db.session.execute(text("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

    db.session.commit()


with app.app_context():
    ensure_schema()

# ----------------------------
# Routes
# ----------------------------
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
@login_required
def index():
    # English-only UI (no translation system)
    return render_template("index.html", welcome_title="Warehouse Dashboard")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("index"))

        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ----------------------------
# API
# ----------------------------
@app.route("/api/products", methods=["GET"])
@login_required
def api_products():
    products = Product.query.all()
    return jsonify(
        {
            "data": [
                {
                    "id": p.id,
                    "item_number": p.item_number,
                    "name": p.name,
                    "current_stock": p.current_stock,
                    "location_name": p.location_name,
                    "unit": p.unit,
                    "notes": p.notes,
                    "qr_product": p.qr_product,
                    "qr_location": p.qr_location,
                }
                for p in products
            ]
        }
    )


@app.route("/api/products", methods=["POST"])
@login_required
def api_products_create():
    """Create or update a product (upsert by item_number).

    You can type values manually or scan QR codes on the UI.
    """
    payload = request.get_json(silent=True) or {}

    item_number = (payload.get("item_number") or "").strip()
    name = (payload.get("name") or "").strip()
    location_name = (payload.get("location_name") or "").strip()
    unit = (payload.get("unit") or "").strip()
    notes = (payload.get("notes") or "").strip()
    qr_product = (payload.get("qr_product") or "").strip()
    qr_location = (payload.get("qr_location") or "").strip()

    # If item/location are not provided, allow using scanned QR strings.
    if not item_number and qr_product:
        item_number = qr_product
    if not location_name and qr_location:
        location_name = qr_location

    if not item_number:
        return jsonify({"message": "item_number is required (or scan product QR)."}), 400
    if not name:
        return jsonify({"message": "name is required."}), 400
    if not location_name:
        location_name = "MAG-1"

    existing = Product.query.filter_by(item_number=item_number).first()
    if existing:
        existing.name = name
        existing.location_name = location_name
        existing.unit = unit
        existing.notes = notes
        existing.qr_product = qr_product or existing.qr_product
        existing.qr_location = qr_location or existing.qr_location
        db.session.add(existing)
        db.session.commit()
        return jsonify({"message": "Updated", "id": existing.id}), 200

    p = Product(
        item_number=item_number,
        name=name,
        location_name=location_name,
        unit=unit,
        notes=notes,
        qr_product=qr_product,
        qr_location=qr_location,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"message": "Created", "id": p.id}), 201

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    amount_raw = data.get("amount")

    try:
        amount = int(amount_raw)
    except (TypeError, ValueError):
        return jsonify({"message": "Invalid amount."}), 400

    if amount <= 0:
        return jsonify({"message": "Amount must be greater than 0."}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({"message": "Product not found."}), 404

    if action == "receive":
        product.current_stock += amount
    elif action == "issue":
        if product.current_stock < amount:
            return jsonify({"message": "Not enough stock."}), 400
        product.current_stock -= amount
    else:
        return jsonify({"message": "Unknown action."}), 400

    db.session.add(
        AuditLog(
            product_id=product.id,
            action=action,
            amount=amount,
            username=current_user.username,
        )
    )
    db.session.commit()

    return jsonify({"message": "Operation completed."})

# ----------------------------
# Init DB (safe on Render and locally)
# ----------------------------
with app.app_context():
    db.create_all()

    # Create default admin user if missing
    if not User.query.filter_by(username="admin").first():
        db.session.add(
            User(
                username="admin",
                password=generate_password_hash("admin123"),
                full_name="Administrator",
            )
        )
        db.session.commit()
