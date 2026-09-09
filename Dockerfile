FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY repetier_octoprint_bridge.py .

RUN useradd -r -u 1000 -m bridge
USER bridge

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('BRIDGE_PORT','5000')+'/',timeout=4)" || exit 1

CMD ["python", "repetier_octoprint_bridge.py"]
