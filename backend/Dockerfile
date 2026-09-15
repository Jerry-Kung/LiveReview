FROM python:3.12-slim

WORKDIR /app

RUN sed -i \
      -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
      -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY app ./app

RUN pip install --no-cache-dir uv -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN uv sync --frozen --no-dev

EXPOSE 12439

CMD ["uv", "run", "python", "-m", "app"]
