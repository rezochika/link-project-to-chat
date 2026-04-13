FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

VOLUME /root/.link-project-to-chat
VOLUME /project

ENTRYPOINT ["link-project-to-chat"]
CMD ["start", "--path", "/project"]
