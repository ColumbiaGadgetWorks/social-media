#!/bin/bash
# Adds the single sign-on (OIDC) settings to your CGW Studio container template on Unraid.
# It asks for each value, keeps a backup of the template, and writes the new variables into it.
# Run it in the Unraid terminal:
#   bash <(curl -fsSL https://raw.githubusercontent.com/ColumbiaGadgetWorks/social-media/HEAD/unraid/add-oidc-env.sh)
# Then open the container in the Docker tab, click Edit, and Apply.
set -u

DIR=/boot/config/plugins/dockerMan/templates-user
FILE="${1:-}"
if [ -z "$FILE" ]; then
  for f in "$DIR"/my-cgw-studio.xml "$DIR"/my-cgw*.xml "$DIR"/*cgw*.xml; do
    if [ -f "$f" ]; then FILE="$f"; break; fi
  done
fi
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "Couldn't find the CGW Studio template in $DIR. Files there:" >&2
  ls "$DIR" >&2
  echo "Run again with the file name, for example:" >&2
  echo "  bash add-oidc-env.sh $DIR/my-cgw-studio.xml" >&2
  exit 1
fi

exec 3<"${CGW_TTY:-/dev/tty}"   # answers come from the keyboard, even when the script itself is piped in

ask() {  # ask VAR "Question" "default"
  local v
  printf '%s [%s]: ' "$2" "$3" >&2
  IFS= read -r v <&3
  printf -v "$1" '%s' "${v:-$3}"
}
ask_required() {  # ask_required VAR "Question"
  local v=""
  while [ -z "$v" ]; do
    printf '%s: ' "$2" >&2
    IFS= read -r v <&3
  done
  printf -v "$1" '%s' "$v"
}
ask_secret() {  # ask_secret VAR "Question"
  local v
  printf '%s (typing is hidden, Enter for none): ' "$2" >&2
  IFS= read -rs v <&3
  echo >&2
  printf -v "$1" '%s' "$v"
}
yesno() {  # yesno VAR "Question" y|n
  local v
  printf '%s [%s]: ' "$2" "$3" >&2
  IFS= read -r v <&3
  v="${v:-$3}"
  case "$v" in y|Y|yes|YES|true) printf -v "$1" '%s' true ;; *) printf -v "$1" '%s' false ;; esac
}

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'; }

TMP="$(mktemp)"
cp "$FILE" "$TMP"

add() {  # add TARGET "Name" masked(true|false) display(always|advanced) "Description" "value"
  local target="$1" name="$2" mask="$3" disp="$4" desc="$5" val="$6" def="$6" line
  [ "$mask" = true ] && def=""            # a secret goes in the value, never in the visible default
  grep -v "Target=\"$target\"" "$TMP" > "$TMP.n" && mv "$TMP.n" "$TMP"
  line="  <Config Name=\"$(xml "$name")\" Target=\"$target\" Default=\"$(xml "$def")\" Mode=\"\" Description=\"$(xml "$desc")\" Type=\"Variable\" Display=\"$disp\" Required=\"false\" Mask=\"$mask\">$(xml "$val")</Config>"
  LINE="$line" awk '/<\/Container>/ { print ENVIRON["LINE"] } { print }' "$TMP" > "$TMP.n" && mv "$TMP.n" "$TMP"
}

BASE="$(grep -o 'Target="STUDIO_BASE_URL"[^>]*>[^<]*' "$FILE" | sed 's/.*>//' | head -n 1)"
echo >&2
echo "Single sign-on (OIDC) for CGW Studio" >&2
echo "Template: $FILE" >&2
echo >&2
echo "First, in your identity provider (VoidAuth), create an OIDC app/client with this redirect URL:" >&2
echo "    ${BASE:-https://<your studio address>}/login/oidc/callback" >&2
echo "Use the authorization code flow. Then answer these:" >&2
echo >&2

while true; do
  echo "Issuer examples: VoidAuth https://auth.example.com/oidc (the /oidc is needed), Keycloak https://kc.example.com/realms/NAME," >&2
  echo "Authentik https://auth.example.com/application/o/APP, Authelia https://auth.example.com. It is the address whose" >&2
  echo "/.well-known/openid-configuration shows JSON in a browser." >&2
  ask_required ISSUER "Issuer URL"
  ISSUER="${ISSUER%/}"
  case "$ISSUER" in https://*|http://*) break ;; *) echo "  It should start with https://" >&2 ;; esac
done
ask_required CLIENT_ID "Client ID"
ask_secret CLIENT_SECRET "Client secret"
echo "Scopes: add groups (openid profile email groups) if you use the group questions below." >&2
ask SCOPES "Scopes" "openid profile email"
ask USERNAME_CLAIM "Which claim holds the username (it must match a Studio username or proxy username)" "preferred_username"
yesno AUTO_CREATE "Create a Studio account the first time someone signs in who doesn't have one?" n
DEFAULT_ROLE="contributor"
if [ "$AUTO_CREATE" = true ]; then
  ask DEFAULT_ROLE "Role for new accounts (contributor, editor, approver, admin)" "contributor"
fi
ask GROUPS_CLAIM "Claim that lists the person's groups" "groups"
echo "Optional: provider groups that decide who may sign in and what role they get. Comma-separated; Enter to skip." >&2
ask ALLOWED_GROUPS "Only these groups may sign in (empty: anyone your provider lets through)" ""
ask EDITOR_GROUPS "Groups that make someone an editor" ""
ask APPROVER_GROUPS "Groups that make someone an approver" ""
ask ADMIN_GROUPS "Groups that make someone an admin" ""
ask BUTTON_LABEL "Text on the sign-in button" "Sign in with SSO"
echo >&2
echo "OIDC-only turns off Studio passwords completely. If your own account can't be matched by SSO," >&2
echo "you would be locked out until you change this setting back in the Docker tab." >&2
yesno ONLY "Allow sign-in ONLY through single sign-on?" n

cp "$FILE" "$FILE.bak-$(date +%Y%m%d-%H%M%S)"

add STUDIO_OIDC_ISSUER "OIDC issuer URL" false always "Address of your identity provider (VoidAuth). Single sign-on is on when this and the client ID are set." "$ISSUER"
add STUDIO_OIDC_CLIENT_ID "OIDC client ID" false always "From the app you created in your identity provider." "$CLIENT_ID"
if [ -n "$CLIENT_SECRET" ]; then
  add STUDIO_OIDC_CLIENT_SECRET "OIDC client secret" true always "From the same app. Leave empty for a public (PKCE-only) client." "$CLIENT_SECRET"
fi
add STUDIO_OIDC_SCOPES "OIDC scopes" false advanced "Space-separated." "$SCOPES"
add STUDIO_OIDC_USERNAME_CLAIM "OIDC username claim" false advanced "Matched to a Studio username or proxy username." "$USERNAME_CLAIM"
add STUDIO_OIDC_AUTO_CREATE "OIDC create accounts" false advanced "true: someone with no Studio account gets one the first time they sign in." "$AUTO_CREATE"
add STUDIO_OIDC_DEFAULT_ROLE "OIDC new account role" false advanced "Role for accounts created at sign-in: contributor, editor, approver or admin." "$DEFAULT_ROLE"
add STUDIO_OIDC_GROUPS_CLAIM "OIDC groups claim" false advanced "Claim that lists the person's groups." "$GROUPS_CLAIM"
[ -n "$ALLOWED_GROUPS" ] && add STUDIO_OIDC_ALLOWED_GROUPS "OIDC allowed groups" false advanced "Only people in one of these groups may sign in (comma-separated)." "$ALLOWED_GROUPS"
[ -n "$EDITOR_GROUPS" ] && add STUDIO_OIDC_EDITOR_GROUPS "OIDC editor groups" false advanced "Members get the editor role at sign-in (comma-separated)." "$EDITOR_GROUPS"
[ -n "$APPROVER_GROUPS" ] && add STUDIO_OIDC_APPROVER_GROUPS "OIDC approver groups" false advanced "Members get the approver role at sign-in (comma-separated)." "$APPROVER_GROUPS"
[ -n "$ADMIN_GROUPS" ] && add STUDIO_OIDC_ADMIN_GROUPS "OIDC admin groups" false advanced "Members get the admin role at sign-in (comma-separated)." "$ADMIN_GROUPS"
add STUDIO_OIDC_BUTTON_LABEL "OIDC button text" false advanced "Label on the sign-in button." "$BUTTON_LABEL"
add STUDIO_OIDC_ONLY "OIDC only (no passwords)" false always "true: turns off password sign-in entirely. Only single sign-on works." "$ONLY"

cp "$TMP" "$FILE"
rm -f "$TMP" "$TMP.n"

echo >&2
echo "Done. A backup of the old template is next to it ($FILE.bak-...)." >&2
echo "Now: Docker tab, click cgw-studio, Edit, then Apply. The container restarts with single sign-on." >&2
echo "Make sure STUDIO_BASE_URL is the https address people use, and Secure cookies is true." >&2
if [ "$ONLY" = true ]; then
  echo "OIDC-only is ON: check that your account matches (username or proxy username = your provider username)." >&2
fi
