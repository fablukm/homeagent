FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "laya[serve]==0.4.31" \
 && useradd --uid 10001 --no-create-home laya
USER laya
ENV HF_HUB_OFFLINE=1 \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8080 \
    LAYA_DEVICE=cpu \
    LAYA_PRELOAD=1 \
    LAYA_MAX_LOADED=1
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["laya-serve"]
