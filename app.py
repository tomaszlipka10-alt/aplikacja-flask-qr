import os
import json
import base64
import datetime as dt
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import urllib.parse

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "bt-venlo-2026-key")

login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- Supabase Config ---
def _supabase_url(): return (os.getenv("SUPABASE_URL") or "").rstrip("/")
def _supabase_key(): return (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "")

def _supabase_request(method, table, params=None, json_body=None):
    base, key = _supabase_url(), _supabase_key()
    if not base or not key: return None
    url = f"{base}/rest/v1/{table}"
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=representation")
    data = json.dumps(json_body).encode("utf-8") if json_body else None
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            r = resp.read().decode("utf-8")
            return json.loads(r) if r else []
    except: return None

class User(UserMixin):
    def __init__(self, id, username, is_admin):
        self.id, self.username, self.is_admin = id, username, is_admin

@login_manager.user_loader
def load_user(uid):
    res = _supabase_request("GET", "users", {"id": f"eq.{uid}"})
    return User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False)) if res else None

# --- DODANA TRASA HEALTH CHECK DLA RENDER ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
@login_required
def index(): return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u, p = request.form.get("username"), request.form.get("password")
        res = _supabase_request("GET", "users", {"username": f"eq.{u}"})
        if res and check_password_hash(res[0]['password_hash'], p):
            login_user(User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False)))
            return redirect(url_for("index"))
return render_template("login.html")

@app.route("/register", methods=["POST"])
def register():
    u = request.form.get("username")
    p = request.form.get("password")
    if not u or not p:
        return render_template("login.html", error="Username and password required")

    # 1. Sprawdź czy użytkownik już istnieje
    existing = _supabase_request("GET", "users", {"username": f"eq.{u}"})
    if existing:
        return render_template("login.html", error="User already exists")

    # 2. Zahaszuj hasło i zapisz w Supabase
    phash = generate_password_hash(p)
    res = _supabase_request("POST", "users", json_body={
        "username": u,
        "password_hash": phash,
        "is_admin": False
    })
    
    if res is not None:
        return render_template("login.html", error="Account created! You can now login.", success=True)
    return render_template("login.html", error="Error creating user")

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

# --- API ---

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "POST":
        d = request.json
        _supabase_request("POST", "products", json_body={
            "item_number": d['item_number'], "name": d['name'], 
            "current_stock": int(d['current_stock']), "location": d['location'], "unit": "pcs"
        })
        _supabase_request("POST", "audit_logs", json_body={
            "item_number": d['item_number'], "name": d['name'], "action": "CREATE",
            "qty": int(d['current_stock']), "location": d['location'], "username": current_user.username
        })
        return jsonify({"ok": True})
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    return jsonify({"ok": True, "data": data})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    d = request.json
    sku, amt = d['item_number'], int(d['amount'])
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    if not res: return jsonify({"ok": False}), 404
    
    p = res[0]
    new_q = p['current_stock'] + amt if action == "receive" else p['current_stock'] - amt
    
    _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"current_stock": new_q})
    _supabase_request("POST", "audit_logs", json_body={
        "item_number": sku, "name": p['name'], "action": action.upper(), 
        "qty": amt, "location": p['location'], "username": current_user.username
    })
    return jsonify({"ok": True, "current_stock": new_q})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    return jsonify({"ok": True, "data": data})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))