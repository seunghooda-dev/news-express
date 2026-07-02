from __future__ import annotations

from .ops_logging import configure_logging, get_logger
from .scheduler import build_auto_collector_from_env
from .settings import env_database, env_path, load_environment
from .storage import Store
from .web import create_app


load_environment()
configure_logging()

logger = get_logger("wsgi")
store = Store(env_database())
config_path = env_path("NEWS_SUMMARY_CONFIG", "config/municipalities.yaml")
store.init_db()

auto_collector = build_auto_collector_from_env(store, config_path)
app = create_app()
app.config["AUTO_COLLECTOR"] = auto_collector

if auto_collector and auto_collector.snapshot().enabled:
    auto_collector.start()
    logger.info("auto collector started for wsgi app")
elif auto_collector:
    logger.info("auto collector prepared but disabled for wsgi app")
