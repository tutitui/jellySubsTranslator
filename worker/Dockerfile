FROM python:3.13-slim
WORKDIR /app
COPY core.py server.py cli.py ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/health',timeout=3)"]
CMD ["python", "server.py"]
