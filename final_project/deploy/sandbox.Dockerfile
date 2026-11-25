# 전략 코드를 돌리는 격리 이미지. 하네스와 numpy·pandas, 학습 라이브러리만 든다(nh_mcp·비밀값은
# 넣지 않는다). 실행 제한(네트워크 없음, 읽기 전용, 메모리·프로세스 수 등)은 docker run 고정 인자가 건다.
FROM python:3.12-slim

# lightgbm은 실행할 때 OpenMP(libgomp)가 있어야 한다. slim 이미지에는 없다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.5.5 pandas==3.1.1 \
    scipy==1.18.1 scikit-learn==1.9.1 statsmodels==0.15.0 lightgbm==4.7.0
# PyTorch는 공식 CPU 휠 색인에서만 받는다. PyPI의 torch는 x86_64에서 CUDA 라이브러리를 함께 끌어온다.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu
# 수치 계산 라이브러리가 CPU 수만큼 스레드를 띄우지 않게 한다. 프로세스 수 한도(32) 안에서
# import numpy가 스레드 생성 실패로 멈추지 않게 하려는 것이다. torch도 OMP_NUM_THREADS를 따른다.
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
COPY src/nh_mcp/sandbox/harness.py /harness/harness.py
# 작업 폴더. root 소유라 전략 코드는 여기에 쓰지 못한다(쓸 곳은 /tmp뿐).
RUN mkdir /sandbox
USER 65534:65534
