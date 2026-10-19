"""Connection settings for the live pipeline, overridable through environment variables.

Defaults match docker-compose.yml (local development only, D-21).
"""

import os

RABBITMQ_URL = os.environ.get("QOE_RABBITMQ_URL", "amqp://qoe:qoe@127.0.0.1:5672/")
MONGO_URL = os.environ.get("QOE_MONGO_URL", "mongodb://127.0.0.1:27017")
MONGO_DB = os.environ.get("QOE_MONGO_DB", "qoe")
