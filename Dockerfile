FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TECH_DB_RUNTIME_DIR=/app/runtime

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x docker-entrypoint.sh

VOLUME ["/app/runtime"]
EXPOSE 8765
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "qa-backend/server.py"]
