import os
from datetime import datetime
from pathlib import Path

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
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------
# Flask app config
# --------------------
app = Flask(__name__)

# SECRET_KEY
# Render: możesz ustawić SECRET_KEY jako "Secret File" (Render doda wtedy zmienną env)
# albo jako env var. Poniżej dajemy bezpieczny fallback na local/dev.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "dev-secret-key-change-me"

# --------------------
# Database URL
# --------------------
# Render: jeśli masz Postgresa, ustaw DATABASE_URL.
# Jeśli nie – używamy SQLite.
# Na Render system plików jest efemeryczny, więc SQLite powinien iść do:
# - /var/data (jeśli używasz Render Disk)
# - w przeciwnym razie /tmp (działa, ale dane znikną po restarcie)

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    # SQLite
    if os.environ.get("RENDER"):
        base_dir = Path("/var/data") if Path("/var/data").exists() else Path("/tmp")
    else:
        base_dir = Path(__file__).resolve().parent

    base_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = base_dir / "warehouse.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --------------------
# Extensions
# --------------------
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# --------------------
# Models
# --------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")


class Product(db.Model):
    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(80), unique=True, nullable=False)  # ID/QR (EAN, ID produktu, itd.)
    name = db.Column(db.String(120), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(120), default="MAG-1")


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    action = db.Column(db.String(20), nullable=False)  # receive / issue / create / update
    product_id = db.Column(db.Integer, nullable=True)
    amount = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(80), nullable=True)
    note = db.Column(db.String(255), default="")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --------------------
# Minimal "migration" helper
# --------------------

def _is_sqlite() -> bool:
    return app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def ensure_schema() -> None:
    """Utrzymuje kompatybilność przy zmianach modeli bez Alembic.

    - tworzy tabele, jeśli nie istnieją
    - dodaje brakujące kolumny (SQLite: bez DEFAULT CURRENT_TIMESTAMP w ALTER TABLE)

    To rozwiązuje m.in. Twoje wcześniejsze błędy typu:
    - "no such column: user.password"
    - "Cannot add a column with non-constant default"
    """
    with app.app_context():
        db.create_all()

        insp = inspect(db.engine)

        def cols(table: str) -> set[str]:
            try:
                return {c["name"] for c in insp.get_columns(table)}
            except Exception:
                return set()

        # --- user table columns ---
        user_cols = cols("user")
        if "password" not in user_cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN password VARCHAR(255)"))
        if "full_name" not in user_cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN full_name VARCHAR(100)"))

        # --- product table columns (future-proof) ---
        product_cols = cols("product")
        if "location_name" not in product_cols and product_cols:
            db.session.execute(text("ALTER TABLE product ADD COLUMN location_name VARCHAR(120)"))
        if "current_stock" not in product_cols and product_cols:
            db.session.execute(text("ALTER TABLE product ADD COLUMN current_stock INTEGER"))

        # --- audit_log table columns ---
        audit_cols = cols("audit_log")
        if "created_at" not in audit_cols and audit_cols:
            # SQLite nie pozwala na DEFAULT CURRENT_TIMESTAMP przy ALTER TABLE.
            # Dodajemy kolumnę bez default, a potem wypełniamy wartości.
            db.session.execute(text("ALTER TABLE audit_log ADD COLUMN created_at DATETIME"))
            if _is_sqlite():
                db.session.execute(text("UPDATE audit_log SET created_at = datetime('now') WHERE created_at IS NULL"))
            else:
                db.session.execute(text("UPDATE audit_log SET created_at = NOW() WHERE created_at IS NULL"))

        if "note" not in audit_cols and audit_cols:
            db.session.execute(text("ALTER TABLE audit_log ADD COLUMN note VARCHAR(255)"))

        db.session.commit()

        # Ensure admin user exists and has password set
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", password=generate_password_hash("admin123"), full_name="Administrator")
            db.session.add(admin)
            db.session.commit()
        else:
            # if password was missing/empty after migration
            if not getattr(admin, "password", None):
                admin.password = generate_password_hash("admin123")
                db.session.commit()


ensure_schema()


# --------------------
# Routes
# --------------------
@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
@login_required
def index():
    return render_template("index.html", welcome_title="WMS")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

        return render_template("login.html", error="Błędny login lub hasło")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --------------------
# API
# --------------------
@app.route("/api/products", methods=["GET"])
@login_required
def api_products_get():
    products = Product.query.order_by(Product.id.desc()).all()
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
    location_name = str(data.get("location_name", "")).strip() or "MAG-1"

    if not item_number or not name:
        return jsonify({"message": "Brak wymaganych pól: item_number, name"}), 400

    product = Product.query.filter_by(item_number=item_number).first()

    if product:
        # UPSERT: aktualizacja zamiast 409
        product.name = name
        product.location_name = location_name
        db.session.commit()

        db.session.add(
            AuditLog(
                action="update",
                product_id=product.id,
                amount=None,
                username=current_user.username,
                note=f"Updated product {item_number}",
            )
        )
        db.session.commit()
        return jsonify({"message": "Produkt zaktualizowany", "id": product.id}), 200

    product = Product(item_number=item_number, name=name, location_name=location_name, current_stock=0)
    db.session.add(product)
    db.session.commit()

    db.session.add(
        AuditLog(
            action="create",
            product_id=product.id,
            amount=None,
            username=current_user.username,
            note=f"Created product {item_number}",
        )
    )
    db.session.commit()

    return jsonify({"message": "Produkt dodany", "id": product.id}), 201


@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action: str):
    if action not in {"receive", "issue"}:
        return jsonify({"message": "Nieznana operacja"}), 400

    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get("product_id"))
        amount = int(data.get("amount"))
    except Exception:
        return jsonify({"message": "Niepoprawne dane"}), 400

    if amount <= 0:
        return jsonify({"message": "Ilość musi być > 0"}), 400

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"message": "Produkt nie istnieje"}), 404

    if action == "receive":
        product.current_stock = (product.current_stock or 0) + amount
    else:
        if (product.current_stock or 0) < amount:
            return jsonify({"message": "Brak stanu"}), 400
        product.current_stock = (product.current_stock or 0) - amount

    db.session.add(
        AuditLog(
            action=action,
            product_id=product.id,
            amount=amount,
            username=current_user.username,
            note="",
        )
    )
    db.session.commit()

    return jsonify({"message": "Operacja wykonana"}), 200


@app.route("/api/audit", methods=["GET"])
@login_required
def api_audit():
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()

    def fmt_dt(dt):
        if not dt:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    return jsonify(
        {
            "data": [
                {
                    "id": l.id,
                    "created_at": fmt_dt(l.created_at),
                    "action": l.action,
                    "product_id": l.product_id,
                    "amount": l.amount,
                    "username": l.username,
                    "note": l.note or "",
                }
                for l in logs
            ]
        }
    )


# --------------------
# Local run
# --------------------
if __name__ == "__main__":
    # Render używa gunicorn, ale lokalnie to ułatwia start
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
