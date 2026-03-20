from app.models import User
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime, timezone
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from app.utils.email import send_confirm_email
from app.extensions import db
from uuid import uuid4
import logging
import os

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

        frontend_url = (os.getenv("CLIENT_URL") or os.getenv("FRONTEND_URL") or "http://localhost:3000").rstrip("/")
        verify_url = f"{frontend_url}/verify-email?token={verify_token}"
        send_confirm_email(user, verify_url)

        token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201

    except Exception as e:
        logger.exception("Create account failed: %s", e)
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500