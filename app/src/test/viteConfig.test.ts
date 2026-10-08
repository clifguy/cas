// SEC-008 (tests/app/bff_security_headers_tests.md): the development server's
// configured headers forbid framing. Vite applies them to the pages, modules
// and assets it serves itself; responses it relays from the proxied API carry
// the upstream's headers instead.
import config from '../../vite.config'

describe('vite development server', () => {
  it('configures frame-protection headers for the pages it serves', () => {
    const headers = config.server?.headers ?? {}
    expect(headers['X-Frame-Options']).toBe('DENY')
    expect(headers['Content-Security-Policy']).toBe("frame-ancestors 'none'")
  })
})
