#!/bin/sh
# (Re)starts the Firefox Flatpak inside the test desktop with a throwaway
# profile, never the real one: --profile under the Flatpak's own ~/.var/app
# dir (the only place the sandbox can write), --no-remote so it neither
# hands over to nor takes over a running Firefox. Run on the HOST. Extra
# args go to Firefox.
#   [FIREFOX_PAGE=controls.html] tools/testdesk/firefox.sh [url]
HERE=$(cd "$(dirname "$0")" && pwd)
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk${DECKERY_TESTDESK:-}"
DIR="$HOME/.var/app/org.mozilla.firefox/deckery-testdesk${DECKERY_TESTDESK:-}"
mkdir -p "$DIR/profile"
cp "$HERE"/*.html "$DIR/"
cat > "$DIR/profile/user.js" <<'EOF'
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.preonboarding.enabled", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
// The address bar opens its suggestions (top sites, trending) when focused
// at start — drawn inside the window, over the page, swallowing clicks.
user_pref("browser.urlbar.suggest.topsites", false);
user_pref("browser.urlbar.suggest.trending", false);
user_pref("browser.urlbar.trending.featureGate", false);
user_pref("browser.startup.couldRestoreSession.count", -1);
EOF
# [p] keeps pkill from matching this script's own command line
pkill -f "[d]eckery-testdesk${DECKERY_TESTDESK:-}/profile --no-remote" && sleep 2
# The test desktop has its own XDG_DATA_HOME; point flatpak back at the
# user installation (Firefox itself is a system one, found either way).
FLATPAK_USER_DIR="$HOME/.local/share/flatpak" setsid "$HERE/run.sh" flatpak run org.mozilla.firefox \
    --profile "$DIR/profile" --no-remote "${@:-file://$DIR/${FIREFOX_PAGE:-page.html}}" \
    > "$STATE/app-firefox.log" 2>&1 &
