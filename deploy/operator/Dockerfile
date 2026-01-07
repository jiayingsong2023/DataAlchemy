FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy project file
COPY pyproject.toml .

# Install dependencies using uv
RUN uv pip install --system .

# Copy operator logic and templates
COPY main.py .
COPY templates.yaml .

CMD ["kopf", "run", "/app/main.py", "--verbose"]
