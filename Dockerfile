FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY coach ./coach
ENV COACH_DB=/data/coach.db
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "coach.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
