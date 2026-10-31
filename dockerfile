# Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OUT_DIR=/downloads

RUN useradd -ms /bin/bash appuser && \
    mkdir -p /app /downloads && \
    chown -R appuser:appuser /app /downloads

WORKDIR /app

COPY requirements.txt /app/requirements.txt
# deps
RUN pip install --no-cache-dir -r requirements.txt
# app
COPY --chown=appuser:appuser app.py /app/app.py

USER appuser
# where host downloads will be mounted
VOLUME ["/downloads"]
# Run the script
ENTRYPOINT ["python", "/app/app.py"]
# Default arguments
CMD ["--csv", "both"]
