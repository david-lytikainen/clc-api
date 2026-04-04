import logging
import os
import pathlib
from uuid import uuid4

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Banner, BannerPicture, FooterPicture
from app.routes.common import is_admin, make_s3_client, presign_key

logger = logging.getLogger(__name__)

home_page_bp = Blueprint("home_page", __name__)

BANNER_COLORS = ("primary", "primary_dark", "secondary", "secondary_dark")


def _banner_pictures_response():
    pics = (
        BannerPicture.query.filter(BannerPicture.banner_index.in_([0, 1, 2]))
        .order_by(BannerPicture.banner_index)
        .all()
    )
    return [
        {"id": p.id, "s3_key": p.s3_key, "banner_index": p.banner_index, "image_url": presign_key(p.s3_key)}
        for p in pics
    ]


def _footer_pictures_response():
    pics = (
        FooterPicture.query.filter(FooterPicture.footer_index.in_([0, 1]))
        .order_by(FooterPicture.footer_index)
        .all()
    )
    return [
        {"id": p.id, "s3_key": p.s3_key, "footer_index": p.footer_index, "image_url": presign_key(p.s3_key)}
        for p in pics
    ]


@home_page_bp.route("/banner-pictures", methods=["GET"])
def get_banner_pictures():
    return jsonify(_banner_pictures_response()), 200


@home_page_bp.route("/banner-pictures", methods=["PUT"])
@jwt_required()
def update_banner_pictures():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    form = request.form
    files = request.files
    s3 = make_s3_client()
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return jsonify({"error": "S3 not configured"}), 500

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
        existing = BannerPicture.query.filter(BannerPicture.banner_index.in_([0, 1, 2])).all()
        for p in existing:
            p.banner_index = None
        db.session.flush()
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


@home_page_bp.route("/footer-pictures", methods=["GET"])
def get_footer_pictures():
    return jsonify(_footer_pictures_response()), 200


@home_page_bp.route("/footer-pictures", methods=["PUT"])
@jwt_required()
def update_footer_pictures():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    form = request.form
    files = request.files
    s3 = make_s3_client()
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


@home_page_bp.route("/banner", methods=["GET"])
def get_banner():
    """Public: return the most recent banner only if it is active, else null."""
    banner = Banner.query.order_by(Banner.created_at.desc()).first()
    if not banner or not banner.is_active:
        return jsonify({"banner": None}), 200
    return jsonify({"banner": banner.to_dict()}), 200


@home_page_bp.route("/admin/banner", methods=["POST"])
@jwt_required()
def admin_create_banner():
    if not is_admin():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    is_active = bool(data.get("is_active", True))
    text = (data.get("text") or "").strip()[:500]
    background_color = (data.get("background_color") or "primary").strip()
    if background_color not in BANNER_COLORS:
        background_color = "primary"
    for b in Banner.query.all():
        b.is_active = False
    banner = Banner(is_active=is_active, text=text, background_color=background_color)
    db.session.add(banner)
    db.session.commit()
    return jsonify(banner.to_dict()), 201
