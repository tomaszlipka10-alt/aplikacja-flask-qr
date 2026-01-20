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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-123")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///warehouse.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- Modele ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(#200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Trasy ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username")).first()
        if u and check_password_hash(u.password_hash, request.form.get("password")):
            login_user(u)
            return redirect(url_for("index"))
        return render_template("login.html", error="Nieprawidłowy login lub hasło")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if User.query.filter_by(username=username).first():
            return render_template("login.html", error="Użytkownik już istnieje", register_mode=True)
        
        new_user = User(
            username=username,
            password_hash=generate_password_hash(password),
            is_admin=False
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("login.html", register_mode=True)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

# --- Inicjalizacja Bazy (WYMUSZONY RESET DLA ADMINÓW) ---
with app.app_context():
    db.drop_all() 
    db.create_all()
    
    admins = [
        {"u": "TomaszLipka", "p": "Welkom01"},
        {"u": "JulesvdHam", "p": "Welkom01"},
        {"u": "TwanvanHeeswijk", "p": "Welkom01"}
    ]
    
    for a in admins:
        if not User.query.filter_by(username=a["u"]).first():
            db.session.add(User(
                username=a["u"],
                password_hash=generate_password_hash(a["p"]),
                is_admin=True
            ))
    db.session.commit()

if __name__ == "__main__":
    app.run(debug=True)