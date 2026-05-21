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
    existing = _supabase_request("GET", "users", {"username": f"eq.{u}"})
    if existing:
        return render_template("login.html", error="User already exists")
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
        sku = str(d.get('item_number', '')).strip()
        prj = str(d.get('project_number', '')).strip()
        supplier = str(d.get('supplier', '')).strip()
        name = str(d.get('name', '')).strip()
        pallet_id = str(d.get('pallet_id', '')).strip()
        
        raw_stock = str(d.get('current_stock', '0')).strip()
        raw_min = str(d.get('min_stock', '0')).strip()
        
        current_stock_val = int(raw_stock) if raw_stock.isdigit() else 0
        min_stock_val = int(raw_min) if raw_min.isdigit() else 0
        
        _supabase_request("POST", "products", json_body={
            "item_number": sku,
            "name": name,
            "current_stock": current_stock_val,
            "min_stock": min_stock_val,
            "location": str(d.get('location', '')).strip(),
            "category": str(d.get('category', 'material')),
            "unit": str(d.get('unit', 'pcs')),
            "project_number": prj,
            "supplier": supplier,
            "pallet_id": pallet_id if pallet_id else None
        })
        
        _supabase_request("POST", "audit_logs", json_body={
            "item_number": sku,
            "name": name,
            "action": "CREATE",
            "qty": current_stock_val,
            "location": str(d.get('location', '')).strip(),
            "username": current_user.username,
            "project_number": prj,
            "note": f"Initial creation. Pallet ID: {pallet_id if pallet_id else 'None'}. Supplier: {supplier}"
        })
        return jsonify({"ok": True})
    
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"}) or []
    low_stock_count = sum(1 for p in data if int(p.get('min_stock', 0)) > 0 and int(p.get('current_stock', 0)) <= int(p.get('min_stock', 0)))
    return jsonify({"ok": True, "data": data, "low_stock_count": low_stock_count})

@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    d = request.json
    sku = d['item_number']
    
    raw_amount = str(d.get('amount', '0')).strip()
    amt = int(raw_amount) if raw_amount.isdigit() else 0
    
    res = _supabase_request("GET", "products", {"item_number": f"eq.{sku}"})
    if not res: return jsonify({"ok": False}), 404
    p = res[0]
    
    if action == "issue" and p['current_stock'] < amt:
        return jsonify({"ok": False, "error": "Insufficient stock"}), 400
        
    new_q = p['current_stock'] + amt if action == "receive" else p['current_stock'] - amt
    _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"current_stock": new_q})
    _supabase_request("POST", "audit_logs", json_body={
        "item_number": sku, "name": p['name'], "action": action.upper(), "qty": amt, 
        "location": p['location'], "username": current_user.username, "note": d.get('note', ''),
        "project_number": p.get('project_number', '')
    })
    return jsonify({"ok": True, "current_stock": new_q})

@app.route("/api/admin/export/excel")
@login_required
def export_excel():
    if not current_user.is_admin: return "Access denied", 403
    data = _supabase_request("GET", "products", {"select": "*", "order": "item_number"})
    if not data: return "No data found", 404
    df = pd.DataFrame(data)
    
    columns_mapping = {
        'item_number': 'ID',
        'name': 'Name',
        'project_number': 'Project',
        'supplier': 'Supplier',
        'current_stock': 'Qty',
        'min_stock': 'Min Stock',
        'location': 'Location',
        'category': 'Category',
        'pallet_id': 'Pallet ID'
    }
    
    available_cols = [c for c in columns_mapping.keys() if c in df.columns]
    df = df[available_cols].rename(columns=columns_mapping)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Inventory')
    
    output.seek(0)
    filename = f"Inventory_{dt.datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=filename)

@app.route("/api/relocate", methods=["POST"])
@login_required
def api_relocate():
    d = request.json
    target_id = str(d.get('item_number', '')).strip()  # Może to być Product ID LUB Pallet ID
    new_loc = str(d.get('new_location', '')).strip()
    
    if not target_id or not new_loc:
        return jsonify({"ok": False, "error": "Missing parameters"}), 400

    # Najpierw sprawdzamy czy to Pallet ID (szukamy produktów przypisanych do tej palety)
    palette_products = _supabase_request("GET", "products", {"pallet_id": f"eq.{target_id}"})
    
    if palette_products:
        # PRZELOKOWANIE GRUPY PRODUKTÓW (CAŁA PALETA)
        for p in palette_products:
            sku = p['item_number']
            old_loc = p.get('location', 'Unknown')
            _supabase_request("PATCH", "products", {"item_number": f"eq.{sku}"}, {"location": new_loc})
            _supabase_request("POST", "audit_logs", json_body={
                "item_number": sku, "name": p['name'], "action": "RELOCATE_PALLET", "qty": p['current_stock'],
                "location": f"[{target_id}] {old_loc} -> {new_loc}", "username": current_user.username,
                "project_number": p.get('project_number', '')
            })
        return jsonify({"ok": True, "mode": "pallet", "count": len(palette_products)})

    # Jeśli nie znaleziono produktów po Pallet ID, traktujemy to jako relokację pojedynczego produktu
    res = _supabase_request("GET", "products", {"item_number": f"eq.{target_id}"})
    if not res: 
        return jsonify({"ok": False, "error": "No product or pallet found with this ID"}), 404
        
    p = res[0]
    old_loc = p.get('location', 'Unknown')
    
    _supabase_request("PATCH", "products", {"item_number": f"eq.{target_id}"}, {"location": new_loc})
    _supabase_request("POST", "audit_logs", json_body={
        "item_number": sku if 'sku' in locals() else p['item_number'], "name": p['name'], "action": "RELOCATE", "qty": p['current_stock'],
        "location": f"{old_loc} -> {new_loc}", "username": current_user.username,
        "project_number": p.get('project_number', '')
    })
    return jsonify({"ok": True, "mode": "single"})

@app.route("/api/audit")
@login_required
def api_audit():
    data = _supabase_request("GET", "audit_logs", {"select": "*", "order": "created_at.desc", "limit": 100}) or []
    return jsonify({"ok": True, "audit": data})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))