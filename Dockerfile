# Deployed on Railway. Using a Dockerfile instead of Railway's default
# Nixpacks builder specifically for Chromium: several people report the bare
# `chromium` Nix package failing on Railway with unclear "command not found"
# / missing-library errors, whereas Debian's `apt` package pulls in its own
# runtime dependencies correctly -- a much more standard, well-documented
# path for headless Chrome in a container. Railway auto-detects and prefers
# a Dockerfile over Nixpacks when both could apply.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Bypasses report_builder.py's PATH-based lookup entirely -- no ambiguity
# about whether the binary is named `chromium` or `chromium-browser`.
ENV CHROME_BIN=/usr/bin/chromium

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects $PORT at runtime. Bare shell-form CMD (no brackets) is
# *supposed* to run through /bin/sh -c and expand env vars either way, but on
# Railway specifically this produced the literal string "$PORT" (dollar sign
# included) rather than its value -- explicitly invoking sh -c sidesteps
# whatever wrapper Railway uses around CMD and reliably expands it.
CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8080}"]
