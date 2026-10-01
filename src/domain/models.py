from sqlalchemy import Column, Integer, String, Float, DateTime
from src.infrastructure.database import Base

class BetAnalysis(Base):
    __tablename__ = "bet_analysis"

    id = Column(Integer, primary_key=True, index=True)
    match_description = Column(String, nullable=False)
    home_lambda = Column(Float, nullable=False)
    away_lambda = Column(Float, nullable=False)
    recommended_market = Column(String, nullable=False)
    odds = Column(Float, nullable=False)
    ev_percentage = Column(Float, nullable=False)
    kelly_stake = Column(Float, nullable=False)
    match_datetime = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)