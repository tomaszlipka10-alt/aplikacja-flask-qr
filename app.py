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
    url = f"{base}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    
    with urllib.request.urlopen(req) as response:
        if response.status in [201, 204]:
            return {}
        return json.loads(response.read().decode("utf-8"))

def _sb_list_products():
    return _supabase_request("GET", "products", params={"select": "*", "order": "item_number"})

def _sb_upsert_product(data: dict):
    return _supabase_request("POST", "products", json_body=data, prefer="resolution=merge-duplicates")

def _sb_get_product(sku: str):
    res = _supabase_request("GET", "products", params={"item_number": f"eq.{sku}", "select": "*"})
    return res[0] if res else None

def _sb_set_product_quantity(sku: str, new_qty: int):
    return _supabase_request("PATCH", "products", params={"item_number": f"eq.{sku}"}, json_body={"current_stock": new_qty})

def _sb_list_audit():
    return _supabase_request("GET", "audit_log", params={"select": "*", "order": "created_at.desc", "limit": "100"})

def _sb_insert_audit(action: str, sku: str, name: str, qty: int, loc: str, user: str):
    body = {
        "action": action,
        "item_number": sku,
        "name": name,
        "qty": qty,
        "location": loc,
        "username": user
    }
    return _supabase_request("POST", "audit_log", json_body=body)

# ----------------------------
# GitHub Helpers
# ----------------------------
def github_put_file(repo: str, path: str, token: str, content_bytes: bytes, message: str, branch: str = "main"):
    base_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    sha = None
    try:
        req_get = Request(base_url)
        req_get.add_header("Authorization", f"token {token}")
        with urlopen(req_get) as r:
            curr = json.loads(r.read().decode("utf-8"))
            sha = curr.get("sha")
    except Exception:
        pass

    data = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch
    }
    if sha:
        data["sha"] = sha

    req_put = Request(base_url, data=json.dumps(data).encode("utf-8"), method="PUT")
    req_put.add_header("Authorization", f"token {token}")
    req_put.add_header("Content-Type", "application/json")
    with urlopen(req_put) as r:
        return json.loads(r.read().decode("utf-8"))

# ----------------------------
# Flask App Setup
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-123")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------
# Models
# ----------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(200))
    current_stock = db.Column(db.Integer, default=0)
    location_name = db.Column(db.String(100))

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    action = db.Column(db.String(50))
    amount = db.Column(db.Integer)
    username = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function

# ----------------------------
# Routes
# ----------------------------
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
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

# API
@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "POST":
        data = request.json
        sku = data.get("item_number")
        if _supabase_enabled():
            _sb_upsert_product(data)
            _sb_insert_audit("CREATE/UPDATE", sku, data.get("name",""), data.get("current_stock",0), data.get("location_name",""), current_user.username)
            return jsonify({"ok": True})
        
        p = Product.query.filter_by(item_number=sku).first()
        if p:
            p.name = data.get("name", p.name)
            p.current_stock = data.get("current_stock", p.current_stock)
            p.location_name = data.get("location_name", p.location_name)
        else:
            p = Product(item_number=sku, name=data.get("name"), current_stock=data.get("current_stock"), location_name=data.get("location_name"))
            db.session.add(p)
        db.session.commit()
        return jsonify({"ok": True})

    if _supabase_enabled():
        return jsonify({"products": _sb_list_products()})
    prods = Product.query.all()
    return jsonify({"products": [{"item_number": p.item_number, "name": p.name, "current_stock": p.current_stock, "location_name": p.location_name} for p in prods]})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.json
    sku = data.get("item_number")
    amount = int(data.get("amount", 0))

    if _supabase_enabled():
        row = _sb_get_product(sku)
        if not row: return jsonify({"ok":False, "error":"Product not found in Supabase"}), 404
        curr_q = int(row.get('current_stock') or 0)
        if action == "receive": new_q = curr_q + amount
        else:
            new_q = curr_q - amount
            if new_q < 0: return jsonify({"ok":False,"error":"Insufficient stock"}), 400
        _sb_set_product_quantity(sku, new_q)
        _sb_insert_audit(action.upper(), sku, row['name'], amount, row['location_name'], current_user.username)
        return jsonify({"ok":True, "current_stock": new_q})

    p = Product.query.filter_by(item_number=sku).first()
    if not p: return jsonify({"ok":False,"error":"Product not found"}), 404
    if action == "receive": p.current_stock += amount
    else:
        if p.current_stock < amount: return jsonify({"ok":False,"error":"Insufficient stock"}), 400
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
            "action": l.AuditLog.action,
            "item_number": l.Product.item_number if l.Product else "N/A",
            "name": l.Product.name if l.Product else "N/A",
            "qty": l.AuditLog.amount,
            "location": l.Product.location_name if l.Product else "N/A",
            "username": l.AuditLog.username
        })
    return jsonify({"audit": out})

def export_warehouse_json():
    if _supabase_enabled():
        return {"products": _sb_list_products(), "audit": _sb_list_audit(), "exported_at_utc": dt.datetime.utcnow().isoformat()}
    prods = Product.query.all()
    logs = AuditLog.query.all()
    return {
        "products": [{"item_number": p.item_number, "name": p.name, "current_stock": p.current_stock, "location": p.location_name} for p in prods],
        "audit": [{"action": l.action, "amount": l.amount, "user": l.username, "time": l.created_at.isoformat()} for l in logs],
        "exported_at_utc": dt.datetime.utcnow().isoformat()
    }

@app.route("/api/admin/backup/github", methods=["POST"])
@login_required
@admin_required
def api_admin_backup_github():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    backup_path = os.environ.get("GITHUB_BACKUP_PATH", "backups/warehouse-backup.json").strip()
    if not token or not repo:
        return jsonify({"message": "Missing GITHUB_TOKEN or GITHUB_REPO"}), 400

    payload = export_warehouse_json()
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    msg = f"Backup warehouse ({payload['exported_at_utc']})"
    try:
        github_put_file(repo=repo, path=backup_path, token=token, content_bytes=content, message=msg)
        return jsonify({"message": "Backup to GitHub successful"})
    except Exception as e:
        return jsonify({"message": f"Backup failed: {str(e)}"}), 500

# ----------------------------
# Database Init
# ----------------------------
# ... (reszta kodu bez zmian aż do sekcji Database Init) ...

# ----------------------------
# Database Init
# ----------------------------
with app.app_context():
    # UWAGA: Ta linia usunie starą bazę z błędem i stworzy nową poprawną.
    # Po jednym poprawnym uruchomieniu możesz ją usunąć lub zakomentować.
    db.drop_all() 
    
    db.create_all()
    
    # Lista użytkowników do utworzenia
    users_to_create = [
        {"username": "TomaszLipka", "password": "Welkom01", "is_admin": True},
        {"username": "JulesvdHam", "password": "Welkom01", "is_admin": True},
        {"username": "CristianJipa", "password": "Welkom01", "is_admin": False},
    ]
    
    for u_data in users_to_create:
        existing = User.query.filter_by(username=u_data["username"]).first()
        if not existing:
            new_user = User(
                username=u_data["username"],
                password_hash=generate_password_hash(u_data["password"]),
                is_admin=u_data["is_admin"]
            )
            db.session.add(new_user)
    
    db.session.commit()

if __name__ == "__main__":
    app.run(debug=True)