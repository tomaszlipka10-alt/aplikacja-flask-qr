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
    if not base or not key: return None
    url = f"{base}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    
    body_data = None
    if json_body is not None:
        body_data = json.dumps(json_body).encode("utf-8")

    try:
        with urllib.request.urlopen(req, data=body_data) as response:
            if response.status in [200, 201]:
                return json.loads(response.read().decode("utf-8"))
            elif response.status == 204:
                return []
            return None
    except Exception:
        return None

def _sb_list_products():
    return _supabase_request("GET", "products", params={"select": "*", "order": "item_number"}) or []

def _sb_get_product(sku: str):
    res = _supabase_request("GET", "products", params={"item_number": f"eq.{sku}", "select": "*"})
    return res[0] if res and len(res) > 0 else None

def _sb_set_product_quantity(sku: str, new_qty: int):
    return _supabase_request("PATCH", "products", params={"item_number": f"eq.{sku}"}, json_body={"current_stock": new_qty})

def _sb_insert_audit(action: str, sku: str, name: str, amount: int, loc: str, user: str):
    body = {
        "product_sku": sku,
        "product_name": name,
        "action": action,
        "amount": amount,
        "location": loc,
        "username": user
    }
    return _supabase_request("POST", "audit_logs", json_body=body)

def _sb_list_audit():
    return _supabase_request("GET", "audit_logs", params={"select": "*", "order": "created_at.desc", "limit": "100"}) or []

# ----------------------------
# Flask App & Database
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-123")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(100))
    current_stock = db.Column(db.Integer, default=0)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    action = db.Column(db.String(50))
    amount = db.Column(db.Integer)
    username = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

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
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username")).first()
        if u and check_password_hash(u.password_hash, request.form.get("password")):
            login_user(u)
            return redirect(url_for("index"))
        return render_template("login.html", error="Nieprawidłowy login lub hasło")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if User.query.filter_by(username=username).first():
            return render_template("login.html", error="Użytkownik już istnieje", register_mode=True)
        new_user = User(
            username=username,
            password_hash=generate_password_hash(password),
            is_admin=False
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("login.html", register_mode=True)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/api/products")
@login_required
def api_products():
    if _supabase_enabled():
        return jsonify({"ok": True, "data": _sb_list_products()})
    prods = Product.query.order_by(Product.item_number).all()
    data = []
    for p in prods:
        data.append({
            "item_number": p.item_number,
            "name": p.name,
            "location": p.location,
            "current_stock": p.current_stock
        })
    return jsonify({"ok": True, "data": data})

@app.route("/api/stock", methods=["POST"])
@login_required
def api_stock():
    data = request.json or {}
    sku = data.get("item_number")
    action = data.get("action") # "receive" or "release"
    amount = int(data.get("amount", 0))
    if amount <= 0: return jsonify({"ok":False,"error":"Invalid amount"}), 400

    if _supabase_enabled():
        row = _sb_get_product(sku)
        if not row: return jsonify({"ok":False,"error":"Not found"}), 404
        new_q = row['current_stock'] + amount if action == "receive" else row['current_stock'] - amount
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
    out = []
    for l in logs:
        out.append({
            "created_at": l.AuditLog.created_at.isoformat(),
            "type": l.AuditLog.action,
            "item_number": l.Product.item_number if l.Product else "Unknown",
            "name": l.Product.name if l.Product else "Unknown",
            "amount": l.AuditLog.amount,
            "username": l.AuditLog.username
        })
    return jsonify({"ok": True, "data": out})

# ----------------------------
# GitHub Backup Logic
# ----------------------------
def github_put_file(repo, path, token, content_bytes, message):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            curr = json.loads(r.read().decode("utf-8"))
            sha = curr.get("sha")
    except: pass
    
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8")
    }
    if sha: payload["sha"] = sha
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")
    with urllib.request.urlopen(req) as r:
        return r.status

def export_warehouse_json():
    prods = Product.query.all()
    logs = AuditLog.query.all()
    return {
        "exported_at_utc": dt.datetime.utcnow().isoformat(),
        "products": [{"sku":p.item_number,"name":p.name,"loc":p.location,"stock":p.current_stock} for p in prods],
        "audit": [{"ts":a.created_at.isoformat(),"act":a.action,"amt":a.amount,"user":a.username} for a in logs]
    }

@app.route("/api/admin/backup/github", methods=["POST"])
@login_required
def api_backup_github():
    if not current_user.is_admin: return jsonify({"message":"Forbidden"}), 403
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    backup_path = "backups/warehouse_data.json"
    if not token or not repo: return jsonify({"message": "Missing Config"}), 400
    try:
        payload = export_warehouse_json()
        content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        github_put_file(repo, backup_path, token, content, f"Backup {payload['exported_at_utc']}")
        return jsonify({"message": "Backup OK"})
    except Exception as e:
        return jsonify({"message": str(e)}), 500

# ----------------------------
# Database Init
# ----------------------------
with app.app_context():
    # WYMUSZONY RESET (usuwa starą bazę i tworzy nową z kolumną is_admin)
    db.drop_all() 
    db.create_all()
    
    admins = [
        {"u": "TomaszLipka", "p": "Welkom01"},
        {"u": "JulesvdHam", "p": "Welkom01"},
        {"u": "TwanvanHeeswijk", "p": "Welkom01"}
    ]
    
    for a in admins:
        db.session.add(User(
            username=a["u"],
            password_hash=generate_password_hash(a["p"]),
            is_admin=True
        ))
    db.session.commit()

if __name__ == "__main__":
    app.run(debug=True)