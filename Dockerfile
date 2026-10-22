# One image for api, worker and migrate; the "test" stage adds test tooling and tests.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY alembic.ini pyproject.toml ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM runtime AS test

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt -c constraints.txt
COPY tests ./tests
USER appuser

# /app is read-only for appuser; keep coverage data in a writable location.
ENV COVERAGE_FILE=/tmp/.coverage
CMD ["pytest"]
