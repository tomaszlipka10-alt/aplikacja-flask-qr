import os
import json
import base64
import datetime as dt
from pathlib import Path
from functools import wraps
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text as sql_text

# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = Flask(__name__)

# SECRET_KEY: allow running even if env not set (but recommend setting it in Render)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "dev-secret-change-me"

# ------------------------------------------------------------
# Database
# - Render free web service has ephemeral filesystem unless you attach a disk.
# - We'll prefer Postgres if DATABASE_URL exists, otherwise use SQLite in /var/data if available (disk),
#   else fallback to /tmp (ephemeral).
# ------------------------------------------------------------
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    sqlite_dir = Path("/var/data") if Path("/var/data").exists() else Path("/tmp")
    sqlite_path = sqlite_dir / "warehouse.db"
    # ensure directory exists (Render: /tmp always exists)
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
    password_hash = db.Column(db.String(255), nullable=False)  # renamed from password -> password_hash
    full_name = db.Column(db.String(100), default="")
    is_admin = db.Column(db.Boolean, default=False, nullable=False)


class Product(db.Model):
    __tablename__ = "product"
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current_stock = db.Column(db.Integer, default=0, nullable=False)
    location_name = db.Column(db.String(100), default="MAG-1", nullable=False)


def _product_table_cols() -> set[str]:
    """Return column names for the existing 'product' table.

    The app has had a few schema variants over time. On Render, your SQLite
    file can keep an older table definition. Using ORM against a mismatched
    table causes 500 errors. We introspect the table and use safe raw SQL in
    the API endpoints so both schemas keep working.
    """
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(product)"))
            return {r[1] for r in rows}  # second column is name
    except Exception:
        return set()


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
    """
    Create missing tables and apply a few safe, idempotent migrations for SQLite/Postgres.
    We DO NOT use non-constant defaults in ALTER TABLE for SQLite (it fails).
    """
    db.create_all()

    # --- User: password -> password_hash migration
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    if "user" in tables:
        # add missing columns
        if not _has_column("user", "password_hash"):
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN password_hash VARCHAR(255)"))
        if not _has_column("user", "is_admin"):
            # constant default ok, but SQLite adds NULLs for existing rows; we will set them
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN"))
        if not _has_column("user", "full_name"):
            db.session.execute(sql_text("ALTER TABLE user ADD COLUMN full_name VARCHAR(100)"))

        # if old column "password" exists, copy values into password_hash then leave it (SQLite cannot drop columns)
        cols = [c["name"] for c in insp.get_columns("user")]
        if "password" in cols:
            # copy only where password_hash is null/empty
            db.session.execute(sql_text("UPDATE user SET password_hash = password WHERE password_hash IS NULL OR password_hash = ''"))

        # normalize is_admin
        db.session.execute(sql_text("UPDATE user SET is_admin = 0 WHERE is_admin IS NULL"))

    # --- AuditLog: created_at migration
    if "audit_log" in tables:
        if not _has_column("audit_log", "created_at"):
            # SQLite: cannot add column with DEFAULT CURRENT_TIMESTAMP, so add nullable first then backfill.
            db.session.execute(sql_text("ALTER TABLE audit_log ADD COLUMN created_at DATETIME"))
            db.session.execute(sql_text("UPDATE audit_log SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

    db.session.commit()

    # Seed admin user if missing
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
    """
    Create or update a file in GitHub via Contents API.
    Uses standard library urllib (no extra deps).
    """
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    b64 = base64.b64encode(content_bytes).decode("utf-8")

    # Step 1: check if file exists to get sha (update)
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
        # network issues etc
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
    cols = _product_table_cols()

    # --- GET ---
    if request.method == "GET":
        try:
            # New schema
            if {"item_number", "name", "current_stock", "location_name"}.issubset(cols):
                sql = """
                    SELECT id, item_number, name, current_stock, location_name
                    FROM product
                    ORDER BY item_number ASC
                """
            # Legacy schema (older commits)
            elif {"sku", "name", "location"}.issubset(cols):
                sql = """
                    SELECT id,
                           sku AS item_number,
                           name,
                           0 AS current_stock,
                           location AS location_name
                    FROM product
                    ORDER BY sku ASC
                """
            else:
                # Unknown schema; return empty instead of 500
                return jsonify({"data": []})

            with db.engine.connect() as conn:
                rows = conn.execute(db.text(sql)).mappings().all()
            return jsonify({"data": [dict(r) for r in rows]})
        except Exception as e:
            # Don’t crash the UI; surface the error
            return jsonify({"ok": False, "error": str(e), "data": []}), 500

    # --- POST (upsert) ---
    data = request.get_json(force=True, silent=True) or {}

    # Be tolerant to frontend payload naming (we've had a few UI iterations).
    item_number = (
        data.get("item_number")
        or data.get("sku")
        or data.get("product_id")
        or data.get("id")
        or data.get("ProductID")
        or ""
    ).strip()
    name = (
        data.get("name")
        or data.get("product_name")
        or data.get("ProductName")
        or ""
    ).strip()
    location = (data.get("location_name") or data.get("location") or "MAG-1").strip()

    stock_raw = data.get("current_stock", data.get("quantity", 0))
    try:
        current_stock = int(stock_raw) if stock_raw is not None and str(stock_raw).strip() != "" else 0
    except Exception:
        current_stock = 0

    if not item_number or not name:
        return jsonify({"ok": False, "error": "item_number and name are required", "message": "item_number and name are required"}), 400

    # New schema upsert
    if {"item_number", "name", "current_stock", "location_name"}.issubset(cols):
        product = Product.query.filter_by(item_number=item_number).first()
        if product:
            product.name = name
            product.location_name = location
            product.current_stock = current_stock
        else:
            product = Product(
                item_number=item_number,
                name=name,
                current_stock=current_stock,
                location_name=location,
            )
            db.session.add(product)
        db.session.commit()
        return jsonify({"ok": True, "product": {
            "id": product.id,
            "item_number": product.item_number,
            "name": product.name,
            "current_stock": product.current_stock,
            "location_name": product.location_name,
        }})

    # Legacy schema upsert (sku/name/location)
    if {"sku", "name", "location"}.issubset(cols):
        try:
            with db.engine.begin() as conn:
                # Update first
                upd = db.text("""
                    UPDATE product
                    SET name = :name,
                        location = :location
                    WHERE sku = :sku
                """)
                res = conn.execute(upd, {"name": name, "location": location, "sku": item_number})
                if res.rowcount == 0:
                    ins = db.text("""
                        INSERT INTO product (sku, name, location)
                        VALUES (:sku, :name, :location)
                    """)
                    conn.execute(ins, {"sku": item_number, "name": name, "location": location})
                # Fetch row (best-effort)
                sel = db.text("SELECT id, sku AS item_number, name, 0 AS current_stock, location AS location_name FROM product WHERE sku = :sku")
                row = conn.execute(sel, {"sku": item_number}).mappings().first()
            return jsonify({"ok": True, "product": dict(row) if row else {
                "item_number": item_number,
                "name": name,
                "current_stock": 0,
                "location_name": location,
            }})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # Unknown schema
    return jsonify({"ok": False, "error": "Unknown product table schema"}), 500

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


@app.route("/api/audit")
@login_required
def api_audit():
    rows = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    return jsonify({
        "data": [
            {
                "id": r.id,
                "product_id": r.product_id,
                "action": r.action,
                "amount": r.amount,
                "username": r.username,
                "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
            } for r in rows
        ]
    })


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
