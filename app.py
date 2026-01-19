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

import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Union, Any, List

# ----------------------------
# Supabase Helpers
# ----------------------------
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
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase HTTP {e.code}")

def _sb_row_to_api(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "item_number": row.get("sku"),
        "name": row.get("name"),
        "current_stock": row.get("quantity", 0),
        "min_stock": row.get("min_stock", 0),
        "location_name": row.get("location"),
        "qr_product": row.get("qr_product"),
        "qr_location": row.get("qr_location"),
        "created_at": row.get("created_at"),
    }

def _sb_list_products(limit: int = 500) -> List[dict]:
    rows = _supabase_request("GET", "products", params={"select": "id,sku,name,quantity,min_stock,location,qr_product,qr_location,created_at", "order": "created_at.desc", "limit": str(limit)}) or []
    return [_sb_row_to_api(r) for r in rows]

def _sb_upsert_product(payload: dict) -> dict:
    sku = payload.get("item_number", "").strip()
    name = payload.get("name", "").strip()
    location = payload.get("location_name", "").strip()
    qty = int(payload.get("current_stock") or 0)
    min_s = int(payload.get("min_stock") or 0)
    row = {"sku": sku, "name": name, "quantity": qty, "min_stock": min_s, "location": location or None}
    created = _supabase_request("POST", "products", params={"on_conflict": "sku"}, json_body=row, prefer="return=representation,resolution=merge-duplicates") or []
    return _sb_row_to_api(created[0]) if created else _sb_row_to_api(row)

def _sb_get_product_by_sku(item_number: str) -> Optional[dict]:
    rows = _supabase_request("GET", "products", params={"select": "id,sku,name,quantity,min_stock,location,qr_product,qr_location,created_at", "sku": f"eq.{item_number.strip()}", "limit": "1"}) or []
    return rows[0] if rows else None

def _sb_set_product_quantity(item_number: str, new_qty: int) -> dict:
    updated = _supabase_request("PATCH", "products", params={"sku": f"eq.{item_number.strip()}"}, json_body={"quantity": int(new_qty)}, prefer="return=representation") or []
    return updated[0] if updated else _sb_get_product_by_sku(item_number)

def _sb_insert_audit(action, item_number=None, name=None, qty=None, location=None, username=None):
    payload = {"action": action, "item_number": item_number, "name": name, "qty": qty, "location": location, "username": username}
    _supabase_request("POST", os.getenv("SUPABASE_AUDIT_TABLE", "audit_log"), json_body={k: v for k, v in payload.items() if v is not None})

def _sb_list_audit(limit: int = 200) -> list[dict]:
    rows = _supabase_request("GET", os.getenv("SUPABASE_AUDIT_TABLE", "audit_log"), params={"select": "id,action,item_number,name,qty,location,username,created_at", "order": "created_at.desc", "limit": str(limit)}) or []
    return [{"created_at": r.get("created_at"), "type": r.get("action"), "item_number": r.get("item_number"), "quantity": r.get("qty"), "location_name": r.get("location"), "username": r.get("username")} for r in rows]

# ------------------------------------------------------------
# App Config
# ------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "dev-secret"

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

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

def repair_database():
    with app.app_context():
        inspector = inspect(db.engine)
        u_cols = [c['name'] for c in inspector.get_columns('user')]
        if 'is_admin' not in u_cols:
            with db.engine.begin() as conn:
                conn.execute(sql_text('ALTER TABLE "user" ADD COLUMN is_admin BOOLEAN DEFAULT FALSE'))
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

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.route("/health")
def health(): return "OK", 200

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
        if _supabase_enabled():
            data = _sb_list_products()
            return jsonify({"ok": True, "products": data, "data": data})
        prods = Product.query.all()
        mapped = [{"item_number": p.item_number, "name": p.name, "current_stock": p.current_stock, "min_stock": p.min_stock, "location_name": p.location_name} for p in prods]
        return jsonify({"ok": True, "products": mapped, "data": mapped})

    data = request.get_json() or {}
    sku = str(data.get("item_number") or "").strip()
    name = str(data.get("name") or "").strip()
    min_s = int(data.get("min_stock") or 0)
    
    if not sku or not name: return jsonify({"ok": False, "error": "SKU and Name required"}), 400

    if _supabase_enabled():
        res = _sb_upsert_product(data)
        _sb_insert_audit("UPSERT", sku, name, data.get("current_stock"), data.get("location_name"), current_user.username)
        return jsonify({"ok": True, "product": res})

    p = Product.query.filter_by(item_number=sku).first()
    if p:
        p.name, p.current_stock, p.min_stock, p.location_name = name, int(data.get("current_stock", 0)), min_s, data.get("location_name", "MAG-1")
    else:
        p = Product(item_number=sku, name=name, current_stock=data.get("current_stock", 0), min_stock=min_s, location_name=data.get("location_name", "MAG-1"))
        db.session.add(p)
    db.session.commit()
    db.session.add(AuditLog(item_number=sku, action="UPSERT", qty=p.current_stock, location_name=p.location_name, username=current_user.username))
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json() or {}
    sku = data.get("item_number", "").strip()
    amt = int(data.get("amount") or 0)
    if amt <= 0: return jsonify({"ok": False, "error": "Invalid amount"}), 400

    if _supabase_enabled():
        row = _sb_get_product_by_sku(sku)
        if not row: return jsonify({"ok": False, "error": "Not found"}), 404
        cur = int(row.get("quantity") or 0)
        new_q = (cur + amt) if action == "receive" else (cur - amt)
        if new_q < 0: return jsonify({"ok": False, "error": "Insufficient stock"}), 400
        _sb_set_product_quantity(sku, new_q)
        _sb_insert_audit(action.upper(), sku, row.get("name"), amt, row.get("location"), current_user.username)
        return jsonify({"ok": True, "current_stock": new_q, "name": row.get("name"), "location": row.get("location")})

    p = Product.query.filter_by(item_number=sku).first()
    if not p: return jsonify({"ok": False, "error": "Not found"}), 404
    if action == "receive": p.current_stock += amt
    else:
        if p.current_stock < amt: return jsonify({"ok": False, "error": "Insufficient stock"}), 400
        p.current_stock -= amt
    db.session.add(AuditLog(item_number=sku, action=action.upper(), qty=amt, location_name=p.location_name, username=current_user.username))
    db.session.commit()
    return jsonify({"ok": True, "current_stock": p.current_stock, "name": p.name, "location": p.location_name})

@app.route("/api/audit")
@login_required
def api_list_audit():
    data = _sb_list_audit() if _supabase_enabled() else [
        {"created_at": l.created_at.isoformat(), "type": l.action, "item_number": l.item_number, "quantity": l.qty, "location_name": l.location_name, "username": l.username}
        for l in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()
    ]
    return jsonify({"data": data})

if __name__ == "__main__":
    app.run(debug=True)