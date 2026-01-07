import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ----------------------------
# App + DB
# ----------------------------
app = Flask(__name__)

# IMPORTANT: keep DB in /var/data when available (Render persistent disk), else /tmp
sqlite_path = "/var/data/warehouse.db" if Path("/var/data").exists() else "/tmp/warehouse.db"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Secret key
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# ----------------------------
# Models
# ----------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=True)
    role = db.Column(db.String(30), nullable=False, default="user")  # 'admin' or 'user'

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_number = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    location = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(80), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def ensure_schema() -> None:
    with app.app_context():
        db.create_all()
        # Create default admin if missing
        if not User.query.filter_by(username="admin").first():
            u = User(username="admin", full_name="Administrator", role="admin")
            u.set_password(os.environ.get("ADMIN_PASSWORD", "admin"))
            db.session.add(u)
            db.session.commit()


def audit(action: str, details: str | None = None) -> None:
    try:
        db.session.add(AuditLog(action=action, details=details))
        db.session.commit()
    except Exception:
        db.session.rollback()


# Ensure schema at import
ensure_schema()


# ----------------------------
# Auth helpers
# ----------------------------
def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if getattr(current_user, "role", "user") != "admin":
            return jsonify({"error": "admin_required"}), 403
        return func(*args, **kwargs)

    return wrapper


# ----------------------------
# Pages
# ----------------------------
@app.get("/health")
def health():
    return "OK", 200


@app.get("/")
@login_required
def index():
    # Pass explicit 'user' to template (avoid jinja undefined issues)
    return render_template("index.html", welcome_title="Warehouse Dashboard", user=current_user)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)

        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")


@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ----------------------------
# API: Products
# ----------------------------
@app.get("/api/products")
@login_required
def api_products_list():
    products = Product.query.order_by(Product.id.desc()).all()
    return jsonify(
        [
            {
                "id": p.id,
                "item_number": p.item_number,
                "name": p.name,
                "location": p.location,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in products
        ]
    )


@app.post("/api/products")
@login_required
def api_products_add():
    data = request.get_json(silent=True) or {}
    item_number = (data.get("item_number") or "").strip()
    name = (data.get("name") or "").strip()
    location = (data.get("location") or "").strip() or None

    if not item_number:
        return jsonify({"error": "item_number_required"}), 400
    if not name:
        return jsonify({"error": "name_required"}), 400

    if Product.query.filter_by(item_number=item_number).first():
        return jsonify({"error": "item_number_exists"}), 409

    p = Product(item_number=item_number, name=name, location=location)
    db.session.add(p)
    db.session.commit()

    audit("product_add", f"{item_number} | {name} | {location or ''}")

    return jsonify({"ok": True, "id": p.id})


# ----------------------------
# API: Audit
# ----------------------------
@app.get("/api/audit")
@login_required
def api_audit_list():
    logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(200).all()
    return jsonify(
        [
            {
                "id": a.id,
                "action": a.action,
                "details": a.details,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in logs
        ]
    )


# ----------------------------
# API: Admin backup to GitHub
# ----------------------------
@app.post("/api/admin/backup/github")
@login_required
@admin_required
def api_backup_github():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")  # e.g. user/repo
    path = os.environ.get("GITHUB_BACKUP_PATH", "backups/warehouse-backup.json")

    if not token or not repo:
        return (
            jsonify(
                {
                    "error": "missing_github_env",
                    "hint": "Set GITHUB_TOKEN and GITHUB_REPO in Render (Environment/Secret Files).",
                }
            ),
            400,
        )

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "products": [
            {
                "item_number": p.item_number,
                "name": p.name,
                "location": p.location,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in Product.query.order_by(Product.id.asc()).all()
        ],
        "audit": [
            {
                "action": a.action,
                "details": a.details,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in AuditLog.query.order_by(AuditLog.id.asc()).all()
        ],
    }

    content_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"

    def _request(method: str, url: str, body: dict | None = None):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "wms-backup")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = e.read().decode("utf-8")
            except Exception:
                err = str(e)
            return e.code, {"error": "http_error", "status": e.code, "body": err}

    # Check if file exists to get sha
    status, existing = _request("GET", api_url)
    sha = None
    if status == 200 and isinstance(existing, dict):
        sha = existing.get("sha")

    put_body = {
        "message": "Backup warehouse to GitHub",
        "content": content_b64,
    }
    if sha:
        put_body["sha"] = sha

    status2, result = _request("PUT", api_url, put_body)
    if status2 not in (200, 201):
        return jsonify({"error": "backup_failed", "details": result}), 500

    audit("backup_github", f"{repo}/{path}")

    commit_url = None
    try:
        commit_url = result.get("commit", {}).get("html_url")
    except Exception:
        commit_url = None

    return jsonify({"ok": True, "path": path, "commit_url": commit_url})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), debug=True)
