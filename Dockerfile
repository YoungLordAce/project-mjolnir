# ==============================================================================
# Stage 1: Builder — compile & fetch standalone Linux x64 CLI tools
# ==============================================================================
FROM fedora:latest AS builder

RUN dnf install -y \
        gcc \
        gcc-c++ \
        cmake \
        make \
        pkgconf-pkg-config \
        libzstd-devel \
        binutils \
        curl \
        tar \
        xz \
        ca-certificates \
    && dnf clean all

RUN mkdir -p /out

# 1. Official standalone 7zz (includes RAR/RAR5 decompression)
RUN curl -fL -o /tmp/7z.tar.xz https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz \
    && tar -xf /tmp/7z.tar.xz -C /out 7zz \
    && chmod 755 /out/7zz \
    && rm -f /tmp/7z.tar.xz

# 2. ZArchive (Wii U Loadiine -> .wua compression, static libzarchive + dynamic libzstd)
RUN mkdir -p /tmp/zarchive_src \
    && curl -fL -o /tmp/zarchive.tar.gz https://github.com/Exzap/ZArchive/archive/refs/heads/master.tar.gz \
    && tar -xzf /tmp/zarchive.tar.gz -C /tmp/zarchive_src --strip-components=1 \
    && cmake -B /tmp/zarchive_build -S /tmp/zarchive_src \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DCMAKE_CXX_FLAGS="-include cstdint" \
    && cmake --build /tmp/zarchive_build -j"$(nproc)" \
    && ( [ -x /tmp/zarchive_build/zarchive ] && cp /tmp/zarchive_build/zarchive /out/zarchive \
         || cp /tmp/zarchive_build/zarchiveTool /out/zarchive ) \
    && strip /out/zarchive \
    && chmod 755 /out/zarchive \
    && rm -rf /tmp/zarchive.tar.gz /tmp/zarchive_src /tmp/zarchive_build

# 3. CDecrypt (Wii U encrypted WUP/NUS title.tmd + *.app decryption)
RUN mkdir -p /tmp/cdecrypt_src \
    && curl -fL -o /tmp/cdecrypt.tar.gz https://github.com/VitaSmith/cdecrypt/archive/refs/tags/v4.8.tar.gz \
    && tar -xzf /tmp/cdecrypt.tar.gz -C /tmp/cdecrypt_src --strip-components=1 \
    && for mf in /tmp/cdecrypt_src/Makefile /tmp/cdecrypt_src/makefile; do \
           [ -f "$mf" ] && sed -i 's/-Werror//g' "$mf"; \
       done \
    && make -C /tmp/cdecrypt_src -j"$(nproc)" \
    && cp /tmp/cdecrypt_src/cdecrypt /out/cdecrypt \
    && strip /out/cdecrypt \
    && chmod 755 /out/cdecrypt \
    && rm -rf /tmp/cdecrypt.tar.gz /tmp/cdecrypt_src

# 4. pkg2zip (PS Vita Sony .pkg + zRIF -> Vita3K .zip)
RUN mkdir -p /tmp/pkg2zip_src \
    && curl -fL -o /tmp/pkg2zip.tar.gz https://github.com/lusid1/pkg2zip/archive/refs/tags/v2.3.tar.gz \
    && tar -xzf /tmp/pkg2zip.tar.gz -C /tmp/pkg2zip_src --strip-components=1 \
    && for mf in /tmp/pkg2zip_src/Makefile /tmp/pkg2zip_src/makefile; do \
           [ -f "$mf" ] && sed -i 's/-Werror//g' "$mf"; \
       done \
    && make -C /tmp/pkg2zip_src -j"$(nproc)" \
    && cp /tmp/pkg2zip_src/pkg2zip /out/pkg2zip \
    && strip /out/pkg2zip \
    && chmod 755 /out/pkg2zip \
    && rm -rf /tmp/pkg2zip.tar.gz /tmp/pkg2zip_src

# ==============================================================================
# Stage 2: Runtime — lean production NAS image (no compilers or build headers)
# ==============================================================================
FROM fedora:latest

ENV PYTHONUNBUFFERED=1 \
    DOTNET_ROLL_FORWARD=LatestMajor \
    DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet_bundle \
    HOME=/tmp

# Native CLI tools: chdman (mame-tools), dolphin-tool (dolphin-emu-tool),
# libzstd (for zarchive), .NET 10 runtime (for psxpackager), and Python 3.11.
RUN dnf install -y \
        python3.11 \
        mame-tools \
        dolphin-emu-tool \
        dotnet-runtime-10.0 \
        libzstd \
        curl \
        tar \
        ca-certificates \
    && dnf clean all

# Copy compiled/fetched standalone CLI binaries from builder stage
COPY --from=builder /out/7zz /out/zarchive /out/cdecrypt /out/pkg2zip /usr/local/bin/

RUN ! ldd /usr/local/bin/zarchive /usr/local/bin/cdecrypt /usr/local/bin/pkg2zip | grep -q "not found" \
    && /usr/local/bin/7zz i >/dev/null

# NSC_Builder (squirrel.py) for Switch Base+Update+DLC .xci merging & .nsz decompression
RUN mkdir -p /opt/nscb /tmp/nscb_src \
    && curl -fL -o /tmp/nscb.tar.gz https://github.com/julesontheroad/NSC_BUILDER/archive/refs/heads/master.tar.gz \
    && tar -xzf /tmp/nscb.tar.gz -C /tmp/nscb_src --strip-components=1 \
    && cp -r /tmp/nscb_src/py/ztools /tmp/nscb_src/py/zconfig /opt/nscb/ \
    && ln -sf /app/tools/nscb/ztools/keys.txt /opt/nscb/ztools/keys.txt \
    && chmod -R a+rwX /opt/nscb \
    && rm -rf /tmp/nscb.tar.gz /tmp/nscb_src

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Install Project Mjolnir deps + NSC_Builder (squirrel.py) Linux dependencies + nsz
RUN python3.11 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && /app/.venv/bin/pip install --no-cache-dir \
        urllib3 \
        unidecode \
        tqdm \
        beautifulsoup4 \
        requests \
        pillow \
        pycryptodome \
        pykakasi \
        chardet \
        bottle \
        zstandard \
        colorama \
        nsz \
        pyctr

COPY main.py config.example.toml config.docker.example.toml /app/
# Use local config.docker.toml if present in the build context; otherwise fall back to the example
COPY config.docker.example.toml config.docker.tom[l] /app/
RUN [ -f /app/config.docker.toml ] || cp /app/config.docker.example.toml /app/config.docker.toml
COPY src /app/src

RUN mkdir -p /app/drop /app/.staging /app/processed /app/quarantine /app/tools /library /tmp/dotnet_bundle \
    && chmod 777 /tmp/dotnet_bundle

CMD ["/app/.venv/bin/python", "/app/main.py", "--config", "/app/config.docker.toml", "watch"]
