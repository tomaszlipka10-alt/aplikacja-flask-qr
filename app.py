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

# --- Supabase Config & Request Helper ---
def _supabase_url():
    return (os.getenv("SUPABASE_URL") or "").rstrip("/")

def _supabase_key():
    return (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "")

def _supabase_request(method, table, params=None, json_body=None):
    base, key = _supabase_url(), _supabase_key()
    if not base or not key:
        return None
    
    url = f"{base}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=representation")
    
    data = json.dumps(json_body).encode("utf-8") if json_body else None
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            res_data = resp.read().decode("utf-8")
            return json.loads(res_data) if res_data else []
    except Exception as e:
        print(f"Supabase Error: {e}")
        return None

# --- User Model for Flask-Login ---
class User(UserMixin):
    def __init__(self, id, username, is_admin):
        self.id = id
        self.username = username
        self.is_admin = is_admin

@login_manager.user_loader
def load_user(user_id):
    res = _supabase_request("GET", "users", {"id": f"eq.{user_id}"})
    if res:
        u = res[0]
        return User(u['id'], u['username'], u.get('is_admin', False))
    return None

# --- Routes ---
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u_name = request.form.get("username")
        p_word = request.form.get("password")
        
        res = _supabase_request("GET", "users", {"username": f"eq.{u_name}"})
        if res and check_password_hash(res[0]['password_hash'], p_word):
            user_obj = User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False))
            login_user(user_obj)
            return redirect(url_for("index"))
        return render_template("login.html", error="Błędny login lub hasło")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u_name = request.form.get("username")
        p_word = request.form.get("password")
        hashed = generate_password_hash(p_word)
        
        payload = {"username": u_name, "password_hash": hashed, "is_admin": False}
        res = _supabase_request("POST", "users", json_body=payload)
        if res:
            return redirect(url_for("login"))
    return render_template("login.html", register_mode=True)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

# --- API Endpoints (Pure Supabase) ---

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "POST":
        d = request.json
        payload = {
            "item_number": d.get("item_number"),
            "name": d.get("name"),
            "current_stock": int(d.get("current_stock") or 0),
            "location": d.get("location"),
            "unit": "pcs"
        }
        res = _supabase_request("POST", "products", json_body=payload)
        if res:
            # Log audit
            _supabase_request("POST", "audit_logs", json_body={
                "product_sku": d.get("item_number"),
                "product_name": d.get("name"),
                "action": "CREATE",
                "amount": int(d.get("current_stock") or 0),
                "location": d.get("location"),
                "username": current_user.username
            })
        return jsonify({"ok": res is not None})
    
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    return jsonify({"ok": True, "data": data})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    d = request.json
    sku = d.get("item_number")
    amount = int(d.get("amount") or 0)
    
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    if not res:
        return jsonify({"ok": False, "error": "Produkt nie istnieje"}), 404
    
    product = res[0]
    if action == "receive":
        new_qty = product['current_stock'] + amount
    else:
        new_qty = product['current_stock'] - amount
        if new_qty < 0:
            return jsonify({"ok": False, "error": "Brak wystarczającej ilości"}), 400
            
    # Update quantity
    update_res = _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"current_stock": new_q})
    
    # Add audit log
    _supabase_request("POST", "audit_logs", json_body={
        "product_sku": sku,
        "product_name": product['name'],
        "action": action.upper(),
        "amount": amount,
        "location": product['location'],
        "username": current_user.username
    })
    
    return jsonify({"ok": True, "current_stock": new_qty})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    return jsonify({"ok": True, "data": data})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))