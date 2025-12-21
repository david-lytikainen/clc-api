from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from datetime import timedelta
from app.extensions import db
from app.models import User
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

main = Blueprint("main", __name__)

@main.route("/create-account", methods=["POST"])
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
        user = User(
            role_id=1,
            email=data["email"],
            password=hashed_password,
            first_name=data["first_name"],
            last_name=data["last_name"],
        )
        db.session.add(user)
        db.session.commit()

        token = create_access_token(identity=user.id, expires_delta=timedelta(days=1))
        return jsonify({"token": token, "user": user.to_dict()}), 201
    
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to create user"}), 500

@main.route("/sign-in", methods=["POST"])
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


@main.route("/product/<product_id>/image")
def get_product_pdf(product_id):
    s3_key = f"test{product_id}.png"

    bucket = os.getenv("S3_BUCKET")
    s3 = _make_s3_client()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": s3_key},
            ExpiresIn=60,
        )
        return jsonify({"url": url, "expires_in": 60})
    except (BotoCoreError, ClientError):
        return jsonify({"error": "Failed to generate presigned url"}), 500


@main.route("/upload", methods=["POST"])
def upload_file():
    file = request.files['file']
    filename = secure_filename(file.filename)
    s3_key = filename
    bucket = os.getenv('S3_BUCKET')
    s3 = _make_s3_client()

    extra_args = {}
    if hasattr(file, 'mimetype') and file.mimetype:
        extra_args['ContentType'] = file.mimetype

    s3.upload_fileobj(file, bucket, s3_key, ExtraArgs=extra_args or None)

    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': s3_key},
        ExpiresIn=3600,
    )
    return jsonify({'url': url}), 201

def _make_s3_client():
    kwargs = {}
    kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
    kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")
    kwargs["region_name"] = os.getenv("AWS_REGION")

    return boto3.client("s3", **kwargs)
