from sqlalchemy import Column, Integer, String
from app.database import Base


class AccountCode(Base):
    __tablename__ = "account_codes"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20),  nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    site = Column(String(255), nullable=False, default="Circle Express Ltd Heathrow")
