FROM eclipse-temurin:17-jdk-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    GRADLE_VERSION=8.7 \
    PATH=/opt/gradle/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip unzip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Gradle
RUN curl -fsSL -o /tmp/gradle.zip https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && mv /opt/gradle-${GRADLE_VERSION} /opt/gradle \
    && rm /tmp/gradle.zip

# Android SDK command-line tools
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
    && curl -fsSL -o /tmp/cmdtools.zip https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
    && unzip -q /tmp/cmdtools.zip -d ${ANDROID_HOME}/cmdline-tools \
    && mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest \
    && rm /tmp/cmdtools.zip

RUN yes | sdkmanager --licenses > /dev/null || true
RUN sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"

RUN pip3 install --no-cache-dir requests pillow

WORKDIR /app
COPY bot.py /app/bot.py

CMD ["python3", "-u", "bot.py"]
