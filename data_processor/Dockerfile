FROM python:3.9-slim

WORKDIR /app

# Install Java (required for Spark) and other dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    default-jre-headless \
    wget \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Spark
ENV SPARK_VERSION=3.5.0
ENV HADOOP_VERSION=3
ENV SPARK_HOME=/opt/spark
ENV PATH=$PATH:$SPARK_HOME/bin:$SPARK_HOME/sbin

RUN wget -q https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz && \
    tar -xzf spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz && \
    mv spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION} ${SPARK_HOME} && \
    rm spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz

# Install Python dependencies
RUN pip install --no-cache-dir \
    python-docx \
    pypdf \
    pyspark==${SPARK_VERSION} \
    beautifulsoup4 \
    python-dotenv

# Copy source code
COPY . /app

# Set python path to include current directory
ENV PYTHONPATH=/app

# Default command (can be overridden)
CMD ["python", "main.py"]
