import os
import json
import base64
import datetime as dt
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import urllib.parse

# ----------------------------
# Flask App Config
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-123")

login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------
# Supabase Engine & Helpers
# ----------------------------
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
    if params:
        url += "?" + urllib.parse.urlencode(params)
    
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer: req.add_header("Prefer", prefer)
    
    body_data = json.dumps(json_body).encode("utf-8") if json_body is not None else None

    try:
        with urllib.request.urlopen(req, data=body_data) as response:
            if response.status in [200, 201]:
                return json.loads(response.read().decode("utf-8"))
            elif response.status == 204:
                return []
            return None
    except urllib.error.HTTPError as e:
        error_msg = e.read().decode("utf-8")
        print(f"Supabase HTTP Error {e.code}: {error_msg}")
        return None
    except Exception as e:
        print(f"Supabase Connection Error: {e}")
        return None

# Pomocnicy Supabase
def _sb_get_product(sku):
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    return res[0] if res and len(res) > 0 else None

def _sb_set_product_quantity(sku, new_qty):
    return _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"current_stock": new_qty})

def _sb_insert_audit(action, sku, name, amount, loc, user):
    body = {
        "product_sku": sku,
        "product_name": name,
        "action": action,
        "amount": amount,
        "location": loc,
        "username": user
    }
    return _supabase_request("POST", "audit_logs", json_body=body)

# ----------------------------
# Flask-Login User Setup
# ----------------------------
class User(UserMixin):
    def __init__(self, id, username, is_admin):
        self.id = id
        self.username = username
        self.is_admin = is_admin

@login_manager.user_loader
def load_user(user_id):
    res = _supabase_request("GET", "users", {"id": f"eq.{user_id}"})
    if res and len(res) > 0:
        return User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False))
    return None

# ----------------------------
# GitHub Backup Logic
# ----------------------------
def github_put_file(repo, path, token, content_bytes, message):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            curr = json.loads(r.read().decode("utf-8"))
            sha = curr.get("sha")
    except: pass
    payload = {"message": message, "content": base64.b64encode(content_bytes).decode("utf-8")}
    if sha: payload["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")
    with urllib.request.urlopen(req) as r: return r.status

# ----------------------------
# Routes
# ----------------------------
@app.route("/health")
def health(): return "OK", 200

@app.route("/")
@login_required
def index(): return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u_name = request.form.get("username")
        pwd = request.form.get("password")
        res = _supabase_request("GET", "users", {"username": f"eq.{u_name}"})
        if res and len(res) > 0:
            if check_password_hash(res[0]['password_hash'], pwd):
                userobj = User(res[0]['id'], res[0]['username'], res[0].get('is_admin', False))
                login_user(userobj)
                return redirect(url_for("index"))
        return render_template("login.html", error="Błędny login lub hasło")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u_name = request.form.get("username")
        pwd = request.form.get("password")
        if _supabase_request("GET", "users", {"username": f"eq.{u_name}"}):
            return render_template("login.html", error="Użytkownik istnieje", register_mode=True)
        _supabase_request("POST", "users", json_body={
            "username": u_name,
            "password_hash": generate_password_hash(pwd),
            "is_admin": False
        })
        return redirect(url_for("login"))
    return render_template("login.html", register_mode=True)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/api/products")
@login_required
def api_products():
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    return jsonify({"ok": True, "data": data})

@app.route("/api/stock", methods=["POST"])
@login_required
def api_stock():
    data = request.json or {}
    sku = data.get("item_number")
    action = data.get("action")
    amount = int(data.get("amount", 0))
    if amount <= 0: return jsonify({"ok":False,"error":"Nieprawidłowa ilość"}), 400

    row = _sb_get_product(sku)
    if not row: return jsonify({"ok":False,"error":"Produkt nie istnieje"}), 404
    
    new_q = row['current_stock'] + amount if action == "receive" else row['current_stock'] - amount
    if new_q < 0: return jsonify({"ok":False,"error":"Brak wystarczającej ilości towaru"}), 400
    
    _sb_set_product_quantity(sku, new_q)
    _sb_insert_audit(action.upper(), sku, row['name'], amount, row['location'], current_user.username)
    return jsonify({"ok":True, "current_stock": new_q})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    out = []
    for r in data:
        out.append({
            "created_at": r.get("created_at"),
            "type": r.get("action"),
            "item_number": r.get("product_sku"),
            "name": r.get("product_name"),
            "amount": r.get("amount"),
            "username": r.get("username")
        })
    return jsonify({"ok": True, "data": out})

@app.route("/api/admin/backup/github", methods=["POST"])
@login_required
def api_backup_github():
    if not current_user.is_admin: return jsonify({"message":"Forbidden"}), 403
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    if not token or not repo: return jsonify({"message": "Missing Config"}), 400
    try:
        prods = _supabase_request("GET", "products")
        logs = _supabase_request("GET", "audit_logs")
        payload = {
            "exported_at_utc": dt.datetime.utcnow().isoformat(),
            "products": prods,
            "audit": logs
        }
        content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        github_put_file(repo, "backups/warehouse_data.json", token, content, f"Backup {payload['exported_at_utc']}")
        return jsonify({"message": "Backup wykonany pomyślnie"})
    except Exception as e: return jsonify({"message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)