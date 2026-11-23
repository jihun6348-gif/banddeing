# 배포용 gunicorn 설정
# SQLite + 메모리 요청 제한을 쓰므로 프로세스 1개 + 스레드 여러 개로 돌린다.
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = int(os.environ.get("THREADS", "8"))
worker_class = "gthread"
timeout = 30
graceful_timeout = 20
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"
