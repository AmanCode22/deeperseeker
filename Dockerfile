FROM python:3.13-slim


COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app


RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY requirements*.in requirements*.txt ./
RUN uv sync --no-install-project    # deps only — no project wheel without source
COPY . .
RUN uv sync                          # install the project now that source exists

COPY . .

# P0-8 (Day 17): the service runs as an unprivileged user. Only the durable
# state directory (SQLite database, cookie backup, generated api_key.txt) is
# writable; the code and the venv stay root-owned and read-only to the
# process. A named volume mounted at /app/data inherits this ownership on
# first use, so the default run needs no manual ownership surgery.
RUN useradd --uid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data \
    && chmod 755 /app \
    && ln -sf /app/data/deeperseeker.db /app/deeperseeker.db \
    && ln -sf /app/data/aws_cookies_deepseek.json /app/aws_cookies_deepseek.json

EXPOSE 4000

# P0-2 (Day 20): stamped by .github/workflows/release.yml so the startup log
# states the exact commit the image was built from. Defaults keep local
# builds honest ("-").
ARG DEEPSEEKER_BUILD_COMMIT=-

ENV DB_PATH=/app/data/deeperseeker.db
ENV DEEPSEEKER_COOKIE_PATH=/app/data/aws_cookies_deepseek.json
ENV HOST=0.0.0.0
ENV DEEPSEEKER_BUILD_COMMIT=${DEEPSEEKER_BUILD_COMMIT}
# uv installs the console script into /app/.venv/bin — without this the
# CMD below never resolves ("exec: deeperseeker: not found").
ENV PATH="/app/.venv/bin:${PATH}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -sf http://localhost:4000/health || exit 1

# Non-root from here on (P0-8). Rollback note: a data volume already chown'd
# to appuser (uid 10001) also works under a root-era image of this service —
# root can write anything — so moving between the two is safe in both
# directions. A volume populated by a pre-P0-8 root-era image holds root-owned
# files; run the one-liner from README "Security defaults" once to hand it to
# appuser (or run this image once with --user root, which heals nothing but
# keeps writing as root).
USER appuser

# If reverting to backup Playwright cookie generation, run under xvfb:
# CMD ["sh", "-c", "xvfb-run -a -s '-screen 0 1280x720x24' python3 app.py"]
# might also need requirements-waf-backup edition
CMD ["deeperseeker"]
