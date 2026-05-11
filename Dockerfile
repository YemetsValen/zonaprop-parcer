FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is needed for the compose healthcheck. Playwright system libs are
# included so flipping USE_PLAYWRIGHT=true at runtime "just works" — the
# Chromium binary itself is downloaded via `playwright install` below, which
# is gated by the same flag at build-time to keep image size down for users
# who don't need it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

ARG USE_PLAYWRIGHT=false
RUN if [ "$USE_PLAYWRIGHT" = "true" ]; then \
        playwright install chromium; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
