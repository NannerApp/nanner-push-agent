FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN adduser --disabled-password --gecos "" nanner && \
    mkdir -p /data /config && \
    chown nanner:nanner /data /config

ENV NANNER_AGENT_CONFIG=/config/config.yaml
VOLUME ["/data", "/config"]
WORKDIR /data

USER nanner

EXPOSE 8421
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8421", "--app-dir", "/app"]
