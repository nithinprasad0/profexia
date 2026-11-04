FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Normalise line endings (in case the repo was checked out on Windows with CRLF)
# and run as an unprivileged user.
RUN sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 profexia \
    && chown -R profexia:profexia /app
USER profexia

# Render/other platforms inject PORT; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# Invoked through sh so it works even if the executable bit is lost (e.g. Windows checkouts, bind mounts).
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
