import os
import json
import base64
import datetime as dt
from pathlib import Path
from functools import wraps
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text as sql_text

# ----------------------------
# Supabase (Postgres) helpers
# ----------------------------
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Union, Any, List

def _supabase_key() -> str:
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    )

def _supabase_enabled() -> bool:
    return os.getenv("USE_SUPABASE", "0") == "1" and bool(os.getenv("SUPABASE_URL") and _supabase_key())

def _supabase_request(method: str, table: str, params: Optional[dict] = None, json_body: Optional[Union[dict, list]] = None, prefer: Optional[str] = None) -> Any:
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = _supabase_key()
    url = f"{base}/rest/v1/{table.lstrip('/')}"
    if params:
        query = urllib.parse.urlencode(params, doseq=True, safe=",:")
        url = f"{url}?{query}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    body = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode("utf-8")
        headers["Prefer"] = prefer or "return=representation"
    elif prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except Exception as e:
        print(f"Supabase error: {e}")
        return None

def _sb_row_to_api(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "item_number": row.get("sku"),
        "name": row.get("name"),
        "current_stock": row.get("quantity", 0),
        "location_name": row.get("location"),
        "created_at": row.get("created_at"),
    }

def _sb_list_products(limit: int = 500) -> List[dict]:
    rows = _supabase_request("GET", "products", params={"select": "id,sku,name,quantity,location,created_at", "order": "created_at.desc", "limit": str(limit)}) or []
    return [_sb_row_to_api(r) for r in rows]

def _sb_upsert_product(payload: dict) -> dict:
    row = {
        "sku": payload.get("item_number"),
        "name": payload.get("name"),
        "quantity": payload.get("current_stock", 0),
        "location": payload.get("location_name"),
    }
    created = _supabase_request("POST", "products", params={"on_conflict": "sku"}, json_body=row, prefer="return=representation,resolution=merge-duplicates") or []
    return _sb_row_to_api(created[0]) if created else _sb_row_to_api(row)

def _sb_get_product_by_sku(sku: str) -> Optional[dict]:
    rows = _supabase_request("GET", "products", params={"select": "*", "sku": f"eq.{sku}", "limit": "1"}) or []
    return rows[0] if rows else None

def _sb_set_product_quantity(sku: str, new_qty: int) -> dict:
    updated = _supabase_request("PATCH", "products", params={"sku": f"eq.{sku}"}, json_body={"quantity": new_qty}) or []
    return updated[0] if updated else {}

def _sb_insert_audit(action, item_number, name, qty, location, username):
    payload = {"action": action, "item_number": item_number, "name": name, "qty": qty, "location": location, "username": username}
    _supabase_request("POST", "audit_log", json_body={k: v for k, v in payload.items() if v is not None})

def _sb_list_audit(limit=100):
    rows = _supabase_request("GET", "audit_log", params={"select": "*", "order": "created_at.desc", "limit": str(limit)}) or []
    return [{"created_at": r.get("created_at"), "type": r.get("action"), "item_number": r.get("item_number"), "quantity": r.get("qty"), "username": r.get("username")} for r in rows]

# ----------------------------
# App Setup
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    sqlite_dir = Path("/var/data") if Path("/var/data").exists() else Path("/tmp")
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(sqlite_dir / 'warehouse.db').as_posix()}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------
# Models
# ----------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0, nullable=False)
    location_name = db.Column(db.String(100), default="MAG-1", nullable=False)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    username = db.Column(db.String(80), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            return jsonify({"message": "Admin only"}), 403
        return fn(*args, **kwargs)
    return wrapper

def ensure_schema():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username="admin").first():
            db.session.add(User(username="admin", password_hash=generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")), full_name="Administrator", is_admin=True))
            db.session.commit()

# ----------------------------
# Routes & API
# ----------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and check_password_hash(user.password_hash, request.form.get("password", "")):
            login_user(user)
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error="Invalid login")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "GET":
        if _supabase_enabled(): return jsonify({"ok": True, "products": _sb_list_products()})
        prods = Product.query.order_by(Product.item_number.asc()).all()
        mapped = [{"id":p.id,"item_number":p.item_number,"name":p.name,"current_stock":p.current_stock,"location_name":p.location_name} for p in prods]
        return jsonify({"ok": True, "products": mapped})

    data = request.get_json(force=True, silent=True) or {}
    sku = data.get("item_number", "").strip()
    name = data.get("name", "").strip()
    if not sku or not name: return jsonify({"ok":False,"error":"Missing SKU or Name"}), 400

    if _supabase_enabled():
        res = _sb_upsert_product(data)
        _sb_insert_audit("CREATE", sku, name, data.get("current_stock", 0), data.get("location_name"), current_user.username)
        return jsonify({"ok": True, "product": res})

    p = Product.query.filter_by(item_number=sku).first()
    if p:
        p.name, p.location_name, p.current_stock = name, data.get("location_name", "MAG-1"), data.get("current_stock", 0)
    else:
        p = Product(item_number=sku, name=name, location_name=data.get("location_name", "MAG-1"), current_stock=data.get("current_stock", 0))
        db.session.add(p)
    db.session.commit()
    db.session.add(AuditLog(product_id=p.id, action="upsert", amount=p.current_stock, username=current_user.username))
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json(force=True, silent=True) or {}
    sku = data.get("item_number", "").strip()
    amount = int(data.get("amount", 0))
    if amount <= 0: return jsonify({"ok": False, "error": "Invalid amount"}), 400

    if _supabase_enabled():
        row = _sb_get_product_by_sku(sku)
        if not row: return jsonify({"ok":False,"error":"Not found"}), 404
        new_q = (row['quantity'] + amount) if action == "receive" else (row['quantity'] - amount)
        if new_q < 0: return jsonify({"ok":False,"error":"Low stock"}), 400
        _sb_set_product_quantity(sku, new_q)
        _sb_insert_audit(action.upper(), sku, row['name'], amount, row['location'], current_user.username)
        return jsonify({"ok":True, "current_stock": new_q})

    p = Product.query.filter_by(item_number=sku).first()
    if not p: return jsonify({"ok":False,"error":"Not found"}), 404
    if action == "receive": p.current_stock += amount
    else:
        if p.current_stock < amount: return jsonify({"ok":False,"error":"Low stock"}), 400
        p.current_stock -= amount
    db.session.add(AuditLog(product_id=p.id, action=action, amount=amount, username=current_user.username))
    db.session.commit()
    return jsonify({"ok":True, "current_stock": p.current_stock})

@app.route("/api/audit")
@login_required
def api_audit():
    if _supabase_enabled(): return jsonify({"ok": True, "data": _sb_list_audit()})
    logs = db.session.query(AuditLog, Product).outerjoin(Product, AuditLog.product_id == Product.id).order_by(AuditLog.created_at.desc()).limit(100).all()
    out = [{"created_at": l.AuditLog.created_at.isoformat(), "type": l.AuditLog.action, "item_number": l.Product.item_number if l.Product else "N/A", "quantity": l.AuditLog.amount, "username": l.AuditLog.username} for l in logs]
    return jsonify({"ok": True, "data": out})

@app.route("/health")
def health(): return "OK", 200

if __name__ == "__main__":
    ensure_schema()
    app.run(debug=True)
else:
    ensure_schema()