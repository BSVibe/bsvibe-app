"""Shared persistence primitives.

``Base`` is the single SQLAlchemy ``DeclarativeBase`` every module-owned
table set inherits from. Per-module ``db.py`` files still own their own
table *definitions* (and keep their historical ``<Module>Base`` name as
an alias of ``Base`` for back-compat), but they all register on one
``Base.metadata`` — so Alembic autogenerate sees a single target and
cross-module foreign keys resolve against one registry.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """The one declarative base shared across every backend module."""


__all__ = ["Base"]
