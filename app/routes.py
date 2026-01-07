from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from datetime import timedelta
from app.extensions import db
from app.models import User, ProductType, Product, ProductImage
from uuid import uuid4
import pathlib
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

@main.route('/me', methods=['GET'])
@jwt_required()
def me():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify(user.to_dict()), 200


@main.route("/product-types", methods=["GET"])
def get_product_types():
    pts = ProductType.query.order_by(ProductType.name).all()
    return jsonify([p.to_dict() for p in pts]), 200

def _make_s3_client():
    kwargs = {}
    kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
    kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")
    kwargs["region_name"] = os.getenv("AWS_REGION")

    return boto3.client("s3", **kwargs)

def _presign_key(key: str, expires: int = 3600) -> str:
    try:
        bucket = os.getenv('S3_BUCKET')
        if not bucket or not key:
            return None
        s3 = _make_s3_client()
        return s3.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=expires
        )
    except Exception:
        logger.exception('Failed to presign s3 key %s', key)
        return ''

@main.route('/products/create', methods=['POST'])
def create_product():
    form = request.form
    required = ['title', 'price', 'description', 'product_type_id', 'dimensions', 'color', 'note_of_cinnamon']
    missing = [f for f in required if not form.get(f)]
    if missing:
        return jsonify({'error': 'Missing fields', 'missing': missing}), 400

    try:
        product = Product(
            product_type_id=int(form.get('product_type_id')),
            title=form.get('title'),
            description=form.get('description'),
            price=form.get('price'),
            dimensions=form.get('dimensions'),
            color=form.get('color'),
            note_of_cinnamon=form.get('note_of_cinnamon'),
        )
        db.session.add(product)
        db.session.flush()  # assigns product.id

        s3 = _make_s3_client()
        bucket = os.getenv('S3_BUCKET')
        uploaded_keys = []

        files = request.files.getlist('images')
        for idx, f in enumerate(files):
            if not f:
                continue
            filename = secure_filename(f.filename or '')
            ext = pathlib.Path(filename).suffix or ''
            key = f"products/{product.id}/{uuid4().hex}{ext}"

            extra_args = {}
            if hasattr(f, 'mimetype') and f.mimetype:
                extra_args['ContentType'] = f.mimetype

            s3.upload_fileobj(f, bucket, key, ExtraArgs=extra_args or None)
            uploaded_keys.append(key)

            pi = ProductImage(product_id=product.id, s3_key=key, sort_order=idx)
            db.session.add(pi)
        db.session.commit()
        first_image = ProductImage.query.filter_by(product_id=product.id).order_by(ProductImage.sort_order).first()
        result = product.to_dict()
        if first_image:
            result['image_url'] = _presign_key(first_image.s3_key)

        return jsonify(result), 201

    except Exception as e:
        logger.exception('Failed to create product')
        db.session.rollback()
        try:
            s3 = _make_s3_client()
            bucket = os.getenv('S3_BUCKET')
            for key in uploaded_keys:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except Exception:
                    logger.exception('Failed to delete orphaned S3 key %s', key)
        except Exception:
            logger.exception('Failed during S3 cleanup')
        return jsonify({'error': 'Failed to create product'}), 500

@main.route('/products/<product_type>', methods=['GET'])
def get_products(product_type):
    if product_type == 'all':
        products = Product.query.where(Product.is_active == True).order_by(Product.created_at.desc()).all()
    else:
        products = (Product.query.join(ProductType).where(ProductType.name == product_type, Product.is_active == True).order_by(Product.created_at.desc()).all())
    out = []
    for p in products:
        pd = p.to_dict()
        first_image = ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.sort_order).first()
        pd['image_url'] = _presign_key(first_image.s3_key) if first_image else None
        out.append(pd)
    return jsonify(out), 200
