import os
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash, check_password_hash

# ----------------------------
# App
# ----------------------------
app = Flask(__name__)

# On Render: set SECRET_KEY in Environment Variables.
# Locally: fallback prevents crash.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# ----------------------------
# Database (PostgreSQL on Render / SQLite locally)
# ----------------------------
db_url = os.environ.get("DATABASE_URL")
if db_url:
    # Render sometimes uses deprecated prefix
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # Local dev fallback
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ----------------------------
# Auth
# ----------------------------
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------
# Models
# ----------------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=True)

class Location(db.Model):
    __tablename__ = "location"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

class Product(db.Model):
    __tablename__ = "product"
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(80), unique=True, nullable=False)  # SKU / product ID in QR
    name = db.Column(db.String(200), nullable=False)
    current_stock = db.Column(db.Integer, default=0, nullable=False)

    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False)
    location = db.relationship("Location", backref=db.backref("products", lazy=True))

class Pallet(db.Model):
    __tablename__ = "pallet"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(120), unique=True, nullable=False)  # PALLET ID in QR
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False)
    location = db.relationship("Location", backref=db.backref("pallets", lazy=True))

class Container(db.Model):
    __tablename__ = "container"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(120), unique=True, nullable=False)  # CONTAINER ID in QR
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False)
    location = db.relationship("Location", backref=db.backref("containers", lazy=True))

class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp_utc = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    action = db.Column(db.String(20), nullable=False)  # receive/issue/add_product/scan
    amount = db.Column(db.Integer, nullable=True)

    username = db.Column(db.String(80), nullable=True)
    product_id = db.Column(db.Integer, nullable=True)
    product_item_number = db.Column(db.String(80), nullable=True)

    location_id = db.Column(db.Integer, nullable=True)
    location_name = db.Column(db.String(120), nullable=True)

    ref_code = db.Column(db.String(200), nullable=True)  # the scanned code if applicable
    details = db.Column(db.String(500), nullable=True)

@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))

# ----------------------------
# Helpers
# ----------------------------
def _is_sqlite() -> bool:
    return app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")

def _ensure_schema_sqlite_reset_if_mismatch():
    """
    This project started with different schemas. On SQLite (local / single-file),
    it's safe to reset automatically if schema mismatches are detected.
    On Postgres, DO NOT reset automatically.
    """
    if not _is_sqlite():
        return

    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    # If no tables, nothing to check
    if not tables:
        return

    # Old schema used "user" with "password" column; new uses "password_hash"
    if "user" in tables:
        cols = {c["name"] for c in insp.get_columns("user")}
        if "password" in cols and "password_hash" not in cols:
            db.drop_all()
            db.create_all()

def _seed_defaults():
    # Create default admin user if missing
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
    admin_full = os.environ.get("ADMIN_FULL_NAME", "Administrator")

    if not User.query.filter_by(username=admin_user).first():
        db.session.add(
            User(
                username=admin_user,
                password_hash=generate_password_hash(admin_pass),
                full_name=admin_full,
            )
        )
        db.session.commit()

    # Ensure at least one location exists
    if Location.query.count() == 0:
        db.session.add(Location(name="MAG-1"))
        db.session.commit()

def _parse_qr_code(raw: str):
    """
    QR formats:
      - P:<item_number>   (product by item_number)
      - P:<id>            (product by numeric id)
      - L:<location>      (location by name or numeric id)
      - PLT:<code>        (pallet by code or numeric id)
      - CNT:<code>        (container by code or numeric id)

    Also accepts raw strings without prefix as Product item_number.
    """
    code = (raw or "").strip()
    if not code:
        return {"ok": False, "message": "Empty code."}

    # Default: treat as Product
    prefix = None
    payload = code

    if ":" in code:
        maybe_prefix, rest = code.split(":", 1)
        maybe_prefix = maybe_prefix.strip().upper()
        rest = rest.strip()
        if maybe_prefix in {"P", "L", "PLT", "CNT"} and rest:
            prefix, payload = maybe_prefix, rest

    def _as_int(s: str):
        try:
            return int(s)
        except Exception:
            return None

    if prefix in (None, "P"):
        pid = _as_int(payload)
        if pid is not None:
            p = Product.query.get(pid)
        else:
            p = Product.query.filter_by(item_number=payload).first()
        if not p:
            return {"ok": False, "message": "Product not found.", "type": "product"}
        return {
            "ok": True,
            "type": "product",
            "product": {
                "id": p.id,
                "item_number": p.item_number,
                "name": p.name,
                "current_stock": p.current_stock,
                "location": {"id": p.location.id, "name": p.location.name},
            },
        }

    if prefix == "L":
        lid = _as_int(payload)
        if lid is not None:
            loc = Location.query.get(lid)
        else:
            loc = Location.query.filter_by(name=payload).first()
        if not loc:
            return {"ok": False, "message": "Location not found.", "type": "location"}
        products = Product.query.filter_by(location_id=loc.id).order_by(Product.item_number.asc()).all()
        return {
            "ok": True,
            "type": "location",
            "location": {"id": loc.id, "name": loc.name},
            "products": [
                {"id": p.id, "item_number": p.item_number, "name": p.name, "current_stock": p.current_stock}
                for p in products
            ],
        }

    if prefix == "PLT":
        pid = _as_int(payload)
        if pid is not None:
            plt = Pallet.query.get(pid)
        else:
            plt = Pallet.query.filter_by(code=payload).first()
        if not plt:
            return {"ok": False, "message": "Pallet not found.", "type": "pallet"}
        loc = plt.location
        products = Product.query.filter_by(location_id=loc.id).order_by(Product.item_number.asc()).all()
        return {
            "ok": True,
            "type": "pallet",
            "pallet": {"id": plt.id, "code": plt.code},
            "location": {"id": loc.id, "name": loc.name},
            "products": [
                {"id": p.id, "item_number": p.item_number, "name": p.name, "current_stock": p.current_stock}
                for p in products
            ],
        }

    if prefix == "CNT":
        cid = _as_int(payload)
        if cid is not None:
            c = Container.query.get(cid)
        else:
            c = Container.query.filter_by(code=payload).first()
        if not c:
            return {"ok": False, "message": "Container not found.", "type": "container"}
        loc = c.location
        products = Product.query.filter_by(location_id=loc.id).order_by(Product.item_number.asc()).all()
        return {
            "ok": True,
            "type": "container",
            "container": {"id": c.id, "code": c.code},
            "location": {"id": loc.id, "name": loc.name},
            "products": [
                {"id": p.id, "item_number": p.item_number, "name": p.name, "current_stock": p.current_stock}
                for p in products
            ],
        }

    return {"ok": False, "message": "Unsupported code format."}

# ----------------------------
# Startup init (safe on Render)
# ----------------------------
with app.app_context():
    db.create_all()
    try:
        _ensure_schema_sqlite_reset_if_mismatch()
    except Exception:
        # Never crash the app on startup due to schema checks.
        pass
    _seed_defaults()

# ----------------------------
# Routes
# ----------------------------
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
@login_required
def index():
    return render_template("index.html", user_full_name=(current_user.full_name or current_user.username))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html", error=None)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ----------------------------
# API
# ----------------------------
@app.route("/api/locations")
@login_required
def api_locations():
    locs = Location.query.order_by(Location.name.asc()).all()
    return jsonify({"data": [{"id": l.id, "name": l.name} for l in locs]})

@app.route("/api/products")
@login_required
def api_products():
    products = (
        Product.query.join(Location, Product.location_id == Location.id)
        .order_by(Product.item_number.asc())
        .all()
    )
    return jsonify(
        {
            "data": [
                {
                    "id": p.id,
                    "item_number": p.item_number,
                    "name": p.name,
                    "current_stock": p.current_stock,
                    "location": {"id": p.location.id, "name": p.location.name},
                }
                for p in products
            ]
        }
    )

@app.route("/api/audit")
@login_required
def api_audit():
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    return jsonify(
        {
            "data": [
                {
                    "id": l.id,
                    "timestamp_utc": l.timestamp_utc.isoformat().replace("+00:00", "Z") if l.timestamp_utc else None,
                    "action": l.action,
                    "amount": l.amount,
                    "username": l.username,
                    "product_item_number": l.product_item_number,
                    "location_name": l.location_name,
                    "ref_code": l.ref_code,
                    "details": l.details,
                }
                for l in logs
            ]
        }
    )

@app.route("/api/product/add", methods=["POST"])
@login_required
def api_add_product():
    data = request.get_json(force=True, silent=True) or {}
    item_number = (data.get("item_number") or "").strip()
    name = (data.get("name") or "").strip()
    location_id = data.get("location_id")
    initial_stock = data.get("initial_stock", 0)

    if not item_number or not name or not location_id:
        return jsonify({"message": "Missing required fields."}), 400

    try:
        initial_stock = int(initial_stock)
    except Exception:
        return jsonify({"message": "Initial stock must be a number."}), 400

    if initial_stock < 0:
        return jsonify({"message": "Initial stock cannot be negative."}), 400

    if Product.query.filter_by(item_number=item_number).first():
        return jsonify({"message": "Item number already exists."}), 400

    loc = Location.query.get(int(location_id))
    if not loc:
        return jsonify({"message": "Location not found."}), 400

    p = Product(item_number=item_number, name=name, current_stock=initial_stock, location_id=loc.id)
    db.session.add(p)
    db.session.add(
        AuditLog(
            action="add_product",
            amount=initial_stock,
            username=current_user.username,
            product_id=None,
            product_item_number=item_number,
            location_id=loc.id,
            location_name=loc.name,
            details=f"Created product '{name}'",
        )
    )
    db.session.commit()

    return jsonify({"message": "Product added.", "id": p.id})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action: str):
    action = (action or "").lower().strip()
    if action not in {"receive", "issue"}:
        return jsonify({"message": "Unknown operation."}), 400

    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    amount = data.get("amount")

    try:
        product_id = int(product_id)
        amount = int(amount)
    except Exception:
        return jsonify({"message": "Invalid product_id or amount."}), 400

    if amount <= 0:
        return jsonify({"message": "Amount must be > 0."}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({"message": "Product not found."}), 404

    if action == "issue" and product.current_stock < amount:
        return jsonify({"message": "Not enough stock."}), 400

    if action == "receive":
        product.current_stock += amount
        details = f"Received {amount}"
    else:
        product.current_stock -= amount
        details = f"Issued {amount}"

    db.session.add(
        AuditLog(
            action=action,
            amount=amount,
            username=current_user.username,
            product_id=product.id,
            product_item_number=product.item_number,
            location_id=product.location.id,
            location_name=product.location.name,
            details=details,
        )
    )
    db.session.commit()

    return jsonify({"message": "Operation completed.", "new_stock": product.current_stock})

@app.route("/api/scan", methods=["POST"])
@login_required
def api_scan():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    result = _parse_qr_code(code)

    db.session.add(
        AuditLog(
            action="scan",
            amount=None,
            username=current_user.username,
            ref_code=code,
            details=("OK" if result.get("ok") else (result.get("message") or "Scan failed")),
        )
    )
    db.session.commit()

    status = 200 if result.get("ok") else 404
    return jsonify(result), status
