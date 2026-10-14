# Glama (glama.ai) builds this image and introspects the stdio MCP server.
# `docker run -i` must start deadlatch-mcp with the example policy and portfolio.
FROM python:3.12-slim

WORKDIR /app

# Install the package from this repo (runtime dependencies only).
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Required startup files. These are server config, not tool arguments.
COPY examples/01_pass/policy.yaml examples/01_pass/portfolio.json ./examples/01_pass/

# stdio MCP frames must not sit in a block buffer.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["deadlatch-mcp"]
CMD ["--policy", "/app/examples/01_pass/policy.yaml", "--portfolio", "/app/examples/01_pass/portfolio.json"]
