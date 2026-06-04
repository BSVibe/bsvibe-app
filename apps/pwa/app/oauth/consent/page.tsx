import { ConsentClient } from "./ConsentClient";

/** OAuth consent screen — the PWA-hosted "Allow {client_name}…" surface.
 *
 *  The backend's `GET /api/oauth/authorize` 302s the browser here with
 *  the OAuth params (response_type, client_id, redirect_uri, scope,
 *  state, code_challenge, code_challenge_method, resource — see
 *  backend/api/oauth.py `authorize_get`). We live here (NOT on the
 *  API origin) because a top-level browser navigation can't carry an
 *  `Authorization` header — the consent commit needs the Supabase
 *  session, which is reachable only on the PWA origin.
 *
 *  This file is the server-side entry. All UI lives in the client
 *  component below — the consent decision depends on the Supabase
 *  session in localStorage, which only the client can read.
 */
export default function OAuthConsentPage() {
  return <ConsentClient />;
}
