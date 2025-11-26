from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from datetime import timedelta
from app.extensions import db
from app.models import User
from werkzeug.security import generate_password_hash, check_password_hash

main = Blueprint("main", __name__)

main.route("/signup", methods=["POST"])
def sign_up():
    data = request.get_json() or {}

    required = ["first_name", "last_name", "email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400
    
    if User.query.filter_by(email=data["email"]).first():
        return jsonify({"error": "Email already exists"}), 409

    try: # TODO do we need this try?
        hashed_password = generate_password_hash(data["password"])
        user = User(
            role_id=1,
            email=data["email"],
            password=hashed_password,
            first_name=data["first_name"],
            last_name=data["last_name"],
        )

        token = create_access_token(identity=user.id, expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201
    
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500

