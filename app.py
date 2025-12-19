import os
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

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
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

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

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer)
    action = db.Column(db.String(20))  # receive | issue
    amount = db.Column(db.Integer)
    username = db.Column(db.String(80))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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
@app.route("/api/products")
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
                }
                for p in products
            ]
        }
    )

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
