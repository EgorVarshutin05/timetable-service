# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /stub

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    uv pip install --system -r requirements.txt

COPY app.py fixtures.json ./

RUN useradd --system --no-create-home stub
USER stub
EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "warning"]
