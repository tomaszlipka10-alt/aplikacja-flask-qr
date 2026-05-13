import os
import json
import base64
import datetime as dt
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import urllib.parse
import pandas as pd
from io import BytesIO
from flask import send_file

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

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    if request.method == "POST":
        d = request.json
        sku = str(d.get('item_number', '')).strip()
        prj = str(d.get('project_number', '')).strip()
        name = str(d.get('name', '')).strip()
        
        _supabase_request("POST", "products", json_body={
            "item_number": sku,
            "name": name,
            "current_stock": int(d.get('current_stock', 0)),
            "min_stock": int(d.get('min_stock', 0)),
            "location": str(d.get('location', '')).strip(),
            "category": str(d.get('category', 'material')),
            "project_number": prj
        })
        
        _supabase_request("POST", "audit_logs", json_body={
            "item_number": sku,
            "name": name,
            "action": "CREATE",
            "qty": int(d.get('current_stock', 0)),
            "location": str(d.get('location', '')).strip(),
            "username": current_user.username,
            "project_number": prj,
            "note": "Initial creation"
        })
        return jsonify({"ok": True})
    
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    low_stock_count = sum(1 for p in data if int(p.get('min_stock', 0)) > 0 and int(p.get('current_stock', 0)) <= int(p.get('min_stock', 0)))
    return jsonify({"ok": True, "data": data, "low_stock_count": low_stock_count})

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
        "item_number": sku, "name": p['name'], "action": action.upper(), "qty": amt, 
        "location": p['location'], "username": current_user.username, "note": d.get('note', ''),
        "project_number": p.get('project_number', '')
    })
    return jsonify({"ok": True, "current_stock": new_q})

@app.route("/api/relocate", methods=["POST"])
@login_required
def api_relocate():
    d = request.json
    sku, new_loc = d.get('item_number'), str(d.get('new_location', '')).strip()
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    if not res: return jsonify({"ok": False}), 404
    p = res[0]
    _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"location": new_loc})
    _supabase_request("POST", "audit_logs", json_body={
        "item_number": sku, "name": p['name'], "action": "RELOCATE", "qty": p['current_stock'],
        "location": f"{p.get('location')} -> {new_loc}", "username": current_user.username,
        "project_number": p.get('project_number', '')
    })
    return jsonify({"ok": True})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    return jsonify({"ok": True, "audit": data})

@app.route("/api/admin/export/excel")
@login_required
def export_excel():
    if not current_user.is_admin: return "Access denied", 403
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"})
    if not data: return "No data found", 404
    df = pd.DataFrame(data)
    cols = {'item_number': 'ID', 'name': 'Nazwa', 'project_number': 'Projekt', 'current_stock': 'Stan', 'location': 'Lokalizacja'}
    df = df[[c for c in cols.keys() if c in df.columns]].rename(columns=cols)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="Storage_Inventory.xlsx")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))