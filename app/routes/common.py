import logging
import os
import pathlib
import random
from uuid import uuid4

import boto3
from flask_jwt_extended import get_jwt_identity
from app.extensions import db
from app.models import Color, Order, Product, ProductImage, User

logger = logging.getLogger(__name__)


def generate_order_number():
    for _ in range(10):
        num = "".join(str(random.randint(0, 9)) for _ in range(6))
        if not Order.query.filter_by(order_number=num).first():
            return num
    return str(random.randint(100000, 999999))


def product_has_color_image(product_id: int, color_id: int) -> bool:
    if Color.query.get(color_id) is None:
        return False
    return (
        ProductImage.query.filter_by(product_id=product_id, color_id=color_id).first()
        is not None
    )


def first_image_for_product_color(product_id: int, color_id: int):
    return (
        ProductImage.query.filter_by(product_id=product_id, color_id=color_id)
        .order_by(ProductImage.sort_order)
        .first()
    )


def stripe_metadata_dict(session_obj):
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


def order_image_url(order: Order):
    if not order.product_id or not order.color_id:
        return None
    img = first_image_for_product_color(order.product_id, order.color_id)
    return presign_key(img.s3_key) if img else None


def is_admin():
    try:
        identity = get_jwt_identity()
        if not identity:
            return False
        user_id = int(identity)
        user = User.query.get(user_id)
        return user and user.role_id == 2
    except Exception:
        return False


def make_s3_client():
    kwargs = {}
    kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
    kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")
    kwargs["region_name"] = os.getenv("AWS_REGION")
    return boto3.client("s3", **kwargs)


def presign_key(key: str, expires: int = 3600) -> str:
    try:
        bucket = os.getenv("S3_BUCKET")
        if not bucket or not key:
            return None
        s3 = make_s3_client()
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
        )
    except Exception:
        logger.exception("Failed to presign s3 key %s", key)
        return ""


def primary_product_image_url(product_id: int):
    img = (
        ProductImage.query.filter_by(product_id=product_id)
        .order_by(ProductImage.sort_order.asc())
        .first()
    )
    return presign_key(img.s3_key) if img else None


def product_card_image_urls(product_id: int) -> list:
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
        url = presign_key(img.s3_key)
        if url:
            out.append(url)
    return out


def product_card_image_color_ids(product_id: int) -> list:
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
        url = presign_key(img.s3_key)
        if url:
            out.append(img.color_id)
    return out


def product_with_images_payload(product: Product) -> dict:
    pd = product.to_dict()
    all_images = (
        ProductImage.query.filter_by(product_id=product.id)
        .order_by(ProductImage.sort_order)
        .all()
    )
    pd["image_urls"] = [presign_key(img.s3_key) for img in all_images]
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


def order_to_dict_with_product(o):
    od = o.to_dict()
    if o.product:
        od["product_title"] = o.product.title
    else:
        od["product_title"] = None
    c = Color.query.get(o.color_id) if getattr(o, "color_id", None) else None
    od["color_name"] = c.name if c else None
    od["color_hex"] = c.hex if c else None
    od["image_url"] = order_image_url(o)
    if o.user_id:
        u = User.query.get(o.user_id)
        if u:
            od["user_first_name"] = u.first_name
            od["user_last_name"] = u.last_name
            od["user_email"] = u.email
    return od
