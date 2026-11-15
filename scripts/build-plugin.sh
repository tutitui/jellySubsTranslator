#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$project_root/refs" "$project_root/artifacts"
if [[ -n "${JELLYFIN_CONTAINER:-}" ]]; then
    source_container="$JELLYFIN_CONTAINER"
    cleanup_container=false
else
    source_container="$(docker create "${JELLYFIN_IMAGE:-linuxserver/jellyfin:latest}")"
    cleanup_container=true
fi
cleanup() { if [[ "$cleanup_container" == true ]]; then docker rm "$source_container" >/dev/null; fi; }
trap cleanup EXIT
for assembly in MediaBrowser.Common MediaBrowser.Model; do
    docker cp "$source_container:/usr/lib/jellyfin/bin/$assembly.dll" "$project_root/refs/$assembly.dll"
done
docker run --rm --user "$(id -u):$(id -g)" \
    -e DOTNET_CLI_HOME=/tmp -e DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    -v "$project_root:/src" -w /src/plugin \
    "${DOTNET_SDK_IMAGE:-mcr.microsoft.com/dotnet/sdk:10.0}" \
    dotnet build -c Release -o /src/artifacts
cp "$project_root/plugin/meta.json" "$project_root/artifacts/meta.json"
printf 'Plugin built in %s/artifacts\n' "$project_root"
