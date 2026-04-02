from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from datetime import timedelta, datetime, timezone
import json
from sqlalchemy import text
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import User, ProductType, Color, Product, ProductImage, Order, Cart, Banner, BannerPicture, FooterPicture, YourFavorite, OurFavorite
from app.utils.email import (
    send_password_reset_code_email,
    send_confirm_email,
    send_welcome_email,
    send_receipt_email,
    send_shipped_email,
    send_delivered_email,
)
from uuid import uuid4
import pathlib
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
import random
import boto3
import stripe
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

main = Blueprint("main", __name__)

def _generate_order_number():
    for _ in range(10):
        num = ''.join(str(random.randint(0, 9)) for _ in range(6))
        if not Order.query.filter_by(order_number=num).first():
            return num
    return str(random.randint(100000, 999999))


def _product_has_color_image(product_id: int, color_id: int) -> bool:
    if Color.query.get(color_id) is None:
        return False
    return (
        ProductImage.query.filter_by(product_id=product_id, color_id=color_id).first()
        is not None
    )


def _first_image_for_product_color(product_id: int, color_id: int):
    return (
        ProductImage.query.filter_by(product_id=product_id, color_id=color_id)
        .order_by(ProductImage.sort_order)
        .first()
    )


def _stripe_metadata_dict(session_obj):
    meta = getattr(session_obj, "metadata", None) or {}
    if hasattr(meta, "to_dict"):
        try:
            return dict(meta)
        except Exception:
            pass
    if isinstance(meta, dict):
        return meta
    try:
        return dict(meta)
    except Exception:
        return {}


def _order_image_url(order: Order):
    if not order.product_id or not order.color_id:
        return None
    img = _first_image_for_product_color(order.product_id, order.color_id)
    return _presign_key(img.s3_key) if img else None

def _is_admin():
    try:
        identity = get_jwt_identity()
        if not identity:
            return False
        user_id = int(identity)
        user = User.query.get(user_id)
        return user and user.role_id == 2
    except Exception:
        return False



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


@main.route("/forgot-password", methods=["POST"])
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


@main.route("/verify-reset-code", methods=["POST"])
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


@main.route("/reset-password", methods=["POST"])
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


@main.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token is required"}), 400
    user = User.query.filter_by(email_verify_token=token, email_verified=False).first()
    if not user:
        return jsonify({"error": "Invalid or expired link"}), 400
    if not user.email_verify_token_expires_at or user.email_verify_token_expires_at < datetime.now(timezone.utc):
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


@main.route('/me', methods=['PATCH'])
@jwt_required()
def update_me():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json() or {}
    if 'allergic_to_cinnamon' in data:
        val = data['allergic_to_cinnamon']
        user.allergic_to_cinnamon = bool(val) if val is not None else None
    db.session.commit()
    return jsonify(user.to_dict()), 200


# PRODUCTS
@main.route("/product-types", methods=["GET"])
def get_product_types():
    pts = ProductType.query.order_by(ProductType.name).all()
    return jsonify([p.to_dict() for p in pts]), 200


@main.route("/colors", methods=["GET"])
def get_colors():
    rows = Color.query.order_by(Color.id).all()
    return jsonify([c.to_dict() for c in rows]), 200


def _banner_pictures_response():
    pics = (
        BannerPicture.query.filter(BannerPicture.banner_index.in_([0, 1, 2]))
        .order_by(BannerPicture.banner_index)
        .all()
    )
    return [{"id": p.id, "s3_key": p.s3_key, "banner_index": p.banner_index, "image_url": _presign_key(p.s3_key)} for p in pics]


@main.route("/banner-pictures", methods=["GET"])
def get_banner_pictures():
    return jsonify(_banner_pictures_response()), 200


@main.route("/banner-pictures", methods=["PUT"])
@jwt_required()
def update_banner_pictures():
    if not _is_admin():
        return jsonify({"error": "Forbidden"}), 403
    form = request.form
    files = request.files
    s3 = _make_s3_client()
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return jsonify({"error": "S3 not configured"}), 500

    # Desired slot state: for each index 0,1,2 either (s3_key, from_upload) or (keep_id,) or None (clear)
    desired = {}
    for n in [0, 1, 2]:
        slot_file = files.get(f"slot_{n}_file")
        keep_id = form.get(f"slot_{n}_keep_id")
        if slot_file and slot_file.filename:
            filename = secure_filename(slot_file.filename or "")
            ext = pathlib.Path(filename).suffix or ""
            key = f"banner/{uuid4().hex}{ext}"
            extra = {}
            if getattr(slot_file, "mimetype", None):
                extra["ContentType"] = slot_file.mimetype
            try:
                s3.upload_fileobj(slot_file, bucket, key, ExtraArgs=extra or None)
            except Exception:
                logger.exception("Banner picture upload failed for slot %s", n)
                return jsonify({"error": "Upload failed"}), 500
            desired[n] = ("key", key)
        elif keep_id:
            try:
                desired[n] = ("id", int(keep_id))
            except (TypeError, ValueError):
                pass
        else:
            desired[n] = None

    try:
        # Remove all current slot assignments (delete or set to null)
        existing = BannerPicture.query.filter(BannerPicture.banner_index.in_([0, 1, 2])).all()
        for p in existing:
            p.banner_index = None
        db.session.flush()
        # Assign new state
        for n in [0, 1, 2]:
            d = desired.get(n)
            if d is None:
                continue
            if d[0] == "key":
                rec = BannerPicture(s3_key=d[1], banner_index=n)
                db.session.add(rec)
            else:
                rec = BannerPicture.query.get(d[1])
                if rec:
                    rec.banner_index = n
        db.session.commit()
        return jsonify(_banner_pictures_response()), 200
    except Exception:
        logger.exception("Failed to update banner pictures")
        db.session.rollback()
        return jsonify({"error": "Failed to update banner pictures"}), 500


def _footer_pictures_response():
    pics = (
        FooterPicture.query.filter(FooterPicture.footer_index.in_([0, 1]))
        .order_by(FooterPicture.footer_index)
        .all()
    )
    return [{"id": p.id, "s3_key": p.s3_key, "footer_index": p.footer_index, "image_url": _presign_key(p.s3_key)} for p in pics]


@main.route("/footer-pictures", methods=["GET"])
def get_footer_pictures():
    return jsonify(_footer_pictures_response()), 200


@main.route("/footer-pictures", methods=["PUT"])
@jwt_required()
def update_footer_pictures():
    if not _is_admin():
        return jsonify({"error": "Forbidden"}), 403
    form = request.form
    files = request.files
    s3 = _make_s3_client()
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return jsonify({"error": "S3 not configured"}), 500

    desired = {}
    for n in [0, 1]:
        slot_file = files.get(f"slot_{n}_file")
        keep_id = form.get(f"slot_{n}_keep_id")
        if slot_file and slot_file.filename:
            filename = secure_filename(slot_file.filename or "")
            ext = pathlib.Path(filename).suffix or ""
            key = f"footer/{uuid4().hex}{ext}"
            extra = {}
            if getattr(slot_file, "mimetype", None):
                extra["ContentType"] = slot_file.mimetype
            try:
                s3.upload_fileobj(slot_file, bucket, key, ExtraArgs=extra or None)
            except Exception:
                logger.exception("Footer picture upload failed for slot %s", n)
                return jsonify({"error": "Upload failed"}), 500
            desired[n] = ("key", key)
        elif keep_id:
            try:
                desired[n] = ("id", int(keep_id))
            except (TypeError, ValueError):
                pass
        else:
            desired[n] = None

    try:
        existing = FooterPicture.query.filter(FooterPicture.footer_index.in_([0, 1])).all()
        for p in existing:
            p.footer_index = None
        db.session.flush()
        for n in [0, 1]:
            d = desired.get(n)
            if d is None:
                continue
            if d[0] == "key":
                rec = FooterPicture(s3_key=d[1], footer_index=n)
                db.session.add(rec)
            else:
                rec = FooterPicture.query.get(d[1])
                if rec:
                    rec.footer_index = n
        db.session.commit()
        return jsonify(_footer_pictures_response()), 200
    except Exception:
        logger.exception("Failed to update footer pictures")
        db.session.rollback()
        return jsonify({"error": "Failed to update footer pictures"}), 500


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


def _primary_product_image_url(product_id: int):
    img = (
        ProductImage.query.filter_by(product_id=product_id)
        .order_by(ProductImage.sort_order.asc())
        .first()
    )
    return _presign_key(img.s3_key) if img else None


def _product_card_image_urls(product_id: int) -> list:
    images = (
        ProductImage.query.filter_by(product_id=product_id)
        .where(ProductImage.is_displayed == True)
        .order_by(ProductImage.sort_order)
        .all()
    )
    seen = set()
    out = []
    for img in images:
        if img.color_id in seen:
            continue
        seen.add(img.color_id)
        url = _presign_key(img.s3_key)
        if url:
            out.append(url)
    return out


def _product_card_image_color_ids(product_id: int) -> list:
    images = (
        ProductImage.query.filter_by(product_id=product_id)
        .order_by(ProductImage.sort_order)
        .all()
    )
    seen = set()
    out = []
    for img in images:
        if img.color_id in seen:
            continue
        seen.add(img.color_id)
        url = _presign_key(img.s3_key)
        if url:
            out.append(img.color_id)
    return out


def _product_with_images_payload(product: Product) -> dict:
    pd = product.to_dict()
    all_images = (
        ProductImage.query.filter_by(product_id=product.id)
        .order_by(ProductImage.sort_order)
        .all()
    )
    pd["image_urls"] = [_presign_key(img.s3_key) for img in all_images]
    pd["image_ids"] = [img.id for img in all_images]
    pd["image_color_ids"] = [img.color_id for img in all_images]
    uniq = sorted({img.color_id for img in all_images if img.color_id is not None})
    if uniq:
        rows = Color.query.filter(Color.id.in_(uniq)).all()
        name_by_id = {c.id: c.name for c in rows}
        hex_by_id = {c.id: c.hex for c in rows}
        pd["product_colors"] = [
            {"id": cid, "name": name_by_id.get(cid, "") or "", "hex": hex_by_id.get(cid, "") or ""}
            for cid in uniq
        ]
    else:
        pd["product_colors"] = []
    return pd


@main.route('/products/create', methods=['POST'])
@jwt_required()
def create_product():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    form = request.form
    required = ['title', 'price', 'description', 'product_type_id', 'dimensions']
    missing = [f for f in required if not form.get(f)]
    if missing:
        return jsonify({'error': 'Missing fields', 'missing': missing}), 400

    try:
        max_sort_order = db.session.query(db.func.max(Product.sort_order)).scalar()
        next_sort_order = (int(max_sort_order) + 1) if max_sort_order is not None else 0
        product = Product(
            product_type_id=int(form.get('product_type_id')),
            title=form.get('title'),
            description=form.get('description'),
            price=form.get('price'),
            dimensions=form.get('dimensions'),
            sort_order=next_sort_order,
        )
        db.session.add(product)
        db.session.flush()  # assigns product.id

        # create Stripe Product and Price and persist the price id
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
        try:
            unit_amount = int(float(product.price) * 100)
            stripe_prod = stripe.Product.create(name=product.title, description=product.description or '')
            stripe_price = stripe.Price.create(product=stripe_prod.id, unit_amount=unit_amount, currency='usd')
            product.stripe_price_id = stripe_price.id
            db.session.add(product)
        except Exception:
            logger.exception('Failed to create stripe product/price')
            raise

        s3 = _make_s3_client()
        bucket = os.getenv('S3_BUCKET')
        uploaded_keys = []

        files = [f for f in request.files.getlist('images') if f and f.filename]
        color_id_list = request.form.getlist('image_color_ids')
        is_displayed_list = request.form.getlist('image_is_displayed')
        if len(files) != len(color_id_list):
            return jsonify({'error': 'Each image requires a color', 'missing': 'image_color_ids'}), 400
        if is_displayed_list and len(files) != len(is_displayed_list):
            return jsonify({'error': 'Each image requires display setting', 'missing': 'image_is_displayed'}), 400
        try:
            parsed_color_ids = [int(x) for x in color_id_list]
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid image_color_ids'}), 400
        if is_displayed_list:
            parsed_is_displayed = [str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'on') for v in is_displayed_list]
        else:
            parsed_is_displayed = [True for _ in files]
        if files:
            valid_color_ids = {
                r[0] for r in db.session.query(Color.id).filter(Color.id.in_(parsed_color_ids)).all()
            }
            if not all(cid in valid_color_ids for cid in parsed_color_ids):
                return jsonify({'error': 'Invalid color id'}), 400

        for idx, f in enumerate(files):
            filename = secure_filename(f.filename or '')
            ext = pathlib.Path(filename).suffix or ''
            key = f"products/{product.id}/{uuid4().hex}{ext}"

            extra_args = {}
            if hasattr(f, 'mimetype') and f.mimetype:
                extra_args['ContentType'] = f.mimetype

            s3.upload_fileobj(f, bucket, key, ExtraArgs=extra_args or None)
            uploaded_keys.append(key)

            pi = ProductImage(
                product_id=product.id,
                s3_key=key,
                sort_order=idx,
                color_id=parsed_color_ids[idx],
                is_displayed=parsed_is_displayed[idx],
            )
            db.session.add(pi)
        db.session.commit()
        result = product.to_dict()
        result['image_urls'] = _product_card_image_urls(product.id)
        result['image_url'] = result['image_urls'][0] if result['image_urls'] else None

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

@main.route('/your-favorites', methods=['GET'])
def get_your_favorites():
    rows = (
        YourFavorite.query.join(Product)
        .filter(Product.is_active == True)
        .order_by(YourFavorite.sort_order.asc())
        .all()
    )
    out = []
    for row in rows:
        p = Product.query.options(joinedload(Product.product_type)).get(row.product_id)
        if not p:
            continue
        pd = p.to_dict()
        pd['product_type_name'] = p.product_type.name if p.product_type else None
        primary = _primary_product_image_url(p.id)
        pd['image_url'] = primary
        pd['image_urls'] = [primary] if primary else []
        out.append(pd)
    return jsonify(out), 200


@main.route('/our-favorites', methods=['GET'])
def get_our_favorites():
    rows = (
        OurFavorite.query.join(Product)
        .filter(Product.is_active == True)
        .order_by(OurFavorite.sort_order.asc())
        .all()
    )
    out = []
    for row in rows:
        p = Product.query.options(joinedload(Product.product_type)).get(row.product_id)
        if not p:
            continue
        pd = p.to_dict()
        pd['product_type_name'] = p.product_type.name if p.product_type else None
        primary = _primary_product_image_url(p.id)
        pd['image_url'] = primary
        pd['image_urls'] = [primary] if primary else []
        out.append(pd)
    return jsonify(out), 200


@main.route('/products', methods=['GET'])
def get_products():
    products = (
        Product.query.options(joinedload(Product.product_type))
        .where(Product.is_active == True)
        .order_by(Product.sort_order.asc())
        .all()
    )
    out = []
    for p in products:
        pd = p.to_dict()
        pd['product_type_name'] = p.product_type.name if p.product_type else None
        pd['image_urls'] = _product_card_image_urls(p.id)
        pd['image_color_ids'] = _product_card_image_color_ids(p.id)
        pd['image_url'] = pd['image_urls'][0] if pd['image_urls'] else None
        out.append(pd)
    return jsonify(out), 200


@main.route('/products/sort', methods=['GET'])
@jwt_required()
def get_products_sort():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = db.session.execute(
        text("SELECT id, title, sort_order FROM products ORDER BY sort_order ASC, id ASC")
    ).mappings().all()
    return jsonify({'products': [dict(r) for r in rows]}), 200


@main.route('/products/sort', methods=['PUT'])
@jwt_required()
def put_products_sort():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or []
    if not isinstance(data, list):
        return jsonify({'error': 'Body must be an array'}), 400
    if not data:
        return jsonify({'error': 'Body cannot be empty'}), 400
    updates = []
    seen_ids = set()
    seen_orders = set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            return jsonify({'error': f'Invalid row at index {i}'}), 400
        try:
            pid = int(row.get('product_id'))
            sort_order = int(row.get('sort_order'))
        except (TypeError, ValueError):
            return jsonify({'error': f'Invalid product_id/sort_order at index {i}'}), 400
        if sort_order < 0:
            return jsonify({'error': 'sort_order must be >= 0'}), 400
        if pid in seen_ids:
            return jsonify({'error': f'Duplicate product_id {pid}'}), 400
        if sort_order in seen_orders:
            return jsonify({'error': f'Duplicate sort_order {sort_order}'}), 400
        seen_ids.add(pid)
        seen_orders.add(sort_order)
        updates.append({'product_id': pid, 'sort_order': sort_order})
    ids_in_db = db.session.execute(text("SELECT id FROM products")).scalars().all()
    ids_set = set(int(v) for v in ids_in_db)
    if ids_set != seen_ids:
        return jsonify({'error': 'Body must include every product exactly once'}), 400
    expected_orders = set(range(len(ids_set)))
    if expected_orders != seen_orders:
        return jsonify({'error': f'sort_order values must be exactly 0..{len(ids_set) - 1}'}), 400
    try:
        for row in updates:
            db.session.execute(
                text("UPDATE products SET sort_order = :sort_order WHERE id = :product_id"),
                {'sort_order': row['sort_order'], 'product_id': row['product_id']},
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('Failed to update products sort order')
        return jsonify({'error': 'Failed to save product order'}), 500
    rows = db.session.execute(
        text("SELECT id, title, sort_order FROM products ORDER BY sort_order ASC, id ASC")
    ).mappings().all()
    return jsonify({'products': [dict(r) for r in rows]}), 200


@main.route('/admin/products/inactive', methods=['GET'])
@jwt_required()
def admin_get_inactive_products():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    products = (
        Product.query.options(joinedload(Product.product_type))
        .where(Product.is_active == False)
        .order_by(Product.created_at.desc())
        .all()
    )
    out = []
    for p in products:
        pd = p.to_dict()
        first_image = ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.sort_order).first()
        pd['image_url'] = _presign_key(first_image.s3_key) if first_image else None
        out.append(pd)
    return jsonify(out), 200

@main.route('/product/<int:product_id>', methods=['GET'])
def get_product(product_id):
    product = Product.query.get_or_404(product_id)
    return jsonify(_product_with_images_payload(product)), 200

@main.route('/product/<int:product_id>', methods=['PATCH'])
@jwt_required()
def update_product(product_id):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    product = Product.query.get_or_404(product_id)
    data = request.get_json() or {}
    if 'title' in data and data['title'] is not None:
        product.title = str(data['title']).strip()[:200]
    if 'description' in data:
        product.description = str(data['description']).strip() if data['description'] else None
    if 'price' in data and data['price'] is not None:
        try:
            product.price = float(data['price'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid price'}), 400
    if 'dimensions' in data and data['dimensions'] is not None:
        product.dimensions = str(data['dimensions']).strip() or None
    if 'color' in data and data['color'] is not None:
        product.color = str(data['color']).strip()[:50]
    if 'is_active' in data:
        product.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify(_product_with_images_payload(product)), 200

@main.route('/product/<int:product_id>/images/order', methods=['PUT'])
@jwt_required()
def reorder_product_images(product_id):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    product = Product.query.get_or_404(product_id)
    data = request.get_json() or {}
    order = data.get('order')
    if not isinstance(order, list) or len(order) == 0:
        return jsonify({'error': 'order must be a non-empty list of image ids'}), 400
    try:
        order = [int(x) for x in order]
    except (TypeError, ValueError):
        return jsonify({'error': 'order must be integers'}), 400
    images = ProductImage.query.filter_by(product_id=product.id).all()
    id_to_img = {img.id: img for img in images}
    if set(order) != set(id_to_img.keys()):
        return jsonify({'error': 'order must contain exactly the same image ids as the product'}), 400
    for idx, img_id in enumerate(order):
        id_to_img[img_id].sort_order = idx
    db.session.commit()
    return jsonify(_product_with_images_payload(product)), 200

@main.route('/product/<int:product_id>/images/<int:image_id>', methods=['DELETE'])
@jwt_required()
def delete_product_image(product_id, image_id):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    product = Product.query.get_or_404(product_id)
    img = ProductImage.query.filter_by(id=image_id, product_id=product.id).first()
    if not img:
        return jsonify({'error': 'Image not found'}), 404
    db.session.delete(img)
    db.session.commit()
    return jsonify(_product_with_images_payload(product)), 200

@main.route('/create-cart-checkout-session', methods=['POST'])
@jwt_required(optional=True)
def create_cart_checkout_session():
    data = request.get_json() or {}
    raw_items = data.get('items')
    logger.info('create-cart-checkout-session items=%s', raw_items)
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({'error': 'items must be a non-empty list'}), 400

    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
    line_items = []
    item_colors = []
    for it in raw_items:
        try:
            product_id = int(it.get('product_id'))
            quantity = int(it.get('quantity', 1))
            color_id = int(it.get('color_id'))
        except (TypeError, ValueError):
            logger.warning('create-cart-checkout-session invalid item: %s', it)
            return jsonify({'error': 'Each item must have product_id, quantity, and color_id'}), 400
        if quantity < 1:
            continue
        product = Product.query.get(product_id)
        if not product or not product.stripe_price_id:
            logger.warning('create-cart-checkout-session product %s missing or no stripe_price_id', product_id)
            return jsonify({'error': f'Product {product_id} not found or has no Stripe price'}), 400
        if not _product_has_color_image(product_id, color_id):
            return jsonify({'error': f'Invalid color_id {color_id} for product {product_id}'}), 400
        line_items.append({'price': product.stripe_price_id, 'quantity': quantity})
        item_colors.append({'color_id': color_id})

    if not line_items:
        return jsonify({'error': 'No valid line items'}), 400

    try:
        user_id = None
        try:
            identity = get_jwt_identity()
            user_id = int(identity) if identity else None
        except Exception:
            pass
        customer_email = None
        if user_id:
            try:
                user = User.query.get(user_id)
                customer_email = (user.email if user and user.email else None)
            except Exception:
                customer_email = None

        success_base = os.getenv('STRIPE_SUCCESS_URL').rstrip('/')
        order_number = _generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        cancel_url = os.getenv('STRIPE_CANCEL_CART_URL').rstrip('/')
        allergic = data.get('allergic_to_cinnamon')
        meta = {
            'order_number': order_number,
            'user_id': str(user_id) if user_id else '',
            'item_colors': json.dumps(item_colors),
        }
        if allergic is not None:
            meta['allergic_to_cinnamon'] = 'true' if allergic else 'false'
        session_kwargs = dict(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
            shipping_address_collection={'allowed_countries': ['US']},
            automatic_tax={'enabled': True},
        )
        if customer_email:
            session_kwargs['customer_email'] = customer_email
        if user_id:
            session_kwargs['client_reference_id'] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({'id': session.id, 'url': session.url}), 200
    except Exception:
        logger.exception('Stripe cart checkout session creation failed')
        return jsonify({'error': 'Failed to create checkout session'}), 500

@main.route('/create-checkout-session/<price_id>', methods=['POST'])
@jwt_required(optional=True)
def create_checkout_session(price_id):
    data = request.get_json() or {}
    try:
        quantity = int(data.get('quantity', 1))
        color_id = int(data.get('color_id'))
    except Exception:
        return jsonify({'error': 'Invalid quantity or missing color_id'}), 400

    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

    product_row = Product.query.filter_by(stripe_price_id=price_id).first()
    if not product_row:
        return jsonify({'error': 'Product not found for price'}), 400
    if not _product_has_color_image(product_row.id, color_id):
        return jsonify({'error': 'Invalid color_id for this product'}), 400

    line_items = [{'price': price_id, 'quantity': quantity}]

    try:
        user_identity = get_jwt_identity()
        try:
            user_id = int(user_identity) if user_identity is not None else None
        except Exception:
            user_id = None
    except Exception:
        user_id = None
    customer_email = None
    if user_id:
        try:
            user = User.query.get(user_id)
            customer_email = (user.email if user and user.email else None)
        except Exception:
            customer_email = None

    try:
        success_base = os.getenv('STRIPE_SUCCESS_URL').rstrip('/')
        order_number = _generate_order_number()
        success_url = f"{success_base}/orders/{order_number}"
        cancel_url = os.getenv('STRIPE_CANCEL_URL') + str(product_row.id)
        allergic = data.get('allergic_to_cinnamon')
        meta = {
            'order_number': order_number,
            'user_id': str(user_id) if user_id else '',
            'color_id': str(color_id),
        }
        if allergic is not None:
            meta['allergic_to_cinnamon'] = 'true' if allergic else 'false'
        session_kwargs = dict(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=meta,
            shipping_address_collection={'allowed_countries': ['US']},
            automatic_tax={'enabled': True},
        )
        if customer_email:
            session_kwargs['customer_email'] = customer_email
        if user_id:
            session_kwargs['client_reference_id'] = str(user_id)

        session = stripe.checkout.Session.create(**session_kwargs)
        return jsonify({'id': session.id, 'url': session.url, 'price_id': price_id}), 200
    except Exception:
        logger.exception('Stripe checkout session creation failed')
        return jsonify({'error': 'Failed to create checkout session'}), 500

@main.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except ValueError:
            logger.exception('Invalid payload')
            return '', 400
        except stripe.error.SignatureVerificationError:
            logger.exception('Invalid signature')
            return '', 400

    event_type = event.get('type')
    logger.info('Received Stripe event: %s', event_type)

    if event_type == 'checkout.session.completed':
        session = event['data']['object']
        logger.info('Checkout session completed: %s', session.get('id'))
        try:
            stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
            session_obj = stripe.checkout.Session.retrieve(
                session.get('id'), expand=['line_items', 'payment_intent', 'customer_details']
            )
        except Exception:
            logger.exception('Failed to retrieve full session from Stripe')
            return '', 200

        try:
            existing = Order.query.filter_by(session_id=session_obj.id).first()
            if existing:
                logger.info('Order already exists for session %s (order id=%s)', session_obj.id, existing.id)
            else:
                line_items_obj = getattr(session_obj, 'line_items', None)
                line_items_data = getattr(line_items_obj, 'data', None) if line_items_obj else None
                if line_items_data is None and line_items_obj is not None and hasattr(line_items_obj, 'get'):
                    line_items_data = line_items_obj.get('data', [])
                line_items_list = list(line_items_data) if line_items_data else []
                if not line_items_list:
                    logger.warning('No line_items in session %s (expanded: %s)', session_obj.id, bool(line_items_obj))
                payment_intent = getattr(session_obj, 'payment_intent', None) or session_obj.get('payment_intent')
                payment_intent_id = getattr(payment_intent, 'id', None) if payment_intent else None
                customer_email = None
                if getattr(session_obj, 'customer_details', None):
                    try:
                        customer_email = session_obj.customer_details.email
                    except Exception:
                        customer_email = session_obj.get('customer_details', {}).get('email')
                shipping_address_raw = '{}'
                try:
                    collected_information = getattr(session_obj, 'collected_information', None) or session_obj.get('collected_information', {})
                    if hasattr(collected_information, 'to_dict'):
                        collected_information = collected_information.to_dict()
                    shipping_details = collected_information.get('shipping_details', {}) if isinstance(collected_information, dict) else {}
                    if hasattr(shipping_details, 'to_dict'):
                        shipping_details = shipping_details.to_dict()
                    shipping_address_payload = shipping_details.get('address', {}) if isinstance(shipping_details, dict) else {}
                    if hasattr(shipping_address_payload, 'to_dict'):
                        shipping_address_payload = shipping_address_payload.to_dict()
                    if isinstance(shipping_address_payload, dict):
                        shipping_address_raw = json.dumps(shipping_address_payload)
                except Exception:
                    shipping_address_raw = '{}'
                try:
                    user_id_val = None
                    order_number = None
                    allergic_to_cinnamon_meta = None
                    if getattr(session_obj, 'metadata', None):
                        try:
                            meta = session_obj.metadata
                            user_id_val = meta.get('user_id') if hasattr(meta, 'get') else getattr(meta, 'user_id', None)
                            order_number = meta.get('order_number') if hasattr(meta, 'get') else getattr(meta, 'order_number', None)
                            ac = meta.get('allergic_to_cinnamon') if hasattr(meta, 'get') else getattr(meta, 'allergic_to_cinnamon', None)
                            allergic_to_cinnamon_meta = ac
                        except Exception:
                            user_id_val = session_obj.get('metadata', {}).get('user_id')
                            order_number = session_obj.get('metadata', {}).get('order_number')
                            allergic_to_cinnamon_meta = session_obj.get('metadata', {}).get('allergic_to_cinnamon')
                    if not user_id_val and getattr(session_obj, 'client_reference_id', None):
                        user_id_val = session_obj.client_reference_id
                    if user_id_val is not None:
                        try:
                            user_id_val = int(user_id_val)
                        except Exception:
                            user_id_val = None
                    if not order_number:
                        order_number = _generate_order_number()
                    try:
                        allergic_to_cinnamon_order = allergic_to_cinnamon_meta == 'true' if allergic_to_cinnamon_meta else None
                    except Exception:
                        allergic_to_cinnamon_order = None
                except Exception:
                    user_id_val = None
                    order_number = _generate_order_number()
                    allergic_to_cinnamon_order = None

                meta_flat = _stripe_metadata_dict(session_obj)
                item_colors_raw = meta_flat.get('item_colors')
                item_colors_list = []
                if item_colors_raw:
                    try:
                        item_colors_list = json.loads(item_colors_raw)
                    except (json.JSONDecodeError, TypeError):
                        item_colors_list = []
                single_color_meta = meta_flat.get('color_id')

                rows_to_process = line_items_list if line_items_list else []
                for idx, item in enumerate(rows_to_process):
                    price_obj = item.get('price') if isinstance(item, dict) else getattr(item, 'price', None)
                    if isinstance(price_obj, dict):
                        stripe_price_id = price_obj.get('id')
                    elif price_obj is not None:
                        stripe_price_id = getattr(price_obj, 'id', None) or (price_obj if isinstance(price_obj, str) else None)
                    else:
                        stripe_price_id = item.get('price') if isinstance(item, dict) else None
                    quantity = int(item.get('quantity', 1)) if isinstance(item, dict) else int(getattr(item, 'quantity', 1))
                    amount_cents = int(item.get('amount_total', 0) or 0) if isinstance(item, dict) else int(getattr(item, 'amount_total', 0) or 0)

                    product = Product.query.filter_by(stripe_price_id=stripe_price_id).first() if stripe_price_id else None
                    product_id = product.id if product else None

                    color_id_order = 1
                    if item_colors_list and idx < len(item_colors_list):
                        try:
                            color_id_order = int(item_colors_list[idx].get('color_id'))
                        except (TypeError, ValueError):
                            color_id_order = 1
                    elif single_color_meta is not None and len(rows_to_process) == 1:
                        try:
                            color_id_order = int(single_color_meta)
                        except (TypeError, ValueError):
                            color_id_order = 1
                    if product_id and not _product_has_color_image(product_id, color_id_order):
                        logger.warning(
                            'Stripe webhook: color_id %s invalid for product %s, using 1',
                            color_id_order,
                            product_id,
                        )
                        color_id_order = 1

                    order = Order(
                        user_id=user_id_val,
                        product_id=product_id,
                        color_id=color_id_order,
                        session_id=session_obj.id,
                        order_number=order_number,
                        payment_intent_id=payment_intent_id,
                        stripe_price_id=stripe_price_id,
                        quantity=quantity,
                        amount_cents=amount_cents,
                        status='Ordered',
                        customer_email=customer_email,
                        shipping_address=shipping_address_raw,
                        paid_at=(datetime.utcnow() if getattr(session_obj, 'payment_status', None) == 'paid' else None),
                        allergic_to_cinnamon=allergic_to_cinnamon_order,
                    )
                    db.session.add(order)
                db.session.commit()
                logger.info('Created %s order(s) for session %s', len(rows_to_process), session_obj.id)
                if customer_email:
                    orders_for_receipt = Order.query.filter_by(session_id=session_obj.id).order_by(Order.id).all()
                    if orders_for_receipt:
                        first = orders_for_receipt[0]
                        order_date = first.created_at.strftime('%B %d, %Y') if first.created_at else ''
                        product_parts = []
                        total_cents = 0
                        for o in orders_for_receipt:
                            title = o.product.title if o.product else 'Item'
                            qty = o.quantity or 1
                            product_parts.append(f"{title} × {qty}")
                            total_cents += o.amount_cents or 0
                        product_names = ', '.join(product_parts)
                        total_formatted = f"${total_cents / 100:.2f}"
                        send_receipt_email(
                            customer_email,
                            order_date,
                            first.order_number,
                            product_names,
                            total_formatted,
                        )
                if user_id_val is not None:
                    cart = Cart.query.filter_by(user_id=user_id_val).first()
                    if cart:
                        cart.items = []
                        db.session.commit()
                        logger.info('Cleared cart for user %s after checkout', user_id_val)
        except Exception:
            logger.exception('Error creating order for session %s', session.get('id'))
            try:
                db.session.rollback()
            except Exception:
                logger.exception('Rollback failed')

    return '', 200

@main.route('/orders', methods=['GET'])
@jwt_required()
def get_orders():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    orders = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).all()

    out = [_order_to_dict_with_product(o) for o in orders]

    return jsonify(out), 200

@main.route('/orders/<order_number>', methods=['GET'])
def get_order_by_number(order_number):
    orders = Order.query.filter_by(order_number=order_number).order_by(Order.id).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404

    out = [_order_to_dict_with_product(o) for o in orders]

    return jsonify({'order_number': order_number, 'orders': out}), 200

def _order_to_dict_with_product(o):
    od = o.to_dict()
    if o.product:
        od['product_title'] = o.product.title
    else:
        od['product_title'] = None
    c = Color.query.get(o.color_id) if getattr(o, 'color_id', None) else None
    od['color_name'] = c.name if c else None
    od['color_hex'] = c.hex if c else None
    od['image_url'] = _order_image_url(o)
    if o.user_id:
        u = User.query.get(o.user_id)
        if u:
            od['user_first_name'] = u.first_name
            od['user_last_name'] = u.last_name
            od['user_email'] = u.email
    return od

@main.route('/admin/orders', methods=['GET'])
@jwt_required()
def admin_list_orders():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    page = max(1, int(request.args.get('page', 1)))
    per_page = max(1, min(50, int(request.args.get('per_page', 10))))
    subq = db.session.query(Order.order_number, db.func.min(Order.created_at).label('created_at')).filter(
        Order.order_number.isnot(None)
    ).group_by(Order.order_number).subquery()
    total = db.session.query(db.func.count()).select_from(subq).scalar() or 0
    order_numbers = db.session.query(subq.c.order_number).order_by(subq.c.created_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    order_numbers = [r[0] for r in order_numbers]
    orders_by_number = {}
    for onum in order_numbers:
        rows = Order.query.filter_by(order_number=onum).order_by(Order.id).all()
        orders_by_number[onum] = [_order_to_dict_with_product(o) for o in rows]
    return jsonify({
        'orders_by_number': orders_by_number,
        'total': total,
        'page': page,
        'per_page': per_page,
    }), 200

@main.route('/admin/orders/<order_number>', methods=['GET'])
@jwt_required()
def admin_get_order(order_number):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    orders = Order.query.filter_by(order_number=order_number).order_by(Order.id).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404
    out = [_order_to_dict_with_product(o) for o in orders]
    return jsonify({'order_number': order_number, 'orders': out}), 200

@main.route('/admin/orders/<order_number>', methods=['PATCH'])
@jwt_required()
def admin_update_order(order_number):
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    orders = Order.query.filter_by(order_number=order_number).all()
    if not orders:
        return jsonify({'error': 'Order not found'}), 404
    if 'status' in data and data['status'] in ('Ordered', 'Shipped', 'Delivered'):
        for o in orders:
            o.status = data['status']
    if 'tracking_url' in data:
        val = data['tracking_url']
        val = str(val).strip() if val else None
        for o in orders:
            o.tracking_url = val
    if 'comments' in data:
        val = data['comments']
        if isinstance(val, list):
            raw = json.dumps([str(x).strip() for x in val if str(x).strip()])
        else:
            raw = json.dumps([str(val).strip()]) if val and str(val).strip() else None
        for o in orders:
            o.comments = raw
    db.session.commit()
    new_status = data.get('status')
    recipient = orders[0].customer_email if orders else None
    if recipient and new_status == 'Shipped':
        send_shipped_email(recipient, order_number, orders[0].tracking_url)
    elif recipient and new_status == 'Delivered':
        delivery_date = datetime.now(timezone.utc).strftime('%B %d, %Y')
        send_delivered_email(recipient, order_number, delivery_date)
    out = [_order_to_dict_with_product(o) for o in orders]
    return jsonify({'order_number': order_number, 'orders': out}), 200

BANNER_COLORS = ('primary', 'primary_dark', 'secondary', 'secondary_dark')

@main.route('/banner', methods=['GET'])
def get_banner():
    """Public: return the most recent banner only if it is active, else null."""
    banner = Banner.query.order_by(Banner.created_at.desc()).first()
    if not banner or not banner.is_active:
        return jsonify({'banner': None}), 200
    return jsonify({'banner': banner.to_dict()}), 200

@main.route('/admin/banner', methods=['POST'])
@jwt_required()
def admin_create_banner():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    is_active = bool(data.get('is_active', True))
    text = (data.get('text') or '').strip()[:500]
    background_color = (data.get('background_color') or 'primary').strip()
    if background_color not in BANNER_COLORS:
        background_color = 'primary'
    for b in Banner.query.all():
        b.is_active = False
    banner = Banner(is_active=is_active, text=text, background_color=background_color)
    db.session.add(banner)
    db.session.commit()
    return jsonify(banner.to_dict()), 201


@main.route('/admin/your-favorites', methods=['GET'])
@jwt_required()
def admin_get_your_favorites():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = YourFavorite.query.order_by(YourFavorite.sort_order.asc()).all()
    products = (
        Product.query.filter_by(is_active=True)
        .order_by(Product.title.asc())
        .all()
    )
    return jsonify({
        'favorites': [r.to_dict() for r in rows],
        'products': [{'id': p.id, 'title': p.title} for p in products],
    }), 200


@main.route('/admin/your-favorites', methods=['PUT'])
@jwt_required()
def admin_put_your_favorites():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    slots = data.get('slots')
    if not isinstance(slots, list):
        return jsonify({'error': 'slots must be an array'}), 400
    active_products = Product.query.filter_by(is_active=True).all()
    max_slots = len(active_products)
    active_ids = {p.id for p in active_products}
    if len(slots) > max_slots:
        return jsonify({'error': f'slots may have at most {max_slots} entries'}), 400
    for i, val in enumerate(slots):
        if val is None:
            continue
        try:
            pid = int(val)
        except (TypeError, ValueError):
            return jsonify({'error': f'Invalid product_id at index {i}'}), 400
        if pid not in active_ids:
            return jsonify({'error': f'Product {pid} is not active'}), 400

    YourFavorite.query.delete()
    for i, val in enumerate(slots):
        if val is None:
            continue
        db.session.add(YourFavorite(product_id=int(val), sort_order=i))
    db.session.commit()
    rows = YourFavorite.query.order_by(YourFavorite.sort_order.asc()).all()
    return jsonify({'favorites': [r.to_dict() for r in rows]}), 200


@main.route('/admin/our-favorites', methods=['GET'])
@jwt_required()
def admin_get_our_favorites():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    rows = OurFavorite.query.order_by(OurFavorite.sort_order.asc()).all()
    products = (
        Product.query.filter_by(is_active=True)
        .order_by(Product.title.asc())
        .all()
    )
    return jsonify({
        'favorites': [r.to_dict() for r in rows],
        'products': [{'id': p.id, 'title': p.title} for p in products],
    }), 200


@main.route('/admin/our-favorites', methods=['PUT'])
@jwt_required()
def admin_put_our_favorites():
    if not _is_admin():
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    slots = data.get('slots')
    if not isinstance(slots, list):
        return jsonify({'error': 'slots must be an array'}), 400
    active_products = Product.query.filter_by(is_active=True).all()
    max_slots = len(active_products)
    active_ids = {p.id for p in active_products}
    if len(slots) > max_slots:
        return jsonify({'error': f'slots may have at most {max_slots} entries'}), 400
    for i, val in enumerate(slots):
        if val is None:
            continue
        try:
            pid = int(val)
        except (TypeError, ValueError):
            return jsonify({'error': f'Invalid product_id at index {i}'}), 400
        if pid not in active_ids:
            return jsonify({'error': f'Product {pid} is not active'}), 400

    OurFavorite.query.delete()
    for i, val in enumerate(slots):
        if val is None:
            continue
        db.session.add(OurFavorite(product_id=int(val), sort_order=i))
    db.session.commit()
    rows = OurFavorite.query.order_by(OurFavorite.sort_order.asc()).all()
    return jsonify({'favorites': [r.to_dict() for r in rows]}), 200


@main.route('/sync-cart', methods=['POST'])
@jwt_required()
def sync_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401

    data = request.get_json() or {}
    items = data.get('items')
    if not isinstance(items, list):
        return jsonify({'error': 'items must be a list'}), 400

    cart = Cart.query.filter_by(user_id=user_id).first()
    if not cart:
        cart = Cart(user_id=user_id, items=items)
        db.session.add(cart)
    else:
        cart.items = items
        cart.updated_at = db.func.now()
    db.session.commit()
    return jsonify(cart.to_dict()), 200

@main.route('/cart', methods=['GET'])
@jwt_required()
def get_cart():
    identity = get_jwt_identity()
    try:
        user_id = int(identity)
    except Exception:
        return jsonify({'error': 'Invalid token identity'}), 401
    cart = Cart.query.filter_by(user_id=user_id).first()
    return jsonify(cart.to_dict() if cart else {'items': []}), 200

