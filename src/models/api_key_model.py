from sqlalchemy.orm import Mapped, mapped_column

from src.database.base import Base, TimestampMixin, UUIDMixin


class ApiKey(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "api_key"

    key_hash: Mapped[str] = mapped_column(unique=True, index=True)  # sha256 hex
    prefix: Mapped[str]  # first 8 chars of the plaintext, for identification in UIs
    name: Mapped[str]
    principal_id: Mapped[str]
    revoked: Mapped[bool] = mapped_column(default=False)
