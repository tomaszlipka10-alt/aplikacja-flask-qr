import os

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import Babel

# -------------------------
# App
# -------------------------
app = Flask(__name__)

# Render env var is set by you in Render dashboard; SECRET_KEY too.
# Fallback keeps local dev from crashing if you forgot .env.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# -------------------------
# Database (PostgreSQL on Render / SQLite local)
# -------------------------
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    # Render sometimes provides postgres:// which SQLAlchemy doesn't accept
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# -------------------------
# Extensions
# -------------------------
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

babel = Babel(app)

# Language kept minimal for now (EN-only UI). We still keep session['lang'] so you can re-enable later.
app.config["BABEL_DEFAULT_LOCALE"] = "en"


@babel.localeselector
def get_locale():
    return session.get("lang", "en")


# -------------------------
# Models
# -------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)  # this is your QR payload for product
    name = db.Column(db.String(120), nullable=False)
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(120), default="MAG-1")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)  # receive/issue/create
    amount = db.Column(db.Integer, default=0)
    username = db.Column(db.String(80), default="")
    created_at = db.Column(db.DateTime, server_default=db.func.now())


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------
# Health
# -------------------------
@app.route("/health")
def health():
    return "OK", 200


# -------------------------
# Pages
# -------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html")


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


@app.route("/set_language/<lang>")
def set_language(lang):
    # kept for future; UI currently not using it
    session["lang"] = lang
    return redirect(request.referrer or url_for("index"))


# -------------------------
# API
# -------------------------
@app.route("/api/products")
@login_required
def api_products():
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


@app.route("/api/products/add", methods=["POST"])
@login_required
def api_products_add():
    data = request.get_json(silent=True) or {}
    item_number = (data.get("item_number") or "").strip()
    name = (data.get("name") or "").strip()
    location_name = (data.get("location_name") or "MAG-1").strip() or "MAG-1"

    try:
        initial_stock = int(data.get("initial_stock") or 0)
    except (TypeError, ValueError):
        initial_stock = 0

    if not item_number or not name:
        return jsonify({"message": "Item number and name are required"}), 400

    exists = Product.query.filter_by(item_number=item_number).first()
    if exists:
        return jsonify({"message": "Product with this item number already exists"}), 409

    p = Product(item_number=item_number, name=name, current_stock=max(0, initial_stock), location_name=location_name)
    db.session.add(p)
    db.session.flush()  # get p.id

    db.session.add(
        AuditLog(
            product_id=p.id,
            action="create",
            amount=p.current_stock,
            username=getattr(current_user, "username", ""),
        )
    )
    db.session.commit()

    return jsonify({"message": "Product created", "product_id": p.id})


@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json(silent=True) or {}

    try:
        product_id = int(data.get("product_id"))
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"message": "Invalid payload"}), 400

    if amount <= 0:
        return jsonify({"message": "Amount must be > 0"}), 400

    product = Product.query.get(product_id)
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

    db.session.add(
        AuditLog(
            product_id=product.id,
            action=action,
            amount=amount,
            username=getattr(current_user, "username", ""),
        )
    )
    db.session.commit()

    return jsonify({"message": "Operation completed", "current_stock": product.current_stock})


@app.route("/api/audit")
@login_required
def api_audit():
    logs = (
        db.session.query(AuditLog, Product)
        .join(Product, Product.id == AuditLog.product_id)
        .order_by(AuditLog.id.desc())
        .limit(200)
        .all()
    )

    return jsonify(
        {
            "data": [
                {
                    "id": log.id,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                    "item_number": prod.item_number,
                    "product_name": prod.name,
                    "action": log.action,
                    "amount": log.amount,
                    "username": log.username,
                }
                for (log, prod) in logs
            ]
        }
    )


@app.route("/api/auth/qr_login", methods=["POST"])
def qr_login():
    return jsonify({"success": False, "message": "QR login disabled"})


# -------------------------
# DB init (simple, without migrations)
# -------------------------
with app.app_context():
    db.create_all()

    # Create default admin if not exists
    if not User.query.filter_by(username="admin").first():
        db.session.add(
            User(
                username="admin",
                password=generate_password_hash("admin123"),
                full_name="Administrator",
            )
        )
        db.session.commit()
