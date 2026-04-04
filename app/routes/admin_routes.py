import json
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Order, OurFavorite, Product, ProductImage, YourFavorite
from app.routes.common import is_admin, order_to_dict_with_product, presign_key
from app.utils.email import send_delivered_email, send_shipped_email

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin/products/inactive", methods=["GET"])
@jwt_required()
def admin_get_inactive_products():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
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
        pd["image_url"] = presign_key(first_image.s3_key) if first_image else None
        out.append(pd)
    return jsonify(out), 200


@admin_bp.route("/admin/orders", methods=["GET"])
@jwt_required()
def admin_list_orders():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    page = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(50, int(request.args.get("per_page", 10))))
    subq = (
        db.session.query(Order.order_number, db.func.min(Order.created_at).label("created_at"))
        .filter(Order.order_number.isnot(None))
        .group_by(Order.order_number)
        .subquery()
    )
    total = db.session.query(db.func.count()).select_from(subq).scalar() or 0
    order_numbers = (
        db.session.query(subq.c.order_number)
        .order_by(subq.c.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    order_numbers = [r[0] for r in order_numbers]
    orders_by_number = {}
    for onum in order_numbers:
        rows = Order.query.filter_by(order_number=onum).order_by(Order.id).all()
        orders_by_number[onum] = [order_to_dict_with_product(o) for o in rows]
    return jsonify(
        {
            "orders_by_number": orders_by_number,
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    ), 200


@admin_bp.route("/admin/orders/<order_number>", methods=["GET"])
@jwt_required()
def admin_get_order(order_number):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    orders = Order.query.filter_by(order_number=order_number).order_by(Order.id).all()
    if not orders:
        return jsonify({"error": "Order not found"}), 404
    out = [order_to_dict_with_product(o) for o in orders]
    return jsonify({"order_number": order_number, "orders": out}), 200


@admin_bp.route("/admin/orders/<order_number>", methods=["PATCH"])
@jwt_required()
def admin_update_order(order_number):
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    orders = Order.query.filter_by(order_number=order_number).all()
    if not orders:
        return jsonify({"error": "Order not found"}), 404
    if "status" in data and data["status"] in ("Ordered", "Shipped", "Delivered"):
        for o in orders:
            o.status = data["status"]
    if "tracking_url" in data:
        val = data["tracking_url"]
        val = str(val).strip() if val else None
        for o in orders:
            o.tracking_url = val
    if "comments" in data:
        val = data["comments"]
        if isinstance(val, list):
            raw = json.dumps([str(x).strip() for x in val if str(x).strip()])
        else:
            raw = json.dumps([str(val).strip()]) if val and str(val).strip() else None
        for o in orders:
            o.comments = raw
    db.session.commit()
    new_status = data.get("status")
    recipient = orders[0].customer_email if orders else None
    if recipient and new_status == "Shipped":
        send_shipped_email(recipient, order_number, orders[0].tracking_url)
    elif recipient and new_status == "Delivered":
        delivery_date = datetime.now(timezone.utc).strftime("%B %d, %Y")
        send_delivered_email(recipient, order_number, delivery_date)
    out = [order_to_dict_with_product(o) for o in orders]
    return jsonify({"order_number": order_number, "orders": out}), 200


@admin_bp.route("/admin/your-favorites", methods=["GET"])
@jwt_required()
def admin_get_your_favorites():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    rows = YourFavorite.query.order_by(YourFavorite.sort_order.asc()).all()
    products = Product.query.filter_by(is_active=True).order_by(Product.title.asc()).all()
    return jsonify(
        {
            "favorites": [r.to_dict() for r in rows],
            "products": [{"id": p.id, "title": p.title} for p in products],
        }
    ), 200


@admin_bp.route("/admin/your-favorites", methods=["PUT"])
@jwt_required()
def admin_put_your_favorites():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    slots = data.get("slots")
    if not isinstance(slots, list):
        return jsonify({"error": "slots must be an array"}), 400
    active_products = Product.query.filter_by(is_active=True).all()
    max_slots = len(active_products)
    active_ids = {p.id for p in active_products}
    if len(slots) > max_slots:
        return jsonify({"error": f"slots may have at most {max_slots} entries"}), 400
    for i, val in enumerate(slots):
        if val is None:
            continue
        try:
            pid = int(val)
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid product_id at index {i}"}), 400
        if pid not in active_ids:
            return jsonify({"error": f"Product {pid} is not active"}), 400

    YourFavorite.query.delete()
    for i, val in enumerate(slots):
        if val is None:
            continue
        db.session.add(YourFavorite(product_id=int(val), sort_order=i))
    db.session.commit()
    rows = YourFavorite.query.order_by(YourFavorite.sort_order.asc()).all()
    return jsonify({"favorites": [r.to_dict() for r in rows]}), 200


@admin_bp.route("/admin/our-favorites", methods=["GET"])
@jwt_required()
def admin_get_our_favorites():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    rows = OurFavorite.query.order_by(OurFavorite.sort_order.asc()).all()
    products = Product.query.filter_by(is_active=True).order_by(Product.title.asc()).all()
    return jsonify(
        {
            "favorites": [r.to_dict() for r in rows],
            "products": [{"id": p.id, "title": p.title} for p in products],
        }
    ), 200


@admin_bp.route("/admin/our-favorites", methods=["PUT"])
@jwt_required()
def admin_put_our_favorites():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    slots = data.get("slots")
    if not isinstance(slots, list):
        return jsonify({"error": "slots must be an array"}), 400
    active_products = Product.query.filter_by(is_active=True).all()
    max_slots = len(active_products)
    active_ids = {p.id for p in active_products}
    if len(slots) > max_slots:
        return jsonify({"error": f"slots may have at most {max_slots} entries"}), 400
    for i, val in enumerate(slots):
        if val is None:
            continue
        try:
            pid = int(val)
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid product_id at index {i}"}), 400
        if pid not in active_ids:
            return jsonify({"error": f"Product {pid} is not active"}), 400

    OurFavorite.query.delete()
    for i, val in enumerate(slots):
        if val is None:
            continue
        db.session.add(OurFavorite(product_id=int(val), sort_order=i))
    db.session.commit()
    rows = OurFavorite.query.order_by(OurFavorite.sort_order.asc()).all()
    return jsonify({"favorites": [r.to_dict() for r in rows]}), 200
