import { useState } from 'react';
import { beginLogin } from '../api/auth';

/**
 * Interstitial sign-in screen for the cloud profile when there is no live
 * session. The "Sign in with Microsoft" button fetches the authorization URL
 * and navigates the browser to the identity provider. A click-through
 * interstitial (rather than an automatic redirect) gives cancellation and
 * callback errors a surface and avoids a redirect loop.
 */
export default function SignIn() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function handleSignIn() {
    setBusy(true);
    setError('');
    try {
      const challenge = await beginLogin();
      window.location.assign(challenge.authorization_url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start sign-in.');
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        fontFamily: 'system-ui, sans-serif',
        gap: 16,
      }}
    >
      <h1 style={{ fontSize: 24, fontWeight: 600, margin: 0 }}>CAS</h1>
      <p style={{ color: '#555', margin: 0 }}>Sign in to continue.</p>
      <button
        type="button"
        onClick={handleSignIn}
        disabled={busy}
        style={{
          padding: '10px 20px',
          fontSize: 14,
          cursor: busy ? 'default' : 'pointer',
          border: '1px solid #ccc',
          borderRadius: 4,
          background: '#fff',
        }}
      >
        {busy ? 'Redirecting…' : 'Sign in with Microsoft'}
      </button>
      {error && <p style={{ color: '#c62828', margin: 0 }}>{error}</p>}
    </div>
  );
}
