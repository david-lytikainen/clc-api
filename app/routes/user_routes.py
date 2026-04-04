import logging
import os
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import User
from app.utils.email import (
    send_confirm_email,
    send_password_reset_code_email,
    send_welcome_email,
)

logger = logging.getLogger(__name__)

user_bp = Blueprint("user", __name__)


@user_bp.route("/create-account", methods=["POST"])
def create_account():
    data = request.get_json() or {}

    required = ["first_name", "last_name", "email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400

    if User.query.filter_by(email=data["email"]).first():
        return jsonify({"error": "Email already exists"}), 409

    try:
        hashed_password = generate_password_hash(data["password"])
        verify_token = str(uuid4())
        user = User(
            role_id=1,
            email=data["email"],
            password=hashed_password,
            first_name=data["first_name"],
            last_name=data["last_name"],
            email_verify_token=verify_token,
            email_verify_token_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.session.add(user)
        db.session.commit()

        frontend_url = (
            os.getenv("CLIENT_URL") or os.getenv("FRONTEND_URL") or "http://localhost:3000"
        ).rstrip("/")
        verify_url = f"{frontend_url}/verify-email?token={verify_token}"
        send_confirm_email(user, verify_url)

        token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201

    except Exception as e:
        logger.exception("Create account failed: %s", e)
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500


@user_bp.route("/sign-in", methods=["POST"])
def sign_in():
    data = request.get_json() or {}

    required = ["email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400

    user = User.query.filter_by(email=data["email"]).first()
    if not user:
        return jsonify({"error": "Invalid email"}), 401
    elif not check_password_hash(user.password, data["password"]):
        return jsonify({"error": "Invalid password"}), 401

    token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
    return jsonify({"token": token, "user": user.to_dict()}), 200


@user_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "No account associated with this email"}), 404
    code = "".join(str(random.randint(0, 9)) for _ in range(6))
    user.forgot_password_code = code
    user.forgot_password_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.session.commit()
    send_password_reset_code_email(user, code)
    return jsonify({"message": "Code sent"}), 200


@user_bp.route("/verify-reset-code", methods=["POST"])
def verify_reset_code():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    if not email or not code:
        return jsonify({"error": "Email and code are required"}), 400
    user = User.query.filter_by(email=email).first()
    if not user or user.forgot_password_code != code or not user.forgot_password_code_expires_at:
        return jsonify({"error": "Invalid code"}), 400
    if user.forgot_password_code_expires_at < datetime.now(timezone.utc):
        return jsonify({"error": "Invalid code"}), 400
    return jsonify({"valid": True}), 200


@user_bp.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password")
    if not email or not code:
        return jsonify({"error": "Email and code are required"}), 400
    if not new_password or len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    user = User.query.filter_by(email=email).first()
    if not user or user.forgot_password_code != code or not user.forgot_password_code_expires_at:
        return jsonify({"error": "Invalid code"}), 400
    if user.forgot_password_code_expires_at < datetime.now(timezone.utc):
        return jsonify({"error": "Code has expired"}), 400
    user.password = generate_password_hash(new_password)
    user.forgot_password_code = None
    user.forgot_password_code_expires_at = None
    db.session.commit()
    token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
    return jsonify({"token": token, "user": user.to_dict()}), 200


@user_bp.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token is required"}), 400
    user = User.query.filter_by(email_verify_token=token, email_verified=False).first()
    if not user:
        return jsonify({"error": "Invalid or expired link"}), 400
    if not user.email_verify_token_expires_at or user.email_verify_token_expires_at < datetime.now(
        timezone.utc
    ):
        return jsonify({"error": "Invalid or expired link"}), 400

    updated = User.query.filter_by(id=user.id, email_verify_token=token, email_verified=False).update(
        {
            "email_verified": True,
            "email_verify_token": None,
            "email_verify_token_expires_at": None,
        },
        synchronize_session=False,
    )
    db.session.commit()
    if updated == 1:
        send_welcome_email(user)
    return jsonify({"message": "Email verified"}), 200


@user_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({"error": "Invalid token identity"}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(user.to_dict()), 200


@user_bp.route("/me", methods=["PATCH"])
@jwt_required()
def update_me():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({"error": "Invalid token identity"}), 401
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    data = request.get_json() or {}
    if "allergic_to_cinnamon" in data:
        val = data["allergic_to_cinnamon"]
        user.allergic_to_cinnamon = bool(val) if val is not None else None
    db.session.commit()
    return jsonify(user.to_dict()), 200
