from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from datetime import timedelta

main = Blueprint("main", __name__)

main.route("/signup", methods=["POST"])
def sign_up():
    data = request.get_json() or {}

    required = ["email", "password"]
    missing = [field for field in required if field not in data]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400
    
    #check if user already exists

    try: # TODO do we need this try?
        #create user

        token = create_access_token(identity=user.id, expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201
    
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500

