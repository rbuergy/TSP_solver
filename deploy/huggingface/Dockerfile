# Dockerfile for the Hugging Face Space. It builds the app straight from the GitHub repository,
# so the Space only needs this file and README.md (see README.md in this folder).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
ENV PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MAX_TIME_LIMIT=120

ARG REPO=rbuergy/TSP_solver
ARG BRANCH=main
# This file changes with every new commit on GitHub, so a rebuild always fetches the latest code.
ADD https://api.github.com/repos/${REPO}/commits/${BRANCH} /tmp/latest-commit.json
RUN git clone --depth 1 --branch ${BRANCH} https://github.com/${REPO}.git . \
 && pip install --no-cache-dir --user -r requirements.txt

EXPOSE 7860
CMD ["gunicorn", "app:server", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "300"]
