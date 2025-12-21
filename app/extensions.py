import logging
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
db = SQLAlchemy()
jwt = JWTManager()