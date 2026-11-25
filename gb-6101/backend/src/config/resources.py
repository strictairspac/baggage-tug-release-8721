from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from .settings import DATABASE_URL, REDIS_URL

if DATABASE_URL.startswith("sqlite"):
    memory = ":memory:" in DATABASE_URL or DATABASE_URL in {"sqlite://", "sqlite:///"}
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
        **({"poolclass": StaticPool} if memory else {}),
    )
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
cache = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=3)
