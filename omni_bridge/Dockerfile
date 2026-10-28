ARG BUILD_ARCH=aarch64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:latest AS build

RUN apk add --no-cache build-base git pax-utils alsa-lib-dev flac-dev libvorbis-dev mpg123-dev soxr-dev
RUN git clone https://github.com/ralph-irving/squeezelite /src \
    && cd /src \
    && git checkout afa8e834b2e2774016ca244a5f19f67ed0bd7d3b \
    && make OPTS="-DLINKALL -DRESAMPLE -DNO_FAAD -DNO_MAD" \
    && scanelf --nobanner --needed --format '%n#p' squeezelite | tr ',' '\n' | sed 's/^/so:/' > /src/deps

FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:latest

COPY --from=build /src/squeezelite /usr/bin/squeezelite
COPY --from=build /src/deps /tmp/deps
RUN apk add --no-cache python3 py3-paho-mqtt wavpack $(cat /tmp/deps)

COPY omni_addon.py wvenc.py templates.json /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
