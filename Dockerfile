FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
# Download OCR models at build time; the runtime user cannot write to site-packages.
RUN python -c "import cv2, numpy as np; from app.photo.extractor import _engine; \
img = np.full((120, 480), 255, np.uint8); cv2.putText(img, 'DL-A131 40', (10, 80), 0, 2, 0, 3); \
_engine()(img)"
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
