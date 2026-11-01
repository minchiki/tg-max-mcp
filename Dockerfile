FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

RUN useradd --system --uid 10001 tgmcp && mkdir -p /data && chown tgmcp /data
USER tgmcp

ENV TG_HOST=0.0.0.0 \
    TG_PORT=8765 \
    TG_SESSION=/data/session \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

CMD ["python", "server.py"]
