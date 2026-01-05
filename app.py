import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError


def _env(name: str, default: str | None = None) -> str | None:
    """Small helper: Render sometimes has missing envs during early setup."""
    val = os.environ.get(name)
    return val if val not in (None, "") else default


# --- APP ---
app = Flask(__name__)

# SECRET_KEY: must exist in production; keep a safe fallback for local/dev
app.config["SECRET_KEY"] = _env("SECRET_KEY", "dev-secret-key-change-me")


# --- DATABASE ---
db_url = _env("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # Keep SQLite in instance/ so it works both locally and on platforms with writable instance dir.
    os.makedirs("instance", exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///instance/warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


# --- EXTENSIONS ---
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# --- MODELS ---
class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), nullable=True)


class Product(db.Model):
    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(100), default="MAG-1")


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=True)
    product_id = db.Column(db.Integer)
    action = db.Column(db.String(20))
    amount = db.Column(db.Integer)
    username = db.Column(db.String(80))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _safe_alter(statement: str) -> None:
    """Run ALTERs that may fail if already applied, without crashing the app."""
    try:
        db.session.execute(text(statement))
        db.session.commit()
    except OperationalError:
        db.session.rollback()


def ensure_schema() -> None:
    """Create tables and apply tiny 'best-effort' migrations.

    We intentionally avoid advanced migrations here (no Alembic) to keep Render deploys simple.
    For SQLite, we also avoid 'ALTER TABLE .. DEFAULT CURRENT_TIMESTAMP' because SQLite rejects
    non-constant defaults in ADD COLUMN.
    """
    insp = inspect(db.engine)

    # 1) Create tables if missing
    db.create_all()

    # 2) Best-effort column migrations for older SQLite files
    try:
        user_cols = {c["name"] for c in insp.get_columns("user")}
        if "password" not in user_cols:
            _safe_alter("ALTER TABLE user ADD COLUMN password VARCHAR(255)")
        if "full_name" not in user_cols:
            _safe_alter("ALTER TABLE user ADD COLUMN full_name VARCHAR(100)")
    except Exception:
        db.session.rollback()

    try:
        prod_cols = {c["name"] for c in insp.get_columns("product")}
        if "location_name" not in prod_cols:
            _safe_alter("ALTER TABLE product ADD COLUMN location_name VARCHAR(100)")
        if "current_stock" not in prod_cols:
            _safe_alter("ALTER TABLE product ADD COLUMN current_stock INTEGER")
    except Exception:
        db.session.rollback()

    try:
        audit_cols = {c["name"] for c in insp.get_columns("audit_log")}
        if "created_at" not in audit_cols:
            # IMPORTANT: no DEFAULT CURRENT_TIMESTAMP here (SQLite would error)
            _safe_alter("ALTER TABLE audit_log ADD COLUMN created_at DATETIME")
    except Exception:
        db.session.rollback()

    # 3) Ensure default admin exists (and has a password)
    try:
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(
                username="admin",
                password=generate_password_hash("admin123"),
                full_name="Administrator",
            )
            db.session.add(admin)
            db.session.commit()
        else:
            # If schema was old/migrated, password could be NULL/empty.
            if not getattr(admin, "password", None):
                admin.password = generate_password_hash("admin123")
                if not getattr(admin, "full_name", None):
                    admin.full_name = "Administrator"
                db.session.commit()
    except Exception:
        db.session.rollback()


with app.app_context():
    ensure_schema()


# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
@login_required
def index():
    return render_template("index.html", welcome_title="Warehouse Panel")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            return render_template("login.html", error="Enter username and password")

        try:
            user = User.query.filter_by(username=username).first()
        except Exception:
            # If DB is corrupted/mismatched, recreate schema (mostly for SQLite)
            db.session.rollback()
            ensure_schema()
            user = User.query.filter_by(username=username).first()

        if user and user.password and check_password_hash(user.password, password):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --- API ---
@app.route("/api/products", methods=["GET"])
@login_required
def api_products_list():
    products = Product.query.order_by(Product.id.asc()).all()
    return jsonify(
        {
            "data": [
                {
                    "id": p.id,
                    "item_number": p.item_number,
                    "name": p.name,
                    "current_stock": p.current_stock,
                    "location_name": p.location_name,
                }
                for p in products
            ]
        }
    )


@app.route("/api/products", methods=["POST"])
@login_required
def api_products_create_or_update():
    data = request.get_json(silent=True) or {}
    item_number = str(data.get("item_number", "")).strip()
    name = str(data.get("name", "")).strip()
    location_name = str(data.get("location_name", "MAG-1")).strip() or "MAG-1"

    if not item_number or not name:
        return jsonify({"message": "Missing item_number or name"}), 400

    product = Product.query.filter_by(item_number=item_number).first()
    if product:
        # Upsert behavior: update existing instead of 409
        product.name = name
        product.location_name = location_name
        db.session.commit()
        return jsonify({"message": "Updated", "id": product.id}), 200

    product = Product(item_number=item_number, name=name, location_name=location_name)
    db.session.add(product)
    db.session.commit()
    return jsonify({"message": "Created", "id": product.id}), 201


@app.route("/api/audit", methods=["GET"])
@login_required
def api_audit():
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    return jsonify(
        {
            "data": [
                {
                    "id": l.id,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                    "product_id": l.product_id,
                    "action": l.action,
                    "amount": l.amount,
                    "username": l.username,
                }
                for l in logs
            ]
        }
    )


@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action: str):
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    amount_raw = data.get("amount")

    try:
        amount = int(amount_raw)
    except Exception:
        return jsonify({"message": "Invalid amount"}), 400

    if not product_id:
        return jsonify({"message": "Missing product_id"}), 400

    product = db.session.get(Product, int(product_id))
    if not product:
        return jsonify({"message": "Product not found"}), 404

    if amount <= 0:
        return jsonify({"message": "Amount must be > 0"}), 400

    if action == "receive":
        product.current_stock = int(product.current_stock or 0) + amount
    elif action == "issue":
        if int(product.current_stock or 0) < amount:
            return jsonify({"message": "Not enough stock"}), 400
        product.current_stock = int(product.current_stock or 0) - amount
    else:
        return jsonify({"message": "Unknown action"}), 400

    db.session.add(
        AuditLog(
            created_at=datetime.utcnow(),
            product_id=product.id,
            action=action,
            amount=amount,
            username=current_user.username,
        )
    )
    db.session.commit()
    return jsonify({"message": "OK"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT", "5000")))
