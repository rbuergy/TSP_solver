# Runs the TSP Visualizer with a production web server (gunicorn).
#   docker build -t tsp-visualizer .
#   docker run -p 7860:7860 tsp-visualizer          -> http://localhost:7860
# Users enter their own OpenRouteService key in the app. To give everyone a default key instead,
# add  -e ORS_API_KEY=...  to docker run (not recommended for a public server).
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
ENV PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MAX_TIME_LIMIT=120

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user . .

EXPOSE 7860
# One worker process: the route pages for "Open in new tab" are kept in that process's memory.
# Several threads, so solving for one user does not block the others.
CMD ["gunicorn", "app:server", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "300"]
