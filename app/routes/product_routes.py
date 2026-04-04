import logging
import os
import pathlib
from uuid import uuid4

import stripe
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import text
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Color, OurFavorite, Product, ProductImage, ProductType, YourFavorite
from app.routes.common import (
    is_admin,
    make_s3_client,
    primary_product_image_url,
    product_card_image_color_ids,
    product_card_image_urls,
    product_with_images_payload,
)

logger = logging.getLogger(__name__)

product_bp = Blueprint("product", __name__)


@product_bp.route("/product-types", methods=["GET"])
def get_product_types():
    pts = ProductType.query.order_by(ProductType.name).all()
    return jsonify([p.to_dict() for p in pts]), 200


@product_bp.route("/colors", methods=["GET"])
def get_colors():
    rows = Color.query.order_by(Color.id).all()
    return jsonify([c.to_dict() for c in rows]), 200


@product_bp.route("/products/create", methods=["POST"])
@jwt_required()
def create_product():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    form = request.form
    required = ["title", "price", "description", "product_type_id", "dimensions", "lead_time"]
    missing = [f for f in required if not form.get(f)]
    if missing:
        return jsonify({"error": "Missing fields", "missing": missing}), 400

    uploaded_keys = []
    try:
        max_sort_order = db.session.query(db.func.max(Product.sort_order)).scalar()
        next_sort_order = (int(max_sort_order) + 1) if max_sort_order is not None else 0
        product = Product(
            product_type_id=int(form.get("product_type_id")),
            title=form.get("title"),
            description=form.get("description"),
            price=form.get("price"),
            dimensions=form.get("dimensions"),
            lead_time=form.get("lead_time"),
            sort_order=next_sort_order,
        )
        db.session.add(product)
        db.session.flush()

        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        try:
            unit_amount = int(float(product.price) * 100)
            stripe_prod = stripe.Product.create(name=product.title, description=product.description or "")
            stripe_price = stripe.Price.create(product=stripe_prod.id, unit_amount=unit_amount, currency="usd")
            product.stripe_price_id = stripe_price.id
            db.session.add(product)
        except Exception:
            logger.exception("Failed to create stripe product/price")
            raise

        s3 = make_s3_client()
        bucket = os.getenv("S3_BUCKET")

        files = [f for f in request.files.getlist("images") if f and f.filename]
        color_id_list = request.form.getlist("image_color_ids")
        is_displayed_list = request.form.getlist("image_is_displayed")
        if len(files) != len(color_id_list):
            return jsonify({"error": "Each image requires a color", "missing": "image_color_ids"}), 400
        if is_displayed_list and len(files) != len(is_displayed_list):
            return jsonify({"error": "Each image requires display setting", "missing": "image_is_displayed"}), 400
        try:
            parsed_color_ids = [int(x) for x in color_id_list]
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid image_color_ids"}), 400
        if is_displayed_list:
            parsed_is_displayed = [
                str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on") for v in is_displayed_list
            ]
        else:
            parsed_is_displayed = [True for _ in files]
        if files:
            valid_color_ids = {
                r[0] for r in db.session.query(Color.id).filter(Color.id.in_(parsed_color_ids)).all()
            }
            if not all(cid in valid_color_ids for cid in parsed_color_ids):
                return jsonify({"error": "Invalid color id"}), 400

        for idx, f in enumerate(files):
            filename = secure_filename(f.filename or "")
            ext = pathlib.Path(filename).suffix or ""
            key = f"products/{product.id}/{uuid4().hex}{ext}"

            extra_args = {}
            if hasattr(f, "mimetype") and f.mimetype:
                extra_args["ContentType"] = f.mimetype

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
        result["image_urls"] = product_card_image_urls(product.id)
        result["image_url"] = result["image_urls"][0] if result["image_urls"] else None

        return jsonify(result), 201

    except Exception:
        logger.exception("Failed to create product")
        db.session.rollback()
        try:
            s3 = make_s3_client()
            bucket = os.getenv("S3_BUCKET")
            for key in uploaded_keys:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except Exception:
                    logger.exception("Failed to delete orphaned S3 key %s", key)
        except Exception:
            logger.exception("Failed during S3 cleanup")
        return jsonify({"error": "Failed to create product"}), 500


@product_bp.route("/your-favorites", methods=["GET"])
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
        pd["product_type_name"] = p.product_type.name if p.product_type else None
        primary = primary_product_image_url(p.id)
        pd["image_url"] = primary
        pd["image_urls"] = [primary] if primary else []
        out.append(pd)
    return jsonify(out), 200


@product_bp.route("/our-favorites", methods=["GET"])
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
        pd["product_type_name"] = p.product_type.name if p.product_type else None
        primary = primary_product_image_url(p.id)
        pd["image_url"] = primary
        pd["image_urls"] = [primary] if primary else []
        out.append(pd)
    return jsonify(out), 200


@product_bp.route("/products", methods=["GET"])
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
        pd["product_type_name"] = p.product_type.name if p.product_type else None
        pd["image_urls"] = product_card_image_urls(p.id)
        pd["image_color_ids"] = product_card_image_color_ids(p.id)
        pd["image_url"] = pd["image_urls"][0] if pd["image_urls"] else None
        out.append(pd)
    return jsonify(out), 200


@product_bp.route("/products/sort", methods=["GET"])
@jwt_required()
def get_products_sort():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    rows = db.session.execute(
        text("SELECT id, title, sort_order FROM products where is_active = true ORDER BY sort_order ASC")
    ).mappings().all()
    return jsonify({"products": [dict(r) for r in rows]}), 200


@product_bp.route("/products/sort", methods=["PUT"])
@jwt_required()
def put_products_sort():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or []
    if not isinstance(data, list):
        return jsonify({"error": "Body must be an array"}), 400
    if not data:
        return jsonify({"error": "Body cannot be empty"}), 400
    updates = []
    seen_ids = set()
    seen_orders = set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            return jsonify({"error": f"Invalid row at index {i}"}), 400
        try:
            pid = int(row.get("product_id"))
            sort_order = int(row.get("sort_order"))
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid product_id/sort_order at index {i}"}), 400
        if sort_order < 0:
            return jsonify({"error": "sort_order must be >= 0"}), 400
        if pid in seen_ids:
            return jsonify({"error": f"Duplicate product_id {pid}"}), 400
        if sort_order in seen_orders:
            return jsonify({"error": f"Duplicate sort_order {sort_order}"}), 400
        seen_ids.add(pid)
        seen_orders.add(sort_order)
        updates.append({"product_id": pid, "sort_order": sort_order})
    ids_in_db = db.session.execute(text("SELECT id FROM products where is_active = true")).scalars().all()
    ids_set = set(int(v) for v in ids_in_db)
    if ids_set != seen_ids:
        return jsonify({"error": "Body must include every product exactly once"}), 400
    expected_orders = set(range(len(ids_set)))
    if expected_orders != seen_orders:
        return jsonify({"error": f"sort_order values must be exactly 0..{len(ids_set) - 1}"}), 400
    try:
        for row in updates:
            db.session.execute(
                text("UPDATE products SET sort_order = :sort_order WHERE id = :product_id"),
                {"sort_order": row["sort_order"], "product_id": row["product_id"]},
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to update products sort order")
        return jsonify({"error": "Failed to save product order"}), 500
    rows = db.session.execute(
        text("SELECT id, title, sort_order FROM products WHERE is_active = true ORDER BY sort_order ASC, id ASC")
    ).mappings().all()
    return jsonify({"products": [dict(r) for r in rows]}), 200


@product_bp.route("/product/<int:product_id>", methods=["GET"])
def get_product(product_id):
    product = Product.query.get_or_404(product_id)
    return jsonify(product_with_images_payload(product)), 200


@product_bp.route("/product/<int:product_id>", methods=["PATCH"])
@jwt_required()
def update_product(product_id):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    product = Product.query.get_or_404(product_id)
    data = request.get_json() or {}
    if "title" in data and data["title"] is not None:
        product.title = str(data["title"]).strip()[:200]
    if "description" in data:
        product.description = str(data["description"]).strip() if data["description"] else None
    if "price" in data and data["price"] is not None:
        try:
            product.price = float(data["price"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid price"}), 400
    if "dimensions" in data and data["dimensions"] is not None:
        product.dimensions = str(data["dimensions"]).strip() or None
    if "lead_time" in data and data["lead_time"] is not None:
        product.lead_time = str(data["lead_time"]).strip() or None
    if "color" in data and data["color"] is not None:
        product.color = str(data["color"]).strip()[:50]
    if "is_active" in data:
        product.is_active = bool(data["is_active"])
    db.session.commit()
    return jsonify(product_with_images_payload(product)), 200


@product_bp.route("/product/<int:product_id>/images/order", methods=["PUT"])
@jwt_required()
def reorder_product_images(product_id):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    product = Product.query.get_or_404(product_id)
    data = request.get_json() or {}
    order = data.get("order")
    if not isinstance(order, list) or len(order) == 0:
        return jsonify({"error": "order must be a non-empty list of image ids"}), 400
    try:
        order = [int(x) for x in order]
    except (TypeError, ValueError):
        return jsonify({"error": "order must be integers"}), 400
    images = ProductImage.query.filter_by(product_id=product.id).all()
    id_to_img = {img.id: img for img in images}
    if set(order) != set(id_to_img.keys()):
        return jsonify({"error": "order must contain exactly the same image ids as the product"}), 400
    for idx, img_id in enumerate(order):
        id_to_img[img_id].sort_order = idx
    db.session.commit()
    return jsonify(product_with_images_payload(product)), 200


@product_bp.route("/product/<int:product_id>/images/<int:image_id>", methods=["DELETE"])
@jwt_required()
def delete_product_image(product_id, image_id):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    product = Product.query.get_or_404(product_id)
    img = ProductImage.query.filter_by(id=image_id, product_id=product.id).first()
    if not img:
        return jsonify({"error": "Image not found"}), 404
    db.session.delete(img)
    db.session.commit()
    return jsonify(product_with_images_payload(product)), 200
