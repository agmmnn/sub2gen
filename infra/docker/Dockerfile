# Build frontend
FROM oven/bun:1.3.14-slim AS frontend-builder
WORKDIR /app
COPY package.json bun.lock ./
COPY apps/admin-web/package.json apps/admin-web/package.json
COPY apps/captcha-extension/package.json apps/captcha-extension/package.json
COPY apps/metadata-extension/package.json apps/metadata-extension/package.json
COPY packages/api-contract/package.json packages/api-contract/package.json
COPY packages/extension-core/package.json packages/extension-core/package.json
RUN bun install --frozen-lockfile
COPY apps/admin-web/ apps/admin-web/
COPY packages/api-contract/ packages/api-contract/
RUN bun run --cwd apps/admin-web build

# Build backend
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Install locked Python dependencies before copying the application for better caching.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages/provider-sdk-python/pyproject.toml packages/provider-sdk-python/pyproject.toml
COPY packages/provider-google-flow/pyproject.toml packages/provider-google-flow/pyproject.toml
COPY packages/provider-chatgpt/pyproject.toml packages/provider-chatgpt/pyproject.toml
COPY packages/worker-protocol/pyproject.toml packages/worker-protocol/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-project --no-install-workspace

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Copy built frontend assets from builder stage
COPY --from=frontend-builder /app/apps/admin-web/dist /app/apps/api/static

EXPOSE 8000

CMD ["sub2gen"]
