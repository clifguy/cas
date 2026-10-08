// SEC-008 (tests/app/bff_security_headers_tests.md): the development server
// may be framed by no page while it runs.
import config from '../../vite.config'

describe('vite development server', () => {
  it('sends frame protection on every response', () => {
    const headers = config.server?.headers ?? {}
    expect(headers['X-Frame-Options']).toBe('DENY')
    expect(headers['Content-Security-Policy']).toBe("frame-ancestors 'none'")
  })
})
