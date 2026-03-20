ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# 国内服务器：替换 apt 源为阿里云镜像（deb.debian.org 在国内不通）
ARG APT_MIRROR=mirrors.aliyun.com
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true

# 安装 git（编码任务需要 git 操作）、ffmpeg（语音/视频处理）、nodejs（yt-dlp YouTube 签名解密）
# libpango/libharfbuzz — weasyprint HTML→PDF 渲染依赖（Pango 文字排版 + HarfBuzz 字体子集化）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg nodejs \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_PROXY
# 主索引用阿里云镜像加速，找不到的包（如 yt-dlp）回退到官方 PyPI
# --timeout 120: 防止国内网络慢导致 ReadTimeoutError（默认 15s 太短）
# --retries 3: 下载失败自动重试（网络抖动常见于 files.pythonhosted.org）
RUN if [ -n "$PIP_PROXY" ]; then export HTTPS_PROXY=$PIP_PROXY HTTP_PROXY=$PIP_PROXY; fi && \
    pip install --no-cache-dir -r requirements.txt \
    -i ${PIP_INDEX_URL} --extra-index-url https://pypi.org/simple \
    --timeout 120 --retries 3

# CJK 字体：下载独立 TTF（TrueType 轮廓），不用 TTC / OTF (CFF/CID-keyed)
# NotoSansCJK 是 CID-keyed CFF 格式，fpdf2 兼容性差 → 改用 NotoSansSC（Google Fonts 版，TTF 格式）
# jsDelivr 有中国 CDN 节点，直连可达；失败则走构建代理
RUN mkdir -p /usr/share/fonts/truetype/noto && \
    python -c "\
import urllib.request, os, sys; \
out = '/usr/share/fonts/truetype/noto/NotoSansSC.ttf'; \
urls = [ \
    'https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf', \
    'https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf', \
]; \
for url in urls: \
    try: \
        print(f'Downloading {url}...'); \
        urllib.request.urlretrieve(url, out); \
        size = os.path.getsize(out); \
        if size > 1_000_000: \
            print(f'OK: {out} ({size//1024}KB)'); sys.exit(0); \
        os.remove(out); \
    except Exception as e: print(f'Failed: {e}'); \
print('ERROR: all font download URLs failed'); sys.exit(1)" \
    || echo "WARNING: CJK font download failed, PDF Chinese may not render"

# Playwright: 安装 Chromium 浏览器 + 系统依赖（browser_ops 浏览器自动化）
# 策略：优先用 npmmirror 国内 CDN 直连，失败则回退到代理
ARG PLAYWRIGHT_PROXY
RUN PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/chrome-for-testing \
      playwright install --with-deps chromium \
    || ( echo ">>> npmmirror failed, fallback to proxy" && \
         if [ -n "$PLAYWRIGHT_PROXY" ]; then \
           HTTPS_PROXY=$PLAYWRIGHT_PROXY HTTP_PROXY=$PLAYWRIGHT_PROXY \
           playwright install --with-deps chromium; \
         else \
           echo "ERROR: no proxy available for fallback" && exit 1; \
         fi )

COPY . .

EXPOSE 8000

CMD ["python", "-m", "app.main"]
