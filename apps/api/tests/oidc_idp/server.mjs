/**
 * A REAL OpenID Connect provider for the SSO integration tests.
 *
 * This is panva/node-oidc-provider (OpenID-certified) v9.13.0 — not a stub, not
 * a mock. It generates a real RSA keypair at startup and serves real
 * discovery, /authorize, /token and /jwks endpoints over HTTP.
 *
 *   PORT=9443 node server.mjs
 *   # -> http://127.0.0.1:9443/.well-known/openid-configuration
 *
 * Two endpoints exist ONLY for tests and are namespaced /test/* so they can
 * never be mistaken for protocol surface:
 *
 *   POST /test/account  {sub,email,email_verified}
 *       picks which account the next auto-login authenticates as.
 *   POST /test/mint     {claims, kid?, expired?}
 *       mints an id_token signed with the provider's REAL RSA private key.
 *       Used to produce tokens the protocol will not hand us on demand
 *       (e.g. an already-expired one). The signature is genuine.
 *
 * Login/consent are auto-completed (no HTML form). Everything on the wire is
 * the real protocol: real authorization code, real code exchange, real RS256
 * id_token, real JWKS.
 */
import { generateKeyPairSync } from 'node:crypto';
import express from 'express';
import Provider from 'oidc-provider';
import { SignJWT, exportJWK } from 'jose';

const PORT = Number(process.env.PORT || 9443);
const HOST = process.env.HOST || '127.0.0.1';
const ISSUER = process.env.ISSUER || `http://${HOST}:${PORT}`;

export const CLIENT_ID = process.env.IDP_CLIENT_ID || 'cc-enterprise-client';
export const CLIENT_SECRET =
  process.env.IDP_CLIENT_SECRET || 'integration-client-secret-value-0123456789';
const REDIRECT_URI = `${ISSUER}/cb`;
const KID = 'integration-rsa-1';

// --- real RSA keypair, generated fresh on every boot ------------------------
const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const privateJwk = { ...(await exportJWK(privateKey)), kid: KID, alg: 'RS256', use: 'sig' };
const signingKey = privateKey;

// --- the account the next auto-login resolves to ----------------------------
let ACCOUNT = { sub: 'integration-subject-1', email: 'sso-user@example.com', email_verified: true };

const configuration = {
  clients: [
    {
      client_id: CLIENT_ID,
      client_secret: CLIENT_SECRET,
      grant_types: ['authorization_code'],
      response_types: ['code'],
      redirect_uris: [REDIRECT_URI],
      // Our HttpxOidcClient authenticates by posting client_id/client_secret in
      // the token request body. That is RFC 6749 client_secret_post, which Okta,
      // Entra ID, Google and Auth0 all accept.
      token_endpoint_auth_method: 'client_secret_post',
    },
  ],
  jwks: { keys: [privateJwk] },
  // Put scope-requested claims in the id_token, the way Okta/Entra/Google do,
  // instead of requiring a userinfo round trip.
  conformIdTokenClaims: false,
  claims: {
    openid: ['sub'],
    email: ['email', 'email_verified'],
  },
  features: {
    devInteractions: { enabled: false },
  },
  cookies: { short: { secure: false }, long: { secure: false } },
  async findAccount(_ctx, id) {
    return {
      accountId: id,
      async claims() {
        return { sub: id, email: ACCOUNT.email, email_verified: ACCOUNT.email_verified };
      },
    };
  },
  interactions: { url: (_ctx, interaction) => `/interaction/${interaction.uid}` },
  ttl: { AuthorizationCode: 60, IdToken: 300, Session: 600, Interaction: 120, Grant: 600 },
};

const provider = new Provider(ISSUER, configuration);
provider.proxy = true;

const app = express();
app.use(express.json());

app.get('/test/health', (_req, res) => res.json({ ok: true, issuer: ISSUER, kid: KID }));

app.post('/test/account', (req, res) => {
  ACCOUNT = {
    sub: req.body.sub || ACCOUNT.sub,
    email: req.body.email,
    email_verified: req.body.email_verified !== false,
  };
  res.json(ACCOUNT);
});

app.post('/test/mint', async (req, res) => {
  const now = Math.floor(Date.now() / 1000);
  const claims = {
    iss: ISSUER,
    aud: CLIENT_ID,
    sub: ACCOUNT.sub,
    email: ACCOUNT.email,
    email_verified: ACCOUNT.email_verified,
    iat: now,
    exp: now + 300,
    ...(req.body.claims || {}),
  };
  const token = await new SignJWT(claims)
    .setProtectedHeader({ alg: 'RS256', kid: req.body.kid || KID, typ: 'JWT' })
    .sign(signingKey);
  res.json({ id_token: token });
});

// The registered redirect_uri. It just echoes the authorization response so a
// test following redirects can read the real code out of the final response.
app.get('/cb', (req, res) => res.json(req.query));

// Auto-complete login and consent: no human, same protocol.
app.get('/interaction/:uid', async (req, res, next) => {
  try {
    const details = await provider.interactionDetails(req, res);
    if (details.prompt.name === 'login') {
      return await provider.interactionFinished(
        req, res, { login: { accountId: ACCOUNT.sub } }, { mergeWithLastSubmission: false },
      );
    }
    if (details.prompt.name === 'consent') {
      let grantId = details.grantId;
      let grant;
      if (grantId) {
        grant = await provider.Grant.find(grantId);
      } else {
        grant = new provider.Grant({
          accountId: details.session.accountId,
          clientId: details.params.client_id,
        });
      }
      grant.addOIDCScope(details.params.scope);
      grantId = await grant.save();
      return await provider.interactionFinished(
        req, res, { consent: { grantId } }, { mergeWithLastSubmission: true },
      );
    }
    return res.status(500).json({ error: `unsupported prompt ${details.prompt.name}` });
  } catch (err) {
    return next(err);
  }
});

app.use(provider.callback());

app.listen(PORT, HOST, () => {
  // The integration fixture waits for this line.
  console.log(`oidc-provider listening on ${ISSUER}`);
});
