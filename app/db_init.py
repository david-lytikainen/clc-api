from app import create_app
from app.extensions import db
from app.models import Role, ProductType

if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        db.create_all()
        if Role.query.count() == 0:
            db.session.add(Role(id=1, name="client"))
            db.session.add(Role(id=2, name="admin"))
            db.session.commit()
        if ProductType.query.count() == 0:
            db.session.add_all([
                ProductType(id=1, name="Leather Bag"),
                ProductType(id=2, name="Wallet"),
                ProductType(id=3, name="Accessory"),
                ProductType(id=4, name="Gift Card"),
            ])
            db.session.commit()
    print("Done.")
