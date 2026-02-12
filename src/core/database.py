from common.database import DatabaseManager

from .config import settings

db_manager = DatabaseManager(settings.DATABASE_URL)

get_db = db_manager.get_db
db_session = db_manager.db_session
engine = db_manager.engine
SessionLocal = db_manager.SessionLocal
