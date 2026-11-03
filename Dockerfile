# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved. Proprietary.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 JOBBOT_DB_PATH=/data/jobbot.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jobbot_enterprise ./jobbot_enterprise
RUN useradd --uid 10001 --create-home jobbot && mkdir -p /data && chown jobbot /data
USER jobbot
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "--factory", "jobbot_enterprise.main:get_app", "--host", "0.0.0.0", "--port", "8080"]
