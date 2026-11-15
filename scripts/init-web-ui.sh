#!/usr/bin/with-contenv bash
# LinuxServer custom-cont-init.d hook: reapplies the editor extension after recreation.
set -euo pipefail
web_dir=/usr/share/jellyfin/web
source_file=/subtitle-translator-web/subtitle-translator-ui.js
if [[ ! -f "$web_dir/index.html" || ! -f "$source_file" ]]; then
    echo '[subtitle-translator] Web UI files unavailable; dashboard plugin remains usable.' >&2
    exit 0
fi
install -m 0644 "$source_file" "$web_dir/subtitle-translator-ui.js"
asset_version="$(sha256sum "$source_file" | cut -c1-12)"
sed -E -i 's#<script[^>]*src="subtitle-translator-ui\.js[^\"]*"[^>]*></script>##g' "$web_dir/index.html"
sed -i "s#</body>#<script defer src=\"subtitle-translator-ui.js?v=${asset_version}\"></script></body>#" "$web_dir/index.html"
echo '[subtitle-translator] Added Translate subtitles to the Jellyfin Web editor.'
