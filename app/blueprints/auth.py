"""Registration, session and profile. Role assignment is server-controlled:
only consumers self-register; merchants/admins are provisioned internally."""

import re
from urllib.parse import urlparse

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_limiter.util import get_remote_address

from ..security import ROLES
from ..extensions import db, limiter
from ..models import User
from ..services import geo as geo_service

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
PHONE_RE = re.compile(r"^0?[789]\d{9}$")  # Nigerian mobile format


def _login_key() -> str:
    """Throttle per client IP *and* per targeted mailbox.

    The IP bucket stops casual hammering; the email bucket stops a distributed
    spray against a single account from rotating addresses.
    """
    email = (request.form.get("email") or "").strip().lower()
    return f"{get_remote_address()}:{email}"


def safe_next(target: str | None, fallback: str) -> str:
    if not target:
        return fallback
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return fallback
    return target if target.startswith("/") else fallback


def _validate_registration(form) -> dict:
    errors = {}
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    phone = re.sub(r"[\s-]", "", form.get("phone") or "")
    password = form.get("password") or ""
    confirm = form.get("confirm_password") or ""

    if len(name) < 3:
        errors["name"] = "Enter your full name."
    if not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."
    elif db.session.query(User.id).filter(User.email == email).first():
        errors["email"] = "That email is already registered."
    if not PHONE_RE.match(phone):
        errors["phone"] = "Enter a Nigerian mobile number, e.g. 0803 000 0000."
    if len(password) < 8:
        errors["password"] = "Use at least 8 characters."
    if password != confirm:
        errors["confirm_password"] = "Passwords do not match."
    return errors


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("storefront.catalog"))

    errors: dict = {}
    form: dict = {}

    if request.method == "POST":
        form = request.form
        errors = _validate_registration(form)
        if not errors:
            user = User(
                name=form["name"].strip(),
                email=form["email"].strip().lower(),
                phone=re.sub(r"[\s-]", "", form["phone"]),
                user_type="consumer",
            )
            user.set_password(form["password"])
            address = (form.get("address") or "").strip()
            if address:
                user.address = address
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            flash("Welcome to NaijaBasket. Your basket is ready.", "success")
            return redirect(safe_next(request.args.get("next"), url_for("storefront.catalog")))

    return render_template("auth/register.html", errors=errors, form=form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute", methods=["POST"])
@limiter.limit("5 per minute", key_func=_login_key, methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("storefront.catalog"))

    error = None
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db.session.query(User).filter(User.email == email).first()

        if user is None or not user.check_password(password):
            error = "Invalid email or password."
        elif not user.is_active:
            error = "This account has been suspended."
        else:
            login_user(user, remember=bool(request.form.get("remember")))
            flash(f"Welcome back, {user.name.split()[0]}.", "success")
            if user.is_merchant:
                return redirect(safe_next(request.args.get("next"), url_for("merchant.dashboard")))
            if user.is_admin:
                return redirect(safe_next(request.args.get("next"), url_for("admin.dashboard")))
            return redirect(safe_next(request.args.get("next"), url_for("storefront.catalog")))

    return render_template("auth/login.html", error=error, email=email)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out. See you at the next basket run.", "info")
    return redirect(url_for("storefront.index"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.name = (request.form.get("name") or current_user.name).strip()
        phone = re.sub(r"[\s-]", "", request.form.get("phone") or "")
        if PHONE_RE.match(phone):
            current_user.phone = phone
        else:
            flash("Enter a valid Nigerian mobile number.", "danger")
        address = (request.form.get("address") or "").strip()
        if address:
            current_user.address = address
            lat = request.form.get("latitude")
            lng = request.form.get("longitude")
            if lat and lng:
                try:
                    current_user.gps_latitude = float(lat)
                    current_user.gps_longitude = float(lng)
                except ValueError:
                    pass
            elif current_user.gps_latitude is None:
                # Default launch hub: Greater Lagos.
                current_user.gps_latitude, current_user.gps_longitude = geo_service.LAGOS_CENTER
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html", roles=ROLES)
