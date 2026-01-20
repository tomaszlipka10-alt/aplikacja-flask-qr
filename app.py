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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-123")

login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- Supabase Helpers ---
def _supabase_url():
    return (os.getenv("SUPABASE_URL") or "").rstrip("/")

def _supabase_key():
    return (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or 
            os.getenv("SUPABASE_SERVICE_KEY") or 
            os.getenv("SUPABASE_KEY") or "")

def _supabase_request(method, table, params=None, json_body=None, prefer=None):
    base = _supabase_url()
    key = _supabase_key()
    if not base or not key: return None
    url = f"{base}/rest/v1/{table}"
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer: req.add_header("Prefer", prefer)
    body_data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    try:
        with urllib.request.urlopen(req, data=body_data) as response:
            if response.status in [200, 201]: return json.loads(response.read().decode("utf-8"))
            return [] if response.status == 204 else None
    except Exception as e:
        print(f"Supabase Error: {e}")
        return None

# --- Auth ---
class User(UserMixin):
    def __init__(self, id, username, is_admin):
        self.id, self.username, self.is_admin = id, username, is_admin

@login_manager.user_loader
def load_user(user_id):
    res = _supabase_request("GET", "users", {"id": f"eq.{user_id}"})
    if res: return User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False))
    return None

# --- Routes ---
@app.route("/health")
def health(): return "OK", 200

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

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u, p = request.form.get("username"), request.form.get("password")
        _supabase_request("POST", "users", json_body={"username": u, "password_hash": generate_password_hash(p), "is_admin": False})
        return redirect(url_for("login"))
    return render_template("login.html", register_mode=True)

@app.route("/logout")
def logout(): logout_user(); return redirect(url_for("login"))

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "POST":
        d = request.json
        body = {"item_number": d.get("item_number"), "name": d.get("name"), "current_stock": int(d.get("current_stock", 0)), "location": d.get("location")}
        res = _supabase_request("POST", "products", json_body=body)
        return jsonify({"ok": res is not None})
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    return jsonify({"ok": True, "data": data})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    d = request.json
    sku, amt = d.get("item_number"), int(d.get("amount", 0))
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    if not res: return jsonify({"ok":False, "error":"Product not found"}), 404
    p = res[0]
    new_q = p['current_stock'] + amt if action == "receive" else p['current_stock'] - amt
    if new_q < 0: return jsonify({"ok":False, "error":"Low stock"}), 400
    _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"current_stock": new_q})
    _supabase_request("POST", "audit_logs", json_body={"product_sku": sku, "product_name": p['name'], "action": action.upper(), "amount": amt, "location": p['location'], "username": current_user.username})
    return jsonify({"ok":True, "current_stock": new_q})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    return jsonify({"ok": True, "data": data})

@app.route("/api/admin/backup/github", methods=["POST"])
@login_required
def api_backup():
    if not current_user.is_admin: return jsonify({"message":"Forbidden"}), 403
    t, r = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    prods = _supabase_request("GET", "products")
    logs = _supabase_request("GET", "audit_logs")
    content = json.dumps({"exported_at": dt.datetime.utcnow().isoformat(), "products": prods, "audit": logs}, indent=2).encode("utf-8")
    url = f"https://api.github.com/repos/{r}/contents/backups/warehouse_data.json"
    headers = {"Authorization": f"token {t}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
            sha = json.loads(resp.read().decode("utf-8")).get("sha")
    except: pass
    p_data = {"message": "Backup", "content": base64.b64encode(content).decode("utf-8")}
    if sha: p_data["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(p_data).encode("utf-8"), headers=headers, method="PUT")
    with urllib.request.urlopen(req): return jsonify({"message": "Backup OK"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))