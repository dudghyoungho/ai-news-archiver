FROM python:3.11-slim
#Base Image : 어떤 OS, 파이썬을 어떤 것을 갖다 쓸 것인가.

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
# 파이썬을 위한 환경설정 : pyc파일을 만들지 마세요. 로그가 버퍼링 없이 출력되도록 하세요.


WORKDIR /app
# 모든 명령어는 컨테이너 내부의 /app에서 실행되도록 한다.

RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*
# 리눅스 차원의 필수 패키지들을 설치한다.

COPY requirements.txt .
# 파이썬 라이브러리를 설치한다.
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
#나머지 소스 코드를 전체 다 복사한다.

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
# 컨테이너가 시작될 때 실행할 명령어




#copy를 두 번 나눠서 한 이유 : 도커는 명령줄 한 줄 마다 레이어를 만드는데,
# 파일이 바뀌지 않았다면 캐시된 것을 재사용한다. 
# 따라서 변경사항이 있을 때 몽땅 다시 빌드하는 것을 막기 위해 나누어 copy를 적용


# 0.0.0.0:8000 : 외부 접속을 허용한다는 의미
# 