"""Exact database money shared by schemas without importing business services."""

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator


class ExactMoney(TypeDecorator):
    impl = Numeric(30, 12)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(
            String(64) if dialect.name == "sqlite" else Numeric(30, 12)
        )

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(Decimal(value), "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)
