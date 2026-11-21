FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY app/photo/requirements.txt app/photo/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
