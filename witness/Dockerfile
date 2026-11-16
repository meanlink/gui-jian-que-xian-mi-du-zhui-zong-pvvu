FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV WITNESS_DB=/data/witness.db WITNESS_HOST=0.0.0.0 WITNESS_PORT=9100
COPY server.py ./server.py
RUN mkdir -p /data
VOLUME ["/data"]
HEALTHCHECK --interval=4s --timeout=3s --retries=20 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9100/health',timeout=2).status==200 else 1)"
CMD ["python","-u","server.py"]
