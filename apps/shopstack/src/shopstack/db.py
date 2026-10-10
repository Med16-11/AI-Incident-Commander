"""Database engine creation, demo schema bootstrap, and deterministic seed data."""

from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from shopstack.config import ShopStackSettings
from shopstack.models import Base, InventoryItemRow, UserRow

DEMO_USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def create_shopstack_engine(settings: ShopStackSettings) -> Engine:
    """Create a bounded PostgreSQL pool suitable for demonstrating exhaustion."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        pool_timeout=2,
    )


def create_shopstack_sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def initialize_database(engine: Engine) -> None:
    """Create demo tables and seed stable users and inventory under a DB lock."""
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(73190422)"))
        try:
            Base.metadata.create_all(connection)
            connection.commit()
            with Session(bind=connection, expire_on_commit=False) as session:
                if session.get(UserRow, DEMO_USER_ID) is None:
                    session.add(
                        UserRow(
                            id=DEMO_USER_ID,
                            name="Alex Shopper",
                            email="alex@example.test",
                        )
                    )
                seed_items = (
                    ("SKU-COFFEE", "House Coffee", 500),
                    ("SKU-TEA", "Earl Grey Tea", 350),
                    ("SKU-MUG", "Ceramic Mug", 120),
                )
                for sku, name, quantity in seed_items:
                    item = session.get(InventoryItemRow, sku)
                    if item is None:
                        session.add(InventoryItemRow(sku=sku, name=name, available=quantity))
                session.commit()
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(73190422)"))
            connection.commit()


if __name__ == "__main__":
    configured = ShopStackSettings.from_environment()
    database_engine = create_shopstack_engine(configured)
    try:
        initialize_database(database_engine)
    finally:
        database_engine.dispose()
