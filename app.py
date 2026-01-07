import base64
import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)


# --- SQLite path (Render free tier safe default) ---
def _sqlite_path() -> str:
    # If a persistent disk is ever attached later, Render typically mounts it at /var/data
    if Path("/var/data").exists():
        return "/var/data/warehouse.db"
    return "/tmp/warehouse.db"


app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL") or f"sqlite:///{_sqlite_path()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    full_name = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.String(32), default=utcnow_iso, nullable=False)


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    sku = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=True)
    qr_code = db.Column(db.String(200), nullable=True)
    location = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.String(32), default=utcnow_iso, nullable=False)


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


def admin_required(fn):
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            return jsonify({"ok": False, "error": "Admin only"}), 403
        return fn(*args, **kwargs)

    wrapper.__name__ = fn.__name__
    return wrapper


def ensure_schema_and_admin() -> None:
    db.create_all()

    # Default admin (user expects admin/admin123)
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    admin_full_name = os.environ.get("ADMIN_FULL_NAME", "Administrator")

    u = User.query.filter_by(username=admin_username).first()
    if not u:
        u = User(
            username=admin_username,
            password_hash=generate_password_hash(admin_password),
            is_admin=True,
            full_name=admin_full_name,
        )
        db.session.add(u)
        db.session.commit()
    else:
        # If an old DB already exists with a different password, reset it to match the UI hint.
        # This is intentionally strict: only for the admin user.
        if u.is_admin and not check_password_hash(u.password_hash, admin_password):
            u.password_hash = generate_password_hash(admin_password)
            if not u.full_name:
                u.full_name = admin_full_name
            db.session.commit()


with app.app_context():
    ensure_schema_and_admin()


@app.get("/health")
def health():
    return "OK", 200


@app.get("/")
@login_required
def index():
    return render_template("index.html", user=current_user)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    nxt = request.args.get("next") or url_for("index")

    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        login_user(user)
        return redirect(nxt)

    return render_template("login.html", error="Invalid username or password"), 401


@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# -------------------- Products API --------------------


@app.get("/api/products")
@login_required
def api_products_list():
    products = Product.query.order_by(Product.id.desc()).all()
    return jsonify(
        {
            "ok": True,
            "products": [
                {
                    "id": p.id,
                    "name": p.name,
                    "sku": p.sku,
                    "description": p.description,
                    "qr_code": p.qr_code,
                    "location": p.location,
                    "created_at": p.created_at,
                }
                for p in products
            ],
        }
    )


@app.post("/api/products")
@login_required
def api_products_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400

    p = Product(
        name=name,
        sku=(data.get("sku") or "").strip() or None,
        description=(data.get("description") or "").strip() or None,
        qr_code=(data.get("qr_code") or "").strip() or None,
        location=(data.get("location") or "").strip() or None,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "id": p.id})


# -------------------- Admin: Backup to GitHub --------------------


def _github_api_request(url: str, method: str, token: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=body, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _github_get_file_sha(repo: str, path: str, token: str):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url=url, method="GET")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


@app.post("/api/admin/backup/github")
@login_required
@admin_required
def api_admin_backup_github():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")  # e.g. tomaszlipka10-alt/aplikacja-flask-qr
    path = os.environ.get("GITHUB_BACKUP_PATH", "backups/warehouse-backup.json")

    if not token or not repo:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Missing GITHUB_TOKEN or GITHUB_REPO in Render environment/secret files",
                }
            ),
            400,
        )

    # Build backup JSON
    users = User.query.order_by(User.id.asc()).all()
    products = Product.query.order_by(Product.id.asc()).all()
    backup_obj = {
        "generated_at": utcnow_iso(),
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "full_name": u.full_name,
                "created_at": u.created_at,
            }
            for u in users
        ],
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "sku": p.sku,
                "description": p.description,
                "qr_code": p.qr_code,
                "location": p.location,
                "created_at": p.created_at,
            }
            for p in products
        ],
    }
    content_bytes = json.dumps(backup_obj, ensure_ascii=False, indent=2).encode("utf-8")
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    sha = _github_get_file_sha(repo=repo, path=path, token=token)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": f"Backup warehouse to GitHub ({backup_obj['generated_at']})",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    try:
        status, data = _github_api_request(url=url, method="PUT", token=token, payload=payload)
        return jsonify({"ok": True, "status": status, "commit": (data.get("commit") or {}).get("sha")})
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        return jsonify({"ok": False, "error": f"GitHub API error: {e.code}", "detail": detail}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
