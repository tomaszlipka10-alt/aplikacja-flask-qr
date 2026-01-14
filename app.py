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
# Supabase (Postgres) products storage (optional)
# ----------------------------
# Required env vars on Render:
#   USE_SUPABASE=1
#   SUPABASE_URL=https://<project-ref>.supabase.co
#   SUPABASE_SERVICE_ROLE_KEY=<service_role_key>  (preferred)
#
# Uses Supabase PostgREST via HTTPS (no extra dependencies).
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


def _supabase_request(
    method: str,
    table: str,
    params: Optional[dict] = None,
    json_body: Optional[Union[dict, list]] = None,
    prefer: Optional[str] = None
) -> Any:
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = _supabase_key()
    url = f"{base}/rest/v1/{table.lstrip('/')}"
    if params:
        query = urllib.parse.urlencode(params, doseq=True, safe=",:")
        url = f"{url}?{query}"

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

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
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_raw = e.read()
        try:
            err_text = err_raw.decode("utf-8", errors="replace")
        except Exception:
            err_text = str(err_raw)
        raise RuntimeError(f"Supabase HTTP {e.code}: {err_text}")


def _sb_row_to_api(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "item_number": row.get("sku"),
        "name": row.get("name"),
        "current_stock": row.get("quantity", 0),
        "location_name": row.get("location"),
        "qr_product": row.get("qr_product"),
        "qr_location": row.get("qr_location"),
        "created_at": row.get("created_at"),
    }


def _sb_list_products(limit: int = 500) -> List[dict]:
    rows = _supabase_request(
        "GET",
        "products",
        params={
            "select": "id,sku,name,quantity,location,qr_product,qr_location,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    ) or []
    return [_sb_row_to_api(r) for r in rows]


def _sb_upsert_product(payload: dict) -> dict:
    sku = (payload.get("item_number") or "").strip()
    name = (payload.get("name") or "").strip()
    location = (payload.get("location_name") or "").strip()

    qty_raw = payload.get("current_stock")
    try:
        qty = int(qty_raw) if qty_raw is not None else 0
    except Exception:
        qty = 0

    row = {
        "sku": sku,
        "name": name,
        "quantity": qty,
        "location": location or None,
        "qr_product": payload.get("qr_product") or None,
        "qr_location": payload.get("qr_location") or None,
    }

    created = _supabase_request(
        "POST",
        "products",
        params={"on_conflict": "sku"},
        json_body=row,
        prefer="return=representation,resolution=merge-duplicates",
    ) or []

    return _sb_row_to_api(created[0]) if created else _sb_row_to_api(row)


# ----------------------------
# Supabase Audit helpers
# ----------------------------
def _sb_audit_table() -> str:
    return os.getenv("SUPABASE_AUDIT_TABLE", "audit_log")


def _sb_insert_audit(
    action: str,
    item_number: Optional[str] = None,
    name: Optional[str] = None,
    qty: Optional[int] = None,
    location: Optional[str] = None,
    username: Optional[str] = None
) -> None:
    payload = {
        "action": action,
        "item_number": item_number,
        "name": name,
        "qty": qty,
        "location": location,
        "username": username,
    }
    # usuń None żeby nie wysyłać nulli jeśli nie trzeba
    payload = {k: v for k, v in payload.items() if v is not None}
    _supabase_request("POST", _sb_audit_table(), json_body=payload)


def _sb_list_audit(limit: int = 200) -> list[dict]:
    rows = _supabase_request(
        "GET",
        _sb_audit_table(),
        params={
            "select": "id,action,item_number,name,qty,location,username,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    ) or []
    # zwracamy wprost (kolumny już pasują pod UI)
    return rows


# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "dev-secret-change-me"

# ------------------------------------------------------------
# Database
# ------------------------------------------------------------
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    sqlite_dir = Path("/var/data") if Path("/var/data").exists() else Path("/tmp")
    sqlite_path = sqlite_dir / "warehouse.db"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path.as_posix()}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# ------------------------------------------------------------
# Models
# ------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), default="")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)


class Product(db.Model):
    __tablename__ = "product"
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0, nullable=False)
    location_name = db.Column(db.String(100), default="MAG-1", nullable=False)


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    username = db.Column(db.String(80), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        if not getattr(current_user, "is_admin", False):
            return jsonify({"message": "Admin only"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _has_column(table: str, column: str) -> bool:
    insp = inspect(db.engine)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def ensure_schema():
    db.create_all()

    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    if "user" in tables:
        if not _has_column("user", "password_hash"):
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN password_hash VARCHAR(255)"))
        if not _has_column("user", "is_admin"):
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN"))
        if not _has_column("user", "full_name"):
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN full_name VARCHAR(100)"))

        cols = [c["name"] for c in insp.get_columns("user")]
        if "password" in cols:
            db.session.execute(sql_text("UPDATE user SET password_hash = password WHERE password_hash IS NULL OR password_hash = ''"))

        db.session.execute(sql_text("UPDATE user SET is_admin = 0 WHERE is_admin IS NULL"))

    if "audit_log" in tables:
        if not _has_column("audit_log", "created_at"):
            db.session.execute(sql_text("ALTER TABLE audit_log ADD COLUMN created_at DATETIME"))
            db.session.execute(sql_text("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

    db.session.commit()

    if not User.query.filter_by(username="admin").first():
        db.session.add(User(
            username="admin",
            password_hash=generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")),
            full_name="Administrator",
            is_admin=True
        ))
        db.session.commit()


def export_warehouse_json() -> dict:
    products = Product.query.order_by(Product.id.asc()).all()
    audit = AuditLog.query.order_by(AuditLog.id.asc()).all()
    users = User.query.order_by(User.id.asc()).all()
    return {
        "exported_at_utc": dt.datetime.utcnow().isoformat() + "Z",
        "products": [
            {
                "id": p.id,
                "item_number": p.item_number,
                "name": p.name,
                "current_stock": p.current_stock,
                "location_name": p.location_name,
            } for p in products
        ],
        "audit_log": [
            {
                "id": a.id,
                "product_id": a.product_id,
                "action": a.action,
                "amount": a.amount,
                "username": a.username,
                "created_at": (a.created_at.isoformat() + "Z") if a.created_at else None,
            } for a in audit
        ],
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "is_admin": bool(u.is_admin),
            } for u in users
        ]
    }


def github_put_file(repo: str, path: str, token: str, content_bytes: bytes, message: str) -> dict:
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    b64 = base64.b64encode(content_bytes).decode("utf-8")

    sha = None
    try:
        req = Request(api_url, headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "render-flask-backup"
        })
        with urlopen(req, timeout=20) as r:
            existing = json.loads(r.read().decode("utf-8"))
            sha = existing.get("sha")
    except HTTPError as e:
        if e.code != 404:
            raise
    except URLError:
        raise

    payload = {"message": message, "content": b64}
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    req2 = Request(api_url, data=data, method="PUT", headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "render-flask-backup",
        "Content-Type": "application/json"
    })
    with urlopen(req2, timeout=20) as r2:
        return json.loads(r2.read().decode("utf-8"))


# ------------------------------------------------------------
# Boot-time schema init
# ------------------------------------------------------------
with app.app_context():
    ensure_schema()


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
@app.route("/api/products", methods=["GET", "POST"])
@login_required
def api_products():
    # ---------------------------
    # GET
    # ---------------------------
    if request.method == "GET":
        if _supabase_enabled():
            try:
                products = _sb_list_products(limit=500)
                return jsonify({"ok": True, "products": products, "data": products})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500

        # SQLite fallback
        products = Product.query.order_by(Product.item_number.asc()).all()
        mapped = [
            {
                "id": p.id,
                "item_number": p.item_number,
                "name": p.name,
                "current_stock": p.current_stock,
                "location_name": p.location_name,
                "created_at": None,
            }
            for p in products
        ]
        return jsonify({"ok": True, "products": mapped, "data": mapped})

    # ---------------------------
    # POST (upsert)
    # ---------------------------
    data = request.get_json(force=True, silent=True) or {}

    item_number = (
        data.get("item_number")
        or data.get("sku")
        or data.get("product_id")
        or data.get("id")
        or ""
    )
    name = (
        data.get("name")
        or data.get("product_name")
        or data.get("product")
        or ""
    )
    location = (data.get("location_name") or data.get("location") or "").strip() or "MAG-1"

    qty = data.get("current_stock")
    if qty is None:
        qty = data.get("quantity")
    try:
        qty = int(qty) if qty is not None and str(qty).strip() != "" else 0
    except Exception:
        qty = 0

    item_number = str(item_number).strip()
    name = str(name).strip()

    if not item_number or not name:
        return jsonify({"ok": False, "error": "item_number and name are required"}), 400

    # ---------------------------
    # SUPABASE PATH
    # ---------------------------
    if _supabase_enabled():
        try:
            result = _sb_upsert_product(
                {
                    "item_number": item_number,
                    "name": name,
                    "location_name": location,
                    "current_stock": qty,
                    "qr_product": data.get("qr_product"),
                    "qr_location": data.get("qr_location"),
                }
            )

            # AUDIT (Supabase)
            try:
                _sb_insert_audit(
                    action="PRODUCT_UPSERT",
                    item_number=item_number,
                    name=name,
                    qty=qty,
                    location=location,
                    username=getattr(current_user, "username", None),
                )
            except Exception as _ae:
                print("[audit] supabase insert failed:", _ae)

            return jsonify({"ok": True, "updated": True, "product": result}), 200

        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------------------------
    # SQLITE FALLBACK
    # ---------------------------
    product = Product.query.filter_by(item_number=item_number).first()
    if product:
        product.name = name
        product.location_name = location
        product.current_stock = qty
        db.session.commit()

        db.session.add(AuditLog(
            product_id=product.id,
            action="update",
            amount=qty,
            username=getattr(current_user, "username", ""),
            created_at=dt.datetime.utcnow()
        ))
        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "updated": True,
                "product": {
                    "id": product.id,
                    "item_number": product.item_number,
                    "name": product.name,
                    "current_stock": product.current_stock,
                    "location_name": product.location_name,
                    "created_at": None,
                },
            }
        )

    product = Product(
        item_number=item_number,
        name=name,
        location_name=location,
        current_stock=qty,
    )
    db.session.add(product)
    db.session.commit()

    db.session.add(AuditLog(
        product_id=product.id,
        action="create",
        amount=qty,
        username=getattr(current_user, "username", ""),
        created_at=dt.datetime.utcnow()
    ))
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "updated": False,
            "product": {
                "id": product.id,
                "item_number": product.item_number,
                "name": product.name,
                "current_stock": product.current_stock,
                "location_name": product.location_name,
                "created_at": None,
            },
        }
    ), 201


@app.route("/api/stock/<action>", methods=["POST"])
@login_required
def api_stock(action):
    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    amount = int(data.get("amount") or 0)

    if not product_id or amount <= 0:
        return jsonify({"message": "product_id and positive amount required"}), 400

    product = Product.query.get(int(product_id))
    if not product:
        return jsonify({"message": "Product not found"}), 404

    if action == "receive":
        product.current_stock += amount
    elif action == "issue":
        if product.current_stock < amount:
            return jsonify({"message": "Insufficient stock"}), 400
        product.current_stock -= amount
    else:
        return jsonify({"message": "Unknown action"}), 400

    db.session.add(AuditLog(
        product_id=product.id,
        action=action,
        amount=amount,
        username=current_user.username,
        created_at=dt.datetime.utcnow()
    ))
    db.session.commit()
    return jsonify({"message": "OK", "current_stock": product.current_stock})


# ✅ JEDYNY audit endpoint (bez duplikatów)
@app.get("/api/audit")
@login_required
def api_audit_list():
    # Supabase: czytaj z audit_log w Supabase
    if _supabase_enabled():
        try:
            rows = _sb_list_audit(limit=300)
            return jsonify({"ok": True, "audit": rows, "data": rows})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # SQLite fallback: czytaj z lokalnej tabeli audit_log
    rows = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    mapped = [
        {
            "id": r.id,
            "action": r.action,
            "item_number": None,
            "name": None,
            "qty": r.amount,
            "location": None,
            "username": r.username,
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
        }
        for r in rows
    ]
    return jsonify({"ok": True, "audit": mapped, "data": mapped})


@app.route("/api/admin/export.json")
@login_required
@admin_required
def api_admin_export_json():
    payload = export_warehouse_json()
    tmp = Path("/tmp/warehouse-export.json")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(tmp, as_attachment=True, download_name="warehouse-export.json", mimetype="application/json")


@app.route("/api/admin/backup/github", methods=["POST"])
@login_required
@admin_required
def api_admin_backup_github():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    backup_path = os.environ.get("GITHUB_BACKUP_PATH", "backups/warehouse-backup.json").strip()

    if not token or not repo:
        return jsonify({"message": "Missing GITHUB_TOKEN or GITHUB_REPO in Render Secret Files"}), 400

    payload = export_warehouse_json()
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    msg = f"Backup warehouse to GitHub ({payload['exported_at_utc']})"

    try:
        result = github_put_file(repo=repo, path=backup_path, token=token, content_bytes=content, message=msg)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return jsonify({"message": f"GitHub API error: HTTP {e.code}", "details": body[:500]}), 502
    except Exception as e:
        return jsonify({"message": f"Backup failed: {type(e).__name__}: {e}"}), 502

    return jsonify({
        "message": "Backup saved to GitHub",
        "path": backup_path,
        "commit": (result.get("commit") or {}).get("sha"),
    })


if __name__ == "__main__":
    app.run(debug=True)
