# Single sign-on (OpenID Connect)

The Studio can sign people in through VoidAuth, Authentik, Authelia, Keycloak or any OpenID Connect
provider. It uses the authorization code flow with PKCE: the browser goes to the provider, comes back to
`<STUDIO_BASE_URL>/login/oidc/callback` with a one-time code, and the Studio trades that code for the
person's identity directly with the provider over HTTPS.

Roles and accounts stay in the Studio. Signing in only proves who someone is; the Studio matches that to an
account (or creates one, if you allow it).

## 1. In the provider

Create an OIDC client/app with:

- **Redirect URL:** `https://<your studio address>/login/oidc/callback` (exactly `STUDIO_BASE_URL` plus that path)
- **Flow:** authorization code (PKCE is used automatically)
- **Scopes:** `openid profile email` (add `groups` if you want groups to set roles)
- A client ID and, normally, a client secret

## 2. Environment variables

| Variable | What it does | Default |
|---|---|---|
| `STUDIO_OIDC_ISSUER` | The provider's address, the one that serves `/.well-known/openid-configuration`. **Required.** | none |
| `STUDIO_OIDC_CLIENT_ID` | From the app you created. **Required.** | none |
| `STUDIO_OIDC_CLIENT_SECRET` | From the same app. Leave empty for a public client. | none |
| `STUDIO_OIDC_ONLY` | `true` turns off password sign-in completely. Only the SSO button works. | `false` |
| `STUDIO_OIDC_SCOPES` | Scopes to request. | `openid profile email` |
| `STUDIO_OIDC_USERNAME_CLAIM` | Claim matched to a Studio username or proxy username. | `preferred_username` |
| `STUDIO_OIDC_AUTO_CREATE` | `true`: someone with no Studio account gets one at first sign-in. | `false` |
| `STUDIO_OIDC_DEFAULT_ROLE` | Role for created accounts: `contributor`, `editor`, `approver`, `admin`. | `contributor` |
| `STUDIO_OIDC_GROUPS_CLAIM` | Claim that lists the person's groups. | `groups` |
| `STUDIO_OIDC_ALLOWED_GROUPS` | Comma-separated. If set, only people in one of these groups may sign in. | empty |
| `STUDIO_OIDC_EDITOR_GROUPS` / `_APPROVER_GROUPS` / `_ADMIN_GROUPS` | Comma-separated. Membership sets the role at every sign-in (the highest match wins). | empty |
| `STUDIO_OIDC_BUTTON_LABEL` | Text on the sign-in button. | `Sign in with SSO` |

Also make sure `STUDIO_BASE_URL` is the public `https://` address and `STUDIO_SECURE_COOKIES=true`.

## 3. How people are matched

1. The username claim against a Studio account's **proxy username**, then its **username**.
2. Otherwise a **verified** email (`email_verified` true) against the account's email.
3. Otherwise a new account, only if `STUDIO_OIDC_AUTO_CREATE=true`.

Turned-off accounts can't sign in. Every sign-in is written to the activity log with the method.

**Before turning on `STUDIO_OIDC_ONLY`:** sign in once with SSO while passwords are still on, and check that
you land on your own admin account. If your provider username differs from your Studio username, set your
**proxy username** under Settings, Users.

## 4. Using it behind a proxy

- **Replace** the proxy's sign-in: remove the forward-auth or LAN-only rule from the proxy host, and set
  `STUDIO_REQUIRE_PROXY_AUTH=false`. The Studio protects its own pages. `/m/`, `/u/` and `/e/icon/` are public
  in the app, `/mcp` is LAN-only inside the app, and `/api/ext/` needs a token. Keep the proxy's
  `return 403` for `/mcp` and `/api/ext/` anyway.
- **Both:** leave `STUDIO_REQUIRE_PROXY_AUTH=true`. The person must pass the proxy and also sign in with
  SSO, and the proxy's user must match the Studio account.

## Signing out and lock-outs

Signing out ends the Studio session only, not the provider's. If SSO breaks while `STUDIO_OIDC_ONLY=true`,
set it back to `false` in the container settings (Unraid: Docker, Edit, Apply) and sign in with a password.

## Unraid

`bash <(curl -fsSL https://raw.githubusercontent.com/ColumbiaGadgetWorks/social-media/HEAD/unraid/add-oidc-env.sh)`
asks for each value and adds the variables to your `my-cgw-studio.xml` template (with a backup). Then open the
container in the Docker tab, Edit, Apply.
