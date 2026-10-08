FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY receptionist ./receptionist
COPY examples ./examples
USER app
EXPOSE 8000
CMD ["sh", "-c", "uvicorn receptionist.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
